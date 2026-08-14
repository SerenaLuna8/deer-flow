# Backend AGENTS.md

This is the source of truth for backend changes. The repository-level
[AGENTS.md](../AGENTS.md) owns monorepo orientation; this guide keeps only
backend architecture, security boundaries, common change paths, and tests.
Detailed operator and feature behavior belongs in [docs/](docs/README.md),
code, and focused tests rather than in this file.

## Runtime topology

- **Gateway** (`app.gateway`, port `8001`) owns authentication, project/admin
  APIs, Run admission, scoped reads, durable SSE replay, and inbound adapters.
  It never executes Agent graphs.
- **Worker** (`app.worker`) is the only process that claims executable Jobs and
  invokes `RunAgentPrivateExecutor` / `run_agent()`. It owns leases, graph
  execution, stream publication, and terminal settlement.
- **Scheduler** (`app.scheduler`) is optional. It owns one PostgreSQL advisory
  lock, discovers due Automations, and admits occurrence/Run/Job rows. It never
  imports or invokes graph execution.
- **Provisioner** is optional and participates only for the configured
  Kubernetes Sandbox provider.

Gateway, Worker, and Scheduler share the same PostgreSQL schema and governed
configuration. Public readiness must expose component state only—never PIDs,
lock keys, credentials, URLs with secrets, private identifiers, or content.

## Commands and layout

Run full-stack commands from the repository root. Run backend-only commands
from `backend/`:

```bash
make install
make gateway
make worker
make scheduler
make test
make lint
make format
make setup-db
make upgrade-db
make check-db
make upgrade-system-assets
make prepare-run-event-partitions
make detect-blocking-io
```

```text
backend/
├── app/
│   ├── gateway/              # HTTP composition and server-issued contexts
│   ├── worker/               # graph and durable Job execution
│   ├── scheduler/            # Automation polling and admission
│   ├── projects/             # project governance and ProjectContext
│   ├── private_work/         # owner-private Thread/Run/file/Memory services
│   ├── shared_assets/        # Agent/Skill/MCP/Credential governance
│   ├── automations/          # Automation definitions and occurrences
│   ├── channels/             # inbound/outbound channel adapters
│   ├── quotas/               # transactional quota enforcement
│   └── audit/                # typed append-only audit events
├── packages/harness/deerflow/
│   ├── agents/               # LangGraph graph and middleware
│   ├── persistence/          # ORM, repositories, schema, bootstrap
│   ├── runtime/              # execution, streams, checkpoints, Jobs
│   ├── sandbox/              # Sandbox providers and file/shell tools
│   ├── skills/               # Skill parsing, scan, review, materialization
│   ├── mcp/                  # admitted MCP discovery and calls
│   └── subagents/            # delegated Agent execution
├── migrations/               # explicit Alembic chain for existing databases
├── scripts/                  # database and operator CLIs
└── tests/                    # unit, PostgreSQL, process, and contract gates
```

The dependency direction is `app.* -> deerflow.*`; harness code must never
import `app.*`.

## Non-negotiable boundaries

### Authorization and transactions

- Authority starts from authenticated identity plus a server-issued immutable
  `ProjectContext`. `system_admin` is not project membership.
- Owner-private work uses a server-issued `PrivateWorkContext`. Every private
  query and composite relation binds `project_id + owner_user_id`.
- Never accept project, owner, membership, capability, Run snapshot, Credential
  grant, Job, lease, or internal runtime authority from request metadata,
  model/tool arguments, or ambient globals.
- Revalidate project, membership, capability, resource state, and Job/Run lease
  inside the transaction that performs each side effect.
- Project outsiders, wrong owners, stale membership, and missing private
  resources collapse to `404`; a current member lacking a capability receives
  `403`, except where an existing route family deliberately hides both.
- Repositories do not commit unless they explicitly own the complete operation.
  Preserve the established Project -> Membership -> resource lock order.
- PostgreSQL RLS is not used. Isolation therefore depends on immutable contexts,
  scoped repositories, composite constraints, and a non-superuser app role.
- Public request/response models reject unknown authority fields. Copy the error
  and strictness convention of the neighboring route family.

### Authentication and secrets

