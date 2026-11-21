"""Manual check against a real advisor: foreground, background + status, steer, stop, resume. Spends real
quota or real money.

Usage: python3 tests/manual/live_check.py ADVISOR [--effort low] [--only fg,steer,resume,stop] [--config PATH]

- The steps run one at a time, so at most one session runs at a time.
- The data directory is a new temporary directory, so ~/.advisors is left alone; the temporary daemon is shut
  down at the end.
- Advisors and plugins still come from your real setup: $ADVISORS_CONFIG and $ADVISORS_PLUGINS if set, else
  advisors.toml and plugins/ in your data directory ($ADVISORS_HOME or ~/.advisors). --config overrides the
  config path.
- Paid advisors cost real money for every step.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BIN = ROOT / "bin" / "advisors"
sys.path.insert(0, str(ROOT))

from advisors import plugins, registry  # noqa: E402

WORDS = {"a.txt": "MAPLE", "b.txt": "CEDAR", "c.txt": "BIRCH"}


class Env:
    def __init__(self, config_path: Path, plugins_path: Path) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="advlive-"))
        self.work = self.tmp / "work"
        self.work.mkdir()
        for f, w in WORDS.items():
            (self.work / f).write_text(f"The secret word in this file is {w}.\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("ADVISORS_")}
        self.env["ADVISORS_HOME"] = str(self.tmp / "home")
        # The temporary data directory has no config or plugins of its own: use the real ones
        self.env["ADVISORS_CONFIG"] = str(config_path)
        self.env["ADVISORS_PLUGINS"] = str(plugins_path)

    def run(self, *a: str, timeout: float = 900) -> tuple[int, dict | None, str]:
        p = subprocess.run([sys.executable, str(BIN), *a], cwd=self.work, env=self.env, capture_output=True,
                           text=True, timeout=timeout)
        try:
            return p.returncode, json.loads(p.stdout), p.stderr
        except ValueError:
            return p.returncode, None, p.stderr + p.stdout

    def popen(self, *a: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(BIN), *a], cwd=self.work, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def shutdown(self) -> None:
        subprocess.run([sys.executable, str(BIN), "_shutdown", "--force"], env=self.env, capture_output=True, timeout=60)


def wait_line(proc: subprocess.Popen, needle: str, timeout: float, seen: list[str]) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            return None
        seen.append(line)
        print("   ", line.rstrip()[:200])
        if needle in line:
            return line
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("advisor")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--only", default="fg,steer,resume,stop")
    ap.add_argument("--config", metavar="PATH",
                    help="advisors config to use (default: $ADVISORS_CONFIG, else advisors.toml in the data directory)")
    a = ap.parse_args()
    steps = a.only.split(",")
    # Resolve the real paths from the caller's environment, before Env swaps in the temporary data directory
    cfg = (Path(a.config).expanduser() if a.config else registry.config_path()).resolve()
    plugins_path = plugins.plugins_dir().resolve()
    if not cfg.is_file():
        sys.exit(f"no advisors config at {cfg}; pass --config PATH")
    print(f"config {cfg}, plugins {plugins_path}")
    e = Env(cfg, plugins_path)
    rows = []
    session = None
    try:
        if "fg" in steps:
            t0 = time.monotonic()
            rc, r, err = e.run("ask", a.advisor, "Reply with exactly: OK", "--effort", a.effort, "--tools", "none", "--json")
            rows.append(("foreground", rc, (r or {}).get("status"), "OK" in ((r or {}).get("answer") or ""), time.monotonic() - t0,
                         (r or {}).get("cost_usd"), (r or {}).get("error") or ""))
        if "steer" in steps:
            t0 = time.monotonic()
            proc = e.popen("ask", a.advisor, "--effort", a.effort, "--json",
                           "There are three files in the current directory: a.txt, b.txt, c.txt. Read them strictly one "
                           "at a time: call the read tool for a.txt and wait for its result, then b.txt, then c.txt. "
                           "One file per tool call, never batch, do not use read_many. After all three, reply with the "
                           "three secret words in order, one per line.")
            seen: list[str] = []
            head = wait_line(proc, " run ", 60, seen)
            run = head.split(" run ", 1)[1].split(" ", 1)[0] if head else None
            session = run.rsplit(":", 1)[0] if run else None
            disp = None
            if run and wait_line(proc, "tool: ", 600, seen):
                st = subprocess.run([sys.executable, str(BIN), "status", run, "--json"], env=e.env, capture_output=True, text=True)
                print("    status:", st.stdout[:300].replace("\n", " "))
                sr = subprocess.run([sys.executable, str(BIN), "steer", run, "--json",
                                     "Change of plan from the user: when you give your final answer, add one last line "
                                     "containing exactly STEER-OK."], env=e.env, capture_output=True, text=True)
                disp = (json.loads(sr.stdout) if sr.stdout.strip().startswith("{") else {}).get("disposition")
            out, err = proc.communicate(timeout=900)
            r = json.loads(out) if out.strip().startswith("{") else {}
            ans = r.get("answer") or ""
            rows.append(("steer", proc.returncode, r.get("status"), f"steer={disp} STEER-OK={'STEER-OK' in ans} "
                         f"words={all(w in ans for w in WORDS.values())}", time.monotonic() - t0, r.get("cost_usd"),
                         r.get("error") or ""))
        if "resume" in steps and session:
            e.shutdown()  # force a resume from disk
            t0 = time.monotonic()
            rc, r, err = e.run("ask", "--session", session, "What was the secret word in b.txt? Reply with just the word.",
                               "--effort", a.effort, "--json")
            rows.append(("resume (new daemon)", rc, (r or {}).get("status"), "CEDAR" in ((r or {}).get("answer") or "").upper(),
                         time.monotonic() - t0, (r or {}).get("cost_usd"), (r or {}).get("error") or ""))
        if "stop" in steps:
            t0 = time.monotonic()
            proc = e.popen("ask", a.advisor, "--effort", a.effort, "--tools", "none", "--json",
                           "Write a 1500-word essay on the history of paper, with many section headings.")
            seen = []
            head = wait_line(proc, " run ", 60, seen)
            run = head.split(" run ", 1)[1].split(" ", 1)[0] if head else None
            if not run:
                out, err = proc.communicate(timeout=120)
                rows.append(("stop, then follow-up", proc.returncode, None, "no run id", time.monotonic() - t0, None, (out + err)[-200:]))
            else:
                wait_line(proc, "writing the answer", 300, seen)
                time.sleep(3)
                sp = subprocess.run([sys.executable, str(BIN), "stop", run, "--json"], env=e.env, capture_output=True,
                                    text=True)
                out, err = proc.communicate(timeout=120)
                r = json.loads(out) if out.strip().startswith("{") else {}
                rc2, r2, _ = e.run("ask", "--session", run.rsplit(":", 1)[0],
                                   "Ignore the essay. Reply with exactly: AFTER-OK", "--effort", a.effort, "--json")
                rows.append(("stop, then follow-up", proc.returncode, r.get("status"),
                             f"stop={json.loads(sp.stdout).get('status') if sp.stdout.strip().startswith('{') else sp.stdout[:80]} "
                             f"after={(r2 or {}).get('status')}:{'AFTER-OK' in ((r2 or {}).get('answer') or '')}",
                             time.monotonic() - t0, (r.get("cost_usd") or 0) + ((r2 or {}).get("cost_usd") or 0),
                             r.get("error") or ""))
    finally:
        e.shutdown()
    print()
    print(f"advisor {a.advisor}, effort {a.effort}, data directory {e.tmp}")
    for name, rc, status, check, secs, cost, err in rows:
        print(f"  {name}: exit code {rc} · {status} · {check} · {secs:.1f} s · cost {cost} {('· ' + err[:120]) if err else ''}")


if __name__ == "__main__":
    main()
