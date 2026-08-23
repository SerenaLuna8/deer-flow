# ActWeave Install

This file is for coding agents working from an ActWeave source tree obtained through the private project's approved internal distribution channel. Continue from the repository root; do not invent or assume a public clone URL.

## Goal

Bootstrap an ActWeave local development workspace on the user's machine with the least risky path available.

Default preference:

1. Docker development environment
2. Local development environment

Do not assume API keys exist. Set up everything that can be prepared safely, then stop with a concise summary of what the user still needs to provide.

## Operating Rules

- Be idempotent. Re-running this document should not damage an existing setup.
- Prefer existing repo commands over ad hoc shell commands.
- Do not use `sudo` or install system packages without explicit user approval.
- Do not overwrite existing user config values unless the user asks.
- If a step fails, stop, explain the blocker, and provide the smallest next action.
- If multiple setup paths are possible, prefer Docker when Docker is already available.

## Success Criteria

Consider the setup successful when all of the following are true:

- The ActWeave source tree is available and the current working directory is the repo root.
- `config.yaml` exists.
- `DATABASE_URL` points to an existing PostgreSQL database and `make check-db` passes.
- For Docker setup, `make docker-init` completed successfully and Docker prerequisites are prepared, but services are not assumed to be running yet.
- For local setup, `make check` passed or reported no missing prerequisites, and `make install` completed successfully.
- The user receives the exact next command to launch ActWeave.
- `make setup-db` seeds active `deepseek-v4-flash`, `deepseek-v4-pro`, and multimodal `deepseek-v4-flash-vision-exp` model configurations. Each owns an independently encrypted API Key copy and a required `max_input_tokens=1,000,000`; the separate `settings.max_tokens=51,200` remains the output limit. Flash is the default lead model and the vision model is the default Vision Bridge selection. A system administrator manages both under `/admin/settings/models` and `/admin/settings/system`.

## Steps

- If the current directory is not the ActWeave repository root, ask for the approved internal source location or an existing checkout, then change into the repository root.
- Confirm the current directory is the ActWeave repository root by checking that `Makefile`, `backend/`, `frontend/`, and `config.example.yaml` exist.
- Detect whether `config.yaml` already exists.
- If `config.yaml` does not exist, run `make config`.
- Detect whether Docker is available and the daemon is reachable with `docker info`.
- Require PostgreSQL-only `DATABASE_URL` and `POSTGRES_ADMIN_URL` entries in the root `.env` or explicit environment. Do not read or print their values. `make setup-db` loads the root `.env` only when it exists. It is the only initialization entry point: it accepts an empty target, executes `full_schema.sql`, records `schema_v1`, and performs first-install bootstrap. First installation also requires `ACT_WEAVE_SECRET_KEY` (Base64 for exactly 32 decoded bytes) and nonempty `ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY`; both are preflighted before DDL and only encrypted per-model copies are stored. Existing legacy, unknown, or mismatched nonempty databases are never upgraded and must be explicitly recreated. Run `make setup-db`, then `make check-db`.
- Never rely on application startup to initialize or repair PostgreSQL. Runtime startup and `make check-db` are read-only schema consumers. If an existing database has a legacy or unknown marker, is unmarked and nonempty, or has catalog drift, stop and require a new empty target instead of stamping, resetting, or repairing it.
- The application compose stack does not provision PostgreSQL. When Docker is available, a standalone `postgres:17-alpine` container is acceptable, but use placeholders for credentials and keep the application role non-superuser. ActWeave does not use RLS; project access is enforced by `ProjectContext` and scoped repositories.
- If Docker is available:
  - Run `make docker-init`.
  - Treat this as Docker prerequisite preparation only. Do not claim that app services, compose validation, or image builds have already succeeded.
  - Do not start long-running services unless the user explicitly asks or this setup request clearly includes launch verification.
  - Tell the user the recommended next command is `make docker-start`.
- If Docker is not available:
  - Run `make check`.
  - If `make check` reports missing system dependencies such as `node`, `pnpm`, `uv`, or `nginx`, stop and report the missing tools instead of attempting privileged installs.
  - If prerequisites are satisfied, run `make install`.
  - Tell the user the recommended next command is `make dev`.
- Do not inspect `config.yaml` for model entries: top-level `models:` is removed and rejected. Model definitions and provider secrets are PostgreSQL system settings.
- System Model create, update, and connection-test forms require `max_input_tokens` in the range `1..2,000,000`. Configure the Provider Model's maximum accepted input, not its output `max_tokens` or a Run token budget; context occupancy and automatic compaction use this frozen model value as their capacity denominator.
- Do not print or copy values from `.env`, `frontend/.env`, or other secret-bearing files. Let `make setup-db` load the root `.env` when present; explicit environment variables also work without the file. Runtime imports must not load dotenv implicitly. After startup, tell a system administrator that each bootstrapped DeepSeek model owns its encrypted API Key copy; all can be inspected or changed at `/admin/settings/models`, and Flash is initially the default.
- If the repository already appears configured, avoid repeating expensive work unless it is necessary to verify the environment.

## Verification

Use the lightest verification that matches the chosen setup path.

For Docker setup:

- Confirm explicit `make setup-db` completed against an empty target, then confirm `make check-db` completed successfully before treating the environment as launchable.
- Confirm `make docker-init` completed successfully.
- Confirm `config.yaml` exists.
- State explicitly that Docker services were not started and `make docker-start` is still the first real launch step.
- Do not leave background services running unless the user asked for that.

For local setup:

- Confirm explicit `make setup-db` completed against an empty target, then confirm `make check-db` completed successfully.
- Confirm `make install` completed successfully.
- Confirm `config.yaml` exists.
- Do not leave background services running unless the user asked for that.

## Final Response Format

Return a short status report with:

1. Setup path used: Docker or local
2. Setup level reached: Docker prerequisites prepared or local dependencies installed
3. Files created or detected: for example `config.yaml`
4. Remaining user action: optional database-backed model API-Key changes, process/tool env values, auth files, or nothing
5. Exact next command to start ActWeave
6. PostgreSQL host/database and schema marker from redacted check output; never include the URL, username, or password

## EXECUTE NOW

Complete the steps above. When finished, stop after the setup boundary and report status instead of continuing into unrelated project work.