- Account email is normalized with `strip + lowercase` on every path and is
  protected by the database's case-insensitive unique index. Login also accepts
  a required username: 3–32 characters, starting with a letter, `[a-z0-9_]`
  only, stored lowercase, unique among human accounts.
- Browser tokens remain valid only while signature, session ID, token version,
  and the durable auth-session row all validate. Logout and password change
  revoke durable authority, not just browser state.
- Passwords, JWTs, CSRF values, raw session IDs, API keys, Credential plaintext,
  nonces, ciphertext, storage locators, and full connection URLs never enter
  logs, traces, public responses, snapshots, audit metadata, or browser caches.
- Credential plaintext is decrypted only at the Worker execution boundary and
  injected into the exact authorized subprocess or remote call. Output masking
  is accidental-leak protection, not DLP; Credential-bearing Skills are trusted
  code.

### PostgreSQL schema and persistence

`deerflow/persistence/full_schema.sql` is the complete source for fresh installs;
the current marker is `full_schema`. Fresh setup runs that schema directly and
stamps the chain head. Runtime processes never create, migrate, stamp, repair, or
downgrade an application database.

- `make setup-db` accepts a new empty target only and initializes application,
  LangGraph, system-asset, and default-project state.
- Every initialized application/Alembic table and column has a checked-in
  Chinese comment. LangGraph comments are applied after its third-party setup,
  and each physical `run_events` partition copies the parent comments. Run
  `uv run python scripts/generate_schema_comments.py --check` after schema edits.
- `make upgrade-db` is the sole path for a database at a known ancestor marker;
  back up first. It runs the migration chain and verifies parity with a fresh
  install.
- `make check-db` is read-only and reports ready, upgrade-required, or a
  fail-closed recreate/unavailable state without exposing credentials.
- `make upgrade-system-assets` is an explicit maintenance-window action for a
  current schema. It preserves immutable history and existing project pins,
  and may be rerun idempotently after checking an uncertain outcome.
- `make prepare-run-event-partitions` is an explicit, idempotent operator action
  for UTC months N through N+2. Schedule it outside application runtime; it
  refuses non-current schemas and bounds DDL lock waiting.
- Unknown markers, an unversioned nonempty schema, and catalog drift are never
  repaired in place.
- A schema change updates the ORM registration, `full_schema.sql`, catalog
  signature/digest, required relations, chain marker, migration script, and
  parity tests together.
- Application metadata and durable state live in PostgreSQL. File/artifact bytes
  may live in configured storage, but access, identity, version, and scope remain
  database-authoritative.

### Jobs, Runs, streams, and checkpoints

- Gateway admits executable work; Worker executes it. Business state, Job,
  quota reservation, snapshot, and audit rows must be committed atomically at
  admission.
- Worker stores raw lease tokens only in memory. Every append, tool side effect,
  and terminal settlement validates the exact current lease in its transaction.
- Run admission freezes an exact, secret-free Agent/model/Skill/MCP/Credential
  reference closure. Retry and resume use that closure rather than current
  catalog pointers.
- Durable events are committed before notification. PostgreSQL `NOTIFY` is only
  a wake-up hint; correctness comes from scoped reads, monotonic cursors, and one
  durable terminal outcome.
- Public event and message sequence values remain canonical signed-BIGINT decimal
  strings at browser boundaries; never coerce them to JavaScript numbers.
- Checkpoint mode is process-frozen. All Gateway and Worker processes sharing a
  database must use the same full/delta settings and restart together. Consumers
  materialize state through the scoped checkpointer; raw delta channels are not
  a complete-state API.
- Current-message files and images are admitted from server file authority.
  Worker retry revalidates the frozen metadata; missing or changed attachments
  fail closed rather than degrading silently.

### Assets, Skills, MCP, and Agents

- `skills/public/*` is the sole source for packaged System Skills. Regenerate the
  manifest/archives with
  `PYTHONPATH=. uv run python scripts/generate_public_system_skill_catalog.py`
  and use `--check` for verification.
- Packaged System Agent/Skill/MCP definitions are bootstrap-only and immutable at
  runtime. Global admin definition routes are read-only; the narrow packaged MCP
  Credential-grant route changes grants, not the definition.
