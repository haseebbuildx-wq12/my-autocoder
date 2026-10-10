"""Autonomous planning. Resumable: progress is saved after every AI call, so a quota stop in
the middle of planning continues from the same chunk/batch next run."""
from __future__ import annotations

from . import prompts
from .schema import validate_requirements, validate_tasks


class PlanError(Exception):
    pass


def split_spec(spec: str, max_chars: int) -> list[str]:
    """Split on line boundaries, preferring headings, into chunks of at most ~max_chars."""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for line in spec.splitlines(keepends=True):
        is_heading = line.lstrip().startswith("#")
        if cur and (size + len(line) > max_chars or (is_heading and size > max_chars * 0.6)):
            chunks.append("".join(cur))
            cur, size = [], 0
        while len(line) > max_chars:                     # pathological single line
            chunks.append(line[:max_chars])
            line = line[max_chars:]
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append("".join(cur))
    return [c for c in chunks if c.strip()]


class Planner:
    def __init__(self, agent):
        self.a = agent

    def plan(self, spec: str) -> None:
        st = self.a.state
        cfg = self.a.cfg
        pl = st.progress.setdefault("planning", {})
        if pl.get("stage") == "done":
            return
        st.progress["status"] = "planning"
        chunks = split_spec(spec, cfg["plan_chunk_chars"])
        if not chunks:
            raise PlanError("MASTER_SPEC.md is empty")
        pl.setdefault("stage", "requirements")
        pl["chunks_total"] = len(chunks)

        if pl["stage"] == "requirements":
            outline = self.a.ctx.outline()
            for i in range(pl.get("chunks_done", 0), len(chunks)):
                st.progress["last_action"] = f"planning: extracting requirements (part {i + 1}/{len(chunks)})"
                prompt = prompts.requirements_prompt(chunks[i], i + 1, len(chunks), outline)
                found = self.a.ask_json("plan-requirements", prompt, prompts.SYSTEM_PLAN, validate_requirements)
                for item in found:
                    st.add_requirement(item["description"], item["source"])
                pl["chunks_done"] = i + 1
                st.save()
            if not st.requirements:
                raise PlanError("no requirements could be extracted from MASTER_SPEC.md")
            pl["stage"] = "tasks"
            st.save()

        if pl["stage"] == "tasks":
            size = cfg["plan_requirement_batch"]
            reqs = st.requirements
            batches = [reqs[i:i + size] for i in range(0, len(reqs), size)]
            hint = self.a.checks.project_info().describe()
            for j in range(pl.get("req_batches_done", 0), len(batches)):
                st.progress["last_action"] = f"planning: building task graph (batch {j + 1}/{len(batches)})"
                batch = [{"id": r["id"], "description": r["description"]} for r in batches[j]]
                prompt = prompts.tasks_prompt(batch, st.tasks, hint)
                tasks = self.a.ask_json("plan-tasks", prompt, prompts.SYSTEM_PLAN, validate_tasks)
                self._add_batch(tasks, {r["id"] for r in reqs})
                pl["req_batches_done"] = j + 1
                st.save()
            self._finalize()
            pl["stage"] = "done"
            st.progress["next_action"] = "implement tasks"
            st.log_decision(f"Plan created: {len(st.requirements)} requirements, {len(st.tasks)} tasks.")
            st.save()

    # -- helpers -----------------------------------------------------------------
    def _add_batch(self, tasks: list[dict], valid_reqs: set[str]) -> None:
        st = self.a.state
        mapping: dict[str, str] = {}
        existing_ids = {t["id"] for t in st.tasks}
        for t in tasks:
            reqs = [r for r in t["requirements"] if r in valid_reqs]
            new = st.add_task(title=t["title"], description=t["description"], verification=t["verification"],
                              verification_commands=t["verification_commands"], files=t["files"],
                              requirements=reqs, depends_on=[])
            if t["id"]:
                mapping[t["id"]] = new["id"]
        # translate dependencies; only allow edges to EARLIER tasks, which guarantees a DAG
        order = [t["id"] for t in st.tasks]
        created = st.tasks[len(st.tasks) - len(tasks):]
        for raw, task in zip(tasks, created):
            deps = []
            for d in raw["depends_on"]:
                target = mapping.get(d) or (d if d in existing_ids else None)
                if target and target != task["id"] and order.index(target) < order.index(task["id"]):
                    deps.append(target)
                else:
                    st.log_decision(f"Dropped invalid/cyclic dependency {d!r} on {task['id']}")
            task["depends_on"] = sorted(set(deps))

    def _finalize(self) -> None:
        st = self.a.state
        covered = {r for t in st.tasks for r in t["requirements"]}
        for req in st.requirements:
            req["tasks"] = [t["id"] for t in st.tasks if req["id"] in t["requirements"]]
            if req["id"] not in covered:
                task = st.add_task(
                    title=f"Implement {req['id']}", description=req["description"],
                    verification="Demonstrate the requirement with a test or command.",
                    requirements=[req["id"]],
                    depends_on=[st.tasks[0]["id"]] if st.tasks else [],
                )
                st.log_decision(f"{req['id']} was not covered by the generated plan; added {task['id']}.")
        if not st.tasks:
            raise PlanError("planner produced no tasks")
