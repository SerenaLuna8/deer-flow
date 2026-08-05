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

The new-install order is:

```bash
# DATABASE_URL names a new empty target; POSTGRES_ADMIN_URL names its maintenance DB.
make setup-db
make check-db
make start
```

Run backend-only commands from `backend/`:

```bash
make gateway
make worker
make scheduler
POSTGRES_TEST_URL="postgresql+asyncpg://.../postgres" make test
make lint
make format
make check-db
```

`scheduler.enabled=false` leaves project Automation APIs and manual triggers available but
does not acquire the Scheduler ownership lock or start polling.

## PostgreSQL full-schema initialization

`full_schema.sql` is the only complete application-schema source. The exact current marker is
`full_schema_v2`; there is no Alembic revision chain or incremental upgrade path.

`make setup-db` requires an explicit administrator URL and application URL. It creates the named
empty target if needed, executes the complete packaged SQL, records the marker, seeds the packaged
system asset catalog, initializes the LangGraph checkpointer/store schema, bootstraps the
default project, and seeds the former example's DeepSeek V4 Pro as the active/default PostgreSQL
model. The backend Make target loads the root `.env` when it exists, with explicit process
environment taking precedence; explicit environment also works without that file.
`DEEPSEEK_API_KEY` and a valid Credential keyring are preflighted and encrypted before
database creation; missing or invalid material fails without creating the target. Credential,
envelope, model version, and default pointer are then written in one transaction under a distinct
non-login bootstrap principal that has no project membership. A complete existing model catalog
is validated and preserved rather than overwritten. The application role must be an ordinary
non-superuser.

An existing legacy DeerFlow database is never upgraded in place. A legacy or unknown marker,
unversioned nonempty schema, or catalog drift fails closed without DDL or repair. Operators must
provision a new empty target and run `make setup-db`.

`make check-db` is read-only. It reports the schema marker, required application and LangGraph
relations, and whether setup or operator intervention is required without printing credentials
or full connection URLs. Manual stamping, automatic repair, automatic deletion, and destructive
reset are unsupported.

### Backend core tests

The backend suite is intentionally small and protects the main client, authentication,
Gateway/Worker stream, sandbox/path, Credential/MCP, Human Input, and PostgreSQL paths.
Run the complete set only against a disposable PostgreSQL maintenance instance:

```bash
POSTGRES_TEST_URL="postgresql+asyncpg://.../postgres" make test
```

The URL must have create/drop/terminate authority for random `deerflow_test_*`
databases. It must never be a production URL or the ordinary application
URL. Missing `POSTGRES_TEST_URL` fails before pytest collection; the complete core suite must
report zero skips. Focused non-database tests may still be run directly with `uv run pytest`.

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

### Authentication and browser sessions

- Email is one case-insensitive account identifier. All user repository create, lookup, and
  update paths normalize with `strip + lowercase`; ORM and `full_schema.sql` both enforce the
  unique `lower(email)` index. This schema change follows the empty-target `make setup-db`
  lifecycle and must never be installed with a runtime or incremental migration.
- A browser access token is valid only while its signed `sid`, user `token_version`, and
  PostgreSQL `auth_sessions` row all validate. Login persists the session before returning the
  JWT; logout revokes the current `sid`, while password change increments the token version,
  revokes every old session, and issues one new session.
- `auth.local.allow_registration` gates only public local self-registration. A disabled gate
  returns structured `403 registration_disabled` before account creation; first-admin
  initialization and controlled OIDC provisioning remain independent. Public setup status has
  the exact `{needs_setup, registration_enabled}` shape; only `needs_setup` may be cached, while
  the registration policy is resolved for every response.
- `AuthAppConfig`, `LocalAuthConfig`, `OIDCAuthConfig`, and every `OIDCProviderConfig` reject
  unknown fields. A misspelled registration, auto-provisioning, domain, or cookie-policy key must
  fail configuration loading rather than silently fall back to a permissive default.
- Remember-me changes browser persistence, not authentication authority. A false choice keeps
  access, CSRF, and the HttpOnly preference cookie session-only. A true choice persists them for
  the configured token lifetime on HTTPS and localhost HTTP; public plain HTTP remains
  session-only unless the operator explicitly enables
  `auth.local.allow_insecure_persistent_cookie`.
- Access and CSRF cookies resolve secure/lifetime policy together. Logout deletes access, CSRF,
  and preference cookies even when durable revocation is unavailable and the endpoint must
  return 503. Passwords, JWTs, CSRF values, and raw `sid` values must never enter browser storage,
  public responses, logs, or traces.
- The removed top-level `authorization:` config is a version-32 tombstone, not an enabled
  generic provider. Project authority remains ProjectContext + capability + private owner scope
  plus side-effect revalidation; a future provider may only add a concrete fail-closed restriction,
  never replace or expand that authority.

## System asset and Agent execution boundary

`app.shared_assets.bootstrap` loads a strict packaged manifest and manifest-listed regular
files only. It rejects unknown keys, duplicate source keys, path escape, symlinks, non-regular
files, and digest mismatch. One transaction writes published system Agent, Skill, and MCP
rows under the fixed non-login builtin principal. Repeated setup with the same catalog is
idempotent; a conflict rolls back the whole seed. The seed never creates a Credential,
project binding, membership, or secret. Separately, each newly created project atomically pins
every currently active System Skill's current published version in an enabled project binding;
this applies to the default project only when that project is first created. Re-running default
project bootstrap never reconciles bindings, re-enables an administrator-disabled Skill, or
binds a later catalog addition into an existing project. System Agent and MCP bindings remain
explicit opt-ins.