- A packaged Skill's catalog scan snapshot is release-time immutable metadata.
  Bootstrap scans the latest release and legacy releases without snapshots;
  historical snapshotted releases keep their authenticated result. Retrospective
  denial uses the explicit, irreversible System Skill version-revocation path,
  never reinterpretation during bootstrap. Revocation preserves published bytes,
  history, the current pointer, asset release revision, and existing project pins;
  new bindings, Run admission, retry/resume materialization, and Worker file writes
  reject the revoked release. Existing pins must be explicitly moved to an eligible
  release or disabled; an already materialized running graph is not force-aborted.
- Project Agent/Skill/MCP versions are immutable. Agent creation is one atomic
  complete package (`suspended` plus draft v1); author edits and restores create
  a new draft without moving the live pointer, while a binding manager explicitly
  publishes the selected draft and controls activation. No flow mutates history
  or moves a pointer backward to an old row.
- A newly admitted Run pins exact versions and checksums. Unrelated catalog
  changes do not invalidate it, while revoked membership, capability, binding,
  Credential, or lease fails at the applicable execution boundary.
- A project Skill is passive until explicit slash activation or verified reading
  of its admitted `SKILL.md`. Active policy restricts model schemas, execution,
  and `tool_search`; stale or malformed evidence fails closed.
- Project MCP authoring accepts only supported remote HTTP/SSE definitions under
  the configured CIDR policy. Secrets use encrypted header/query Credential
  slots. Worker disables redirects and ambient proxy trust, revalidates the
  target and closure for discovery/calls, and treats inventory as diagnostic—not
  execution authority.
- System MCP may retain packaged stdio, header, and OAuth capabilities; do not
  copy that broader trust model into project-authored MCP.
- Project Agent logical `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` are
  immutable version fields, not files in this repository.
- Agent payload checksums are recomputed at resolution and Worker materialization.
  Legacy schema v1 fields outside its historical digest must remain empty.

### Memory, audit, quota, and retention

- PostgreSQL is the only project Memory authority. Every document, history row,
  episode, Dream, and Run snapshot remains bound to project, owner, and namespace.
- Compaction creates continuity plus tagged Memory input; only durable checkpoint
  persistence activates the corresponding history receipt. Dream and idle seal
  reuse the same scoped admission and settlement boundaries.
- Model work runs outside database transactions. Admission freezes its inputs;
  settlement re-locks and rejects stale policy, model, preference, document, Job,
  or lease state before publishing results.
- Memory is injected as low-authority user-private context, never as a System
  instruction. Disabled or over-budget Memory must not widen authority.
- Quota reserve/consume/release belongs in the authoritative business
  transaction. Tightening a limit does not interrupt already admitted work.
- Audit has closed action/actor/target/outcome contracts. Private identifiers are
  domain-separated HMACs; content, errors, secrets, and storage locations are
  forbidden. Audit and committed usage ledgers are append-only.
- Retention deletes exact project/owner dependencies in documented order; it
  never broadens scope or cascades through retained governance references.

### Configuration

Configuration is read only from explicit `DEER_FLOW_CONFIG_PATH` or the
repository-root `config.yaml`. Infrastructure settings are restart-required.
Model definitions, runtime/auth/Memory/quota/Automation policy, and provider Credentials are
PostgreSQL authority and must not be reintroduced as YAML or ambient-key
fallbacks.

The example declares `config_version: 38`. Version 38 replaces the YAML `scheduler:`
section with PostgreSQL automations policy. Removed top-level
policy keys remain fail-closed tombstones; use `make config-upgrade` rather than
manually guessing a migration.

`make setup-db` is the only command allowed to consume initial provider keys and
persist encrypted Credential versions. Normal Gateway, Worker, Scheduler,
doctor, and Compose startup must not broadcast provider keys as process-wide
model configuration.

## Common change paths

### Gateway endpoint or domain service

1. Add strict request/response models and a route in the owning router family.
2. Resolve authority through server dependencies or transaction-bound project
   context; never reconstruct it from fields.
3. Put locking, revalidation, writes, quota, audit, and Job admission in the
   owning domain transaction.
4. Register the router once in `app/gateway/app.py`.
5. Add focused tests for success, outsider/wrong-owner, missing capability,
   stale revision, and rollback behavior.

