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
| Agents, Skills, MCP, domain secrets | Governed assets                          |
| Knowledge bases, RAG, retrieval  | Knowledge (optional RAG module)             |
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
configuration. Public readiness exposes component state only—never PIDs, lock
keys, credentials, URLs with secrets, private identifiers, or content.

## Where changes live

Run full-stack commands from the repository root and backend targets from
`backend/`; use the applicable `Makefile` as the current command index.

- `app/<domain>/` owns application composition, HTTP admission, server-issued
  contexts, and domain transactions.
- `packages/harness/deerflow/<domain>/` owns reusable graph, runtime,
  persistence, sandbox, Skill, MCP, and subagent primitives.
- `packages/knowledge/actweave_knowledge/` owns the self-contained RAG
  Knowledge module; `app/knowledge/` owns its host adapters (config mapping,
  Gateway routers, Worker handlers, Agent tool, authority seam, model port).
  `app/model_registry/` owns the host-level Model Provider registry
  (admin routes, provider/retrieval-model service, provider-secret envelopes,
  and the optional install-time default-provider bootstrap). It administers
  the only API Keys in the model domain and stays administrable while the
  Knowledge module is disabled; a provider key or endpoint change fans out
  re-encryption to every bound text model inside one transaction
  (`app/system_settings/provider_key_fanout.py`).
- `scripts/` owns explicit Schema V1 setup and operator workflows.
- `tests/` owns unit, PostgreSQL, process, and contract gates.

The dependency direction is `app.* -> deerflow.*` and
`app.knowledge -> actweave_knowledge`; harness code must never import `app.*`,
and `actweave_knowledge` must never import `app.*` or `deerflow.*`.

## Non-negotiable boundaries

### Authorization and transactions

- Authority starts from authenticated identity plus a server-issued immutable
  `ProjectContext`. `system_admin` is not project membership.
- Owner-private work uses a server-issued `PrivateWorkContext`. Every private
  query and composite relation binds `project_id + owner_user_id`.
- Project channel configuration, group bindings, and owner-attributed channel
  connection state require `project.channels.manage`; member private-work
  capabilities never grant access to the project Connections surface or API.
- Never accept project, owner, membership, capability, Run snapshot, secret
  authority, Job, lease, or internal runtime authority from request metadata,
  model/tool arguments, or ambient globals. Revalidate project, membership,
  capability, resource state, and Job/Run lease inside the transaction that
  performs each side effect.
- Project outsiders, wrong owners, stale membership, and missing private
  resources collapse to `404`; a current member lacking a capability receives
  `403`, except where an existing route family deliberately hides both.
- Repositories do not commit unless they explicitly own the complete operation.
  Preserve the established Project -> Membership -> resource lock order. Agent
  Builder operations extend it with session advisory fence -> turn operation ->
  design session; cancellation terminalizes active turn operations and clears
  their generation profiles in the same transaction.
- PostgreSQL RLS is not used. Isolation depends on immutable contexts, scoped
  repositories, composite constraints, and a non-superuser app role.
- Public request/response models reject unknown authority fields. Copy the error
  and strictness convention of the neighboring route family.
  Project responses expose the persisted `ProjectRow.created_at` as read-only
  metadata; creation and update requests cannot supply or replace it.

### Authentication, secrets, and public contracts

- Account email is normalized with `strip + lowercase` on every path and is
  protected by the database's case-insensitive unique index. Login also accepts
  a required username: 3–32 characters, starting with a letter, `[a-z0-9_]`
  only, stored lowercase, unique among human accounts.
- Browser tokens remain valid only while signature, session ID, token version,
  and the durable auth-session row all validate. Logout and password change
  revoke durable authority, not just browser state.
- Passwords, JWTs, CSRF values, raw session IDs, API keys, secret plaintext,
  nonces, ciphertext, storage locators, and full connection URLs never enter
  logs, traces, public responses, snapshots, audit metadata, or browser caches.
- Runtime Skill and MCP plaintext materialization belongs to the Worker
  execution boundary and the exact authorized Sandbox, child process, or remote
  call. The only Gateway materialization path is an authorized,
  transaction-serialized compatible Candidate/Version copy that immediately
  re-encrypts an independent Generation for the new recipient. Output masking
  is accidental-leak protection, not DLP; secret-bearing Skills are trusted code.

### PostgreSQL schema and persistence

`deerflow/persistence/full_schema.sql` is the complete source for fresh installs;
the current marker is `schema_v1`. Fresh setup runs that schema directly and
stamps the V1 marker. Runtime processes never create, migrate, stamp, repair,
or downgrade an application database. Schema V1 has no migration ancestry:
unknown markers, an unversioned nonempty schema, and catalog drift are
recreated explicitly, never repaired in place.

- `make setup-db` accepts a new empty target only. `make reset-db` is an
  explicit destructive operator action (never a runtime startup step) that
  rebuilds only the exact `DATABASE_URL` target's `public` schema and requires
  the exact database name as confirmation. `make check-db` is read-only
  readiness evidence. `make upgrade-system-assets` is an explicit idempotent
  maintenance-window action; changed System Skill behavior must ship under a
  new Skill identity. `make prepare-run-event-partitions` is an explicit,
  idempotent operator action for UTC months N through N+2.
- Fresh setup seeds Runtime Policy schema v1 (the consolidated pre-release
  baseline; distinct from the PostgreSQL DDL Schema V1 marker). It is the only
  supported policy schema: stored rows and Run snapshots declaring any other
  number fail closed, and no upgrade path exists. Internal tool-call limits
  seed at `200` for the Lead per Run and `50` for each Sub-Agent Task; a `task`
  delegation is a Lead tool occurrence, the Lead count persists across Graph
  Turns, each Task execution owns a separate count, and there is no additional
  Run aggregate. Automatic summarization has exactly one trigger,
  `summarization.trigger_tokens` (seeded at `320000`; null disables).
  Post-summarization retention (`summarization.keep`) is token-count-only and
  seeds at `64000` tokens; the manual compact API accepts only a token keep,
  while manual compaction, Seal, and Dream keep the internal `("messages", 0)`
  archive-all sentinel below the policy layer. Every site that freezes the
  trigger next to a known model capacity clamps it to that model's
  `max_input_tokens`, so a small-context model cannot sit in the dead zone
  where the capacity guard rejects before the trigger is reachable. These
  defaults belong to `default_policy_value()` and must not rewrite already
  admitted Runs or existing immutable policy versions.
- Every initialized application table and column has a checked-in Chinese
  comment; `run_events` partitions copy the parent comments. Run
  `uv run python scripts/generate_schema_comments.py --check` after schema edits.
- A schema change updates the ORM registration, `full_schema.sql`, catalog
  signature/digest, required relations, V1 marker, and schema tests together.
- Application metadata and durable state live in PostgreSQL. File/artifact bytes
  may live in configured storage, but access, identity, version, and scope remain
  database-authoritative.

### Jobs, Runs, streams, checkpoints, and files

- Gateway admits executable work; Worker executes it. Business state, Job,
  quota reservation, snapshot, and audit rows commit atomically at admission.
