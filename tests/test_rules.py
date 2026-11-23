"""Unit tests for rules, parsing, config and argv building (no processes started)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from advisors import config, registry, rules, store  # noqa: E402
from advisors.backends.base import Attachments, BackendError  # noqa: E402
from advisors.backends.codex_app import BASE_LEAN, CodexBackend, codex_quota_from_limits, lean_config, thread_config  # noqa: E402
from advisors.backends.codex_exec import build_exec_argv  # noqa: E402
from advisors.backends.pi_rpc import FIXED_FLAGS, build_argv  # noqa: E402
from advisors.probe import effective_efforts, pi_levels, read_tools_view  # noqa: E402
from advisors import quota  # noqa: E402
from advisors.run import Run  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
A, GROUPS = registry.load_config(DATA / "advisors.toml")


def make_run(advisor, **kw) -> Run:
    args = dict(session="s", turn=1, advisor=advisor, prompt="p", files=[], cwd="/", tools="read",
                effort_requested=None, effort="low", effort_note=None, key=None, label=None, timeout_sec=60,
                max_tool_calls=None, new_session=True)
    args.update(kw)
    return Run(**args)


class EffortTest(unittest.TestCase):
    def test_default(self):
        self.assertEqual(rules.resolve_effort(A["gpt"], None), ("max", None))
        self.assertEqual(rules.resolve_effort(A["wide"], None), ("xhigh", None))
        self.assertEqual(rules.resolve_effort(A["narrow"], None), ("high", None))
        self.assertEqual(rules.resolve_effort(A["gemini"], None), ("high", None))
        self.assertEqual(rules.resolve_effort(A["claude"], None), ("high", None))

    def test_supported(self):
        self.assertEqual(rules.resolve_effort(A["wide"], "low"), ("low", None))
        self.assertEqual(rules.resolve_effort(A["gpt"], "XHIGH"), ("xhigh", None))

    def test_clamp_down_and_up(self):
        eff, note = rules.resolve_effort(A["narrow"], "max")
        self.assertEqual(eff, "high")
        self.assertIn("requested max", note)
        self.assertIn("using high", note)
        eff, note = rules.resolve_effort(A["gemini"], "xhigh")
        self.assertEqual(eff, "high")
        eff, note = rules.resolve_effort(A["gemini"], "minimal")
        self.assertEqual(eff, "low")
        self.assertIsNotNone(note)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            rules.resolve_effort(A["wide"], "ultra-mega")

    def test_ladder_override(self):
        # When pi supports fewer efforts than configured, clamp within the intersection
        eff, note = rules.resolve_effort(A["wide"], "xhigh", ("low", "medium", "high"))
        self.assertEqual(eff, "high")


class FormatTest(unittest.TestCase):
    def test_durations(self):
        from advisors import fmt
        self.assertEqual([fmt.dur(x) for x in (9.24, 65, 1800, 3600, 3725)],
                         ["9.2 s", "1 min 5 s", "30 min", "1 h", "1 h 2 min"])

    def test_codex_shell_wrapper_is_unwrapped(self):
        from advisors.backends.codex_app import shell_command_text as f
        self.assertEqual(f("/opt/homebrew/bin/bash -lc 'nl -ba ratelimit.py'"), "nl -ba ratelimit.py")
        self.assertEqual(f(["/bin/zsh", "-c", "ls -la"]), "ls -la")
        self.assertEqual(f("python3 x.py"), "python3 x.py")
        self.assertEqual(f("bash -lc 'unterminated"), "bash -lc 'unterminated")


class LimitsTest(unittest.TestCase):
    def test_paid_breaker_cannot_be_raised(self):
        lim = config.Limits()
        t, tools = rules.turn_limits(A["gemini"], lim, 99999)
        self.assertEqual(t, 1800)
        self.assertEqual(tools, 40)
        t, tools = rules.turn_limits(A["claude"], lim, 60)
        self.assertEqual((t, tools), (60, 40))

    def test_unpaid(self):
        lim = config.Limits()
        self.assertEqual(rules.turn_limits(A["wide"], lim, None), (1800, None))
        self.assertEqual(rules.turn_limits(A["gpt"], lim, 3600), (3600, None))

    def test_knobs_only_in_test_home(self):
        with mock.patch.dict(os.environ, {"ADVISORS_PAID_MAX_TOOL_CALLS": "999"}, clear=False):
            os.environ.pop("ADVISORS_HOME", None)
            self.assertEqual(config.Limits().paid_max_tool_calls, 40)
            with tempfile.TemporaryDirectory() as d:
                os.environ["ADVISORS_HOME"] = d
                try:
                    self.assertEqual(config.Limits().paid_max_tool_calls, 999)
                finally:
                    del os.environ["ADVISORS_HOME"]

    def test_exit_codes_match_design(self):
        self.assertEqual(rules.EXIT_CODES, {"ok": 0, "failed": 1, "busy": 3, "blocked_budget": 4, "quota_exhausted": 5,
                                            "rate_limited": 6, "timeout": 7, "aborted": 8, "unknown": 9,
                                            "auth_required": 10})


class ParseTest(unittest.TestCase):
    MSG = ("You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
           "https://chatgpt.com/settings/usage to purchase more credits or try again at Nov 21st, 2026 2:25 AM.")

    def test_codex_reset(self):
        r = rules.parse_codex_reset(self.MSG)
        self.assertTrue(r.startswith("2026-11-21T02:25"), r)
        self.assertIsNone(rules.parse_codex_reset("something else"))

    def test_rate_limit_text(self):
        for s in ["429 Too Many Requests", "RESOURCE_EXHAUSTED (code 429)",
                  "Provider request failed after its own retries: RESOURCE_EXHAUSTED (code 429): x", "rate limit hit"]:
            self.assertTrue(rules.looks_rate_limited(s), s)
        for s in ["403 Permission denied", "This operation was aborted", None, ""]:
            self.assertFalse(rules.looks_rate_limited(s), s)

    def test_auth_text(self):
        new = "Example provider requires sign-in: run `example-cli login` in a terminal (session/new: Authentication required)"
        old = "Error: not logged in. Please log in with `example-cli login`."
        self.assertTrue(rules.looks_auth_required(new))
        self.assertTrue(rules.looks_auth_required(old))
        self.assertFalse(rules.looks_auth_required("429 RESOURCE_EXHAUSTED"))
        self.assertFalse(rules.looks_rate_limited(new))

    def test_codex_limits(self):
        q = codex_quota_from_limits({"primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 1795256720},
                                     "secondary": {"usedPercent": 16, "windowDurationMins": 10080, "resetsAt": 1795800000}})
        self.assertTrue(q["exhausted"])
        self.assertTrue(q["resets_at"].startswith("2026-11-21T02:25"), q["resets_at"])
        self.assertIn("5-hour 100% used", q["text"])

    def test_pi_levels(self):
        fable = {"reasoning": True, "thinkingLevelMap": {"off": None, "xhigh": "xhigh", "max": "max"}}
        self.assertEqual(pi_levels(fable), ["minimal", "low", "medium", "high", "xhigh", "max"])
        carbon = {"reasoning": True, "thinkingLevelMap": {"off": None, "minimal": None, "low": "low", "medium": "medium",
                                                          "high": "high", "xhigh": None, "max": None}}
        self.assertEqual(pi_levels(carbon), ["low", "medium", "high"])
        self.assertEqual(pi_levels({"reasoning": False}), ["off"])

    def test_effective_efforts(self):
        caps = {"pi": {"ok": True, "models": {"example/wide-model": {"found": True, "levels": ["low", "medium", "high"]}}}}
        eff, dropped = effective_efforts(A["wide"], caps)
        self.assertEqual(eff, ("low", "medium", "high"))
        self.assertEqual(dropped, ["xhigh"])
        self.assertEqual(effective_efforts(A["gpt"], caps), (A["gpt"].efforts, []))


class ArgvTest(unittest.TestCase):
    def test_real_pi_never_no_extensions(self):
        for aid in ("gemini", "claude", "wide", "narrow"):
            for tools in ("read", "none"):
                argv = build_argv("pi", A[aid], "advisors-x", "low", tools)
                self.assertNotIn("--no-extensions", argv)
                self.assertNotIn("-e", argv)
                for f in FIXED_FLAGS:
                    self.assertIn(f, argv)
                self.assertEqual(argv[argv.index("--model") + 1], A[aid].model)
                if tools == "none":
                    self.assertIn("--no-tools", argv)
                else:
                    self.assertEqual(argv[argv.index("--tools") + 1], ",".join(A[aid].read_tools))

    def test_independence_note_reaches_every_advisor(self):
        from advisors.backends.base import independence_note
        note = independence_note()
        self.assertIn("other reviewers' records", note)
        self.assertIn("sessions", note)
        argv = build_argv("pi", A["wide"], "s", "low", "read")
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], note)
        exec_argv = build_exec_argv("codex", "m", "low", "read", "/w", None, [])
        self.assertTrue(any(x.startswith("developer_instructions=") for x in exec_argv))

    def test_faux_gets_no_extensions_only_with_explicit_e(self):
        a = registry.Advisor(id="test-x", backend=registry.PI, model="faux/fast", efforts=("low",), default_effort="low",
                             billing="", group="test", paid=False, description="", extensions=("/x.ts",),
                             no_extensions=True, test=True)
        argv = build_argv("pi", a, "s", "low", "read")
        self.assertIn("--no-extensions", argv)
        self.assertEqual(argv[argv.index("-e") + 1], "/x.ts")

    def test_non_faux_cannot_disable_extensions(self):
        a = registry.Advisor(id="test-x", backend=registry.PI, model="google-vertex/gemini-4-carbon", efforts=("low",),
                             default_effort="low", billing="", group="vertex", paid=True, description="",
                             extensions=("/x.ts",), no_extensions=True, test=True)
        with self.assertRaises(BackendError):
            build_argv("pi", a, "s", "low", "read")

    def test_registry_rejects_bad_test_advisor(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.json"
            f.write_text(json.dumps({"advisors": [{"id": "test-bad", "model": "anthropic/claude-fable-5-2",
                                                    "no_extensions": True, "extensions": ["/x.ts"]}]}))
            with mock.patch.dict(os.environ, {"ADVISORS_HOME": d, "ADVISORS_TEST_ADVISORS": str(f)}):
                with self.assertRaises(registry.RegistryError):
                    registry.all_advisors()
            f.write_text(json.dumps({"advisors": [{"id": "fable", "model": "faux/x"}]}))
            with mock.patch.dict(os.environ, {"ADVISORS_HOME": d, "ADVISORS_TEST_ADVISORS": str(f)}):
                with self.assertRaises(registry.RegistryError):
                    registry.all_advisors()

    def test_test_advisors_ignored_in_default_home(self):
        with mock.patch.dict(os.environ, {"ADVISORS_TEST_ADVISORS": "/nonexistent.json",
                                          "ADVISORS_CONFIG": str(DATA / "advisors.toml")}):
            os.environ.pop("ADVISORS_HOME", None)
            self.assertEqual(set(registry.all_advisors()), set(A))

    def test_codex_lean_config(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.toml").write_text('[mcp_servers.node_repl]\ncommand = "x"\n[mcp_servers.other]\ncommand = "y"\n')
            with mock.patch.dict(os.environ, {"CODEX_HOME": d}):
                cfg = lean_config()
                expected = dict(BASE_LEAN)
                expected.update({"mcp_servers.node_repl.enabled": False, "mcp_servers.other.enabled": False})
                self.assertEqual(cfg, expected)
                tc = thread_config("low", "none")
                self.assertEqual(tc["model_reasoning_effort"], "low")
                self.assertFalse(tc["features.shell_tool"])
                argv = build_exec_argv("codex", "gpt-6.2-astra", "max", "read", "/w", None, [])
                self.assertIn('sandbox_mode="read-only"', argv)
                self.assertIn('approval_policy="never"', argv)
                self.assertIn("features.plugins=false", argv)
                self.assertEqual(argv[-1], "-")


class AttachmentTest(unittest.TestCase):
    def test_formats(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "t.md"
            t.write_text("﻿hello")
            img = Path(d) / "x.png"
            img.write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"rest")
            empty = Path(d) / "e.txt"
            empty.write_text("")
            att = Attachments([str(t), str(img), str(empty)])
            self.assertEqual(att.text, f'<file name="{t}">\nhello\n</file>\n<file name="{img}"></file>\n')
            self.assertEqual(len(att.images), 1)
            self.assertEqual(att.images[0]["mime"], "image/png")
            self.assertTrue(att.message("Q").endswith("</file>\nQ"))
            bad = Path(d) / "b.bin"
            bad.write_bytes(b"\xff\xfe\x00\x01garbage\xff")
            with self.assertRaises(BackendError):
                Attachments([str(bad)])


class CodexEventsTest(unittest.TestCase):
    """Feed the Codex adapter the real events of a turn that hit the usage limit (2026-11-20)."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_usage_limit_is_quota_exhausted(self):
        evs = json.loads((DATA / "codex_usage_limit_events.json").read_text())["events"]
        b = CodexBackend(A["gpt"], {"session": "s"}, {}, {})
        run = make_run(A["gpt"])
        b.turn_id = evs[-1]["params"]["turn"]["id"]
        for e in evs:
            if e["method"] == "account/rateLimits/updated":
                b._merge_limits(e["params"]["rateLimits"])
            b._handle(run, e)
        b._finish(run, evs[-1]["params"]["turn"])
        self.assertEqual(run.backend_status, "quota_exhausted")
        self.assertTrue(run.quota["resets_at"].startswith("2026-11-21T02:25"), run.quota)
        self.assertIn("resets at", run.quota["text"])

    def test_normal_turn_and_steer_delivery(self):
        b = CodexBackend(A["gpt"], {"session": "s"}, {}, {})
        run = make_run(A["gpt"])
        run.steers.append({"text": "STEER", "mode": "steer", "at": 0, "state": "queued"})
        tid = "t1"
        seq = [
            {"method": "turn/started", "params": {"turn": {"id": tid}}},
            {"method": "item/started", "params": {"item": {"type": "reasoning"}}},
            {"method": "item/reasoning/summaryTextDelta", "params": {"delta": "x"}},
            {"method": "item/started", "params": {"item": {"type": "commandExecution", "command": "cat a.txt"}}},
            {"method": "item/completed", "params": {"item": {"type": "commandExecution"}}},
            {"method": "item/started", "params": {"item": {"type": "userMessage", "content": [{"type": "text", "text": "STEER"}]}}},
            {"method": "item/started", "params": {"item": {"type": "agentMessage"}}},
            {"method": "item/agentMessage/delta", "params": {"delta": "Hel"}},
            {"method": "item/agentMessage/delta", "params": {"delta": "lo"}},
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "Hello", "phase": "final_answer"}}},
            {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
                "total": {"inputTokens": 30000, "cachedInputTokens": 10000, "outputTokens": 50, "reasoningOutputTokens": 10, "totalTokens": 30050},
                "last": {"inputTokens": 16000, "cachedInputTokens": 0, "outputTokens": 50, "reasoningOutputTokens": 10, "totalTokens": 16050}}}},
        ]
        for e in seq:
            b._handle(run, e)
        b._finish(run, {"id": tid, "status": "completed"})
        self.assertEqual(run.backend_status, "ok")
        self.assertEqual(run.final_answer(), "Hello")
        self.assertEqual(run.text, "Hello")
        self.assertEqual(run.tool_calls, 1)
        self.assertEqual(run.thinking_events, 1)
        self.assertEqual(run.steers[0]["state"], "delivered")
        self.assertEqual(run.usage["input"], 16000)  # the 14k tokens before this turn are not counted
        self.assertEqual(run.usage["output"], 50)

    def test_other_threads_are_ignored(self):
        b = CodexBackend(A["gpt"], {"session": "s"}, {}, {})
        b.thread_id = "main"
        run = make_run(A["gpt"])
        b.turn_id = "t1"
        for e in [
            {"method": "item/started", "params": {"threadId": "child", "item": {"type": "commandExecution", "command": "cat x"}}},
            {"method": "item/agentMessage/delta", "params": {"threadId": "child", "delta": "CHILD"}},
            {"method": "item/completed", "params": {"threadId": "child", "item": {"type": "agentMessage", "text": "CHILD", "phase": "final_answer"}}},
            {"method": "item/agentMessage/delta", "params": {"threadId": "main", "delta": "Hi"}},
            {"method": "item/completed", "params": {"threadId": "main", "item": {"type": "agentMessage", "text": "Hi", "phase": "final_answer"}}},
        ]:
            b._handle(run, e)
        b._finish(run, {"id": "t1", "status": "completed"})
        self.assertEqual(run.final_answer(), "Hi")
        self.assertEqual(run.tool_calls, 0)

    def test_no_tools_config_turns_off_nested_tools(self):
        cfg = thread_config("low", "none")
        for k in ("features.shell_tool", "features.view_image", "features.multi_agent_v2", "features.image_generation"):
            self.assertIs(cfg[k], False, k)
        self.assertEqual(cfg["web_search"], "disabled")
        self.assertNotIn("web_search", thread_config("low", "read"))
        self.assertIs(thread_config("low", "read")["features.multi_agent_v2"], False)

    def test_interrupted_and_other_failures(self):
        b = CodexBackend(A["gpt"], {"session": "s"}, {}, {})
        run = make_run(A["gpt"])
        b._finish(run, {"status": "interrupted"})
        self.assertEqual(run.backend_status, "aborted")
        run = make_run(A["gpt"])
        b._finish(run, {"status": "failed", "error": {"message": "slow down", "codexErrorInfo": "rateLimitExceeded"}})
        self.assertEqual(run.backend_status, "rate_limited")
        run = make_run(A["gpt"])
        b._finish(run, {"status": "failed", "error": {"message": "x", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}}})
        self.assertEqual(run.backend_status, "rate_limited")
        run = make_run(A["gpt"])
        b._finish(run, {"status": "failed", "error": {"message": "bad", "codexErrorInfo": "badRequest"}})
        self.assertEqual(run.backend_status, "failed")


