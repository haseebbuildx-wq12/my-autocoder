"""Strict validation of everything the model returns. Nothing is applied unless it validates."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath


class OutputError(Exception):
    """The model's reply was malformed or unsafe. Rejected without side effects."""


def extract_json(text: str):
    if not isinstance(text, str) or not text.strip():
        raise OutputError("empty response")
    s = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", s, re.S)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except json.JSONDecodeError as exc:
            raise OutputError(f"response is not valid JSON: {exc}") from None
    raise OutputError("response contains no JSON object")


def _str(obj: dict, key: str, default: str = "", required: bool = False, max_len: int = 20000) -> str:
    value = obj.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise OutputError(f"'{key}' must be a string")
    if required and not value.strip():
        raise OutputError(f"'{key}' is required")
    return value[:max_len]


def _str_list(obj: dict, key: str, max_items: int = 200) -> list[str]:
    value = obj.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise OutputError(f"'{key}' must be a list of strings")
    return value[:max_items]


def check_rel_path(path: str) -> str:
    """Syntactic validation only; the Toolbox performs the real containment check."""
    if not isinstance(path, str) or not path.strip():
        raise OutputError("empty path")
    norm = path.replace("\\", "/").strip()
    p = PurePosixPath(norm)
    if p.is_absolute() or ".." in p.parts or norm.startswith("~") or re.match(r"^[A-Za-z]:", norm) or len(norm) > 300:
        raise OutputError(f"unsafe path: {path!r}")
    if ".git" in p.parts or "\x00" in norm:
        raise OutputError(f"unsafe path: {path!r}")
    return p.as_posix()


# -- implementation response -------------------------------------------------------
@dataclass
class Implementation:
    summary: str
    edits: dict[str, str] = field(default_factory=dict)        # path -> full content
    patches: list[tuple[str, str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    done: bool = False
    notes: str = ""


def parse_implementation(text: str, max_files: int = 40, max_write_bytes: int = 600000) -> Implementation:
    obj = extract_json(text)
    if not isinstance(obj, dict):
        raise OutputError("top-level JSON must be an object")
    edits_raw = obj.get("edits", [])
    if not isinstance(edits_raw, list):
        raise OutputError("'edits' must be a list")
    edits: dict[str, str] = {}
    for item in edits_raw:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
            raise OutputError("each edit needs string 'path' and string 'content'")
        path = check_rel_path(item["path"])
        if len(item["content"].encode("utf-8")) > max_write_bytes:
            raise OutputError(f"{path}: content too large; split the file or use patches")
        edits[path] = item["content"]
    patches: list[tuple[str, str, str]] = []
    patches_raw = obj.get("patches", [])
    if patches_raw is None:
        patches_raw = []
    if not isinstance(patches_raw, list):
        raise OutputError("'patches' must be a list")
    for item in patches_raw:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("path", "old", "new")):
            raise OutputError("each patch needs string 'path', 'old' and 'new'")
        if not item["old"]:
            raise OutputError("patch 'old' must not be empty")
        patches.append((check_rel_path(item["path"]), item["old"], item["new"]))
    deletes = [check_rel_path(p) for p in _str_list(obj, "delete")]
    if len(edits) + len(patches) + len(deletes) > max_files:
        raise OutputError(f"too many file operations in one response (max {max_files})")
    commands = [c.strip() for c in _str_list(obj, "commands", 20) if c.strip()]
    done = obj.get("done", False)
    if not isinstance(done, bool):
        raise OutputError("'done' must be a boolean")
    return Implementation(
        summary=_str(obj, "summary", required=True, max_len=1000),
        edits=edits, patches=patches, deletes=deletes, commands=commands,
        done=done, notes=_str(obj, "notes", max_len=4000),
    )


# -- planning ----------------------------------------------------------------------
def validate_requirements(obj) -> list[dict]:
    if not isinstance(obj, dict) or not isinstance(obj.get("requirements"), list):
        raise OutputError("expected {'requirements': [...]}")
    out = []
    for item in obj["requirements"]:
        if isinstance(item, str):
            item = {"description": item}
        if not isinstance(item, dict):
            raise OutputError("each requirement must be an object")
        desc = _str(item, "description", required=True, max_len=1500).strip()
        out.append({"description": desc, "source": _str(item, "source", max_len=400)})
    return out


def validate_tasks(obj) -> list[dict]:
    if not isinstance(obj, dict) or not isinstance(obj.get("tasks"), list) or not obj["tasks"]:
        raise OutputError("expected {'tasks': [...]} with at least one task")
    out = []
    for item in obj["tasks"]:
        if not isinstance(item, dict):
            raise OutputError("each task must be an object")
        out.append({
            "id": _str(item, "id", max_len=40),
            "title": _str(item, "title", required=True, max_len=200),
            "description": _str(item, "description", required=True, max_len=4000),
            "depends_on": _str_list(item, "depends_on", 30),
            "requirements": _str_list(item, "requirements", 60),
            "verification": _str(item, "verification", max_len=1500),
            "verification_commands": _str_list(item, "verification_commands", 10),
            "files": [check_rel_path(p) for p in _str_list(item, "files", 20) if p.strip()],
        })
    return out


# -- verification ------------------------------------------------------------------
def validate_verification(obj) -> dict:
    if not isinstance(obj, dict) or not isinstance(obj.get("requirements"), list):
        raise OutputError("expected {'requirements': [...], 'critical_bugs': [...]}")
    reqs = []
    for item in obj["requirements"]:
        if not isinstance(item, dict):
            raise OutputError("each requirement result must be an object")
        status = _str(item, "status", required=True).lower()
        if status not in {"verified", "partial", "missing"}:
            raise OutputError("status must be verified, partial or missing")
        files = []
        for p in _str_list(item, "files", 20):
            try:
                files.append(check_rel_path(p))
            except OutputError:
                continue
        reqs.append({"id": _str(item, "id", required=True, max_len=40), "status": status,
                     "evidence": _str(item, "evidence", max_len=2000), "files": files})
    bugs = []
    for item in obj.get("critical_bugs", []) or []:
        if isinstance(item, str):
            bugs.append({"description": item[:1000], "file": ""})
        elif isinstance(item, dict):
            bugs.append({"description": _str(item, "description", max_len=1000), "file": _str(item, "file", max_len=300)})
    return {"requirements": reqs, "critical_bugs": bugs}


# -- test-weakening guard ----------------------------------------------------------
_ASSERT_RES = [
    re.compile(r"\bassert\w*\b"), re.compile(r"\bexpect\s*\("), re.compile(r"\bdef\s+test_"),
    re.compile(r"\b(?:it|test)\s*\("), re.compile(r"\bfunction\s+test\w*"),
    re.compile(r"@Test\b"), re.compile(r"\$this->assert\w+"),
]
_SKIP_RE = re.compile(r"@pytest\.mark\.skip|unittest\.skip|\.skip\s*\(|xit\s*\(|xdescribe\s*\(|markTestSkipped|@Disabled|@Ignore")


def count_assertions(text: str) -> int:
    return sum(len(rx.findall(text)) for rx in _ASSERT_RES)


def weakens_tests(old: str, new: str) -> str | None:
    """Return a reason if `new` looks like a weaker version of the test file `old`."""
    if count_assertions(new) < count_assertions(old):
        return "it reduces the number of assertions/tests"
    if len(_SKIP_RE.findall(new)) > len(_SKIP_RE.findall(old)):
        return "it adds skipped/disabled tests"
    return None