- Worker stores raw lease tokens only in memory. Every append, tool side effect,
  and terminal settlement validates the exact current lease in its transaction.
  Production checkpoint `aput`/`aput_writes` also validates the exact
  Job/Run/Attempt lease before and after the raw write on the same psycopg
  transaction; an earlier application-connection preflight is not write
  authority. The only cancellation exception is the typed delegation-ledger
  terminal settlement, which admits ordinary Stop but never authorization
  revocation.
- Run admission freezes an exact Agent/model/Skill/MCP payload closure plus
  exact domain-secret Generation references. Retry and resume use that closure
  rather than current catalog pointers.
- Sub-Agent Task lifecycle ownership is process-wide and lives only in
  `deerflow.subagents.lifecycle`: scheduler gate, internal UUID registry, graph
  task/future, cancellation, timeout arbitration, usage receipt, quiescence,
  and reaper. `task_tool` is a wire Adapter; the internal graph runner must not
  grow `start/get/cancel/cleanup` lifecycle APIs.
- Each Agent graph binds one explicit SDK, Embedded, configured Lead, or Private
  profile factory. Each task invocation creates a per-call parent binding from
  that factory plus live runtime authority; never infer the profile from
  `private_scope` or reconstruct SDK model/tools/middleware from global config.
  Expensive materialization occurs only after scheduler admission and counts
  against the execution budget.
- A Private Run's `Run Workload Profile` is a server-owned `interactive` or
  `research` value frozen at admission and inherited by hidden Graph Turns, Job
  Attempts, and delegated bindings. Request metadata, model output, and tool
  arguments cannot choose or upgrade it; SDK/Embedded callers choose only their
  own single invocation profile.
- Every automatically assembled Lead, SDK/Embedded, and delegated Agent Graph
  has exactly one `ToolCallControl`. It alone owns repeated-call identity,
  complete-batch arbitration, replay receipts, and the correctly bound internal
  tool-call count: the Lead binds one Run-stable count (including `task`
  calls), each delegated Task execution binds its own count, and parallel
  Tasks stay independent with no extra Run aggregate. Reaching a limit rejects
  later internal calls only in that exact binding. `SubagentLimitMiddleware`
  remains a separate policy; register only one control middleware per graph.
- The public SDK graph adapter generates a fresh internal ToolCallControl scope
  for every top-level `invoke`, `stream`, `ainvoke`, and `astream` call while
  preserving a copied caller context; callers never supply or reuse that scope
  key. Synchronous SDK model calls are permitted only without Private scope or
  authorization authority; those contexts fail closed.
- With automatic ToolCallControl active, an unanchored SDK `extra_middleware`
  that implements `after_model` or `aafter_model` belongs to the protected
  Custom response band and cannot use `@Next`/`@Prev`; arbitrary response
  ordering requires explicit `middleware=` full takeover. Position-only extras
  without response hooks keep their documented anchors.
- Parent-to-child runtime propagation is one opaque
  `DelegatedRuntimeContextProjection`. It alone selects and copies inherited
  identity, preserves channel-identity absence versus explicit clear, applies
  Private-profile gates, and adapts loop-affine authority; the graph runner
  must not reconstruct raw runtime-context keys or accept the corresponding
  authority fields separately.
- The process scheduler's execution deadline is independent from owner-loop
  observers. Its concurrency lease remains held through cancelled thread work,
  inherited-operation quiescence, and usage settlement; a timed-out child still
  consuming resources cannot admit a replacement into the same slot. Admission
  reserves one process slot from any single parent Run and rotates queued Runs;
  queue wait is logged as a bounded structured record in production JSON and
  text output, warns after five seconds, and always warns when the queue budget
  expires. The first terminal decision owns timeout logging, independent of
  whether the scheduler loop or caller loop observes the deadline first.
  Every admission lease retains its exact scheduler epoch and gate. Replacing a
  dead scheduler loop retires the old gate, builds a new epoch, and reaps live
  records even when shutdown occurs without another submission. Retired-loop
  cleanup cancels the source task on its owning loop, waits for the real graph,
  finalizer, and inherited operations to quiesce, then closes that loop; it
  never fabricates a quiescence receipt. The default executor is epoch-owned,
  and a late old finalizer can release only its own retired gate.
- A Private Sub-Agent Task cannot return until its graph and inherited parent
  operations are quiescent; Worker drain closes the lifecycle and joins
  detached Run handlers before tearing down checkpointer/store resources.
  Aggregate ToolMessage usage is replay-idempotent by internal execution
  receipt; detailed records settle once into the parent Run Journal on its
  owner loop, and pre-receipt checkpoint messages are not re-attributed.
  Conflicting receipt values create a persistent transcript tombstone on every
  occurrence-owning turn; TokenBudget hard-stops only when that conflict is
  new to the Run, including terminal paths that reach `after_agent` without
  another model hook. Provider output truncation preserves usable partial text
  but stamps the additive `output_truncated` stop reason; ordinary parent Stop
  terminalizes still-running entries as cancelled before Run settlement, so a
  later Lead never receives stale "already delegated" guidance.
- Durable events are committed before notification. PostgreSQL `NOTIFY` is only
  a wake-up hint; correctness comes from scoped reads, monotonic cursors, and
  one durable terminal outcome.
- Stored stream events are immutable, but reconnect replay may omit root
  `values` frames below the connection's frozen full-state horizon. Event ids
  stay monotonic with gaps; namespaced subgraph frames, non-`values` frames,
  the horizon frame, the live tail, and the terminal are never dropped, and
  consumers must not require contiguous ids.
- A completed Lead Provider response is flushed to the RunJournal while the
  execution lease can still authorize message writes; later cancellation or
  Graph rollback must not erase already observed user-visible history. Root
  tool-argument deltas publish in bounded, byte-equivalent batches so a
  degenerate argument stream cannot delay the terminal Run frame. An
  unrecoverable Provider output-limit receipt becomes authoritative only after
  that response barrier; its durable error terminal wins over a later ordinary
  user Stop, while explicit rollback and authorization revocation keep their
  stronger precedence. The Worker first stores that fact as an internal
  terminal candidate, not a public `stream.end`; Job/Run settlement re-locks
  the authorities, applies `ordinary Stop < durable response < authorization
  revocation`, and writes the one public terminal in the same transaction.
  After revocation, only a transaction-local typed capability issued by the
  exact locked Run Settlement may omit the executable-governance recheck; it
  remains bound to the owner scope, Job, Run, Job Attempt, lease, and lineage.
  Executor and Job settlement carry this as an explicit durable-terminal fact,
  never an error-code-string inference. Expired Attempts and precisely eligible
  dead Jobs may recover only from that candidate or a retained legacy terminal;
  dead-Job recovery creates a settlement-only successor, preserves predecessor
  lineage (and rebinds an Automation occurrence when applicable), and must
  never invoke the Agent Graph again. Fallback discovery uses bounded
  `(dead_at, job_id)` keyset pages with process-local, engine/scope-isolated
  progress and an end-of-scan wrap; an ineligible first page must not starve a
  later proven terminal. PostgreSQL locks and unique successor lineage remain
  the cross-process correctness authority.