The 14 directories under `../skills/public/` are the sole source of truth for every System Skill
entry. There are no separately maintained builtin Skill payloads. The generator replaces the
complete packaged Skill set from those directories, adding missing entries and archives and
removing every stale Skill entry and generated archive while retaining Agent and MCP entries.
Archives include `SKILL.md`, scripts, references, templates, and other regular files. Regenerate the
checked-in archives and manifest entries from `backend/` with
`PYTHONPATH=. uv run python scripts/generate_public_system_skill_catalog.py`; use `--check`
to verify that they are current. Generation and bootstrap use the same bounded frontmatter,
archive, and static-scan validation; generated destinations reject symlinks and are replaced
atomically. Runtime processes never scan `skills/public/`, and setup does not create project
bindings while seeding the catalog. A project-creation transaction applies the System Skill
default bindings described above without changing the catalog bootstrap boundary.

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
the exact secret-free Agent/Skill/MCP, MCP Credential-grant, and Skill Credential-reference
snapshot for a Run. Worker reloads and revalidates that exact closure, decrypts Skill Credential
fields only inside Worker memory, and materializes system Skill bytes below
`/mnt/skills/public/<name>` and project Skill bytes below `/mnt/skills/custom/<asset_uuid>` in a
run-owned read-only tree. Skill Credential configuration is scoped by project, Skill, and exact
Skill version so a newly published version cannot overwrite bindings used by an older pinned
Agent. Each activated Skill execution revalidates and decrypts its exact closure, then injects
plaintext only into that sandbox subprocess environment; platform code does not intentionally
serialize it into prompts, version payloads, snapshots, API responses, logs, or traces, and masks
literal command output. This masking is an accidental-leak guard, not DLP: a Skill granted a
Credential is trusted code and could transform, persist, or exfiltrate the value. Subagents inherit
only the internal path-scoped reference carrier and perform the same execution-boundary validation.
New project Skills are created in `suspended` state. Authors may create, fork, replace, and
publish versions while suspended; activation is a separate capability-checked transition that
requires a published version. Resolution and runtime materialization accept only active,
published Skills. Project Skill display names are case-insensitively unique within one project;
different projects may use the same display name.

### Runtime Skill activation, Scan, and Review

Every Skill in an admitted Agent snapshot is passive until activated. Lead-Agent
`allowed-tools` authority has exactly two sources: an explicit user slash activation, or a
successful `read_file` of an exact admitted `SKILL.md` observed by
`SkillToolPolicyMiddleware` in the current private Run. The latter creates ephemeral evidence
bound to `project_id + owner_user_id + run_id` and a middleware-local owner token. Durable
`ThreadState.skill_context` is an observational reminder channel only; it is never tool-policy,
Credential, secret, or execution authority. The Worker rejects caller-supplied evidence, clears
stale evidence when a runtime config is reused, and redacts the evidence and policy decision
from every observable serialization surface.

Slash authority has priority over later successful reads. One authenticated run marker binds
the slash to `project_id + owner_user_id + run_id + message id/hash`, so the same command reads,
injects, and audits once per Run while secret bindings are still recomputed on every model call.
The marker is runtime-only, rejected from caller context, cleared on config reuse, and never
contains secret material.

Active Skill policy is enforced at three independent lead-Agent boundaries:

1. model-call tool schemas;
2. tool execution before the handler runs;
3. `tool_search` returned schemas and promoted names.

The decision binds a middleware owner token, decision version, source, exact paths, exact
admitted version IDs, and allowed names. Malformed evidence, stale decisions, unresolved exact
paths, or unvalidated `tool_search` result shapes fail closed. `describe_skill`, `read_file`,
and policy-filtered `tool_search` remain framework tools; Review does not add a framework tool.
Subagent Skill loading keeps its required framework tools and escapes loaded Skill text, but
must not be described as having the lead middleware's complete dynamic active-policy lifecycle.

Autonomous Skill secret selection consumes the same authenticated successful-read evidence,
not `ThreadState.skill_context`; explicit slash selection consumes its authenticated slash
source. Both are resolved back to the exact admitted runtime registry. Existing command-boundary
project/membership/Run/lease/version/checksum/Credential revalidation and one-subprocess
plaintext injection remain authoritative.

Project Skill authoring always runs deterministic SkillScan. Its Python analysis covers direct
network sinks, supported HTTP client-instance data flow, uncertain `subprocess shell` arguments,
reverse-shell call sites, and PEP 695 type-alias handling while retaining the final 100 MiB,
16384-file, bounded-log, and Mach-O boundaries. Both archive authoring and archive preflight
reject any member name containing `:`, including ZIP/TAR NTFS alternate-data-stream names.
Untrusted Skill names, descriptions, paths, allowed-tool metadata, slash reminders, and
subagent-loaded content must be escaped at their prompt or ToolMessage rendering boundary.

Deterministic Review v1 lives in the app-independent
`deerflow.skills.review` package and emits the three checked-in
`contracts/skill_review` schemas. The app-only `PostgresSkillVersionReader` requires a
server-issued `ProjectContext` plus exact Skill/version/checksum, reads through project-scoped
repository authority in one transaction, revalidates immutable rows/files/checksums, and emits
a secret-free PackageSnapshot. `PostgresSkillReviewService` analyzes and renders outside the
transaction with `asyncio.to_thread`; harness code never imports `app.*`. Static report
`completed_at` is supplied from the immutable version `created_at`, not the current clock, so
repeated review of one exact version is byte-stable.

Review has no Gateway API, model tool, LLM moderation step, persistence side effect, public
`skill-reviewer` Skill, or runtime authorization effect. The changed-public-Skill check is a
step in the existing consolidated release workflow, not a separate workflow. Its PostgreSQL
exact-version integration test requires a real `POSTGRES_TEST_URL`; a local skip caused by a
missing URL is not release evidence.

