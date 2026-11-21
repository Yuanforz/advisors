"""Paths and tunable limits.

The data directory defaults to ~/.advisors; set ADVISORS_HOME to use another one (the tests always do).
The time limits below accept environment overrides only when the data directory is not the default one.
Tests need to shrink 30 minutes to a few seconds, but in normal use a caller must not be able to get around
the circuit breaker and the timeouts by setting an environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = Path.home() / ".advisors"


def home() -> Path:
    raw = os.environ.get("ADVISORS_HOME")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_HOME


def is_test_home() -> bool:
    try:
        return home() != DEFAULT_HOME.resolve()
    except OSError:
        return home() != DEFAULT_HOME


def _knob(name: str, default: float) -> float:
    if is_test_home():
        raw = os.environ.get(name)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return default


def socket_path() -> Path:
    return home() / "advisord.sock"


SUN_PATH_MAX = 100  # sun_path is limited to 104 bytes on macOS; leave some margin


def socket_address() -> tuple[str, Path | None]:
    """Return (the address to bind / connect to, the directory to switch to while doing so, or None).

    When the absolute path is too long (common for test directories), use the relative path
    "advisord.sock" inside the data directory.
    """
    p = socket_path()
    if len(str(p).encode()) <= SUN_PATH_MAX:
        return str(p), None
    return p.name, p.parent


def pid_path() -> Path:
    return home() / "advisord.pid"


def log_path() -> Path:
    return home() / "advisord.log"


def sessions_dir() -> Path:
    return home() / "sessions"


def keys_dir() -> Path:
    return home() / "keys"


def caps_path() -> Path:
    return home() / "capabilities.json"


class Limits:
    """Reads the environment once; the daemon creates one at startup."""

    def __init__(self) -> None:
        # How long the daemon stays up with no active session before it exits (section 2: 30 minutes)
        self.idle_exit_sec = _knob("ADVISORS_IDLE_EXIT_SEC", 1800)
        # How long a session can sit idle before its backend process is closed (section 2: 10 minutes)
        self.backend_idle_sec = _knob("ADVISORS_BACKEND_IDLE_SEC", 600)
        # Hard timeout per turn (section 7: 30 minutes by default)
        self.turn_timeout_sec = _knob("ADVISORS_TURN_TIMEOUT_SEC", 1800)
        # How long the backend may send no events at all before the turn times out (section 7: 10 minutes)
        self.stall_sec = _knob("ADVISORS_STALL_SEC", 600)
        # Circuit breaker for paid advisors (section 9: 40 tool calls, 30 minutes)
        self.paid_max_tool_calls = int(_knob("ADVISORS_PAID_MAX_TOOL_CALLS", 40))
        self.paid_max_sec = _knob("ADVISORS_PAID_MAX_SEC", 1800)
        # Foreground heartbeat interval (section 4: every 30 seconds)
        self.heartbeat_sec = _knob("ADVISORS_HEARTBEAT_SEC", 30)
        # How long without a backend event before status says "waiting for the model, progress unknown"
        self.quiet_sec = _knob("ADVISORS_QUIET_SEC", 20)
        # After sending an abort, how long to wait before killing the backend process
        self.stop_grace_sec = _knob("ADVISORS_STOP_GRACE_SEC", 20)
        # After a steer, how long to wait to see whether it was delivered before replying to the caller
        self.steer_wait_sec = _knob("ADVISORS_STEER_WAIT_SEC", 3)
        # How long quota lookups are cached
        self.quota_cache_sec = _knob("ADVISORS_QUOTA_CACHE_SEC", 60)