- A loop hard-stop owns one private-state, tool-free finalization turn; its
  Run-scoped semantic recorder authorizes durable suppression of unexecuted
  proposals on success and error paths. A loop-capped Lead Run is terminal
  error `LOOP_SAFETY_LIMIT`; answers or files already produced are partial
  results. Lead graph-step exhaustion settles as nonretryable
  `GRAPH_RECURSION_LIMIT`, preserving that known cause even after side effects
  without clearing Retry Safety or claiming successful File Finalization.
  Recovered LLM retry attempts live only in the bounded, redacted
  `run.recovered_issue` trace (closed caller/subtype plus bounded HTTP status;
  never raw exception text, provider bodies, or URLs). The receipt is never
  attached to an `AIMessage`, never projected into conversation history, and
  never overrides the durable Run terminal.
- Harness Execution resource ownership transfers exactly once at runner entry.
  After transfer, `run_agent()` alone joins File Finalization and
  private-resource cleanup, and only after cleanup, approval sealing, and
  durable stream terminal publication may it return an immutable
  `RunAgentOutcome`. The executor maps that outcome plus lease/cancellation
  facts and must not infer semantic success from the mutable `RunRecord`. Once
  the assistant response and required file finalization are durable, teardown
  failure is a Worker operational fault and never downgrades the successful
  business terminal.
- Job settlement distinguishes domain/lease conflicts, database outcomes whose
  commit acknowledgement is unknown, and programming invariants. A connectivity
  failure after commit starts abandons only that exact Attempt for durable
  recovery and must not stop sibling Runs; invariant violations remain loud at
  process level and are never hidden by a broad catch.
- Public event and message sequence values remain canonical signed-BIGINT
  decimal strings at browser boundaries; never coerce them to JavaScript
  numbers.
- Run settlement reaches the durable stream through the typed
  `stream_terminal_status_for_run_settlement` adapter: a successful Run always
  writes `completed`. `canonical_stream_terminal_status` only normalizes stream
  spellings, so retained pre-cutover `success` frames stay readable and
  idempotent terminal settlement treats them as semantically equal instead of
  failing the settlement.
- Checkpoint mode is process-frozen: all Gateway and Worker processes sharing a
  database use the same full/delta settings and restart together. Consumers
  materialize state through the scoped checkpointer; raw delta channels are not
  a complete-state API. `CheckpointStateAccessor.replacement_values()` is the
  canonical whole-state replacement mechanic for both modes: source values win,
  current-only channels reset to their effective schema default, unknown
  channels fail closed, and reducer/delta channels use `Overwrite`.
- Current-message files and images are admitted from server file authority.
  Worker retry revalidates the frozen metadata; missing or changed attachments
  fail closed rather than degrading silently.
- Runtime-only dependency environments belong under `/tmp`. The secure
  finalization scan prunes only the exact top-level workspace `.venv` tree;
  other workspace/output symlinks and special files still fail closed.
- Each private Sub-Agent Task receives a distinct server-generated
  `workspace/.deerflow/subagents/<scope>/outputs` view; its canonical
  `/mnt/user-data/outputs` paths map only to that view, so parallel Tasks and
  Lead output writes cannot overwrite one another. Delivery requires a Lead
  copy plus `present_files`; Lead-only promotion mappings stay in model-visible
  Task results, and public Sub-Agent card metadata never carries scratch paths
  or publication instructions. Scratch is cleared before restore, finalization,
  failure settlement, and lease release, and is never persisted as a file,
  Artifact, or workspace change. Private Sub-Agent `bash` remains fail-closed
  until a provider supplies a real per-Task filesystem namespace; command
  rewriting is not an isolation boundary.
- Conditional composer cleanup takes the same Thread lock as Run admission. It
  retains uploads present in any frozen current-upload snapshot, while
  admission rejects the whole request if any selected upload is no longer ready.

### Governed assets

#### Packaged System assets

- `skills/public/*` is the sole source for packaged System Skills. Regenerate
  and verify the authenticated catalog with
  `uv run python -m scripts.generate_public_system_skill_catalog --check`.
- Packaged System Agent/Skill/MCP definitions are bootstrap-only and immutable
  at runtime; global admin definition routes are read-only, and Projects
  configure their own secret values after binding exact versions. A packaged
  System Agent has one deterministic identity and `definition_id` replaceable
  only by the explicit System Asset Upgrade path. A packaged System Skill has
  one deterministic v1 exposed through `current_version_id`; a changed payload
  is rejected and must ship under a new identity. Same-byte bootstrap is
  idempotent, and Project bindings store only the asset identity.
- Server-owned Builder Agents are absent from every regular project,
  global-admin, and runtime System Agent catalog, including direct definition
  lookup; only bootstrap and the dedicated internal resolver may address them.
- Retrospective denial of a packaged Skill's immutable v1 bytes uses explicit
  System governance revocation: new bindings and Run Admission reject a revoked
  v1, while already admitted Runs keep their immutable Run Snapshot. Same-byte
  bootstrap preserves revocation; changed authenticated bytes clear it.

#### Project assets and domain secrets

- A Project Agent owns one mutable Definition on the Agent aggregate.
  Instruction and capability saves replace that Definition under optimistic
  `revision`, rotate its opaque Definition identity, and immediately affect
  later Run Admission; the API exposes no Candidate, activation, or history
  lifecycle. Project Skill/MCP versions remain immutable: creation saves a
  suspended asset plus Candidate Version v1, activation atomically sets
  `current_version_id`, and skipped or older versions become Historical.
- Project Skill creation happens only through a validated archive upload or an
  AI Builder commit; there is no metadata-only or template-create API, and
  browser upload never moves `current_version_id`. All lifecycle flows validate
  structure and integrity without a static content-security classification
  (the legacy scan columns are inert). Archive structure, `SKILL.md`,
  frontmatter, content checksums, compatibility, runtime-secret readiness,
  optimistic revision, and runtime-name uniqueness remain authoritative.
- Skill export is a read-only, audit-required distribution operation over one
  exact persisted version: Project exports require `shared_assets.edit`,
  System exports require the eligible Current Version. The deterministic
  root-layout ZIP excludes root `evals/`, any `node_modules/` or
  `__pycache__/`, `.DS_Store`, and `*.pyc`, and never includes secret values,
  lifecycle state, or version history.
- A Skill's `SKILL.md` is the sole authority for `required-secrets` and
  `secrets-autonomous`. Every consumer uses the canonical parser; form edits
  patch only those managed frontmatter fields and preserve the rest.
- Project Skill secrets belong to one exact Skill Version and declared target
  environment name. Historical values are read-only except an explicit
  administrator clear for revocation; saving a forward Candidate re-encrypts
  only compatible values; activation requires every required declaration
  configured. Secret values never enter Skill bytes, public readiness, audit
  metadata, or API responses.
- Project Skill deletion is an irreversible transition to `archived` in one
  Project-governed transaction: it hides the Skill, removes every direct Agent
  Skill reference, advances each affected Agent Definition without changing
  Agent status, and records the affected count. Archived Skills leave ordinary
  reads and the uniqueness namespace but retain their Current Version pointer,
  every Version and file, quota reservation, Secret state, Generations, and
  ciphertext for the Project lifetime; only final Project deletion destroys
  that content. No Skill-scoped physical purge or periodic reconciler exists.

