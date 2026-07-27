# AGENTS.md

This is the source of truth for backend work. The repository-level
[AGENTS.md](../AGENTS.md) owns monorepo orientation; this guide owns the final M7 backend
runtime, persistence, authorization, and test boundaries.

## Final service topology

- **Gateway** (`app.gateway`, port 8001) owns authentication, account/project/admin REST
  APIs, project-private Run admission and queries, durable SSE replay, and signed inbound
  channel/webhook adapters. It does not execute Agent graphs.
- **Worker** (`app.worker`) is the only process that claims executable jobs and invokes
  `RunAgentPrivateExecutor`/`run_agent()`. It owns graph execution, lease heartbeats,
  project/owner revalidation, checkpoint access, durable stream publication, and terminal
  settlement.
- **Scheduler** (`app.scheduler`) is an independent optional process. It holds and verifies
  one PostgreSQL session advisory lock, scans due project Automations, and atomically admits
  occurrence, Run, snapshot, and job rows. It never imports or invokes graph execution.
- **Nginx** forwards `/api/*` directly to Gateway. The frontend is served separately on port
  3000 and the combined public entry is port 2026.
- **Provisioner** is optional and only participates when the configured sandbox provider
  requires it.

Gateway, Worker, and Scheduler use one final PostgreSQL schema and the same retained audit
keyring. Process readiness reports public component state only; it never reports PIDs, lock
keys, database URLs, credentials, private resource IDs, or content.

## Commands

Run full-stack commands from the repository root:

```bash
make check
make install
make config
make setup-db
make migrate-db
make check-db
make doctor
make dev
make start
make stop
make support-bundle
```

The new-install order is:

```bash
# DATABASE_URL names a new empty target; POSTGRES_ADMIN_URL names its maintenance DB.
make setup-db
make check-db
make start
```

After future revisions are added, for an existing database at a supported ancestor revision:

```bash
# DATABASE_URL names the existing target; no administrator URL is required.
make migrate-db
make check-db
make start
```

Run backend-only commands from `backend/`:

```bash
make gateway
make worker
make scheduler
make test
make test-blocking-io
make lint
make format
make check-db
```

`scheduler.enabled=false` leaves project Automation APIs and manual triggers available but
does not acquire the Scheduler ownership lock or start polling.

## Forward-only PostgreSQL migrations

`0001_project_saas_baseline.py` is the immutable merged baseline.
`0002_project_skill_hard_delete.py` adds the controlled project-Skill package deletion boundary,
and `0003_project_skill_unique_name.py` is the current linear head that makes project Skill
display names case-insensitively unique within each project. Every later schema change adds
another linear Alembic revision; never edit, squash, or restamp an existing revision.
When upgrading an exact `0001` or `0002` ancestor, `0003` preserves every Skill row, keeps the
earliest `(created_at, id)` display name in each project-local case-insensitive duplicate group,
and deterministically suffixes later names without changing their slug, ID, or asset version.

`make setup-db` requires an explicit administrator URL and application URL. It creates the
named empty target if needed, applies the complete committed migration chain through head,
seeds the packaged system asset catalog, initializes the LangGraph checkpointer/store schema,
and bootstraps the default project. The application role must be an ordinary non-superuser.

`make migrate-db` uses only `DATABASE_URL` and upgrades an existing database from a verified,
known ancestor revision by applying its pending committed migrations through head. It never
creates the database and never seeds the catalog, initializes LangGraph, or bootstraps a default
project. Runtime startup only performs read-only validation: an ancestor revision reports
migration required, while an unknown revision, unversioned nonempty schema, or catalog drift
fails closed without DDL or repair.

`make check-db` is also read-only. It reports current/head revision, required application and
LangGraph relations, and whether setup, migration, or operator intervention is required without
printing credentials or full connection URLs. Downgrade, manual stamp, automatic repair,
automatic deletion, and destructive reset are unsupported.

### Release PostgreSQL gate

The root `Makefile` variable `PROJECT_FOUNDATION_POSTGRES_TESTS` is the sole ordered
20-file M1-M7 PostgreSQL release list. Run it only against a disposable maintenance instance:

```bash
POSTGRES_TEST_URL="postgresql://.../postgres" make test-project-foundation-postgres
```

The URL must have create/drop/terminate authority for random `deerflow_test_*`
databases. It must never be a production URL or the ordinary application
URL. Missing `POSTGRES_TEST_URL` fails before pytest collection; selected tests must report
zero skips.