Interactive project Skill creation atomically writes the suspended asset and version 1 Draft
with one backend-generated root `SKILL.md` template in the same transaction, including quota
reservation and both governance events. It never publishes that template, never leaves an
asset without its initial version, and returns the final asset revision after both writes.
Every later UI-authored version is a fork of the exact selected immutable version; there is no
independent blank-version UI path.
Conversational project Skill creation uses private owner-scoped Skill Builder sessions. Session
creation stores only the normalized project-local name and pins the exact current published
System `skill-creator` asset/version/checksum without requiring a project binding. Generation
loads that immutable `SKILL.md`, calls a no-tool model, strictly validates its JSON contract,
and permits one bounded repair attempt without echoing the invalid output. It persists only
bounded UTF-8 candidate files plus validated clarification/messages. Candidate edits, validation, and commit
are revision-, checksum-, and idempotency-bound. Final commit revalidates the exact draft and
atomically creates a suspended Skill with published version 1; it never enables, binds, or adds
the Skill to an Agent. Cancel clears candidate bytes. Hard deletion tombstones completed Builder
references before deleting the Skill package. A clarification reply must transition the row to a
database-valid `generating` state before any repository query that can trigger SQLAlchemy
autoflush; `awaiting_clarification` may never be flushed with a cleared clarification payload.
Candidate directories are implicit prefixes of canonical relative file paths rather than stored
directory rows. `scripts/`, `references/`, `templates/`, and other safe nested paths share the
same validation and persistence path; Builder cannot represent empty directories, binary files,
symlinks, or executable mode bits. Validation and commit preserve every canonical path without
flattening it into the published `skill_version_files` snapshot.
Agent `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` entries are logical UI documents
backed by fields on the immutable Agent version; they are not filesystem assets or a separate
version graph. Saving them against a published Agent atomically clones and publishes the current
runtime configuration before moving the published pointer. An Agent without a published runtime
version receives an internal Draft that later runtime-version authoring inherits. New Agent
payloads use checksum schema v2; migrated and packaged v1 rows retain their exact legacy
Soul-only checksum and `<soul>` content wrapper, while runtime placement is intentionally
promoted to the same highest project-configurable tier as v2 profiles. Worker renders the
admitted four-field bundle after
other project-configurable instructions, making it the highest project-configurable tier, and
places it immediately before the final platform critical reminders. Platform authorization,
confidentiality, and isolation invariants remain authoritative wherever they appear in the
template. Worker passes the same redacted in-memory bundle to subagents without metadata or log
serialization.
Project and admin-project Gateway APIs expose Agent revision history as read-only data and do
not register manual Agent-version creation or publish mutations. Builder confirmation and
instruction saving continue to create or advance immutable revisions through internal service
operations; Run admission and snapshots continue to pin the exact published revision.
Agent creation uses a dedicated project-and-owner scoped Builder session, not an ordinary
private Thread or Run. Creating the session stores only the normalized name and does not call a
model or create an Agent. Each generation turn builds a bounded, server-authorized context and
stores only validated clarification/candidate results; raw prompts are never attached to tracing.
Final confirmation is one transaction that creates the project Agent as `suspended`, publishes
complete version 1 with all four logical documents, advances the pointer, and marks the Builder
session completed. Dependency child rows are inserted while version 1 is still Draft; only after
the complete Skill/MCP ref set is flushed may the same transaction transition it to Published and
advance the pointer. The public confirmation response returns only the completed session and Agent;
the internal revision is not exposed. Project-local Agent slugs remain unique. Interrupted generation is recoverable,
and retention removes Builder messages and blueprints with the exact project/owner private scope.
Agent lifecycle does not expose an archive mutation. Project and project-override APIs retain
capability-checked activate/suspend transitions; project UI labels these as enable/disable. A
project member with `shared_assets.edit` may hard-delete an unreferenced project Agent package,
including every version and dependency-ref row. Any retained Thread, Automation, or exact Run
snapshot reference rejects deletion with conflict; deletion never cascades into private work.
Completed private Builder history is retained with a deleted-Agent tombstone. System Agents and
platform override routes never expose Agent deletion. Project MCP lifecycle exposes
activate/suspend for assets with a current Published version; activation revalidates the exact
definition and Credential closure. A project member with `shared_assets.edit` may hard-delete an
unreferenced project MCP package, including its versions, Credential slots, and grants, while any
Agent-version, exact Run, or grant-snapshot reference rejects deletion with conflict. System and
platform-override MCP assets never expose deletion. The archive mutation remains only for
historical/admin compatibility and is not a project UI primary action. Skill activate/suspend
remains independent. Historical Agent rows already carrying the frozen-schema
`archived` status remain unreadable to execution; removing the API does not rewrite published
migrations or historical data.
Agent version authoring rejects a dependency set containing duplicate Skill slugs across
system and project scope before that layout can become ambiguous. Worker materializes MCP
secrets only after current capability, exact selected version/checksum,
binding, slot, grant, Credential, and envelope closure are locked and revalidated. Catalog
generation is diagnostic snapshot metadata, not a global invalidation token: an unrelated
project mutation must not stale an exact admitted Run. Secret material never enters an API
response, cache, checkpoint, event, repr, log, or audit metadata.

