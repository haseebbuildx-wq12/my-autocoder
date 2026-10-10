"""Command safety. Model-generated commands are validated before anything runs.

Policy (least privilege):
* commands run WITHOUT a shell, so pipes/redirects/chaining are rejected rather than silently misparsed;
* only allow-listed programs run (plus project-local executables such as ./vendor/bin/phpunit);
* inline-code flags (python -c, node -e, php -r ...) are rejected - they bypass any allow-list;
* path-like arguments must stay inside the workspace;
* obviously destructive or credential-exfiltrating patterns are rejected;
* the child environment is scrubbed of secrets (see `clean_env`).
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path, PurePosixPath

from .util import SECRET_ENV_RE


class UnsafeCommandError(Exception):
    pass


DEFAULT_ALLOWED = {
    "python", "python3", "pip", "pip3", "pytest", "ruff", "flake8", "mypy", "black",
    "node", "npm", "npx", "yarn", "pnpm", "tsc", "eslint", "prettier", "jest", "vitest",
    "php", "composer", "phpunit",
    "java", "javac", "mvn", "mvnw", "gradle", "gradlew",
    "make", "go", "cargo", "ruby", "bundle", "rake", "dotnet",
    "git",
}

GIT_READONLY = {"status", "diff", "log", "show", "rev-parse", "ls-files", "branch"}

SHELL_TOKENS = {"&&", "||", ";", "|", "&", ">", ">>", "<", "<<", "2>", "2>&1", "&>"}

DANGEROUS_RE = re.compile(
    r"""(
        \brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~|\*|\$HOME)   # rm -rf / ~ *
      | \bmkfs\b | \bdd\s+if= | :\(\)\s*\{ | /dev/(sd|nvme|disk) | \bshutdown\b | \breboot\b
      | \bcurl\b[^\n]*\|\s*(sh|bash) | \bwget\b[^\n]*\|\s*(sh|bash)
      | \bprintenv\b | (^|\s)env(\s|$) | /etc/(passwd|shadow) | ~/\.ssh | \.ssh/ | \.aws/
      | \.git/config | GITHUB_TOKEN | ACTIONS_RUNTIME | ACTIONS_ID_TOKEN
      | (API_KEY|SECRET_KEY|ACCESS_TOKEN|PRIVATE_KEY)
    )""",
    re.X,
)

INLINE_EVAL = {
    "node": {"-e", "--eval", "-p", "--print"},
    "php": {"-r"},
    "ruby": {"-e"},
    "perl": {"-e", "-E"},
}

NPM_FORBIDDEN = {"publish", "login", "logout", "adduser", "token", "whoami", "owner", "deprecate", "unpublish"}


def _base(exe: str) -> str:
    return os.path.basename(exe)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _check_path_arg(arg: str, ws: Path) -> None:
    value = arg
    if arg.startswith("-"):
        if "=" not in arg:
            return
        value = arg.split("=", 1)[1]
    if not value or any(c.isspace() for c in value) or "://" in value:
        return
    if value.startswith("~"):
        raise UnsafeCommandError(f"home-directory path not allowed: {arg}")
    if value.startswith("/"):
        if not _inside(Path(value), ws):
            raise UnsafeCommandError(f"absolute path outside the workspace: {arg}")
        return
    if ".." in PurePosixPath(value.replace("\\", "/")).parts:
        if not _inside(ws / value, ws):
            raise UnsafeCommandError(f"path escapes the workspace: {arg}")


def validate_command(cmd: str, ws: Path, extra_allowed: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Return the argv to execute, or raise UnsafeCommandError."""
    if not isinstance(cmd, str) or not cmd.strip():
        raise UnsafeCommandError("empty command")
    if len(cmd) > 2000 or "\n" in cmd or "\x00" in cmd:
        raise UnsafeCommandError("command must be a single line under 2000 characters")
    if DANGEROUS_RE.search(cmd):
        raise UnsafeCommandError("command matches a destructive or credential-access pattern")
    if "`" in cmd or "$(" in cmd or "${" in cmd:
        raise UnsafeCommandError("command substitution / variable expansion is not supported (no shell is used)")
    try:
        argv = shlex.split(cmd, posix=True)
    except ValueError as exc:
        raise UnsafeCommandError(f"unparseable command: {exc}") from exc
    if not argv:
        raise UnsafeCommandError("empty command")
    for token in argv:
        if token in SHELL_TOKENS:
            raise UnsafeCommandError(f"shell operator '{token}' is not supported; run one program per command")

    exe = argv[0]
    allowed = DEFAULT_ALLOWED | set(extra_allowed)
    if "/" in exe:
        resolved = (ws / exe).resolve()
        if not _inside(resolved, ws):
            raise UnsafeCommandError(f"executable outside the workspace: {exe}")
        if not exe.startswith("./"):
            raise UnsafeCommandError("use a './relative/path' for project-local executables")
    else:
        name = _base(exe)
        if name not in allowed:
            raise UnsafeCommandError(f"program '{name}' is not on the allow-list")

    name = _base(exe)
    args = argv[1:]

    if name == "git":
        sub = next((a for a in args if not a.startswith("-")), None)
        if sub not in GIT_READONLY:
            raise UnsafeCommandError("only read-only git subcommands are allowed (the agent makes commits itself)")
    if name.startswith("python"):
        for a in args:
            if a == "-m":
                break
            if a == "-c" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:]):
                raise UnsafeCommandError("python -c (inline code) is not allowed; write a file or use -m")
            if not a.startswith("-"):
                break
    if name in INLINE_EVAL and any(a in INLINE_EVAL[name] for a in args):
        raise UnsafeCommandError(f"inline code flag for {name} is not allowed")
    if name in {"npm", "pnpm", "yarn"}:
        sub = next((a for a in args if not a.startswith("-")), None)
        if sub in NPM_FORBIDDEN or (sub == "config" and "set" in args):
            raise UnsafeCommandError(f"{name} {sub} is not allowed")
    if name in {"pip", "pip3"} and any(a in {"config"} for a in args[:1]):
        raise UnsafeCommandError("pip config is not allowed")

    for arg in args:
        _check_path_arg(arg, ws)
    return argv


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for child processes: no secrets, no CI tokens, non-interactive defaults."""
    src = dict(os.environ if base is None else base)
    env = {}
    for key, value in src.items():
        if SECRET_ENV_RE.search(key) or key.startswith(("GITHUB_", "ACTIONS_", "RUNNER_", "INPUT_")):
            continue
        env[key] = value
    env.update({
        "CI": "1",
        "NO_COLOR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        "COMPOSER_NO_INTERACTION": "1",
        "PYTHONUNBUFFERED": "1",
    })
    return env