### PostgreSQL table or durable Job type

1. Add/import the ORM model so `Base.metadata` registers it.
2. Update `full_schema.sql` and all database constraints.
3. Add an explicit migration with the previous head as `down_revision`.
4. Update schema marker, catalog signature/digest, readiness relations, and the
   closed Job/audit/API type contracts that apply.
5. Prove fresh-install and migration parity with disposable PostgreSQL targets.
   Never stamp or hand-patch a development database as evidence.

### Agent tool or middleware

- Config-driven tools need a callable plus an exact `tools:` entry whose name and
  group match runtime registration. Builtins are registered in the owning tool
  registry. New write, shell, or network tools must join the applicable read-
  before-write, audit, guardrail, and loop-detection policies.
- Register middleware in exactly one lead/base/subagent/SDK builder. List order
  is nesting order; preserve the assertions around clarification, progress,
  guardrail, error handling, MCP routing, and deferred filtering.
- A new state channel needs an explicit schema and delta-compatible
  materialization behavior.

### System Skill or project asset flow

- Validate all archive paths, sizes, file types, frontmatter, and static scan
  results before persistence or publication.
- Keep packaged System assets separate from project-authored assets and bindings.
- Authoring services use optimistic revisions and one transaction; failed flows
  must not leave an asset without its initial version or publish a partial
  dependency closure.
- Run deterministic review for each changed public Skill:
  `uv run python -m deerflow.skills.review.cli ../skills/public/<slug> --format text --fail-on error --fail-on-incomplete`.

## Guarded operational limits

The focused constants test keeps these values synchronized with code. If a
source constant changes, update this section and the owning detailed document.

- Subagent concurrency is canonically clamped to `1..4`; the per-Run total remains independently bounded to `1..50`.
- SkillScan rejects unsafe archives while retaining the final 100 MiB and the
  16384-file, bounded-log contract.
- Project Skill archives remain limited to 100 MiB and 16384 regular files; the
  archive-create routes have a scoped 160 MiB wire limit.
- Current-message vision injects at most four unique images with a 20 MiB per-image limit.
- Verified Skill reads remain active for `skills.read_evidence_ttl_calls` subsequent lead model calls (default 12).
- SNIP free-prose task continuity bounded to 2,000 characters and tagged fact lines bounded to 1,000 characters; the packaged prompt raises a declared output cap below 4,096 tokens.
- `worker.stream.text_delta_flush_ms`, default 75ms, controls text coalescing.
  `worker.stream.run_event_notify_enabled` is true (the default).
- `mcp_security.run_session_reuse` (default `true`) reuses an exact-closure MCP
  session only within one Run.
- Memory `dream_interval_minutes` (`15..1440`, default `120`),
  `idle_seal_minutes` (`0` or `30..10080`, default `1440`), and
  `episode_retention_days` (`0` or `30..3650`, default `365`) remain governed
  system settings.
- Dream selects the strictly oldest 20 pending history rows. A scope is due when a full batch of 20 is already pending or a tool row has been pending for over 10 minutes.
- Recall audit has a per-Run audit cap of 5; `remember` has a per-Run cap of 5
  and a pending-backlog cap of 200.
- Review flags consider a document with at least 8 content lines when over 40% of them vanished without correction evidence.
- Platform quota defaults are 20 members, 5 GiB storage, 3 concurrent Runs, and 10,000 MCP calls per UTC day; project limits may only tighten them.

## Tests and code quality

Backend changes follow strict TDD: add a focused failing test, observe the
expected failure, implement the final-path change, rerun focused/affected tests,
then run the relevant local gates.

```bash
uv run pytest tests/test_<feature>.py -q
make test
uvx ruff format --check .
uvx ruff check .
make detect-blocking-io
```

`make test` requires a non-production development `DATABASE_URL`, derives a
maintenance connection, and creates/drops random `deerflow_test_*` databases.
It must never run test DDL in the named development database and must complete
the core suite with zero skips. Focused offline tests do not certify PostgreSQL,
network providers, or live target environments.

Python is 3.12+. Keep async production paths free of blocking filesystem or
subprocess work, settle cancellation, use precise types, and keep public errors
stable and free of SQL, credentials, private identifiers, and raw exceptions.