Project MCP execution is fail-closed. Project and admin-project authoring accept only
`http`/`sse` definitions with a secret-free HTTP(S) URL whose IP literal belongs to
`mcp_security.project_remote_allowed_networks`. Exact `localhost` is deterministically normalized
to `127.0.0.1` before validation and persistence, so it never depends on a later resolver choice;
IPv6 loopback callers use `[::1]` explicitly. Every other DNS hostname is rejected because a
one-time resolution followed by a separate connection would create a DNS-rebinding/TOCTOU gap.
The defaults cover IPv4 loopback, RFC 1918, IPv6 loopback, and IPv6 ULA; an empty list denies every
target, while `/0` must be an explicit operator choice. CIDR membership does not restrict a target's
port, path, or service identity. HTTP carries header/query Credential values in plaintext and is
only suitable for a trusted isolated network; HTTPS IP literals require a matching IP SAN. With a
forward proxy, `127.0.0.1` refers to the proxy's network namespace and the proxy must independently
enforce the intended network boundary. Project `stdio`, `streamable_http`, OAuth,
literal env/header values, and env/OAuth Credential targets are rejected. Project secrets may
enter a remote MCP call only through an approved encrypted Credential header or query slot;
query secrets are appended in Worker memory to the validated secret-free base URL for
each discovery/tool-call client. The configured base URL itself cannot contain a query/fragment
delimiter, and runtime validation also prevents a slot from replacing an existing query parameter.
They necessarily appear in the outbound request-target, so production egress proxies and
upstream access logs must omit or fully redact query strings. Scoped project MCP clients suppress
their own `httpx`/`httpcore` transport logs without disabling unrelated HTTP observability.
Authority, proxy-authentication, content-length, and other hop-by-hop header names are forbidden. Run
admission takes shared MCP/version locks, rejects historical project `stdio`, and revalidates
the current definition and Credential-slot closure even when a caller omitted the endpoint
policy (which fails closed). Worker revalidates every exact project MCP snapshot before
discovery and every call. Worker injects a no-redirect, `trust_env=false` HTTP client, uses the
controlled egress proxy only when configured, and applies separate operator hard timeouts to
discovery and tool calls. `require_egress_proxy` defaults to false; deployments may enable it as
an additional network-policy boundary.
The member-facing create path is the project-only strict
`POST /api/projects/{project_id}/mcp-servers/configured` aggregate mutation; it does not accept
raw JSON, workflow flags, Credential values/IDs, or an expected asset revision. One service
transaction creates the active MCP row, inserts immutable Draft revision 1, and advances the
asset revision for both steps. A definition without Credential slots reuses the Draft-to-Published
transition and advances the current pointer; any declared slot reuses Draft-to-Pending-Approval
and leaves the pointer empty until a separate authorized approval binds existing project
Credential versions. Both outcomes return the final asset plus redacted configuration and leave
the asset at revision 3. The legacy granular asset/revision endpoints remain compatibility
surfaces, but the project UI must not sequence them for initial creation.
Project editing uses the strict configured PUT for the exact project MCP and expected asset
revision. In one transaction it inserts the next internal Draft revision and immediately reuses
the Published or Pending-Approval transition, so a revision-hidden UI never leaves an unreachable
Draft. Draft submission/publication and pending approval require the stored `supersedes` pointer
to equal the asset's locked current pointer; stale branches fail with conflict and cannot replace a
newer current configuration. System MCP enable/update uses the project-only `sync-current` binding
mutation. The request never accepts a version ID: after locking project and binding, the service
locks the System MCP and its current Published revision, validates the full Credential closure,
then creates, re-enables, or moves the exact binding with optimistic binding revision control.
Publishing a project MCP without Credential slots atomically enqueues one durable
`mcp_discovery` Job. A Credential-bearing revision stays pending without a Job; successful
project approval atomically publishes the exact grant closure and enqueues it. The project-only
manual discovery POST requires both edit and execute capability and either returns the active
same-closure attempt or enqueues a new one. Gateway only admits and reads these attempts: the
Worker revalidates the current project membership, exact version/checksum/grant closure, endpoint
CIDR policy, and lease immediately before remote initialize/list-tools, and never invokes a tool.
The Worker atomically settles the Job, attempt, and `project_mcp_tool_inventories` observation.
That project/version-scoped row carries only bounded provider tool names, sanitized plain-text
descriptions, stable public failure codes, timestamps, the version payload checksum, and a digest
of the Credential grant closure. Gateway exposes it only through the project exact-version tools
GET and never contacts MCP or decrypts Credential material. A changed checksum or grant closure
makes prior tools stale; an active attempt reports testing, and a failed current attempt may retain
a matching last success as degraded. Discovery failure does not roll back the published config.
This inventory is display-only diagnostic state: every Run still performs fresh discovery and
must never use it as execution authority.
`mcp_security` is startup-only: update Gateway, Scheduler, and every Worker together. Historical
immutable versions remain readable with env/header values redacted and Project remote URLs
reduced to their HTTP(S) origin; path/query details are never replayed. Unsafe versions are never
rewritten or silently skipped. The project-only configured GET is the narrow editing exception:
it requires `shared_assets.edit`, returns only the latest pending revision on the current lineage
or the exact current Published revision, revalidates that definition against the current endpoint
policy, and may then expose its secret-free IP-literal URL path. It never returns query, fragment,
embedded credentials, literal env/header values, or arbitrary historical paths. Gateway and
Scheduler import the neutral
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
Every executable Run has a server-owned `origin_trace_id`. Client metadata, config, context, and
request bodies may not select either `origin_trace_id` or `deerflow_trace_id`. Admission persists
the same value on Run and Job; the composite
`project_id + owner_user_id + run_id + origin_trace_id` foreign key and Worker-side revalidation
must both hold before model, tool, stream, or settlement side effects. Same-`run_id` semantic
retries retain the first admitted trace. Retention jobs carry no Run trace. Worker keeps this
durable trace in its internal ContextVar/runtime authority regardless of logging configuration,
while `logging.enhance.enabled=false` prevents only the external Langfuse
`deerflow_trace_id` attribute. Public Run responses, metadata, browser caches, and audit payloads
never expose the raw durable trace; audit stores only its domain-separated request HMAC.
Operator log records may carry the trace only when the startup-frozen logging enhancement is
enabled.
`run_events.id` and `run_events.seq` are signed PostgreSQL BIGINT values in the full schema.
The schema change has no in-place upgrade path: an older database must be replaced with an
empty target and initialized through `make setup-db`. A settled terminal is replayed only when
its cursor is strictly greater than the request cursor; an exact terminal cursor returns an
empty successful response rather than a duplicate `stream.end`.
The four non-SSE private-work feeds — per-Run messages, Thread messages, per-Run events, and
Thread events — serialize every `seq` as a canonical non-negative decimal string, never a JSON
number. Their `before_seq`/`after_seq` cursor contract uses the same decimal representation and
is bounded by signed PostgreSQL BIGINT. The privacy-center NDJSON attachment is outside this
Thread/task feed contract and still exports its event `seq` as a JSON number.
Thread search offsets, Thread patch/delete `expected_version`, Run-list offsets, and ready-file
offsets are likewise bounded by signed PostgreSQL BIGINT; overflow is a stable private 422 rather
than a database error. Explicit same-scope Thread creation is intentionally non-idempotent under
an insert race: exactly one request returns 201 and the loser receives 409, with one Thread row
and one root checkpoint. Two renames using the same `expected_version` similarly produce one 200
and one 409, incrementing the version once without a silent overwrite.
Each project may hold one revisioned default pointer to an active, published, executable
project-owned Agent. Project admins manage that pointer with `shared_assets.manage_bindings`;
readers may observe it but cannot change it. Ordinary Thread creation omits both Agent fields and
resolves the pointer inside the authoritative Gateway transaction, falling back only to the
packaged Main Agent when the pointer is unset. Explicit Agent-card creation still supplies both
fields and wins over the default. A configured but unavailable default fails closed, and neither
changing the pointer nor clearing it rewrites an existing Thread or Run snapshot.
Canonical packaged Main is a binding-free project orchestrator. Run admission resolves its
effective closure from only the entered project: active/current project-owned Agent/Skill/MCP
assets plus enabled System bindings. Other projects are never eligible. The snapshot persists one
globally ordered lead -> delegates -> Skills -> MCPs closure; current Main Skill/MCP versions form
the prefix and delegate-only historical versions follow. Ordinary project and System Agents never
expand this pool and keep only the Skill/MCP versions explicitly referenced by their exact Agent
version. Automation admission uses the same closure path as interactive private Runs.
Run catalogs use stable newest-first `limit/offset` pages. Ready-file catalogs use stable
`logical_path + version + id` pages and return `X-Next-Offset` only for a safe full page; that
header is CORS-exposed. Clients must enumerate the complete catalog with an AbortSignal, strict
public schemas, duplicate/progress checks, and a hard safety bound. Invalid responses, repeated
full pages, or an unsafe next offset fail closed rather than truncating or looping forever.
Worker stream consumers are root/namespace separated. Child graph frames keep their namespaced
event names and bypass root fallback detection, root file-tool batching, and parent subagent
persistence. Root `write_file` and `str_replace` argument deltas are grouped in bounded batches
without bypassing `LeaseAuthorizedStreamBridge`; normal text and meaningful metadata still
stream immediately. Provider transport-only metadata such as `model_provider` must neither
flush nor publish a pending file batch. Every pending batch is flushed on identity/mode/value,
finish, and error boundaries.
Worker-side RunJournal, subagent, and workspace-change event writes use the same exact private
scope and raw Job lease. `DbRunEventStore` revalidates project membership, Job/Run state,
cancellation, expiry, and both lease hashes in the event-write transaction; an old Worker may
not append internal events after lease loss.

