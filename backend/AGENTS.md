# Backend AGENTS.md

This guide owns backend process boundaries, authorization, persistence,
governed assets, change paths, operational limits, and verification. Read the
repository-level [AGENTS.md](../AGENTS.md) for cross-cutting rules and
[README.md](../README.md) plus [Install.md](../Install.md) for setup and
operator workflows. Feature behavior is authoritative in code and focused
tests; this guide states boundaries, not implementation narratives.

## Guide map

| Change area                         | Read first                                  |
| ----------------------------------- | ------------------------------------------- |
| HTTP, domains, authorization        | Authorization and transactions              |
| Schema or durable state             | PostgreSQL schema and persistence           |
| Jobs, Runs, streams, files          | Jobs, Runs, streams, checkpoints, and files |
| Agents, Skills, MCP, domain secrets | Governed assets                             |
| Knowledge bases, RAG, retrieval     | Knowledge (optional RAG module)             |
| Memory, audit, quota, retention     | Memory, audit, quota, and retention         |
| Configuration, models, vision       | Configuration, models, and `inspect_image`  |
| Implementation or verification      | Common change paths; Tests and code quality |

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
- **Docker** is optional and belongs only to the AIO Sandbox runtime. Docker Run
  Skill read-only mounts require Worker and daemon to share the same absolute
  filesystem view; a distinct view fails closed.

Gateway, Worker, and Scheduler share the same PostgreSQL schema and governed
configuration. Public readiness exposes component state only—never PIDs, lock
keys, credentials, URLs with secrets, private identifiers, or content.

The dependency direction is `app.* -> deerflow.*` and
`app.knowledge -> actweave_knowledge`. Harness code never imports `app.*`;
`actweave_knowledge` never imports `app.*` or `deerflow.*`.

## Where changes live

Run full-stack commands from the repository root and backend targets from
`backend/`; use the applicable `Makefile` as the current command index.

| Path                                         | Owns                                                              |
| -------------------------------------------- | ----------------------------------------------------------------- |
| `app/<domain>/`                              | Composition, HTTP admission, server-issued contexts, transactions |
| `app/gateway/routers/*_routes/`              | Route handlers per resource module, composed once in `router.py`  |
| `app/reliability/run_execution/`             | Private Run executor: lease boundary, frozen preparation, mapping |
| `app/private_work/`                          | Run snapshot admission and the scoped checkpointer                |
| `app/shared_assets/`                         | Agent/Skill/MCP services, Builder lifecycles, package integrity   |
| `app/knowledge/`                             | Knowledge host adapters (config, routers, handlers, tool, models) |
| `app/model_registry/`                        | Model Provider registry; the only API Keys in the model domain    |
| `packages/harness/deerflow/<domain>/`        | Reusable graph, runtime, persistence, sandbox, Skill, MCP code    |
| `packages/harness/deerflow/runtime/runs/`    | Worker execution; `worker.py` keeps `run_agent()`                 |
| `packages/harness/deerflow/sandbox/tooling/` | Path mapping, Sandbox init, Host Execution planning, Tool objects |
| `packages/knowledge/actweave_knowledge/`     | Self-contained RAG module                                         |
| `scripts/`                                   | Explicit Schema V1 setup and operator workflows                   |
| `tests/`                                     | Unit, PostgreSQL, process, and contract gates                     |

Within a domain, `*_contracts.py`, `*_codec.py`, `*_validation.py`, and
`*_rules.py` modules own immutable payloads and pure transformations; the
Service or Repository owns authorization, optimistic revisions, lifecycle
transitions, and the transaction. `deerflow.sandbox.security` is the Host Bash
policy authority, and each LangChain Tool is decorated exactly once.

Compatibility façades (for example `deerflow.sandbox.tools`,
`provider_request_usage.py`, `skill_design_service.py`,
`execution_approval.py`, and the sibling `.py` next to a `*_routes/` package)
only re-export. Put new code in the owning module and patch the owning module in
tests; patch a façade only where the façade itself still calls the seam.

## Non-negotiable boundaries

### Authorization and transactions

- Authority starts from authenticated identity plus a server-issued immutable
  `ProjectContext`. `system_admin` is not project membership.
