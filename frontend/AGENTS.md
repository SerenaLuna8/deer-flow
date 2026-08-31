# Frontend AGENTS.md

This guide owns frontend route authority, client scope, data contracts, UI
ownership, concurrency, and verification. Read the repository-level
[AGENTS.md](../AGENTS.md) for cross-cutting rules and [README.md](README.md) plus
`package.json` for setup, the current stack, and command lookup. Exact feature
behavior remains authoritative in components and focused tests.

## Guide map

| Change area                    | Read first                                |
| ------------------------------ | ----------------------------------------- |
| Routes, identity, capabilities | Authority and identity                    |
| Queries, mutations, project UI | Project scope and TanStack ownership      |
| SSE, history, concurrency      | Streams, history, and concurrency         |
| Agents, Skills, MCP, files     | Governed assets and runtime state         |
| Admin or Automation            | Admin and Automation                      |
| Implementation or verification | Change checklist; Tests and release gates |

## Application boundaries

The normal full stack starts from the repository root with `make dev` and uses
same-origin `/api/*` through Nginx. Override the backend base URL only for
deliberate split-origin development.

- `/workspace` is the authenticated account-wide project landing page.
- `/projects/[project_slug]/*` is the only live project shell: chats, assets,
  Memory, Connections, Automation, Usage, Audit, members, and settings
  according to server-issued capabilities.
- `/admin/*` contains platform assets, operations, projects, and settings. Its
  server layout hides pages from authenticated non-system-admin users and
  preserves safe login destinations for unauthenticated users.
- `BUILD_MODE=static` exposes a local no-network demo at `/workspace` and rejects
  authenticated project/admin routes. Static code must not import production API
  clients or send `/api/` requests.

Project slugs are display/navigation identifiers. `ProjectContextProvider`
pages the member-scoped project list, exact-matches the slug, enters by UUID,
and owns the resulting context. Nested pages consume `useCurrentProject()` and
never repeat slug resolution or send a slug to UUID-only APIs.

Keep routes and layouts thin. `core/<domain>/` owns contracts and data flow;
feature components own presentation of already-scoped, validated state. Prefer
updating generated or shared primitives through their owning registry or
generator; a necessary local patch needs focused coverage and an explanation.

## Non-negotiable client boundaries

### Authority and identity

- Gateway is the authorization authority. Frontend code renders only
  server-issued capabilities and never derives them from system or project
  roles. A route/layout gate improves UX but does not replace scoped API
  authorization.
- Project navigation groups follow issued capabilities: owner-private Memory
  under Work; Connections guarded by `project.channels.manage`; the Agent
  catalog and existing Builder sessions require `shared_assets.read`, starting
  or mutating a design requires `shared_assets.edit`; each Project Management
  destination requires its exact governance capability. Runner-side execution
  reads remain available to chat/Automation and are not authoring access.
- Use the canonical account UUID returned by Gateway for identity and cache
  keys; never key authority by email, route slug, or display name.
- Authentication distinguishes `authenticated`, `unauthenticated`, and
  `unavailable`. Only an authoritative `401` or explicit logout clears identity
  and account-scoped state; network, 5xx, 403, and malformed responses do not.
- Browser storage may retain safe preferences, never passwords, access/CSRF
  tokens, raw session IDs, secret values, or private runtime authority.
- Redirects preserve only validated local destinations; never reflect an
  arbitrary external URL through login/setup flows.

### Project scope and TanStack ownership

- `ProjectPrivateWorkProvider` owns the only live project client. Its identity
  is the exact authenticated account UUID plus entered project UUID.
- Every project query key starts under that account/project root; never key by
  slug. Add a new domain root to `transitionPrivateWorkScope` so scope changes
  cancel and remove it.
- On account/project transition: abort in-flight work, invalidate the old
  generation, remove old queries/mutations/reconnect state/clients, then create
  the new client only after both UUIDs are known.
- Every request-capable query forwards TanStack's `AbortSignal`. Late callbacks
  from a disposed client or old generation may not update the new scope.
- Before a Skill Builder mutation writes its exact-session cache, cancel the
  in-flight exact-session query so an older GET cannot erase a newly admitted
  Run or newer revision and accidentally stop polling.
- Declare response schemas as strict Zod objects and reject unknown authority,
  owner, trace, secret, or storage fields before data reaches the UI.
