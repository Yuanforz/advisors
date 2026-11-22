"""advisord: the local daemon (DESIGN.md section 2).

It listens on a unix socket (0600), owns every backend process, and handles concurrency, queueing,
writing to disk and the rules. The client starts it the first time an advisors command runs. It exits
on its own after 30 minutes with no active session and no connection.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import sys
import time

from . import __version__, config, fmt, plugins, rules, store
from .backends.base import Backend, BackendError
from .backends.codex_app import CodexBackend, HandshakeError
from .backends.codex_exec import CodexExecBackend
from .backends.pi_rpc import PiBackend
from .probe import availability, capability_view, effective_efforts, probe, resolve_bins
from .quota import QuotaCache
from .registry import CODEX, PI, Advisor, Registry, RegistryError
from .run import Run

log = logging.getLogger("advisord")


class UsageError(Exception):
    """The caller's arguments are wrong; exit code 2."""


class QueueCancelled(Exception):
    pass


# ---- concurrency groups ----

class Scheduler:
    def __init__(self, limit_of) -> None:
        self.limit_of = limit_of
        self.active: dict[str, set[str]] = {}
        self.waiting: dict[str, list[tuple[Run, asyncio.Future]]] = {}

    def usage(self, group: str) -> dict:
        return {"group": group, "active": len(self.active.get(group, ())), "limit": self.limit_of(group),
                "queued": len(self.waiting.get(group, ()))}

    async def acquire(self, run: Run) -> None:
        g = run.advisor.group
        act = self.active.setdefault(g, set())
        wait = self.waiting.setdefault(g, [])
        if len(act) < self.limit_of(g) and not wait:
            act.add(run.id)
            return
        fut = asyncio.get_running_loop().create_future()
        wait.append((run, fut))
        self._positions(g)
        await fut

    def release(self, run: Run) -> None:
        g = run.advisor.group
        if run.id in self.active.get(g, set()):
            self.active[g].discard(run.id)
            self._pump(g)

    def cancel(self, run: Run) -> bool:
        g = run.advisor.group
        wait = self.waiting.get(g, [])
        for i, (r, fut) in enumerate(wait):
            if r is run:
                wait.pop(i)
                if not fut.done():
                    fut.set_exception(QueueCancelled())
                self._positions(g)
                return True
        return False

    def _pump(self, g: str) -> None:
        act = self.active.setdefault(g, set())
        wait = self.waiting.setdefault(g, [])
        while wait and len(act) < self.limit_of(g):
            r, fut = wait.pop(0)
            if fut.done():
                continue
            act.add(r.id)
            r.queue_pos = None
            fut.set_result(None)
        self._positions(g)

    def _positions(self, g: str) -> None:
        u = self.usage(g)
        for i, (r, _) in enumerate(self.waiting.get(g, [])):
            if r.queue_pos != i + 1:
                r.queue_pos = i + 1
                r.queue_group = g
                r.emit(f"queued: group {g} has {u['active']}/{u['limit']} in use, position {i + 1}")


class SessionState:
    def __init__(self, meta: dict, advisor: Advisor) -> None:
        self.meta = meta
        self.advisor = advisor
        self.backend: Backend | None = None
        self.busy: Run | None = None
        self.last_used = time.monotonic()
        self.lock = asyncio.Lock()


