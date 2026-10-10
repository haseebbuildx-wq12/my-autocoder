"""Verification runners: dependency install, syntax checks, per-task checks, final checks."""
from __future__ import annotations

import json
import logging
import shlex
import shutil
from pathlib import Path

from .detect import ProjectInfo, detect, manifest_fingerprint
from .state import StateStore
from .tools import CommandResult, Toolbox


class CheckRunner:
    def __init__(self, tb: Toolbox, cfg: dict, state: StateStore, log: logging.Logger):
        self.tb = tb
        self.cfg = cfg
        self.state = state
        self.log = log
        self._installed_fp: str | None = None   # per-process: a fresh CI runner must reinstall

    def project_info(self) -> ProjectInfo:
        return detect(self.tb.ws, self.cfg)

    # -- install -----------------------------------------------------------------
    def ensure_installed(self, info: ProjectInfo) -> CommandResult | None:
        """Install dependencies once per process and again whenever manifests change."""
        if not info.install:
            return None
        fp = manifest_fingerprint(self.tb.ws) or "override"
        if self._installed_fp == fp:
            return None
        for cmd in info.install:
            res = self.tb.run_command(cmd, kind="install")
            if not res.ok:
                return res
        self._installed_fp = fp
        return None

    # -- syntax ------------------------------------------------------------------
    def syntax(self, files: list[str]) -> list[CommandResult]:
        results: list[CommandResult] = []
        for rel in sorted(set(files)):
            if not self.tb.exists(rel):
                continue
            suffix = Path(rel).suffix.lower()
            try:
                if suffix == ".py":
                    src = self.tb.read_file(rel, self.cfg["max_write_bytes"])
                    try:
                        compile(src, rel, "exec")
                        results.append(CommandResult(f"syntax:{rel}", 0, kind="syntax"))
                    except SyntaxError as exc:
                        results.append(CommandResult(
                            f"syntax:{rel}", 1, kind="syntax",
                            stderr=f'File "{rel}", line {exc.lineno}\n    SyntaxError: {exc.msg}'))
                elif suffix == ".json":
                    try:
                        json.loads(self.tb.read_file(rel, self.cfg["max_write_bytes"]))
                        results.append(CommandResult(f"syntax:{rel}", 0, kind="syntax"))
                    except json.JSONDecodeError as exc:
                        results.append(CommandResult(f"syntax:{rel}", 1, kind="syntax",
                                                     stderr=f"{rel}: invalid JSON: {exc}"))
                elif suffix in (".yml", ".yaml"):
                    import yaml
                    try:
                        list(yaml.safe_load_all(self.tb.read_file(rel, self.cfg["max_write_bytes"])))
                        results.append(CommandResult(f"syntax:{rel}", 0, kind="syntax"))
                    except yaml.YAMLError as exc:
                        results.append(CommandResult(f"syntax:{rel}", 1, kind="syntax",
                                                     stderr=f"{rel}: invalid YAML: {exc}"))
                elif suffix in (".js", ".mjs", ".cjs") and shutil.which("node"):
                    results.append(self.tb.run_command(f"node --check {shlex.quote(rel)}", kind="syntax"))
                elif suffix == ".php" and shutil.which("php"):
                    results.append(self.tb.run_command(f"php -l {shlex.quote(rel)}", kind="syntax"))
            except Exception as exc:  # unreadable/binary file: not a syntax matter
                self.log.debug("syntax check skipped for %s: %s", rel, exc)
        return results

    # -- per-task verification ---------------------------------------------------
    def run_task_checks(self, task: dict, changed_files: list[str], ai_commands: list[str]):
        """Returns (ok, results, failure). Stops at the first failing check so the AI fixes
        one thing at a time with a focused error."""
        info = self.project_info()
        results: list[CommandResult] = []

        failing = self.ensure_installed(info)
        if failing is not None:
            results.append(failing)
            return False, results, self.tb.inspect_errors(failing)

        if "syntax" in self.cfg["task_checks"]:
            for res in self.syntax(changed_files):
                results.append(res)
                if not res.ok:
                    return False, results, self.tb.inspect_errors(res)

        detected_tests = list(info.test) if "tests" in self.cfg["task_checks"] else []
        planned: list[tuple[str, str]] = []
        seen: set[str] = set()
        for cmd in [*ai_commands, *task.get("verification_commands", []), *self.cfg["verification_commands"]]:
            if cmd not in seen:
                seen.add(cmd)
                planned.append((cmd, "tests" if cmd in detected_tests else "command"))
        for cmd in detected_tests:
            if cmd not in seen:
                seen.add(cmd)
                planned.append((cmd, "tests"))

        for cmd, kind in planned:
            res = self.tb.run_command(cmd, kind=kind)
            if res.exit_code == 126 and "rejected by safety policy" in res.stderr:
                # A command the safety policy refuses is not a failing task: skip it (it proves
                # nothing) and rely on the remaining checks. Retrying would only burn AI turns.
                self.log.warning("skipping unsafe verification command %r: %s", cmd, res.stderr.strip()[:160])
                continue
            results.append(res)
            if not res.ok:
                return False, results, self.tb.inspect_errors(res)
        return True, results, None

    # -- final checks ------------------------------------------------------------
    def run_final_checks(self) -> list[dict]:
        info = self.project_info()
        report: list[dict] = []
        failing = self.ensure_installed(info)
        if failing is not None:
            err = self.tb.inspect_errors(failing)
            return [{"name": "install", "status": "fail", "commands": [failing.summary()],
                     "excerpt": err["excerpt"], "signature": err["signature"], "files": err["files"]}]
        table = {"tests": info.test, "lint": info.lint, "typecheck": info.typecheck, "build": info.build}
        names = [n for n in self.cfg["final_checks"] if n in table]
        for name in names:
            cmds = table[name]
            if not cmds:
                report.append({"name": name, "status": "skipped", "commands": [], "note": "nothing detected"})
                continue
            report.append(self._run_group(name, cmds))
        if self.cfg["verification_commands"]:
            report.append(self._run_group("custom", self.cfg["verification_commands"]))
        return report

    def _run_group(self, name: str, cmds: list[str]) -> dict:
        summaries = []
        for cmd in cmds:
            res = self.tb.run_command(cmd, kind=name if name != "custom" else "command")
            summaries.append(res.summary())
            if not res.ok:
                err = self.tb.inspect_errors(res)
                return {"name": name, "status": "fail", "commands": summaries,
                        "excerpt": err["excerpt"], "signature": err["signature"], "files": err["files"]}
        return {"name": name, "status": "pass", "commands": summaries}
