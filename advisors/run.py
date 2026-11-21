"""The live state of one turn (a run), its progress signals (section 6) and its final result."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from . import fmt, rules, store
from .registry import Advisor


class Run:
    def __init__(
        self,
        *,
        session: str,
        turn: int,
        advisor: Advisor,
        prompt: str,
        files: list[str],
        cwd: str,
        tools: str,
        effort_requested: str | None,
        effort: str,
        effort_note: str | None,
        key: str | None,
        label: str | None,
        timeout_sec: float,
        max_tool_calls: int | None,
        new_session: bool,
    ) -> None:
        self.session = session
        self.turn = turn
        self.id = f"{session}:{turn}"
        self.advisor = advisor
        self.prompt = prompt
        self.files = files
        self.cwd = cwd
        self.tools = tools
        self.effort_requested = effort_requested
        self.effort = effort
        self.effort_note = effort_note
        self.effort_confirmed: str | None = None
        self.effort_confirm_source: str | None = None
        self.key = key
        self.label = label
        self.timeout_sec = timeout_sec
        self.max_tool_calls = max_tool_calls
        self.new_session = new_session

        self.created_wall = store.now_iso()
        self.t0 = time.monotonic()
        self.started: float | None = None  # when the turn got a concurrency slot and began connecting to the backend
        self.ended: float | None = None
        self.phase = "queued"
        self.phase_detail: str | None = None
        self.queue_pos: int | None = None
        self.queue_group: str | None = None

        self.last_event: tuple[str, float] | None = None  # (backend event type, monotonic time)
        self.tool_trace: deque = deque(maxlen=5)
        self.tool_calls = 0
        self.thinking_events = 0
        self._chunks: list[str] = []
        self._text_cache: str | None = None
        self.answer_parts: list[str] = []

        self.usage: dict = {}
        self.cost: float | None = None
        self.quota: dict | None = None
        self.backend_session_id: str | None = None

        self.backend_status: str | None = None  # the result as the backend reports it
        self.error: str | None = None
        self.status: str | None = None  # the final status
        self.exit_code: int | None = None
        self.note: str | None = None
        self.stop_reason: str | None = None  # aborted / timeout
        self.stop_detail: str | None = None
        self.steers: list[dict] = []

        self.on_tool_limit = None  # set by the daemon; called as soon as tool calls exceed the limit
        self.subscribers: list[asyncio.Queue] = []
        self.done = asyncio.Event()
        self.events: store.EventLog | None = None
        self.task: asyncio.Task | None = None

    # ---- progress ----

    def elapsed(self) -> float:
        end = self.ended if self.ended is not None else time.monotonic()
        return end - self.t0

    def emit(self, msg: str) -> None:
        line = f"{fmt.PREFIX} {fmt.short(self.elapsed())} {msg}"
        for q in list(self.subscribers):
            q.put_nowait({"type": "progress", "line": line})

    def backend_event(self, kind: str) -> None:
        self.last_event = (kind, time.monotonic())

    def set_phase(self, phase: str, detail: str | None = None, quiet: bool = False) -> None:
        if phase == self.phase and detail == self.phase_detail:
            return
        self.phase = phase
        self.phase_detail = detail
        if quiet:
            return
        text = rules.PHASES.get(phase, phase)
        self.emit(f"phase: {text}" + (f" ({detail})" if detail else ""))

    def add_text(self, delta: str) -> None:
        if not delta:
            return
        self._chunks.append(delta)
        self._text_cache = None
        if self.phase != "writing":
            self.set_phase("writing")

    def add_thinking(self) -> None:
        self.thinking_events += 1
        if self.phase not in ("thinking",):
            self.set_phase("thinking")

    def add_tool(self, name: str, args_summary: str) -> None:
        self.tool_calls += 1
        self.tool_trace.append({"tool": name, "args": args_summary, "at": round(self.elapsed(), 1)})
        self.phase = "tool"
        self.phase_detail = name
        self.emit(f"tool: {name} {args_summary}".rstrip())
        if self.max_tool_calls is not None and self.tool_calls > self.max_tool_calls and self.on_tool_limit:
            self.on_tool_limit()

    @property
    def text(self) -> str:
        if self._text_cache is None:
            self._text_cache = "".join(self._chunks)
        return self._text_cache

    def last_event_desc(self) -> tuple[str | None, float | None]:
        if not self.last_event:
            return None, None
        kind, t = self.last_event
        return kind, time.monotonic() - t

    def quiet_text(self, quiet_sec: float) -> str:
        kind, age = self.last_event_desc()
        if kind is None:
            if self.phase == "queued":
                return "still queued; the backend has not started"
            return "no backend events yet: waiting for the model, progress unknown"
        if age is not None and age >= quiet_sec:
            return f"last backend event {kind}, {int(age)} s ago; waiting for the model, progress unknown"
        return f"last backend event {kind}, {int(age or 0)} s ago"

    def heartbeat_line(self, quiet_sec: float) -> str:
        bits = [f"heartbeat: phase {rules.PHASES.get(self.phase, self.phase)}"]
        if self.phase == "queued" and self.queue_pos:
            bits.append(f"queue position {self.queue_pos} in group {self.queue_group}")
        bits.append(self.quiet_text(quiet_sec))
        bits.append(f"answer text {fmt.size(len(self.text.encode()))}")
        bits.append(f"thinking events {self.thinking_events}")
        bits.append(f"tool calls {self.tool_calls}")
        return " · ".join(bits)

    # ---- status and result ----

    def finished(self) -> bool:
        return self.done.is_set()

    def snapshot(self, quiet_sec: float, tail: int = 400, since: int | None = None) -> dict:
        text = self.text
        if since is None:
            chunk = text[-tail:] if tail > 0 else ""
            start = len(text) - len(chunk)
        else:
            start = max(0, min(since, len(text)))
            limit = tail if tail > 0 else 4000
            chunk = text[start : start + limit]
        kind, age = self.last_event_desc()
        snap = {
            "run": self.id,
            "session": self.session,
            "turn": self.turn,
            "advisor": self.advisor.id,
            "label": self.label,
            "phase": self.phase,
            "phase_text": rules.PHASES.get(self.phase, self.phase),
            "phase_detail": self.phase_detail,
            "queue": {"group": self.queue_group, "position": self.queue_pos} if self.phase == "queued" else None,
            "last_backend_event": kind,
            "last_backend_event_age_sec": round(age, 1) if age is not None else None,
            "progress_note": self.quiet_text(quiet_sec),
            "tool_trace": list(self.tool_trace),
            "tool_calls": self.tool_calls,
            "text_bytes": len(text.encode()),
            "thinking_events": self.thinking_events,
            "tail": chunk,
            "tail_start": start,
            "cursor": start + len(chunk),
            "elapsed_sec": round(self.elapsed(), 1),
            "steers": list(self.steers),
            "finished": self.finished(),
        }
        if self.finished():
            snap.update(self.result(include_answer=False))
        return snap

    def final_answer(self) -> str:
        parts = [p for p in self.answer_parts if p.strip()]
        if parts:
            return "\n\n".join(parts)
        return self.text

    def turn_meta(self) -> dict[str, Any]:
        return {
            "run": self.id,
            "session": self.session,
            "turn": self.turn,
            "advisor": self.advisor.id,
            "model": self.advisor.model,
            "backend": self.advisor.backend,
            "label": self.label,
            "key": self.key,
            "cwd": self.cwd,
            "tools": self.tools,
            "files": self.files,
            "effort_requested": self.effort_requested,
            "effort": self.effort,
            "effort_note": self.effort_note,
            "effort_confirmed": self.effort_confirmed,
            "effort_confirm_source": self.effort_confirm_source,
            "status": self.status or ("queued" if self.phase == "queued" else "running"),
            "exit_code": self.exit_code,
            "error": self.error,
            "note": self.note,
            "created_at": self.created_wall,
            "queued_sec": round(self.started - self.t0, 1) if self.started else None,
            "elapsed_sec": round(self.elapsed(), 1),
            "ended_at": store.now_iso() if self.finished() else None,
            "usage": self.usage or None,
            "cost_usd": self.cost,
            "quota": self.quota,
            "tool_calls": self.tool_calls,
            "thinking_events": self.thinking_events,
            "steers": self.steers,
            "timeout_sec": self.timeout_sec,
            "max_tool_calls": self.max_tool_calls,
            "backend_session_id": self.backend_session_id,
        }

    def result(self, include_answer: bool = True) -> dict:
        r = {
            "type": "result",
            "status": self.status,
            "status_text": rules.STATUS_TEXT.get(self.status or "", ""),
            "exit_code": self.exit_code,
            "run": self.id,
            "session": self.session,
            "turn": self.turn,
            "advisor": self.advisor.id,
            "model": self.advisor.model,
            "label": self.label,
            "effort_requested": self.effort_requested,
            "effort": self.effort,
            "effort_note": self.effort_note,
            "effort_confirmed": self.effort_confirmed,
            "effort_confirm_source": self.effort_confirm_source,
            "elapsed_sec": round(self.elapsed(), 1),
            "usage": self.usage or None,
            "cost_usd": self.cost,
            "quota": self.quota,
            "error": self.error,
            "note": self.note,
            "tool_calls": self.tool_calls,
            "steers": self.steers,
            "answer_path": str(store.turn_path(self.session, self.turn, "answer.md")),
            "new_session": self.new_session,
        }
        if include_answer:
            r["answer"] = self.final_answer()
        return r