Checkpoint message storage is process-frozen as `database.checkpoint_channel_mode=full|delta`;
`database.checkpoint_delta.snapshot_frequency` is frozen with it because the cadence is compiled
into the graph. Gateway and every Worker sharing PostgreSQL must use the same pair and restart
together. The supported migration is full to delta only; once a Thread carries the delta marker,
a full process must fail before reading or writing it. Private state consumers bind a
mode-matched materialization graph above `ProjectScopedCheckpointer`, preserving the exact
project/owner/Thread authority marker and row-lock boundary. Raw `channel_values.messages` is
never a complete-state API in delta mode. Goal, compaction, branch, regenerate, connection
inbound, Worker resume, and rollback must use materialized snapshots; replace-style writes wrap
reducer channels in `Overwrite`. Branch writes the exact pre-human replay base and then the
selected state as two target checkpoints, excluding `sandbox` and `thread_data`, so later
regeneration has a valid ancestor. Unknown middleware channels fail closed instead of being
silently discarded.

Manual compaction validates current project, membership, capabilities, Thread ownership, and
incomplete-Run state in a short transaction before materializing the summary model or its
Credential. A request authorization boundary revalidates the same authority immediately before
the external summary-model call. Both transactions are closed before the model `await`; final
checkpoint persistence still performs its own lock, revalidation, and CAS.

Custom summarization prompts accept only the required `{messages}` replacement field (literal
braces use `{{` and `}}`). Durable summary and message text is escaped before the final rendered
prompt is token-bounded. Empty or failed model output is a no-op: it neither removes messages nor
fires compaction hooks.

Loop detection keeps the higher global frequency allowance for local file and shell workflows,
but applies lower default frequency bounds to `web_search` and `web_fetch`. Varying remote
queries must receive a stop warning and hard-stop before a private Run can exhaust its default
LangGraph recursion ceiling.

Hidden goal continuations advance their counter from the fresh goal state while holding the
thread goal lock. A stale evaluator may not regress or collapse a committed attempt, and the
stand-down write after a racing user message records only its reason rather than counting the
same continuation twice.

The durable top-level `message` journal is a lead-Agent conversation projection. Lead AI
messages and their exact `tool_call_id` results belong there; subagent and middleware AI/tool
callbacks are persisted as `trace` events and continue to use the dedicated bounded subagent
event stream. Caller attribution first follows the exact callback run ID from tool start, then
falls back to the issuing AI tool call and callback tags, so provider-local call-ID reuse cannot
leak nested reasoning or tool output into the user conversation. Run-level message counts and
last-answer summaries are lead-only.

Public Run ingress never trusts client-selected message visibility. A structured
`human_input_response` may become hidden only inside Run admission after the exact project,
membership, and Thread locks are held and no active Run conflicts: the Gateway reads the
transaction-bound materialized checkpoint, requires the latest unanswered server-issued
`ask_clarification` request, and matches request ID, tool-call ID, response mode, option/value,
canonical response text, and message-ID uniqueness before adding server-owned
`hide_from_ui=true`. Forged, stale, already-answered, batched, or non-canonical messages remain
visible. Existing-Run idempotency may ignore only that one server-owned visibility bit when every
other request field is identical.

