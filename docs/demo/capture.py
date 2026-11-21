"""Record the README demos from real advisors sessions.

    python3 docs/demo/capture.py [list] [ask] [steer] [stop]
    python3 docs/demo/render.py

It copies ratelimit.py and advisors.toml from this directory into a temporary project and data directory,
runs real `advisors` commands there, and writes the timestamped output to cast.json. render.py turns that into
docs/media/*.gif and list.png. The scenes use the `gpt` advisor (Codex), so they spend a few low and medium
effort turns of your ChatGPT subscription quota.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

D = Path(__file__).resolve().parent
WORK = Path(tempfile.mkdtemp(prefix="advisors-demo-"))
PROJECT = WORK / "project"
HOME = WORK / "home"
PROJECT.mkdir()
HOME.mkdir()
shutil.copy(D / "ratelimit.py", PROJECT / "ratelimit.py")
shutil.copy(D / "advisors.toml", HOME / "advisors.toml")
ENV = {k: v for k, v in os.environ.items() if not k.startswith("ADVISORS_")}
ENV.update({"ADVISORS_HOME": str(HOME), "PYTHONUNBUFFERED": "1", "COLUMNS": "200"})


class Recorder:
    def __init__(self):
        self.t0 = time.monotonic()
        self.events = []
        self.lock = threading.Lock()

    def add(self, pane, kind, text):
        with self.lock:
            self.events.append({"t": round(time.monotonic() - self.t0, 2), "pane": pane, "kind": kind, "text": text})

    def start(self, pane, shown, argv):
        self.add(pane, "cmd", shown)
        p = subprocess.Popen(argv, cwd=PROJECT, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             bufsize=1)
        lines = []

        def pump():
            for line in p.stdout:
                line = line.rstrip("\n")
                lines.append(line)
                self.add(pane, "out", line)

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        return p, th, lines


def wait_for(lines, needle, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for ln in list(lines):
            if needle in ln:
                return ln
        time.sleep(0.05)
    return None


def main():
    which = sys.argv[1:] or ["list", "ask", "steer", "stop"]
    out = D / "cast.json"
    scenes = {s["name"]: s for s in json.loads(out.read_text())["scenes"]} if out.exists() else {}

    if "list" in which:
        r = Recorder()
        p, th, _ = r.start(0, "advisors list", ["advisors", "list"])
        p.wait(); th.join()
        scenes["list"] = {"name": "list", "layout": "single", "events": r.events}

    if "ask" in which:
        q = "Review ratelimit.py. Reply with at most 3 short bullet points."
        r = Recorder()
        p, th, _ = r.start(0, f'advisors ask gpt "{q}" --effort low', ["advisors", "ask", "gpt", q, "--effort", "low"])
        p.wait(timeout=600); th.join()
        scenes["ask"] = {"name": "ask", "layout": "single", "events": r.events}

    if "steer" in which:
        q = "List every issue in ratelimit.py, then write a fixed version of the file."
        r = Recorder()
        p, th, lines = r.start(0, f'advisors ask gpt "{q}"', ["advisors", "ask", "gpt", q])
        head = wait_for(lines, " run ", 60)
        run = head.split(" run ", 1)[1].split(" ", 1)[0]
        wait_for(lines, "tool:", 120) or wait_for(lines, "phase:", 5)
        time.sleep(1.5)
        steer = "Only the most serious issue, and show the fix as a diff."
        sp, sth, _ = r.start(1, f'advisors steer {run} "{steer}"', ["advisors", "steer", run, steer])
        sp.wait(); sth.join()
        p.wait(timeout=900); th.join()
        scenes["steer"] = {"name": "steer", "layout": "split", "events": r.events}

    if "stop" in which:
        q = "Write a detailed design document for a distributed rate limiter based on ratelimit.py."
        r = Recorder()
        p, th, lines = r.start(0, f'advisors ask gpt "{q}"', ["advisors", "ask", "gpt", q, "--effort", "low"])
        head = wait_for(lines, " run ", 60)
        run = head.split(" run ", 1)[1].split(" ", 1)[0]
        session = run.rsplit(":", 1)[0]
        wait_for(lines, "tool:", 120) or wait_for(lines, "phase: thinking", 10)
        time.sleep(2.5)
        sp, sth, _ = r.start(1, f"advisors stop {run}", ["advisors", "stop", run])
        sp.wait(); sth.join()
        p.wait(timeout=120); th.join()
        time.sleep(0.8)
        q2 = "Skip the document. In one sentence: what is the main design risk?"
        p2, th2, _ = r.start(1, f'advisors ask --session {session} "{q2}"',
                             ["advisors", "ask", "--session", session, q2, "--effort", "low"])
        p2.wait(timeout=600); th2.join()
        scenes["stop"] = {"name": "stop", "layout": "split", "events": r.events}

    out.write_text(json.dumps({"paths": {"project": str(PROJECT), "home": str(HOME)},
                               "scenes": list(scenes.values())}, indent=1))
    try:  # stop the demo daemon
        s = socket.socket(socket.AF_UNIX)
        os.chdir(HOME)
        s.connect("advisord.sock")
        s.sendall(b'{"op": "shutdown"}\n')
        s.recv(4096)
    except OSError:
        pass
    for s in scenes.values():
        print(s["name"], len(s["events"]), "events,", s["events"][-1]["t"], "s")


if __name__ == "__main__":
    main()