#### Runtime admission and MCP

- Runtime-visible Skill names are unique case-insensitively within a project,
  across active Project Skills and enabled System Skill bindings. Activation
  and binding enable enforce the inverse checks under the project lock; Run
  resolution rejects any legacy conflicting closure.
- Every Run Admission (including later Thread messages, Automation, Channel,
  edit, regeneration, and fork paths) resolves the current Agent Definition and
  Skill Current Version, then persists an immutable referential Run closure:
  exact Agent/MCP payloads, Skill version-4 manifests with database-protected
  exact Version pins, checksums, domain-secret Generation references, and
  policy. Worker execution and retries read only that exact closure and never
  reread Current Versions. Unrelated catalog changes do not invalidate an
  admitted Run; revoked membership, capability, binding, secret Generation, or
  lease fails at the applicable execution boundary.
- Every Skill materialization control/fingerprint check and readonly-mount
  publish fence is one transaction ordered as shared `Project → Membership`,
  then `Job → Run → exact active Attempt`, binding the claim Attempt to the
  trusted Worker identity.
- A project Skill is passive until explicit slash activation or verified
  reading of its admitted `SKILL.md`. Active policy restricts model schemas,
  execution, and `tool_search`; stale or malformed evidence fails closed.
- Project MCP authoring accepts only supported remote HTTP/SSE definitions
  under the configured CIDR policy. Encrypted values bind to the exact MCP
  Version and slot; one slot may declare Header fields, Query fields, or both
  and is replaced atomically. Worker disables redirects and ambient proxy
  trust, revalidates the target and closure for discovery/calls, and treats
  inventory as diagnostic, not execution authority. After exact closure and
  secret materialization succeed, a remote discovery transport/catalog failure
  is isolated to that MCP for the current Run with a stable secret-free
  capability notice; authorization, snapshot, database, and secret
  materialization uncertainty still fail closed.
- System MCP may retain packaged stdio, header, and OAuth capabilities; do not
  copy that broader trust model into project-authored MCP. A System MCP OAuth
  token endpoint passes the same frozen endpoint policy as its transport and
  uses the same explicit egress proxy, bounded timeout, disabled ambient proxy
  trust, and `follow_redirects=False` client before any client secret is sent.

#### Project Agents and Builder

- Project Agent logical `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md`
  are fields of its single persisted Definition, not files in this repository.
  Payload checksums are recomputed at resolution and Worker materialization;
  legacy schema v1 fields outside the historical digest must remain empty.
- Incomplete Agent Builder pagination uses an opaque, immutable
  `created_at + id` keyset (`updated_at` is presentation only). Each
  project/owner retains at most eight incomplete sessions, enforced under the
  create-admission lock after idempotency replay. Commit's optional slug is
  normalized, joins the idempotency checksum, and becomes the Agent
  slug/display name. `AGENT_DESIGN_SLUG_CONFLICT` and
  `AGENT_DESIGN_CONFLICT_UNRESOLVED` are 409 domain errors;
  `AGENT_DESIGN_SESSION_LIMIT_EXCEEDED` (429) instructs the owner to resume or
  cancel an existing design. Builder HTTP responses default to the strict v1
  shape; the current frontend explicitly requests `contract_version=3`. Agent
  Design Activity is a dedicated owner-private append-only table and SSE
  cursor, never a Thread/Run event; its payload is a closed public projection
  and cancellation removes it with the session's private draft content.
- Skill Builder keeps Run/Job/Worker settlement authoritative and projects only
  provider reasoning plus allowlisted safe stages into its separate
  owner-private Activity stream. Each model turn captures one immutable draft
  baseline shared by all Job attempts; stop/failure settlement restores it
  before writing the unique terminal, and whole-session cancellation completes
  that flow before clearing Activity and draft content. Commit success is
  atomic with Skill/Candidate Version, while a rolled-back Commit records a
  separate failed terminal projection; authoring-owner reads with edit
  capability fail stale in-progress Commits and restore a stranded
  `committing` session to its still-validated candidate.
- Project Agent DELETE is a soft archive. It retains the Agent tombstone and
  all Thread/Run/Automation/Channel/OAuth references, atomically clears a
  matching project-default pointer, hides the Agent from catalogs, and rejects
  new Run admission with `PRIVATE_WORK_AGENT_ARCHIVED`. Earlier exact Run
  Snapshots keep their Definition payload; suspended Agents remain
  fail-closed, and an archived slug may be reused by a new Agent.

### Knowledge (optional RAG module)

- Knowledge is disabled by default. The host-owned `knowledge_system_settings`
  singleton is its only configuration source; `/api/admin/settings/knowledge`
  requires a current `system_admin`, revision CAS, secret-envelope encryption,
  and a transaction-free MinIO probe before an enabled setting can commit.
  Changing the endpoint requires resubmitting the secret. Setup seeds the row;
  runtime never repairs a missing row. Nonempty YAML `knowledge` is rejected;
  use the explicit migration workflow in `Install.md` for legacy configuration.
  Gateway/Worker restart together for storage, limits, and cache changes;
  the text System Model reference for summaries is resolved per task.
  The knowledge routes are always mounted but answer 404
  `KNOWLEDGE_DISABLED` while the module is absent; a disabled module also means
  the Worker registers no knowledge task handlers and no Run receives the
  `knowledge_search` tool. The Worker's Project-retention cleanup capability is
  composed independently and remains active while the feature is disabled.
  If historical Document rows or a `delete_document_object` task not in
  `succeeded` remain but MinIO configuration was removed, Project purge fails
  closed and retries; operators must restore the original endpoint, bucket,
  and credentials until those rows and objects are purged.
  When enabled, Gateway and Worker startup verify the configured MinIO bucket
  is reachable and unversioned. A failed storage check closes the feature
  module and reports `knowledge=unavailable` in Gateway readiness without
  preventing other features or administration from starting. Retention still
  fails closed for missing storage evidence. `Enabled`/`Suspended` versioning,
  Object Lock, or missing `GetBucketVersioning` authority are rejected. The bucket is
  administrator-provisioned and never auto-created. Members lacking a knowledge
  capability receive 403 `KNOWLEDGE_FORBIDDEN`; outsiders and missing projects
  collapse to 404.
- `actweave_knowledge` stays host-agnostic: the host supplies engine, secret
  cipher, and configuration through `app/knowledge/` adapters. Do not import
  `app.*` or `deerflow.*` from the package.
- The eight package-owned `knowledge_*` tables plus the host settings singleton
  are ordinary Schema V1 members: ORM,
  `full_schema.sql`, catalog digest, Chinese comments, and schema tests change
  together, and `public.vector` (pgvector) must exist before install.
  PostgreSQL owns all metadata, task, and segment authority; MinIO owns only
  document bytes, keyed by database-issued storage keys.
- Embedding/Reranker model configurations are PostgreSQL-administered and use
  their Model Provider's encrypted API key (shared secret-envelope
  infrastructure); the same provider row also carries the key for its bound
  text models. Keys are write-only through the admin API and never appear in
  responses, logs, or the browser; a configuration referenced by knowledge
  bases cannot be disabled or deleted, and a provider with bound text models
  or retrieval models cannot be deleted. Admin routes require `system_admin`; project routes
  require membership plus `shared_assets.read` for reads and
  `shared_assets.edit` for writes; the search API and Agent tool return only
  segments of the caller's project.