- Owner-private work uses a server-issued `PrivateWorkContext`. Every private
  query and composite relation binds `project_id + owner_user_id`.
- Project channel configuration and connection state require
  `project.channels.manage`; member private-work capabilities never grant
  access to the project Connections surface or API.
- Never accept project, owner, membership, capability, Run snapshot, secret,
  Job, lease, or runtime authority from request metadata, model/tool arguments,
  or ambient globals. Revalidate membership, capability, resource state, and
  Job/Run lease inside the transaction that performs each side effect.
- Project outsiders, wrong owners, stale membership, and missing private
  resources collapse to `404`; a current member lacking a capability receives
  `403`, except where an existing route family deliberately hides both.
- Repositories do not commit unless they explicitly own the complete operation.
  Preserve the Project -> Membership -> resource lock order; Agent Builder
  extends it with session advisory fence -> turn operation -> design session.
- PostgreSQL RLS is not used. Isolation depends on immutable contexts, scoped
  repositories, composite constraints, and a non-superuser app role.
- Public request/response models reject unknown authority fields and follow the
  error/strictness convention of the neighboring route family. Server-owned
  metadata such as `ProjectRow.created_at` is read-only.

### Authentication, secrets, and public contracts

- Account email is normalized with `strip + lowercase` on every path and is
  protected by a case-insensitive unique index. Username is required: 3–32
  characters, starting with a letter, `[a-z0-9_]` only, stored lowercase,
  unique among human accounts.
- Browser tokens remain valid only while signature, session ID, token version,
  and the durable auth-session row all validate. Logout and password change
  revoke durable authority, not just browser state.
- Passwords, JWTs, CSRF values, raw session IDs, API keys, secret plaintext,
  nonces, ciphertext, storage locators, and full connection URLs never enter
  logs, traces, public responses, snapshots, audit metadata, or browser caches.
- Runtime Skill and MCP plaintext materialization belongs to the Worker
  execution boundary and the exact authorized Sandbox, child process, or remote
  call. The only Gateway materialization path is the authorized,
  transaction-serialized Candidate/Version copy that immediately re-encrypts an
  independent Generation for the new recipient. Output masking is
  accidental-leak protection, not DLP.

### PostgreSQL schema and persistence

- `deerflow/persistence/full_schema.sql` is the structural template for fresh
  installs and generated `schema_comments.sql` is the single source of table
  and column comments (checked-in Chinese text; run
  `uv run python scripts/generate_schema_comments.py --check` after edits).
  Explicit setup validates both and applies them as one transaction; neither
  file is a standalone installation entry point.
- Runtime processes never create, migrate, stamp, repair, or downgrade an
  application database. Schema V1 is the baseline (current marker is `schema_v1`).
  The forward migration registry is intentionally empty, so
  `make upgrade-db` is an exact no-op on a current catalog; a future head adds
  one linear packaged migration and bumps the marker in the same change. Unknown
  markers, an unversioned nonempty schema, and catalog drift are never repaired
  in place.
- Packaged migration SQL uses a small fail-closed subset (one top-level
  statement per line; no transaction control, marker access, comments,
  search-path control, or non-`public` qualification). Extend the parser and its
  rollback tests through a separate review rather than weakening the checks.
- `make setup-db` installs a new empty target and is the only public command
  that consumes bootstrap secrets. `make upgrade-db` takes only `DATABASE_URL`
  under the schema mutation lock; `make check-db` is read-only readiness
  evidence; `make upgrade-system-assets` and `make prepare-run-event-partitions`
  are explicit idempotent operator actions. `scripts.reset_postgres` is an
  internal destructive test helper, not a documented or Make-exposed path.
- Fresh setup seeds Runtime Policy schema v1 (distinct from the DDL marker); it
  is the only supported policy schema and rows declaring another number fail
  closed. Seed defaults belong to `default_policy_value()` and never rewrite
  admitted Runs or existing immutable policy versions.
- A schema change updates the ORM registration, `full_schema.sql`, generated
  `schema_comments.sql`, catalog signature/digest, required relations, marker,
  and schema tests together.
- Application metadata and durable state live in PostgreSQL. File/artifact bytes
  may live in configured storage, but access, identity, version, and scope stay
  database-authoritative.

### Jobs, Runs, streams, checkpoints, and files

