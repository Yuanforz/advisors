"""Fallback for Codex advisors: `codex exec --json`, used when the app-server handshake fails (DESIGN.md
section 5).

This mode cannot steer, and stopping means ending the codex exec process. Each turn starts its own
process; later turns continue with `codex exec resume <thread>`.
"""

from __future__ import annotations

import asyncio
import json
import logging

from .. import rules, store
from ..run import Run
from .base import Attachments, Backend, BackendError, JsonlProcess, independence_note, summarize_args
from .codex_app import thread_config

log = logging.getLogger("advisord")


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v))


def build_exec_argv(codex_bin: str, model: str, effort: str, tools: str, cwd: str, thread_id: str | None,
                    images: list[str]) -> list[str]:
    cfg = thread_config(effort, tools)
    cfg["sandbox_mode"] = "read-only"
    cfg["approval_policy"] = "never"
    cfg["model"] = model
    cfg["developer_instructions"] = independence_note()
    argv = [codex_bin, "exec"]
    if thread_id:
        argv += ["resume", thread_id]
    argv += ["--json", "--skip-git-repo-check"]
    for k, v in cfg.items():
        argv += ["-c", f"{k}={_toml(v)}"]
    if not thread_id:
        argv += ["-C", cwd]
        for img in images:
            argv += ["-i", img]
    argv.append("-")
    return argv


class CodexExecBackend(Backend):
    kind = "codex-exec"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.p: JsonlProcess | None = None
        self.events: asyncio.Queue = asyncio.Queue()
        self.stopping = False

    def alive(self) -> bool:
        return self.p is not None and self.p.alive()

    def pid(self) -> int | None:
        return self.p.pid if self.p else None

    async def ensure_started(self, run: Run) -> None:
        run.effort_confirmed = None
        run.effort_confirm_source = "codex exec does not echo the effort"
        self.tools_mode = run.tools

    def _on_message(self, obj: dict) -> None:
        self.events.put_nowait(obj)

    async def run_turn(self, run: Run) -> None:
        self.current = run
        self.stopping = False
        att = Attachments(run.files)
        thread_id = self.meta.get("backend_session_id")
        images = [i["path"] for i in att.images]
        if thread_id and images:
            run.note = "codex exec resume cannot attach images; image attachments were skipped"
        argv = build_exec_argv(self.bins["codex"], self.advisor.model, run.effort, run.tools, self.meta["cwd"], thread_id, images)
        self.events = asyncio.Queue()
        self.p = JsonlProcess(f"codex-exec[{self.meta['session']}]")
        self.p.on_message = self._on_message
        try:
            await self.p.start(argv, self.meta["cwd"], dict(self.env), store.session_dir(self.meta["session"]) / "backend.stderr.log")
            assert self.p.proc and self.p.proc.stdin
            self.p.proc.stdin.write(att.message(run.prompt).encode())
            await self.p.proc.stdin.drain()
            self.p.proc.stdin.close()
            run.set_phase("thinking")
            error = None
            while True:
                ev = await self.events.get()
                if ev.get("__eof__"):
                    break
                t = ev.get("type", "")
                item = ev.get("item") or {}
                ity = item.get("type")
                if t == "item.updated" or "delta" in t:
                    run.backend_event(t)
                    continue
                run.backend_event(t)
                if run.events:
                    run.events.write("codex-exec", ev)
                if t == "thread.started":
                    self.meta["backend_session_id"] = ev.get("thread_id")
                    run.backend_session_id = ev.get("thread_id")
                elif t == "item.started":
                    if ity == "command_execution":
                        run.add_tool("shell", summarize_args(item.get("command")))
                    elif ity == "reasoning":
                        run.add_thinking()
                elif t == "item.completed":
                    if ity == "agent_message" and (item.get("text") or "").strip():
                        run.add_text(item["text"])
                        run.answer_parts.append(item["text"])
                    elif ity == "reasoning":
                        run.add_thinking()
                    elif ity == "command_execution":
                        run.set_phase("thinking")
                elif t == "turn.completed":
                    u = ev.get("usage") or {}
                    run.usage = {"input": u.get("input_tokens"), "cache_read": u.get("cached_input_tokens"),
                                 "output": u.get("output_tokens"), "reasoning": u.get("reasoning_output_tokens")}
                elif t in ("turn.failed", "error"):
                    error = (ev.get("error") or {}).get("message") or ev.get("message") or error
            await self.p.close(grace=5)
            rc = self.p.proc.returncode if self.p.proc else None
            if self.stopping:
                run.backend_status = "aborted"
            elif error:
                run.error = error
                if "usage limit" in error.lower():
                    run.backend_status = "quota_exhausted"
                    reset = rules.parse_codex_reset(error)
                    run.quota = {"exhausted": True, "resets_at": reset, "text": f"Codex quota exhausted, resets at {reset or 'unknown'}"}
                elif rules.looks_rate_limited(error):
                    run.backend_status = "rate_limited"
                else:
                    run.backend_status = "failed"
            elif rc not in (0, None):
                run.backend_status = "failed"
                run.error = f"codex exec exited with code {rc}"
            else:
                run.backend_status = "ok"
        except (BackendError, OSError) as e:
            run.backend_status = "failed"
            run.error = str(e)
        finally:
            self.current = None
            if self.p:
                await self.p.close(grace=2)
            self.p = None

    async def steer(self, run: Run, text: str, mode: str, wait_sec: float) -> str:
        run.steers.append({"text": text, "mode": mode, "at": round(run.elapsed(), 1), "state": "unsupported"})
        return "unsupported"

    async def stop(self, run: Run) -> None:
        if self.current is run and self.p:
            self.stopping = True
            self.p.kill_now()

    async def close(self) -> None:
        if self.p:
            await self.p.close(grace=2)
        self.p = None

    def kill_now(self) -> None:
        if self.p:
            self.p.kill_now()