- Secret-bearing create/replace operations are imperative authenticated calls,
  not TanStack mutations. Keep values in local form state, clear them after the
  request, and invalidate only safe metadata after success.

### Streams, history, and concurrency

- Durable SSE cursor state is keyed by account/project/thread. Event IDs and
  non-SSE message/event `seq` values remain canonical signed-BIGINT decimal
  strings; compare by decimal length/value, never as JavaScript numbers.
- Replay and live SSE frames merge monotonically by decimal `seq`; initial or
  later REST snapshots must never replace newer cached SSE frames. Drop
  duplicate or non-advancing frames. A newly mounted projection joins an
  active Run from cursor `0`.
- Context Usage consumes the strict Context Projection v2 contract: one
  Thread-owned stream plus initial REST reads feed a monotonic read model
  keyed by Lead or the server-issued Sub-Agent `execution_id`, merged by
  decimal-string `projection_seq`. The browser sees only the safe Projection
  Head, never raw Context Evidence. The idle Lead projection is established
  history, so the composer model is neither a request parameter nor cache
  identity; Context Usage is readable independently from manual-compaction
  capability, and scope or exact-Thread disposal blocks late writes.
- Skill Builder joins its dedicated account/project/session Activity stream
  from cursor `0`; it does not project process UI from the private Run stream.
  The closed tool projection may show only allowlisted safe detail (result
  count, public resource name, candidate-relative path, byte count); raw
  arguments, results, file content, provider errors, internal references,
  message content, and delegated-subgraph payloads never enter that UI state.
- Resolve the one active Run only from authoritative Run/history state; a local
  duration or an intermediate `ask_clarification` message is not a terminal
  signal. While active, render the strict six-field execution-state
  projection. When REST/history proves terminal, stop and forget that exact
  Run reconnect even if its SSE source never closes; late SSE must not
  overwrite canonical terminal history or cause a second Run/cancel request.
  Preserve the visible conversation projection across any SDK/history detach,
  then replace it atomically with canonical history.
- Scope disposal aborts the stream and prevents late state writes. Compare-and-
  remove reconnect metadata so an old consumer cannot erase a newer Run. A
  successful Thread DELETE immediately tears down only that exact
  account/project/thread runtime and query subtree, and late stream callbacks
  may not recreate it; a failed DELETE must not tear down local state.
- Preserve LangGraph stream-handle property descriptors; do not object-spread a
  handle with lazy getters.
- Thread/Run/file catalogs page to completion with strict schemas, abort
  support, duplicate/progress checks, and a bounded page/offset limit; never
  rely on SDK default page sizes or silently truncate. Agent Builder resume
  catalogs follow the same rule over the server's opaque cursor.
- Mutations that carry `expected_version` treat `409` as an explicit concurrent
  edit: preserve local unsaved changes and refresh authoritative state; never
  silently overwrite. Builder session cache merges are revision-monotonic, so
  late responses cannot overwrite a newer cancelled tombstone.
- Conversation rendering is a lead-Agent projection. Subagent/middleware events
  use their dedicated task/progress surfaces and must not be attached to an
  arbitrary assistant message.
- A Lead Agent's ordinary text on a tool-calling message is process output, not
  a terminal answer. Render every reasoning, process-output, and tool step in
  canonical model-call order without a generic step aggregation; keep each
  reasoning body independently collapsible and specialized steps such as
  `present_files` in their original position. Withhold the delivered-file card
  while the Run is active, then mount the deduplicated card once after the
  final assistant answer.
- The Main Project Chat composer may select `Research` for the next Run only.
  Promote that choice through the reserved admission context and clear it only
  after authoritative Run admission succeeds. Research is workload policy, not
  a capability or model instruction, and is unavailable on other chat surfaces.
- Repeated-call, Run tool-call-limit, and Sub-Agent-total progress are three
  strict event contracts. Merge live custom frames with durable Run Event
  replay by deterministic `observation_id`; refresh, reconnect, or duplicated
  middleware execution must not double-render them. Hard-limit exhaustion
  stays visible with its frozen policy scope: a Lead limit blocks later Lead
  tools (including new `task` calls), while one Sub-Agent Task limit blocks
  only that Task and parallel Tasks keep independent counts.

### Governed assets and runtime state

#### Agents and Builder

