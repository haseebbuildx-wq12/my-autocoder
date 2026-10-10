"""Final verification. The agent may only declare completion through this module.

Completion = all final checks pass (tests/lint/typecheck/build where they exist)
           + every requirement independently audited as verified WITH evidence
           + no open critical bugs.
Anything else becomes a corrective task and the loop continues.
"""
from __future__ import annotations

from . import prompts
from .detect import is_test_path
from .schema import validate_verification
from .util import now_iso, parse_iso


class FinalVerifier:
    def __init__(self, agent):
        self.a = agent

    # -- public ------------------------------------------------------------------
    def run(self) -> str:
        """Returns 'complete', 'corrective' (new tasks were added) or 'failed' (rounds exhausted)."""
        st, cfg = self.a.state, self.a.cfg
        p = st.progress
        if p["verification_round"] > cfg["max_verification_rounds"]:
            return "failed"

        # 1) deterministic evidence first: cheap, and failures need fixing before any audit
        p["last_action"] = "final verification: running tests/lint/typecheck/build"
        checks = self.a.checks.run_final_checks()
        p["final_checks"] = checks
        st.save()
        failed = [c for c in checks if c["status"] == "fail"]
        if failed:
            for check in failed:
                self._corrective_for_check(check)
            return self._next_round("final checks failed: " + ", ".join(c["name"] for c in failed))

        # 2) requirement audit (resumable: results persist after every batch)
        todo = [r for r in st.requirements if self._needs_audit(r)]
        size = cfg["verify_batch"]
        for i in range(0, len(todo), size):
            batch = todo[i:i + size]
            p["last_action"] = f"final verification: auditing {batch[0]['id']}..{batch[-1]['id']}"
            self._audit(batch, checks)
            st.save()

        # 3) decide
        issues: list[str] = []
        for bug in [b for b in st.bugs if b["severity"] == "critical" and b["status"] == "open"]:
            if self._corrective(f"bug:{bug['id']}", f"Fix critical bug {bug['id']}", bug["error"], [],
                                fixes_bugs=[bug["id"]]):
                pass
            issues.append(bug["id"])
        for req in st.requirements:
            if req["status"] != "verified":
                self._corrective(
                    f"req:{req['id']}", f"Complete {req['id']} ({req['status']})",
                    f"Requirement {req['id']}: {req['description']}\n\nVerifier finding: {req['evidence'] or 'no evidence found'}\n"
                    "Implement what is missing and add tests or commands that demonstrate it.",
                    [req["id"]])
                issues.append(req["id"])
        if issues:
            return self._next_round("unresolved: " + ", ".join(issues[:12]))
        return "complete"

    # -- internals ---------------------------------------------------------------
    def _needs_audit(self, req: dict) -> bool:
        if req["status"] != "verified" or not req.get("verified_at"):
            return True
        verified_at = parse_iso(req["verified_at"])
        for tid in req.get("tasks", []):
            t = self.a.state.task(tid)
            if t and t.get("completed_at") and parse_iso(t["completed_at"]) > verified_at:
                return True       # code changed after the last audit
        return False

    def _next_round(self, reason: str) -> str:
        st = self.a.state
        st.progress["verification_round"] += 1
        st.log_decision(f"Final verification round {st.progress['verification_round']}: {reason}")
        st.save()
        if st.progress["verification_round"] > self.a.cfg["max_verification_rounds"]:
            return "failed"
        return "corrective"

    def _corrective(self, key: str, title: str, description: str, reqs: list[str],
                    fixes_bugs: list[str] | None = None) -> dict | None:
        st = self.a.state
        for t in st.tasks:
            if t.get("corrective_for") == key and t["status"] in ("pending", "in_progress", "blocked"):
                return None      # already queued
        task = st.add_task(title=title, description=description, requirements=list(reqs),
                           verification="Re-run the failing check / demonstrate the requirement with a test.",
                           corrective_for=key, fixes_bugs=fixes_bugs or [], depends_on=[])
        st.log_decision(f"Corrective task {task['id']} created for {key}")
        return task

    def _corrective_for_check(self, check: dict) -> None:
        cmds = [c["cmd"] for c in check.get("commands", []) if not c.get("ok")]
        desc = (f"The final '{check['name']}' check failed.\nCommand(s): {', '.join(cmds)}\n\n"
                f"Output excerpt:\n{check.get('excerpt', '')}\n\nFix the implementation (never weaken tests) "
                "so that the check passes.")
        task = self._corrective(f"check:{check['name']}:{check.get('signature', '')}",
                                f"Fix failing {check['name']} check", desc, [])
        if task is not None:
            task["files"] = list(check.get("files", []))
            task["verification_commands"] = cmds

    def _audit(self, batch: list[dict], checks: list[dict]) -> None:
        st, ctx, cfg = self.a.state, self.a.ctx, self.a.cfg
        view = [{"id": r["id"], "description": r["description"], "source": r.get("source", "")} for r in batch]
        budget = max(4000, cfg["max_prompt_chars"] // 2)
        files_block, task_info = ctx.verification_files(batch, budget)
        prompt = prompts.verify_prompt(view, task_info, files_block, checks, ctx.structure(200))
        result = self.a.ask_json("verify", prompt, prompts.SYSTEM_VERIFY, validate_verification)

        by_id = {r["id"]: r for r in batch}
        seen: set[str] = set()
        for item in result["requirements"]:
            req = by_id.get(item["id"])
            if req is None:
                continue
            seen.add(req["id"])
            status, evidence = item["status"], item["evidence"]
            existing = [f for f in item["files"] if self.a.tb.exists(f)]
            linked = [t for t in (st.task(tid) for tid in req.get("tasks", [])) if t]
            notes = []
            if not linked:
                status = "missing"
                notes.append("no implementing task is linked to this requirement")
            elif any(t["status"] != "completed" for t in linked):
                status = "partial" if status == "verified" else status
                notes.append("not all implementing tasks are completed")
            if status == "verified":
                if len(evidence.strip()) < 20 or not existing:
                    status = "partial"
                    notes.append("verifier gave no concrete evidence (needs real file paths and an explanation)")
                else:
                    executable = any(not (t.get("evidence") or {}).get("weak", True) for t in linked) \
                        or any(is_test_path(f) for f in existing)
                    if not executable:
                        status = "partial"
                        notes.append("no executable test evidence (only syntax checks ran)")
            req["status"] = status
            req["evidence"] = (evidence + (" | " + "; ".join(notes) if notes else "")).strip()
            req["files"] = existing
            req["verified_at"] = now_iso() if status == "verified" else None
        for rid, req in by_id.items():
            if rid not in seen:
                req["status"] = "partial"
                req["evidence"] = "the verifier did not report on this requirement"
                req["verified_at"] = None
        for bug in result["critical_bugs"]:
            if bug["description"].strip():
                st.add_bug(None, f"{bug['description']} ({bug['file']})", attempt=0, severity="critical",
                           source="final_verifier")
