"""Rules: effort checks (section 3), result classification and exit codes (section 7), circuit breaker (section 9)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .config import Limits
from .registry import EFFORT_ORDER, Advisor  # noqa: F401  (EFFORT_ORDER is re-exported)

# Statuses and exit codes from section 7. 2 is reserved for command-line usage errors (argparse's default).
EXIT_CODES = {
    "ok": 0,
    "failed": 1,
    "busy": 3,
    "blocked_budget": 4,
    "quota_exhausted": 5,
    "rate_limited": 6,
    "timeout": 7,
    "aborted": 8,
    "unknown": 9,
    # auth_required was added beyond section 7: the advisor needs a person to sign in again, so retrying cannot help
    "auth_required": 10,
}
USAGE_ERROR = 2

STATUS_TEXT = {
    "ok": "finished normally",
    "failed": "backend error",
    "busy": "session is busy with another turn",
    "blocked_budget": "blocked by a budget gate; the request was not sent",
    "quota_exhausted": "subscription quota exhausted",
    "rate_limited": "rate limited (429); quota not exhausted",
    "timeout": "timed out",
    "aborted": "stopped",
    "unknown": "result unknown",
    "auth_required": "the advisor needs to sign in again; the user has to do it",
}

PHASES = {
    "queued": "queued",
    "connecting": "connecting",
    "thinking": "thinking",
    "tool": "calling a tool",
    "writing": "writing the answer",
    "retrying": "retrying",
    "done": "done",
}


def resolve_effort(advisor: Advisor, requested: str | None,
                   efforts: tuple[str, ...] | None = None) -> tuple[str, str | None]:
    """Return (the effort actually sent, a note). An unsupported effort moves to the nearest supported one; on a tie, the lower one."""
    ladder = efforts or advisor.efforts
    if not requested:
        if advisor.default_effort in ladder:
            return advisor.default_effort, None
        requested = advisor.default_effort
    req = requested.lower()
    if req in ladder:
        return req, None
    if req not in EFFORT_ORDER:
        raise ValueError(f"unknown effort {requested}; choices: {', '.join(ladder)}")
    ri = EFFORT_ORDER.index(req)
    best = min(ladder, key=lambda e: (abs(EFFORT_ORDER.index(e) - ri), EFFORT_ORDER.index(e)))
    return best, f"requested {req}, which {advisor.id} does not support; using {best}"


def turn_limits(advisor: Advisor, limits: Limits, timeout_arg: float | None) -> tuple[float, int | None]:
    """Return (hard timeout per turn in seconds, tool call limit). --timeout cannot loosen a paid advisor's circuit breaker."""
    timeout = float(timeout_arg) if timeout_arg else limits.turn_timeout_sec
    max_tools = None
    if advisor.paid:
        timeout = min(timeout, limits.paid_max_sec)
        max_tools = limits.paid_max_tool_calls
    return timeout, max_tools


_RATE_RE = re.compile(r"\b429\b|RESOURCE_EXHAUSTED|rate[ _-]?limit|too many requests|overloaded", re.I)


def looks_rate_limited(text: str | None) -> bool:
    return bool(text and _RATE_RE.search(text))


_AUTH_RE = re.compile(r"requires sign-in|Authentication required|\bauth_required\b|not logged in|please (?:log|sign) in", re.I)


def looks_auth_required(text: str | None) -> bool:
    return bool(text and _AUTH_RE.search(text))


_CODEX_RESET_RE = re.compile(
    r"try again at ([A-Z][a-z]{2,8})\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}) (\d{1,2}):(\d{2}) ?([AP]M)", re.I
)


def parse_codex_reset(message: str | None) -> str | None:
    """Extract the reset time from "try again at Nov 21st, 2026 2:25 AM."

    Codex writes this sentence in the local time zone. Returns the local time in ISO format (with the UTC
    offset), or None if the message cannot be parsed.
    """
    if not message:
        return None
    m = _CODEX_RESET_RE.search(message)
    if not m:
        return None
    mon, day, year, hh, mm, ampm = m.groups()
    try:
        dt = datetime.strptime(f"{mon[:3]} {day} {year} {hh}:{mm} {ampm.upper()}", "%b %d %Y %I:%M %p")
    except ValueError:
        return None
    return dt.astimezone().isoformat(timespec="minutes")


def epoch_to_local(sec: float | int | None) -> str | None:
    if not sec:
        return None
    return datetime.fromtimestamp(float(sec), tz=timezone.utc).astimezone().isoformat(timespec="minutes")