- Summary indexes are opt-in per Base. A `summarize_document` task shares the
  single open versioned-task slot with ingest/reembed, but never changes a ready
  Document to failed. The host materializes one active text System Model per
  call and uses private `ModelRuntime` invocation, with SDK retries disabled;
  durable task retries re-enter the lease guard. Each LLM/Embedding dispatch
  and summary publication rechecks Project state and lease. Publication also
  checks document version, Base binding/flag, and source digests; changed sources
  are skipped and admitted for a follow-up refresh. Re-embed preserves summary
  text and refreshes vectors atomically; reparse cascades old summaries away.
  Summary vectors are the semantic route's third source, deduplicated by each
  Segment's maximum cosine before the existing per-base cap. They never replace
  real Segment text in reranking, citations, ToolMessages, or logs. Details alone
  expose the separately labeled generated text and time.
- An empty Knowledge Base may leave `embedding_model_id` NULL and retains the
  normal status values. Its first configuration uses PATCH to bind Embedding
  and save retrieval settings together, under the Base lock, only while no
  documents exist. Reranker binding requires Embedding. Upload admission rejects
  unconfigured bases before reserving a document or writing an object; search
  excludes them before model loading and candidate budgeting. Once configured,
  Embedding changes use rebuild; PATCH cannot replace or clear that binding.
- Ingestion, document deletion, base deletion, and exact-key orphan cleanup run
  as `knowledge_tasks` claimed by the Worker under lease semantics. Project
  purge is a separate retention hook that stays composed when Knowledge is
  disabled. Extraction
  covers PDF, DOCX, TXT, MD, CSV, XLSX, HTML, PPTX, and EPUB (source
  positions mark page/paragraph/row/slide/chapter) and enforces a total
  character budget and archive pre-checks. DOCX traversal includes tables in
  document order, groups cell text into row blocks with table/row positions,
  and counts merged cells only once. Splitting is
  recursive-separator based: the user separator (escaped form, default
  `\n\n`) leads a fixed fallback sequence, optional pre-processing rules
  (`remove_extra_spaces`, `remove_urls_emails`) clean text before splitting,
  and all chunk parameters freeze on the document row at upload so retry
  reuses them. The `parent_child` chunking mode second-splits each parent
  into `knowledge_segment_children` rows: only children carry embeddings
  (parent rows store NULL), publish is single-transaction and
  version-checked, and segment governance re-splits children on edit/add.
  Chunk preview runs synchronously in the Gateway through the
  same extract→clean→split path (stateless, no rows, no queue, temp file
  removed per request) and nests child contents in parent_child mode. The
  per-document vector-entry budget also applies to manual Segment create/edit:
  general counts parent Segment rows and parent_child counts child rows, with a
  pre-Embedding check plus a final check under the Document lock. Deletion
  removes MinIO objects before releasing database rows and records
  `delete_error` for operator-visible retry. A late upload put whose row was
  already removed receives a durable `delete_document_object` task carrying the
  exact server-issued storage key. When its Base still exists, cleanup also
  recreates a `deleting` Document tombstone so terminal failure remains visible
  and the normal user delete action can retry it. The exact-key handler validates
  Project/Document scope before deleting; final Project retention still sweeps
  the entire trusted Project prefix as a fallback. A recent `uploading` row
  defers Project purge without touching storage or rows. An upload older than
  the one-day settlement grace is first converted to `deleting` plus exact-key
  cleanup and only a later retention attempt may delete it. PostgreSQL time is
  authoritative; one day is well above normal MinIO transfer/retry settlement
  and well below the fixed 30-day Project retention window.
  Project deletion admission also fences Knowledge execution: after claiming,
  the Worker checks Project `active` state under the same transaction and a
  Project share lock. A pending-deletion claim returns to `retry_wait` for 60
  seconds without spending an attempt, so restore resumes it automatically and
  no handler starts during the retention window. Indexing handlers repeat the
  Project and unexpired-lease checks before every Provider request/retry and
  in the publish transaction. An in-flight Project deletion defers the claim
  only after started work settles, without consuming an attempt; expired
  claims cannot renew or publish and remain owned by expired-claim recovery.
  Renewal and settlement compare database time after acquiring the Task lock;
  publish repeats the check after acquiring the Document lock.
  Before destructive Project
  cleanup, the purger recovers expired claims and locks every Project open Task;
  a live `running` Task defers the attempt, while `queued|retry_wait` rows are
  removed before objects or relationship rows.
  Knowledge supports only an unversioned MinIO bucket: startup health, every
  upload immediately before its PUT, and every destructive storage path require
  bucket versioning to be absent/`Off`. `Enabled` and `Suspended` (and therefore
  Object Lock) fail closed. Credentials must allow object get/put/delete,
  `GetBucketVersioning`, and listing the configured Knowledge prefix.
  Every accepted upload is hard-capped at 50 MiB and `fput_object` is forced to
  one PUT. Because the MinIO Python SDK buffers that part in process memory,
  each `MinioObjectStore` has exactly one upload slot. Do not raise the cap,
  remove the slot, or re-enable multipart upload: an interrupted multipart
  session is not returned by ordinary `list_objects`/`remove_object` cleanup
  and would invalidate Project-purge evidence.
  Worker timeout cancellation joins any already-started
  parser or MinIO SDK call before releasing the claim, stopping cleanup and
  retry from overlapping that work; a synchronous call that never returns can
  therefore exceed the nominal timeout and requires process recovery rather
  than an unsafe concurrent retry.
- Segment and document governance runs synchronously in the Gateway: document
  rename and batch enable/disable/delete (all-or-nothing, bounded batch), plus
  segment edit/add/toggle/delete on `ready` documents. Content edits and
  manual additions re-embed with the base's model configuration before the
  write transaction, which re-checks the document version and answers
  `KNOWLEDGE_CONFLICT` when a re-ingest or delete won the race. Every actual
  edit/add Embedding batch and internal retry revalidates the caller's
  Project authority in a short transaction before dispatch. A disabled
  document or segment is excluded from retrieval candidates without touching
  its vectors; `word_count` tracks segment characters and aggregates onto the
  document row.   Per-base metadata field definitions
  (`knowledge_metadata_fields`, string/number/time) validate typed values
  written into `knowledge_documents.doc_metadata` (JSONB, GIN-indexed);
  field rename and delete rewrite document keys in the same transaction.
  Batch metadata assignment (`PATCH .../documents/metadata`) writes only the
  explicitly submitted fields across the selection in one all-or-nothing
  transaction; field discovery (`GET .../filter-fields`) returns builtin and
  custom definitions with `field_kind`, and the read-only
  `knowledge_metadata_fields` Agent tool returns definitions only, never
  values. Reprocessing is two explicit entry points with distinct
  preservation contracts: base rebuild (re-embed) rebinds the embedding
  Provider Model and queues `reembed_document` tasks for published documents
  only (admission reports accepted/skipped counts) — the handler is
  constructed without extractor or object-store access, keeps Segment
  UUIDs/text/positions/enabled states/manual edits, regenerates vectors, and
  flips the whole generation in one version-checked publish; document reparse
  (`POST .../reparse-preview` + `POST .../reparse`) re-splits the stored
  original file with edited parameters under an `expected_version` CAS,
  freezes the parameters on the task, writes them back to the document row on
  publish, and replaces every segment row (manual edits and disables are
  overwritten). Retry inherits the latest indexing task's kind and frozen
  reparse settings. Indexing tasks record real progress (stage, verified
  batch counts, attempt) that projects to the API only for the current
  document generation; ingest and reembed embedding loops re-check authority
  before every provider batch so lease loss or revocation stops undispatched
  work.