- Gateway admits executable work; Worker executes it. Business state, Job,
  quota reservation, snapshot, and audit rows commit atomically at admission.
  Admission freezes an exact Agent/model/Skill/MCP closure plus exact
  domain-secret Generation references; retry and resume use that closure, never
  current catalog pointers.
- Worker stores raw lease tokens only in memory. Every append, tool side effect,
  checkpoint write, and terminal settlement validates the exact current lease
  in its own transaction; an earlier preflight is not write authority.
- A Private Run's `Run Workload Profile` (`interactive` or `research`) is
  server-owned, frozen at admission, and inherited by hidden Graph Turns, Job
  Attempts, and delegated bindings; requests, model output, and tool arguments
  cannot choose or upgrade it.
- Sub-Agent Task lifecycle lives only in `deerflow.subagents.lifecycle`;
  `task_tool` is a wire Adapter and the graph runner grows no lifecycle APIs.
  Each graph binds one explicit SDK, Embedded, Lead, or Private profile factory
  and never infers the profile from `private_scope` or rebuilds SDK
  model/tools/middleware from global config. Parent-to-child propagation is the
  single opaque `DelegatedRuntimeContextProjection`.
- Every assembled graph has exactly one `ToolCallControl`: the Lead binds one
  Run-stable internal tool-call count (including `task` calls), each delegated
  Task binds its own, and there is no Run aggregate. `SubagentLimitMiddleware`
  is a separate policy. The SDK adapter creates a fresh control scope per
  top-level call; synchronous SDK model calls are rejected under Private scope
  or authorization authority.
- The process scheduler's concurrency lease stays held until the child graph,
  finalizer, and inherited operations are quiescent, including after timeout;
  it never fabricates a quiescence receipt. A Private Sub-Agent Task cannot
  return before that quiescence; usage settles once into the parent Run Journal
  by execution receipt, and ordinary parent Stop terminalizes running Tasks as
  cancelled before Run settlement.
- Durable events commit before notification; PostgreSQL `NOTIFY` is only a
  wake-up hint. Stored stream events are immutable, event ids stay monotonic
  with gaps, reconnect replay may omit root `values` frames below the frozen
  full-state horizon, and consumers must not require contiguous ids. Public
  sequence values are canonical signed-BIGINT decimal strings at browser
  boundaries; never coerce them to JavaScript numbers.
- A completed Lead Provider response is flushed to the RunJournal while the
  lease can still authorize writes; later cancellation or rollback must not
  erase observed history. Terminal precedence is
  `ordinary Stop < durable response < authorization revocation`, applied by
  Job/Run settlement under re-locked authorities in the transaction that writes
  the one public terminal (`stream_terminal_status_for_run_settlement`; success
  writes `completed`).
  Dead-Job recovery creates a settlement-only successor with preserved lineage
  and never re-invokes the Agent Graph.
- A loop hard-stop owns one tool-free finalization turn and settles as
  `LOOP_SAFETY_LIMIT`; graph-step exhaustion settles as nonretryable
  `GRAPH_RECURSION_LIMIT`. Both keep produced answers or files as partial
  results. Recovered LLM retries live only in the bounded, redacted
  `run.recovered_issue` trace and never override the durable terminal.
- Harness resource ownership transfers exactly once at runner entry.
  `run_agent()` returns an immutable `RunAgentOutcome` only after cleanup,
  approval sealing, and durable terminal publication; the executor maps that
  outcome plus lease/cancellation facts and never infers success from the
  mutable `RunRecord`. Teardown failure after a durable business terminal is an
  operational fault. Job settlement distinguishes domain/lease conflicts,
  unknown-commit database outcomes, and programming invariants; invariant
  violations stay loud and are never hidden by a broad catch.
- Checkpoint mode is process-frozen: all Gateway and Worker processes sharing a
  database use the same full/delta settings and restart together. Consumers
  materialize state through the scoped checkpointer;
  `CheckpointStateAccessor.replacement_values()` is the canonical whole-state
  replacement mechanic.
- Current-message files and images are admitted from server file authority;
  Worker retry revalidates frozen metadata and fails closed on change. Composer
  upload cleanup takes the same Thread lock as Run admission.