## Repository layout

```text
backend/
├── app/
│   ├── gateway/              # project/account/admin API composition
│   ├── worker/               # Worker process and job handlers
│   ├── scheduler/            # Scheduler ownership and polling
│   ├── projects/             # ProjectContext and project governance
│   ├── private_work/         # project+owner Thread/Run/file/artifact/Memory services
│   ├── shared_assets/        # Agent/Skill/MCP/Credential catalog and bootstrap
│   ├── automations/          # project Automation domain
│   ├── jobs/                 # durable job application ports
│   ├── quotas/               # transactional project quota enforcement
│   ├── audit/                # typed append-only audit
│   └── channels/             # final project-bound inbound adapters
├── packages/harness/deerflow/
│   ├── agents/               # LangGraph graph and middleware
│   ├── assets/               # app-independent catalog protocol
│   ├── persistence/          # ORM, repositories, baseline, bootstrap
│   ├── runtime/              # graph execution primitives
│   ├── sandbox/              # sandbox providers and file/shell tools
│   ├── mcp/                  # admitted MCP tool materialization
│   ├── skills/               # immutable admitted Skill parsing/loading
│   └── subagents/            # delegated Agent execution
├── scripts/                  # setup/check/operator CLIs
└── tests/                    # unit, PostgreSQL, process, blocking-I/O gates
```

The dependency direction is `app.* -> deerflow.*`. Harness code must never import
`app.*`; `tests/test_harness_boundary.py` enforces this.

## Authorization and persistence boundaries

- Project authority begins with authenticated identity and a server-issued immutable
  `ProjectContext`. Platform `system_admin` does not imply project membership.
- Private authority derives a `PrivateWorkContext`; it cannot be constructed from request
  fields. Every private SQL predicate and composite relationship binds
  `project_id + owner_user_id`.
- Request metadata/config recursively discards client-supplied owner, project, membership,
  role, capability, snapshot, Credential grant, job, lease, and internal runtime fields.
- Every side-effect boundary re-locks and revalidates current project, membership, role,
  capability, Run, and job lease in the caller-owned transaction.
- Project outsider, stale membership, wrong owner, and missing private resource collapse to
  public 404. A current project member lacking a required capability receives 403.
- Repositories never commit unless their contract explicitly owns the whole operation.
  Domain services preserve the documented Project -> Membership -> resource lock order.
- The application does not use PostgreSQL RLS. Application isolation therefore depends on
  immutable contexts, scoped repositories, composite constraints, and non-superuser runtime
  roles.

## System asset and Agent execution boundary

`app.shared_assets.bootstrap` loads a strict packaged manifest and manifest-listed regular
files only. It rejects unknown keys, duplicate source keys, path escape, symlinks, non-regular
files, and digest mismatch. One transaction writes published system Agent, Skill, and MCP
rows under the fixed non-login builtin principal. Repeated setup with the same catalog is
idempotent; a conflict rolls back the whole seed. The seed never creates a Credential,
project binding, membership, or secret.

The 21 directories under `../skills/public/` are maintained as complete multi-file system
Skill archives, including `SKILL.md`, scripts, references, templates, and other regular
files. Regenerate the checked-in archives and manifest entries from `backend/` with
`PYTHONPATH=. uv run python scripts/generate_public_system_skill_catalog.py`; use `--check`
to verify that they are current. Generation and bootstrap use the same bounded frontmatter,
archive, and static-scan validation; generated destinations reject symlinks and are replaced
atomically. Runtime processes never scan `skills/public/`, and setup does not create project
bindings for these assets.

The runtime system catalog is bootstrap-only. Global `/api/admin/assets` Agent, Skill, and
MCP definition/version routes expose governance metadata with GET only; Gateway and domain
services reject runtime create, version authoring, publish, submit/approve, archive, and
suspend even for a system admin. The one MCP-specific write is the dedicated
`.../versions/{version_id}/credential-grants` route for the current published packaged MCP:
it replaces only active System Credential grants with optimistic grant revisions and never
changes the definition, workflow state, checksum, published pointer, or asset revision.
System Credential lifecycle routes and project-scoped admin overrides remain independently
mutable.

