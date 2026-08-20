# Backend AGENTS.md

This guide owns backend process boundaries, authorization, persistence,
governed assets, change paths, operational limits, and verification. Read the
repository-level [AGENTS.md](../AGENTS.md) for cross-cutting rules and
[README.md](../README.md) plus [Install.md](../Install.md) for setup and
operator workflows. Feature behavior remains authoritative in code and focused
tests.

## Guide map

| Change area                      | Read first                                  |
| -------------------------------- | ------------------------------------------- |
| HTTP, domains, authorization     | Authorization and transactions              |
| Schema or durable state          | PostgreSQL schema and persistence           |
| Jobs, Runs, streams, files       | Jobs, Runs, streams, checkpoints, and files |
| Agents, Skills, MCP, Credentials | Governed assets                             |
| Memory, audit, quota, retention  | Memory, audit, quota, and retention         |
| Configuration, models, vision    | Configuration, models, and `inspect_image`  |
| Implementation or verification   | Common change paths; Tests and code quality |

## Process and dependency boundaries

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

## Where changes live

Run full-stack commands from the repository root and backend targets from
`backend/`; use the applicable `Makefile` as the current command index.

- `app/<domain>/` owns application composition, HTTP admission, server-issued
  contexts, and domain transactions.
- `packages/harness/deerflow/<domain>/` owns reusable graph, runtime,
  persistence, sandbox, Skill, MCP, and subagent primitives.
- `migrations/` and `scripts/` own explicit schema and operator workflows.
- `tests/` owns unit, PostgreSQL, process, and contract gates.

The dependency direction is `app.* -> deerflow.*`; harness code must never
import `app.*`.

## Non-negotiable boundaries

### Authorization and transactions

- Authority starts from authenticated identity plus a server-issued immutable
  `ProjectContext`. `system_admin` is not project membership.
- Owner-private work uses a server-issued `PrivateWorkContext`. Every private
  query and composite relation binds `project_id + owner_user_id`.
- Project channel configuration, group bindings, and owner-attributed channel
  connection state require `project.channels.manage`; member private-work
  capabilities never grant access to the project Connections surface or API.
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
- Agent Builder prepare, settlement, stale recovery, and cancellation use
  Project -> Membership -> session advisory fence -> turn operation -> design
  session. Locked rereads refresh ORM identity state, and cancellation
  terminalizes active turn operations in the same transaction.
- PostgreSQL RLS is not used. Isolation therefore depends on immutable contexts,
  scoped repositories, composite constraints, and a non-superuser app role.
- Public request/response models reject unknown authority fields. Copy the error
  and strictness convention of the neighboring route family.

### Authentication, secrets, and public contracts

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
the current marker is `current_asset_version_lifecycle`. Fresh setup runs that schema directly
and stamps the chain head. Runtime processes never create, migrate, stamp, repair,
or downgrade an application database.

- `make setup-db` accepts a new empty target only and initializes application,
  LangGraph, system-asset, and default-project state.
- Every initialized application/Alembic table and column has a checked-in
  Chinese comment. LangGraph comments are applied after its third-party setup,
  and each physical `run_events` partition copies the parent comments. Run
  `uv run python scripts/generate_schema_comments.py --check` after schema edits.
- `make preflight-upgrade` is the read-only Agent/Skill lifecycle inventory for a known ancestor marker. `make upgrade-db` repeats that fail-closed preflight and is the sole mutation path.
  It runs the migration chain and verifies parity with a fresh install.
- `make check-db` is read-only and reports ready, upgrade-required, or a
  fail-closed recreate/unavailable state without exposing credentials.
- `make upgrade-system-assets` is an explicit maintenance-window action for a
  current schema. It replaces deterministic System v1 definitions in place,
  preserves already-admitted Run Snapshots and asset-ID project bindings, and
  affects only later admissions. It may be rerun idempotently after checking an
  uncertain outcome.
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

### Jobs, Runs, streams, checkpoints, and files

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
- Once a private Run has durably completed its assistant response and required
  file finalization, teardown failure is a Worker operational fault, not an
  Agent-execution failure. Keep its in-process resource-cleanup barrier and
  record the fault, but never downgrade that successful business terminal state.
- Public event and message sequence values remain canonical signed-BIGINT decimal
  strings at browser boundaries; never coerce them to JavaScript numbers.
- Checkpoint mode is process-frozen. All Gateway and Worker processes sharing a
  database must use the same full/delta settings and restart together. Consumers
  materialize state through the scoped checkpointer; raw delta channels are not
  a complete-state API.
- Current-message files and images are admitted from server file authority.
  Worker retry revalidates the frozen metadata; missing or changed attachments
  fail closed rather than degrading silently.
- Runtime-only dependency environments belong under `/tmp`. The secure
  finalization scan prunes only the exact top-level workspace `.venv` tree;
  other workspace/output symlinks and special files still fail closed.
