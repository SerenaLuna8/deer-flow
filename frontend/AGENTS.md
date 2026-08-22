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
- `/projects/[project_slug]/*` is the only live project shell. It contains
  project chats, assets, Memory, Connections, Automation, Usage, Audit, members,
  and settings according to server-issued capabilities.
- `/admin/*` contains platform assets, operations, projects, and settings. Its
  server layout hides pages from authenticated non-system-admin users and
  preserves safe login destinations for unauthenticated users.
- `BUILD_MODE=static` exposes a local no-network demo at `/workspace` and rejects
  authenticated project/admin routes. Static code must not import production API
  clients or send `/api/` requests.

Project slugs are display/navigation identifiers. `ProjectContextProvider`
pages the member-scoped project list, exact-matches the slug, enters by UUID, and
owns the resulting context. Nested pages consume `useCurrentProject()` and never
repeat slug resolution or send a slug to UUID-only APIs.

Keep routes and layouts thin. `core/<domain>/` owns contracts and data flow;
feature components own presentation of already-scoped, validated state. Prefer
updating generated or shared primitives through their owning registry or
generator. A necessary local patch needs focused coverage and an explanation.

## Non-negotiable client boundaries

### Authority and identity

- Gateway is the authorization authority. Frontend code renders only
  server-issued capabilities and never derives them from system or project roles.
- System role and project membership role are separate domains. A route/layout
  gate improves UX but does not replace scoped API authorization.
- Project navigation groups follow issued capabilities: owner-private Memory
  remains under Work; Connections is project-admin configuration guarded by
  `project.channels.manage`; the Agent catalog and an owner's existing Builder
  sessions require `shared_assets.read`, while starting or mutating a design
  requires `shared_assets.edit`. Skill/MCP authoring still requires edit or
  binding-management authority, and each Project Management destination requires
  its exact governance capability. Runner-side execution reads remain available
  to chat/Automation and are not authoring access.
- Use the canonical account UUID returned by Gateway for identity and cache keys;
  never key authority by email, route slug, or display name.
- Authentication distinguishes `authenticated`, `unauthenticated`, and
  `unavailable`. Only an authoritative `401` or explicit logout clears identity
  and account-scoped state. Network, 5xx, 403, and malformed-response failures do
  not silently log the user out.
- Browser storage may retain safe preferences, never passwords, access/CSRF
  tokens, raw session IDs, secret values, or private runtime authority.
- Redirects preserve only validated local destinations. Never reflect an
  arbitrary external URL through login/setup flows.

### Project scope and TanStack ownership

- `ProjectPrivateWorkProvider` owns the only live project client. Its identity is
  the exact authenticated account UUID plus entered project UUID.
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
  strings. Compare by decimal length/value; never convert to JavaScript number.
- Skill Builder joins its dedicated account/project/session Activity stream
  from cursor `0`; it does not project process UI from the private Run stream.
  The closed tool projection may show only its allowlisted safe detail: result
  count, public resource name, candidate-relative path, or byte count. Raw
  arguments, results, file content, provider errors, internal references,
  message content, and delegated-subgraph payloads never enter that UI state.
- Activity replay and live SSE frames merge monotonically by decimal `seq`;
  initial or later REST snapshots must never replace newer cached SSE frames.
- Drop duplicate or non-advancing frames. A newly mounted projection joins an
  active Run from cursor `0` because an old hidden consumer's cursor does not
  prove that the new UI rendered those frames.
- Scope disposal aborts the stream and prevents late state writes. Compare-and-
  remove reconnect metadata so an old consumer cannot erase a newer Run.
- A successful Thread DELETE immediately tears down only that exact
  account/project/thread runtime and query subtree. Late stream callbacks may
  not recreate its cursor, reconnect owner, version, or cached projection; a
  failed DELETE must not tear down local state.
- Preserve LangGraph stream-handle property descriptors; do not object-spread a
  handle with lazy getters.
- Thread/Run/file catalogs must page to completion with strict schemas, abort
  support, duplicate/progress checks, and a bounded page/offset limit. Never rely
  on SDK default page sizes or silently return a truncated catalog.
- Agent Builder resume catalogs follow the same rule over the server's opaque
  cursor. A revision `409` refreshes the authoritative session, preserves local
  blueprint/name edits, and releases the stale idempotency entry. Session cache
  merges are revision-monotonic, so late responses cannot overwrite a newer
  cancelled tombstone.
- Mutations that carry `expected_version` treat `409` as an explicit concurrent
  edit. Preserve local unsaved changes and refresh authoritative state; never silently
  overwrite it.
- Conversation rendering is a lead-Agent projection. Subagent/middleware events
  use their dedicated task/progress surfaces and must not be attached to an
  arbitrary assistant message.

### Governed assets and runtime state

#### Agents and Builder

- Project Agent/Skill/MCP versions are immutable server objects. The UI authors
  through aggregate mutations and optimistic revisions; it never fabricates a
  Current Version, capability, binding, or dependency closure.
- Saving a Project Agent or Skill creates an immutable Candidate Version without
  moving `current_version_id`. Activation atomically selects the Candidate and
  enables the asset. Asset suspension is a separate emergency stop that keeps the
  same Current Version. Editor and Admin may save, activate, enable, and suspend.
- An Agent version selection owns the visible Instructions and Capabilities for
  that exact immutable version. Only the latest forward head is editable.
  Historical Versions are view-only and have no restore, copy, delete, edit, or
  activation action. A `409` recovery reloads the catalog and complete history,
  preserves unsaved local edits, and adopts authority only from a newer CAS
  revision.