- A Project Agent has one mutable server-owned Definition. The UI reads and
  saves that aggregate with optimistic `revision`; it exposes no Candidate,
  activation, version picker, diff, or history. A successful save immediately
  becomes authoritative for later Run Admission; a `409` recovery reloads the
  catalog and Definition, preserves unsaved local edits, and adopts authority
  only from a newer CAS revision. Asset suspension remains a separate
  emergency stop.
- Agent `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` are four logical
  Definition fields, not a filesystem editor. Project Skill/MCP versions
  remain immutable server objects; the UI never fabricates a Current Version,
  capability, binding, or dependency closure.
- Builder sessions are account/project/owner scoped. Candidate edits remain
  revision/checksum-bound; final confirmation is one server transaction and
  does not imply activation or binding unless the response says so.
- Agent Builder Activity has its own account/project/session query and SSE
  cursor; it never shares Thread/Run history, reconnect state, or cache keys.
  Render only the strict public Activity contract, page durable replay to
  completion before joining SSE, merge late REST monotonically, preserve
  replayed real reasoning, and do not invent reasoning for a stage-only
  generation. A generated blueprint lives in the separate workbench; the
  Builder model/mode chooser reuses the ordinary chat resolver while its
  preference remains session-local.
- An error-severity Builder conflict blocks commit until a later AI turn
  regenerates the candidate; editing a document alone does not resolve it.
  Commit includes `slug` only when the normalized review name differs from
  `session.slug`, and its idempotency signature must match that request body.

#### Skills and secrets

- Project Skill creation exposes exactly two user flows: AI Builder and
  validated archive upload; no manual metadata or starter-template form.
  Archive upload has one submit path and sends only the selected archive, with
  no browser risk-confirmation retry or scan-result presentation. Skill
  surfaces present structural validation, immutable version content, and
  activation readiness without a static content-security decision.
- Save failures in the Skill version workbench render next to the save
  controls, before the potentially large file editor.
- Skill secret declaration forms are a server-parsed projection of the current
  `SKILL.md` buffer, never a second source of truth or a browser YAML parser.
  Parse/patch races preserve newer local edits; invalid or pending source
  blocks save, validation, and activation without discarding unsaved changes.
- The Skill version workbench presents one `Runtime secrets` surface.
  Declarations edit the current `SKILL.md` buffer; the Project stores one
  encrypted value per exact Skill Version and declared environment-variable
  name. System Skill definitions declare slots only. After a new Candidate
  Version is saved, compatible declarations receive independently re-encrypted
  copies and changed declarations require new input.
- Skill activation uses read-only server readiness: the request pins the exact
  payload checksum and secret revision and never submits plaintext. A
  Candidate cannot activate until all required declarations are configured;
  create/revise results with declarations lead to the exact created version's
  Runtime secrets controls, and AI never chooses or sees a secret value.
- Project Skill deletion is a terminal archive action. The confirmation
  explains the Skill is hidden and removed from every Agent without suspending
  those Agents, while all versions, files, quota, secret state, and ciphertext
  remain Project-owned until final Project deletion. On success, remove the
  Skill query subtree, invalidate Agent Definition/catalog/runtime and Builder
  caches, and show the server-returned affected Agent count. No `ASSET_IN_USE`,
  physical-delete, manual-unbind, secret-loss, or storage-reclamation guidance.
- System asset definitions are read-only in global admin views; System Agents
  are single Definitions and System Skills single-v1 assets. Project bindings
  store only the asset identity, and runtime resolves the Definition or Current
  Version. System Skill revocation is governance eligibility, not a version
  state; optimistic `409` responses refresh authority and require a fresh
  choice.
- Project and global System Skill details export the currently selected,
  persisted version through the same `Export ZIP` interaction. Unsaved Skill or
  secret edits disable export until saved or discarded; revoked System versions
  remain visible but cannot export. Client success means the complete ZIP
  arrived and the browser download started.
- Secret forms use write-only semantics: blank edit preserves the stored value,
  replacement writes a new value, and clear requires a separate confirmation.
  Responses expose only configured/readiness/revision metadata; plaintext never
  enters TanStack Query caches, and controls clear immediately after submit.

#### MCP, models, Agent state, and files

