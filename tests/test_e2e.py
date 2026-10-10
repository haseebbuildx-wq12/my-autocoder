import json
import os
import unittest
import zipfile
from pathlib import Path

from agent.loop import (Agent, EXIT_COMPLETE, EXIT_CONFIG, EXIT_PAUSED_BUDGET, EXIT_PAUSED_QUOTA)
from agent.providers.base import ErrorKind

from .helpers import QUIET, ScriptedProvider, make_repo


def state(root: Path, name: str):
    return json.loads((root / "agent_state" / name).read_text())


class EndToEnd(unittest.TestCase):
    def test_full_cycle_with_intentional_failure(self):
        """Spec -> plan -> code -> test -> intentional failure -> debug -> pass -> verify -> ZIP."""
        tmp, root, cfg = make_repo()
        self.addCleanup(tmp.cleanup)
        prov = ScriptedProvider()
        code = Agent(root, cfg, providers=[prov], log=QUIET).run()
        self.assertEqual(code, EXIT_COMPLETE)

        progress = state(root, "PROGRESS.json")
        self.assertEqual(progress["status"], "complete")
        tasks = state(root, "TASKS.json")["tasks"]
        self.assertEqual([t["status"] for t in tasks], ["completed", "completed"])
        self.assertEqual(tasks[0]["evidence"]["repairs"], 1)          # the bug was debugged
        self.assertFalse(tasks[0]["evidence"]["weak"])
        bugs = state(root, "BUGS.json")["bugs"]
        self.assertTrue(bugs and all(b["status"] == "fixed" for b in bugs))
        self.assertIn("AssertionError", bugs[0]["error"])
        reqs = state(root, "REQUIREMENTS.json")["requirements"]
        self.assertTrue(all(r["status"] == "verified" for r in reqs))
        self.assertEqual(reqs[0]["tasks"], ["T001"])

        zpath = root / "artifact" / "project.zip"
        self.assertTrue(zpath.exists())
        names = zipfile.ZipFile(zpath).namelist()
        self.assertIn("calc.py", names)
        self.assertFalse([n for n in names if ".git" in n or "__pycache__" in n or "agent_state" in n])
        report = (root / "artifact" / "FINAL_REPORT.md").read_text()
        self.assertIn("Project status: COMPLETE", report)
        self.assertIn("REQ-001: VERIFIED", report)
        self.assertIn("Tests:\nPASS", report)
        self.assertIn("Critical bugs:\n0", report)
        self.assertIn("MASTER_SPEC.md", os.listdir(root))
        self.assertIn("# Tiny calculator", (root / "MASTER_SPEC.md").read_text())   # never modified
        log = os.popen(f"git -C {root} log --format=%s").read()
        self.assertIn("agent: complete T001 (after 1 repair attempt(s))", log)
        self.assertIn("agent: final verification", log)

    def test_quota_pause_then_resume_without_replanning(self):
        tmp, root, cfg = make_repo()
        self.addCleanup(tmp.cleanup)
        first = ScriptedProvider(fail_after=4)                 # 2 planning calls + T001 implement + repair
        code = Agent(root, cfg, providers=[first], log=QUIET).run()
        self.assertEqual(code, EXIT_PAUSED_QUOTA)
        p = state(root, "PROGRESS.json")
        self.assertEqual(p["status"], "paused_quota")
        self.assertIn("unavailable_until", p["providers"]["gemini"])
        done_before = p["completed_task_ids"]

        # later run: cooldown still active -> must not even call the provider
        again = ScriptedProvider()
        self.assertEqual(Agent(root, cfg, providers=[again], log=QUIET).run(), EXIT_PAUSED_QUOTA)
        self.assertEqual(again.calls, [])

        # simulate time passing: clear cooldown, a fresh provider resumes
        p = state(root, "PROGRESS.json")
        p["providers"]["gemini"].pop("unavailable_until")
        (root / "agent_state" / "PROGRESS.json").write_text(json.dumps(p))
        os.system(f"git -C {root} add -A && git -C {root} commit -qm sim")
        resumed = ScriptedProvider()
        code = Agent(root, cfg, providers=[resumed], log=QUIET).run()
        self.assertEqual(code, EXIT_COMPLETE)
        self.assertNotIn("REQUIREMENTS", resumed.calls)       # no re-planning
        self.assertNotIn("TASKS", resumed.calls)
        self.assertTrue(set(done_before) <= set(state(root, "PROGRESS.json")["completed_task_ids"]))

    def test_fallback_to_second_provider(self):
        tmp, root, cfg = make_repo({"providers": ["gemini", "openrouter"]})
        self.addCleanup(tmp.cleanup)
        a = ScriptedProvider(fail_after=0, fail_kind=ErrorKind.RATE_LIMIT, name="gemini")
        b = ScriptedProvider(name="openrouter")
        code = Agent(root, cfg, providers=[a, b], log=QUIET).run()
        self.assertEqual(code, EXIT_COMPLETE)
        self.assertTrue(b.calls)
        self.assertEqual(state(root, "PROGRESS.json")["providers"]["gemini"]["last_error_kind"], "rate_limit")

    def test_auth_error_halts_without_fallback(self):
        tmp, root, cfg = make_repo({"providers": ["gemini", "openrouter"]})
        self.addCleanup(tmp.cleanup)
        a = ScriptedProvider(fail_after=0, fail_kind=ErrorKind.AUTH, name="gemini")
        b = ScriptedProvider(name="openrouter")
        code = Agent(root, cfg, providers=[a, b], log=QUIET).run()
        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual(b.calls, [])                          # no silent fallback
        self.assertEqual(state(root, "PROGRESS.json")["status"], "blocked_config")

    def test_turn_budget_checkpoints_and_continues(self):
        tmp, root, cfg = make_repo({"max_ai_turns": 3})
        self.addCleanup(tmp.cleanup)
        prov = ScriptedProvider()
        codes = []
        for _ in range(10):
            codes.append(Agent(root, cfg, providers=[prov], log=QUIET).run())
            if codes[-1] == EXIT_COMPLETE:
                break
        self.assertEqual(codes[0], EXIT_PAUSED_BUDGET)
        self.assertEqual(codes[-1], EXIT_COMPLETE)

    def test_weakening_tests_is_rejected(self):
        from agent.schema import weakens_tests
        self.assertIsNotNone(weakens_tests("def test_a(self):\n self.assertEqual(1,1)\n", "def test_a(self):\n pass\n"))
        self.assertIsNone(weakens_tests("def test_a(self):\n self.assertEqual(1,1)\n", "def test_a(self):\n self.assertEqual(2,2)\n"))


if __name__ == "__main__":
    unittest.main()
