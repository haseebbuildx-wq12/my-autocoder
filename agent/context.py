"""Builds focused prompts. The whole repository is never sent: only the task, its requirements,
the files that matter (failure files, files already touched, dependencies, imports) and compact
history, all under configurable limits."""
from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath

from . import prompts
from .checks import CheckRunner
from .state import StateStore
from .tools import Toolbox, is_secret_file, UnsafePathError
from .util import Redactor, truncate

MANIFEST_FILES = ["package.json", "composer.json", "requirements.txt", "pyproject.toml", "pom.xml", "phpunit.xml"]

_PY_IMPORT = [re.compile(r"^\s*from\s+([\w\.]+)\s+import\s", re.M), re.compile(r"^\s*import\s+([\w\.]+)", re.M)]
_JS_IMPORT = re.compile(r"""(?:require\(\s*|from\s+|import\s+)['"](\.{1,2}/[^'"]+)['"]""")
_PHP_IMPORT = re.compile(r"""(?:require|include)(?:_once)?\s*\(?\s*['"]([^'"]+\.php)['"]""")


class ContextBuilder:
    def __init__(self, tb: Toolbox, cfg: dict, state: StateStore, checks: CheckRunner, redactor: Redactor):
        self.tb = tb
        self.cfg = cfg
        self.state = state
        self.checks = checks
        self.redactor = redactor
        self.spec_text = ""

    def set_spec(self, spec_text: str) -> None:
        self.spec_text = spec_text

    # -- small pieces ------------------------------------------------------------
    def outline(self) -> str:
        heads = [ln.strip() for ln in self.spec_text.splitlines() if ln.lstrip().startswith("#")]
        return truncate("\n".join(heads[:60]), 3000)

    def structure(self, limit: int = 300) -> str:
        files = self.tb.list_files(max_entries=limit + 1)
        text = "\n".join(files[:limit]) or "(empty project)"
        return text + ("\n[... more files not listed ...]" if len(files) > limit else "")

    def requirements_for(self, task: dict) -> list[dict]:
        out = []
        for rid in task.get("requirements", []):
            req = self.state.requirement(rid)
            if req:
                out.append({"id": req["id"], "description": req["description"], "source": req.get("source", "")})
        return out

    def bug_history(self, task_id: str) -> str:
        bugs = [b for b in self.state.bugs if b["task_id"] == task_id][-3:]
        return "\n".join(f"- attempt {b['attempt']} [{b['status']}]: {truncate(b['error'].strip(), 240)}" for b in bugs)

    # -- file selection ----------------------------------------------------------
    def _candidates(self, base_dir: str, spec: str, exts: list[str]) -> list[str]:
        rels = []
        stem = posixpath.normpath(posixpath.join(base_dir, spec))
        for cand in [stem, *(stem + e for e in exts), *(posixpath.join(stem, "index" + e) for e in exts)]:
            rels.append(cand)
        return rels

    def neighbors(self, rel: str) -> list[str]:
        """Files imported by `rel` (best effort for Python, JS/TS and PHP)."""
        try:
            text, _ = self.tb.read_text_limited(rel, 60000)
        except (UnsafePathError, OSError):
            return []
        base = posixpath.dirname(rel)
        found: list[str] = []
        suffix = PurePosixPath(rel).suffix
        if suffix == ".py":
            for rx in _PY_IMPORT:
                for mod in rx.findall(text):
                    path = mod.replace(".", "/")
                    for root in {"", base}:
                        for cand in (f"{path}.py", f"{path}/__init__.py"):
                            found.append(posixpath.normpath(posixpath.join(root, cand)))
        elif suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            for spec in _JS_IMPORT.findall(text):
                found += self._candidates(base, spec, [".js", ".jsx", ".ts", ".tsx"])
        elif suffix == ".php":
            for spec in _PHP_IMPORT.findall(text):
                found += self._candidates(base, spec, [])
                found.append(posixpath.normpath(spec))
        return [f for f in found if self.tb.exists(f)]

    def pack_files(self, ordered: list[str], budget: int) -> str:
        blocks, used, seen = [], 0, set()
        for rel in ordered:
            if rel in seen or len(seen) >= self.cfg["max_context_files"]:
                continue
            seen.add(rel)
            if is_secret_file(rel) or not self.tb.exists(rel):
                continue
            try:
                text, truncated = self.tb.read_text_limited(rel)
            except (UnsafePathError, OSError):
                continue
            block = f"=== {rel}{' (truncated)' if truncated else ''} ===\n{self.redactor.redact(text)}\n"
            if used + len(block) > budget:
                remaining = budget - used
                if remaining > 1500 and not blocks:
                    blocks.append(block[:remaining] + "\n[... cut to fit prompt budget ...]\n")
                    used = budget
                continue
            blocks.append(block)
            used += len(block)
        return "\n".join(blocks)

    # -- the implementation prompt -------------------------------------------------
    def task_prompt(self, task: dict, rt: dict, mode: str) -> str:
        info = self.checks.project_info()
        commands = "; ".join(f"{k}: {', '.join(v)}" for k, v in
                             (("install", info.install), ("test", info.test), ("lint", info.lint),
                              ("typecheck", info.typecheck), ("build", info.build)) if v) or "none detected yet"
        failure = rt.get("failure") if mode in ("repair", "continue") else None
        ordered: list[str] = []
        if failure:
            ordered += failure.get("files", [])
        ordered += rt.get("files", []) + task.get("files_changed", []) + task.get("files", [])
        for dep in task.get("depends_on", []):
            dep_task = self.state.task(dep)
            if dep_task:
                ordered += dep_task.get("files_changed", [])
        ordered += [n for f in list(ordered)[:8] for n in self.neighbors(f)]
        ordered += [m for m in MANIFEST_FILES if self.tb.exists(m)]

        kwargs = dict(
            mode=mode, task=task, requirements=self.requirements_for(task),
            project_info=info.describe(), commands=commands,
            outline=self.outline() if mode == "implement" else "",
            structure=self.structure() if mode == "implement" else "",
            bug_history=self.bug_history(task["id"]),
            decisions=truncate(self.state.decisions_tail(1200), 1200, keep_tail=True) if mode == "implement" else "",
            notes=task.get("notes", []), failure=failure, hint=rt.get("hint"),
        )
        fixed = len(prompts.implement_prompt(files_block="", **kwargs))
        budget = max(3000, self.cfg["max_prompt_chars"] - fixed - 1500)
        return prompts.implement_prompt(files_block=self.pack_files(ordered, budget), **kwargs)

    # -- final verification context ------------------------------------------------
    def verification_files(self, batch: list[dict], budget: int) -> tuple[str, dict]:
        task_info: dict = {}
        ordered: list[str] = []
        for req in batch:
            for tid in req.get("tasks", []):
                t = self.state.task(tid)
                if not t or tid in task_info:
                    continue
                ev = t.get("evidence") or {}
                task_info[tid] = {
                    "title": t["title"], "status": t["status"],
                    "verification": t.get("verification", ""),
                    "checks": [c.get("cmd") + (" ✓" if c.get("ok") else " ✗") for c in ev.get("checks", [])][:8],
                    "weak_evidence": ev.get("weak"),
                    "files": t.get("files_changed", [])[:15],
                }
                ordered += t.get("files_changed", [])
            ordered += req.get("files", [])
        return self.pack_files(ordered, budget), task_info