- Every Project-facing Knowledge read carries the server-issued Project
  authority into the transaction that reads bases, metadata, Documents,
  Segments, or query history; request-admission context alone is never read
  authority. Model options come from the host model registry, not from a
  Knowledge-owned table. Health probes revalidate before and after external
  storage I/O. Document download revalidates in a fresh transaction after the
  MinIO copy and before the Gateway can return the staged file.
- Retrieval recalls with pgvector exact cosine similarity, grouping bases by
  their `(embedding model, reranker model)` pair. A group with a reranker
  scores every recalled candidate and its `[0,1]` relevance score is the
  final score; a reranker failure fails the search rather than silently
  degrading to cosine order. A rerank-free group keeps raw cosine similarity
  (`[-1,1]`) as the final score.
  Recall merges two paths: general segments carry their own vectors;
  parent_child documents recall through child chunks whose best score rolls
  up to the parent (one candidate per parent, deduplicated). `top_k` and
  `score_threshold` omitted by the caller resolve from the per-base defaults
  stored on `knowledge_bases`. Optional `metadata_filters`
  (eq/contains/gte/lte against defined fields, AND-combined, max 10) gate
  both recall paths and are validated against the base's field definitions
  before SQL.
  Per-base candidate budgeting caps every base at
  `C = min(min(100, max(20, 5*top_k)), floor(400/N))` for N target bases and
  rejects the search when `C < 1`. Final ordering is three-branch: one shared
  non-null reranker (or no reranker anywhere with one shared embedding) keeps
  native ordering and native `citation.score`; heterogeneous score domains
  rank-fuse with `61/2 × 1/(60+domain_rank)` over per-domain shared ranks
  (ties broken only by stable identity, never fabricated score deltas), and
  `score_kind` (`cosine|rerank|rank_fusion`) plus `local_score` preserve the
  native evidence. Base `retrieval_mode` (`semantic|hybrid`, request-level
  override never persists) adds a `lexical_v1` path for hybrid bases:
  deterministic tokenizer (Chinese bigrams, ASCII terms, business identifier
  and IP rules, byte caps) over PostgreSQL `tsvector`/GIN, per-base RRF merge
  (k=60) of the lexical and vector paths before the C cut, >128 deduplicated
  query tokens rejected explicitly, and any in-scope row with a stale
  `lexical_version` failing with `KNOWLEDGE_CONFLICT` instead of silently
  degrading. Lexical columns are maintained in the same transaction as every
  content write (publish, reparse, segment edit/add, child re-split);
  re-embedding leaves them byte-identical. The search snapshots each base's
  model bindings, summary-index flags, per-base threshold/mode, and the
  effective global top_k, then re-verifies them before provider dispatch and at the final review — a
  mid-search change is a `KNOWLEDGE_CONFLICT`; changes to defaults already
  overridden by the request, or a smaller top_k default that does not change
  the effective maximum, do not invalidate its strategy. The final
  unified review drops stale candidates while backfilling true
  `matched_children`. Request-level `debug` returns safe diagnostics only in
  that response (strategy, budgets, real counts, monotonic timings, model
  ids, per-hit local scores, four-valued `empty_reason`; never segment text).
  Citations carry `document_version`/`content_digest` plus `score_kind`; the
  segment detail endpoint pages children and answers `KNOWLEDGE_CONFLICT`
  when the caller's expected version/digest drifted. The agent tool packs
  full passages under a 64 KiB UTF-8 JSON budget with `omitted_count`. Every completed search with searchable bases appends a
  `knowledge_queries` row bound to the trusted `project_id + owner_user_id`
  (source `agent` or `retrieval_test`); recent-query reads return only that
  actor's raw query text. Query text follows owner-private retention:
  former-owner and account Phase B delete only the exact Project/owner rows
  while retaining Project-shared Knowledge; final Project retention deletes
  every owner's query rows with the rest of that Project's Knowledge. Query-
  history and hit-counter database faults remain best-effort inside a
  savepoint, while authority/content validation and the outer transaction
  must succeed. Search
  revalidates before every Provider-bound query embedding (cache misses only),
  inside the exact recall transaction even on a cache hit, again immediately before
  sending Segment text to the Reranker, and finally after Provider work;
  revocation thus suppresses later Provider spend, Segment disclosure, query
  logging, hit updates, and already-computed citations at the applicable
  boundary. The
  process-local LRU/TTL query cache keys by model identity and query digest;
  it caches no source embeddings, content, or authorization. Cache counts are
  diagnostic only. The
  `knowledge_search` tool binds `project_id` and owner from the Run context
  (never model arguments) and persists citations in the ToolMessage's
  `additional_kwargs.knowledge_citations`, which stream, values, and journal
  paths all preserve for replay.
- Knowledge tests live under `backend/tests/knowledge/` and require the
  development PostgreSQL plus local MinIO from the root `.env`.

### Memory, audit, quota, and retention

- PostgreSQL is the only project Memory authority. Every document, history row,
  episode, Dream, and Run snapshot remains bound to project, owner, and
  namespace.
- Compaction creates continuity plus tagged Memory input; only durable
  checkpoint persistence activates the corresponding history receipt. Seal and
  Dream Prepare freeze the current PostgreSQL `agent_runtime` Memory and
  summarization policy once at Worker authorization and reuse that exact
  overlay for every drain pass and the final locked barrier—never the
  restart-frozen base `AppConfig` policy leaves.
- One terminal idle-Seal failure consumes only the exact Thread activity epoch
  identified by its `updated_at`; a later settled Run must touch Thread
  activity before Scheduler admission can create a new generation.
- Packaged SNIP may use a bounded in-memory projection to summarize one
  oversized complete turn, but checkpoint replacement and receipt digest always
  use the unchanged original messages. Automatic compaction normally protects
  the latest complete turn; once an open follow-up exists, an over-budget
  complete prefix may advance in bounded passes, and an explicit forced
  compaction may archive a lone over-budget latest turn. Every hierarchical
  leaf, reduction, and repair prompt stays within the admitted trim budget.
  Automatic (`before_model`) compaction degrades an impossible plan or an
  unmeasurable checkpoint to a typed skip-this-turn warning; only explicit
  force paths (manual compaction, Seal, Dream) terminate with a typed reason.
  Custom summary prompts never acquire this projection.
