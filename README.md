# Autonomous Multi-AI Coding Agent

Give it one `MASTER_SPEC.md`. It plans, codes, tests, debugs, verifies and packages a ZIP on
GitHub Actions, survives quota limits, and resumes on its own.

## Quick start

1. Put this repo on GitHub (private recommended).
2. Replace `MASTER_SPEC.md` with your full specification (delete the `MASTER_SPEC_TEMPLATE` line).
3. Add secrets (Settings → Secrets and variables → Actions): `GEMINI_API_KEY`
   (optional: `OPENROUTER_API_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`).
4. Edit `.github/agent.yml` (providers, models, limits).
5. Actions → **Autonomous Coder** → *Run workflow*. That is the only manual step.

The generated project is committed under `project/`; progress under `agent_state/`.
When finished, download `final-project` from the run's artifacts (`project.zip`, `FINAL_REPORT.md`).

Local run: `pip install -r requirements.txt && GEMINI_API_KEY=... python -m agent.main run`
Other commands: `status`, `unblock T012`, `package`, `run --replan`, `run --clear-blocks`.

## How it works

```
MASTER_SPEC.md ─► requirements (REQ-001…) ─► dependency-ordered tasks (TASKS.json)
      loop: next ready task → focused context → AI JSON → validate → apply → verify
            pass → commit "agent: complete T0xx" │ fail → capture error → repair (bounded) → …
      all tasks done ─► FINAL VERIFIER: tests/lint/typecheck/build + per-requirement audit
            missing/partial/critical bug → corrective task → loop again
      verified ─► secret scan ─► artifact/project.zip + FINAL_REPORT.md
```

* **State** (`agent_state/`): `TASKS.json`, `PROGRESS.json`, `BUGS.json`, `REQUIREMENTS.json`, `DECISIONS.md`.
  Saved atomically before every expensive step; planning itself is resumable.
* **Providers** (`agent/providers/`): common interface `generate / is_available / classify_error /
  get_usage_status`. Fallback only for rate-limit, quota and outage; auth/permission/config errors
  halt (exit 3) with a clear record. Cooldowns persist in `PROGRESS.json` with exponential backoff.
* **No false completion**: a requirement counts as verified only if the verifier cites existing files,
  the linked tasks are complete, and executable test evidence exists (not just syntax checks).
* **Tests are protected**: edits that reduce assertions, add skips, or delete committed tests are rejected.
* **Safety**: all paths confined to `project/`; commands run without a shell from an allow-list, no
  inline-eval flags, no path escapes, scrubbed environment (no secrets/tokens reach generated code);
  secrets are redacted from logs and prompts, and the ZIP is scanned before packaging.
* **Repair limits**: identical repeated failures force a different approach/provider; after
  `max_repair_attempts` the task is rolled back to the last good commit and retried in a later cycle;
  after `max_task_cycles` it is marked `blocked` (exit 5).

## Exit codes (`run`)

| code | meaning | workflow behaviour |
|---|---|---|
| 0 | complete | uploads ZIP |
| 10 | per-run budget used, work remains | self-dispatches again (capped by `MAX_CHAIN`) |
| 11 | no provider available (quota) | waits for the scheduled run |
| 3 | provider auth/config problem | fails; fix secret/config, re-run |
| 4 | uncommitted changes not made by the agent | fails |
| 5 / 6 | stuck / final verification did not converge | fails; see `agent_state/` |

## Honest limitations

* Quality depends on the models you configure. Free tiers (and model names on OpenRouter/Cloudflare)
  change; set `provider_options.*.model` to something currently available. Gemini's default is `gemini-2.5-flash`.
* Provider adapters are tested with classification unit tests and a scripted model, **not** against the
  live APIs in this repo's test suite. Do a first real run on a tiny spec (`examples/MASTER_SPEC.calc.md`).
* Generated tests/commands run on the runner. The sandbox is least-privilege (allow-list, scrubbed env)
  but not a container; `make`/`npm` scripts can run project-defined code. Use a private repo.
* GitHub disables scheduled workflows after ~60 days of repo inactivity; agent commits count as activity.
* Python tooling for the generated project shares the runner's Python environment.

## Tests

`python -m unittest discover -s tests -t .` — includes the controlled end-to-end project (plan → code →
intentional failure → automatic debugging → pass → final verification → ZIP), quota pause/resume without
re-planning, provider fallback, no-fallback on auth errors, turn-budget checkpointing, path/command safety.