For a streaming lead-Agent model call, `RunJournal` observes the interval from the first
non-empty reasoning delta to the first visible answer delta. A reasoning-only tool-call step
closes at that model call's end. The bounded millisecond value is persisted on the selected AI
message as `additional_kwargs.reasoning_duration_ms`; it is never copied from Run latency,
tool/subagent time, another model candidate, or an unobserved non-streaming response. Missing
observation therefore remains missing rather than inventing a duration.

Main delegates through a Worker-sealed namespaced runtime Agent catalog. Every dynamic Agent gets
its own exact model snapshot and model settings, Prompt bundle, tool groups, Skill versions, and
private MCP proxies; it never inherits Main's complete runtime assets and cannot recursively call
`task`. Project-authored Agent/Skill/MCP text is followed by a final platform security and
confidentiality reminder in the delegated Agent's single SystemMessage. Worker disables global
MCP/ACP discovery and marshals each exact proxy call back to the owner Worker loop, so the same
authorization, grant, Credential, endpoint, and side-effect checks run for every delegated
invocation. Detached subagent execution preserves
request identity, authorization, and trace ContextVars but clears the parent LangGraph
`RunnableConfig` before entering its isolated loop, so raw child model/tool frames cannot leak
through the lead stream writer. The parent `task` tool's bounded `task_running`/terminal custom
events and persisted `subagent.step`/`subagent.end` rows are the authoritative subtask UI channels.
Namespaced child custom frames remain visible on their namespaced SSE channel but must not be
persisted again as parent Run subagent rows; only root-namespace task lifecycle events feed that
parent event buffer.
A `general-purpose` subagent without a command-execution tool rejects explicit Shell/Python
execution requests before creating its model Agent and returns a structured failed result; it
must never fabricate output or loop through wrapper files as a substitute for execution.

Subagent batch concurrency is canonically clamped to `1..4` in configuration, prompt rendering,
and `SubagentLimitMiddleware`; the per-Run total remains independently bounded to `1..50` and
private ledgers count the exact project, owner, Run, and provider occurrence. A child
`deerflow_error_fallback` marker fails only that task; only a root-namespace marker may decide the
parent Run terminal state. Terminal ToolMessage metadata and task lifecycle events may expose only
the effective model name and the validated cumulative `input_tokens`, `output_tokens`, and
`total_tokens` snapshot. The executor publishes a thread-safe collector snapshot after each
streamed graph chunk so polling can report usage before terminal completion. Subagent event-store
batch failures are re-buffered in original order and must retain the server-issued private scope;
`CancelledError` must re-buffer the in-flight batch before cancellation is re-raised.

## Project Memory runtime

PostgreSQL is the only project Memory authority. Every row remains bound to
`project_id + owner_user_id + namespace`; the harness must not derive these coordinates from
model arguments, request payloads, ambient user state, or a replaceable Memory backend.

The `full_schema_v2` snapshot reserves the complete staged Memory v2 contract: Source Batch and
Item, Extraction/Consolidation Generation, Candidate, versioned Fact/Evidence, derived Summary,
Suppression, and per-Run Context Snapshot rows, plus the `memory_extract`,
`memory_consolidate`, and `memory_retention_purge` Job types. `memory.pipeline_mode` is frozen in
the existing Run runtime-policy snapshot and defaults to `off`. The Worker handles
`memory_extract` jobs with the frozen model snapshot, a fixed no-tool/no-tracing extractor, and an
atomic Candidate-plus-Job settlement. In `consolidate` or `v2` mode, the existing Scheduler admits
at most one due `memory_consolidate` Job per project/owner/namespace, with 20 Candidates per Job;
only the Worker calls the fixed no-tool/no-tracing consolidator. The Generation freezes the exact
runtime-policy revision and model ID/version/checksum, and Candidate decisions plus
Fact/Revision/Evidence writes settle in one transaction. A safe transient dead Job may receive at
most one automatic successor for the same frozen Generation; a second dead result stays available
for operator diagnosis instead of creating an unbounded Job chain. Terminal Candidate bodies are
erased by `memory_retention_purge` at the exact cutoff frozen on admission while pending Candidates
remain. Both consolidation and retention lock and recheck the current policy in their settlement
transaction, so pausing keeps the backlog and cannot commit a Fact or erase Candidate text after
the pause has linearized.

Gateway exposes `/api/projects/{project_id}/memory/v2/*` as the writable management surface. Fact
lists support bounded search, category/status filtering, and `limit/offset` pagination; Candidate
lists are likewise paged. Reads expose scoped Candidates/Facts and their Revision/Evidence history,
while writes use Candidate `updated_at` or Fact `version` CAS for accept/reject, user Revision,
disable/restore, and irreversible hard forget. The read-only `/v2/status` response projects only the
current committed `enabled`, Pipeline mode, search/injection switches, consolidation interval, and
Candidate retention period. Hard forget erases every derived body, writes source/lineage
suppression, and retained audit-HMAC keys remain eligible when a replay is checked after key
rotation. Owner export is streaming NDJSON and excludes HMAC/checksum material. Thread or Run
deletion first suppresses and erases its source lineage while preserving accepted Fact content;
Thread deletion conflicts with an active/finalizing Run. A deleted Run referenced by its immutable
Job is retained only as a hidden scrubbed shell, with Run events, feedback, artifacts, and removable
admitted snapshots deleted explicitly.

The v1 aggregate is rollback-only and read-only. Its scoped list, status, and export routes remain;
the compatibility reload route returns fixed `501` without mutation, while import and Fact
create/update/delete routes do not exist. The historical aggregate has no automatic writer: the
built-in `MemoryMiddleware`, process-local queue/updater, pre-summarization Memory hook,
message-processing helper, and Worker shutdown flush are removed. `off`, `shadow`, and
`consolidate` may still read retained v1 data; no current runtime or API path creates or changes it.