- Conditional composer cleanup takes the same Thread lock as Run admission. It
  retains uploads present in any frozen current-upload snapshot, while admission
  rejects the whole request if any selected upload is no longer ready.

### Governed assets

#### Packaged System assets

- `skills/public/*` is the sole source for packaged System Skills. Regenerate the
  manifest/archives with
  `PYTHONPATH=. uv run python scripts/generate_public_system_skill_catalog.py`
  and use `--check` for verification.
- Packaged System Agent/Skill/MCP definitions are bootstrap-only and immutable at
  runtime. Global admin definition routes are read-only; the narrow packaged MCP
  Credential-grant route changes grants, not the definition.
- Packaged System Agent and Skill assets have one deterministic v1 identity and
  expose it through `current_version_id`. Bootstrap replaces changed authenticated
  v1 bytes in place, never appends a version, and is idempotent on rerun. Project
  bindings store only the System Agent/Skill asset identity.
- Server-owned Builder Agents are absent from every regular project, global-admin,
  and runtime System Agent catalog, including direct detail and version-history
  lookup. Only bootstrap and the dedicated internal resolver may address them.
- A packaged Skill's catalog scan snapshot is immutable for its authenticated v1
  bytes. Retrospective denial uses explicit System governance revocation. A
  same-byte bootstrap preserves revocation; changed authenticated bytes clear it.
  New bindings and Run Admission reject a revoked v1. Already admitted Runs retain
  their immutable Run Snapshot and are not force-aborted.

#### Project assets and Credentials

- Project Agent/Skill/MCP versions are immutable. Agent/Skill creation saves a
  complete suspended asset plus Candidate Version v1. Further versions may be
  authored only from the latest forward head. Activation atomically sets
  `current_version_id` and enables the asset; skipped and older versions become
  Historical Versions. No flow mutates, deletes, copies, or reactivates history,
  and content cannot be moved backward under a higher version number.
- Project Skill creation is available only through a validated archive upload
  or an AI Builder commit; there is no metadata-only or template-create API.
- Skill export is a read-only, audit-required distribution operation over one
  exact persisted version. Project exports require `shared_assets.edit` and may
  select any persisted Project version; System exports require the eligible
  Current Version. The deterministic root-layout ZIP excludes root `evals/`,
  any `node_modules/` or `__pycache__/`, `.DS_Store`, and `*.pyc`, and never
  includes Credential mappings, secrets, lifecycle state, or version history.
- A Skill's `SKILL.md` is the sole authority for `required-secrets` and
  `secrets-autonomous`. Archive, Builder, validation, review, activation, and
  runtime consumers use the canonical parser; form edits patch only those
  managed frontmatter fields and preserve the rest of the document.
- Skill Credential mappings belong to one exact Skill version and map each
  declared target environment name to one exact Project Credential version and
  one source `env` field. Candidate and Current mappings use revision CAS;
  Historical Versions are read-only. Saving a forward Candidate inherits only
  compatible mappings. Activation requires every required mapping to be valid
  and no optional mapping to be invalid. Secret values never enter Skill bytes,
  activation readiness, snapshots, audit metadata, or API responses.

#### Runtime admission and MCP

- Runtime-visible Skill names are unique case-insensitively within a project,
  across active Project Skills and enabled System Skill bindings. Activation and
  binding enable enforce the inverse checks under the project lock; Run
  resolution rejects any legacy conflicting closure.
- Every Run Admission resolves current Agent/Skill asset pointers, including later
  messages in an existing Thread, Automation, Channel, edit, regeneration, and
  fork paths. It then persists exact versions, bytes, checksums, Credentials, MCP
  configurations, and policy in one self-contained Run Snapshot. Worker execution
  and retries decode only that snapshot and never reread Current Versions.
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
- After exact closure and Credential materialization succeed, Worker isolates a
  remote MCP discovery transport/catalog failure to that MCP for the current Run,
  exposes no tools from it, and supplies only a stable secret-free capability
  notice to the Agent. Authorization, snapshot, database, and Credential
  materialization uncertainty still fail closed. Ordinary Skill/tool execution
  failures return safe error results; malformed or stale Skill evidence remains
  fail closed.
- System MCP may retain packaged stdio, header, and OAuth capabilities; do not
  copy that broader trust model into project-authored MCP.

#### Project Agents and Builder

- Project Agent logical `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` are
  immutable version fields, not files in this repository.
- Agent payload checksums are recomputed at resolution and Worker materialization.
  Legacy schema v1 fields outside its historical digest must remain empty.
- Incomplete Agent Builder pagination uses an opaque, immutable `created_at + id`
  keyset; `updated_at` is presentation ordering only. Each project/owner may
  retain at most eight incomplete sessions, enforced under the create-admission
  lock after idempotency replay. Commit's optional slug is normalized,
  secret-checked, and included in the idempotency checksum; the effective slug
  becomes both the new Agent slug/display name and is synced back to the session.
  `AGENT_DESIGN_SECRET_DETECTED` (422),
  `AGENT_DESIGN_SLUG_CONFLICT` (409), and
  `AGENT_DESIGN_CONFLICT_UNRESOLVED` (409) are domain errors, not generic CAS
  conflicts; `AGENT_DESIGN_SESSION_LIMIT_EXCEEDED` (429) instructs the owner to
  resume or cancel an existing design. Builder HTTP responses default to the
  strict v1 shape for already-open clients; the current frontend explicitly
  requests `contract_version=2` for assumptions, conflicts, and pagination.
