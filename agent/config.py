"""Configuration: defaults, .github/agent.yml loading, validation."""
from __future__ import annotations

import copy
import os
from pathlib import Path, PurePosixPath
from typing import Any

KNOWN_PROVIDERS = ("gemini", "openrouter", "cloudflare")

DEFAULTS: dict[str, Any] = {
    # --- providers ---------------------------------------------------------------
    "providers": ["gemini"],            # priority order; unused providers need no credentials
    "provider_options": {
        "gemini": {"model": "gemini-2.5-flash"},
        "openrouter": {"model": "meta-llama/llama-3.3-70b-instruct:free"},
        "cloudflare": {"model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "account_id": ""},
    },
    "request_timeout_seconds": 180,
    "max_output_tokens": 16384,
    "cooldowns": {                      # seconds a provider stays "unavailable" after a failure
        "rate_limit": 90,
        "quota": 7200,
        "outage": 300,
    },
    "max_cooldown_seconds": 21600,      # exponential backoff cap

    # --- layout -------------------------------------------------------------------
    "workspace_dir": "project",         # where the generated project lives (agent code stays outside it)
    "spec_path": "MASTER_SPEC.md",
    "state_dir": "agent_state",
    "artifact_dir": "artifact",

    # --- loop limits --------------------------------------------------------------
    "max_ai_turns": 25,                 # AI calls per workflow run, then checkpoint and exit
    "max_repair_attempts": 5,           # repair attempts per task attempt-cycle
    "max_same_failure": 2,              # identical consecutive failures before forcing another approach
    "max_task_cycles": 3,               # failed attempt-cycles before a task is marked blocked
    "max_continuations": 3,             # "not done yet" follow-ups per task
    "max_verification_rounds": 5,       # corrective rounds in final verification
    "max_runtime_minutes": 300,         # wall-clock budget per run (GitHub jobs are finite)
    "command_timeout_seconds": 600,
    "max_command_output_chars": 20000,

    # --- context limits -----------------------------------------------------------
    "max_file_bytes": 120000,
    "max_context_files": 30,
    "max_prompt_chars": 120000,
    "plan_chunk_chars": 24000,
    "plan_requirement_batch": 30,
    "verify_batch": 6,
    "max_write_bytes": 600000,
    "max_files_per_response": 40,

    # --- verification -------------------------------------------------------------
    "verification_commands": [],        # extra commands run on every verification (tasks + final)
    "build_commands": [],               # overrides the detected build command(s)
    "overrides": {"install": [], "test": [], "lint": [], "typecheck": []},
    "task_checks": ["syntax", "tests"],
    "final_checks": ["tests", "lint", "typecheck", "build"],
    "allowed_executables_extra": [],
    "test_env": {},                     # dummy, NON-secret values injected into project commands (tests)

    # --- scheduling / git ---------------------------------------------------------
    "schedule_enabled": True,
    "git": {
        "commit": True,
        "push": False,                  # the workflow sets AGENT_PUSH=1
        "author_name": "autonomous-coder[bot]",
        "author_email": "autonomous-coder@users.noreply.github.com",
    },
}


class ConfigError(Exception):
    pass


def deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _check_relative_dir(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts:
        raise ConfigError(f"{name} must be a relative path inside the repository")


def validate(cfg: dict) -> None:
    providers = cfg["providers"]
    if not isinstance(providers, list) or not providers:
        raise ConfigError("providers must be a non-empty list")
    for name in providers:
        if name not in KNOWN_PROVIDERS:
            raise ConfigError(f"unknown provider '{name}' (known: {', '.join(KNOWN_PROVIDERS)})")
    for key in ("workspace_dir", "spec_path", "state_dir", "artifact_dir"):
        _check_relative_dir(key, cfg[key])
    for key in (
        "max_ai_turns", "max_repair_attempts", "max_same_failure", "max_task_cycles",
        "max_continuations", "max_verification_rounds", "max_runtime_minutes",
        "command_timeout_seconds", "max_file_bytes", "max_context_files", "max_prompt_chars",
    ):
        if not isinstance(cfg[key], int) or cfg[key] < 1:
            raise ConfigError(f"{key} must be a positive integer")
    for key in ("verification_commands", "build_commands", "allowed_executables_extra"):
        if not isinstance(cfg[key], list) or not all(isinstance(x, str) for x in cfg[key]):
            raise ConfigError(f"{key} must be a list of strings")
    te = cfg["test_env"]
    if not isinstance(te, dict) or not all(isinstance(k, str) and isinstance(v, (str, int, float, bool)) for k, v in te.items()):
        raise ConfigError("test_env must be a mapping of names to simple values")
    ws = PurePosixPath(cfg["workspace_dir"])
    for key in ("state_dir", "artifact_dir"):
        other = PurePosixPath(cfg[key])
        if other == ws or ws in other.parents:
            raise ConfigError(f"{key} must not live inside workspace_dir (it would end up in the ZIP)")


def load_config(root: Path, path: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    cfg_path = Path(path) if path else Path(root) / ".github" / "agent.yml"
    if cfg_path.exists():
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise ConfigError("PyYAML is required: pip install -r requirements.txt") from exc
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{cfg_path} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{cfg_path} must contain a mapping")
        deep_merge(cfg, data)
    if os.environ.get("AGENT_PUSH") == "1":
        cfg["git"]["push"] = True
    validate(cfg)
    return cfg
