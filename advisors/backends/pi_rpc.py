"""pi-rpc backend: one `pi --mode rpc` process per active session (DESIGN.md sections 2, 4 and 9).

For the protocol, see pi's docs/rpc.md, rpc-commands.md, json.md and rpc-extension-ui.md.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .. import rules, store
from ..run import Run
from .base import Attachments, Backend, BackendError, JsonlProcess, independence_note, summarize_args

log = logging.getLogger("advisord")

# Section 9: flags always passed. --no-extensions is never passed: budget gates and providers are extensions.
FIXED_FLAGS = ["--no-context-files", "--no-skills", "--no-prompt-templates", "--no-mcp"]

# Delta events, which are not written to events.jsonl
DELTA_TYPES = {"text_delta", "thinking_delta", "toolcall_delta"}

# Notify text that means the vertex-budget extension blocked the request. The bundled extension emits
# the English text; older installs of the extension emit the Chinese text ("blocked"), so it stays.
_BUDGET_NOTIFY_MARKERS = ("blocked; the request was not sent", "已拦下")


def build_argv(pi_bin: str, advisor, backend_session_id: str, effort: str, tools: str) -> list[str]:
    argv = [pi_bin, "--mode", "rpc", "--model", advisor.model, "--thinking", effort,
            "--session-id", backend_session_id, *FIXED_FLAGS, "--append-system-prompt", independence_note()]
    if tools == "none":
        argv.append("--no-tools")
    else:
        argv += ["--tools", ",".join(advisor.read_tools)]
    if advisor.test:
        if advisor.no_extensions:
            argv.append("--no-extensions")
            argv += ["--api-key", "faux-test-key"]
        for ext in advisor.extensions:
            argv += ["-e", ext]
    # Last check of a hard rule: only test advisors on a faux model may get --no-extensions
    if "--no-extensions" in argv and not (advisor.test and advisor.provider.startswith("faux")):
        raise BackendError("refusing to start: a real advisor cannot use --no-extensions")
    return argv


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


class PiBackend(Backend):
    kind = "pi-rpc"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.p: JsonlProcess | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self._nid = 0
        self.level: str | None = None
        self.pi_session_file: str | None = None
        # Reset every turn
        self.budget_block: dict | None = None
        self.budget_notify: str | None = None
        self.last_assistant: dict | None = None
        self.ui_notes: list[str] = []

    # ---- process and requests ----

    def alive(self) -> bool:
        return self.p is not None and self.p.alive()

    def pid(self) -> int | None:
        return self.p.pid if self.p else None

    def _on_message(self, obj: dict) -> None:
        if obj.get("__eof__"):
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(BackendError("the pi process exited"))
            self.pending.clear()
            self.events.put_nowait({"type": "__eof__"})
            return
        if obj.get("type") == "response" and obj.get("id") in self.pending:
            fut = self.pending.pop(obj["id"])
            if not fut.done():
                fut.set_result(obj)
            return
        if obj.get("type") == "extension_ui_request" and obj.get("method") in ("select", "confirm", "input", "editor"):
            # A dialog waits for a reply forever and an advisor has nobody to ask, so cancel every one and log it
            log.warning("pi extension dialog cancelled: %s", str(obj)[:300])
            if self.p:
                asyncio.ensure_future(self._safe_send({"type": "extension_ui_response", "id": obj.get("id"), "cancelled": True}))
        self.events.put_nowait(obj)

    async def _safe_send(self, obj: dict) -> None:
        try:
            if self.p:
                await self.p.send(obj)
        except BackendError:
            pass

    async def request(self, cmd: dict, timeout: float = 30) -> dict:
        if not self.p:
            raise BackendError("pi is not running")
        self._nid += 1
        rid = f"adv{self._nid}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.p.send({**cmd, "id": rid})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rid, None)

    def _stderr_tail(self) -> str:
        p = store.session_dir(self.meta["session"]) / "backend.stderr.log"
        try:
            data = p.read_bytes()[-600:].decode("utf-8", "replace")
        except OSError:
            return ""
        return data.strip().splitlines()[-1] if data.strip() else ""

    async def ensure_started(self, run: Run) -> None:
        if self.alive() and self.tools_mode == run.tools:
            await self._confirm_level(run)
            return
        if self.alive():
            run.emit(f"tools changed from {self.tools_mode} to {run.tools}; restarting the backend process")
            await self.close()
        argv = build_argv(self.bins["pi"], self.advisor, self.meta["backend_session_id"], run.effort, run.tools)
        env = dict(self.env)
        env.update(self.advisor.env)
        self.p = JsonlProcess(f"pi[{self.meta['session']}]")
        self.p.on_message = self._on_message
        self.events = asyncio.Queue()
        await self.p.start(argv, self.meta["cwd"], env, store.session_dir(self.meta["session"]) / "backend.stderr.log")
        try:
            st = await self.request({"type": "get_state"}, timeout=90)
        except (BackendError, asyncio.TimeoutError) as e:
            tail = self._stderr_tail()
            await self.close()
            raise BackendError(f"pi failed to start: {e}{'; ' + tail if tail else ''}") from e
        if not st.get("success"):
            await self.close()
            raise BackendError(f"pi get_state failed: {st.get('error')}")
        data = st.get("data") or {}
        model = data.get("model") or {}
        got = f"{model.get('provider')}/{model.get('id')}"
        if got != self.advisor.model:
            await self.close()
            raise BackendError(f"pi selected the model {got}, not {self.advisor.model} (pi may not have this model)")
        self.level = data.get("thinkingLevel")
        self.pi_session_file = data.get("sessionFile")
        self.tools_mode = run.tools
        run.backend_session_id = self.meta["backend_session_id"]
        await self._confirm_level(run)

    async def _confirm_level(self, run: Run) -> None:
        if self.level != run.effort:
            r = await self.request({"type": "set_thinking_level", "level": run.effort}, timeout=15)
            if not r.get("success"):
                log.warning("set_thinking_level failed: %s", r.get("error"))
            st = await self.request({"type": "get_state"}, timeout=15)
            self.level = (st.get("data") or {}).get("thinkingLevel")
        run.effort_confirmed = self.level
        run.effort_confirm_source = "pi get_state"
        if self.level != run.effort:
            run.emit(f"effort: requested {run.effort}, pi confirmed {self.level}")

    async def close(self) -> None:
        if self.p:
            await self.p.close(grace=5)
        self.p = None
        self.tools_mode = None
        self.level = None

    def kill_now(self) -> None:
        if self.p:
            self.p.kill_now()

    # ---- one turn ----

    async def run_turn(self, run: Run) -> None:
        self.current = run
        self.budget_block = None
        self.budget_notify = None
        self.last_assistant = None
        self.ui_notes = []
        while not self.events.empty():  # events that arrived late, after the previous turn
            stale = self.events.get_nowait()
            if run.events:
                run.events.write("pi-stale", stale)
        try:
            att = Attachments(run.files)
            cmd: dict = {"type": "prompt", "message": att.message(run.prompt)}
            if att.images:
                cmd["images"] = [{"type": "image", "data": i["data"], "mimeType": i["mime"]} for i in att.images]
            resp = await self.request(cmd, timeout=60)
            if run.events:
                run.events.write("pi", {"type": "response", "command": "prompt", "success": resp.get("success"),
                                        "data": resp.get("data"), "error": resp.get("error")})
            if not resp.get("success"):
                run.backend_status = "failed"
                run.error = f"pi rejected the prompt: {resp.get('error')}"
                return
            disp = (resp.get("data") or {}).get("disposition")
            if disp == "handled":
                run.backend_status = "failed"
                run.error = "pi handled the prompt as a command (disposition handled) and did not start a new turn"
                return
            run.set_phase("thinking")
            for s in run.steers:  # steers that arrived while the run was queued
                if s["state"] == "pending":
                    await self._send_steer(run, s)
            while True:
                ev = await self.events.get()
                t = ev.get("type")
                if t == "__eof__":
                    tail = self._stderr_tail()
                    run.backend_status = "failed"
                    run.error = "the pi process exited in the middle of the turn" + (f": {tail}" if tail else "")
                    break
                self._handle(run, ev)
                if t == "agent_settled":
                    self._finish(run, ev)
                    break
        finally:
            for s in run.steers:
                if s["state"] in ("pending", "queued", "sent"):
                    s["state"] = "expired"
            self.current = None

    def _handle(self, run: Run, ev: dict) -> None:
        t = ev.get("type")
        if t == "message_update":
            a = ev.get("assistantMessageEvent") or {}
            at = a.get("type")
            run.backend_event(f"message_update/{at}")
            if at == "text_delta":
                run.add_text(a.get("delta", ""))
            elif at == "thinking_delta":
                run.add_thinking()
            elif at == "thinking_start":
                run.set_phase("thinking")
            elif at == "toolcall_start":
                run.set_phase("tool", a.get("toolName"), quiet=True)
            if at not in DELTA_TYPES and run.events:
                run.events.write("pi", {"type": t, "assistantMessageEvent": a})
            return
        run.backend_event(t or "?")
        if run.events:
            run.events.write("pi", ev)
        if t in ("agent_start", "turn_start"):
            if run.phase in ("connecting", "retrying", "queued"):
                run.set_phase("thinking")
        elif t == "tool_execution_start":
            run.add_tool(ev.get("toolName") or "?", summarize_args(ev.get("args")))
        elif t == "tool_execution_end":
            run.set_phase("thinking", quiet=True)  # whatever comes next, a tool call or the answer, prints its own line
        elif t == "message_start":
            m = ev.get("message") or {}
            if m.get("role") == "user":
                text = _message_text(m.get("content"))
                for s in run.steers:
                    if s["state"] in ("queued", "sent") and s["text"] == text:
                        s["state"] = "delivered"
                        s["delivered_at"] = round(run.elapsed(), 1)
                        run.emit("steer delivered" if s["mode"] == "steer" else "follow-up delivered")
                        break
        elif t == "message_end":
            m = ev.get("message") or {}
            if m.get("role") == "assistant":
                self.last_assistant = m
                self._add_usage(run, m.get("usage") or {})
                if m.get("stopReason") != "toolUse":
                    text = _message_text(m.get("content"))
                    if text.strip():
                        run.answer_parts.append(text)
        elif t == "auto_retry_start":
            delay = (ev.get("delayMs") or 0) / 1000
            err = (ev.get("errorMessage") or "")[:120]
            run.set_phase("retrying", f"attempt {ev.get('attempt')}/{ev.get('maxAttempts')} in {delay:.0f} s; {err}")
        elif t == "auto_retry_end":
            if ev.get("success"):
                run.set_phase("thinking")
        elif t == "entry_appended":
            entry = ev.get("entry") or {}
            if entry.get("customType") == "vertex-budget":
                data = entry.get("data") or {}
                if data.get("blocked", True):
                    self.budget_block = data
        elif t == "extension_ui_request" and ev.get("method") == "notify":
            msg = str(ev.get("message") or "")
            if any(m in msg for m in _BUDGET_NOTIFY_MARKERS):
                self.budget_notify = msg
            if ev.get("notifyType") in ("warning", "error"):
                self.ui_notes.append(msg)
                run.emit(f"extension notice: {msg[:200]}")
        elif t == "compaction_start":
            run.set_phase("thinking", "compacting context")
        elif t == "extension_error":
            log.warning("pi extension_error: %s", str(ev)[:400])

    @staticmethod
    def _add_usage(run: Run, u: dict) -> None:
        if not u:
            return
        us = run.usage
        us["input"] = us.get("input", 0) + (u.get("input") or 0)
        us["output"] = us.get("output", 0) + (u.get("output") or 0)
        us["cache_read"] = us.get("cache_read", 0) + (u.get("cacheRead") or 0)
        us["cache_write"] = us.get("cache_write", 0) + (u.get("cacheWrite") or 0)
        cost = (u.get("cost") or {}).get("total")
        if isinstance(cost, (int, float)):
            run.cost = (run.cost or 0.0) + float(cost)

    def _finish(self, run: Run, settled: dict) -> None:
        last = self.last_assistant or {}
        if self.budget_block is not None or self.budget_notify:
            run.backend_status = "blocked_budget"
            if self.budget_notify:
                run.error = self.budget_notify
            else:
                b = self.budget_block or {}
                run.error = (f"blocked by vertex-budget: budget ${b.get('limit')}, spent ${b.get('spent')}, "
                             f"this request could cost up to ${b.get('worst')}")
        elif settled.get("aborted"):
            run.backend_status = "aborted"
            run.error = last.get("errorMessage")
        elif last.get("stopReason") == "error":
            err = last.get("errorMessage") or "pi reported an error without a message"
            run.error = err
            if rules.looks_auth_required(err):
                run.backend_status = "auth_required"
            elif rules.looks_rate_limited(err):
                run.backend_status = "rate_limited"
            else:
                run.backend_status = "failed"
        elif not last:
            run.backend_status = "failed"
            run.error = "pi produced no answer in this turn"
        else:
            run.backend_status = "ok"
            if self.ui_notes:
                run.note = "; ".join(self.ui_notes)[:500]

    # ---- steer and stop ----

    async def _send_steer(self, run: Run, s: dict) -> None:
        kind = "steer" if s["mode"] == "steer" else "follow_up"
        s["state"] = "sent"
        try:
            r = await self.request({"type": kind, "message": s["text"]}, timeout=15)
        except (BackendError, asyncio.TimeoutError) as e:
            s["state"] = "expired"
            s["error"] = str(e)
            return
        if not r.get("success"):
            s["state"] = "expired"
            s["error"] = r.get("error")
            return
        disp = (r.get("data") or {}).get("disposition")
        if s["state"] == "sent":
            s["state"] = "delivered" if disp == "handled" else "queued"

    async def steer(self, run: Run, text: str, mode: str, wait_sec: float) -> str:
        s = {"text": text, "mode": mode, "at": round(run.elapsed(), 1), "state": "pending"}
        run.steers.append(s)
        if run.finished():
            s["state"] = "expired"
            return "expired"
        if self.current is not run or not self.alive():
            # Still connecting to the backend, so the prompt has not been sent: run_turn sends the steer after it
            return "queued"
        await self._send_steer(run, s)
        deadline = time.monotonic() + wait_sec
        while s["state"] in ("queued", "sent") and time.monotonic() < deadline and not run.finished():
            await asyncio.sleep(0.1)
        return s["state"]

    async def stop(self, run: Run) -> None:
        if not self.alive() or self.current is not run:
            return
        # Clear the queue first: after an abort, pi goes on to process the steers left in its queue
        # (see clear_queue in rpc-commands.md)
        try:
            await self.request({"type": "clear_queue"}, timeout=5)
        except (BackendError, asyncio.TimeoutError):
            pass
        for s in run.steers:
            if s["state"] in ("queued", "sent", "pending"):
                s["state"] = "expired"
        asyncio.ensure_future(self._abort())

    async def _abort(self) -> None:
        try:
            await self.request({"type": "abort"}, timeout=120)
        except (BackendError, asyncio.TimeoutError) as e:
            log.warning("pi abort: %s", e)