- Runtime-only dependency environments belong under `/tmp`; the finalization
  scan prunes only the top-level workspace `.venv` and fails closed on other
  symlinks or special files. Each private Sub-Agent Task gets a distinct
  scratch outputs view; delivery requires a Lead copy plus `present_files`, and
  scratch is never persisted. Private Sub-Agent `bash` stays fail-closed until
  a provider supplies a real per-Task filesystem namespace.

### Governed assets

#### Packaged System assets

- `skills/public/*` is the sole source of packaged System Skills. Regenerate
  and verify the catalog with
  `uv run python -m scripts.generate_public_system_skill_catalog --check`.
- Packaged System Agent/Skill/MCP definitions are bootstrap-only and immutable
  at runtime; admin definition routes are read-only. A System Skill has one
  deterministic v1; a changed payload must ship under a new identity. Same-byte
  bootstrap is idempotent, and Project bindings store only the asset identity.
- Server-owned Builder Agents are absent from every project, admin, and runtime
  System Agent catalog; only bootstrap and the internal resolver address them.
- Revoking a packaged Skill's v1 is explicit System governance: new bindings and
  Run Admission reject it, already admitted Runs keep their Run Snapshot.

#### Project assets and domain secrets

- A Project Agent owns one mutable Definition saved under optimistic
  `revision`; saves rotate its Definition identity and immediately affect later
  Run Admission. There is no Candidate, activation, or history lifecycle.
  Project Skill/MCP versions are immutable: creation saves a suspended asset
  plus Candidate v1, activation sets `current_version_id`, older versions
  become Historical.
- Project Skill creation happens only through a validated archive upload or an
  AI Builder commit; browser upload never moves `current_version_id`. Lifecycle
  validates structure, `SKILL.md`, frontmatter, checksums, compatibility, and
  secret readiness; there is no static content-security scan.
- Skill export is a read-only, audit-required operation over one exact
  persisted version; the deterministic ZIP never includes secret values,
  lifecycle state, or version history.
- A Skill's `SKILL.md` is the sole authority for `required-secrets` and
  `secrets-autonomous`; every consumer uses the canonical parser. Project Skill
  secrets belong to one exact Version and environment name, a forward Candidate
  re-encrypts only compatible values, and activation requires every required
  declaration configured. Values never enter Skill bytes, readiness, audit
  metadata, or responses.
- Project Skill deletion is an irreversible `archived` transition in one
  transaction: it hides the Skill, removes direct Agent references, and retains
  every Version, file, quota reservation, and ciphertext until final Project
  deletion. No Skill-scoped purge or reconciler exists.

#### Runtime admission and MCP

- Runtime-visible Skill names are unique case-insensitively per project across
  active Project Skills and enabled System bindings, enforced under the project
  lock; Run resolution rejects a conflicting closure.
- Every Run Admission path resolves the current Agent Definition and Skill
  Current Version, then persists an immutable referential closure (exact
  payloads, v4 Skill manifests with pinned Versions, checksums, secret
  Generation references, policy). Worker execution and retries read only that
  closure. Revoked membership, capability, binding, Generation, or lease fails
  at the applicable execution boundary.
- Skill materialization checks and readonly-mount fences run in one transaction
  ordered `Project -> Membership`, then `Job -> Run -> exact active Attempt`.
- A project Skill is passive until explicit slash activation or verified
  reading of its admitted `SKILL.md`; stale or malformed evidence fails closed.
- Project MCP authoring accepts only remote HTTP/SSE definitions under the
  configured CIDR policy; encrypted values bind to the exact Version and slot.
  Worker disables redirects and ambient proxy trust and revalidates target and
  closure for discovery/calls. A remote discovery failure after successful
  closure and secret materialization is isolated to that MCP for the Run;
  authorization, snapshot, database, and secret uncertainty fail closed.
- System MCP may keep packaged stdio, header, and OAuth capabilities; do not
  extend that trust model to project-authored MCP. OAuth token endpoints use the
  same frozen endpoint policy, egress proxy, timeout, and
  `follow_redirects=False` client as the transport.

#### Project Agents and Builder

- Agent `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` are fields of the
  single persisted Definition. Payload checksums are recomputed at resolution
  and Worker materialization.
