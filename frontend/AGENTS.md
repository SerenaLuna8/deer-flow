# Frontend AGENTS.md

This guide owns frontend route authority, client scope, data contracts, UI
ownership, concurrency, and verification. Read the repository-level
[AGENTS.md](../AGENTS.md) for cross-cutting rules and [README.md](README.md) plus
`package.json` for setup, the current stack, and command lookup. Exact feature
behavior is authoritative in components and focused tests; this guide states
boundaries, not implementation narratives.

## Guide map

| Change area                    | Read first                                |
| ------------------------------ | ----------------------------------------- |
| Routes, identity, capabilities | Authority and identity                    |
| Queries, mutations, project UI | Project scope and TanStack ownership      |
| SSE, history, concurrency      | Streams, history, and concurrency         |
| Agents, Skills, MCP, files     | Governed assets and runtime state         |
| Knowledge bases                | Knowledge bases                           |
| Admin or Automation            | Admin and Automation                      |
| Implementation or verification | Change checklist; Tests and release gates |

## Application boundaries

The normal full stack starts from the repository root with `make dev` and uses
same-origin `/api/*` through Nginx. Override the backend base URL only for
deliberate split-origin development.

- `/workspace` is the authenticated account-wide project landing page.
  `ProjectWorkbench` owns its toolbar and project-card grid; edit controls
  follow the server-issued `project.update` capability, and card times come
  from the server's `created_at`, never `last_entered_at` or the current time.
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
Navigation rows (project cards, conversation rail) use one full-surface link;
secondary controls are sibling buttons outside the link.

## Non-negotiable client boundaries

### Authority and identity

- Gateway is the authorization authority. Frontend code renders only
  server-issued capabilities and never derives them from system or project
  roles. A route/layout gate improves UX but does not replace scoped API
  authorization.
- Project navigation follows issued capabilities: owner-private Memory under
  Work; Connections guarded by `project.channels.manage`; Agent catalog and
  existing Builder sessions require `shared_assets.read`, starting or mutating
  a design requires `shared_assets.edit`; each Project Management destination
  requires its exact governance capability.
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
  slug. Register every new domain root in `scope-registry.ts` /
  `transitionPrivateWorkScope` so scope changes cancel and remove it.
- On account/project transition: abort in-flight work, invalidate the old
  generation, remove old queries/mutations/reconnect state/clients, then create
  the new client only after both UUIDs are known.
- Every request-capable query forwards TanStack's `AbortSignal`. Late callbacks
  from a disposed client or old generation may not update the new scope; cancel
  an in-flight exact-resource query before a mutation writes that cache.
- Declare response schemas as strict Zod objects and reject unknown authority,
  owner, trace, secret, or storage fields before data reaches the UI.
- Secret-bearing create/replace operations are imperative authenticated calls,
  not TanStack mutations. Keep values in local form state, clear them after the
  request, and invalidate only safe metadata after success.

### Streams, history, and concurrency

- Durable SSE cursor state is keyed by account/project/thread. Event IDs and
  message/event `seq` values remain canonical signed-BIGINT decimal strings;
  compare by decimal length/value, never as JavaScript numbers.
- Replay and live SSE frames merge monotonically by decimal `seq`; REST
  snapshots never replace newer cached SSE frames, and duplicate or
  non-advancing frames are dropped. A newly mounted projection joins an active
  Run from cursor `0`.
- Context Usage consumes the strict Context Projection v2 contract: a monotonic
  read model keyed by Lead or the server-issued Sub-Agent `execution_id`,
  merged by decimal-string `projection_seq`. The browser sees only the safe
  Projection Head, never raw Context Evidence; the composer model is neither a
  request parameter nor cache identity.
- Agent Builder and Skill Builder Activity each have a dedicated
  account/project/session query and SSE cursor joined from cursor `0`; they
  never share Thread/Run history, reconnect state, or cache keys, render only
  the strict public Activity contract, and never invent reasoning for a
  stage-only generation.
- Resolve the one active Run only from authoritative Run/history state; a local
  duration or an intermediate `ask_clarification` message is not a terminal
  signal. When REST/history proves terminal, stop and forget that exact Run
  reconnect even if its SSE source never closes; late SSE must not overwrite
  canonical terminal history or trigger a second Run/cancel request.
- Render `GRAPH_RECURSION_LIMIT` consistently from live errors, durable
  terminals, and Run history as step exhaustion. Keep partial output visible
  and suppress direct replay and input-restoration actions.
- Scope disposal aborts the stream and prevents late state writes.
  Compare-and-remove reconnect metadata so an old consumer cannot erase a newer
  Run. A successful Thread DELETE tears down only that exact
  account/project/thread runtime and query subtree; a failed DELETE must not.
