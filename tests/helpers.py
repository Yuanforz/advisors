"""A daemon environment for the tests: a temporary data directory, test advisors (the faux model) and short
time limits.

Each DaemonEnv has its own ADVISORS_HOME, and so its own advisord; closing it shuts the daemon down and
deletes the temporary directory. pi's config directory (PI_CODING_AGENT_DIR) also points into the temporary
directory, so ~/.pi/agent is never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "advisors"
FAUX = ROOT / "tests" / "faux" / "faux-advisor.ts"

def _png(w: int = 8, h: int = 8) -> bytes:
    """Build a valid RGB PNG with the standard library (pi decodes and resizes images, and replaces a broken one
    with "Image omitted")."""
    import struct
    import zlib

    def chunk(t: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes([200, 30, 30]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = _png()


def faux_advisor(aid: str, model: str = "faux/fast", **kw) -> dict:
    d = {"id": aid, "model": model, "no_extensions": True, "extensions": [str(FAUX)],
         "group": "test", "efforts": ["low", "medium", "high"], "default_effort": "medium"}
    d.update(kw)
    return d


class DaemonEnv:
    def __init__(self, advisors: list[dict] | None = None, knobs: dict | None = None, pi_settings: dict | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="advtest-"))
        self.home = self.tmp / "home"
        self.work = self.tmp / "work"
        self.pi_agent = self.tmp / "pi-agent"
        for d in (self.work, self.pi_agent):
            d.mkdir(parents=True)
        (self.work / "a.txt").write_text("alpha\n")
        (self.pi_agent / "settings.json").write_text(json.dumps(
            pi_settings or {"retry": {"enabled": True, "maxRetries": 1, "baseDelayMs": 100}}))
        env_pi = {"PI_CODING_AGENT_DIR": str(self.pi_agent), "PI_OFFLINE": "1"}
        if advisors is None:
            advisors = [faux_advisor("test-faux"), faux_advisor("test-slow", model="faux-slow/slow")]
        for a in advisors:
            a.setdefault("env", {})
            for k, v in env_pi.items():
                a["env"].setdefault(k, v)
        self.advisors_file = self.tmp / "advisors.json"
        self.advisors_file.write_text(json.dumps({"advisors": advisors}, ensure_ascii=False))
        self.env = dict(os.environ)
        for k in list(self.env):
            if k.startswith("ADVISORS_"):
                del self.env[k]
        self.env.update({
            "ADVISORS_HOME": str(self.home),
            "ADVISORS_TEST_ADVISORS": str(self.advisors_file),
            "ADVISORS_ONLY_TEST": "1",
            "ADVISORS_SKIP_PROBE": "1",
            "ADVISORS_HEARTBEAT_SEC": "2",
            "ADVISORS_STEER_WAIT_SEC": "3",
            "ADVISORS_STOP_GRACE_SEC": "5",
        })
        for k, v in (knobs or {}).items():
            self.env[k] = str(v)

    # ---- commands ----

    def run(self, *args: str, timeout: float = 60, input: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BIN), *args], cwd=self.work, env=self.env, capture_output=True,
                              text=True, timeout=timeout, input=input)

    def popen(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(BIN), *args], cwd=self.work, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def json(self, *args: str, timeout: float = 60) -> tuple[int, dict]:
        p = self.run(*args, "--json", timeout=timeout)
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError as e:
            raise AssertionError(f"not JSON: rc={p.returncode} stdout={p.stdout!r} stderr={p.stderr!r}") from e

    def wait_for_run(self, proc: subprocess.Popen, timeout: float = 15) -> str:
        """Read the run id from a background ask's stderr (the first progress line)."""
        deadline = time.monotonic() + timeout
        assert proc.stderr
        while time.monotonic() < deadline:
            line = proc.stderr.readline()
            if not line:
                break
            self._stderr_lines.setdefault(proc.pid, []).append(line)
            if " run " in line:
                return line.split(" run ", 1)[1].split(" ", 1)[0]
        raise AssertionError("no run id in the output")

    _stderr_lines: dict = {}

    def finish(self, proc: subprocess.Popen, timeout: float = 60) -> tuple[int, str, str]:
        out, err = proc.communicate(timeout=timeout)
        before = "".join(self._stderr_lines.pop(proc.pid, []))
        return proc.returncode, out, before + err

    def status(self, target: str | None = None, *extra: str) -> dict:
        args = ["status"] + ([target] if target else []) + list(extra)
        rc, d = self.json(*args)
        assert rc == 0, d
        return d

    def daemon_pid(self) -> int | None:
        try:
            return int((self.home / "advisord.pid").read_text().strip())
        except (OSError, ValueError):
            return None

    def log(self) -> str:
        try:
            return (self.home / "advisord.log").read_text()
        except OSError:
            return ""

    def close(self) -> None:
        # Ask the current daemon to exit first. If the pid file cannot be read, send _shutdown over
        # the socket anyway, so no orphan daemon is left behind
        if not self.daemon_pid():
            self.run("_shutdown", "--force", timeout=30)
        pid = self.daemon_pid()
        if pid and _alive(pid):
            self.run("_shutdown", "--force", timeout=30)
            deadline = time.monotonic() + 15
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        _reap_children_of(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie process counts as exited
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(out) and not out.startswith("Z")
    except OSError:
        return True


def pids_matching(text: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", text], capture_output=True, text=True).stdout.split()
    return [int(x) for x in out if x.isdigit() and int(x) != os.getpid()]


def _reap_children_of(tmp: Path) -> None:
    """Kill only processes whose command line contains this test's temporary directory (the test started them all)."""
    for pid in pids_matching(str(tmp)):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def wait_until(fn, timeout: float = 15, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    return last
