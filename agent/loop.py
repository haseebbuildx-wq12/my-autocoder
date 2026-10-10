"""The autonomous development loop.

    load spec -> load state -> plan if needed -> pick next ready task -> ask AI -> apply
    -> verify -> (pass: complete+commit | fail: analyse, repair, repeat) -> next task ...
    -> final verification -> package

Every state transition is persisted before the next expensive step, so a quota stop, a killed
runner or a crash resumes exactly where it left off.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import traceback
from pathlib import Path

from . import prompts
from .checks import CheckRunner
from .context import ContextBuilder
from .detect import is_test_path
from .git_ops import Git
from .packager import Packager, SecretsFound
from .planner import PlanError, Planner
from .providers import ConfigBlocked, NoProviderAvailable, ProviderManager, build_providers
from .providers.base import ErrorKind, GenerationResult, ProviderError
from .schema import OutputError, extract_json, parse_implementation, weakens_tests
from .state import StateStore
from .tools import ProtectedPathError, Toolbox, UnsafePathError, is_secret_file
from .util import Redactor, now_iso, sha256_text
from .verifier import FinalVerifier

EXIT_COMPLETE = 0
EXIT_ERROR = 1
EXIT_CONFIG = 3            # provider auth/permission/config problem: human action needed
EXIT_DIRTY = 4             # uncommitted changes the agent did not make
EXIT_STUCK = 5             # tasks blocked after repeated failed cycles
EXIT_VERIFY_FAILED = 6     # final verification could not converge
EXIT_PAUSED_BUDGET = 10    # per-run turn/time budget used up: more work remains, safe to continue now
EXIT_PAUSED_QUOTA = 11     # no provider available: wait (scheduled resume) until quota returns


class Paused(Exception):
    def __init__(self, status: str, reason: str, code: int):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.code = code


class EditRejected(Exception):
    pass


class Agent:
    def __init__(self, root: Path, cfg: dict, providers: list | None = None, log: logging.Logger | None = None):
        self.root = Path(root).resolve()
        self.cfg = cfg
        self.log = log or logging.getLogger("agent")
        self.redactor = Redactor()
        self.git = Git(self.root, cfg, self.log)
        self.state = StateStore(self.root, cfg["state_dir"])
        self.tb = Toolbox(self.root, cfg, self.git, self.redactor, self.log)
        self.checks = CheckRunner(self.tb, cfg, self.state, self.log)
        self.ctx = ContextBuilder(self.tb, cfg, self.state, self.checks, self.redactor)
        self.manager = ProviderManager(
            providers if providers is not None else build_providers(cfg),
            self.state, cfg, self.log, on_unavailable=self._on_provider_unavailable)
        self.planner = Planner(self)
        self.verifier = FinalVerifier(self)
        self.packager = Packager(self.tb, cfg, self.state)
        self.turns = 0
        self.started = time.monotonic()

    # ======================================================================== entry
    def run(self, replan: bool = False, allow_dirty: bool = False, clear_blocks: bool = False) -> int:
        self.git.ensure_repo()
        self.state.load()
        p = self.state.progress
        try:
            spec = self._load_spec()
        except (FileNotFoundError, ValueError) as exc:
            self.log.error("%s", exc)
            return EXIT_ERROR
        if p["status"] == "complete":
            self._ensure_package()
            self.log.info("project already complete; nothing to do")
            return EXIT_COMPLETE
        unexpected = self._preflight(allow_dirty)
        if unexpected:
            self.log.error("the working tree has uncommitted changes the agent did not make:\n  %s\n"
                           "Commit or stash them, or re-run with --allow-dirty.", "\n  ".join(unexpected[:20]))
            return EXIT_DIRTY
        p["run_count"] += 1
        if "last_good_commit" not in p:
            p["last_good_commit"] = self.git.head_full()    # rollback target for abandoned attempts
        if clear_blocks:
            self.manager.clear_blocks()
        self.ctx.set_spec(spec)
        try:
            self.manager.assert_not_blocked()
            self._ensure_plan(spec, replan)
            code = self._main_loop()
            return code
        except Paused as exc:
            return self._pause(exc)
        except ConfigBlocked as exc:
            return self._blocked(exc)
        except (OutputError, PlanError) as exc:
            return self._failed(f"unusable model output / planning problem: {exc}")
        except Exception as exc:  # noqa: BLE001 - any crash must leave a recoverable checkpoint
            self.log.error("unexpected error: %s\n%s", exc, self.redactor.redact(traceback.format_exc()))
            return self._failed(f"unexpected error: {exc}")

    # ===================================================================== plumbing
    def _load_spec(self) -> str:
        path = self.root / self.cfg["spec_path"]
        if not path.is_file():
            raise FileNotFoundError(f"{self.cfg['spec_path']} not found: create it with your full project specification")
        text = path.read_text(encoding="utf-8")
        if "MASTER_SPEC_TEMPLATE" in text:
            raise ValueError(f"{self.cfg['spec_path']} still contains the template marker; replace it with your real specification")
        if not text.strip():
            raise ValueError(f"{self.cfg['spec_path']} is empty")
        return text

    def _preflight(self, allow_dirty: bool) -> list[str]:
        p = self.state.progress
        entries = self.git.status([self.cfg["workspace_dir"], self.cfg["state_dir"]])
        in_flight = {Path(os.path.relpath(self.tb.ws / f, self.root)).as_posix() for f in p.get("in_flight_files", [])}
        state_prefix = self.cfg["state_dir"].rstrip("/") + "/"
        unexpected = [path for _, path in entries if not path.startswith(state_prefix) and path not in in_flight]
        if unexpected and allow_dirty:
            self.state.log_decision(f"Proceeding with {len(unexpected)} pre-existing uncommitted file(s) (--allow-dirty)")
            return []
        return unexpected

    def _checkpoint(self, message: str) -> None:
        try:
            self.state.save()
            self.tb.git_commit(message)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("checkpoint failed: %s", exc)

    def _on_provider_unavailable(self, name: str, kind: ErrorKind) -> None:
        self._checkpoint(f"agent: checkpoint ({name} {kind.value}; trying next provider)")

    def _ensure_plan(self, spec: str, replan: bool) -> None:
        st, p = self.state, self.state.progress
        digest = sha256_text(spec)
        if replan:
            st.tasks, st.requirements, st.bugs = [], [], []
            p.update(planning={}, task_runtime={}, in_flight_files=[], verification_round=0, final_checks=[])
            st.log_decision("Re-planning requested: previous plan discarded (code on disk is kept).")
        elif p.get("spec_sha256") and p["spec_sha256"] != digest:
            st.log_decision("MASTER_SPEC.md changed since planning. Keeping the existing plan; run with --replan to re-plan.")
            self.log.warning("MASTER_SPEC.md changed since planning; use --replan to re-plan")
            digest = p["spec_sha256"]
        p["spec_sha256"] = digest
        if p.get("planning", {}).get("stage") != "done":
            self.planner.plan(spec)
        p["status"] = "working"
        st.save()

    # ===================================================================== AI access
    def _llm(self, label: str, prompt: str, system: str, avoid: list[str] | tuple[str, ...] = ()) -> GenerationResult:
        cfg = self.cfg
        if self.turns >= cfg["max_ai_turns"]:
            raise Paused("paused_budget", f"AI turn budget for this run reached ({cfg['max_ai_turns']})", EXIT_PAUSED_BUDGET)
        if time.monotonic() - self.started > cfg["max_runtime_minutes"] * 60:
            raise Paused("paused_budget", "run time budget reached", EXIT_PAUSED_BUDGET)
        try:
            result = self.manager.generate(prompt, system=system, avoid=list(avoid))
        except NoProviderAvailable as exc:
            raise Paused("paused_quota", str(exc), EXIT_PAUSED_QUOTA) from None
        except ProviderError as exc:
            raise OutputError(f"provider rejected the request ({exc.kind.value}): {exc}") from None
        self.turns += 1
        self.state.progress["last_provider"] = result.provider
        self.log.info("AI turn %d/%d [%s] via %s", self.turns, cfg["max_ai_turns"], label, result.provider)
        self.state.save()
        return result

    def ask_json(self, label: str, prompt: str, system: str, validator, avoid=()):
        """Ask for JSON and validate; one corrective re-ask on malformed output."""
        result = self._llm(label, prompt, system, avoid)
        for attempt in (1, 2):
            try:
                if result.truncated:
                    raise OutputError("the response was cut off (too long); return less content")
                return validator(extract_json(result.text))
            except OutputError as exc:
                if attempt == 2:
                    raise
                self.log.warning("malformed %s output: %s; asking once more", label, exc)
                result = self._llm(label + ":retry",
                                   prompt + f"\n\nYour previous reply was rejected: {exc}. Reply again with ONLY the valid JSON object.",
                                   system, avoid)

    # ================================================================ main loop / tasks
    def _main_loop(self) -> int:
        st = self.state
        while True:
            task = st.next_ready_task()
            if task is not None:
                self._run_task(task)
                continue
            if st.unfinished_tasks():
                return self._stuck()
            code = self._final_phase()
            if code is not None:
                return code

    def _fresh_runtime(self, task: dict) -> dict:
        return {"task_id": task["id"], "phase": "implement", "repairs": 0, "continuations": 0,
                "last_signature": None, "same_count": 0, "failure": None, "last_done": None,
                "commands": [], "files": [], "providers_used": [], "hint": None, "last_results": []}

    def _run_task(self, task: dict) -> None:
        st, p, cfg = self.state, self.state.progress, self.cfg
        rt = p.get("task_runtime") or {}
        if rt.get("task_id") != task["id"]:
            rt = self._fresh_runtime(task)
            p["task_runtime"] = rt
        task["status"] = "in_progress"
        p["current_task_id"] = task["id"]
        p["status"] = "working"
        p["last_action"] = f"{rt['phase']} {task['id']}: {task['title']}"
        st.save()
        self.log.info("== %s: %s (phase %s)", task["id"], task["title"], rt["phase"])

        while True:
            phase = rt["phase"]
            if phase == "verify":
                self._verify_phase(task, rt)
                if rt["phase"] == "done":
                    self._complete_task(task, rt)
                    return
                continue
            if phase == "repair" and rt["repairs"] >= cfg["max_repair_attempts"]:
                self._abandon_task(task, rt)
                return

            prompt = self.ctx.task_prompt(task, rt, phase)
            avoid = list(p.get("avoid_provider") or [])
            try:
                result = self._llm(f"{task['id']}:{phase}", prompt, prompts.SYSTEM_IMPLEMENT, avoid)
            except OutputError as exc:                     # provider rejected our request
                self._record_failure(task, rt, {"kind": "provider_rejected", "excerpt": str(exc), "files": []})
                rt["phase"] = "repair"
                rt["repairs"] += 1
                st.save()
                continue
            p["avoid_provider"] = []
            task["attempts"] += 1
            if phase == "repair":
                rt["repairs"] += 1
            if result.provider not in rt["providers_used"]:
                rt["providers_used"].append(result.provider)

            try:
                if result.truncated:
                    raise OutputError("your reply was cut off: reduce its size (use patches, split large files)")
                impl = parse_implementation(result.text, cfg["max_files_per_response"], cfg["max_write_bytes"])
                self._apply(task, rt, impl)
            except (OutputError, EditRejected) as exc:
                self.log.warning("%s: rejected model output: %s", task["id"], exc)
                kind = "edit_rejected" if isinstance(exc, EditRejected) else "malformed_output"
                self._record_failure(task, rt, {"kind": kind, "excerpt": str(exc), "files": []})
                rt["phase"] = "repair"
                st.save()
                continue

            rt["commands"] = impl.commands
            rt["last_done"] = impl.done
            rt["last_summary"] = impl.summary
            rt["phase"] = "verify"
            p["last_action"] = f"verifying {task['id']}: {impl.summary[:120]}"
            st.save()

    # -- applying model edits ------------------------------------------------------
    def _apply(self, task: dict, rt: dict, impl) -> None:
        tb = self.tb
        limit = self.cfg["max_write_bytes"]
        touched = [*impl.edits, *(p for p, _, _ in impl.patches), *impl.deletes]
        if set(impl.edits) & set(impl.deletes):
            raise EditRejected("a path appears in both 'edits' and 'delete'")
        for path in touched:
            try:
                tb.resolve(path)
            except UnsafePathError as exc:
                raise EditRejected(str(exc)) from None
            if tb.is_protected(path):
                raise EditRejected(f"{path} is protected and must not be modified")
            if is_secret_file(path):
                raise EditRejected(f"{path} looks like a credentials file; use environment variables instead")

        contents: dict[str, str] = dict(impl.edits)
        for path, old, new in impl.patches:
            if path in contents:
                text = contents[path]
            elif tb.exists(path):
                text = tb.read_file(path, limit)
            else:
                raise EditRejected(f"patch target {path} does not exist")
            if text.count(old) != 1:
                raise EditRejected(f"patch for {path}: 'old' text must match exactly once (found {text.count(old)}); send the full file instead")
            contents[path] = text.replace(old, new, 1)

        for path in impl.deletes:
            if is_test_path(path) and tb.exists(path) and self.git.tracked_at(tb.resolve(path), self.state.progress.get("last_good_commit")):
                raise EditRejected(f"deleting the existing test file {path} is not allowed; fix the implementation instead")
        for path, new in contents.items():
            if is_test_path(path) and tb.exists(path) and self.git.tracked_at(tb.resolve(path), self.state.progress.get("last_good_commit")):
                reason = weakens_tests(tb.read_file(path, limit), new)
                if reason:
                    raise EditRejected(f"{path}: rejected because {reason}. Fix the implementation, do not weaken tests")

        changed: list[str] = []
        for path, new in contents.items():
            tb.write_file(path, new)
            changed.append(path)
        for path in impl.deletes:
            tb.delete_file(path)
            if path in rt["files"]:
                rt["files"].remove(path)
        for path in changed:
            if path not in rt["files"]:
                rt["files"].append(path)
            if path not in task["files_changed"]:
                task["files_changed"].append(path)
        self.state.progress["in_flight_files"] = list(rt["files"])
        self.log.info("%s: applied %d write(s), %d delete(s)", task["id"], len(changed), len(impl.deletes))

    # -- verification of one task -------------------------------------------------
    def _verify_phase(self, task: dict, rt: dict) -> None:
        st, cfg = self.state, self.cfg
        ok, results, failure = self.checks.run_task_checks(task, rt["files"], rt["commands"])
        rt["last_results"] = [r.summary() for r in results]
        if ok:
            if rt.get("last_done") is False:
                if rt["continuations"] < cfg["max_continuations"]:
                    rt["continuations"] += 1
                    rt["phase"] = "continue"
                    st.save()
                    return
                failure = {"kind": "incomplete", "files": rt["files"][:10], "signature": "incomplete",
                           "excerpt": "The model keeps reporting the task as not done after several continuations."}
            else:
                rt["phase"] = "done"
                return
        self._record_failure(task, rt, failure)
        rt["phase"] = "repair"
        st.save()

    def _record_failure(self, task: dict, rt: dict, failure: dict) -> None:
        st, p, cfg = self.state, self.state.progress, self.cfg
        excerpt = self.redactor.redact(failure.get("excerpt", ""))
        failure["excerpt"] = excerpt
        sig = failure.get("signature") or hashlib.sha1(f"{failure.get('kind')}|{excerpt[:200]}".encode()).hexdigest()[:12]
        bug = st.add_bug(task["id"], f"[{failure.get('cmd') or failure.get('kind')}] {excerpt}",
                         attempt=rt["repairs"] + 1, signature=sig)
        rt["failure"] = {**failure, "bug_id": bug["id"]}
        if sig == rt.get("last_signature"):
            rt["same_count"] += 1
        else:
            rt["last_signature"], rt["same_count"] = sig, 1
        if rt["same_count"] >= cfg["max_same_failure"]:
            last = p.get("last_provider")
            p["avoid_provider"] = [last] if last else []
            rt["hint"] = ("The same failure has repeated. Your previous fix did not work: re-read the error and the code, "
                          "form a different hypothesis and take a different approach.")
            rt["same_count"] = 0
            st.log_decision(f"{task['id']}: identical failure repeated; forcing a different approach/provider")
        p["last_error"] = excerpt[:500]
        self.log.warning("%s verification failed [%s]: %s", task["id"], failure.get("cmd") or failure.get("kind"), excerpt[:300])

    def _complete_task(self, task: dict, rt: dict) -> None:
        st, p = self.state, self.state.progress
        st.resolve_bugs(task_id=task["id"], status="fixed")
        if task.get("fixes_bugs"):
            st.resolve_bugs(ids=task["fixes_bugs"], status="fixed")
        task["status"] = "completed"
        task["completed_at"] = now_iso()
        kinds = {r.get("kind") for r in rt.get("last_results", []) if r.get("ok")}
        task["evidence"] = {
            "checks": rt.get("last_results", []), "repairs": rt["repairs"], "continuations": rt["continuations"],
            "providers": rt["providers_used"], "summary": rt.get("last_summary", ""),
            "weak": not (kinds & {"tests", "command"}),     # weak = only syntax checks ran
        }
        p.update(in_flight_files=[], task_runtime={}, current_task_id=None, avoid_provider=[],
                 last_error=None, last_action=f"completed {task['id']}", next_action="next task")
        msg = f"agent: complete {task['id']}" + (f" (after {rt['repairs']} repair attempt(s))" if rt["repairs"] else "")
        st.save()
        self.tb.git_commit(f"{msg} - {task['title'][:60]}")
        p["last_good_commit"] = self.git.head_full()
        st.save()
        self.log.info("%s completed", task["id"])

    def _abandon_task(self, task: dict, rt: dict) -> None:
        """Repair budget exhausted: roll the workspace back to the last good commit, remember
        what failed, and try a different provider/approach in a later cycle."""
        st, p, cfg = self.state, self.state.progress, self.cfg
        ref = p.get("last_good_commit")
        self.git.restore([self.tb.resolve(f) for f in rt["files"] if self._safe(f)], ref)
        st.resolve_bugs(task_id=task["id"], status="abandoned")
        task["cycles"] += 1
        failure = rt.get("failure") or {}
        task["notes"].append(f"cycle {task['cycles']} failed: {(failure.get('excerpt') or 'unknown')[:200]}")
        task["files_changed"] = [f for f in task["files_changed"] if f not in rt["files"]]
        if task["cycles"] >= cfg["max_task_cycles"]:
            task["status"] = "blocked"
            st.log_decision(f"{task['id']} blocked after {task['cycles']} failed attempt cycles: {task['title']}")
        else:
            task["status"] = "pending"
            st.log_decision(f"{task['id']}: attempt cycle {task['cycles']} failed; rolled back, retrying with a different approach/provider")
        last = p.get("last_provider")
        p.update(avoid_provider=[last] if last else [], task_runtime={}, in_flight_files=[], current_task_id=None)
        self._checkpoint(f"agent: checkpoint {task['id']} (attempt cycle {task['cycles']} failed, rolled back)")

    def _safe(self, rel: str) -> bool:
        try:
            self.tb.resolve(rel)
            return True
        except UnsafePathError:
            return False

    # ============================================================== final phase
    def _final_phase(self) -> int | None:
        st, p = self.state, self.state.progress
        p["status"] = "final_verification"
        p["next_action"] = "final verification"
        st.save()
        outcome = self.verifier.run()
        if outcome == "corrective":
            p["status"] = "working"
            st.save()
            return None
        if outcome == "failed":
            p["status"] = "failed_verification"
            p["last_error"] = "final verification did not converge within max_verification_rounds"
            self._checkpoint("agent: checkpoint (final verification failed)")
            self.log.error("final verification failed after %d rounds; see agent_state/REQUIREMENTS.json", p["verification_round"])
            return EXIT_VERIFY_FAILED
        try:
            self.packager.build()
        except SecretsFound as exc:
            for path, finding in exc.findings[:10]:
                self.verifier._corrective(f"secret:{path}", f"Remove secret from {path}",
                                          f"The final package scan found a secret in {path} ({finding}). Remove it, "
                                          "load it from an environment variable instead, and add a placeholder.", [])
            st.log_decision(f"Packaging blocked: {len(exc.findings)} potential secret(s) found")
            p["verification_round"] += 1
            p["status"] = "working"
            st.save()
            return None
        p.update(status="complete", next_action="none", last_action="final verification passed; ZIP built", last_error=None)
        st.log_decision("FINAL VERIFICATION PASSED. artifact/project.zip and FINAL_REPORT.md created.")
        self._checkpoint("agent: final verification")
        self.log.info("PROJECT COMPLETE: %s", self.packager.zip_path)
        return EXIT_COMPLETE

    def _ensure_package(self) -> None:
        if self.packager.zip_path.exists():
            return
        try:
            self.packager.build()
            self.log.info("re-created missing artifact: %s", self.packager.zip_path)
        except SecretsFound as exc:
            self.log.error("cannot package: %s", exc)

    # ======================================================================= exits
    def _pause(self, exc: Paused) -> int:
        p = self.state.progress
        p["status"] = exc.status
        p["last_action"] = f"paused: {exc.reason[:200]}"
        p["next_action"] = ("resume now (run budget used)" if exc.code == EXIT_PAUSED_BUDGET
                            else "wait for a provider to become available, then resume")
        self.log.warning("PAUSED (%s): %s", exc.status, exc.reason)
        self._checkpoint(f"agent: wip checkpoint (paused: {exc.status})")
        return exc.code

    def _blocked(self, exc: ConfigBlocked) -> int:
        p = self.state.progress
        p["status"] = "blocked_config"
        p["last_error"] = f"{exc.provider}: {exc.kind.value}: {exc.detail}"[:500]
        p["next_action"] = "fix provider credentials/configuration, then re-run (blocks lift when the credential changes)"
        self.log.error("BLOCKED by provider configuration error: %s", exc)
        self._checkpoint("agent: checkpoint (blocked: provider configuration error)")
        return EXIT_CONFIG

    def _failed(self, message: str) -> int:
        p = self.state.progress
        p["last_error"] = self.redactor.redact(message)[:500]
        self.log.error("%s", p["last_error"])
        self._checkpoint("agent: wip checkpoint (error)")
        return EXIT_ERROR

    def _stuck(self) -> int:
        st, p = self.state, self.state.progress
        blocked = [t for t in st.tasks if t["status"] == "blocked"]
        p["status"] = "stuck"
        p["next_action"] = "human review: unblock tasks with 'python -m agent.main unblock <TASK_ID>' after adjusting spec/config"
        for t in blocked:
            st.log_decision(f"STUCK: {t['id']} '{t['title']}' is blocked. Notes: {' | '.join(t['notes'][-3:])}")
        self.log.error("no runnable tasks remain; blocked: %s", ", ".join(t["id"] for t in blocked) or "(dependency chain)")
        self._checkpoint("agent: checkpoint (stuck: blocked tasks)")
        return EXIT_STUCK

    def unblock(self, task_id: str) -> bool:
        self.state.load()
        task = self.state.task(task_id)
        if not task or task["status"] != "blocked":
            return False
        task["status"], task["cycles"] = "pending", 0
        self.state.progress["status"] = "working"
        self.state.log_decision(f"{task_id} manually unblocked")
        self.state.save()
        return True