- Preserve LangGraph stream-handle property descriptors; do not object-spread a
  handle with lazy getters.
- Thread/Run/file/Builder catalogs page to completion with strict schemas,
  abort support, duplicate/progress checks, and a bounded page/offset limit;
  never rely on SDK default page sizes or silently truncate.
- Mutations that carry `expected_version` or `revision` treat `409` as an
  explicit concurrent edit: preserve local unsaved changes and refresh
  authoritative state; never silently overwrite. Cache merges are
  revision-monotonic so late responses cannot overwrite newer state.
- Conversation rendering is a lead-Agent projection. Render every reasoning,
  process-output, and tool step in canonical model-call order without generic
  aggregation; Subagent/middleware events use their dedicated task/progress
  surfaces. Withhold the delivered-file card while the Run is active, then
  mount the deduplicated card once after the final assistant answer.
- The Main Project Chat composer may select `Research` for the next Run only,
  promoted through the reserved admission context and cleared only after
  authoritative Run admission succeeds. Research is workload policy, not a
  capability or model instruction.
- Repeated-call, Run tool-call-limit, and Sub-Agent-total progress are three
  strict event contracts merged by deterministic `observation_id`; refresh,
  reconnect, or duplicated middleware execution must not double-render them. A
  Lead limit blocks later Lead tools; one Sub-Agent Task limit blocks only that
  Task.

### Governed assets and runtime state

#### Agents and Builder

- A Project Agent has one mutable server-owned Definition saved with optimistic
  `revision`; the UI exposes no Candidate, activation, version picker, diff, or
  history. A `409` recovery reloads catalog and Definition, preserves unsaved
  local edits, and adopts authority only from a newer revision.