- Project MCP authoring exposes only backend-supported remote transports and
  secret-free URLs. Encrypted values are stored by exact MCP Version and slot;
  the browser never probes the endpoint, performs discovery, or infers CIDR
  authorization. Credential fields are dynamic rows targeting Header or Query;
  one slot may contain both targets and is submitted as one write-only
  replacement. Replacing or clearing a value marks discovery stale.
- Model API keys belong to the Model configuration. Connection tests always
  require a temporary Key from the current form and never read the stored copy.
- System Model create, update, and connection-test forms require the bounded
  `max_input_tokens` capability (`1..2,000,000`), presented as the Provider
  Model's maximum input context and context-percentage denominator, never as
  Provider output `max_tokens` or a Run token-budget setting.
- Render Provider settings from the backend adapter descriptor as typed form
  controls: common fields visible, `advanced=true` fields in a collapsed
  Advanced Settings section, platform defaults distinguished from an omitted
  Provider default. Never restore a raw JSON settings editor; preserve
  descriptor-declared compatibility fields unchanged and fail closed on
  unknown historical fields or adapters.
- Model/thinking selections are preferences. Gateway returns the effective
  execution profile; UI history and status use that server result.
- If a Thread's project Agent becomes suspended, history remains readable but
  send, retry/regenerate, edited rerun, and human-input submission fail closed
  until the user re-enables the Agent or starts a new Thread. If the Agent is
  archived, those paths plus execution-approval admission surface the
  deleted-Agent message and require switching Agents; Agent deletion is not
  blocked by default, current Thread, or historical Run references.
- Upload messages carry opaque ready-file IDs and safe metadata, not
  browser-made image data URLs; vision admission and file integrity remain
  Worker authority.
- A persisted Thread composer eagerly uploads accepted attachments on
  selection, paste, or drop. Background upload must not lock text editing;
  Send waits for and reuses the exact pending or ready upload instead of
  starting a duplicate. Thread and scope transitions detach local ownership
  and clean up late ready results in their original scope.
- `present_files` is the explicit published-file boundary; workspace previews
  and live file-tool state do not prove durable delivery. Delegated output
  scratch files are not publishable cards—only a Lead-promoted copy under
  `outputs/` can cross `present_files`. Workspace-change line counts may be
  unavailable and remain unknown rather than fabricated zeros; ordinary
  conversation history does not mount the change card or issue its query.
- Every `task` tool call remains visible as its own Sub-Agent card in canonical
  message order. Card metadata is a single bounded row whose flexible text
  truncates before fixed status and disclosure controls. A card exposes live or
  settled Context Usage only when lifecycle events or terminal ToolMessage
  metadata provide a valid server-owned execution UUID; the Tool Call ID is
  never substituted as Context authority.

#### Knowledge bases

- Knowledge is a deployment-level optional module. The project navigation entry
  renders only when the per-project health probe succeeds; a 404
  `KNOWLEDGE_DISABLED` (or any probe failure) hides it. Reading pages requires
  `shared_assets.read`; create, edit, upload, retry, and delete controls
  require `shared_assets.edit`. UX gates never replace scoped API
  authorization.
- `core/knowledge/` owns the strict contracts, query keys, and hooks; the
  knowledge root is registered in `scope-registry.ts`. Base and document lists
  poll every 2 seconds only while a row is in an active status; a `deleting`
  row with a recorded `delete_error` parks (stops polling, shows the reason)
  until the user explicitly re-deletes. A recoverable background document-list
  refresh failure keeps the last account/project-scoped rows with an explicit
  retry, while `401`, `403`, and not-found authority failures remove and hide
  those cached rows.