- Agent and Skill Builder Activity are dedicated owner-private append-only
  tables with their own SSE cursors, never Thread/Run events; they project only
  allowlisted safe stages, and cancellation removes them with the private draft.
  Builder sessions page by an opaque `created_at + id` keyset and are capped
  per project/owner under the create lock. Commit is atomic with the resulting
  Agent Definition or Skill Candidate Version; a rolled-back Commit records a
  failed terminal projection.
- Project Agent DELETE is a soft archive: the tombstone and all
  Thread/Run/Automation/Channel/OAuth references remain, a matching default
  pointer is cleared, catalogs hide it, and new Run admission fails with
  `PRIVATE_WORK_AGENT_ARCHIVED`. An archived slug may be reused.

### Knowledge (optional RAG module)

- Knowledge is optional. `make setup-db` consumes the install-only
  `ACT_WEAVE_KNOWLEDGE_MINIO_{ENDPOINT,BUCKET,ACCESS_KEY,SECRET_KEY}` group as
  one closed set before any DDL (complete: probe and seed enabled; absent: seed
  disabled; partial: fail) and never creates a bucket. The host-owned
  `knowledge_system_settings` row is the only runtime configuration source;
  nonempty YAML `knowledge` is rejected. Storage, limit, cache, `etl_type`, and
  `extraction_cache_enabled` changes require Gateway and Worker to restart
  together.
- While disabled, knowledge routes answer 404 `KNOWLEDGE_DISABLED`, the Worker
  registers no knowledge handlers, and no Run receives `knowledge_search`.
  Project-retention cleanup stays composed independently and fails closed while
  Document rows or object tasks remain without storage configuration.
- While enabled, startup verifies the bucket is reachable and unversioned and
  runs a small fixed extraction to prove the OS sandbox launches. Storage
  failure closes the module and reports `knowledge=unavailable` without
  blocking other features; sandbox failure blocks new parsing admissions only.
- Members lacking a knowledge capability receive 403 `KNOWLEDGE_FORBIDDEN`;
  outsiders and missing projects collapse to 404. Admin routes require
  `system_admin`; project routes require `shared_assets.read` for reads and
  `shared_assets.edit` for writes.
- `actweave_knowledge` stays host-agnostic; engine, secret cipher, quota port,
  and configuration arrive through `app/knowledge/` adapters. Every
  Project-facing read or write carries the server-issued Project authority into
  its transaction. A search is three authority-checked transactions plus one
  guard per reranked group: group load (the query embedding dispatches on that
  authority), the single recall transaction for every group (which also
  re-checks the strategy snapshot and scores the lexical fusion evidence), a
  revalidation immediately before each reranked group's Segment text leaves
  for the Provider, and the final review. Batches inside one Provider call
  share their preceding check; a change that lands between batches is caught by
  the final review as `KNOWLEDGE_CONFLICT`. `knowledge_search` binds project
  and owner from the Run context, may narrow to `knowledge_base_ids` the model
  discovered through `knowledge_metadata_fields` (which returns each base's
  name and description), hands the model the persisted `index_text` as the
  passage (never the escaped display Markdown), and persists citations in the
  ToolMessage's `additional_kwargs.knowledge_citations`.
- PostgreSQL owns all metadata, task, and Segment authority; MinIO owns only
  bytes keyed by database-issued storage keys. Only an unversioned bucket is
  supported (`Enabled`, `Suspended`, Object Lock, or missing
  `GetBucketVersioning` fail closed). Uploads are hard-capped at 50 MiB, forced
  to one PUT, and each `MinioObjectStore` has exactly one upload slot; do not
  raise the cap, remove the slot, or re-enable multipart upload. Never hold a
  database transaction during object I/O.
- Byte accounting uses the host `KnowledgeStorageQuotaPort`: reserve the exact
  database-owned UUID and size before PUT, move reserved to used on confirmed
  storage, release only on confirmed deletion. There is no separate Knowledge
  quota policy or production no-op port.
