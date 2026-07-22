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
make check-db
make doctor
make dev
make start
make stop
make support-bundle
```

The required fresh-install order is:

```bash
# DATABASE_URL names a new empty target; POSTGRES_ADMIN_URL names its maintenance DB.
make setup-db
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

## Fresh PostgreSQL baseline

The application schema has one static Alembic revision:

```text
packages/harness/deerflow/persistence/migrations/versions/
└── 0001_project_saas_baseline.py
```

`make setup-db` requires an explicit administrator URL and an explicit application URL. It
creates the named target if needed, requires that target to be empty, installs
`0001_project_saas_baseline`, seeds the packaged system asset catalog in one transaction,
initializes the LangGraph checkpointer/store schema, and bootstraps the default project.
The application role must be an ordinary non-superuser. Runtime startup only validates the
target and never creates or repairs it.

An old revision, an unknown nonempty schema, or extra/missing root objects fails before DDL
with `M7_RECREATE_REQUIRED`. The operator must retain the old database if needed, create a
new empty database, update `DATABASE_URL`, and rerun `make setup-db`. Downgrade, manual stamp,
in-place conversion, and automatic deletion are unsupported.

`make check-db` is read-only. It verifies the exact M7 revision, required application and
LangGraph relations, and final readiness without printing credentials or full connection
URLs.

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

Runtime processes use PostgreSQL as the only catalog authority. Gateway admission persists
the exact secret-free Agent/Skill/MCP and Credential-grant snapshot for a Run. Worker reloads
that exact snapshot, materializes Skill bytes in a run-owned read-only tree, and materializes
MCP secrets only after current capability, catalog generation, binding, slot, grant,
Credential, and envelope closure are locked and revalidated. Secret material never enters an
API response, cache, checkpoint, event, repr, log, or audit metadata.

`RunAgentPrivateExecutor` is the only production adapter that calls `run_agent()`. The Worker
holds the raw lease token only in memory; PostgreSQL stores its hash. Durable stream appends
validate the exact current lease in the same transaction, use thread-monotonic sequence IDs,
and persist one terminal outcome. Gateway only reads scoped durable frames and honors
`Last-Event-ID` after restart.

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

M1–M8 已完成，总体进度为 8/8（100%）。M8 宿主机发布验收的后端 full gate 为 6867 passed、
0 failed、940 个由专用 live/PostgreSQL 阶段覆盖的 expected skip；固定 M1–M8 PostgreSQL gate 为
326 passed、0 skipped。认证范围及未认证部署方式见根 `AGENTS.md` 和 operator runbook。
`M8_RELEASE_POSTGRES_TESTS` 保留该 20 文件前缀并只追加三个 M8 PostgreSQL 文件；最终
0-skip gate 是根目录 `make test-project-saas-postgres`。完整宿主机验收使用随机自有
`deerflow_test_*` 数据库并通过根目录 `make release-acceptance` 执行。