Recall follows the exact Run-frozen Pipeline mode. `off`, `shadow`, and `consolidate` continue to
read the v1 aggregate so an operator can roll back without deleting v2 data. A Run frozen in `v2`
creates at most one `run_memory_context_snapshot` on its first Memory read, pins at most the frozen
`max_facts` active exact Revisions as ordered items, and reuses those items for every retry/resume.
Candidates and non-active Facts never participate. Later Fact edits or newly consolidated Facts are
visible only to a new Run; disable and hard forget are applied as an overlay to the pinned items at
the next read, without selecting replacement Facts into the old Snapshot.

Private Lead Agent Runs may expose the async, read-only `memory_search` tool when
`memory.enabled` and `memory.search_enabled` are both true. Its model-visible arguments are only
`query`, optional `category`, and `top_k`. The Worker creates an opaque Run-bound Memory authority
and installs it under the internal `__memory_authority` runtime key after stripping caller values.
One search transaction locks/revalidates the exact membership and capability, active Run
authorization, Job/Run/lease/cancellation/thread binding, then reads the exact Memory source for the
frozen Pipeline mode. A v2 search ranks the same pinned items and revision ceiling used by automatic
injection; an empty v2 Snapshot has the valid virtual ceiling `0`. The ranker owns no scope, cache,
index, or persistence.

The canonical code-registered `memory_search` object uses the trusted read-only tool boundary, so
its PostgreSQL read does not mark Job retry safety unknown. A name or metadata value cannot claim
this status; legacy boundaries without the read-only hook fall back to the ordinary tool boundary.
Search errors expose only a stable public code, and untrusted fact content/category are
neutralized and bounded before returning to the model.

Memory injection remains a hidden low-authority Human message. Do not promote Memory text to System
or replace dev's latest-genuine-user selection with main's first-user selection. The v1 fallback
keeps the historical Thread-checkpoint read behavior. The v2 path reads through the same opaque Run
authority at every model boundary: it replaces the prior Run's hidden Memory instead of appending
duplicates, reuses the same pinned items, and removes hard-forgotten or authorization-revoked
content before the model call. Renderer work runs off the event loop so the bounded model-boundary
timeout remains effective. A missing v1 row is a read-only virtual version `0`; it is never created
as a side effect of recall. A failed v1 injection does not mark Memory loaded merely because the
date reminder succeeded, so a later turn may retry.

New long-term Memory is produced only through the durable v2 Source -> `memory_extract` ->
Candidate -> scheduled `memory_consolidate` -> versioned Fact path. Thread summarization owns only
Thread context compaction and has no long-term Memory write hook.

## Project APIs

All private and governance APIs are project-scoped:

- `/api/projects/{project_id}/private-work` for Threads, Runs, durable SSE, feedback,
  uploads/files, artifacts, token usage, and input polish;
- `/api/projects/{project_id}/memory` for project-owner Memory;
- `/api/projects/{project_id}/connections` for project-bound providers and connections;
- `/api/projects/{project_id}/channel-group-bindings` for Admin-managed project group bindings;
- `/api/projects/{project_id}/automations` for definitions, manual admission, history, and
  readiness;
- `/api/projects/{project_id}/agents`, `skills`, `mcp-servers`, and `credentials` for visible
  project/system assets and project bindings;
- `/api/projects/{project_id}/usage`,
  `/api/projects/{project_id}/usage/token-series`, and
  `/api/projects/{project_id}/audit` for project governance;
- `/api/admin/assets` and `/api/admin/operations` for authenticated system administration.

The system-admin Job catalog searches projects through a bounded, case-insensitive
`project_query` matched against public project display names and slugs. Its response carries both
human project fields for the catalog and `project_id` for exact recovery coordinates; the legacy
exact UUID filter remains API-only and must not become the primary operator workflow.

Project input polish requires both `private_work.create` and `shared_assets.execute`, locks the
Thread, and validates the exact Agent/Credential closure before the auxiliary model call.
Project follow-up suggestions read only the authoritative scoped checkpoint and Agent snapshot;
their stable `model_ref` (including `default`) must be resolved through the
PostgreSQL model catalog to an exact active logical model version before the auxiliary model
call.
Channel inbound must resolve one connected PostgreSQL row carrying exact
account/project/owner/connection authority before it can create a private Thread, Run, or job.
Provider delivery identity is carried separately from authority. Its durable delivery row stores
only a SHA-256 digest; the raw identifier remains transient in adapter/message processing and
must not be persisted in delivery metadata. Admission locks the exact connection, conversation,
and Thread, checks the scoped delivery before the active-Run conflict, and binds a new delivery
to its Run in the same Run/Job/quota/audit transaction. A duplicate delivery creates no second
Run or outbound reply. Delivery rows have no TTL, remain across current conversation-mapping
rotation, and are retained until their Run or channel connection is deleted by explicit retention.

Project group binding is separate from personal `p2p /connect`. An Admin selects an executable
Agent and generates one short-lived `/bind-project` command; the provider adapter consumes that
command in the target group. The persistence/service model is provider-neutral, while the current
UI and adapter implementation expose Feishu only. Group members need neither an ActWeave login nor
a personal connection. Each provider sender resolves to a distinct pseudonymous `channel_guest`
owner; `(group, topic, sender)` may reuse that owner's Thread, but two senders in the same topic
must never share owner, Thread, Memory, files, Runs, or context. Guest principals cannot
authenticate, are excluded from public membership/account catalogs and member-quota reconciliation,
and remain absent from human owner-scoped web Thread catalogs. Admin group-binding APIs return only
group name, selected Agent, status, and bounded activity time, never message bodies, private-work
content, or raw provider identifiers.

GitHub does not automatically retry failed webhook deliveries. A transient fan-out failure
returns 503 so Recent Deliveries and recovery tooling retain an operator-visible failed event
for manual/API redelivery. A known delivery can still be manually/API redelivered after an
accidental 200, but it is not discoverable through the normal failed-delivery recovery set.