- Compaction freezes the exact Provider profile before dispatch. Static binding
  proves `fixed/overlay safety bound + 4,096-token summary headroom < effective
  trigger`, where the effective trigger is the lesser of the authorized trigger
  and known model input capacity. `keep` is an approximate recent-history
  selection target, not a finite Provider-wire upper bound: every automatic
  complete-turn candidate is also remeasured with the frozen profile (including
  message, multibyte serialization, visual, and framing costs), and the actual
  generated summary is remeasured again before commit. Authoring still rejects
  `keep >= trigger`; a tail that cannot satisfy the profile is not dispatched
  and reaches the typed final capacity guard instead of silently relying on a
  percentage clamp.
- Context Usage v2 reads only the mutable Projection Head rebuilt from a
  Thread-owned append-only Context Evidence sequence and checkpoint-safe
  Projection snapshots. The Lead Thread and every Sub-Agent `execution_id` are
  independent Context Subjects sharing one Thread-wide sequence; browsers never
  receive raw Evidence.
- The innermost final Provider guard measures the fully shaped request and
  positively assigns every model-visible contribution to the closed Context
  lanes through its frozen Adapter-specific wire projector. Provider
  observations remain a separate total; a provider usage report above the
  versioned safety bound is estimator drift, never a Run failure. Adapters
  with a declared per-image token bound admit visual material as bounded
  `visual_media` cost; images without a declared safe cost keep a partial
  lower-bound Projection while Provider capacity admission fails closed, and
  such adapters never arm Lead vision injection or `view_image`.
- Provider dispatch outcomes are two orthogonal facts: result classification
  and adapter-proven retry safety. An adapter-proven definite failure answer
  is recorded as `ProviderFailedV1`; the dedicated
  `ProviderFailedResponseError` preserves its SDK cause and independent retry
  proof so the outer policy retries only when both dimensions allow it. Across
  OpenAI, Anthropic, and vLLM adapters, 429 is known-failed and retry-safe,
  401/500 is known-failed and unsafe, and intermediary 502/504 remains
  ambiguous because it cannot prove whether upstream inference or billing
  occurred. Exact connect/connect-timeout/pool-timeout failures remain the
  separate `NO_RESPONSE_PROVEN` class.
- An idle Projection describes the established historical window; model, Agent,
  Skill, MCP, or Runtime Policy changes do not invalidate or rewrite it.
  Whole-history replacement and compaction begin a new Context Window
  Generation and retain only safe digests and sizes in Evidence.
- A Run's frozen `token_usage.enabled` value governs cumulative Journal,
  billing, SSE usage-detail, and diagnostic presentation only. It never
  disables minimum Context Evidence, Provider capacity protection, Context
  Projection, or automatic-compaction decisions.
- Model work runs outside database transactions. Admission freezes its inputs;
  settlement re-locks and rejects stale policy, model, preference, document,
  Job, or lease state before publishing results.
- Memory is injected as low-authority user-private context, never as a System
  instruction. Disabled or over-budget Memory must not widen authority.
- Quota reserve/consume/release belongs in the authoritative business
  transaction. Tightening a limit does not interrupt already admitted work.
- Audit has closed action/actor/target/outcome contracts. Private identifiers
  are domain-separated HMACs; content, errors, secrets, and storage locations
  are forbidden. Audit and committed usage ledgers are append-only.
- Retention deletes exact project/owner dependencies in documented order and
  never broadens scope or cascades through retained governance references. A
  sealed Run closure is removed only by owning-Run cascade or by the
  transaction-local `RetentionPurgeAuthority` exact Run set issued after
  eligibility and execution quiescence are locked and revalidated; maintenance
  GUCs never authorize closure deletion, and Skill refs remain
  parent-cascade-only. Phase-B retention never treats terminal Job/Attempt
  rows as provider mount-absence proof: after `Job -> Run -> Attempt` locks,
  an exact scoped owner in `acquiring`, `mounted`, or `release_pending` blocks
  until a matching provider-absence proof removes its durable owner root.
- Private Thread deletion is an exact project/owner force-revocation boundary:
  it terminalizes matching Run/Job/Attempt and approval authority before the
  tombstone commits, while retaining the Thread checkpoint, ready files,
  Artifacts, and file quota reservation hidden from ordinary reads. Only a
  trusted failed-create/branch compensation or explicit retention purge may
  request raw checkpoint removal, fenced to the exact tombstone generation and
  never a recreated same-ID Thread. Compensation physically purges only when
  every cleanup step succeeds; any incomplete or unprovable step fails closed
  into a hidden retained tombstone for explicit retention cleanup, and the
  raw-checkpoint reconciler never infers provenance or purges metadata, files,
  or Artifacts.

### Configuration, models, and `inspect_image`

#### Configuration authority

Configuration is read only from explicit `ACT_WEAVE_CONFIG_PATH` or the
repository-root `config.yaml`. Infrastructure settings are restart-required.
Model definitions, runtime/auth/Memory/quota/Automation policy, and
provider-owned API Keys are PostgreSQL authority and must not be reintroduced
as YAML or ambient-key fallbacks.

Model API Keys belong to the Model Provider, never to an individual System
Model. Every System Model binds a required `provider_id`; its `base_url` is
derived from the provider and rejected as external authoring input, and its
per-model secret Generation is re-encrypted from the provider key on create,
rebind, or provider key/endpoint change. Provider fan-out locks
`catalog_state`, providers `FOR SHARE`, then bound models and their current
Generations `FOR UPDATE NOWAIT` in UUID order; lock contention rolls back the
whole settle and surfaces 409 `KNOWLEDGE_CONFLICT` with zero partial commits.
Runs keep freezing the exact model secret Generation, so a rotation
invalidates Runs frozen on the old material; tombstoned Generations are never
restored.

Every System Model stores a required `max_input_tokens` capability in the
bounded range `1..2,000,000`. It is frozen with the exact model execution
payload and supplies the Provider Model capacity used by Context Projection,
the final request guard, and automatic compaction. Keep it distinct from
Provider output `max_tokens` and Run token-budget policy. Fresh DeepSeek v4
bootstrap rows use `1,000,000`.

The authorable adapter descriptor is also the Model admin form authority. Each
setting declares whether it is an editable input or a preserve-only historical
value, whether it is common or advanced, and whether its default is fixed by
the platform or intentionally omitted for the Provider to decide. Runtime
materialization applies platform defaults with explicit model settings taking
precedence; do not invent Provider defaults or expose raw JSON authoring for
structured compatibility settings.

The `openai`, `openai_responses`, and `vllm` adapter forms expose
reasoning-effort choices in this order: `none`, `low`, `medium`, `high`,
`xhigh`, and `max`.

The `deepseek` adapter exposes only DeepSeek's
provider-native `low`, `high`, and `max` reasoning-effort settings. Per-Run
product modes remain canonical: the Worker translates `thinking` to `low`,
`pro` to `high`, and `ultra` to `max`; `flash` uses the configured
thinking-disabled payload and does not forward a reasoning-effort value.

#### Model adapters and `inspect_image`

