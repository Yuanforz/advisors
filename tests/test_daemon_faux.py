"""Integration tests for the daemon's code paths. The backend is pi's fauxProvider fake model (no cost, no network).

Needs pi (@earendil-works/pi-coding-agent) installed. Each test class has its own data directory and daemon.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers import PNG, DaemonEnv, _alive, faux_advisor, pids_matching, wait_until  # noqa: E402


class Basic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = DaemonEnv(knobs={"ADVISORS_QUIET_SEC": 1})

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_foreground_ok_and_files_on_disk(self):
        p = self.e.run("ask", "test-faux", "FAUX:echo hi")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ECHO turns=1", p.stdout)
        last = p.stdout.strip().splitlines()[-1]
        self.assertTrue(last.startswith("[advisors] test-faux"), last)
        self.assertIn("confirmed medium", last)
        self.assertIn("new session", p.stderr)
        sess = p.stderr.split("new session ", 1)[1].split(" (", 1)[0]
        d = self.e.home / "sessions" / sess
        for f in ("meta.json", "turns/1.prompt.md", "turns/1.answer.md", "turns/1.meta.json", "turns/1.events.jsonl"):
            self.assertTrue((d / f).exists(), f)
        tm = json.loads((d / "turns/1.meta.json").read_text())
        self.assertEqual(tm["status"], "ok")
        self.assertEqual(tm["effort"], "medium")
        self.assertEqual(tm["effort_confirmed"], "medium")
        self.assertIsNotNone(tm["elapsed_sec"])
        self.assertIn("input", tm["usage"])
        ev = (d / "turns/1.events.jsonl").read_text()
        self.assertNotIn("text_delta", ev)  # answer-text deltas are not written to disk
        self.assertIn("agent_settled", ev)
        self.assertEqual((d / "turns/1.answer.md").read_text().strip(), "ECHO turns=1 files=0 images=0 seen=[]")
        sm = json.loads((d / "meta.json").read_text())
        self.assertEqual(sm["advisor"], "test-faux")
        self.assertEqual(sm["backend_session_id"], f"advisors-{sess}")
        self.assertIsNone(sm["busy_run"])
        # Session resume: the second turn in the same pi process sees the first
        rc, r = self.e.json("ask", "--session", sess, "FAUX:echo again")
        self.assertEqual(rc, 0)
        self.assertEqual(r["turn"], 2)
        self.assertIn("turns=2", r["answer"])
        lg = self.e.run("log", sess)
        self.assertIn("## Turn 2", lg.stdout)
        self.assertIn("FAUX:echo again", lg.stdout)

    def test_attachments(self):
        (self.e.work / "note.md").write_text("some notes")
        (self.e.work / "pic.png").write_bytes(PNG)
        rc, r = self.e.json("ask", "test-faux", "FAUX:echo files", "--files", "note.md", "pic.png")
        self.assertEqual(rc, 0, r)
        self.assertIn("files=2 images=1", r["answer"])
        rc, r = self.e.json("ask", "test-faux", "x", "--files", "missing.txt")
        self.assertEqual(rc, 2)

    def test_options_between_positionals(self):
        rc, r = self.e.json("ask", "test-faux", "--effort", "low", "FAUX:echo mixed")
        self.assertEqual(rc, 0, r)
        self.assertEqual(r["effort"], "low")
        proc = self.e.popen("ask", "test-faux", "FAUX:echo delay=4")
        run = self.e.wait_for_run(proc)
        rc, s = self.e.json("steer", run, "--mode", "follow_up", "MIXED-FOLLOW")
        self.assertEqual(rc, 0, s)
        self.assertEqual(s["mode"], "follow_up")
        self.assertEqual(self.e.finish(proc)[0], 0)
        self.assertEqual(self.e.run("ask", "test-faux", "x", "--bogus").returncode, 2)

    def test_prompt_from_stdin(self):
        p = self.e.run("ask", "test-faux", "-", "--json", input="FAUX:echo from stdin")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ECHO", json.loads(p.stdout)["answer"])

    def test_effort(self):
        rc, r = self.e.json("ask", "test-faux", "FAUX:echo", "--effort", "max")
        self.assertEqual(rc, 0)
        self.assertEqual(r["effort_requested"], "max")
        self.assertEqual(r["effort"], "high")
        self.assertEqual(r["effort_confirmed"], "high")
        self.assertIn("using high", r["effort_note"])
        p = self.e.run("ask", "test-faux", "FAUX:echo", "--effort", "bogus")
        self.assertEqual(p.returncode, 2)

    def test_usage_errors(self):
        rc, r = self.e.json("ask", "test-faux", "FAUX:echo")
        sess = r["session"]
        self.assertEqual(self.e.run("ask", "--session", sess, "test-faux", "x").returncode, 2)
        self.assertEqual(self.e.run("ask", "--session", "nope-1-xx", "x").returncode, 2)
        self.assertEqual(self.e.run("ask", "nosuch", "x").returncode, 2)
        p = self.e.run("ask", "--session", sess, "--cwd", "/", "x")
        self.assertEqual(p.returncode, 2)
        self.assertIn("is fixed at", p.stderr)

    def test_background_status_tail_and_cursor(self):
        proc = self.e.popen("ask", "test-slow", "FAUX:slow words=150")
        run = self.e.wait_for_run(proc)
        s = wait_until(lambda: (lambda d: d if d["text_bytes"] > 50 else None)(self.e.status(run, "--tail", "20")), 20)
        self.assertTrue(s, "the answer text did not grow")
        self.assertFalse(s["finished"])
        self.assertEqual(s["phase"], "writing")
        tm = json.loads((self.e.home / "sessions" / run.rsplit(":", 1)[0] / "turns/1.meta.json").read_text())
        self.assertEqual(tm["status"], "running")  # not "queued" until the end
        self.assertLessEqual(len(s["tail"]), 20)
        self.assertLess(s["last_backend_event_age_sec"], 5)
        cur = s["cursor"]
        first = s["tail_start"]
        got = s["tail"]
        while True:
            s = self.e.status(run, "--since", str(cur))
            self.assertEqual(s["tail_start"], cur)
            got += s["tail"]
            cur = s["cursor"]
            if s["finished"]:
                self.assertEqual(s["phase"], "done")
                break
            time.sleep(0.5)
        rc, out, err = self.e.finish(proc)
        self.assertEqual(rc, 0, err)
        answer = (self.e.home / "sessions" / run.rsplit(":", 1)[0] / "turns/1.answer.md").read_text().rstrip("\n")
        self.assertEqual(got, answer[first:])  # reading on from the cursor: nothing missing, nothing repeated
        self.assertTrue(answer.startswith("word word"))
        # a session name works too (it picks the latest turn)
        s2 = self.e.status(run.rsplit(":", 1)[0])
        self.assertEqual(s2["run"], run)
        self.assertTrue(s2["finished"])

    def test_heartbeat_reports_backend_silence(self):
        p = self.e.run("ask", "test-faux", "FAUX:sleep s=5", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        hb = [l for l in p.stderr.splitlines() if "heartbeat" in l]
        self.assertTrue(hb, p.stderr)
        self.assertTrue(any("waiting for the model, progress unknown" in l for l in hb), hb)
        self.assertTrue(any("last backend event" in l for l in hb), hb)

    def test_steer_delivered_and_expired(self):
        proc = self.e.popen("ask", "test-faux", "FAUX:tools n=4 delay=1")
        run = self.e.wait_for_run(proc)
        wait_until(lambda: self.e.status(run)["tool_calls"] >= 1, 15)
        rc, r = self.e.json("steer", run, "STEER-OK please")
        self.assertEqual(rc, 0, r)
        self.assertIn(r["disposition"], ("delivered", "queued"))
        rc2, out, err = self.e.finish(proc)
        self.assertEqual(rc2, 0, err)
        self.assertIn('seen=["STEER-OK please"]', out)
        s = self.e.status(run)
        self.assertEqual(s["steers"][0]["state"], "delivered")
        rc, r = self.e.json("steer", run, "too late")
        self.assertEqual(rc, 1)
        self.assertEqual(r["disposition"], "expired")

    def test_follow_up(self):
        proc = self.e.popen("ask", "test-faux", "FAUX:echo delay=5")
        run = self.e.wait_for_run(proc)
        time.sleep(0.5)
        rc, r = self.e.json("steer", run, "FOLLOW-1", "--mode", "follow_up")
        self.assertEqual(rc, 0, r)
        self.assertEqual(r["disposition"], "queued")
        rc2, out, err = self.e.finish(proc)
        self.assertEqual(rc2, 0, err)
        # The first answer and the second answer after the follow-up message are both in this turn
        self.assertIn("seen=[]", out)
        self.assertIn('seen=["FOLLOW-1"]', out)
        self.assertEqual(self.e.status(run)["steers"][0]["state"], "delivered")

    def test_stop_is_idempotent(self):
        proc = self.e.popen("ask", "test-faux", "FAUX:sleep s=30")
        run = self.e.wait_for_run(proc)
        time.sleep(0.8)
        t0 = time.monotonic()
        rc, r = self.e.json("stop", run)
        self.assertEqual(rc, 0)
        self.assertEqual(r["status"], "aborted")
        self.assertLess(time.monotonic() - t0, 5)
        rc, r = self.e.json("stop", run)
        self.assertEqual(rc, 0)
        self.assertTrue(r["already_finished"])
        self.assertEqual(r["status"], "aborted")
        rc2, out, err = self.e.finish(proc)
        self.assertEqual(rc2, 8)
        # After a stop, the same session still takes questions
        rc, r = self.e.json("ask", "--session", run.rsplit(":", 1)[0], "FAUX:echo after")
        self.assertEqual(rc, 0, r)
        self.assertIn("ECHO", r["answer"])

    def test_busy(self):
        proc = self.e.popen("ask", "test-faux", "FAUX:sleep s=4")
        run = self.e.wait_for_run(proc)
        sess = run.rsplit(":", 1)[0]
        rc, r = self.e.json("ask", "--session", sess, "FAUX:echo")
        self.assertEqual(rc, 3)
        self.assertEqual(r["status"], "busy")
        self.assertEqual(r["run"], run)
        self.assertEqual(self.e.finish(proc)[0], 0)

    def test_idempotency_key(self):
        rc, r1 = self.e.json("ask", "test-faux", "FAUX:echo key", "--key", "K1")
        self.assertEqual(rc, 0)
        rc, r2 = self.e.json("ask", "test-faux", "FAUX:echo key", "--key", "K1")
        self.assertEqual(rc, 0)
        self.assertEqual(r2["run"], r1["run"])
        self.assertTrue(r2.get("replayed"))
        self.assertEqual(r2["answer"], r1["answer"])
        meta = json.loads((self.e.home / "sessions" / r1["session"] / "meta.json").read_text())
        self.assertEqual(meta["turns"], 1)  # not submitted again
        p = self.e.run("ask", "test-faux", "FAUX:echo different", "--key", "K1")
        self.assertEqual(p.returncode, 2)
        # The same key while the turn is still running: attaches to that turn
        proc = self.e.popen("ask", "test-faux", "FAUX:tools n=2 delay=1", "--key", "K2")
        run = self.e.wait_for_run(proc)
        rc, r3 = self.e.json("ask", "test-faux", "FAUX:tools n=2 delay=1", "--key", "K2")
        self.assertEqual(rc, 0)
        self.assertEqual(r3["run"], run)
        self.assertEqual(self.e.finish(proc)[0], 0)

    def test_timeout_flag(self):
        t0 = time.monotonic()
        rc, r = self.e.json("ask", "test-faux", "FAUX:sleep s=30", "--timeout", "2")
        self.assertEqual(rc, 7, r)
        self.assertEqual(r["status"], "timeout")
        self.assertIn("turn time limit", r["error"])
        self.assertLess(time.monotonic() - t0, 15)

    def test_sigterm_becomes_stop(self):
        proc = self.e.popen("ask", "test-faux", "FAUX:sleep s=30")
        run = self.e.wait_for_run(proc)
        time.sleep(0.8)
        proc.send_signal(signal.SIGTERM)
        rc, out, err = self.e.finish(proc, timeout=20)
        self.assertEqual(rc, 8, err)
        self.assertIn("SIGTERM", err)
        s = self.e.status(run)
        self.assertEqual(s["status"], "aborted")

    def test_sigint_becomes_stop(self):
        proc = self.e.popen("ask", "test-faux", "FAUX:sleep s=30")
        run = self.e.wait_for_run(proc)
        time.sleep(0.8)
        proc.send_signal(signal.SIGINT)
        rc, out, err = self.e.finish(proc, timeout=20)
        self.assertEqual(rc, 8, err)
        self.assertEqual(self.e.status(run)["status"], "aborted")

    def test_tools_none_and_switch(self):
        rc, r = self.e.json("ask", "test-faux", "FAUX:echo", "--tools", "none")
        self.assertEqual(rc, 0)
        sess = r["session"]
        first = [l for l in self.e.log().splitlines() if f"advisors-{sess}" in l and "started pid" in l][0]
        self.assertIn(f"--session-id advisors-{sess} --no-context-files --no-skills --no-prompt-templates --no-mcp", first)
        self.assertIn("--no-tools", first)
        self.assertIn("--append-system-prompt", first)
        rc, r = self.e.json("ask", "--session", sess, "FAUX:echo", "--tools", "read")
        self.assertEqual(rc, 0)
        self.assertIn("turns=2", r["answer"])  # a new tool setting restarts pi; history comes from disk
        lines = [l for l in self.e.log().splitlines() if f"advisors-{sess}" in l and "started pid" in l]
        self.assertEqual(len(lines), 2)
        self.assertIn("--tools read,grep,find,ls", lines[1])

    def test_rate_limited_and_failed(self):
        rc, r = self.e.json("ask", "test-faux", "FAUX:error msg=429 RESOURCE_EXHAUSTED: try later")
        self.assertEqual(rc, 6, r)
        self.assertEqual(r["status"], "rate_limited")
        rc, r = self.e.json("ask", "test-faux", "FAUX:error msg=400 invalid request")
        self.assertEqual(rc, 1, r)
        self.assertEqual(r["status"], "failed")
        self.assertIn("400 invalid request", r["error"])

    def test_auth_required(self):
        msg = "Example provider requires sign-in: run `example-cli login` in a terminal (session/new: Authentication required)"
        rc, r = self.e.json("ask", "test-faux", f"FAUX:error msg={msg}")
        self.assertEqual(rc, 10, r)
        self.assertEqual(r["status"], "auth_required")
        self.assertIn("example-cli login", r["error"])
        self.assertIn("will not retry automatically", r["note"])
        ev = (self.e.home / "sessions" / r["session"] / "turns/1.events.jsonl").read_text()
        self.assertNotIn("auto_retry_start", ev)

    def test_status_overview(self):
        d = self.e.status()
        self.assertEqual(d["kind"], "overview")
        self.assertIn("groups", d)
        self.assertEqual(Path(d["daemon"]["home"]), self.e.home.resolve())

    def test_list(self):
        rc, d = self.e.json("list")
        self.assertEqual(rc, 0)
        ids = [a["id"] for a in d["advisors"]]
        self.assertEqual(sorted(ids), ["test-faux", "test-slow"])

    def test_socket_permissions(self):
        st = (self.e.home / "advisord.sock").stat()
        self.assertEqual(st.st_mode & 0o777, 0o600)


class Stall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = DaemonEnv(knobs={"ADVISORS_STALL_SEC": 2})

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_no_backend_events_is_timeout(self):
        rc, r = self.e.json("ask", "test-faux", "FAUX:sleep s=30")
        self.assertEqual(rc, 7, r)
        self.assertIn("no events from the backend", r["error"])


class PaidBreaker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = DaemonEnv(advisors=[faux_advisor("test-paid", paid=True)],
                          knobs={"ADVISORS_PAID_MAX_TOOL_CALLS": 2, "ADVISORS_PAID_MAX_SEC": 600})

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_tool_call_breaker(self):
        rc, r = self.e.json("ask", "test-paid", "FAUX:tools n=10")
        self.assertEqual(rc, 7, r)
        self.assertIn("circuit breaker", r["error"])
        self.assertGreaterEqual(r["tool_calls"], 3)
        self.assertLess(r["tool_calls"], 10)

    def test_timeout_capped(self):
        rc, r = self.e.json("ask", "test-paid", "FAUX:echo", "--timeout", "99999")
        self.assertEqual(rc, 0)
        tm = json.loads((self.e.home / "sessions" / r["session"] / "turns/1.meta.json").read_text())
        self.assertEqual(tm["timeout_sec"], 600)


class IdleAndResume(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = DaemonEnv(knobs={"ADVISORS_BACKEND_IDLE_SEC": 1, "ADVISORS_IDLE_EXIT_SEC": 3})

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_backend_and_daemon_idle_exit_then_resume(self):
        rc, r = self.e.json("ask", "test-faux", "FAUX:echo one")
        self.assertEqual(rc, 0)
        sess = r["session"]
        pid = self.e.daemon_pid()
        self.assertTrue(pid and _alive(pid))
        # The backend process is closed after 1 second idle
        self.assertTrue(wait_until(lambda: not pids_matching(f"advisors-{sess}"), 10), "the pi process was not closed")
        # The daemon exits after 3 seconds with no activity, deleting the socket and clearing the pid file
        self.assertTrue(wait_until(lambda: not _alive(pid), 15), "the daemon did not exit")
        self.assertFalse((self.e.home / "advisord.sock").exists())
        self.assertEqual((self.e.home / "advisord.pid").read_text(), "")  # cleared, not deleted (see daemon._shutdown)
        # Ask again: a new daemon starts automatically, and pi picks up the history from disk with --session-id
        rc, r = self.e.json("ask", "--session", sess, "FAUX:echo two")
        self.assertEqual(rc, 0, r)
        self.assertIn("turns=2", r["answer"])
        self.assertNotEqual(self.e.daemon_pid(), pid)


class Gemini4Cap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = DaemonEnv(advisors=[faux_advisor("test-g4a", group="gemini4"), faux_advisor("test-g4b", group="gemini4")])

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_three_runs_two_slots(self):
        procs = [self.e.popen("ask", aid, "FAUX:sleep s=3") for aid in ("test-g4a", "test-g4b", "test-g4a")]
        runs = [self.e.wait_for_run(p) for p in procs]
        ov = wait_until(lambda: (lambda d: d if any(g["group"] == "gemini4" and g["queued"] == 1 for g in d["groups"]) else None)(self.e.status()), 10)
        self.assertTrue(ov, "the third run was not queued")
        g = [x for x in ov["groups"] if x["group"] == "gemini4"][0]
        self.assertEqual((g["active"], g["limit"]), (2, 2))
        queued = [a for a in ov["active"] if a["phase"] == "queued"]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["queue"]["position"], 1)
        results = [self.e.finish(p) for p in procs]
        for rc, out, err in results:
            self.assertEqual(rc, 0, err)
        self.assertTrue(any("position 1" in err for _, _, err in results))
        metas = [json.loads((self.e.home / "sessions" / r.rsplit(":", 1)[0] / "turns/1.meta.json").read_text()) for r in runs]
        waited = sorted(m["queued_sec"] for m in metas)
        self.assertLess(waited[0], 1)
        self.assertLess(waited[1], 1)
        self.assertGreater(waited[2], 2)


class ClientAndDaemonSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = DaemonEnv()

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_client_killed_run_continues(self):
        proc = self.e.popen("ask", "test-faux", "FAUX:tools n=3 delay=1")
        run = self.e.wait_for_run(proc)
        proc.kill()  # SIGKILL cannot be caught, so it does not turn into a stop
        proc.communicate()
        s = wait_until(lambda: (lambda d: d if d.get("finished") else None)(self.e.status(run)), 20)
        self.assertTrue(s)
        self.assertEqual(s["status"], "ok")
        self.assertIn("DONE tools=3", s["tail"])

    def test_ask_right_after_shutdown_gets_new_daemon(self):
        rc, r = self.e.json("ask", "test-faux", "FAUX:echo before")
        self.assertEqual(rc, 0)
        old = self.e.daemon_pid()
        self.e.run("_shutdown", "--force")
        rc, r2 = self.e.json("ask", "--session", r["session"], "FAUX:echo after")  # don't wait for the old one to exit
        self.assertEqual(rc, 0, r2)
        self.assertIn("turns=2", r2["answer"])
        new = self.e.daemon_pid()
        self.assertTrue(new and new != old and _alive(new))  # the pid file holds the new daemon
        self.assertTrue(wait_until(lambda: not _alive(old), 15))

    def test_daemon_sigterm_aborts_runs(self):
        proc = self.e.popen("ask", "test-faux", "FAUX:sleep s=30")
        run = self.e.wait_for_run(proc)
        time.sleep(0.8)
        pid = self.e.daemon_pid()
        os.kill(pid, signal.SIGTERM)
        rc, out, err = self.e.finish(proc, timeout=30)
        self.assertEqual(rc, 8, err)
        self.assertIn("the daemon is shutting down", err)
        self.assertTrue(wait_until(lambda: not _alive(pid), 15))
        self.assertFalse(pids_matching(f"advisors-{run.rsplit(':', 1)[0]}"))  # the backend was cleaned up too
        self.assertEqual(self.e.status(run)["status"], "aborted")


class DaemonCrash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = DaemonEnv()

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_killed_daemon_leaves_unknown(self):
        proc = self.e.popen("ask", "test-faux", "FAUX:sleep s=30", "--key", "CRASH")
        run = self.e.wait_for_run(proc)
        time.sleep(1)
        backends = [b["pid"] for b in self.e.status()["live_backends"]]
        pid = self.e.daemon_pid()
        os.kill(pid, signal.SIGKILL)
        for b in backends:  # a SIGKILLed daemon cannot clean up its backends, so the test does
            try:
                os.killpg(b, signal.SIGKILL)
            except ProcessLookupError:
                pass
        rc, out, err = self.e.finish(proc, timeout=20)
        self.assertEqual(rc, 9, err)
        self.assertIn("result of this turn is unknown", err)
        s = self.e.status(run)  # starts a new daemon
        self.assertEqual(s["status"], "unknown")
        self.assertEqual(s["exit_code"], 9)
        rc, r = self.e.json("ask", "test-faux", "FAUX:sleep s=30", "--key", "CRASH")
        self.assertEqual(rc, 9, r)  # not submitted again
        self.assertEqual(r["run"], run)


if __name__ == "__main__":
    unittest.main()
