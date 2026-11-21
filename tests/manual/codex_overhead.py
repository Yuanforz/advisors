"""Manual measurement: the input tokens of the first request (the fixed overhead) with and without Codex's
lean config. Spends two very small amounts of Codex subscription quota.

Usage: python3 tests/manual/codex_overhead.py [--advisor ID] [WORKDIR]

The advisor comes from your advisors config: the one named by --advisor, else the first advisor whose backend
is codex-app-server. For the same prompt "Reply with exactly: OK" (low effort) it opens one ephemeral thread
each way:
  default: the user's Codex config as it is
  lean:    the thread/start config turns off MCP servers, plugins, apps and the multi-agent tools (the same
           lean config advisors uses for Codex advisors)
It reads last.inputTokens of the first request from thread/tokenUsage/updated. When the quota is used up,
both sides report usageLimitExceeded and there is no token count; the script prints that as it is.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from advisors import registry  # noqa: E402
from advisors.backends.codex_app import CodexBackend, thread_config  # noqa: E402
from advisors.probe import resolve_bins  # noqa: E402


def pick_advisor(aid: str | None) -> registry.Advisor:
    path = registry.config_path()
    if not path.is_file():
        sys.exit(f"no advisors config at {path}; set ADVISORS_CONFIG or create it")
    try:
        advisors, _ = registry.load_config(path)
    except registry.RegistryError as e:
        sys.exit(f"cannot load the advisors config: {e}")
    if aid:
        adv = advisors.get(aid)
        if not adv:
            sys.exit(f"no advisor {aid!r} in {path}")
        if adv.backend != registry.CODEX:
            sys.exit(f"advisor {aid!r} uses the {adv.backend} backend, not {registry.CODEX}")
        return adv
    adv = next((a for a in advisors.values() if a.backend == registry.CODEX), None)
    if not adv:
        sys.exit(f"no advisor with backend = \"{registry.CODEX}\" in {path}; add one or pass --advisor ID")
    return adv


async def one(advisor: registry.Advisor, cwd: str, lean: bool, bins: dict, env: dict) -> dict:
    b = CodexBackend(advisor, {"session": "overhead"}, env, bins)
    tmp = Path(tempfile.mkdtemp())
    try:
        await b.handshake(cwd, tmp / "stderr.log")
        cfg = thread_config("low", "read", lean=lean)
        res = await b.request("thread/start", {"cwd": cwd, "sandbox": "read-only", "approvalPolicy": "never",
                                               "model": advisor.model, "config": cfg, "ephemeral": True}, 60)
        tid = res["thread"]["id"]
        mcp = []
        r = await b.request("turn/start", {"threadId": tid, "effort": "low",
                                           "input": [{"type": "text", "text": "Reply with exactly: OK", "text_elements": []}]}, 60)
        turn = r["turn"]["id"]
        first = None
        status = None
        while True:
            ev = await asyncio.wait_for(b.events.get(), 180)
            m = ev.get("method")
            p = ev.get("params") or {}
            if m == "mcpServer/startupStatus/updated":
                mcp.append(p.get("name"))
            if m == "thread/tokenUsage/updated" and first is None:
                first = (p.get("tokenUsage") or {}).get("last")
            if m == "turn/completed" and (p.get("turn") or {}).get("id") == turn:
                status = p["turn"].get("status"), (p["turn"].get("error") or {}).get("codexErrorInfo")
                break
        return {"lean": lean, "first_request": first, "turn": status, "mcp_servers_started": sorted(set(mcp))}
    finally:
        await b.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description="Measure Codex's fixed per-request input tokens with and without "
                                             "the lean config. Spends a little Codex quota.")
    ap.add_argument("workdir", nargs="?", help="working directory for the threads (default: a new temporary one)")
    ap.add_argument("--advisor", metavar="ID",
                    help="codex-app-server advisor to use (default: the first one in the advisors config)")
    a = ap.parse_args()
    advisor = pick_advisor(a.advisor)
    cwd = os.path.abspath(a.workdir or tempfile.mkdtemp())
    bins, env = resolve_bins(dict(os.environ))
    print(f"advisor {advisor.id} ({advisor.model}), cwd {cwd}", file=sys.stderr)
    for lean in (False, True):
        print(json.dumps(await one(advisor, cwd, lean, bins, env), ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
