"""The coding agent's tool belt. Every operation is confined to the workspace directory.

list_files, read_file, search_code, write_file, edit_file, delete_file,
run_command, run_tests, inspect_errors, inspect_git_diff, git_commit
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .detect import IGNORE_DIRS, ProjectInfo
from .git_ops import Git
from .safety import UnsafeCommandError, clean_env, validate_command
from .util import Redactor, atomic_write_text, truncate


class UnsafePathError(Exception):
    pass


class ProtectedPathError(Exception):
    pass


@dataclass
class CommandResult:
    cmd: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False
    kind: str = "command"          # command | tests | syntax | install | lint | typecheck | build

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def combined(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()

    def summary(self) -> dict:
        return {"cmd": self.cmd, "kind": self.kind, "exit_code": self.exit_code, "ok": self.ok,
                "timed_out": self.timed_out, "duration_s": round(self.duration, 2)}


SECRET_FILE_RE = re.compile(r"(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx)|id_rsa.*|\.npmrc|\.netrc)$", re.I)
ALLOWED_SECRET_TEMPLATES = (".env.example", ".env.sample", ".env.dist", ".env.template")


def is_secret_file(rel: str) -> bool:
    return bool(SECRET_FILE_RE.search(rel)) and not rel.endswith(ALLOWED_SECRET_TEMPLATES)


class Toolbox:
    def __init__(self, repo_root: Path, cfg: dict, git: Git, redactor: Redactor, log: logging.Logger):
        self.repo_root = Path(repo_root).resolve()
        self.cfg = cfg
        self.git = git
        self.redactor = redactor
        self.log = log
        self.ws = (self.repo_root / cfg["workspace_dir"]).resolve()
        self.ws.mkdir(parents=True, exist_ok=True)
        self._protected = self._compute_protected()

    # -- paths -------------------------------------------------------------------
    def _compute_protected(self) -> list[str]:
        out = [".git"]
        for key in ("spec_path", "state_dir", "artifact_dir"):
            full = (self.repo_root / self.cfg[key]).resolve()
            try:
                out.append(full.relative_to(self.ws).as_posix())
            except ValueError:
                pass
        return out

    def resolve(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
            raise UnsafePathError("empty or invalid path")
        norm = rel.replace("\\", "/")
        p = PurePosixPath(norm)
        if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", norm) or norm.startswith("~"):
            raise UnsafePathError(f"path must be relative and stay inside the workspace: {rel}")
        if ".git" in p.parts:
            raise UnsafePathError("access to .git is not allowed")
        full = (self.ws / p).resolve()
        try:
            full.relative_to(self.ws)
        except ValueError:
            raise UnsafePathError(f"path escapes the workspace: {rel}") from None
        return full

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.ws).as_posix()

    def is_protected(self, rel: str) -> bool:
        norm = PurePosixPath(rel.replace("\\", "/")).as_posix()
        return any(norm == p or norm.startswith(p + "/") for p in self._protected)

    def exists(self, rel: str) -> bool:
        try:
            return self.resolve(rel).is_file()
        except UnsafePathError:
            return False

    # -- file tools --------------------------------------------------------------
    def list_files(self, subdir: str = ".", max_entries: int = 500) -> list[str]:
        base = self.ws if subdir in (".", "") else self.resolve(subdir)
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
            for name in sorted(filenames):
                out.append(self.rel(Path(dirpath) / name))
                if len(out) >= max_entries:
                    return out
        return out

    def read_file(self, rel: str, max_bytes: int | None = None) -> str:
        text, _ = self.read_text_limited(rel, max_bytes)
        return text

    def read_text_limited(self, rel: str, max_bytes: int | None = None) -> tuple[str, bool]:
        path = self.resolve(rel)
        if is_secret_file(rel):
            raise UnsafePathError(f"refusing to read a credentials file: {rel}")
        limit = max_bytes or self.cfg["max_file_bytes"]
        with open(path, "rb") as fh:
            raw = fh.read(limit + 1)
        if b"\x00" in raw[:8000]:
            raise UnsafePathError(f"binary file: {rel}")
        truncated = len(raw) > limit
        return raw[:limit].decode("utf-8", errors="replace"), truncated

    def search_code(self, pattern: str, glob: str | None = None, max_results: int = 50) -> list[dict]:
        rx = re.compile(pattern)
        results: list[dict] = []
        for rel in self.list_files(max_entries=5000):
            if glob and not PurePosixPath(rel).match(glob):
                continue
            if is_secret_file(rel):
                continue
            try:
                text, _ = self.read_text_limited(rel, 400_000)
            except (UnsafePathError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    results.append({"path": rel, "line": i, "text": line.strip()[:200]})
                    if len(results) >= max_results:
                        return results
        return results

    def write_file(self, rel: str, content: str) -> None:
        if self.is_protected(rel):
            raise ProtectedPathError(f"{rel} is protected")
        if len(content.encode("utf-8")) > self.cfg["max_write_bytes"]:
            raise UnsafePathError(f"{rel}: content exceeds max_write_bytes")
        atomic_write_text(self.resolve(rel), content)

    def edit_file(self, rel: str, old: str, new: str) -> None:
        if self.is_protected(rel):
            raise ProtectedPathError(f"{rel} is protected")
        text = self.read_file(rel, self.cfg["max_write_bytes"])
        count = text.count(old)
        if not old or count != 1:
            raise ValueError(f"{rel}: 'old' text must occur exactly once (found {count})")
        self.write_file(rel, text.replace(old, new, 1))

    def delete_file(self, rel: str) -> bool:
        if self.is_protected(rel):
            raise ProtectedPathError(f"{rel} is protected")
        path = self.resolve(rel)
        if path.is_file():
            path.unlink()
            return True
        return False

    # -- commands ----------------------------------------------------------------
    def _argv_for_runtime(self, argv: list[str]) -> list[str]:
        exe = argv[0]
        if exe in ("python", "python3") and not shutil.which(exe):
            return [sys.executable, *argv[1:]]
        if exe in ("pip", "pip3") and not shutil.which(exe):
            return [sys.executable, "-m", "pip", *argv[1:]]
        return argv

    def run_command(self, cmd: str, timeout: int | None = None, kind: str = "command") -> CommandResult:
        """Validate and run one command in the workspace. Never raises: rejected or failed
        commands come back as a failing CommandResult so the AI can react to them."""
        timeout = timeout or self.cfg["command_timeout_seconds"]
        try:
            argv = validate_command(cmd, self.ws, self.cfg["allowed_executables_extra"])
        except UnsafeCommandError as exc:
            return CommandResult(cmd, 126, stderr=f"command rejected by safety policy: {exc}", kind=kind)
        argv = self._argv_for_runtime(argv)
        if "/" not in argv[0] and not os.path.isabs(argv[0]) and not shutil.which(argv[0]):
            return CommandResult(cmd, 127, stderr=f"executable not found: {argv[0]} (is it installed on this runner?)", kind=kind)
        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.Popen(
                argv, cwd=self.ws, env=clean_env(), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            return CommandResult(cmd, 127, stderr=f"could not start command: {exc}", kind=kind)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except OSError:
                pass
            out, err = proc.communicate()
        limit = self.cfg["max_command_output_chars"]
        res = CommandResult(
            cmd=cmd,
            exit_code=proc.returncode if not timed_out else 124,
            stdout=self.redactor.redact(truncate(out or "", limit, keep_tail=True)),
            stderr=self.redactor.redact(truncate(err or "", limit, keep_tail=True)),
            duration=time.monotonic() - start,
            timed_out=timed_out,
            kind=kind,
        )
        if timed_out:
            res.stderr += f"\ncommand timed out after {timeout}s"
        return res

    def run_tests(self, info: ProjectInfo) -> list[CommandResult]:
        results = []
        for cmd in info.test:
            results.append(self.run_command(cmd, kind="tests"))
            if not results[-1].ok:
                break
        return results

    # -- diagnostics -------------------------------------------------------------
    _PATH_RES = [
        re.compile(r'File "([^"]+)", line (\d+)'),
        re.compile(r"\(?((?:[\w.\-]+/)*[\w.\-]+\.(?:js|jsx|ts|tsx|mjs|cjs|php|java|py|rb|go|rs)):(\d+)"),
        re.compile(r"in (\S+\.php) on line (\d+)"),
        re.compile(r"^\s*(\S+\.(?:py|js|ts|php|java))\s*[:(]\s*\d+", re.M),
    ]

    def inspect_errors(self, result: CommandResult) -> dict:
        text = result.combined()
        files: list[str] = []
        for rx in self._PATH_RES:
            for m in rx.finditer(text):
                cand = m.group(1)
                for variant in (cand, os.path.relpath(cand, self.ws) if os.path.isabs(cand) else cand):
                    try:
                        if self.exists(variant) and variant not in files:
                            files.append(variant)
                            break
                    except Exception:
                        continue
        lines = [ln for ln in text.splitlines() if ln.strip()]
        interesting = [ln for ln in lines if re.search(r"error|exception|fail|assert|traceback|cannot|undefined|not found", ln, re.I)]
        excerpt_lines = (lines[-40:] if len(lines) <= 60 else interesting[:15] + ["..."] + lines[-25:])
        excerpt = "\n".join(excerpt_lines)
        sig_src = "\n".join(interesting[:8] or lines[-8:])
        sig_src = re.sub(r"0x[0-9a-fA-F]+|\d+(\.\d+)?|/[\w./\-]+", "N", sig_src)
        signature = hashlib.sha1(f"{result.cmd}|{result.exit_code}|{sig_src}".encode()).hexdigest()[:12]
        return {
            "cmd": result.cmd, "kind": result.kind, "exit_code": result.exit_code,
            "excerpt": truncate(excerpt, 6000, keep_tail=True), "files": files[:10], "signature": signature,
            "timed_out": result.timed_out,
        }

    def inspect_git_diff(self, max_chars: int = 20000) -> str:
        return self.git.diff([self.cfg["workspace_dir"]], max_chars)

    def git_commit(self, message: str) -> bool:
        paths = [self.cfg["workspace_dir"], self.cfg["state_dir"]]
        committed = self.git.commit(message, paths)
        if committed:
            self.git.push()
        return committed
