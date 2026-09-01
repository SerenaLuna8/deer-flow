# ActWeave Install

This file is for coding agents working from an ActWeave source tree obtained through the private project's approved internal distribution channel. Continue from the repository root; do not invent or assume a public clone URL.

## Goal

Bootstrap an ActWeave local development workspace on the user's machine with the least risky path available.

The repository supports a local host-process application environment. Docker is
optional and is used only for Sandbox containers or the standalone Sandbox
Provisioner.

Do not assume API keys exist. Set up everything that can be prepared safely, then stop with a concise summary of what the user still needs to provide.

## Operating Rules

- Be idempotent. Re-running this document should not damage an existing setup.
- Prefer existing repo commands over ad hoc shell commands.
- Do not use `sudo` or install system packages without explicit user approval.
- Do not overwrite existing user config values unless the user asks.
- If a step fails, stop, explain the blocker, and provide the smallest next action.
- Do not substitute an application container workflow for the supported local commands.

## Success Criteria

Consider the setup successful when all of the following are true:

- The ActWeave source tree is available and the current working directory is the repo root.
- `config.yaml` exists.
- `DATABASE_URL` points to an existing PostgreSQL database and `make check-db` passes.
- `make check` passed or reported no missing prerequisites, and `make install` completed successfully.
- If the user selected Docker Sandbox, `make setup-sandbox` completed successfully; this does not start the application.
- The user receives the exact next command to launch ActWeave.
- `make setup-db` seeds a DeepSeek Model Provider owning the bootstrap DeepSeek key, with active `deepseek-v4-flash`, `deepseek-v4-pro`, and multimodal `deepseek-v4-flash-vision-exp` model configurations bound to it, plus (unless explicitly skipped) a SiliconFlow Model Provider owning its own key with the default embedding and reranker models. API Keys and endpoints belong to the provider; each bound text model stores an encrypted per-model copy derived from the provider key, and every model requires `max_input_tokens=1,000,000`; the separate `settings.max_tokens=51,200` remains the output limit. Flash is the default lead model and the vision model is the default Vision Bridge selection. A system administrator manages everything under `/admin/settings/models` and `/admin/settings/system`; additional providers are added from that page.

## Steps

- If the current directory is not the ActWeave repository root, ask for the approved internal source location or an existing checkout, then change into the repository root.
- Confirm the current directory is the ActWeave repository root by checking that `Makefile`, `backend/`, `frontend/`, and `config.example.yaml` exist.
- Detect whether `config.yaml` already exists.
- If `config.yaml` does not exist, run `make config`.
- Require PostgreSQL-only `DATABASE_URL` and `POSTGRES_ADMIN_URL` entries in the root `.env` or explicit environment. Do not read or print their values. `make setup-db` loads the root `.env` only when it exists and is the only supported public Make initialization entry point: it accepts an empty target, validates `full_schema.sql` plus generated `schema_comments.sql`, composes them into one PostgreSQL transaction, records `schema_v1`, and performs first-install bootstrap; neither SQL file is a standalone installer. First installation also requires `ACT_WEAVE_SECRET_KEY` (Base64 for exactly 32 decoded bytes) and nonempty `ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY`; both are preflighted before DDL. Provide `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY` to seed the SiliconFlow provider or set `ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP=1` when Knowledge is unused. Optional Knowledge bootstrap accepts all four `ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT`, `ACT_WEAVE_KNOWLEDGE_MINIO_BUCKET`, `ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY`, and `ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY`, or none; partial input fails before DDL. Run `make setup-db` for a new target, then `make check-db`.
- `make upgrade-db` is the only database Schema upgrade entry point and never runs at application startup. Stop all services, back up the target, and run it in a maintenance window. The current head remains `schema_v1` and the migration Registry is empty, so today it only verifies an exact current Catalog and returns without DDL. A future head must ship a linear forward migration and the matching fresh-install snapshot together. Unknown markers, unmarked nonempty schemas, and Catalog drift are rejected; downgrade is unsupported and rollback means restoring the operator backup. The command uses only `DATABASE_URL`, never `POSTGRES_ADMIN_URL` or bootstrap model/storage secrets.
- Never rely on application startup to initialize or repair PostgreSQL. Runtime startup and `make check-db` are read-only schema consumers. If an existing database has a legacy or unknown marker, is unmarked and nonempty, or has catalog drift, stop and require a new empty target instead of stamping, resetting, or repairing it.
- Provision PostgreSQL outside the application startup path and keep the application role non-superuser. ActWeave does not use RLS; project access is enforced by `ProjectContext` and scoped repositories.
- Run `make check`.
- If `make check` reports missing system dependencies such as `node`, `pnpm`, `uv`, or `nginx`, stop and report the missing tools instead of attempting privileged installs.
- If prerequisites are satisfied, run `make install`.
- If the user explicitly selected Docker Sandbox, confirm the Docker daemon is reachable with `docker info`, then run `make setup-sandbox`. Treat this as Sandbox image preparation only.
- Tell the user the application launch command is `make dev`.
- Do not inspect `config.yaml` for model entries: top-level `models:` is removed and rejected. Model definitions and provider secrets are PostgreSQL system settings.
- Knowledge (RAG knowledge bases) is optional. With all four install-time MinIO variables present, explicit Schema V1 setup probes the existing bucket before DDL, encrypts the secret, and seeds `knowledge_system_settings` enabled; with all four absent it seeds the module disabled. The bootstrap endpoint is `host:port` and currently uses non-TLS MinIO. Use a dedicated empty bucket for a fresh database: setup never deletes pre-existing objects. The bucket is always administrator-provisioned and never guessed or created by ActWeave. It must be reachable by both Gateway and Worker, unversioned with Object Lock off, and grant object get/put/delete, `GetBucketVersioning`, and listing `projects/*/knowledge/`. Administrators can configure or change the singleton at `/admin/settings/knowledge`, even while Knowledge is unavailable. Saving an enabled configuration probes storage before committing; a failed probe leaves the previous revision unchanged. The secret is encrypted with `ACT_WEAVE_SECRET_KEY` and never returned; changing its endpoint requires re-entering the secret.
- Local Knowledge parsing requires the workspace `extraction-local` extra; `make install` installs it explicitly. The host must also provide libmagic and an available OS sandbox (`sandbox-exec` on macOS or bubblewrap on Linux). Do not install system packages with `sudo` from this workflow. Missing resources or denied isolation keep new parsing unavailable instead of falling back to an unsandboxed process. Before enabling Knowledge on a Linux target, run the extraction runtime and format matrix on that exact host and review its platform resource lock.
- Restart Gateway and Worker together after changing Knowledge enablement, storage, limits, or cache settings. A failed Knowledge startup storage check disables only the optional module and reports `knowledge=unavailable`; administration and the other application services continue. Summary-model changes apply to subsequent summary tasks immediately. Each accepted upload remains a single PUT capped at 50 MiB, with one upload slot per store instance. Use a storage endpoint reachable by both host processes.
- Retain the original storage settings after disabling Knowledge until all historical Documents and exact-key cleanup tasks are gone. Project retention remains independently composed and fails closed if required storage evidence is unavailable. If its retry budget is exhausted, restore the exact storage settings and permissions, restart the Worker, then requeue the safe dead retention job from `/admin/jobs`.
- System Model create, update, and connection-test forms require `max_input_tokens` in the range `1..2,000,000`. Configure the Provider Model's maximum accepted input, not its output `max_tokens` or a Run token budget; context occupancy and automatic compaction use this frozen model value as their capacity denominator.
- Do not print or copy values from `.env`, `frontend/.env`, or other secret-bearing files. Let `make setup-db` load the root `.env` when present; explicit environment variables also work without the file. Runtime imports must not load dotenv implicitly. After startup, tell a system administrator that the bootstrapped DeepSeek and SiliconFlow Model Providers each own their API Key and endpoint (bound text models carry derived encrypted copies); everything can be inspected or changed at `/admin/settings/models`, rotating a provider key re-encrypts all of its bound text models, and Flash is initially the default.
- If the repository already appears configured, avoid repeating expensive work unless it is necessary to verify the environment.