The authorable System Model adapter allowlist is intentionally narrow:
`anthropic`, `deepseek`, `openai`, `openai_responses`, and `vllm`. `openai`
and `openai_responses` are two fixed protocol entries sharing the native
ChatOpenAI implementation — `openai` pins Chat Completions and
`openai_responses` pins the Responses API; the wire protocol is decided by
the adapter identity, never by a user-editable switch. `deepseek` is the
single DeepSeek entry and points at the reasoning-replay implementation
class. The `patched_openai` and `patched_deepseek` adapter identities are
retired and not accepted as aliases. Retired
adapter rows may remain admin-readable for remediation, but they must not be
reactivated, made default, exposed in the public model catalog, or admitted to
a new Run snapshot.

Text-model image inspection uses `inspect_image`, a reserved, conditional
Worker tool selected through `agent_runtime.vision_bridge.model_name`, never
`config.yaml`. The selected active System Model must declare
`supports_vision=true`; its existing Provider adapter owns SDK construction,
credentials, wire serialization, and response parsing through the single
`ModelRuntime`. The tool owns only Run/file authorization, bounded image
normalization, fixed mode instructions plus one required bounded analysis
goal, durable dispatch settlement, and a bounded untrusted ToolMessage, using
standard LangChain multimodal content blocks. Never add a Bridge-specific
adapter, Provider HTTP client, protocol resolver, raw headers/body
passthrough, or a second model factory. `vision_openai_compatible_v1` has no
production descriptor/class path and `vision_bridge_fake` is test-only;
neither may enter new authoring, defaults, bindings, or Run snapshots.
Preserve exact frozen `purpose="vision"` snapshots, Worker abort/deadline
behavior, tracing suppression, and durable dispatch authority.
Run-scoped dispatch budget exhaustion returns `VISION_BUDGET_EXHAUSTED` and
instructs the Agent to continue without further image analysis or guessed
image contents. Waiting or a new Job Attempt does not reset that budget.
Reserve `VISION_RATE_LIMITED` for temporary Provider rate limits.

#### Configuration schema and bootstrap secrets

The example declares `config_version: 1`. Version 1 is the initial public
configuration schema, including the restart-required `host_execution_approval`
policy and PostgreSQL-owned Automation policy as one baseline. Removed
top-level policy keys remain fail-closed tombstones; use `make config-upgrade`
rather than manually guessing a future migration.

`make setup-db` and `make reset-db` are the only commands allowed to consume
the bootstrap DeepSeek Key and persist three independently encrypted
model-owned copies. Normal Gateway, Worker, Scheduler, doctor, and Compose
startup must not broadcast provider keys as process-wide model configuration.

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
3. Update schema marker, catalog signature/digest, readiness relations, and the
   closed Job/audit/API type contracts that apply.
4. Prove fresh-install ORM/full-schema/catalog parity with a disposable
   PostgreSQL target. Schema V1 has no migration ancestry; recreate a drifted
   development database instead of stamping or hand-patching it.

### Agent tool or middleware

- Config-driven tools need a callable plus an exact `tools:` entry whose name
  and group match runtime registration. Builtins are registered in the owning
  tool registry. New write, shell, or network tools must join the applicable
  read-before-write, audit, guardrail, and loop-detection policies.
- Register middleware in exactly one lead/base/subagent/SDK builder. List order
  is nesting order; preserve the assertions around clarification, progress,
  guardrail, error handling, MCP routing, ToolCallControl, host execution, and
  deferred filtering.
- A new state channel needs an explicit schema and delta-compatible
  materialization behavior.

### System Skill or project asset flow

- Validate all archive paths, sizes, file types, frontmatter, and content
  checksums before persistence or activation; there is no static content scan.
- Keep packaged System assets separate from project-authored assets and
  bindings.
- Authoring services use optimistic revisions and one transaction; failed flows
  must not leave an asset without its initial version or activate a partial
  dependency closure.
- Regenerate and verify the authenticated System Skill catalog after changing a
  public Skill: `uv run python -m scripts.generate_public_system_skill_catalog --check`.

## Guarded operational limits

The focused constants test keeps these values synchronized with code. If a
source constant changes, update this section and the owning detailed document.

### Execution and asset limits

- Fresh System Runtime Policy and SDK/Embedded fallback repeated-call defaults
  are warning `3`, hard limit `20`, and window `20`.
- Fresh Runtime Policy schema v1 internal tool-call defaults are Lead-per-Run
  `200` and Sub-Agent-per-Task `50`. The summarization trigger is a single
  `trigger_tokens` threshold seeded at `320000`; the token-only
  `summarization.keep` retention seeds at `64000` tokens.
- Subagent concurrency is canonically clamped to `1..4`; the per-Run total remains independently bounded to `1..50`.
- Project Skill archives remain limited to 100 MiB total, 64 MiB per regular
  file, and 16384 regular files; archive-create routes have a scoped
  160 MiB wire limit.
- New Skill Run asset snapshots use a strict byte-free v4 manifest plus an
  exact immutable Version reference. Retained v2 inline-Base64 and v3
  compressed Skill rows stay readable only through the bounded legacy source
  adapter; corrupt or source-mismatched legacy rows fail closed, and execution
  never falls back to Current Version or rewrites them as v4.
- New-write rollback is centralized behind the restart-frozen
  `run_skill_snapshots.writer_mode`; `v4_reference` is the default and never
  reads Skill file content. Controlled rollback may select `legacy_v3` only
  when every Gateway and Scheduler uses the same baked artifact version and
  canonical policy digest; missing or mixed identity fails startup. Release v2
  fixes the source/Skill ceiling at 36 MiB, per-Skill codec envelope at
  256 MiB, and cumulative encoded Skill JSON per Run at 48 MiB. Busy and
  Scheduler oversize remain retryable Admissions without a terminal
  occurrence; interactive oversize is a stable 413. Admin operations exposes
  only the non-secret mode, artifact, digest, and ready readback.
- Agent/MCP and current compatibility codec JSON remains bounded; cumulative encoded
  assets for one Run are independently limited to 80 MiB, and each encoded asset
  uses the same limit. Historical v2 Skill rows keep a reader-only 128 MiB
  ceiling; the Skill v4 manifest has its separate 256 KiB database gate.
- Current-message vision injects at most four unique images with a 20 MiB per-image limit.
- Fresh installs seed the `inspect_image` end-to-end deadline at 60 seconds; administrators may set `5..120`, and each Run freezes the selected value.
- Agent Builder's one-turn model-generation deadline defaults to and is capped at 600 seconds; its dedicated proxy route retains a 60-second settlement margin.
- Verified Skill reads remain active for `skills.read_evidence_ttl_calls` subsequent lead model calls (default 12).
- SNIP free-prose task continuity bounded to 2,000 characters and tagged fact lines bounded to 1,000 characters; the packaged prompt raises a declared output cap below 4,096 tokens.
- `trim_tokens_to_summarize` has a floor of 2,000 tokens: policy authoring rejects smaller values and the production factory clamps legacy stored values up to it.
- Vision-capable Lead requests use declared per-image token upper bounds: 1,600 (anthropic) and 2,048 (openai families). Adapters without a declaration keep Lead vision fail-closed and never register `view_image`.
- Non-ASCII conversation material adds a declared safety supplement of 0.19 tokens per byte on anthropic/vllm and 0.05 on the other adapters, raising the CJK per-character ceiling above the bytes/4 baseline.

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
