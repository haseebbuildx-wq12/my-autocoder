"""Persistent agent memory under agent_state/. Every write is atomic."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .util import atomic_write_text, now_iso, read_json, write_json


def default_progress() -> dict[str, Any]:
    return {
        "status": "new",               # new|planning|working|paused_quota|paused_budget|blocked_config|
                                       # stuck|final_verification|failed_verification|complete
        "current_task_id": None,
        "completed_task_ids": [],
        "last_provider": None,
        "last_action": None,
        "last_error": None,
        "next_action": "plan",
        "updated_at": None,
        "spec_sha256": None,
        "run_count": 0,
        "planning": {},                # resumable planning cursor
        "providers": {},               # per-provider cooldown / block state
        "task_runtime": {},            # where we are inside the current task
        "in_flight_files": [],         # files modified by the unfinished task (workspace-relative)
        "avoid_provider": [],          # prefer a different provider for the next call
        "verification_round": 0,
        "final_checks": [],
    }


def new_task(**kw: Any) -> dict[str, Any]:
    task = {
        "id": "",
        "title": "",
        "description": "",
        "depends_on": [],
        "requirements": [],
        "verification": "",
        "verification_commands": [],
        "files": [],                   # planner hints
        "status": "pending",           # pending|in_progress|completed|blocked
        "cycles": 0,
        "attempts": 0,
        "files_changed": [],
        "evidence": None,
        "completed_at": None,
        "corrective_for": None,
        "fixes_bugs": [],
        "notes": [],
    }
    task.update(kw)
    return task


class StateStore:
    def __init__(self, root: Path, state_dir: str):
        self.dir = Path(root) / state_dir
        self.tasks: list[dict] = []
        self.requirements: list[dict] = []
        self.bugs: list[dict] = []
        self.progress: dict = default_progress()

    # -- persistence -------------------------------------------------------------
    def load(self) -> None:
        self.tasks = read_json(self.dir / "TASKS.json", {"tasks": []}).get("tasks", [])
        self.requirements = read_json(self.dir / "REQUIREMENTS.json", {"requirements": []}).get("requirements", [])
        self.bugs = read_json(self.dir / "BUGS.json", {"bugs": []}).get("bugs", [])
        loaded = read_json(self.dir / "PROGRESS.json", {})
        self.progress = default_progress()
        self.progress.update(loaded)

    def save(self) -> None:
        self.progress["completed_task_ids"] = [t["id"] for t in self.tasks if t["status"] == "completed"]
        self.progress["updated_at"] = now_iso()
        write_json(self.dir / "TASKS.json", {"tasks": self.tasks})
        write_json(self.dir / "REQUIREMENTS.json", {"requirements": self.requirements})
        write_json(self.dir / "BUGS.json", {"bugs": self.bugs})
        write_json(self.dir / "PROGRESS.json", self.progress)
        decisions = self.dir / "DECISIONS.md"
        if not decisions.exists():
            atomic_write_text(decisions, "# Decisions log\n\nAppend-only record of planning and recovery decisions.\n")

    def log_decision(self, text: str) -> None:
        path = self.dir / "DECISIONS.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else "# Decisions log\n\n"
        atomic_write_text(path, existing.rstrip("\n") + f"\n- {now_iso()} — {text}\n")

    def decisions_tail(self, max_chars: int = 2000) -> str:
        path = self.dir / "DECISIONS.md"
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        return text[-max_chars:]

    # -- tasks -------------------------------------------------------------------
    def task(self, task_id: str) -> dict | None:
        return next((t for t in self.tasks if t["id"] == task_id), None)

    def next_ready_task(self) -> dict | None:
        for t in self.tasks:
            if t["status"] == "in_progress":
                return t  # resume interrupted work first
        done = {t["id"] for t in self.tasks if t["status"] == "completed"}
        ready = [
            (t.get("cycles", 0), i, t)
            for i, t in enumerate(self.tasks)
            if t["status"] == "pending" and all(d in done for d in t["depends_on"])
        ]
        if not ready:
            return None
        return min(ready, key=lambda x: (x[0], x[1]))[2]

    def unfinished_tasks(self) -> list[dict]:
        return [t for t in self.tasks if t["status"] != "completed"]

    def next_task_number(self) -> int:
        nums = [int(t["id"][1:]) for t in self.tasks if t["id"][1:].isdigit()]
        return (max(nums) if nums else 0) + 1

    def add_task(self, **kw: Any) -> dict:
        task = new_task(id=f"T{self.next_task_number():03d}", **kw)
        self.tasks.append(task)
        for rid in task["requirements"]:
            req = self.requirement(rid)
            if req is not None and task["id"] not in req["tasks"]:
                req["tasks"].append(task["id"])
        return task

    # -- requirements ------------------------------------------------------------
    def requirement(self, req_id: str) -> dict | None:
        return next((r for r in self.requirements if r["id"] == req_id), None)

    def add_requirement(self, description: str, source: str = "") -> dict:
        req = {
            "requirement_id": None,
            "id": f"REQ-{len(self.requirements) + 1:03d}",
            "description": description,
            "source": source,
            "tasks": [],
            "status": "unverified",    # unverified|verified|partial|missing
            "evidence": "",
            "files": [],
            "verified_at": None,
        }
        req["requirement_id"] = req["id"]
        self.requirements.append(req)
        return req

    # -- bugs --------------------------------------------------------------------
    def add_bug(self, task_id: str | None, error: str, attempt: int, *, status: str = "open",
                severity: str = "normal", source: str = "verification", signature: str | None = None) -> dict:
        bug = {
            "id": f"B{len(self.bugs) + 1:04d}",
            "task_id": task_id,
            "error": error[:4000],
            "attempt": attempt,
            "status": status,           # open|fixed|abandoned
            "severity": severity,       # normal|critical
            "source": source,
            "signature": signature,
            "created_at": now_iso(),
            "resolved_at": None,
        }
        self.bugs.append(bug)
        return bug

    def resolve_bugs(self, *, task_id: str | None = None, ids: list[str] | None = None, status: str = "fixed") -> None:
        for bug in self.bugs:
            if bug["status"] != "open":
                continue
            if (task_id is not None and bug["task_id"] == task_id) or (ids and bug["id"] in ids):
                bug["status"] = status
                bug["resolved_at"] = now_iso()

    def snapshot(self) -> dict:
        return copy.deepcopy({"tasks": self.tasks, "requirements": self.requirements,
                              "bugs": self.bugs, "progress": self.progress})