The project Token series is a `project.usage.read` aggregate across owners in one project. It
returns exactly 24 consecutive UTC hour buckets, assigns terminal Run counters by durable Job
settlement time, and zero-fills missing buckets. The current-hour bucket is partial. The query
must pin PostgreSQL bucketing to UTC and must not expose Run, Thread, owner, model, or payload
identifiers.

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
Private Run file finalization is separate from the ten-file upload batch contract. It scans at
most 2,000 regular workspace/output files within a 10,000-entry traversal boundary; directories
do not consume the file count. Per-file and 100 MiB total byte limits apply only to files created
or changed by the current Run, not to the restored cumulative Thread workspace.
After finalization, a Run that created or modified `outputs/*` succeeds only when at least one
of those exact current-Run paths has a server-verified artifact from `present_files`. Presenting
only an older output is insufficient. Finalized files remain ready when this delivery verdict
fails; this rule does not add main's Gateway-owned RunStore receipt chain.
The lead-Agent prompt and `write_file` tool schema treat a file explicitly requested by the user
(including source code, scripts, configuration, and documents) as a final deliverable: write or
copy it to `outputs/*` and call `present_files` before the final response. Workspace-only files
remain valid temporary/intermediate files and are not implicitly published.
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
It removes project group-binding challenges/bindings, HMAC external-principal mappings, guest
connections/conversations, and guest-owned private data in dependency order. It may delete only
unreferenced `channel_guest` memberships and users; human accounts and retained governance
references remain outside that cleanup.

## Configuration

Configuration is read only from an explicit `DEER_FLOW_CONFIG_PATH` or the repository-root
`config.yaml`. `database`, `worker`, `scheduler`, `channels`, sandbox, tools, logging, and
deployment-owned prompt/path policy remain supported. Infrastructure configuration is
restart-required. Unknown application extension fields remain allowed where their typed models
permit them, but removed top-level keys fail validation instead of being ignored.

The current example schema is `config_version: 35`. Version 35 replaces the retired exact
`mcp_security.project_remote_allowed_endpoints` list with CIDR-based
`project_remote_allowed_networks`. `make config-upgrade` preserves an old empty endpoint list as
an empty deny-all network list, but refuses to guess a CIDR for any nonempty endpoint list. In
addition to the top-level `models:` and
`authorization:` tombstones, YAML leaves now owned by the PostgreSQL `agent_runtime`, `auth`, and
`quotas` policy sections are rejected; run `make config-upgrade` to remove them from an older local
file. Deployment-owned siblings such as `title.prompt_template`,
`summarization.summary_prompt`, `tool_output.storage_subdir`, and non-policy `subagents` fields stay
in YAML. Model definitions, immutable versions, the default pointer, exact Credential
references, and Run snapshots are PostgreSQL authority. A system admin manages the catalog at
`/admin/settings/models` and live runtime policy at `/admin/settings/system`; provider secrets are
encrypted Credential envelopes and are decrypted
only at the execution boundary. Runtime configuration imports never load dotenv implicitly.
The explicit local `make setup-db` command is the sole one-time exception: it imports
`DEEPSEEK_API_KEY` from an existing root `.env` or explicit environment into an
encrypted Credential before runtime starts. Doctor, Docker Compose, Helm, Gateway, Scheduler, and
Worker must not broadcast provider keys as process-wide model configuration.
Backend module role and database-operations Make targets use
`scripts/run_runtime.py` to load non-provider root `.env` settings explicitly;
caller-supplied values win, and ambient model-provider API keys are removed
before the target process starts. `setup-db` remains the sole exception because
it consumes the initial provider key exactly once and persists its encrypted
Credential.

Secret values must come from environment-backed configuration and must be separated by domain:
Auth, Credential encryption, audit/quota HMAC, and database passwords cannot reuse material.

Project-managed IM providers are not process-wide `config.yaml` credentials. Gateway stores one
live `project_channel_instances` row per `project + provider`, pins an exact encrypted project
Credential version through `project_channel_credential_bindings`, and acquires a fenced
`project_channel_instance_leases` single-writer lease before starting the adapter. Messages,
member connections, OAuth states, callbacks, and outbound replies carry the exact
`channel_instance_id`; provider name alone is never sufficient for a project-managed instance.
The nullable instance ID on historical connection/state rows is reserved only for the explicit
deployment-config compatibility path. Admin updates and Secret rotation use the imperative
project channel API, while members retain separate owner-scoped connect/disconnect authority.
`channel_connections.*` is reserved for nullable-instance deployment compatibility and must not
gate listing, readiness, or member binding for an exact database-backed project instance.
Project group bindings persist only domain-separated HMAC references for group and sender IDs.
Guest conversation, topic, and provider response-alias rows use the bound group's retained HMAC
generation as well; raw provider coordinates remain transient for delivery only. Concurrent first
messages derive one deterministic private Thread ID and converge through database uniqueness rather
than holding a second advisory-lock connection, so `pool_size=1,max_overflow=0` remains supported.
Disabling or deleting a binding freezes every derived guest principal and connection; inbound
admission must fail closed until an enabled binding resolves exact project, instance, Agent, and
guest-owner authority. Personal `p2p /connect` behavior remains unchanged.

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
POSTGRES_TEST_URL="postgresql+asyncpg://.../postgres" make test
uvx ruff format --check .
uvx ruff check .
```

Production async paths must offload synchronous filesystem/subprocess work and await
cancellation-settled cleanup. `make detect-blocking-io` remains a static review aid. Test doubles
belong under `tests/support/`; do not add a production persistence or execution fallback to make
a test easier.

Python is 3.12+, Ruff uses double quotes and a 240-character line limit, and all public/domain
interfaces should use precise types. Keep public errors stable and free of SQL, connection,
credential, private resource, and exception detail.

Historical pass counts do not certify the current checkout. The complete current core suite is
`POSTGRES_TEST_URL=... make test` from the repository root. Its database cases use owned random
`deerflow_test_*` databases and must finish with zero skips.