class RunSnapshotTest(unittest.TestCase):
    def test_tail_and_cursor(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            run = make_run(A["wide"])
            run.add_text("一二三四五六七八九十")
            s = run.snapshot(20, tail=3)
            self.assertEqual(s["tail"], "八九十")
            self.assertEqual(s["cursor"], 10)
            run.add_text("abc")
            s2 = run.snapshot(20, tail=400, since=s["cursor"])
            self.assertEqual(s2["tail"], "abc")
            self.assertEqual(s2["cursor"], 13)
            self.assertIn("waiting for the model, progress unknown", run.snapshot(0)["progress_note"])
        finally:
            loop.close()


class StoreTest(unittest.TestCase):
    def test_orphans_and_keys(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"ADVISORS_HOME": d}):
            meta = {"session": "wide-1-ab", "advisor": "wide", "turns": 1, "busy_run": "wide-1-ab:1"}
            store.save_session(meta)
            store.save_turn_meta("wide-1-ab", 1, {"status": "running"})
            store.save_turn_meta("wide-1-ab", 2, {"status": "ok"})
            self.assertEqual(store.mark_orphans_unknown(), ["wide-1-ab:1"])
            self.assertEqual(store.load_turn_meta("wide-1-ab", 1)["status"], "unknown")
            self.assertEqual(store.load_turn_meta("wide-1-ab", 2)["status"], "ok")
            self.assertIsNone(store.load_session("wide-1-ab")["busy_run"])
            store.save_key("k1", "wide-1-ab:1", "dig")
            self.assertEqual(store.load_key("k1")["run"], "wide-1-ab:1")
            self.assertIsNone(store.load_key("k2"))
            self.assertTrue(store.valid_session_name("wide-1121-0712-ab12"))
            self.assertFalse(store.valid_session_name("../etc"))
            self.assertFalse(store.valid_session_name("a/b"))


