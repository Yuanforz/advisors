"""Formatting for people: durations, sizes, progress lines and the result metadata line. Shared by the daemon and the CLI."""

from __future__ import annotations

PREFIX = "[advisors]"


def dur(sec: float | None) -> str:
    if sec is None:
        return "?"
    if sec < 60:
        return f"{sec:.1f} s"
    m, s = divmod(int(round(sec)), 60)
    if m < 60:
        return f"{m} min {s} s" if s else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def short(sec: float | None) -> str:
    if sec is None:
        return "?"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def tokens(n: int | None) -> str:
    if n is None:
        return "?"
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


def usage_text(usage: dict | None, cost: float | None) -> str:
    parts = []
    if usage:
        inp = usage.get("input")
        out = usage.get("output")
        cr = usage.get("cache_read")
        seg = f"tokens in {tokens(inp)} / out {tokens(out)}"
        if cr:
            seg += f" / cache hits {tokens(cr)}"
        if usage.get("reasoning"):
            seg += f" (reasoning {tokens(usage['reasoning'])})"
        parts.append(seg)
    if cost is not None:
        parts.append(f"cost ${cost:.4f}")
    return " · ".join(parts) if parts else "usage unknown"


def quota_text(quota: dict | None) -> str:
    if not quota:
        return ""
    if quota.get("text"):
        return str(quota["text"])
    return ""


def result_line(r: dict) -> str:
    """The metadata line that ask prints on stdout after the answer."""
    eff = f"effort requested {r.get('effort_requested') or 'default'} / sent {r.get('effort')}"
    conf = r.get("effort_confirmed")
    eff += f" / confirmed {conf}" if conf else " / confirmed none"
    bits = [
        f"{PREFIX} {r.get('advisor')}",
        eff,
        f"session {r.get('session')}",
        f"turn {r.get('turn')}",
        f"{r.get('status')} (exit code {r.get('exit_code')})",
        dur(r.get("elapsed_sec")),
        usage_text(r.get("usage"), r.get("cost_usd")),
    ]
    q = quota_text(r.get("quota"))
    if q:
        bits.append(f"quota {q}")
    if r.get("answer_path"):
        bits.append(f"full answer {r['answer_path']}")
    return " · ".join(bits)
