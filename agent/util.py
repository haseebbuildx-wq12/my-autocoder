"""Shared helpers: time, hashing, atomic file IO, secret redaction and scanning."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Environment variable names that are treated as secrets everywhere.
SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)

# Well-known credential shapes. Used both to redact logs/prompts and to scan the final ZIP.
SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),                       # Google API key
    re.compile(r"sk-or-v1-[0-9a-fA-F]{32,}"),                    # OpenRouter
    re.compile(r"sk-[A-Za-z0-9]{32,}"),                          # generic sk- keys
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),                   # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),                 # GitHub fine-grained PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),                             # AWS access key id
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
]

REDACTED = "***REDACTED***"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    """Write a file atomically (temp file + rename) so a killed process never leaves a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


def write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def env_secret_values(min_len: int = 8) -> list[str]:
    values = {v for k, v in os.environ.items() if SECRET_ENV_RE.search(k) and len(v) >= min_len}
    return sorted(values, key=len, reverse=True)


class Redactor:
    """Removes secret values (from the environment) and well-known key shapes from text."""

    def redact(self, text: str | None) -> str:
        if not text:
            return text or ""
        for value in env_secret_values():
            text = text.replace(value, REDACTED)
        for pattern in SECRET_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text


def scan_for_secrets(text: str) -> list[str]:
    """Return human-readable descriptions of secrets found in `text` (never the secret itself)."""
    findings: list[str] = []
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(f"matches credential pattern {pattern.pattern[:24]}...")
    for value in env_secret_values():
        if value in text:
            findings.append("contains the value of a secret environment variable")
            break
    return findings


def truncate(text: str, limit: int, keep_tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    if keep_tail:
        return "[... truncated ...]\n" + text[-limit:]
    return text[:limit] + "\n[... truncated ...]"
