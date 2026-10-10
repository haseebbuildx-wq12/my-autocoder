"""Final packaging: artifact/project.zip and artifact/FINAL_REPORT.md.

The ZIP contains only the verified project. It is scanned for secrets first; anything found
blocks packaging and becomes a corrective task.
"""
from __future__ import annotations

import fnmatch
import os
import zipfile
from pathlib import Path

from .detect import IGNORE_DIRS
from .tools import Toolbox, is_secret_file
from .util import atomic_write_text, now_iso, scan_for_secrets

EXCLUDE_DIRS = IGNORE_DIRS | {"agent_state", "artifact", ".github_agent", ".agent_cache", "coverage", ".nyc_output"}
EXCLUDE_GLOBS = ["*.pyc", "*.pyo", ".DS_Store", "*.log", ".coverage"]


class SecretsFound(Exception):
    def __init__(self, findings: list[tuple[str, str]]):
        super().__init__(f"{len(findings)} potential secret(s) found in the project")
        self.findings = findings


class Packager:
    def __init__(self, tb: Toolbox, cfg: dict, state):
        self.tb = tb
        self.cfg = cfg
        self.state = state
        self.out_dir = tb.repo_root / cfg["artifact_dir"]

    @property
    def zip_path(self) -> Path:
        return self.out_dir / "project.zip"

    @property
    def report_path(self) -> Path:
        return self.out_dir / "FINAL_REPORT.md"

    def project_files(self) -> list[Path]:
        files = []
        for dirpath, dirnames, filenames in os.walk(self.tb.ws):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                rel = path.relative_to(self.tb.ws).as_posix()
                if is_secret_file(rel) or any(fnmatch.fnmatch(name, g) for g in EXCLUDE_GLOBS):
                    continue
                if path.is_symlink():
                    continue
                files.append(path)
        return files

    def scan(self, files: list[Path]) -> list[tuple[str, str]]:
        findings = []
        for path in files:
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for finding in scan_for_secrets(text):
                findings.append((path.relative_to(self.tb.ws).as_posix(), finding))
        return findings

    def build(self) -> tuple[Path, Path]:
        files = self.project_files()
        findings = self.scan(files)
        if findings:
            raise SecretsFound(findings)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.zip_path.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, path.relative_to(self.tb.ws).as_posix())
        os.replace(tmp, self.zip_path)
        atomic_write_text(self.report_path, self.report(len(files)))
        return self.zip_path, self.report_path

    # -- report ------------------------------------------------------------------
    def report(self, file_count: int) -> str:
        st = self.state
        p = st.progress
        checks = {c["name"]: c for c in p.get("final_checks", [])}

        def verdict(name: str) -> str:
            c = checks.get(name)
            if c is None or c["status"] == "skipped":
                return "N/A (nothing detected for this project)"
            return "PASS" if c["status"] == "pass" else "FAIL"

        critical = [b for b in st.bugs if b["severity"] == "critical" and b["status"] == "open"]
        all_verified = all(r["status"] == "verified" for r in st.requirements)
        lines = [
            "# Final Report", "",
            "Project status: COMPLETE", "",
            "Requirements:",
            *[f"{r['id']}: {r['status'].upper()}" for r in st.requirements], "",
            "Tests:", verdict("tests"), "",
            "Build:", verdict("build"), "",
            "Critical bugs:", str(len(critical)), "",
            "Final verification:", "PASS" if all_verified and not critical else "FAIL", "",
            "---", "",
            f"Generated: {now_iso()}  ",
            f"Files in ZIP: {file_count}  ",
            f"Tasks completed: {sum(t['status'] == 'completed' for t in st.tasks)}/{len(st.tasks)}  ",
            f"Verification rounds with corrections: {p.get('verification_round', 0)}  ",
            f"Lint: {verdict('lint')}  ",
            f"Type check: {verdict('typecheck')}  ", "",
            "## Requirement evidence", "",
        ]
        for r in st.requirements:
            lines += [f"### {r['id']} — {r['status'].upper()}", r["description"], "",
                      f"Evidence: {r['evidence'] or '-'}  ",
                      f"Files: {', '.join(r['files']) or '-'}  ",
                      f"Tasks: {', '.join(r['tasks']) or '-'}", ""]
        bugs = st.bugs
        lines += ["## Bug history", "",
                  f"{len(bugs)} failure(s) recorded, "
                  f"{sum(b['status'] == 'fixed' for b in bugs)} fixed, "
                  f"{sum(b['status'] == 'abandoned' for b in bugs)} abandoned.", ""]
        return "\n".join(lines)