- Ingestion, re-embedding, summarization, lexical re-derivation
  (`relex_document`), deletion, and exact-key orphan cleanup run as
  `knowledge_tasks` under Worker lease. Handlers recheck Project `active` state
  and unexpired lease (PostgreSQL time) before every Provider request and in the
  publish transaction; a pending-deletion Project returns the claim to
  `retry_wait` without spending an attempt. Timeout cancellation joins any
  started parser or SDK call before releasing the claim. Embedding batches of
  one call dispatch under a bounded concurrency with a serialized progress
  hook; the single in-client retry backs off (`Retry-After`, capped, else
  jitter). Summaries generate, embed, and publish in `SUMMARY_PUBLISH_BATCH`
  blocks so a late failure keeps finished blocks. `relex_document` shares the
  open-indexing slot, rewrites `lexical_tsv`/`lexical_version` from stored
  model text under the Document lock, never touches vectors, and leaves the
  Document `ready`; `POST /bases/{base_id}/relex` admits it per stale published
  document.
- `actweave_knowledge.extraction` owns local parsing through one registry with
  no fallback parser, OCR, remote API, or runtime download. An OS sandbox is
  mandatory (macOS deny-default profile, Linux bubblewrap) and fails closed
  when missing. Preview and ingestion share one extract/clean/split/index path;
  chunk parameters freeze on the Document at upload, and publication atomically
  replaces Segment, Child, Attachment, profile, and Extraction state under a
  version check. The token splitter (`splitter-v2`) caps a Segment at
  `KNOWLEDGE_MAX_SEGMENT_CHARS` (16000) characters, falls back through
  paragraph, line, sentence-final (`。！？.!?`), clause (`；，`), word, and
  character boundaries, and degrades an over-budget context prefix (outer
  heading levels first, then truncation, after emitting any source heading or
  header text in full) with `CONTEXT_PREFIX_TRUNCATED` /
  `OVERSIZED_PREFIX_SPLIT` warnings instead of failing the document. The
  frozen character profile keeps its original algorithm and fallback list.
- Published `content` is the Markdown shown to users and Agents; embedding,
  lexical indexing, reranking, and summaries use the persisted `index_text`,
  and an empty `index_text` on a token profile fails closed.
- Index-text and source-attribution parsers have independent, precompiled,
  immutable rule configurations. Prefix Token counters belong to one packing
  group, separately for display and index text; reuse only proven boundaries
  of the frozen tokenizer pattern, otherwise count the full text. Optimizations
  must preserve chunk boundaries, budgets, source spans, and warnings.
- Embedding/Reranker models are PostgreSQL-administered through their Model
  Provider's encrypted key. A Provider Model referenced by any Base cannot be
  disabled or deleted; a configured Base changes embedding only through rebuild.
- Retrieval groups bases by `(embedding model, reranker model)`; a reranker
  failure fails the search rather than degrading. Semantic recall runs one
  `LATERAL` top-`C` branch per base ordered by `embedding::vector(D) <=> query`
  under `vector_dims(embedding) = D`, the exact shape of the per-dimension
  partial HNSW indexes (384/512/768/1024/1536 on segments, children, and
  summaries; other dimensions run the same statement as a sorted scan) with
  `hnsw.iterative_scan = relaxed_order` set per recall transaction;
  parent_child takes `C × PARENT_CHILD_WINDOW_FACTOR` nearest children per base
  before rolling up. A reranked group receives at most
  `max(top_k, ceil(min(100, 10·top_k) / bases))` candidates per base in recall
  order. Hybrid bases add the `lexical_v1` path maintained in the same
  transaction as every content write; the query side derives a subset of the
  index tokens (Han bigrams only, stop tokens dropped), truncates to 128 tokens
  instead of rejecting, and in a rerank-free group lexical evidence exempts a
  candidate from the cosine threshold. A per-base optional relative cutoff
  (`default_relative_cutoff`, request-overridable) drops candidates below that
  fraction of the base's best native score. Searches snapshot base bindings and
  re-verify them inside recall, before each reranked dispatch, and at the final
  review; a mid-search change, stale `lexical_version`, or drifted
  `expected_version`/digest is `KNOWLEDGE_CONFLICT`. `debug` returns safe
  diagnostics only, never Segment text.
- Segment and document governance runs synchronously in the Gateway under
  `expected_version` CAS. Re-embed preserves Segment identity, text, and manual
  edits; reparse replaces every Segment row; summary indexes never turn a ready
  Document into failed. Attachment reads validate the exact database binding
  before and after bounded object I/O and return only image bytes with
  `private, no-store` and `nosniff`.
