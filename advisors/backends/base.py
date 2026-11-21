"""Shared parts of the backend adapters: the JSONL subprocess, attachment handling and the interface."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any, Callable

from .. import config
from ..registry import Advisor
from ..run import Run

log = logging.getLogger("advisord")

LINE_LIMIT = 256 * 1024 * 1024  # pi's agent_end carries every message, so one line can be very long


class BackendError(Exception):
    pass


class JsonlProcess:
    """A subprocess that exchanges JSON lines over stdin/stdout. It runs in its own process group, and closing
    it cleans up the whole group."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.proc: asyncio.subprocess.Process | None = None
        self.on_message: Callable[[dict], None] | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_f = None
        self.eof = asyncio.Event()

    async def start(self, argv: list[str], cwd: str, env: dict, stderr_path: Path) -> None:
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_f = open(stderr_path, "ab")
        self._stderr_f.write(f"\n=== start {' '.join(argv)}\n".encode())
        self._stderr_f.flush()
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr_f,
            limit=LINE_LIMIT,
            start_new_session=True,
        )
        log.info("%s: started pid %s: %s", self.name, self.proc.pid, " ".join(argv))
        self._reader = asyncio.create_task(self._read_loop())

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and not self.eof.is_set()

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    log.warning("%s: non-JSON stdout line: %r", self.name, line[:200])
                    continue
                if self.on_message and isinstance(obj, dict):
                    try:
                        self.on_message(obj)
                    except Exception:  # noqa: BLE001 - one bad message must not stop the read loop
                        log.exception("%s: on_message failed", self.name)
        except Exception:  # noqa: BLE001
            log.exception("%s: reader crashed", self.name)
        finally:
            self.eof.set()
            if self.on_message:
                try:
                    self.on_message({"__eof__": True})
                except Exception:  # noqa: BLE001
                    log.exception("%s: eof handler failed", self.name)

    async def send(self, obj: dict) -> None:
        if not self.alive() or not self.proc or not self.proc.stdin:
            raise BackendError(f"{self.name} has exited")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        self.proc.stdin.write(data)
        try:
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise BackendError(f"{self.name}: stdin is broken: {e}") from e

    async def close(self, grace: float = 5.0) -> None:
        """Close stdin so it exits on its own and wait `grace` seconds. If it is still running, SIGTERM the
        whole process group, then SIGKILL."""
        p = self.proc
        if p is None:
            return
        if p.returncode is None:
            try:
                if p.stdin and not p.stdin.is_closing():
                    p.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(p.wait(), grace)
            except asyncio.TimeoutError:
                self._signal_group(signal.SIGTERM)
                try:
                    await asyncio.wait_for(p.wait(), 3)
                except asyncio.TimeoutError:
                    self._signal_group(signal.SIGKILL)
                    try:
                        await asyncio.wait_for(p.wait(), 3)
                    except asyncio.TimeoutError:
                        log.error("%s: pid %s did not die", self.name, p.pid)
        # The group may still hold grandchildren, such as processes a pi extension started, so signal it
        # even after the main process has exited
        self._signal_group(signal.SIGTERM, quiet=True)
        if self._reader:
            try:
                await asyncio.wait_for(self._reader, 3)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self._reader.cancel()
        if self._stderr_f:
            try:
                self._stderr_f.close()
            except OSError:
                pass
        log.info("%s: closed (returncode %s)", self.name, p.returncode)

    def kill_now(self) -> None:
        self._signal_group(signal.SIGKILL, quiet=True)

    def _signal_group(self, sig: int, quiet: bool = False) -> None:
        p = self.proc
        if p is None:
            return
        try:
            os.killpg(p.pid, sig)  # start_new_session=True, so the process group id is the pid
        except ProcessLookupError:
            pass
        except PermissionError:
            if not quiet:
                log.warning("%s: cannot signal group %s", self.name, p.pid)


# ---- attachments ----

_IMAGE_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def image_mime(head: bytes) -> str | None:
    for magic, mime in _IMAGE_MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class Attachments:
    """The contents of --files. Text files are wrapped as <file name="…">…</file>, the same format as pi's
    @file, and images are listed separately."""

    def __init__(self, paths: list[str]) -> None:
        self.text = ""
        self.images: list[dict] = []  # {"path", "mime", "data"(base64)}
        for p in paths:
            path = Path(p)
            data = path.read_bytes()
            if not data:
                continue  # pi skips empty files too
            mime = image_mime(data[:16])
            if mime:
                self.images.append({"path": str(path), "mime": mime, "data": base64.b64encode(data).decode()})
                self.text += f'<file name="{path}"></file>\n'
            else:
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError as e:
                    raise BackendError(f"attachment is neither UTF-8 text nor a supported image: {path}") from e
                if content.startswith("﻿"):
                    content = content[1:]
                self.text += f'<file name="{path}">\n{content}\n</file>\n'

    def message(self, prompt: str) -> str:
        return f"{self.text}{prompt}" if self.text else prompt


def _tilde(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s


def independence_note() -> str:
    """Instructions every advisor gets (pi: appended system prompt; Codex: developer instructions).

    A soft rule by design: advisors keep their tools, and are asked not to look at other reviewers' records.
    """
    pi_dir = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser()
    codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    records = ", ".join(_tilde(p) for p in (config.home(), codex_dir / "sessions", pi_dir / "sessions"))
    return ("You are being consulted as an independent advisor by another agent, and other models may get the same "
            "question. Check claims yourself with the tools you have instead of taking them on trust. Keep your view "
            f"independent: do not open other reviewers' records, which are kept in {records}.")


def summarize_args(args: Any, limit: int = 160) -> str:
    """Summarize tool arguments as key=value, truncating long values."""
    if args is None:
        return ""
    if isinstance(args, str):
        s = args
    elif isinstance(args, dict):
        parts = []
        for k, v in args.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            v = str(v).replace("\n", " ")
            if len(v) > 80:
                v = v[:77] + "…"
            parts.append(f"{k}={v}")
        s = " ".join(parts)
    else:
        s = json.dumps(args, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


class Backend:
    """The backend of one session. The daemon makes sure only one run uses it at a time."""

    kind = "base"

    def __init__(self, advisor: Advisor, session_meta: dict, home_env: dict, bins: dict, quota=None) -> None:
        self.advisor = advisor
        self.meta = session_meta
        self.env = home_env
        self.bins = bins
        self.quota = quota
        self.tools_mode: str | None = None
        self.current: Run | None = None

    def alive(self) -> bool:
        raise NotImplementedError

    def pid(self) -> int | None:
        return None

    async def ensure_started(self, run: Run) -> None:
        raise NotImplementedError

    async def run_turn(self, run: Run) -> None:
        raise NotImplementedError

    async def steer(self, run: Run, text: str, mode: str, wait_sec: float) -> str:
        return "unsupported"

    async def stop(self, run: Run) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    def kill_now(self) -> None:
        pass

    def capabilities(self) -> dict:
        return {}