Runtime processes use PostgreSQL as the only catalog authority. Gateway admission persists
the exact secret-free Agent/Skill/MCP and Credential-grant snapshot for a Run. Worker reloads
that exact snapshot and materializes system Skill bytes below `/mnt/skills/public/<name>` and
project Skill bytes below `/mnt/skills/custom/<asset_uuid>` in a run-owned read-only tree.
New project Skills are created in `suspended` state. Authors may create, fork, replace, and
publish versions while suspended; activation is a separate capability-checked transition that
requires a published version. Resolution and runtime materialization accept only active,
published Skills. Project Skill display names are case-insensitively unique within one project;
different projects may use the same display name.
Agent version authoring rejects a dependency set containing duplicate Skill slugs across
system and project scope before that layout can become ambiguous. Worker materializes MCP
secrets only after current capability, exact selected version/checksum,
binding, slot, grant, Credential, and envelope closure are locked and revalidated. Catalog
generation is diagnostic snapshot metadata, not a global invalidation token: an unrelated
project mutation must not stale an exact admitted Run. Secret material never enters an API
response, cache, checkpoint, event, repr, log, or audit metadata.

Project MCP execution is fail-closed. Project and admin-project authoring accept only
`http`/`sse` definitions whose complete HTTPS URL appears in the operator-owned
`mcp_security.project_remote_allowed_endpoints`; project `stdio`, `streamable_http`, OAuth,
literal env/header values, and env/OAuth Credential targets are rejected. Project secrets may
enter a remote MCP call only through an approved encrypted Credential header slot; authority,
proxy-authentication, content-length, and other hop-by-hop header names are forbidden. Run
admission takes shared MCP/version locks, rejects historical project `stdio`, and revalidates
the current definition and Credential-slot closure even when a caller omitted the endpoint
policy (which fails closed). Worker revalidates every exact project MCP snapshot before
discovery and every call. Worker injects a no-redirect, `trust_env=false` HTTP client, uses the
configured controlled egress proxy, and applies separate operator hard timeouts to discovery
and tool calls.
`mcp_security` is startup-only: update Gateway, Scheduler, and every Worker together. Historical
immutable versions remain readable with env/header values redacted and Project remote URLs
reduced to their HTTPS origin; path/query details are never replayed. Unsafe versions are never
rewritten or silently skipped. Gateway and Scheduler import the neutral
`deerflow.mcp_definition_policy` module so policy validation cannot pull the Agent execution
graph into either process.
Packaged System MCP keeps the supported `stdio` env and remote header/OAuth Credential paths.
For OAuth, Worker derives the access header only inside the one-shot call, bounds acquisition
with the discovery deadline, refreshes through a local interceptor, and rejects tool metadata
or results that echo either the admitted secret or the derived access token.

`RunAgentPrivateExecutor` is the only production adapter that calls `run_agent()`. The Worker
holds the raw lease token only in memory; PostgreSQL stores its hash. Durable stream appends
validate the exact current lease in the same transaction, use thread-monotonic sequence IDs,
and persist one terminal outcome. Gateway only reads scoped durable frames and honors
`Last-Event-ID` after restart.

The durable top-level `message` journal is a lead-Agent conversation projection. Lead AI
messages and their exact `tool_call_id` results belong there; subagent and middleware AI/tool
callbacks are persisted as `trace` events and continue to use the dedicated bounded subagent
event stream. Caller attribution first follows the exact callback run ID from tool start, then
falls back to the issuing AI tool call and callback tags, so provider-local call-ID reuse cannot
leak nested reasoning or tool output into the user conversation. Run-level message counts and
last-answer summaries are lead-only.

For a private Run, Worker also installs only the exact admitted MCP proxy objects in internal
runtime context. Delegated Agents disable global MCP/ACP discovery and marshal each proxy call
back to the owner Worker loop, so the same authorization, grant, Credential, endpoint, and
side-effect checks run for every delegated invocation.

## Project APIs

All private and governance APIs are project-scoped:

- `/api/projects/{project_id}/private-work` for Threads, Runs, durable SSE, feedback,
  uploads/files, artifacts, token usage, and input polish;
- `/api/projects/{project_id}/memory` for project-owner Memory;
- `/api/projects/{project_id}/connections` for project-bound providers and connections;
- `/api/projects/{project_id}/automations` for definitions, manual admission, history, and
  readiness;
- `/api/projects/{project_id}/agents`, `skills`, `mcp-servers`, and `credentials` for visible
  project/system assets and project bindings;