class Daemon:
    def __init__(self) -> None:
        self.limits = config.Limits()
        # Not strict: a broken config must not keep the daemon from starting; `list` shows the error.
        self.registry = Registry(strict=False)
        self.plugins = plugins.load()
        self.bins, self.env = resolve_bins(dict(os.environ), self.plugins.bins)
        self.sessions: dict[str, SessionState] = {}
        self.runs: dict[str, Run] = {}
        self.sched = Scheduler(self.registry.group_limit)
        self.caps: dict | None = None
        self.caps_task: asyncio.Task | None = None
        self.quota = QuotaCache(self.bins, self.env, self.limits.quota_cache_sec, self.registry, self.plugins)
        self.clients = 0
        self.last_activity = time.monotonic()
        self.t_start = time.monotonic()
        self.stopping: asyncio.Event | None = None
        self.closing = False  # once shutdown starts, new requests are refused; the client reconnects and starts a new daemon
        self.server: asyncio.base_events.Server | None = None
        self._pid_fd: int | None = None

    @property
    def advisors(self) -> dict[str, Advisor]:
        return self.registry.advisors

    # ---- startup and shutdown ----

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def _lock_pid(self, wait_sec: float = 30) -> bool:
        """Take the exclusive lock on the pid file. If the previous daemon is still exiting, wait for it (up to
        wait_sec seconds)."""
        fd = os.open(config.pid_path(), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + wait_sec
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    os.close(fd)
                    return False
                time.sleep(0.1)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._pid_fd = fd
        return True

    async def main(self) -> int:
        home = config.home()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(home, 0o700)
        if not self._lock_pid():
            print("advisord is already running", file=sys.stderr)
            return 1
        self.stopping = asyncio.Event()
        orphans = store.mark_orphans_unknown()
        if orphans:
            log.warning("marked unknown after restart: %s", orphans)
        sock = config.socket_path()
        try:
            sock.unlink()
        except FileNotFoundError:
            pass
        addr, chdir = config.socket_address()
        old = os.umask(0o177)
        try:
            if chdir:
                os.chdir(chdir)
            self.server = await asyncio.start_unix_server(self.handle, path=addr, limit=64 * 1024 * 1024)
        finally:
            os.umask(old)
        os.chmod(sock, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        loop.add_signal_handler(signal.SIGHUP, lambda: None)
        log.info("advisord %s started pid %s home %s bins %s", __version__, os.getpid(), home, self.bins)
        self.caps_task = asyncio.create_task(self._load_caps())
        janitor = asyncio.create_task(self._janitor())
        await self.stopping.wait()
        janitor.cancel()
        await self._shutdown()
        return 0

    async def _load_caps(self) -> None:
        if config.is_test_home() and os.environ.get("ADVISORS_SKIP_PROBE"):
            log.info("probe skipped (test)")
            return
        try:
            self.caps = await probe(self.bins, self.env, self.registry, self.plugins.bins)
            log.info("capabilities: versions %s reused %s codex %s pi ok %s", self.caps.get("versions"),
                     self.caps.get("reused"), self.caps.get("codex_app_server"), (self.caps.get("pi") or {}).get("ok"))
        except Exception:  # noqa: BLE001
            log.exception("probe failed")
            self.caps = None

    async def _get_caps(self, timeout: float = 90) -> dict | None:
        if self.caps_task and not self.caps_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.caps_task), timeout)
            except asyncio.TimeoutError:
                pass
        return self.caps

    async def _janitor(self) -> None:
        period = max(0.5, min(5.0, self.limits.backend_idle_sec / 2, self.limits.idle_exit_sec / 2))
        while True:
            await asyncio.sleep(period)
            now = time.monotonic()
            for ss in list(self.sessions.values()):
                if ss.backend and ss.busy is None and now - ss.last_used > self.limits.backend_idle_sec:
                    async with ss.lock:
                        if ss.backend and ss.busy is None:
                            log.info("session %s idle, closing backend pid %s", ss.meta["session"], ss.backend.pid())
                            await ss.backend.close()
                            ss.backend = None
            busy = any(not r.finished() for r in self.runs.values())
            live = any(ss.backend for ss in self.sessions.values())
            if not busy and not live and self.clients == 0 and now - self.last_activity > self.limits.idle_exit_sec:
                log.info("idle for %.0fs, exiting", now - self.last_activity)
                self._begin_close()
                assert self.stopping
                self.stopping.set()
                return

    def _begin_close(self) -> None:
        self.closing = True
        if self.server:
            self.server.close()

    async def _shutdown(self) -> None:
        log.info("shutting down")
        self._begin_close()
        active = [r for r in self.runs.values() if not r.finished()]
        for r in active:
            ss = self.sessions.get(r.session)
            if ss:
                await self.trigger_stop(ss, r, "aborted", "the daemon is shutting down")
        if active:
            await asyncio.wait([asyncio.ensure_future(r.done.wait()) for r in active], timeout=self.limits.stop_grace_sec + 5)
        for ss in self.sessions.values():
            if ss.backend:
                try:
                    await ss.backend.close()
                except Exception:  # noqa: BLE001
                    log.exception("close backend")
        try:
            config.socket_path().unlink()
        except FileNotFoundError:
            pass
        # Truncate the pid file but do not delete it. The next daemon may already have it open and be waiting
        # for the lock; if the file were deleted, that daemon would write its pid into a file with no path,
        # and a later daemon could not lock against it.
        if self._pid_fd is not None:
            try:
                os.ftruncate(self._pid_fd, 0)
            except OSError:
                pass
        log.info("bye")

    # ---- connections ----

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients += 1
        self.touch()
        try:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line)
            op = req.get("op")
            self.registry.refresh()
            if self.closing and op != "shutdown":
                await self._send(writer, {"type": "error", "error": "the daemon is shutting down", "retry": True, "exit_code": 1})
                return
            try:
                if op == "ask":
                    await self.op_ask(req, writer)
                    return
                handler = {"status": self.op_status, "steer": self.op_steer, "stop": self.op_stop,
                           "list": self.op_list, "ping": self.op_ping, "shutdown": self.op_shutdown}.get(op)
                if not handler:
                    raise UsageError(f"unknown operation {op}")
                await self._send(writer, {"type": "result", **(await handler(req))})
            except UsageError as e:
                await self._send(writer, {"type": "error", "error": str(e), "exit_code": rules.USAGE_ERROR})
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("request failed")
            try:
                await self._send(writer, {"type": "error", "error": f"internal daemon error: {e}", "exit_code": 1})
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.clients -= 1
            self.touch()
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write((json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode())
        await writer.drain()

    # ---- ask ----

    def _session(self, name: str) -> SessionState | None:
        ss = self.sessions.get(name)
        if ss:
            return ss
        if not store.valid_session_name(name):
            return None
        meta = store.load_session(name)
        if not meta:
            return None
        adv = self.advisors.get(meta.get("advisor"))
        if not adv:
            raise UsageError(f"session {name} is bound to advisor {meta.get('advisor')}, which no longer exists")
        ss = SessionState(meta, adv)
        self.sessions[name] = ss
        return ss

    async def op_ask(self, req: dict, writer: asyncio.StreamWriter) -> None:
        try:
            run, attached = await self._prepare_ask(req)
        except UsageError as e:
            await self._send(writer, {"type": "error", "error": str(e), "exit_code": rules.USAGE_ERROR})
            return
        if isinstance(run, dict):  # busy, or the turn for this idempotency key has already finished
            await self._send(writer, run)
            return
        await self._stream(run, writer, attached)

    async def _prepare_ask(self, req: dict) -> tuple[Run | dict, bool]:
        prompt = req.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise UsageError("the prompt is empty")
        files = [os.path.abspath(f) for f in req.get("files") or []]
        for f in files:
            if not os.path.isfile(f):
                raise UsageError(f"attachment not found: {f}")
        tools = req.get("tools") or "read"
        if tools not in ("read", "none"):
            raise UsageError("--tools must be read or none")
        key = req.get("key")
        advisor_id = req.get("advisor")
        session_name = req.get("session")

        if key:
            rec = store.load_key(key)
            if rec:
                digest = store.prompt_digest(advisor_id or "", session_name, prompt, files)
                if rec.get("digest") != digest:
                    raise UsageError(f"idempotency key {key} is already used by another request ({rec.get('run')})")
                existing = self.runs.get(rec["run"])
                if existing and not existing.finished():
                    return existing, True
                if existing:
                    return {**existing.result(), "replayed": True}, False
                return self._stored_result(rec["run"], replayed=True), False

        if session_name:
            if advisor_id:
                raise UsageError("cannot name an advisor when continuing a session: a session is bound to one advisor")
            ss = self._session(session_name)
            if not ss:
                raise UsageError(f"no such session: {session_name}")
            if req.get("cwd_explicit") and os.path.abspath(req["cwd"]) != ss.meta["cwd"]:
                raise UsageError(f"the working directory of session {session_name} is fixed at {ss.meta['cwd']} and cannot be changed")
            if ss.busy is not None:
                b = ss.busy
                return {"type": "result", "status": "busy", "status_text": rules.STATUS_TEXT["busy"],
                        "exit_code": rules.EXIT_CODES["busy"], "run": b.id, "session": ss.meta["session"],
                        "turn": b.turn, "advisor": ss.advisor.id,
                        "error": f"the session is running turn {b.turn} ({b.id}). To steer it, use advisors steer {b.id} …; "
                                 f"to ask again, wait for it to finish."}, False
            new = False
        else:
            if not advisor_id:
                raise UsageError("name an advisor, or continue a session with --session")
            adv = self.advisors.get(advisor_id)
            if not adv:
                raise UsageError(f"no advisor {advisor_id}; available: {', '.join(self.advisors)}")
            cwd = os.path.abspath(req.get("cwd") or os.getcwd())
            if not os.path.isdir(cwd):
                raise UsageError(f"working directory not found: {cwd}")
            name = store.new_session_name(adv.id)
            now = store.now_iso()
            meta = {"session": name, "advisor": adv.id, "model": adv.model, "backend": adv.backend, "cwd": cwd,
                    "created_at": now, "updated_at": now, "turns": 0, "label": req.get("label"),
                    "backend_session_id": f"advisors-{name}" if adv.backend == PI else None,
                    "usage_total": {}, "cost_total_usd": 0.0, "busy_run": None}
            store.save_session(meta)
            ss = SessionState(meta, adv)
            self.sessions[name] = ss
            new = True

        adv = ss.advisor
        caps = self.caps if (self.caps_task and self.caps_task.done()) else None
        efforts, dropped = effective_efforts(adv, caps)
        try:
            effort, note = rules.resolve_effort(adv, req.get("effort"), efforts)
        except ValueError as e:
            raise UsageError(str(e)) from e
        timeout, max_tools = rules.turn_limits(adv, self.limits, req.get("timeout"))
        n = int(ss.meta.get("turns") or 0) + 1
        run = Run(session=ss.meta["session"], turn=n, advisor=adv, prompt=prompt, files=files, cwd=ss.meta["cwd"],
                  tools=tools, effort_requested=req.get("effort"), effort=effort, effort_note=note, key=key,
                  label=req.get("label"), timeout_sec=timeout, max_tool_calls=max_tools, new_session=new)
        ss.meta["turns"] = n
        ss.meta["busy_run"] = run.id
        ss.meta["updated_at"] = store.now_iso()
        ss.busy = run
        self.runs[run.id] = run
        self._trim_runs()
        prompt_md = prompt
        if files:
            prompt_md += "\n\n<!-- attachments -->\n" + "\n".join(f"- {f}" for f in files)
        store.atomic_write(store.turn_path(run.session, n, "prompt.md"), prompt_md + "\n")
        store.save_turn_meta(run.session, n, run.turn_meta())
        store.save_session(ss.meta)
        if key:
            store.save_key(key, run.id, store.prompt_digest(advisor_id or "", session_name, prompt, files))
        run.events = store.EventLog(run.session, n)
        if max_tools is not None:
            run.on_tool_limit = lambda: asyncio.ensure_future(self.trigger_stop(
                ss, run, "timeout", f"circuit breaker: a paid advisor made more than {max_tools} tool calls in one turn"))
        run.task = asyncio.create_task(self._execute(ss, run))
        return run, False

    def _trim_runs(self) -> None:
        done = [r for r in self.runs.values() if r.finished()]
        for r in done[:-200]:
            self.runs.pop(r.id, None)

    def _accepted(self, run: Run, attached: bool) -> dict:
        return {"type": "accepted", "run": run.id, "session": run.session, "turn": run.turn,
                "advisor": run.advisor.id, "model": run.advisor.model, "new_session": run.new_session,
                "attached": attached, "effort": run.effort, "effort_requested": run.effort_requested,
                "effort_note": run.effort_note, "tools": run.tools, "cwd": run.cwd, "timeout_sec": run.timeout_sec,
                "max_tool_calls": run.max_tool_calls, "label": run.label}

    async def _stream(self, run: Run, writer: asyncio.StreamWriter, attached: bool) -> None:
        q: asyncio.Queue = asyncio.Queue()
        run.subscribers.append(q)
        hb = None
        try:
            await self._send(writer, self._accepted(run, attached))
            if run.finished():
                await self._send(writer, run.result())
                return

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(self.limits.heartbeat_sec)
                    if not run.finished():
                        q.put_nowait({"type": "progress",
                                      "line": f"{fmt.PREFIX} {fmt.short(run.elapsed())} {run.heartbeat_line(self.limits.quiet_sec)}"})

            hb = asyncio.create_task(heartbeat())
            while True:
                msg = await q.get()
                await self._send(writer, msg)
                if msg.get("type") == "result":
                    return
        except (ConnectionResetError, BrokenPipeError):
            log.info("client of %s disconnected; run continues", run.id)
        finally:
            if hb:
                hb.cancel()
            if q in run.subscribers:
                run.subscribers.remove(q)

    def _make_backend(self, ss: SessionState) -> Backend:
        adv = ss.advisor
        if adv.backend == PI:
            return PiBackend(adv, ss.meta, self.env, self.bins)
        if adv.backend == CODEX:
            c = (self.caps or {}).get("codex_app_server") or {}
            forced = config.is_test_home() and os.environ.get("ADVISORS_FORCE_CODEX_EXEC")
            if forced or (c and not c.get("ok", True)):
                return CodexExecBackend(adv, ss.meta, self.env, self.bins)
            return CodexBackend(adv, ss.meta, self.env, self.bins)
        raise BackendError(f"unknown backend {adv.backend}")

    async def _execute(self, ss: SessionState, run: Run) -> None:
        watchdog = None
        try:
            try:
                await self.sched.acquire(run)
            except QueueCancelled:
                run.backend_status = "aborted"
                return
            run.started = time.monotonic()
            run.set_phase("connecting")
            # The turn's meta.json said "queued" until now; readers of the files should see that it is running.
            store.save_turn_meta(run.session, run.turn, run.turn_meta())
            watchdog = asyncio.create_task(self._watchdog(ss, run))
            async with ss.lock:
                if ss.backend and not ss.backend.alive():
                    await ss.backend.close()
                    ss.backend = None
                if ss.backend is None:
                    ss.backend = self._make_backend(ss)
                try:
                    await ss.backend.ensure_started(run)
                except HandshakeError as e:
                    log.warning("codex handshake failed, falling back to exec: %s", e)
                    if self.caps is not None:
                        self.caps["codex_app_server"] = {"ok": False, "error": str(e)}
                    run.emit(f"Codex app-server handshake failed; falling back to codex exec (no steering): {e}")
                    ss.backend = CodexExecBackend(ss.advisor, ss.meta, self.env, self.bins)
                    await ss.backend.ensure_started(run)
                store.save_session(ss.meta)
                if run.stop_reason:
                    run.backend_status = "aborted"
                else:
                    await ss.backend.run_turn(run)
        except BackendError as e:
            run.backend_status = "failed"
            run.error = str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("run %s crashed", run.id)
            run.backend_status = "failed"
            run.error = f"internal daemon error: {e!r}"
        finally:
            if watchdog:
                watchdog.cancel()
            self.sched.release(run)
            await self._finalize(ss, run)

    async def _watchdog(self, ss: SessionState, run: Run) -> None:
        while not run.finished():
            await asyncio.sleep(0.5)
            if run.stop_reason or run.started is None:
                continue
            now = time.monotonic()
            if now - run.started > run.timeout_sec:
                await self.trigger_stop(ss, run, "timeout", f"exceeded the turn time limit of {fmt.dur(run.timeout_sec)}")
            elif now - (run.last_event[1] if run.last_event else run.started) > self.limits.stall_sec:
                await self.trigger_stop(ss, run, "timeout", f"no events from the backend for {fmt.dur(self.limits.stall_sec)}")
            elif run.max_tool_calls is not None and run.tool_calls > run.max_tool_calls:
                await self.trigger_stop(ss, run, "timeout", f"circuit breaker: a paid advisor made more than {run.max_tool_calls} tool calls in one turn")

    async def trigger_stop(self, ss: SessionState, run: Run, reason: str, detail: str) -> None:
        if run.finished() or run.stop_reason:
            return
        run.stop_reason = reason
        run.stop_detail = detail
        run.emit(f"stopping: {detail}")
        if self.sched.cancel(run):
            return
        if ss.backend:
            try:
                await ss.backend.stop(run)
            except Exception:  # noqa: BLE001
                log.exception("backend stop")
        asyncio.create_task(self._force_after_grace(ss, run))

    async def _force_after_grace(self, ss: SessionState, run: Run) -> None:
        await asyncio.sleep(self.limits.stop_grace_sec)
        if not run.finished() and ss.backend:
            log.warning("run %s did not stop in %.0fs; killing backend pid %s", run.id, self.limits.stop_grace_sec, ss.backend.pid())
            run.emit("the backend did not stop in time; killing the backend process")
            ss.backend.kill_now()

    async def _finalize(self, ss: SessionState, run: Run) -> None:
        adv = run.advisor
        bs = run.backend_status or "failed"
        if run.stop_reason == "timeout":
            status = "timeout"
            if run.error and bs != "aborted":
                run.note = f"last backend error: {run.error}"
            run.error = run.stop_detail
        elif run.stop_reason == "aborted":
            status = "aborted"
            run.error = run.stop_detail
        elif bs == "aborted":
            status = "failed"
            run.error = run.error or "the backend aborted this turn on its own (not a stop from advisors; possibly a pi extension aborted it)"
        else:
            status = bs
        # Quota: after a failed turn (a 429 above all) look once more, to tell "quota exhausted" from other
        # failures. Codex reports its quota in the event stream, so that snapshot is only remembered.
        # vertex-budget is a read-only estimate: shown, never used to reclassify the turn.
        if adv.quota == "codex":
            if run.quota:
                self.quota.note_codex(run.quota)
        elif adv.quota:
            fresh = status in ("failed", "rate_limited")
            try:
                q = await asyncio.wait_for(self.quota.for_advisor(adv, fresh=fresh), 200)
            except asyncio.TimeoutError:
                q = {"text": "quota check timed out"}
            if q:
                run.quota = q
                if q.get("exhausted") and status in ("failed", "rate_limited") and adv.quota != "vertex-budget":
                    status = "quota_exhausted"
        if status == "auth_required":
            run.note = ((run.note + "; ") if run.note else "") + "the user has to sign in again; advisors will not retry automatically"
        run.status = status
        run.exit_code = rules.EXIT_CODES[status]
        run.ended = time.monotonic()
        answer = run.final_answer()
        try:
            store.atomic_write(store.turn_path(run.session, run.turn, "answer.md"), answer + ("\n" if answer else ""))
        except OSError:
            log.exception("write answer")
        run.done.set()
        store.save_turn_meta(run.session, run.turn, run.turn_meta())
        if run.events:
            run.events.close()
        m = ss.meta
        m["busy_run"] = None
        m["updated_at"] = store.now_iso()
        m["last_status"] = status
        tot = m.setdefault("usage_total", {})
        for k, v in (run.usage or {}).items():
            if isinstance(v, (int, float)) and k != "last_request_input":
                tot[k] = tot.get(k, 0) + v
        if run.cost:
            m["cost_total_usd"] = round(float(m.get("cost_total_usd") or 0) + run.cost, 6)
        store.save_session(m)
        ss.busy = None
        ss.last_used = time.monotonic()
        self.touch()
        tail = f": {run.error}" if run.error and status != "ok" else ""
        run.emit(f"finished: {status} ({rules.STATUS_TEXT.get(status, '')}) in {fmt.dur(run.elapsed())}{tail[:300]}")
        result = run.result()
        for q in list(run.subscribers):
            q.put_nowait(result)
        log.info("run %s finished: %s %.1fs", run.id, status, run.elapsed())

    def _stored_result(self, run_id: str, replayed: bool = False) -> dict:
        session, _, n = run_id.rpartition(":")
        tm = store.load_turn_meta(session, int(n)) if n.isdigit() else None
        if not tm:
            return {"type": "result", "status": "unknown", "exit_code": rules.EXIT_CODES["unknown"], "run": run_id,
                    "session": session, "error": "no record of this turn on disk; the result is unknown"}
        status = tm.get("status")
        if status in (None, "queued", "running"):
            status = "unknown"
        answer = store.read_text(store.turn_path(session, int(n), "answer.md")) or ""
        r = {"type": "result", **{k: tm.get(k) for k in (
            "run", "session", "turn", "advisor", "model", "label", "effort_requested", "effort", "effort_note",
            "effort_confirmed", "effort_confirm_source", "elapsed_sec", "usage", "cost_usd", "quota", "error",
            "note", "tool_calls", "steers")}}
        r.update({"status": status, "status_text": rules.STATUS_TEXT.get(status, ""),
                  "exit_code": rules.EXIT_CODES.get(status, 9), "answer": answer.rstrip("\n"),
                  "answer_path": str(store.turn_path(session, int(n), "answer.md")), "replayed": replayed})
        return r

    # ---- other commands ----

    def _find_run(self, target: str) -> tuple[SessionState | None, Run | None, str | None]:
        """target is a RUN (session:turn) or a SESSION. Returns (session, the run in memory, the run id on disk)."""
        if not target:
            raise UsageError("give a session name or a run id")
        if ":" in target:
            name, _, n = target.rpartition(":")
            if not n.isdigit():
                raise UsageError(f"a run id has the form session:turn, for example {name}:1")
            ss = self._session(name)
            if not ss:
                raise UsageError(f"no such session: {name}")
            run = self.runs.get(target)
            if not run and not store.load_turn_meta(name, int(n)):
                raise UsageError(f"no such turn: {target}")
            return ss, run, target
        ss = self._session(target)
        if not ss:
            raise UsageError(f"no such session or run: {target}")
        if ss.busy:
            return ss, ss.busy, ss.busy.id
        nums = store.turn_numbers(target)
        if not nums:
            return ss, None, None
        rid = f"{target}:{nums[-1]}"
        return ss, self.runs.get(rid), rid

    async def op_status(self, req: dict) -> dict:
        target = req.get("target")
        tail = int(req.get("tail") if req.get("tail") is not None else 400)
        since = req.get("since")
        if not target:
            active = [r.snapshot(self.limits.quiet_sec, tail=0) for r in self.runs.values() if not r.finished()]
            groups = [self.sched.usage(g) for g in sorted({a.group for a in self.advisors.values()})]
            recent = []
            for m in store.list_sessions()[:10]:
                recent.append({k: m.get(k) for k in ("session", "advisor", "turns", "last_status", "updated_at", "label",
                                                      "cost_total_usd")})
            return {"kind": "overview", "daemon": {"pid": os.getpid(), "version": __version__,
                                                   "uptime_sec": round(time.monotonic() - self.t_start, 1),
                                                   "home": str(config.home())},
                    "active": active, "groups": groups, "recent_sessions": recent,
                    "live_backends": [{"session": n, "pid": ss.backend.pid(), "kind": ss.backend.kind}
                                      for n, ss in self.sessions.items() if ss.backend and ss.backend.alive()]}
        ss, run, rid = self._find_run(target)
        if run:
            return {"kind": "run", **run.snapshot(self.limits.quiet_sec, tail=tail, since=since)}
        if not rid:
            return {"kind": "session", "session": ss.meta["session"], "advisor": ss.advisor.id, "turns": 0,
                    "note": "this session has no turns yet"}
        r = self._stored_result(rid)
        text = r.pop("answer", "") or ""
        if since is None:
            chunk = text[-tail:] if tail > 0 else ""
            start = len(text) - len(chunk)
        else:
            start = max(0, min(int(since), len(text)))
            chunk = text[start: start + (tail if tail > 0 else 4000)]
        r.pop("type", None)
        return {"kind": "run", "finished": True, "phase": "done", "phase_text": rules.PHASES["done"], "tail": chunk,
                "tail_start": start, "cursor": start + len(chunk), "text_bytes": len(text.encode()), **r}

    async def op_steer(self, req: dict) -> dict:
        text = req.get("text")
        mode = req.get("mode") or "steer"
        if mode not in ("steer", "follow_up"):
            raise UsageError("--mode must be steer or follow_up")
        if not text:
            raise UsageError("the steer text is empty")
        ss, run, rid = self._find_run(req.get("target") or "")
        if run is None or run.finished():
            return {"run": rid, "disposition": "expired", "note": "this turn has already finished. To start a new turn, use advisors ask --session"}
        if run.stop_reason:
            return {"run": run.id, "disposition": "expired", "note": "this turn is stopping"}
        if run.started is None or ss.backend is None:
            run.steers.append({"text": text, "mode": mode, "at": round(run.elapsed(), 1), "state": "pending"})
            disp = "queued"
        else:
            disp = await ss.backend.steer(run, text, mode, self.limits.steer_wait_sec)
        notes = {"delivered": "delivered; the backend saw this message during the turn",
                 "queued": "queued; it will be delivered at the next gap (for pi, between tool calls; with no tool calls, "
                           "after the current answer is written)" if mode == "steer"
                 else "queued; it will be sent after this turn's answer is finished",
                 "expired": "this turn has already finished; not sent",
                 "unsupported": "this backend does not support steering right now"}
        return {"run": run.id, "disposition": disp, "mode": mode, "note": notes.get(disp, "")}

    async def op_stop(self, req: dict) -> dict:
        ss, run, rid = self._find_run(req.get("target") or "")
        if run is None:
            if not rid:
                return {"note": "this session has no running turn"}
            r = self._stored_result(rid)
            r.pop("answer", None)
            r.pop("type", None)
            return {**r, "already_finished": True}
        if not run.finished():
            detail = "the ask process received a signal; treating it as a stop" if req.get("nowait") else "stop requested by the caller"
            await self.trigger_stop(ss, run, "aborted", detail)
            if req.get("nowait"):
                return {"run": run.id, "status": None, "note": "stop sent"}
            try:
                await asyncio.wait_for(run.done.wait(), self.limits.stop_grace_sec + 10)
            except asyncio.TimeoutError:
                pass
            already = False
        else:
            already = True
        r = run.result(include_answer=False) if run.finished() else {"run": run.id, "status": None,
                                                                       "note": "stop sent; the backend has not stopped yet"}
        r.pop("type", None)
        return {**r, "already_finished": already}

    async def op_list(self, req: dict) -> dict:
        caps = await self._get_caps()
        ids = list(self.advisors)
        quotas = await asyncio.gather(*(asyncio.wait_for(self.quota.for_advisor(self.advisors[i]), 60) for i in ids),
                                      return_exceptions=True)
        out = []
        for aid, q in zip(ids, quotas):
            a = self.advisors[aid]
            efforts, dropped = effective_efforts(a, caps)
            ok, why = availability(a, caps)
            out.append({
                "id": a.id, "model": a.model, "backend": a.backend, "billing": a.billing, "paid": a.paid,
                "description": a.description, "efforts": list(efforts), "default_effort": a.default_effort,
                "efforts_dropped": dropped, "capabilities": capability_view(a, caps), "available": ok,
                "availability": why, "quota": q if isinstance(q, dict) else ({"text": f"quota lookup failed: {q!r}"} if q else None),
                "group": self.sched.usage(a.group),
                "breaker": {"max_tool_calls": self.limits.paid_max_tool_calls, "max_sec": self.limits.paid_max_sec} if a.paid else None,
            })
        return {"advisors": out, "config": {"path": str(self.registry.path), "error": self.registry.error},
                "plugins": {"dir": str(plugins.plugins_dir()), "loaded": self.plugins.loaded, "errors": self.plugins.errors},
                "versions": (caps or {}).get("versions"), "probed_at": (caps or {}).get("probed_at"),
                "codex_app_server": (caps or {}).get("codex_app_server"),
                "limits": {"turn_timeout_sec": self.limits.turn_timeout_sec, "stall_sec": self.limits.stall_sec,
                           "backend_idle_sec": self.limits.backend_idle_sec, "idle_exit_sec": self.limits.idle_exit_sec}}

    async def op_ping(self, req: dict) -> dict:
        return {"pid": os.getpid(), "version": __version__, "home": str(config.home())}

    async def op_shutdown(self, req: dict) -> dict:
        active = [r.id for r in self.runs.values() if not r.finished()]
        if active and not req.get("force"):
            return {"stopped": False, "note": f"turns still running: {', '.join(active)}; --force stops them"}
        self._begin_close()
        assert self.stopping
        asyncio.get_running_loop().call_later(0.1, self.stopping.set)
        return {"stopped": True, "pid": os.getpid()}


def main() -> int:
    home = config.home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lp = config.log_path()
    try:
        if lp.exists() and lp.stat().st_size > 5 * 1024 * 1024:
            lp.replace(lp.with_suffix(".log.1"))
    except OSError:
        pass
    logging.basicConfig(filename=str(lp), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        d = Daemon()
    except RegistryError as e:
        log.error("registry: %s", e)
        print(f"advisor config error: {e}", file=sys.stderr)
        return 1
    return asyncio.run(d.main())


if __name__ == "__main__":
    sys.exit(main())