GOOD = """
[groups]
g = 3

[advisors.one]
backend = "pi-rpc"
model = "example/model"
efforts = ["high", "low"]
paid = false
group = "g"
"""


class ConfigTest(unittest.TestCase):
    def write(self, d, text):
        f = Path(d) / "advisors.toml"
        f.write_text(text)
        return f

    def test_defaults_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            adv, groups = registry.load_config(self.write(d, GOOD))
            a = adv["one"]
            self.assertEqual(a.efforts, ("low", "high"))  # sorted along the effort ladder
            self.assertEqual(a.default_effort, "high")    # default: the highest allowed
            self.assertEqual(a.read_tools, registry.DEFAULT_READ_TOOLS)
            self.assertEqual(groups, {"g": 3})
            self.assertEqual(registry.caps_for(a), registry.BACKEND_CAPS[registry.PI])

    def test_description_joined_into_one_line(self):
        self.assertEqual(registry._one_line("  First line.\n  second  line\n\n"), "First line. second line")
        self.assertEqual(registry._one_line("第一句。\n第二句，\nthird"), "第一句。第二句， third")

    def test_missing_file_means_no_advisors(self):
        self.assertEqual(registry.load_config(Path("/nonexistent/advisors.toml")), ({}, {}))

    def test_rejects_bad_tables(self):
        bad = {
            "unknown key": GOOD.replace('paid = false', 'paid = false\nno_extensions = true'),
            "paid missing": GOOD.replace('paid = false\n', ''),
            "bad backend": GOOD.replace('"pi-rpc"', '"ollama"'),
            "pi model without provider": GOOD.replace('"example/model"', '"model"'),
            "default not allowed": GOOD.replace('paid = false', 'paid = false\ndefault_effort = "max"'),
            "unknown effort": GOOD.replace('"high", "low"', '"high", "ultra"'),
            "test- prefix": GOOD.replace("[advisors.one]", "[advisors.test-one]"),
            "codex quota on pi": GOOD.replace('paid = false', 'paid = false\nquota = "codex"'),
            "bad group limit": GOOD.replace("g = 3", "g = 0"),
            "top-level typo": GOOD + "\n[advisor.x]\nmodel = 'y'\n",
            "bad note": GOOD + '\n[advisors.one.notes]\nspeed = "fast"\n',
            "toml syntax": GOOD + "\n[[[",
        }
        with tempfile.TemporaryDirectory() as d:
            for name, text in bad.items():
                with self.subTest(name):
                    with self.assertRaises(registry.RegistryError):
                        registry.load_config(self.write(d, text))

    def test_notes_override_caps(self):
        with tempfile.TemporaryDirectory() as d:
            adv, _ = registry.load_config(self.write(d, GOOD + '\n[advisors.one.notes]\nsteer = "verified"\n'))
            self.assertEqual(registry.caps_for(adv["one"])["steer"], "verified")

    def test_registry_reload_keeps_last_good_table(self):
        with tempfile.TemporaryDirectory() as d:
            f = self.write(d, GOOD)
            with mock.patch.dict(os.environ, {"ADVISORS_CONFIG": str(f), "ADVISORS_HOME": d}):
                r = registry.Registry()
                self.assertEqual(set(r.advisors), {"one"})
                self.assertEqual(r.group_limit("g"), 3)
                self.assertEqual(r.group_limit("other"), registry.DEFAULT_GROUP_LIMIT)
                f.write_text(GOOD + "\n[[[")
                os.utime(f, (time.time() + 5, time.time() + 5))
                r.refresh()
                self.assertEqual(set(r.advisors), {"one"})
                self.assertIn("cannot read", r.error)
                f.write_text(GOOD.replace("[advisors.one]", "[advisors.two]"))
                os.utime(f, (time.time() + 10, time.time() + 10))
                r.refresh()
                self.assertEqual(set(r.advisors), {"two"})
                self.assertIsNone(r.error)

    def test_example_config_is_valid(self):
        adv, _ = registry.load_config(Path(__file__).resolve().parent.parent / "examples" / "advisors.toml")
        self.assertTrue(adv)