- Project Agent DELETE is a soft archive. It retains immutable versions and all
  Thread/Run/Automation/Channel/OAuth references, atomically clears a matching
  project-default pointer, hides the Agent from project catalogs, and rejects new
  Run admission with `PRIVATE_WORK_AGENT_ARCHIVED`. Exact snapshots admitted
  before archive may still materialize; suspended Agents remain fail-closed.
  Archived project Agents retain their slug for history but do not occupy the
  active project namespace, so a new Agent may reuse that slug with a new ID.

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
- Private Thread deletion is an exact project/owner force-revocation boundary:
  it terminalizes matching Run/Job/Attempt and approval authority before the
  tombstone commits. Raw checkpoint removal is idempotent recovery work fenced
  to the exact tombstone generation; it must never make a committed logical
  deletion appear to fail or touch a recreated same-ID Thread.

### Configuration, models, and `inspect_image`

#### Configuration authority

Configuration is read only from explicit `DEER_FLOW_CONFIG_PATH` or the
repository-root `config.yaml`. Infrastructure settings are restart-required.
Model definitions, runtime/auth/Memory/quota/Automation policy, and provider
Credentials are PostgreSQL authority and must not be reintroduced as YAML or
ambient-key fallbacks.

#### Model adapters and `inspect_image`

The authorable System Model adapter allowlist is intentionally narrow. Retired
adapter rows may remain admin-readable for remediation, but they must not be
reactivated, made default, exposed in the public model catalog, or admitted to a
new Run snapshot.

Text-model image inspection uses `inspect_image`, a reserved, conditional Worker
tool selected through
`agent_runtime.vision_bridge.model_name`, never `config.yaml`. The selected
active System Model must declare `supports_vision=true`; its existing Provider
adapter owns SDK construction, credentials, wire serialization, and response
parsing through the single `ModelRuntime`. The tool owns only Run/file
authorization, bounded image normalization, fixed mode instructions plus one
required bounded analysis goal, durable dispatch settlement, and a bounded
untrusted ToolMessage. Use standard LangChain multimodal content blocks. Never
add a Bridge-specific adapter,
Provider HTTP client, protocol resolver, raw headers/body passthrough, or a
second model factory. `vision_openai_compatible_v1` has no production
descriptor/class path and `vision_bridge_fake` is test-only; neither may enter
new authoring, defaults, bindings, or Run snapshots. Preserve exact frozen
`purpose="vision"` snapshots, Worker abort/deadline behavior, tracing
suppression, and durable dispatch authority.

#### Configuration schema and bootstrap Credentials

The example declares `config_version: 1`. Version 1 is the initial public
configuration schema. It includes the explicit restart-required
`host_execution_approval` policy, PostgreSQL-owned Automation policy, and the
current process/runtime settings as one baseline rather than as public upgrade
milestones. Removed top-level policy keys remain fail-closed tombstones; use
`make config-upgrade` rather than manually guessing a future migration.

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
   stale revision, and transactional failure behavior.

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
  results before persistence or activation.
- Keep packaged System assets separate from project-authored assets and bindings.
- Authoring services use optimistic revisions and one transaction; failed flows
  must not leave an asset without its initial version or activate a partial
  dependency closure.
- Run deterministic review for each changed public Skill:
  `uv run python -m deerflow.skills.review.cli ../skills/public/<slug> --format text --fail-on error --fail-on-incomplete`.

## Guarded operational limits

The focused constants test keeps these values synchronized with code. If a
source constant changes, update this section and the owning detailed document.

### Execution and asset limits

- Subagent concurrency is canonically clamped to `1..4`; the per-Run total remains independently bounded to `1..50`.
- SkillScan rejects unsafe archives while retaining the final 100 MiB and the
  16384-file, bounded-log contract.
- Project Skill archives remain limited to 100 MiB total, 64 MiB per regular
  file, and 16384 regular files; archive-create routes have a scoped
  160 MiB wire limit.
- Current-message vision injects at most four unique images with a 20 MiB per-image limit.
- Fresh installs seed the `inspect_image` end-to-end deadline at 60 seconds; administrators may set `5..120`, and each Run freezes the selected value.
- Verified Skill reads remain active for `skills.read_evidence_ttl_calls` subsequent lead model calls (default 12).
- SNIP free-prose task continuity bounded to 2,000 characters and tagged fact lines bounded to 1,000 characters; the packaged prompt raises a declared output cap below 4,096 tokens.

### Runtime, Memory, and quota limits

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