- The `knowledge_*` tables plus the settings singleton are ordinary Schema V1
  members, and `public.vector` (pgvector) must exist before install.
- Knowledge tests live under `backend/tests/knowledge/` and require the
  development PostgreSQL plus local MinIO from the root `.env`. Their
  `conftest.py` installs Schema V1 (with pgvector) once per session into a
  `deerflow_test_*` template and clones it per test, so knowledge harnesses
  must not call `_install_full_schema()` themselves; tests whose subject is
  the installation use `empty_postgres_database_url`.

### Memory, audit, quota, and retention

- PostgreSQL is the only project Memory authority. Every document, history row,
  episode, Dream, and Run snapshot remains bound to project, owner, and
  namespace.
- Compaction creates continuity plus tagged Memory input; only durable
  checkpoint persistence activates the history receipt, and checkpoint
  replacement and receipt digests always use the unchanged original messages.
  Seal and Dream freeze the PostgreSQL `agent_runtime` Memory and summarization
  policy once at Worker authorization—never the restart-frozen `AppConfig`.
- Compaction freezes the exact Provider profile before dispatch and remeasures
  candidates and the generated summary with it; `keep` is an approximate
  selection target and authoring rejects `keep >= trigger`. Automatic
  compaction degrades an impossible plan to a typed skip-this-turn warning; only
  explicit force paths (manual compaction, Seal, Dream) terminate with a typed
  reason.
- Context Usage v2 reads only the Projection Head rebuilt from a Thread-owned
  append-only Context Evidence sequence; Lead and every Sub-Agent
  `execution_id` are independent Context Subjects, and browsers never receive
  raw Evidence. An idle Projection is established history. The innermost
  Provider guard measures the fully shaped request; Provider usage above the
  safety bound is estimator drift, never a Run failure.
- Provider dispatch outcomes are two orthogonal facts: result classification
  and adapter-proven retry safety (`ProviderFailedV1` /
  `ProviderFailedResponseError`). 429 is known-failed and retry-safe, 401/500
  known-failed and unsafe, intermediary 502/504 ambiguous; exact
  connect/pool-timeout failures are `NO_RESPONSE_PROVEN`.
- A Run's frozen `token_usage.enabled` governs presentation and billing only;
  it never disables Context Evidence, capacity protection, or compaction.
- Model work runs outside database transactions. Admission freezes inputs;
  settlement re-locks and rejects stale policy, model, document, Job, or lease
  state before publishing results.
- Memory is injected as low-authority user-private context, never as a System
  instruction. Disabled or over-budget Memory must not widen authority.
- Quota reserve/consume/release belongs in the authoritative business
  transaction. Tightening a limit does not interrupt admitted work.
- Audit has closed action/actor/target/outcome contracts. Private identifiers
  are domain-separated HMACs; content, errors, secrets, and storage locations
  are forbidden. Audit and committed usage ledgers are append-only.
- Retention deletes exact project/owner dependencies in documented order and
  never cascades through retained governance references. A sealed Run closure
  is removed only by owning-Run cascade or the transaction-local
  `RetentionPurgeAuthority`; maintenance GUCs never authorize it. Terminal
  Job/Attempt rows are not provider mount-absence proof.
- Private Thread deletion force-revokes matching Run/Job/Attempt and approval
  authority before the tombstone commits, while retaining checkpoint, ready
  files, Artifacts, and quota reservation hidden from ordinary reads. Raw
  checkpoint removal is fenced to the exact tombstone generation and happens
  only through trusted compensation or explicit retention purge; anything
  unprovable fails closed into a retained tombstone.

### Configuration, models, and `inspect_image`

#### Configuration authority

Configuration is read only from explicit `ACT_WEAVE_CONFIG_PATH` or the
repository-root `config.yaml`. Infrastructure settings are restart-required.
Model definitions, runtime/auth/Memory/quota/Automation policy, and
provider-owned API Keys are PostgreSQL authority and must not return as YAML or
ambient-key fallbacks.

The example declares `config_version: 1`. Version 1 is the initial public
configuration schema. Removed top-level policy keys remain fail-closed
tombstones; use `make config-upgrade` rather than guessing a migration.

