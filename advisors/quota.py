"""Quota lookups: Codex's account/rateLimits, a read-only vertex-budget estimate, and plugin sources.

None of them spend model quota: Codex only does the handshake and reads its limits, and vertex-budget
is read from its local config and ledger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config
from .registry import CODEX, Advisor

log = logging.getLogger("advisord")


async def run_cmd(argv: list[str], timeout: float, env: dict | None = None, cwd: str | None = None) -> tuple[int | None, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, cwd=cwd, start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        await proc.wait()
        return None, "", "timeout"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def age_text(ts: float) -> str:
    sec = max(0, time.time() - ts)
    if sec < 90:
        return f"{int(sec)} s ago"
    if sec < 5400:
        return f"{int(sec // 60)} min ago"
    return f"{sec / 3600:.1f} h ago"


# ---- Codex ----

async def codex_limits(advisor: Advisor, bins: dict, env: dict) -> dict:
    from .backends.codex_app import CodexBackend, codex_quota_from_limits

    b = CodexBackend(advisor, {"session": "quota-probe"}, env, bins)
    try:
        await b.handshake(str(config.home()), config.home() / "probe.stderr.log")
        res = await b.request("account/rateLimits/read", None, 30)
    finally:
        await b.close()
    q = codex_quota_from_limits(res.get("rateLimits") or {})
    if res.get("ordinaryUsageAllowed") is False:
        q["exhausted"] = True
    return q


# ---- vertex-budget (a read-only estimate with the extension's arithmetic; the extension decides) ----

VERTEX_BUDGET_DEFAULT_TZ = "America/Los_Angeles"


def vertex_budget_dir(advisor: Advisor) -> Path:
    raw = advisor.env.get("PI_VERTEX_BUDGET_DIR") or os.environ.get("PI_VERTEX_BUDGET_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".pi" / "agent"


def vertex_budget_estimate(advisor: Advisor) -> dict:
    d = vertex_budget_dir(advisor)
    src = "vertex-budget (read-only estimate)"
    try:
        c = json.loads((d / "vertex-budget.json").read_text())
        since = datetime.fromisoformat(c["since"].replace("Z", "+00:00"))
        try:
            tz = ZoneInfo(c.get("timeZone") or VERTEX_BUDGET_DEFAULT_TZ)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown timeZone {c.get('timeZone')!r}") from e
        frm = since.astimezone(tz).date()
        to = datetime.now(tz).date()
        grants = 0
        y, m = frm.year, frm.month
        for _ in range(1200):
            g = f"{y}-{m:02d}-{int(c['grantDay']):02d}"
            if g > to.isoformat():
                break
            if g > frm.isoformat():
                grants += 1
            m += 1
            if m > 12:
                m, y = 1, y + 1
        limit = max(0.0, c["credit"] + c["grantUsd"] * grants - c["reserve"])
    except FileNotFoundError:
        return {"source": src, "text": f"no budget config at {d / 'vertex-budget.json'}"}
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {"source": src, "text": f"cannot read the budget config: {e}"}
    spent = 0.0
    try:
        for line in (d / "vertex-ledger.jsonl").read_text().splitlines():
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                if isinstance(r.get("usd"), (int, float)) and ts >= since:
                    spent += r["usd"]
            except (ValueError, KeyError, TypeError):
                continue
    except FileNotFoundError:
        pass
    left = limit - spent
    return {"source": src, "limit_usd": round(limit, 2), "spent_usd": round(spent, 2), "left_usd": round(left, 2),
            "text": f"about ${left:.2f} left (budget ${limit:.2f}, spent ${spent:.2f}, at list prices)",
            "exhausted": left <= 0}


# ---- cache and dispatch ----

class QuotaCache:
    def __init__(self, bins: dict, env: dict, ttl: float, registry=None, plugins=None) -> None:
        self.bins, self.env, self.ttl = bins, env, ttl
        self.registry = registry
        self.sources = dict(getattr(plugins, "quota_sources", None) or {})
        self.ctx = SimpleNamespace(bins=bins, env=env, run_cmd=run_cmd)
        self._codex: tuple[float, dict | Exception] | None = None
        self._codex_lock = asyncio.Lock()

    async def codex(self, advisor: Advisor, fresh: bool = False) -> dict:
        async with self._codex_lock:
            if fresh or not self._codex or time.monotonic() - self._codex[0] > self.ttl:
                try:
                    self._codex = (time.monotonic(), await codex_limits(advisor, self.bins, self.env))
                except Exception as e:  # noqa: BLE001
                    self._codex = (time.monotonic(), e)
            val = self._codex[1]
        if isinstance(val, Exception):
            raise val
        return val

    def note_codex(self, q: dict) -> None:
        self._codex = (time.monotonic(), q)

    async def for_advisor(self, advisor: Advisor, fresh: bool = False) -> dict | None:
        src = advisor.quota
        try:
            if src == "codex" and advisor.backend == CODEX:
                return await self.codex(advisor, fresh)
            if src == "vertex-budget":
                return vertex_budget_estimate(advisor)
            if src:
                name, _, arg = src.partition(":")
                fn = self.sources.get(name)
                if fn is None:
                    return {"text": f"unknown quota source {name!r} (no plugin defines it)"}
                return await fn(self.ctx, advisor, arg, fresh)
        except Exception as e:  # noqa: BLE001
            return {"text": f"quota lookup failed: {e}"}
        if advisor.paid:
            return {"text": "pay as you go; no balance available here"}
        return None