- All chunk parameters (size, overlap, separator, pre-processing rules,
  chunking mode with child size/separator) are
  immutable after upload and retry reuses them; the wizard's step 2 and the
  upload dialog expose the same controls. The wizard expands parameters inside
  the selected mode card and scrolls configuration and preview independently on
  desktop. Its preview panel carries a
  file picker over the selected files: each newly shown `File` object
  auto-previews exactly once via the stateless `chunk-preview` endpoint, and
  later parameter edits keep the last preview visible as stale requiring an
  explicit refresh, so the browser does not repeatedly upload complete files
  (failures surface inline and never pose as a valid preview). Preview
  identity (`core/knowledge/preview-identity.ts`) tracks the File object,
  parameter snapshot, scope generation, and a monotonic request sequence, so
  a late response from a replaced request can never overwrite the current
  winner (fast A→B switches, re-submitted parameters, removed or same-name
  replaced files, scope changes). Child fields
  render only in parent_child mode and are omitted from general-mode
  requests; child preview chips retain access to the complete parent text.
  The separator inputs hold the escaped form the backend decodes
  (`\n\n` and `\n` by default). The upload dialog submits multiple files
  sequentially, reports one verdict per file, and keeps only the failed files
  queued for a retry that never re-uploads the succeeded ones. Search labels
  scores with the
  neutral "Retrieval score" wording plus a `score_kind` provenance badge
  (Cosine, Rerank, or Rank fusion — never a "confidence" percentage); result
  rows show the final rank, and a collapsed diagnostics disclosure exposes
  strategy/budget/count/timing/model details without any segment text. Hit
  detail pins the full scored segment via the authoritative detail read with
  the result's expected version/digest — drift shows a "run the search again"
  conflict instead of silently newer content — highlights the truly matched
  children from the response evidence (never inferred from list order), and
  locates into the documents view. Never-searched, no-hits, filtered-empty,
  not-ready, and stale-content empty states are distinct; a model failure
  stays visible with a Retry that re-sends the last input, and a base config
  change (embedding/reranker/mode/defaults) resets results so a late slow
  response cannot resurrect them.
- The retrieval test's top_k and threshold inputs are optional: empty inputs
  omit the field so the backend resolves the base defaults (placeholders show
  them), which are edited in the base settings panel. Empty-base creation only
  submits name/description and accepts `embedding_model_id: null`; it neither
  fetches model options nor silently chooses a model. The first Upload action
  opens `knowledge-base-setup-dialog.tsx`: an atomic initial PATCH binds the
  embedding model plus retrieval/reranker choices before the original upload
  dialog opens. Failure preserves choices without uploading. Settings can open
  the same setup without starting an upload. Unconfigured bases show a neutral
  state; read-only members never receive setup controls. Already configured
  bases retain rebuild as their only embedding replacement path.
  The document-first wizard, first configuration, and base settings persist the
  semantic/hybrid retrieval mode;
  the UI calls `semantic` "Vector search" (向量检索); the API value remains
  `semantic`. The creation wizard can bind an optional reranker inside either
  selected mode card. It snapshots the binding at submit, omits an unselected
  `reranker_model_id`, and freezes the control during creation. Model selection
  is a base setting, not a per-query override.
  Changing the retrieval test's mode overrides only that request and does not
  change what an Agent inherits from the base. Metadata filter rows
  (field/operator/typed value; operators follow the field type) are built
  client-side and sent as `metadata_filters` only when present; with no
  defined fields the section explains why none can be added. Below the
  results, the recent-queries table (`useKnowledgeBaseQueries`) pages the
  query log with source labels; clicking a logged query backfills the search
  input, and each finished search invalidates the log.
- The base detail's Metadata section (edit capability only) manages the
  per-base field definitions (add with type, rename, delete with confirm);
  each document row's Metadata dialog assigns typed values (text, number,
  datetime-local mapped to epoch seconds) where an emptied stored value
  sends an explicit null and untouched fields are omitted. The batch bar's
  metadata dialog edits the current selection with per-field keep/set/clear
  modes, reports mixed values as "n distinct values" instead of a fake
  blank, submits only explicitly edited fields in one all-or-nothing patch,
  and on a 409 conflict keeps the unsaved form while refreshing the
  authoritative rows for re-confirmation. The settings
  panel uses semantic/hybrid choice cards and binds or clears the optional
  reranker model (effective on save, no
  rebuild; the search panel drops stale results on the change), and its
  re-embed block confirms what is preserved (text, manual edits, enabled
  states) before POSTing the selected embedding model, then reports the real
  admission outcome (accepted count plus skipped never-published documents);
  documents then repoll back to ready. Settings controls placed outside the
  PATCH form retain explicit `form` association for native validation and Enter
  submission; the re-embed operation remains separate. Each document row's "Reparse from
  original" action opens a dialog prefilled with the document's frozen chunk
  parameters: edits invalidate the server-side preview, submission carries
  `expected_version`, and a conflict keeps the form, retires the preview, and
  refreshes the authoritative row for an explicit re-confirmation.