## Knowledge configuration migration

The old nonempty `knowledge` YAML block is rejected. This is a configuration-data migration, not a database schema upgrade; it neither creates tables nor stamps a schema marker.

1. Stop Gateway and Worker before the cutover. Check the target against the current Schema V1. A nonempty database with schema drift needs a separately approved replacement/recreation decision; this workflow does not authorize reset or repair.
2. Keep the legacy YAML block and its referenced environment variables available. Against an already installed current schema, from `backend/` run `PYTHONPATH=. uv run --env-file ../.env python scripts/migrate_knowledge_config.py`. Use `--config /absolute/path/config.yaml` if necessary. The script reads raw YAML, validates it, encrypts the MinIO secret, and writes the singleton atomically. Repeating it updates the same singleton and advances its revision; output lists field names only.
3. Remove the legacy `knowledge` block from the configuration used by every process. Preserve the master key: its loss makes stored credentials unreadable. Start the same frontend/backend version together.
4. Check `/admin/settings/knowledge` and `/admin/operations`: confirm the intended settings and `knowledge=ready` (or intentionally `disabled`). Validate a document upload and retrieval before declaring the Knowledge cutover complete. Storage failure keeps administration reachable and does not constitute a successful Knowledge launch.

Query caches are process-local and store only query vectors. A Base summary index uses the configured text System Model; summary tasks share the document's open-task slot, and failures leave the source Document ready. Re-embedding preserves summary text without LLM calls; reparse regenerates it. Real summary quality and expenditure require the separate opt-in M11 quality gate, not replay evidence.

## Verification

Use the lightest verification that matches the local setup:

- Confirm explicit `make setup-db` completed against an empty target, then confirm `make check-db` completed successfully.
- Confirm `make install` completed successfully.
- Confirm `config.yaml` exists.
- If Docker Sandbox was selected, confirm `make setup-sandbox` completed; do not represent that as application startup.
- Do not leave background services running unless the user asked for that.

## Final Response Format

Return a short status report with:

1. Local setup level reached
2. Optional Sandbox runtime prepared, if any
3. Files created or detected: for example `config.yaml`
4. Remaining user action: optional database-backed Model Provider API-Key changes, process/tool env values, auth files, or nothing
5. Exact next command to start ActWeave
6. PostgreSQL host/database and schema marker from redacted check output; never include the URL, username, or password

## EXECUTE NOW

Complete the steps above. When finished, stop after the setup boundary and report status instead of continuing into unrelated project work.