- Agent `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, and `USER.md` are four logical
  Definition fields, not a filesystem editor. Project Skill/MCP versions are
  immutable server objects; the UI never fabricates a Current Version,
  capability, binding, or dependency closure.
- Builder sessions are account/project/owner scoped. Candidate edits remain
  revision/checksum-bound; final confirmation is one server transaction and
  does not imply activation or binding unless the response says so. An
  error-severity conflict blocks commit until a later AI turn regenerates the
  candidate; Commit's idempotency signature must match its request body.

#### Skills and secrets

- Project Skill creation exposes exactly two flows: AI Builder and validated
  archive upload; no manual metadata or template form, no browser
  risk-confirmation retry, and no scan-result presentation.
- Skill secret declaration forms are a server-parsed projection of the current
  `SKILL.md` buffer, never a browser YAML parser or second source of truth.
  Invalid or pending source blocks save, validation, and activation without
  discarding unsaved changes.
- The Skill workbench presents one `Runtime secrets` surface: one encrypted
  value per exact Skill Version and environment-variable name. Activation pins
  the payload checksum and secret revision and never submits plaintext; a
  Candidate cannot activate until all required declarations are configured, and
  AI never chooses or sees a secret value.
- Project Skill deletion is a terminal archive action: the confirmation explains
  the Skill is hidden and removed from every Agent without suspending them, and
  success removes the Skill query subtree, invalidates Agent and Builder caches,
  and shows the server-returned affected Agent count.
- System asset definitions are read-only in admin views; System Skill
  revocation is governance eligibility, not a version state. Project and System
  Skill details export the selected persisted version through the same
  `Export ZIP` action; unsaved edits disable export.
- Secret forms are write-only: blank preserves, replacement writes, clear needs
  a separate confirmation. Responses expose only configured/readiness/revision
  metadata; plaintext never enters query caches, and controls clear after
  submit.

#### MCP, models, Agent state, and files

- Project MCP authoring exposes only backend-supported remote transports and
  secret-free URLs. Credentials are dynamic Header/Query rows submitted as one
  write-only replacement per slot; the browser never probes the endpoint,
  performs discovery, or infers CIDR authorization.
- Model API keys belong to the Model Provider, never to a model form.
  Text-model connection tests address the bound `provider_id`; the provider
  dialog's candidate test carries a transient URL/Key pair that may persist in
  dialog state until close or save success and never enters caches or storage.
- System Model forms require the bounded `max_input_tokens` capability
  (`1..2,000,000`), presented as maximum input context, never as output
  `max_tokens` or a Run token budget. Provider settings render from the backend
  adapter descriptor as typed controls (common visible, `advanced` collapsed);
  never restore a raw JSON settings editor.
- Text and retrieval model Delete share one terminal soft-delete UX. Retrieval
  deletion surfaces the server rejection when a Knowledge Base still references
  the model; the client never infers reference authority locally.
- Model/thinking selections are preferences. Gateway returns the effective
  execution profile; UI history and status use that server result.
- A suspended Thread Agent keeps history readable but fails send, retry,
  edited rerun, and human-input paths closed; an archived Agent additionally
  surfaces the deleted-Agent message and requires switching Agents.
- Upload messages carry opaque ready-file IDs and safe metadata, not
  browser-made data URLs. The composer eagerly uploads on selection, paste, or
  drop without locking text editing; Send reuses the exact pending or ready
  upload rather than starting a duplicate.
- `present_files` is the explicit published-file boundary; workspace previews
  and live file-tool state do not prove delivery, and delegated scratch files
  are not publishable cards. Unknown workspace-change line counts stay unknown
  rather than fabricated zeros.
- Every `task` tool call is its own Sub-Agent card in canonical order. A card
  exposes Context Usage only with a valid server-owned execution UUID; the Tool
  Call ID is never substituted as Context authority.

### Knowledge bases

- Knowledge is a deployment-level optional module. The navigation entry renders
  only when the per-project health probe succeeds; 404 `KNOWLEDGE_DISABLED` or
  any probe failure hides it. Reads require `shared_assets.read`; create, edit,
  upload, retry, and delete controls require `shared_assets.edit`.
- `core/knowledge/` owns the strict contracts, query keys, and hooks, with its
  root registered in `scope-registry.ts`. Base and document lists poll every 2
  seconds while a row or task is active; a `deleting` row with `delete_error`
  parks until the user re-deletes. Recoverable background refresh failures keep
  the last scoped rows with an explicit retry; `401`, `403`, and not-found
  remove and hide them.
- Chunk parameters are immutable after upload; retry reuses them. Base creation
  and uploads share `knowledge-create-wizard.tsx`. Each shown `File` previews
  exactly once via the stateless `chunk-preview` endpoint; later parameter
  edits mark the preview stale and require an explicit refresh. Preview
  identity (`core/knowledge/preview-identity.ts`) tracks File, parameter
  snapshot, scope generation, and request sequence so a late response can never
  overwrite the current winner. Multi-file submission reports one verdict per
  file and retries only failed files with the frozen settings.
- Empty-base creation submits only name/description with
  `embedding_model_id: null` and never silently chooses a model. An
  unconfigured base is configured once by PATCH before its first upload;
  configured bases change embedding only through rebuild. Retrieval mode,
  reranker, and top_k/threshold defaults are base settings; the retrieval test
  overrides only its own request and omits empty inputs so the backend resolves
  defaults.
- Search labels scores as "Retrieval score" with a `score_kind` badge, never a
  confidence percentage. Hit detail pins the segment through the authoritative
  detail read with the result's expected version/digest and shows a conflict on
  drift; matched children come from response evidence, never list order. A
  base configuration change resets results so a late response cannot resurrect
  them. Diagnostics never show segment text.
- Governance mutations carry `expected_version`; a 409 keeps the unsaved form
  and refreshes authoritative rows for re-confirmation. Reparse warns that
  manual edits, disables, and attachment bindings are replaced; re-embed states
  what is preserved and reports the real accepted/skipped admission outcome.
  Knowledge workspace state lives in the URL; the document list is filtered
  client-side over a completeness-checked full fetch, and an incomplete or
  drifted read is an explicit error, never a partial table.
- Segment rows show Token counts only for Token profiles and never invent
  parser identity for null/historical character profiles. Published images are
  attachment-backed Markdown; edit sheets insert only logical refs from the
  current Document's authorized attachment catalog, and the backend revalidates
  every ref on save. The edit/add sheets' 16000-character ceiling mirrors the
  backend `KNOWLEDGE_MAX_SEGMENT_CHARS`.
- The base contract carries `default_relative_cutoff` (nullable) next to the
  threshold defaults, task progress may report the `relex_document` kind, and
  search diagnostics include `relative_filtered`, `lexical_threshold_exempt`,
  `lexical_query_token_count`, and `lexical_query_truncated` (rendered as a
  note when true). The relex admission endpoint (`POST .../bases/{id}/relex`)
  has no UI control yet.
- `/admin/settings/models` is one unified "Model providers" surface owned by
  `admin-model-settings-page.tsx` with contracts in
  `core/admin-settings/model-registry/`. Registry administration works while
  Knowledge is disabled; chat selection stays per model, never per provider.
- `/admin/settings/knowledge` is gated by current `system_admin` in layout and
  component. `core/admin-settings/knowledge/` has strict GET/PUT contracts; GET
  exposes only `secret_key_configured`, blank secret retains the value, and
  changing the endpoint requires re-entry. The page distinguishes
  restart-required settings from the summary System Model reference.
- Chat citations render only from the thread projection's validated
  `knowledge_citations` payload on the Run's final AI text message; the
  renderer degrades to "no citations" rather than trusting an arbitrary payload.

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
