"""codex-app-server backend: one `codex app-server` (stdio JSON-RPC) per active session.

The protocol follows the schema from `codex app-server generate-json-schema` (Codex 0.171).
Codex may also run an app-server daemon that it manages itself (--managed-daemon). This backend leaves
that daemon alone and only talks to the subprocesses it starts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
import tomllib
from pathlib import Path

from .. import __version__, rules, store
from ..run import Run
from .base import Attachments, Backend, BackendError, JsonlProcess, summarize_args

log = logging.getLogger("advisord")

# Section 9: a lean per-thread config. Turns off the MCP servers, plugins and apps (the codex_apps MCP)
# from the user's Codex config; MCP servers are turned off one by one by name, read from config.toml
# (read only). An advisor is read-only, so the multi-agent tools (spawn_agent / send_input /
# resume_agent / wait_agent / close_agent) are turned off as well: a global AGENTS.md that asks for
# aggressive delegation would otherwise make the advisor spawn sub-agents, and the tool definitions
# alone cost about 2.4k tokens per request. Codex 0.171 has no per-thread switch to skip the global
# AGENTS.md (project_doc_max_bytes = 0 only covers project files), and a separate CODEX_HOME would fork
# the login credentials, so the file itself is still loaded. Codex accepts unknown config keys
# silently, so a successful handshake does not prove a key took effect; check a real turn.
BASE_LEAN = {"features.plugins": False, "features.apps": False,
             "features.multi_agent": False, "agents.enabled": False}


_SHELLS = {"bash", "zsh", "sh", "dash", "fish"}


def shell_command_text(cmd):
    """Codex runs commands as `<shell> -lc '<command>'`. Show only the command itself."""
    try:
        argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd or [])
    except ValueError:
        return cmd
    if len(argv) == 3 and os.path.basename(str(argv[0])) in _SHELLS and argv[1] in ("-c", "-lc", "-lic", "-ic"):
        return argv[2]
    return cmd


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def lean_config() -> dict:
    cfg = dict(BASE_LEAN)
    try:
        with open(codex_home() / "config.toml", "rb") as f:
            user = tomllib.load(f)
        for name in (user.get("mcp_servers") or {}):
            cfg[f"mcp_servers.{name}.enabled"] = False
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("cannot read codex config.toml for MCP names: %s", e)
    return cfg


def thread_config(effort: str, tools: str, lean: bool = True) -> dict:
    cfg = lean_config() if lean else {}
    cfg["model_reasoning_effort"] = effort
    if tools == "none":
        # Remove the shell tool. view_image and apply_patch remain (apply_patch cannot write in the
        # read-only sandbox).
        cfg["features.shell_tool"] = False
    return cfg


# Requests from the server are always declined (section 9)
DECLINE = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "mcpServer/elicitation/request": {"action": "decline"},
    "item/tool/requestUserInput": {"answers": {}},
    "item/tool/call": {"contentItems": [], "success": False},
}


class HandshakeError(BackendError):
    """The app-server did not start or initialize failed; the advisor then falls back to codex exec."""


def _user_text(content) -> str:
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return str(content or "")


class CodexBackend(Backend):
    kind = "codex-app-server"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.p: JsonlProcess | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self._nid = 0
        self.thread_id: str | None = None
        self.thread_effort: str | None = None
        self.rate_limits: dict = {}
        self.turn_id: str | None = None
        self.interrupt_requested = False
        self.followups: list[dict] = []
        self._usage_base: dict | None = None
        self.server_requests: list[str] = []

    def alive(self) -> bool:
        return self.p is not None and self.p.alive()

    def pid(self) -> int | None:
        return self.p.pid if self.p else None

    # ---- JSON-RPC ----

    def _on_message(self, obj: dict) -> None:
        if obj.get("__eof__"):
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(BackendError("codex app-server exited"))
            self.pending.clear()
            self.events.put_nowait({"method": "__eof__"})
            return
        method = obj.get("method")
        if method is None and "id" in obj:
            fut = self.pending.pop(obj["id"], None)
            if fut and not fut.done():
                fut.set_result(obj)
            return
        if method and "id" in obj:
            asyncio.ensure_future(self._answer_server_request(obj))
        if method == "account/rateLimits/updated":
            self._merge_limits((obj.get("params") or {}).get("rateLimits") or {})
        self.events.put_nowait(obj)

    async def _answer_server_request(self, obj: dict) -> None:
        method = obj.get("method")
        self.server_requests.append(method)
        log.warning("codex server request declined: %s %s", method, str(obj.get("params"))[:300])
        if method in DECLINE:
            msg = {"id": obj["id"], "result": DECLINE[method]}
        else:
            msg = {"id": obj["id"], "error": {"code": -32601, "message": f"advisors declines {method}"}}
        try:
            if self.p:
                await self.p.send(msg)
        except BackendError:
            pass

    async def request(self, method: str, params, timeout: float = 30) -> dict:
        if not self.p:
            raise BackendError("codex app-server is not running")
        self._nid += 1
        rid = self._nid
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.p.send({"id": rid, "method": method, "params": params})
        try:
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rid, None)
        if "error" in resp:
            err = resp["error"] or {}
            raise BackendError(f"{method} failed: {err.get('message') or err}")
        return resp.get("result") or {}

    def _merge_limits(self, rl: dict) -> None:
        for k in ("primary", "secondary"):
            if rl.get(k):
                self.rate_limits[k] = rl[k]
        for k in ("limitId", "planType", "rateLimitReachedType", "credits"):
            if rl.get(k) is not None:
                self.rate_limits[k] = rl[k]

    def quota_snapshot(self) -> dict | None:
        if not self.rate_limits:
            return None
        return codex_quota_from_limits(self.rate_limits)

    # ---- lifecycle ----

    async def handshake(self, cwd: str, stderr_path: Path) -> dict:
        self.p = JsonlProcess(f"codex[{self.meta.get('session', 'probe')}]")
        self.p.on_message = self._on_message
        self.events = asyncio.Queue()
        await self.p.start([self.bins["codex"], "app-server"], cwd, dict(self.env), stderr_path)
        init = await self.request("initialize", {"clientInfo": {"name": "advisors", "title": "advisors", "version": __version__}}, 30)
        await self.p.send({"method": "initialized"})
        return init

    async def ensure_started(self, run: Run) -> None:
        if self.alive() and self.tools_mode == run.tools:
            self._confirm(run)
            return
        if self.alive():
            run.emit(f"tools changed from {self.tools_mode} to {run.tools}; restarting the backend process")
            await self.close()
        try:
            await self.handshake(self.meta["cwd"], store.session_dir(self.meta["session"]) / "backend.stderr.log")
        except (BackendError, OSError, asyncio.TimeoutError) as e:
            await self.close()
            raise HandshakeError(f"codex app-server handshake failed: {e}") from e
        try:
            params = {
                "cwd": self.meta["cwd"],
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "model": self.advisor.model,
                "config": thread_config(run.effort, run.tools),
            }
            if self.meta.get("backend_session_id"):
                res = await self.request("thread/resume", {"threadId": self.meta["backend_session_id"], **params, "excludeTurns": True}, 60)
            else:
                res = await self.request("thread/start", {**params, "ephemeral": False}, 60)
        except (BackendError, asyncio.TimeoutError) as e:
            await self.close()
            raise BackendError(f"codex app-server failed to start: {e}") from e
        th = res.get("thread") or {}
        self.thread_id = th.get("id")
        if not self.thread_id:
            await self.close()
            raise BackendError("codex returned no thread id")
        self.meta["backend_session_id"] = self.thread_id
        run.backend_session_id = self.thread_id
        self.thread_effort = res.get("reasoningEffort")
        self.tools_mode = run.tools
        if run.events:
            run.events.write("codex", {"thread": {"id": self.thread_id, "path": th.get("path")}, "model": res.get("model"),
                                       "reasoningEffort": self.thread_effort, "sandbox": res.get("sandbox"),
                                       "approvalPolicy": res.get("approvalPolicy"),
                                       "instructionSources": res.get("instructionSources")})
        self._confirm(run)

    def _confirm(self, run: Run) -> None:
        if self.thread_effort == run.effort:
            run.effort_confirmed = self.thread_effort
            run.effort_confirm_source = "echoed by codex thread/start or thread/resume"
        else:
            run.effort_confirmed = None
            run.effort_confirm_source = "Codex does not echo a per-turn effort"

    async def close(self) -> None:
        if self.p:
            await self.p.close(grace=5)
        self.p = None
        self.tools_mode = None

    def kill_now(self) -> None:
        if self.p:
            self.p.kill_now()

    # ---- one turn ----

    def _input(self, text: str, att: Attachments) -> list[dict]:
        items: list[dict] = [{"type": "text", "text": text, "text_elements": []}]
        for img in att.images:
            items.append({"type": "localImage", "path": img["path"]})
        return items

    async def _start_turn(self, run: Run, items: list[dict]) -> None:
        params = {"threadId": self.thread_id, "input": items}
        if run.effort != self.thread_effort:
            params["effort"] = run.effort
        res = await self.request("turn/start", params, 60)
        self.turn_id = (res.get("turn") or {}).get("id")
        if not self.turn_id:
            raise BackendError("turn/start returned no turn id")

    async def run_turn(self, run: Run) -> None:
        self.current = run
        self.turn_id = None
        self.interrupt_requested = False
        self.followups = []
        self._usage_base = None
        self.server_requests = []
        while not self.events.empty():
            stale = self.events.get_nowait()
            if run.events:
                run.events.write("codex-stale", stale)
        try:
            att = Attachments(run.files)
            try:
                await self._start_turn(run, self._input(att.message(run.prompt), att))
            except (BackendError, asyncio.TimeoutError) as e:
                run.backend_status = "failed"
                run.error = str(e)
                return
            run.set_phase("thinking")
            if self.interrupt_requested:
                await self._interrupt()
            for s in run.steers:
                if s["state"] == "pending":
                    await self._send_steer(run, s)
            while True:
                ev = await self.events.get()
                m = ev.get("method")
                if m == "__eof__":
                    run.backend_status = "failed"
                    run.error = "codex app-server exited in the middle of the turn"
                    break
                self._handle(run, ev)
                if m == "turn/completed":
                    turn = (ev.get("params") or {}).get("turn") or {}
                    if turn.get("id") != self.turn_id:
                        continue
                    if turn.get("status") == "completed" and self.followups and not run.stop_reason:
                        batch, self.followups = self.followups, []
                        text = "\n\n".join(s["text"] for s in batch)
                        try:
                            await self._start_turn(run, self._input(text, Attachments([])))
                        except (BackendError, asyncio.TimeoutError) as e:
                            for s in batch:
                                s["state"] = "expired"
                                s["error"] = str(e)
                            self._finish(run, turn)
                            break
                        for s in batch:
                            s["state"] = "delivered"
                            s["delivered_at"] = round(run.elapsed(), 1)
                        run.emit("follow-up delivered (another Codex turn started within this turn)")
                        continue
                    self._finish(run, turn)
                    break
        finally:
            for s in run.steers:
                if s["state"] in ("pending", "queued", "sent"):
                    s["state"] = "expired"
            q = self.quota_snapshot()
            if q and not run.quota:  # when the quota is exhausted, _finish has already written a copy with the reset time
                run.quota = q
            if self.server_requests:
                extra = f"declined server approval requests: {', '.join(self.server_requests)}"
                run.note = f"{run.note}; {extra}" if run.note else extra
            self.current = None

    def _handle(self, run: Run, ev: dict) -> None:
        m = ev.get("method") or ""
        p = ev.get("params") or {}
        if "elta" in m or m == "item/reasoning/summaryPartAdded":
            run.backend_event(m)
            if m == "item/agentMessage/delta":
                run.add_text(p.get("delta", ""))
            elif m.startswith("item/reasoning/"):
                run.add_thinking()
            return
        run.backend_event(m or "response")
        if run.events:
            run.events.write("codex", ev)
        if m == "item/started":
            it = p.get("item") or {}
            ty = it.get("type")
            if ty == "reasoning":
                run.set_phase("thinking")
            elif ty == "agentMessage":
                run.set_phase("writing")
            elif ty == "commandExecution":
                run.add_tool("shell", summarize_args(shell_command_text(it.get("command"))))
            elif ty == "mcpToolCall":
                run.add_tool(f"mcp:{it.get('server')}/{it.get('tool')}", summarize_args(it.get("arguments")))
            elif ty == "dynamicToolCall":
                run.add_tool(str(it.get("tool")), summarize_args(it.get("arguments")))
            elif ty == "webSearch":
                run.add_tool("web_search", summarize_args(it.get("query")))
            elif ty == "fileChange":
                run.add_tool("file_change", "")
            elif ty == "imageView":
                run.add_tool("view_image", summarize_args(it.get("path")))
            elif ty in ("collabAgentToolCall", "subAgentActivity"):
                run.add_tool("subagent", summarize_args(it.get("tool") or it.get("kind")))
            elif ty == "userMessage":
                text = _user_text(it.get("content"))
                for s in run.steers:
                    if s["state"] in ("queued", "sent") and s["text"] == text:
                        s["state"] = "delivered"
                        s["delivered_at"] = round(run.elapsed(), 1)
                        run.emit("steer delivered")
                        break
        elif m == "item/completed":
            it = p.get("item") or {}
            if it.get("type") == "agentMessage" and it.get("phase") != "commentary":
                if (it.get("text") or "").strip():
                    run.answer_parts.append(it["text"])
            elif it.get("type") in ("commandExecution", "mcpToolCall", "dynamicToolCall", "webSearch", "imageView"):
                run.set_phase("thinking", quiet=True)
        elif m == "thread/tokenUsage/updated":
            tu = p.get("tokenUsage") or {}
            total, last = tu.get("total") or {}, tu.get("last") or {}
            if self._usage_base is None:
                self._usage_base = {k: (total.get(k) or 0) - (last.get(k) or 0) for k in total}
            base = self._usage_base
            d = {k: (total.get(k) or 0) - base.get(k, 0) for k in total}
            run.usage = {
                "input": d.get("inputTokens"),
                "cache_read": d.get("cachedInputTokens"),
                "output": d.get("outputTokens"),
                "reasoning": d.get("reasoningOutputTokens"),
                "last_request_input": last.get("inputTokens"),
            }
        elif m == "error":
            err = p.get("error") or {}
            if p.get("willRetry"):
                run.set_phase("retrying", (err.get("message") or "")[:120])
        elif m == "model/rerouted":
            run.note = f"Codex switched to another model: {str(p)[:200]}"

    def _finish(self, run: Run, turn: dict) -> None:
        status = turn.get("status")
        err = turn.get("error") or {}
        if status == "completed":
            run.backend_status = "ok"
            return
        if status == "interrupted":
            run.backend_status = "aborted"
            run.error = err.get("message")
            return
        msg = err.get("message") or f"turn status {status}"
        info = err.get("codexErrorInfo")
        run.error = msg
        if info == "usageLimitExceeded":
            run.backend_status = "quota_exhausted"
            q = self.quota_snapshot() or {}
            reset = rules.parse_codex_reset(msg) or q.get("resets_at")
            q["exhausted"] = True
            q["resets_at"] = reset
            q["text"] = f"Codex quota exhausted, resets at {reset or 'unknown'}"
            run.quota = q
        elif info == "unauthorized":
            run.backend_status = "auth_required"
            run.error = f"{msg} (sign in to Codex again: run codex login in a terminal)"
        elif info == "rateLimitExceeded" or (isinstance(info, dict) and any(
                (v or {}).get("httpStatusCode") == 429 for v in info.values() if isinstance(v, dict))):
            run.backend_status = "rate_limited"
        else:
            run.backend_status = "failed"
            if info:
                run.error = f"{msg} (codexErrorInfo: {info})"

    # ---- steer and stop ----

    async def _send_steer(self, run: Run, s: dict) -> None:
        if s["mode"] == "follow_up":
            s["state"] = "queued"
            self.followups.append(s)
            return
        s["state"] = "sent"
        try:
            await self.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                              "input": [{"type": "text", "text": s["text"], "text_elements": []}]}, 15)
        except (BackendError, asyncio.TimeoutError) as e:
            s["state"] = "expired"
            s["error"] = str(e)
            return
        if s["state"] == "sent":
            s["state"] = "queued"

    async def steer(self, run: Run, text: str, mode: str, wait_sec: float) -> str:
        s = {"text": text, "mode": mode, "at": round(run.elapsed(), 1), "state": "pending"}
        run.steers.append(s)
        if run.finished():
            s["state"] = "expired"
            return "expired"
        if self.current is not run or not self.alive() or not self.turn_id:
            return "queued"
        await self._send_steer(run, s)
        deadline = time.monotonic() + wait_sec
        while s["state"] in ("queued", "sent") and s["mode"] == "steer" and time.monotonic() < deadline and not run.finished():
            await asyncio.sleep(0.1)
        return s["state"]

    async def stop(self, run: Run) -> None:
        if self.current is not run:
            return
        self.interrupt_requested = True
        self.followups = []
        if self.turn_id and self.alive():
            await self._interrupt()

    async def _interrupt(self) -> None:
        try:
            await self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, 15)
        except (BackendError, asyncio.TimeoutError) as e:
            # The turn may already be over (for example, it failed at once because the quota ran out), so
            # the interrupt can fail; that does not affect cleanup
            log.info("codex turn/interrupt: %s", e)


def codex_quota_from_limits(rl: dict) -> dict:
    out: dict = {"source": "codex account/rateLimits"}
    texts = []
    for key, label in (("primary", "5-hour"), ("secondary", "weekly")):
        w = rl.get(key)
        if not w:
            continue
        used = w.get("usedPercent")
        reset = rules.epoch_to_local(w.get("resetsAt"))
        out[key] = {"used_percent": used, "window_mins": w.get("windowDurationMins"), "resets_at": reset}
        if used is not None:
            texts.append(f"{label} {used}% used (resets {reset or '?'})")
    used_all = [rl.get(k, {}).get("usedPercent") for k in ("primary", "secondary") if rl.get(k)]
    if any(u is not None and u >= 100 for u in used_all):
        out["exhausted"] = True
        resets = [out[k]["resets_at"] for k in ("primary", "secondary") if k in out and (out[k]["used_percent"] or 0) >= 100]
        out["resets_at"] = max(r for r in resets if r) if any(resets) else None
    out["text"] = ", ".join(texts) if texts else "unknown"
    return out