- `/api/projects/{project_id}/usage` and `/api/projects/{project_id}/audit` for project
  governance;
- `/api/admin/assets` and `/api/admin/operations` for authenticated system administration.

Project input polish requires both `private_work.create` and `shared_assets.execute`, locks the
Thread, and validates the exact Agent/Credential closure before the auxiliary model call.
Channel inbound must resolve one connected PostgreSQL row carrying exact
account/project/owner/connection authority before it can create a private Thread, Run, or job.

## Quota, audit, and retention

Quota reservation/consume/release occurs in the authoritative business transaction. Platform
defaults are 20 members, 5 GiB storage, 3 concurrent Runs, and 10,000 MCP calls per UTC day;
project Admins may only tighten them. Hard-limit HTTP responses use stable 429 plus
`Retry-After: 1`. Already admitted work is not interrupted by a later policy tightening.
Every immutable project Skill version reserves the sum of all stored version-file bytes in
`storage_bytes`; system Skill versions are excluded. The reservation rolls back with failed
authoring, reconciliation sums ready private files plus every project `SkillVersionFileRow`,
and either project-Skill package deletion or project retention releases each exact Skill-version
reservation before physical deletion. Project Skill deletion is unavailable for system assets
and fails closed while an immutable Agent dependency or admitted Run snapshot still references
the package.
Gateway authoring and the explicit project-Skill import CLI use the same `AppConfig.quotas`
defaults. Archive-creation requests have a scoped 160 MiB wire limit at both the ASGI receive
boundary and each Nginx entry point, before JSON/Pydantic or multipart route processing.
Direct version authoring retains its JSON/base64 contract; project creation also accepts one
multipart `.zip`, `.skill` (ZIP), `.tar`, `.tar.gz`, or `.tgz` package, strips one common wrapper
directory, and atomically creates the suspended asset plus its first published version. Decoded
archives remain limited to 100 MiB and 16384 regular files; traversal and non-regular archive
members are rejected. TAR limits cover the complete decompressed byte stream, including
headers and PAX/GNU extended metadata, rather than only regular-file bodies.

Audit uses closed action/actor/target/outcome contracts and action-specific strict metadata.
Private target identifiers are domain-separated HMACs; raw project-owner resources, prompt,
messages, files, Memory, Run output, exception text, URLs, and secrets are forbidden. Audit
rows and committed usage ledger rows are append-only.

Retention purge remains a project-governance operation and validates the current pending-deletion
authority in the same transaction before physically deleting project data.

## Configuration

Configuration is read only from an explicit `DEER_FLOW_CONFIG_PATH` or the repository-root
`config.yaml`. `database`, `worker`, `scheduler`, `quotas`, `channels`, sandbox,
models, tools, logging, and final runtime policy remain supported. Infrastructure configuration
is restart-required. Unknown application extension fields remain allowed where their typed
models permit them, but removed top-level keys fail validation instead of being ignored.

Secret values must come from environment-backed configuration and must be separated by domain:
Auth, Credential encryption, audit/quota HMAC, and database passwords cannot reuse material.

## Testing and code quality

Backend changes use strict TDD:

1. Add a focused failing test under `tests/`.
2. Run it and confirm the expected failure.
3. Implement the smallest final-path change.
4. Rerun focused and affected tests.
5. Before completion run the full relevant release gates.

Common commands:

```bash
uv run pytest tests/test_<feature>.py -q
uv run pytest -q
uv run pytest tests/blocking_io -q
uvx ruff format --check .
uvx ruff check .
```

`tests/blocking_io/` runs business code under strict Blockbuster detection. Production async
paths must offload synchronous filesystem/subprocess work and await cancellation-settled cleanup.
Test doubles belong under `tests/support/`; do not add a production persistence or execution
fallback to make a test easier.

Python is 3.12+, Ruff uses double quotes and a 240-character line limit, and all public/domain
interfaces should use precise types. Keep public errors stable and free of SQL, connection,
credential, private resource, and exception detail.

Historical pass counts do not certify the current checkout. `M8_RELEASE_POSTGRES_TESTS` keeps the
20-file foundation prefix and appends three M8 PostgreSQL files; the current zero-skip gate is
`make test-project-saas-postgres` from the repository root. Full host acceptance uses owned random
`deerflow_test_*` databases and runs through `make release-acceptance`.
