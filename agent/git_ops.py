"""Git checkpoints. All commands run at the repository root."""
from __future__ import annotations

import base64
import logging
import os
import subprocess
from pathlib import Path


class GitError(Exception):
    pass


class Git:
    def __init__(self, root: Path, cfg: dict, log: logging.Logger):
        self.root = Path(root).resolve()
        self.cfg = cfg
        self.log = log

    # -- plumbing ----------------------------------------------------------------
    def _run(self, *args: str, check: bool = True, env_extra: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            ["git", *args], cwd=self.root, env=env, capture_output=True, text=True,
            errors="replace", stdin=subprocess.DEVNULL,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[:500]}")
        return proc

    def _identity_args(self) -> list[str]:
        name = self._run("config", "user.name", check=False).stdout.strip()
        email = self._run("config", "user.email", check=False).stdout.strip()
        args: list[str] = ["-c", "commit.gpgsign=false"]
        if not name:
            args += ["-c", f"user.name={self.cfg['git']['author_name']}"]
        if not email:
            args += ["-c", f"user.email={self.cfg['git']['author_email']}"]
        return args

    def ensure_repo(self) -> None:
        if self._run("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
            self._run("init", "-q")
            self.log.info("initialised a new git repository at %s", self.root)

    def head(self) -> str | None:
        proc = self._run("rev-parse", "--short", "HEAD", check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    # -- status / diff -----------------------------------------------------------
    def status(self, paths: list[str] | None = None) -> list[tuple[str, str]]:
        args = ["status", "--porcelain=v1", "-z", "-uall"]
        if paths:
            args += ["--", *paths]
        out = self._run(*args).stdout
        entries = [e for e in out.split("\0") if e]
        result: list[tuple[str, str]] = []
        skip_next = False
        for entry in entries:
            if skip_next:
                skip_next = False
                continue
            code, path = entry[:2], entry[3:]
            if "R" in code or "C" in code:
                skip_next = True
            result.append((code, path))
        return result

    def diff(self, paths: list[str] | None = None, max_chars: int = 20000) -> str:
        args = ["diff", "HEAD", "--stat", "--patch"] if self.head() else ["diff", "--cached", "--stat", "--patch"]
        if paths:
            args += ["--", *paths]
        text = self._run(*args, check=False).stdout
        return text if len(text) <= max_chars else text[:max_chars] + "\n[... diff truncated ...]"

    # -- commits -----------------------------------------------------------------
    def commit(self, message: str, paths: list[str]) -> bool:
        """Stage `paths` and commit if anything changed. Returns True if a commit was made."""
        if not self.cfg["git"]["commit"]:
            return False
        for p in paths:
            self._run("add", "-A", "--", p, check=False)
        if self._run("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self._run(*self._identity_args(), "commit", "-q", "-m", message)
        self.log.info("git commit: %s", message)
        return True

    def head_full(self) -> str | None:
        proc = self._run("rev-parse", "HEAD", check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def tracked_at(self, path: Path, ref: str | None = "HEAD") -> bool:
        """Is `path` part of commit `ref`?"""
        if not ref:
            return False
        rel = os.path.relpath(path, self.root)
        return self._run("cat-file", "-e", f"{ref}:{rel}", check=False).returncode == 0

    def restore(self, abs_paths: list[Path], ref: str | None = "HEAD") -> None:
        """Roll files back to their state at `ref`; files that do not exist there are deleted."""
        for path in abs_paths:
            rel = os.path.relpath(path, self.root)
            if self.tracked_at(path, ref):
                self._run("checkout", ref, "--", rel, check=False)
            elif path.is_file():
                path.unlink()

    # -- push --------------------------------------------------------------------
    def _auth_env(self) -> dict[str, str]:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            return {}
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }

    def push(self) -> bool:
        if not (self.cfg["git"]["push"] or os.environ.get("AGENT_PUSH") == "1"):
            return False
        if not self._run("remote", check=False).stdout.strip():
            return False
        env = self._auth_env()
        branch = self._run("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() or "HEAD"
        for attempt in range(2):
            proc = self._run("push", "origin", f"HEAD:{branch}", check=False, env_extra=env)
            if proc.returncode == 0:
                return True
            self.log.warning("git push failed (attempt %d): %s", attempt + 1, proc.stderr.strip()[:300])
            if attempt == 0:
                self._run(*self._identity_args(), "pull", "--rebase", "origin", branch, check=False, env_extra=env)
        return False
