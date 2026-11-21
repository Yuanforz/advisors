"""The vertex-budget block, end to end and at zero cost, against the extension bundled in pi-extensions/.

- PI_CODING_AGENT_DIR points at a temporary directory: auth.json has only a google-vertex entry for a
  project that does not exist, models.json has only one Vertex model, and extensions/ links the bundled
  vertex-budget.ts.
- PI_VERTEX_BUDGET_DIR points at another temporary directory with a test vertex-budget.json. With a budget
  of 0 the turn must come back as blocked_budget, and no request is sent.
- The control group (budget $1000) does send its request, which fails with 403 because the project does not
  exist (no cost). That proves the first group really was blocked. It needs network access and Google
  application-default credentials, so it only runs with ADVISORS_NETWORK_TESTS=1.
- Your real ~/.pi/agent ledger is never touched; the test checks its line count before and after.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers import DaemonEnv  # noqa: E402

REAL_EXT = Path(__file__).resolve().parent.parent / "pi-extensions" / "vertex-budget.ts"
REAL_LEDGER = Path.home() / ".pi" / "agent" / "vertex-ledger.jsonl"

CARBON = {
    "id": "gemini-4-carbon", "name": "Gemini 4 Carbon", "api": "google-vertex",
    "baseUrl": "https://{location}-aiplatform.googleapis.com", "reasoning": True,
    "thinkingLevelMap": {"off": None, "minimal": None, "low": "low", "medium": "medium", "high": "high",
                         "xhigh": None, "max": None},
    "input": ["text", "image"], "contextWindow": 262144, "maxTokens": 65536,
    "cost": {"input": 4, "output": 20, "cacheRead": 0.2, "cacheWrite": 0},
}


def _ledger_lines() -> int:
    try:
        return len(REAL_LEDGER.read_text().splitlines())
    except FileNotFoundError:
        return 0


@unittest.skipUnless(REAL_EXT.exists(), "pi-extensions/vertex-budget.ts is missing")
class BudgetBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger_before = _ledger_lines()
        # Create the environment first for its temporary directory, then write the isolated pi config
        cls.e = DaemonEnv(advisors=[])
        t = cls.e.tmp
        agent = t / "carbon-agent"
        (agent / "extensions").mkdir(parents=True)
        (agent / "extensions" / "vertex-budget.ts").symlink_to(REAL_EXT)
        (agent / "auth.json").write_text(json.dumps({"google-vertex": {"type": "api_key", "env": {
            "GOOGLE_CLOUD_PROJECT": "advisors-budget-test-nonexistent-0", "GOOGLE_CLOUD_LOCATION": "global"}}}))
        os.chmod(agent / "auth.json", 0o600)
        (agent / "models.json").write_text(json.dumps({"providers": {"google-vertex": {"models": [CARBON]}}}))
        (agent / "settings.json").write_text(json.dumps({"enabledModels": ["google-vertex/gemini-4-carbon"],
                                                         "retry": {"enabled": False}}))
        since = datetime.now(timezone.utc).isoformat()
        for name, credit in (("budget-zero", 0), ("budget-high", 1000)):
            (t / name).mkdir()
            (t / name / "vertex-budget.json").write_text(json.dumps(
                {"since": since, "credit": credit, "grantDay": 15, "grantUsd": 0, "reserve": 0}))
        advisors = []
        for aid, bdir in (("test-carbon-zero", "budget-zero"), ("test-carbon-high", "budget-high")):
            advisors.append({"id": aid, "model": "google-vertex/gemini-4-carbon", "group": "vertex", "paid": True,
                             "efforts": ["low", "medium", "high"], "default_effort": "low", "quota": "vertex-budget",
                             "env": {"PI_CODING_AGENT_DIR": str(agent), "PI_VERTEX_BUDGET_DIR": str(t / bdir),
                                     "PI_OFFLINE": "1"}})
        cls.e.advisors_file.write_text(json.dumps({"advisors": advisors}))
        cls.t = t

    @classmethod
    def tearDownClass(cls):
        cls.e.close()

    def test_zero_budget_is_blocked(self):
        rc, r = self.e.json("ask", "test-carbon-zero", "Reply with exactly: HELLO", "--tools", "none")
        self.assertEqual(rc, 4, r)
        self.assertEqual(r["status"], "blocked_budget")
        self.assertIn("the request was not sent", r["error"])
        self.assertFalse((self.t / "budget-zero" / "vertex-ledger.jsonl").exists())
        ev = (self.e.home / "sessions" / r["session"] / "turns/1.events.jsonl").read_text()
        self.assertIn('"customType": "vertex-budget"', ev)
        self.assertEqual(r["quota"]["limit_usd"], 0)
        # Extensions were not turned off: the pi command line has no --no-extensions
        line = [l for l in self.e.log().splitlines() if f"advisors-{r['session']}" in l and "started pid" in l][0]
        self.assertNotIn("--no-extensions", line)
        self.assertEqual(_ledger_lines(), self.ledger_before)

    @unittest.skipUnless(os.environ.get("ADVISORS_NETWORK_TESTS"), "network control group; set ADVISORS_NETWORK_TESTS=1")
    def test_high_budget_reaches_vertex_and_gets_403(self):
        rc, r = self.e.json("ask", "test-carbon-high", "Reply with exactly: HELLO", "--tools", "none", timeout=120)
        self.assertEqual(rc, 1, r)
        self.assertEqual(r["status"], "failed")
        self.assertRegex(r["error"] or "", r"403|PERMISSION_DENIED|denied|not found|404", r)
        ev = (self.e.home / "sessions" / r["session"] / "turns/1.events.jsonl").read_text()
        self.assertNotIn('"customType": "vertex-budget"', ev)
        self.assertEqual(_ledger_lines(), self.ledger_before)


if __name__ == "__main__":
    unittest.main()
