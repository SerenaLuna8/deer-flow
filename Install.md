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
- `make setup-db` seeds a DeepSeek Model Provider owning the bootstrap DeepSeek key, with active `deepseek-v4-flash`, `deepseek-v4-pro`, and multimodal `deepseek-v4-flash-vision-exp` model configurations bound to it, plus (unless explicitly skipped) a SiliconFlow Model Provider owning its own key with the default embedding and reranker models. API Keys and endpoints belong to the provider; each bound text model stores an encrypted per-model copy derived from the provider key, and every model requires `max_input_tokens=1,000,000`; the separate `settings.max_tokens=51,200` remains the output limit. Flash is the default lead model and the vision model is the default Vision Bridge selection. A system administrator manages everything under `/admin/settings/models` and `/admin/settings/system`; additional providers are added from that page.

## Steps

- If the current directory is not the ActWeave repository root, ask for the approved internal source location or an existing checkout, then change into the repository root.
- Confirm the current directory is the ActWeave repository root by checking that `Makefile`, `backend/`, `frontend/`, and `config.example.yaml` exist.
- Detect whether `config.yaml` already exists.
- If `config.yaml` does not exist, run `make config`.
- Detect whether Docker is available and the daemon is reachable with `docker info`.
- Require PostgreSQL-only `DATABASE_URL` and `POSTGRES_ADMIN_URL` entries in the root `.env` or explicit environment. Do not read or print their values. `make setup-db` loads the root `.env` only when it exists. It is the only initialization entry point: it accepts an empty target, executes `full_schema.sql`, records `schema_v1`, and performs first-install bootstrap. First installation also requires `ACT_WEAVE_SECRET_KEY` (Base64 for exactly 32 decoded bytes) and nonempty `ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY`; both are preflighted before DDL. The DeepSeek key seeds the DeepSeek Model Provider and is stored only as encrypted provider plus per-model copies. The SiliconFlow provider seed is preflighted the same way: provide `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY` (SiliconFlow keeps its own key, separate from DeepSeek's) to seed that provider with its embedding and reranker models, or set `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP=1` to install the schema without that seed when the deployment does not use Knowledge. Existing legacy, unknown, or mismatched nonempty databases are never upgraded and must be explicitly recreated. Context Evidence / Projection v2 likewise has no compatibility migration: stop every Gateway, Frontend, Worker, and Scheduler, confirm the exact database target, recreate it with `make reset-db`, and deploy the same application version together; this irreversibly removes all application history and mixed old/new clients are unsupported. Run `make setup-db` for a new target or the explicitly confirmed reset workflow for an existing development target, then `make check-db`.
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
- Knowledge (RAG knowledge bases) is optional and disabled by default; a missing `knowledge` block in `config.yaml` keeps its routes, Agent tool, and Knowledge Task worker off at runtime (`make setup-db` still preflights the Knowledge seed key or its explicit skip switch, see above). Enabling it later requires: an administrator-prepared MinIO S3 API endpoint (`host:port`, not the `:9001` Console) reachable by both Gateway and Worker, a pre-created bucket (default `actweave-knowledge`) with versioning and Object Lock off, credentials allowed to get/put/delete objects, call `GetBucketVersioning`, and list the bucket's `projects/*/knowledge/` prefixes, the `ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT` / `ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY` / `ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY` environment values, and a restart of Gateway and Worker together. Gateway and Worker verify bucket accessibility and unversioned deletion semantics at startup and fail fast for `Enabled` or `Suspended` versioning; every upload checks again immediately before its PUT, so runtime versioning drift is rejected before object creation. Knowledge forces each accepted file into one PUT so a crash cannot leave an incomplete multipart upload invisible to ordinary object cleanup; `knowledge.upload_max_bytes` is fixed to a maximum of 50 MiB, and each Gateway/Worker `MinioObjectStore` serializes PUTs because the MinIO Python SDK buffers a one-part upload in process memory. Inside Compose the endpoint must be an address both containers can reach (for example `host.docker.internal:9000`), never the container's own `127.0.0.1`. After a deployment has stored Knowledge Documents, retain its original `knowledge.minio` block even when changing `enabled` back to `false`; the Worker keeps only the Project-retention cleanup capability active. Removing that storage configuration while Document rows or unfinished exact-key object-cleanup tasks remain, or removing versioning-read/prefix-list/delete permission, makes Project purge fail closed and use the normal five-attempt retention retry budget rather than falsely complete. If those attempts are exhausted, restore the exact MinIO configuration and permission and requeue the safe dead retention job from `/admin/jobs`.
- System Model create, update, and connection-test forms require `max_input_tokens` in the range `1..2,000,000`. Configure the Provider Model's maximum accepted input, not its output `max_tokens` or a Run token budget; context occupancy and automatic compaction use this frozen model value as their capacity denominator.
- Do not print or copy values from `.env`, `frontend/.env`, or other secret-bearing files. Let `make setup-db` load the root `.env` when present; explicit environment variables also work without the file. Runtime imports must not load dotenv implicitly. After startup, tell a system administrator that the bootstrapped DeepSeek and SiliconFlow Model Providers each own their API Key and endpoint (bound text models carry derived encrypted copies); everything can be inspected or changed at `/admin/settings/models`, rotating a provider key re-encrypts all of its bound text models, and Flash is initially the default.
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
4. Remaining user action: optional database-backed Model Provider API-Key changes, process/tool env values, auth files, or nothing
5. Exact next command to start ActWeave
6. PostgreSQL host/database and schema marker from redacted check output; never include the URL, username, or password

## EXECUTE NOW

Complete the steps above. When finished, stop after the setup boundary and report status instead of continuing into unrelated project work.
