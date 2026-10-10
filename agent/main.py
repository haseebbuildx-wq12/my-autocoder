"""Command line: python -m agent.main <run|status|gate|checkpoint|package|unblock>

Exit codes (run):
  0 complete | 1 error | 3 provider config/auth problem | 4 foreign uncommitted changes
  5 stuck (blocked tasks) | 6 final verification failed
  10 per-run budget used (continue immediately) | 11 no provider available (wait for quota)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .loop import Agent, EXIT_COMPLETE, EXIT_ERROR
from .util import Redactor


class RedactingFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.redactor = Redactor()

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor.redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(verbose: bool) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    handler.addFilter(RedactingFilter())
    log = logging.getLogger("agent")
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    return log


def github_output(**values) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for k, v in values.items():
            fh.write(f"{k}={v}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent")
    ap.add_argument("--root", default=".", help="repository root")
    ap.add_argument("--config", default=None, help="path to agent.yml (default .github/agent.yml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="work until complete, paused or blocked")
    run.add_argument("--replan", action="store_true")
    run.add_argument("--allow-dirty", action="store_true")
    run.add_argument("--clear-blocks", action="store_true")
    sub.add_parser("status")
    gate = sub.add_parser("gate", help="decide whether a workflow run should do work")
    gate.add_argument("--event", default="workflow_dispatch")
    sub.add_parser("checkpoint", help="commit (and push) current state")
    sub.add_parser("package", help="rebuild artifact/project.zip if the project is complete")
    unb = sub.add_parser("unblock")
    unb.add_argument("task_id")
    args = ap.parse_args(argv)

    log = setup_logging(args.verbose)
    root = Path(args.root).resolve()
    try:
        cfg = load_config(root, args.config)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_ERROR
    agent = Agent(root, cfg, log=log)

    if args.cmd == "run":
        code = agent.run(replan=args.replan, allow_dirty=args.allow_dirty, clear_blocks=args.clear_blocks)
        status = agent.state.progress["status"]
        github_output(code=code, status=status, complete=str(status == "complete").lower())
        return code

    agent.state.load()
    p = agent.state.progress
    if args.cmd == "status":
        done = sum(t["status"] == "completed" for t in agent.state.tasks)
        print(json.dumps({
            "status": p["status"], "tasks": f"{done}/{len(agent.state.tasks)}",
            "current_task": p.get("current_task_id"), "last_provider": p.get("last_provider"),
            "last_action": p.get("last_action"), "last_error": p.get("last_error"),
            "next_action": p.get("next_action"), "providers": agent.manager.status(),
            "requirements": {s: sum(r["status"] == s for r in agent.state.requirements)
                             for s in ("verified", "partial", "missing", "unverified")},
        }, indent=2, default=str))
        return 0
    if args.cmd == "gate":
        complete = p["status"] == "complete"
        run_it = True
        if args.event == "schedule" and (not cfg["schedule_enabled"] or complete):
            run_it = False
        if not (root / cfg["spec_path"]).is_file():
            run_it = False
        log.info("gate: event=%s status=%s run=%s", args.event, p["status"], run_it)
        github_output(run=str(run_it).lower(), status=p["status"])
        return 0
    if args.cmd == "checkpoint":
        agent.state.save()
        agent.tb.git_commit("agent: workflow checkpoint")
        agent.git.push()
        return 0
    if args.cmd == "package":
        if p["status"] != "complete":
            log.error("project is not complete (status: %s)", p["status"])
            return EXIT_ERROR
        agent._ensure_package()
        return EXIT_COMPLETE
    if args.cmd == "unblock":
        ok = agent.unblock(args.task_id)
        log.info("unblocked %s" if ok else "%s is not a blocked task", args.task_id)
        return 0 if ok else EXIT_ERROR
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