- The documents table carries per-row enabled switches, rename, a characters
  column, and (with `shared_assets.edit`) row checkboxes with a batch bar for
  enable/disable/delete plus batch metadata; rows in `deleting` are not
  selectable. Knowledge workspace state (base, view, document, segment,
  status filter, sort, page) lives in the URL for deep links and history;
  the document list is searched/filtered/sorted client-side over a
  completeness-checked full fetch (a changed total, duplicate identity, or
  incomplete multi-page read is an explicit error, never a partial table).
  Segment lists and location details share the document-scoped segments query
  root, so maintenance invalidation also refreshes a mounted location card's
  edited or deleted content.
  Status cells render the projected
  real task progress (kind, stage, verified counts, attempt, retry wait) and
  a summary bar above the table counts processing/retry-wait/failed/ready
  documents without folding failures into success. "View
  segments" replaces the table in place with the segment browser
  (`knowledge-segments-browser.tsx`): list with word counts and manual badges,
  enable toggles, right-side edit/add sheets (4000-character ceiling mirroring
  the backend), and confirmed deletion. Condensed list previews open complete
  text in a read-only sheet, also available to read-only members; those members
  still receive no mutation controls. These sheets use `SheetContent`'s optional
  `overlayClassName` for a light backdrop while keeping the shared modal,
  focus, and dismissal behavior; other sheets retain their default overlay.
- The admin retrieval model registry shares `/admin/settings/models` with
  language model settings (`core/admin-settings/model-registry/` and
  `admin-model-registry-page.tsx`): providers own write-only API keys sent as
  imperative requests that never enter query caches, and a provider or model
  referenced by knowledge bases (`in_use`) cannot be disabled or deleted.
- Chat citations render only from the thread projection's validated
  `knowledge_citations` payload on the Run's final AI text message; the
  renderer re-validates and degrades to "no citations" rather than trusting an
  arbitrary payload.

### Admin and Automation

- Admin queries mount only after authenticated `system_admin` state. Strict
  schemas exclude private message, Run, Thread, owner, error, locator, and
  secret fields; unavailable readiness is not a fabricated zero.
- Project governance queries and controls mount only after the exact capability
  gate; the UI does not infer access from role names.
- Automation keys include account, project, and owner. Schedule validation is
  pure; manual triggers use idempotency and the same durable server admission
  as Scheduler.

## Change checklist

1. Keep project routes below `src/app/projects/[project_slug]/` thin. Use
   `requireServerProjectCapability` for an SSR UX gate or `useCurrentProject()`
   in the delegated client component; register navigation with the same
   capability and never resolve the slug or construct the project client again.
2. Put response contracts and data flow in the owning `core/<domain>/`. Use the
   authenticated fetcher, validate strictly, forward the abort signal, key
   project state by account/project UUID, register every new root in
   `scope-registry.ts`, and invalidate the smallest authoritative root.
3. Put presentation in the owning feature directory and compose shared
   primitives. Import cross-module code through `@/*`, merge conditional classes
   with `cn()`, and add an explicit source/safelist entry for dynamically
   assembled Tailwind classes.
4. Extend streaming from `core/private-work/api-client.ts`,
   `core/threads/hooks.ts`, and the scoped chat projection rather than adding a
   second client. Cover reconnect, duplicate/late frames, scope change, terminal
   dedup, bigint cursors, compaction/history merge, and cancellation.
5. Add focused coverage for unauthorized access, stale scope or late responses,
   optimistic conflicts, and the changed feature's failure boundary.

## Tests and release gates

Unit tests mirror source paths under `tests/unit/` and import through `@/*`.
Restore globals and dispose scoped clients after each test.

Playwright modes are intentionally isolated: deterministic mocked tests live in
`tests/e2e/`, static-boundary tests in `tests/e2e-static/`, and live integration
tests in `tests/e2e-real-backend/`. Use the matching package script or
Playwright config; never mix their assumptions. Keep mocked E2E deterministic:
mock every relevant `/api/` request with fixed IDs/timestamps, and let an
unmocked request fail rather than reach a live backend. Static and production
artifacts use separate output directories.

Features and fixes follow TDD. Before handoff run `pnpm check`, `pnpm test`, and
the affected build/Playwright gates for the current checkout. State explicitly
when dependencies, a live Gateway/PostgreSQL, a browser, or a target deployment
prevented a gate from running.