class ReadToolsViewTest(unittest.TestCase):
    def test_pi_lists_its_allowlist_and_codex_describes_its_sandbox(self):
        pi_adv = next(a for a in A.values() if a.backend == registry.PI and "read_many" in a.read_tools)
        self.assertEqual(read_tools_view(pi_adv), ", ".join(pi_adv.read_tools))
        self.assertIn("web search", read_tools_view(A["gpt"]))


class PluginTest(unittest.TestCase):
    def test_quota_source_from_plugin(self):
        from advisors import plugins
        from advisors.quota import QuotaCache

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "mine.py").write_text(
                "PROBE_BINS = ['mytool']\n"
                "async def q(ctx, advisor, arg, fresh):\n"
                "    return {'text': f'{advisor.id} {arg} fresh={fresh}', 'exhausted': arg == 'empty'}\n"
                "QUOTA_SOURCES = {'mine': q}\n")
            (Path(d) / "broken.py").write_text("raise RuntimeError('boom')\n")
            with self.assertLogs("advisord", level="ERROR"):
                pl = plugins.load(Path(d))
            self.assertEqual(pl.loaded, ["mine.py"])
            self.assertIn("boom", pl.errors["broken.py"])
            self.assertEqual(pl.bins, ["mytool"])
            qc = QuotaCache({}, {}, 60, None, pl)
            adv = registry.Advisor(**{**A["narrow"].__dict__, "quota": "mine:pool"})
            q = asyncio.run(qc.for_advisor(adv, fresh=True))
            self.assertEqual(q["text"], "narrow pool fresh=True")
            adv = registry.Advisor(**{**A["narrow"].__dict__, "quota": "nobody:x"})
            self.assertIn("unknown quota source", asyncio.run(qc.for_advisor(adv))["text"])
            self.assertIn("pay as you go", asyncio.run(qc.for_advisor(A["claude"]))["text"])
            self.assertIsNone(asyncio.run(qc.for_advisor(A["wide"])))


if __name__ == "__main__":
    unittest.main()
