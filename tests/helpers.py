"""Test doubles: a scripted 'AI' that plays the role of the model for the end-to-end test."""
from __future__ import annotations

import copy
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from agent.config import DEFAULTS, deep_merge
from agent.providers.base import ErrorKind, GenerationResult, Provider, ProviderError

SPEC = """# Tiny calculator library

- Provide a Python module `calc.py` with `add(a, b)` returning the sum.
- `calc.divide(a, b)` returns a / b and raises `ValueError` when b is zero.
- Include unit tests (unittest) covering both functions.
"""

CALC_BUGGY = "def add(a, b):\n    return a - b  # intentional bug\n"
CALC_ADD = "def add(a, b):\n    return a + b\n"
CALC_FULL = CALC_ADD + (
    "\n\ndef divide(a, b):\n    if b == 0:\n        raise ValueError('division by zero')\n    return a / b\n")
TEST_ADD = (
    "import unittest\nfrom calc import add\n\n\nclass TestAdd(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    "    def test_add_negative(self):\n        self.assertEqual(add(-1, 1), 0)\n")
TEST_FULL = TEST_ADD.replace("from calc import add", "from calc import add, divide") + (
    "\n\nclass TestDivide(unittest.TestCase):\n"
    "    def test_divide(self):\n        self.assertEqual(divide(6, 3), 2)\n"
    "    def test_zero(self):\n        with self.assertRaises(ValueError):\n            divide(1, 0)\n")


def impl(summary, edits, commands=(), done=True):
    return json.dumps({"summary": summary, "edits": [{"path": p, "content": c} for p, c in edits.items()],
                       "delete": [], "commands": list(commands), "done": done, "notes": ""})


class ScriptedProvider(Provider):
    """Routes on the '### PHASE:' marker of each prompt, like a (very predictable) model."""
    name = "gemini"
    env_vars = ()

    def __init__(self, fail_after: int | None = None, fail_kind: ErrorKind = ErrorKind.QUOTA, name: str = "gemini"):
        super().__init__({})
        self.name = name
        self.calls: list[str] = []
        self.fail_after = fail_after
        self.fail_kind = fail_kind
        self.t1_attempts = 0

    def is_available(self) -> bool:
        return True

    def generate(self, prompt, system=None, json_mode=True):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise ProviderError(self.fail_kind, "scripted failure", status=429)
        phase = prompt.split("### PHASE:", 1)[1].split("\n", 1)[0].strip()
        self.calls.append(phase)
        return GenerationResult(text=self._answer(phase, prompt), provider=self.name)

    def _answer(self, phase: str, prompt: str) -> str:
        if phase == "REQUIREMENTS":
            return json.dumps({"requirements": [
                {"description": "calc.add returns the sum of two numbers", "source": "add(a, b)"},
                {"description": "calc.divide divides and raises ValueError on zero divisor", "source": "divide"}]})
        if phase == "TASKS":
            return json.dumps({"tasks": [
                {"id": "a", "title": "Implement add with tests", "description": "Create calc.py add() and tests/test_calc.py",
                 "depends_on": [], "requirements": ["REQ-001"], "verification": "unit tests pass"},
                {"id": "b", "title": "Implement divide with tests", "description": "Add divide() raising ValueError on zero",
                 "depends_on": ["a"], "requirements": ["REQ-002"], "verification": "unit tests pass"}]})
        if phase in ("IMPLEMENT", "REPAIR", "CONTINUE"):
            if "### TASK: T001" in prompt:
                if phase == "IMPLEMENT":      # first attempt contains the intentional failure
                    return impl("add + tests (buggy)", {"calc.py": CALC_BUGGY, "tests/__init__.py": "",
                                                          "tests/test_calc.py": TEST_ADD},
                                ["python -m unittest discover -s tests -v"])
                return impl("fix add", {"calc.py": CALC_ADD}, ["python -m unittest discover -s tests -v"])
            return impl("divide + tests", {"calc.py": CALC_FULL, "tests/test_calc.py": TEST_FULL},
                        ["python -m unittest discover -s tests -v"])
        if phase == "VERIFY":
            ids = [i for i in ("REQ-001", "REQ-002") if f'"{i}"' in prompt]
            return json.dumps({"requirements": [
                {"id": i, "status": "verified", "files": ["calc.py", "tests/test_calc.py"],
                 "evidence": "calc.py implements it and tests/test_calc.py exercises it"} for i in ids],
                "critical_bugs": []})
        raise AssertionError(f"unexpected phase {phase}")


def make_repo(extra_cfg: dict | None = None):
    """A throwaway git repo containing MASTER_SPEC.md; returns (tmpdir, root, cfg)."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "MASTER_SPEC.md").write_text(SPEC, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=root, check=True)
    (root / ".gitignore").write_text("artifact/\n__pycache__/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    cfg = copy.deepcopy(DEFAULTS)
    cfg["max_ai_turns"] = 50
    cfg["providers"] = ["gemini"]
    if extra_cfg:
        deep_merge(cfg, extra_cfg)
    return tmp, root, cfg


QUIET = logging.getLogger("agent-test")
QUIET.addHandler(logging.NullHandler())
