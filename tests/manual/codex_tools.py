"""Manual check: which tools Codex really gives an advisor thread. Spends one very small Codex turn per mode.

Usage: python3 tests/manual/codex_tools.py [--advisor ID] [--tools read|none|both]

Codex accepts unknown config keys silently, so the lean config can only be verified by looking at what the
model was offered. For each mode this starts a thread with advisors' own thread config, runs "Reply with
exactly: OK" at low effort, then reads the tool list Codex wrote to the thread's rollout file
(~/.codex/sessions/...) and prints it as a tree. Code mode nests most tools inside one `exec` tool; the nested
ones are listed from its description.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from advisors.backends.codex_app import CodexBackend, thread_config  # noqa: E402
from advisors.probe import resolve_bins  # noqa: E402
from codex_overhead import pick_advisor  # noqa: E402

FORBIDDEN_WITHOUT_TOOLS = ("exec_command", "web__run", "view_image", "spawn_agent", "image_gen")


def tool_tree(rollout: Path) -> list[str]:
    out: list[str] = []
    for line in rollout.read_text().splitlines():
        o = json.loads(line)
        p = o.get("payload") or {}
        if o.get("type") != "response_item" or p.get("type") != "additional_tools":
            continue

        def walk(tools, depth=0):
            for t in tools:
                out.append("  " * depth + f"{t.get('name')}")
                if t.get("name") == "exec":
                    for n in sorted(set(re.findall(r"(?m)^#+ `([A-Za-z0-9_]+)`", t.get("description") or ""))):
                        out.append("  " * (depth + 1) + f"exec > {n}")
                walk(t.get("tools") or [], depth + 1)

        walk(p.get("tools") or [])
        break
    return out


async def one(advisor, tools: str, cwd: str, bins: dict, env: dict) -> list[str]:
    b = CodexBackend(advisor, {"session": "tools-check"}, env, bins)
    tmp = Path(tempfile.mkdtemp())
    try:
        await b.handshake(cwd, tmp / "stderr.log")
        res = await b.request("thread/start", {"cwd": cwd, "sandbox": "read-only", "approvalPolicy": "never",
                                               "model": advisor.model, "config": thread_config("low", tools)}, 60)
        tid, path = res["thread"]["id"], res["thread"].get("path")
        r = await b.request("turn/start", {"threadId": tid, "effort": "low",
                                           "input": [{"type": "text", "text": "Reply with exactly: OK", "text_elements": []}]}, 60)
        turn = r["turn"]["id"]
        while True:
            ev = await asyncio.wait_for(b.events.get(), 180)
            p = ev.get("params") or {}
            if ev.get("method") == "turn/completed" and (p.get("turn") or {}).get("id") == turn:
                status = p["turn"].get("status")
                break
    finally:
        await b.close()
    if not path or not Path(path).is_file():
        return [f"(turn {status}; no rollout file at {path})"]
    return [f"(turn {status})"] + tool_tree(Path(path))


async def main() -> None:
    ap = argparse.ArgumentParser(description="Print the tools Codex offers an advisor thread. Spends a little quota.")
    ap.add_argument("--advisor", metavar="ID", help="codex-app-server advisor (default: the first in the config)")
    ap.add_argument("--tools", choices=["read", "none", "both"], default="both")
    a = ap.parse_args()
    advisor = pick_advisor(a.advisor)
    bins, env = resolve_bins(dict(os.environ))
    cwd = tempfile.mkdtemp()
    bad = False
    for mode in (["none", "read"] if a.tools == "both" else [a.tools]):
        tree = await one(advisor, mode, cwd, bins, env)
        print(f"== --tools {mode}")
        print("\n".join(tree))
        names = " ".join(tree)
        if mode == "none":
            leaks = [n for n in FORBIDDEN_WITHOUT_TOOLS if n in names]
            print("leaks:", ", ".join(leaks) if leaks else "none")
            bad |= bool(leaks)
        if "spawn_agent" in names:
            print("sub-agent tools are present")
            bad = True
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    asyncio.run(main())