`make setup-db` is the only public command allowed to consume the bootstrap
DeepSeek Key. Gateway, Worker, Scheduler, doctor, upgrade, and local startup
must not broadcast provider keys as process-wide model configuration.

#### Model Providers and System Models

- API Keys belong to the Model Provider, never to an individual System Model.
  Every System Model binds a required `provider_id`; its `base_url` derives from
  the provider, and its per-model secret Generation is re-encrypted from the
  provider key on create, rebind, or provider key/endpoint change. Fan-out locks
  `catalog_state`, providers, then bound models and Generations in UUID order;
  contention rolls back the whole settle with 409 and zero partial commits.
  Runs freeze the exact Generation, so rotation invalidates Runs frozen on old
  material.
- System Model, Provider Model, and Provider deletion are terminal soft deletes
  that keep historical foreign keys valid. Never delete or clear a System
  Model's current secret Generation; admitted Runs still resolve it. Provider
  deletion requires no live child models; key rotation still fans out to
  soft-deleted text models.
- Every System Model stores a required `max_input_tokens` in `1..2,000,000`.
  It is frozen with the model payload and supplies Provider capacity for
  Context Projection, the final request guard, and automatic compaction; keep it
  distinct from output `max_tokens` and Run token-budget policy.
- The authorable adapter descriptor is the Model admin form authority: each
  setting declares editable versus preserve-only, common versus advanced, and
  platform-fixed versus Provider-decided default. Do not invent Provider
  defaults or expose raw JSON authoring.
- The adapter allowlist is `anthropic`, `deepseek`, `openai`,
  `openai_responses`, and `vllm`. `openai` pins Chat Completions and
  `openai_responses` pins the Responses API; protocol follows adapter identity,
  never a user switch. Retired adapter rows may stay admin-readable but must not
  be reactivated, made default, exposed publicly, or admitted to a new Run.
- `openai`, `openai_responses`, and `vllm` expose reasoning effort `none`,
  `low`, `medium`, `high`, `xhigh`, `max`. `deepseek` exposes only `low`,
  `high`, `max`; the Worker maps product modes `thinking -> low`,
  `pro -> high`, `ultra -> max`, and `flash` sends no reasoning-effort value.

#### `inspect_image`

Text-model image inspection uses `inspect_image`, a conditional Worker tool
selected through `agent_runtime.vision_bridge.model_name`, never `config.yaml`.
The selected active System Model must declare `supports_vision=true`; its
existing Provider adapter owns SDK construction, credentials, serialization,
and parsing through the single `ModelRuntime`. The tool owns only Run/file
authorization, bounded image normalization, fixed mode instructions plus one
bounded analysis goal, durable dispatch settlement, and a bounded untrusted
ToolMessage. Never add a Bridge-specific adapter, HTTP client, protocol
resolver, raw passthrough, or a second model factory; `vision_bridge_fake` is
test-only. Budget exhaustion returns `VISION_BUDGET_EXHAUSTED` and is not reset
by waiting or a new Job Attempt; `VISION_RATE_LIMITED` is reserved for
temporary Provider rate limits.

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
2. Update `full_schema.sql`, regenerate `schema_comments.sql`, and update all
   database constraints.
3. Update the marker-independent catalog signature/digest and recreate
   disposable development targets. When a future head is introduced, also add
   one linear forward migration and bump the marker; `upgrade-db` must reach
   the same catalog as a fresh install.
4. Prove fresh-install ORM/full-schema/catalog parity with a disposable
   PostgreSQL target. Replace a drifted development target instead of stamping
   or hand-patching it.

### Agent tool or middleware

- Config-driven tools need a callable plus an exact `tools:` entry whose name
  and group match runtime registration. Builtins are registered in the owning
  tool registry. New write, shell, or network tools join the applicable
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

`tests/test_agents_md_constants.py` keeps these values synchronized with code.
If a source constant changes, update this section in the same change.

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
  exact immutable Version reference. Retained v2/v3 Skill rows stay readable
  only through the bounded legacy source adapter and are never rewritten.
- New-write rollback is centralized behind the restart-frozen
  `run_skill_snapshots.writer_mode`; `v4_reference` is the default. `legacy_v3`
  requires every Gateway and Scheduler to share the same artifact version and
  policy digest; mixed identity fails startup.
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