- Agent `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` are four logical
  version fields, not a filesystem editor.
- Builder sessions are account/project/owner scoped. Candidate edits remain
  revision/checksum-bound; final confirmation is one server transaction and does
  not imply activation or binding unless the response says so.
- Agent Builder Activity has its own account/project/session query and SSE cursor;
  it never shares Thread/Run history, reconnect state, or cache keys. Render only
  the strict public Activity contract, preserve replayed real reasoning, and do
  not invent reasoning for a stage-only generation. The Builder model/mode chooser
  reuses the ordinary chat resolver while its preference remains session-local.
- An error-severity Builder conflict blocks commit until a later AI turn
  regenerates the candidate; editing a document alone does not resolve it. Commit
  includes `slug` only when the normalized review name differs from
  `session.slug`, and its idempotency signature must match that request body.

#### Skills and secrets

- Project Skill creation exposes exactly two user flows: AI Builder and validated
  archive upload. Do not reintroduce a manual metadata or starter-template form.
- Skill secret declaration forms are a server-parsed projection of the current
  `SKILL.md` buffer, never a second source of truth or a browser YAML parser.
  Parse/patch races must preserve newer local edits; invalid or pending source
  blocks save, validation, and activation without discarding unsaved changes.
- The Skill version workbench presents one `Runtime secrets` surface. Declarations
  edit the current `SKILL.md` buffer; the Project stores one encrypted value for
  each exact Skill Version and declared environment-variable name. System Skill
  definitions declare slots only; every Project binding supplies its own values.
  After a new Candidate Version is saved, compatible declarations receive
  independently re-encrypted copies and changed declarations require new input.
- Skill activation uses read-only server readiness. The activation request pins
  the exact payload checksum and secret revision and never submits plaintext. A
  Candidate cannot activate until all required declarations are configured.
  Archive plus Builder create/revise results with declarations
  must lead to the exact created version's Runtime secrets controls; AI never
  chooses or sees a secret value.
- System asset definitions are read-only in global admin views. Project binding
  and domain-secret operations are separate, narrow mutations.
- System Agent/Skill definitions are read-only single-v1 assets. Project bindings
  store only the asset identity and runtime resolves its Current Version. System
  Skill revocation is displayed as governance eligibility, not a version state;
  optimistic `409` responses refresh authority and require a fresh choice.
- Project and global System Skill details export the currently selected,
  persisted version through the same `Export ZIP` interaction. Unsaved Skill or
  secret edits disable export until saved or discarded; revoked
  System versions remain visible but cannot export. Client success means the
  complete ZIP response arrived and the browser download was started, not that
  the user saved the file.
- Secret forms use write-only semantics: blank edit preserves the stored value,
  replacement writes a new value, and clear requires a separate confirmation.
  Responses expose only configured/readiness/revision metadata. Plaintext never
  enters TanStack Query caches and controls are cleared immediately after submit.

#### MCP, models, Agent state, and files

- Project MCP authoring exposes only backend-supported remote transports and
  secret-free URLs. Every Project stores encrypted values by exact MCP Version
  and slot; the browser never probes the endpoint, performs discovery, or infers
  CIDR authorization. Replacing or clearing a value marks discovery stale.
- Model API keys belong to the Model configuration. Connection tests always
  require a temporary Key from the current form and never read the stored copy.
- Model/thinking selections are preferences. Gateway returns the effective
  execution profile; UI history and status use that server result rather than
  claiming the local selection was accepted.
- If a Thread's project Agent becomes suspended, history remains readable but
  send, retry/regenerate, edited rerun, and human-input submission all fail
  closed until the user re-enables the Agent or starts a new Thread with another
  Agent.
- If a Thread's project Agent is archived, history remains readable. New send,
  retry/regenerate, edited rerun, human-input, and execution-approval admission
  surface the deleted-Agent message and require switching Agents. Agent deletion
  is not blocked by default, current Thread, or historical Run references.
- Upload messages carry opaque ready-file IDs and safe metadata, not browser-made
  image data URLs. Vision admission and file integrity remain Worker authority.
- A persisted Thread composer eagerly uploads accepted attachments on selection,
  paste, or drop. Background upload must not lock text editing; Send waits for and
  reuses the exact thread/client pending or ready upload instead of starting a
  duplicate request. Thread and private-work scope transitions detach local
  ownership and clean up late ready results in their exact original scope.
- `present_files` is the explicit published-file boundary. Workspace previews
  and live file-tool state do not by themselves prove durable delivery.

### Admin and Automation

- Admin queries mount only after authenticated `system_admin` state. Strict
  schemas exclude private message, Run, Thread, owner, error, locator, and secret
  fields; unavailable readiness is not a fabricated zero.
- Project governance queries and controls mount only after the exact capability
  gate. The UI does not infer access from role names.
- Automation keys include account, project, and owner. Schedule validation is
  pure; manual triggers use idempotency and the same durable server admission as
  Scheduler.

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
tests in `tests/e2e-real-backend/`. Use the matching package script or Playwright
config; never mix their assumptions.

Keep mocked E2E deterministic: mock every relevant `/api/` request and use fixed
IDs/timestamps. An unmocked request should fail rather than reach a developer's
live backend. Static and production artifacts use separate output directories.

Features and fixes follow TDD. Before handoff run `pnpm check`, `pnpm test`, and
the affected build/Playwright gates for the current checkout. State explicitly
when dependencies, a live Gateway/PostgreSQL, a browser, or a target deployment
prevented a gate from running.
