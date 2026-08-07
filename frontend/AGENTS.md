# AGENTS.md

This is the source of truth for ActWeave frontend work. The repository-level
[AGENTS.md](../AGENTS.md) owns monorepo orientation; this guide owns the current
project-first routes, authorization, cache isolation, and frontend gates.

## Stack and commands

The frontend uses Next.js 16, React 19, TypeScript 5.8, Tailwind CSS 4, TanStack Query
5, strict Zod contracts, Rstest, Playwright, Node.js 22+, and pnpm 10.26.2+.

Run from `frontend/`:

```bash
pnpm dev
pnpm test
pnpm check
pnpm test:e2e
pnpm test:e2e:static
pnpm build:production
pnpm build:static
```

`pnpm check` runs lint and type checking. The production Playwright gate writes to
`test-results/core-production`; the static gate builds into `.next-static` and writes to
`test-results/core-static`, so normal and static artifacts cannot be reused accidentally.
`pnpm test:e2e` runs the deterministic dynamic-mode Chromium suite without a live model.

## Final route model

- `/workspace` is the authenticated account-wide multi-project landing page. It shows
  project cards, invitations, and recoverable projects without a project sidebar.
- `/projects/[project_slug]` is the only live project shell. Nested pages include project
  overview, members/settings, Chats, Memory, Connections, Automation, shared assets,
  Usage, and Audit according to server-issued capabilities.
- `/admin/assets/*`, `/admin/operations/*`, and `/admin/settings/*` are the platform
  administration shells.
  Their server layouts return not-found for an authenticated non-system-admin and retain
  the requested destination only for an unauthenticated login redirect.
- `BUILD_MODE=static` renders a local no-network demo at `/workspace` and returns
  not-found for all project and admin routes. Static code must not import authenticated
  API clients or send any `/api/` request.

Project slugs are resolved only by paging the member-scoped project list and exact-matching
the returned slug. UUID-only detail, enter, pin, and mutation APIs never receive a slug.
`ProjectContextProvider` is the sole slug-resolution and enter owner; nested pages consume
`useCurrentProject()` and do not repeat those requests.

## Source layout

This lists the directories these rules govern, not every directory under `src/`.

```text
frontend/src/
├── app/
│   ├── (auth)/                       # login, registration, setup, OIDC callback
│   ├── workspace/                    # live account landing or static local demo
│   ├── projects/[project_slug]/      # only live project shell
│   └── admin/                        # platform administration
├── components/
│   ├── projects/                     # project shell and project-private pages
│   ├── workspace/                    # reusable chat/message/artifact presentation
│   ├── assets/                       # shared asset presentation
│   ├── admin/                        # platform administration surfaces
│   └── ui/                           # generated UI primitives
├── core/
│   ├── auth/                         # authenticated account identity
│   ├── projects/                     # strict project contracts and provider
│   ├── private-work/                 # account+project clients and keys
│   ├── project-automations/          # Automation API and pure schedules
│   ├── shared-assets/                # project/system asset contracts
│   ├── admin-operations/             # system-admin safe operations contract
│   ├── admin-settings/               # model catalog and global policy contracts
│   ├── threads/                      # project-injected Thread state and streaming
│   └── messages/                     # pure message/human-input rendering model
├── lib/                              # cross-cutting helpers including `cn()`
├── styles/                           # Tailwind entry and theme tokens
└── env.js                            # environment validation
```

Generated primitives under `components/ui/` and `components/ai-elements/` should not be
edited manually.

## Task recipes

Entry points for the most common changes. The sections below own the invariants; these
recipes only say where to start and which wiring step is easy to miss.

### Add a page or route

1. Create `src/app/projects/[project_slug]/<segment>/page.tsx`. Both parent shells already
   exist: `app/projects/layout.tsx` resolves the account and mounts `QueryClientProvider`
   plus `AuthProvider`, and `app/projects/[project_slug]/layout.tsx` rejects static builds
   and mounts `ProjectContextProvider`.
2. Keep the route file thin. A Server Component route awaits
   `requireServerProjectCapability(slug, "project.members.manage")` and renders a component
   from `src/components/projects/`. A `"use client"` route wrapper instead reads
   `useCurrentProject()` and delegates.
3. Register navigation in `src/components/projects/project-nav.tsx` with the matching
   capability check. Creating the route file does not add a sidebar entry.
4. Never resolve the slug yourself. `ProjectContextProvider` owns slug resolution and enter;
   nested pages consume `useCurrentProject()`.

`requireServerProjectCapability` calls `notFound()` when the project is invisible to the
caller and `forbidden()` when a member lacks the capability, but it returns without blocking
when the lookup itself is `unavailable`. It is a fast SSR gate, not the authorization
boundary; the scoped API calls behind the page still fail closed.

### Add a data-fetching hook or API call

1. Declare the response contract in `src/core/<domain>/types.ts` as a strict Zod schema that
   rejects unknown authority fields.
2. Add the request in `src/core/<domain>/api.ts` using the authenticated fetcher from
   `src/core/api/fetcher.ts`, and validate the response through that schema before returning.
3. Add the key factory in `src/core/<domain>/query-keys.ts`. A project-scoped key starts at
   the account+project root the way `privateWorkRoot()` builds
   `["account", accountId, "project", projectId, "private-work"]`. Never key by slug.
4. Add the `useQuery`/`useMutation` wrapper in `src/core/<domain>/hooks.ts` and forward
   TanStack's `signal` into the request.
5. If the domain introduces a new project-scoped root, add it to the `roots` array in
   `transitionPrivateWorkScope` (`src/core/private-work/scope-registry.ts`). That array is
   what cancels and removes the previous scope's queries and mutations on account or project
   transition, so a root missing from it leaks across scopes.

Secret-bearing input never travels through TanStack. Follow the `useSecureCredentialWrite`
pattern: hold the value in local component state, call the imperative API directly, clear the
form, then invalidate the list query only after success.

`createAccountQueryClient()` returns a bare `QueryClient` with no default `staleTime`, so each
query declares its own caching and polling behavior.

### Add a UI component

1. Place feature components under the owning directory: `components/projects/` for the
   project shell and project-private pages, `components/workspace/` for chat, message, and
   artifact presentation, `components/assets/` for shared asset presentation, and
   `components/admin/` for admin surfaces.
2. Compose from the generated primitives in `components/ui/`; aliases are declared in
   `components.json`.
3. Merge conditional classes with `cn()` from `src/lib/utils.ts`. Tailwind 4 enters through
   `src/styles/globals.css`; a class name assembled at runtime needs an `@source inline(...)`
   entry there or it is purged from the build.
4. Import across modules through the `@/*` alias rather than relative paths.

### Change streaming or SSE handling

1. Frame acceptance, cursor state, and reconnect storage live in
   `src/core/private-work/api-client.ts`. `acceptProjectStreamFrame` compares canonical
   decimal strings and drops duplicate or non-advancing frames.
2. The UI-facing hook is `useThreadStream` in `src/core/threads/hooks.ts`, consumed by
   `ScopedChatPage`.
3. A newly mounted projection joins with `lastEventId: "0"` deliberately, because a shared
   cursor may have been advanced by an old invisible consumer whose frames this UI never
   rendered. Do not turn that into a resume from the stored cursor.
4. Never object-spread the LangGraph stream handle. `overlayThreadProjection` rebuilds it from
   property descriptors so the SDK's lazy getters survive.

### Add a unit test

1. Create `tests/unit/<mirror of the source path>/<name>.test.ts` or `.tsx`. `rstest.config.ts`
   collects only `tests/unit/**/*.test.{ts,tsx}`.
2. Import through `@/…`; that alias is configured in `rstest.config.ts`. There is no setup
   file and no `tests/support/` directory, so helpers and mocks are declared in the spec.
3. Use `rs.fn` and `rs.stubGlobal` from `@rstest/core` and restore with
   `rs.unstubAllGlobals()` in `afterEach`. Dispose any scoped client the test created.

Run `pnpm test` for the suite or `pnpm test <path>` for one file.

### Add or change a Playwright E2E test

Three gates, each with its own config and output directory so artifacts cannot be reused
across modes:

| Gate             | Specs                     | Config                              | Command                                                                |
| ---------------- | ------------------------- | ----------------------------------- | ---------------------------------------------------------------------- |
| Dynamic (mocked) | `tests/e2e/`              | `playwright.config.ts`              | `pnpm test:e2e`                                                        |
| Static boundary  | `tests/e2e-static/`       | `playwright.static.config.ts`       | `pnpm test:e2e:static`                                                 |
| Real backend     | `tests/e2e-real-backend/` | `playwright.real-backend.config.ts` | `pnpm exec playwright test --config playwright.real-backend.config.ts` |

1. Keep specs deterministic: mock `**/api/**` inside the spec and use fixed UUIDs and
   timestamps. The dynamic web server runs with `DEER_FLOW_AUTH_DISABLED=1` and points the
   Gateway at a dead port, so an unmocked request fails instead of reaching a real backend.
2. `pnpm test:e2e` names one spec explicitly. A new dynamic spec needs that script updated or
   an explicit path on the command line, or no gate will run it.
3. `PLAYWRIGHT_SKIP_WEB_SERVER=1` reuses an already running server instead of rebuilding.

## Project authority and client ownership

Platform role is exactly `system_admin | user`; project membership role is a separate
`admin | editor | runner | viewer` domain. Frontend code never derives capabilities from
either role. It renders only capabilities returned by Gateway.

Authentication probes strictly validate both the User response and the exact
`{needs_setup, registration_enabled}` setup-status response. Registration stays closed while
that status is checking, unavailable, malformed, or explicitly disabled. Remember-me is an
explicit backend session-lifetime input across local login, registration, OIDC, initialization,
and forced setup; browser storage may retain only the preference and email, never a password or
session material. Client identity and account-scoped caches are cleared only by an authoritative
401 or explicit logout. Network errors, 5xx responses, 403, and malformed 200 responses retain the
current identity while surfacing availability failure. `refreshUser()` returns that distinction
as `authenticated | unauthenticated | unavailable`; OIDC callback and forced-setup callers must
offer retry for unavailable rather than reporting an auth failure or clearing state. Protected
redirects preserve the complete safe browser pathname and query in `next`; client-side redirects
also retain the fragment, which is never sent to the server. All probes and submissions have
bounded timeouts and abort on supersession or unmount.

Email case folding and uniqueness are Gateway/PostgreSQL responsibilities; frontend identity and
cache keys use the returned canonical account UUID, never a user-entered email string. A checked
remember-me control means only that the browser may retain its cookies under the server's
transport policy. It is not proof of authentication, durable-session validity, system role,
project membership, or capability, and the UI must never derive any of those from the preference.

`ProjectPrivateWorkProvider` owns the only live project client. Its scope contains exact
authenticated account UUID plus entered project UUID. Private-work, Automation, shared
asset, Usage, Audit, and reconnect state all derive their roots from the same pair. There
is no module-level default client, optional unscoped client, or URL fallback.

On account or project transition, always:

1. abort/cancel in-flight queries and mutations;
2. invalidate the old generation so late callbacks cannot commit;
3. remove old scoped queries, mutations, reconnect metadata, and clients;
4. create the new scoped client only after both identities are known.

Every request-capable hook must accept and forward TanStack's `AbortSignal`. A late response
from an old account/project must never update the new scope.

## Project-private data flow

### Project API surfaces and channel configuration

Project clients use `/api/projects/{project_id}/private-work` for Thread, Run, file,
artifact, input-polish, and durable SSE operations. Project Memory uses
`/api/projects/{project_id}/memory`; Connections use
`/api/projects/{project_id}/connections`; Admin group binding uses
`/api/projects/{project_id}/channel-group-bindings`; safe project provider configuration uses
`/api/projects/{project_id}/channel-instances`; Automation uses
`/api/projects/{project_id}/automations`.

Channel instance GET state is account/project scoped and may be cached. Provider Secret writes
must use direct imperative authenticated requests, must never enter TanStack Query/Mutation
variables or cache, and must be cleared from the form immediately after submission. All members
may read bounded provider status; only the server-issued `project.channels.manage` capability
shows configuration, enable/disable, or delete controls. Personal external-account and Agent
selection remains a separate `private_work.create` flow.

The current group-binding surface is Feishu-only although the backend DTO is provider-neutral.
Only `project.channels.manage` may see or mutate it. The Admin chooses an available Agent, receives
one `/bind-project` command, copies it to the target Feishu group, and explicitly checks completion.
The resulting rows show only group name, Agent, running/disabled state, and recent activity, with
compact change-Agent, enable/disable, and delete actions. Do not fetch or render message bodies,
Thread/Run content, raw provider identifiers, or guest identities. Group guests are not web users
and their owner-scoped Threads must not be merged into the normal signed-in conversation menu.
Personal `p2p /connect` UI and behavior remain independent.

### Navigation gating and project Memory

Chats, Memory, Connections, and Automation navigation require a non-static build, server
readiness `ready`, and `private_work.read_own`. Create/run/upload/connect controls additionally
require their exact server capability. Viewer can read/list/export and delete their own
ready upload/workspace/output files, but never sees mutation controls that require
create/manage authority.

The project Memory page presents one owner-private long-term document, its current version and
pending count, immediate Dream admission, and bounded version history with the real unified diff
and explicit restore confirmation. The client uses only `/memory`, `/memory/dream`, and the
`/memory/versions` family; it never sends owner or namespace. Every response is strict Zod,
requests forward the active AbortSignal, and loading, error, empty, running, and pagination states
remain distinct. Read, Dream, and restore controls derive only from server-issued capabilities.
A `409` refreshes the scoped Memory root rather than applying an optimistic overwrite.

`/Dream` drains the existing current Thread with the dedicated
`keep={type: messages, value: 0}` boundary before admitting Dream with that exact `threadId`.
It repeats whole-turn compaction until Gateway explicitly returns `not_enough_messages`; any
other non-compacted result, missing checkpoint progress, or the bounded pass limit fails closed
and does not admit Dream.
`/dream-log [version]` navigates to the Memory page, and `/dream-restore <version>` requires an
explicit confirmation before calling restore. These built-ins, like `/compact`, never enter the
ordinary Agent message stream.

### Durable SSE cursor and stream handling

Durable SSE cursor and deduplication state is keyed by account/project/thread. Event IDs are
thread-monotonic; duplicate IDs and duplicate terminal frames are ignored. Gateway restart
must resume from the stored `Last-Event-ID` without cross-scope replay.
Treat the PostgreSQL cursor as a canonical signed-BIGINT decimal string, never a JavaScript
number. Compare it by decimal length/value, reject non-canonical or overflow input, and persist
only monotonic advances. A newly mounted Thread projection joins the current Run from cursor
`0`; a cursor advanced by an old invisible consumer is diagnostic state and does not prove the
new UI rendered those frames. Project clients own an AbortController and active generation:
disposed clients may neither yield late frames nor update cursor/reconnect storage. Reconnect
metadata deletion is compare-and-remove so an old consumer cannot erase a newer Run.

The same no-`number` rule applies to the four non-SSE private-work feeds for per-Run messages,
Thread messages, per-Run events, and Thread events. Their `seq`, `before_seq`, and `after_seq`
values remain canonical decimal strings through parsing, sorting, and pagination. Thread history
and historical Subtask-step loading compare strings by decimal length/value and fail closed on
numeric, non-canonical, or signed-BIGINT-overflow responses. The privacy-center NDJSON attachment
is not consumed by these Thread/task clients and remains outside this contract.

React cleanup defers the local stream detach so a Strict Mode remount can retain it. The eventual
detach clears only the local SDK projection and must not send backend cancellation. Project chat
requests remain root-stream only; namespaced child custom frames cannot update root task state.

Do not object-spread the LangGraph SDK stream handle: it exposes enumerable lazy getters, and
`toolCalls` can traverse a transient sparse message array while `RemoveMessage(__remove_all__)`
compaction rebuilds message-tuple indexes. Preserve the handle with property descriptors and
override only ActWeave's normalized `messages`, scoped `values`, and local `stop` projection.

### Conversation history and catalog pagination

Conversation history is a lead-Agent projection. Historical rows tagged with a subagent or
middleware caller are hidden, and their tool results are filtered by the issuing AI
`(run_id, tool_call_id)` pair; internal HumanMessages are hidden unless they are the explicit
Run-admission row. This compatibility filter must remain until every retained Run was written by
the lead-only journal contract. Rendering also associates tool results with the exact issuing
AI group by `(run_id, tool_call_id)`, including late/replayed and result-before-call pagination
order. Legacy rows without a Run ID are isolated to one Human turn. Never attach an unknown
orphan tool result to the most recent or final assistant group, and always synthesize a stable
non-empty group key when legacy messages have no ID.
After checkpoint compaction, a Run-admission HumanMessage may exist only in the complete journal
while the materialized checkpoint retains that Run's tail. History/live merging must restore the
admission before the first message of the same Run, not append it after the Run on refresh.

Thread history must enumerate the complete newest-first Run catalog before offering per-Run
message pagination. Never rely on the LangGraph SDK's default `runs.list()` limit of 10:
page with explicit bounded `limit/offset`, preserve the server's stable order, forward the active
scope's abort signal, validate every public Run through a strict schema, and reject unknown
authority/private fields. Duplicate rows caused by concurrent offset drift may be deduplicated,
but a non-advancing full page, malformed page, maximum page count, or maximum offset must fail
closed. Message bodies remain lazy and are loaded one Run/page at a time.

Ready-file lists are also complete catalogs, not a single default page. Fetch bounded pages of
100 with the active AbortSignal, strictly validate every file row, and accept `X-Next-Offset`
only when it is canonical, advancing, and within the configured safety bound. Duplicate file IDs,
unknown fields, a full page without a usable next offset, or excessive page count fail closed.
Thread rename/delete mutations send the last server-issued `expected_version`; a 409 is an
explicit concurrent-edit result and must never be converted into an optimistic silent overwrite.

### Run presentation: artifacts, reasoning, and subtasks

During a live project Run, successful lead-Agent `write_file` and `str_replace` tool calls select
the written file in the artifact preview, open the right-hand file panel, and collapse the desktop
project navigation. Trusted stream messages tagged `subagent:<name>` are internal progress and
must be excluded from the lead conversation projection: their steps render only in the matching
SubtaskCard, and their temporary file writes never select or open artifact preview. `present_files`
remains the explicit publication boundary for rendering downloadable file cards inside the
conversation. Terminal Run handling must invalidate the project Thread file list so finalized
UUID-backed file routes are available without a reload. Once that ready-file query settles,
replace a selected `write-file:` URL with its matching durable logical path; never perform this
replacement while the Run or ready-file refetch is active, because an existing path may still
point at the prior file version. The toolbar Files action is a directory action when ready files
exist: it clears the detail selection, opens the file list, and exposes each file through a
separate keyboard-operable open button that does not overlap download/delete controls.

After a terminal assistant answer exists, the UI keeps the safe semantic groups unchanged but
projects any immediately preceding processing, Subagent, and `present_files` groups into one
result. Earlier lead-Agent reasoning, tool calls, Subagent cards, and the terminal message's own
reasoning render in chronological order inside one compact, collapsed-by-default “Execution
details” disclosure before the final answer.

Every AI message keeps its own `ThinkingDisclosure`; reasoning must never be flattened into a
generic execution step or combined with a later model call. Each disclosure reads that message's
server-observed `additional_kwargs.reasoning_duration_ms`, floors it to whole seconds, and uses
“under 1 second” for an observed sub-second interval. Opening the process disclosure shows the
complete ordered history without a second “more steps” fold. In a completed turn that has this
execution history, the terminal message's reasoning appears exactly once as its last reasoning
disclosure; the answer and compact published-file rows remain outside and follow it. A simple
direct answer with no preceding execution history keeps its own standalone reasoning disclosure.
Missing or invalid legacy durations remain the neutral
“Reasoning” label; the UI never substitutes Run duration. Exact Run duration remains a separate
“Completed in” row after the result because it includes model latency, tools, Subagents, queues,
and wait time. `present_files` transition prose is not repeated beside the result. During a live
Run, every reasoning round remains represented and the current round opens automatically;
failed-before-answer and clarification groups retain their original presentation so active work
still has visible progress.

Subtask state folds effective model and cumulative Token metadata from `task_started`,
`task_running`, terminal custom events, and the authoritative terminal ToolMessage. Older delayed
usage snapshots must never reduce a displayed cumulative total. The SubtaskCard resolves a known
model to its configured display name, falls back to the model identifier, and hides per-subtask
Token totals when global `token_usage.enabled` is false. Keep project-scoped historical step
fetching and the `inferred < custom_event < tool_result` status authority ordering intact.

The composer context-window indicator is separate from cumulative Thread/Run Token usage. It reads
the current retained checkpoint through the strict project-scoped `context-usage` contract and
measures progress against the current automatic-compression trigger. `tokens`, `fraction`, and
`messages` keep their own units; multiple triggers are OR conditions and the server-selected
primary trigger drives the compact ring. Hide the indicator for new/mock/non-runnable Threads, and
invalidate it after terminal Runs, stop, `/compact`, and `/Dream`.

### Composer submission and execution profile

Input polish is project-scoped and never runs without `private_work.create` plus
`shared_assets.execute`. The server revalidates the current Thread Agent snapshot and
Credential-grant closure; the browser never constructs authority fields.

Composer model and thinking choices are Run preferences, not browser authority. Normal submit
and replay derive one complete `execution_profile` from the selected active model and its
declared thinking/reasoning capabilities. The SDK adapter promotes that profile into the strict
top-level private-Run field and removes its reserved transport key before the request crosses the
trust boundary; generic context/config copies of model or reasoning fields remain stripped.
Gateway may honor a model choice only for a `default`-bound Agent, rejects a conflicting choice
for an exact-model Agent, and returns the effective model, thinking switch, reasoning effort, and
vision capability on the Run. The UI must use that effective profile for historical/execution
presentation and must never imply that the local selection proves admission or provider support.

Model and mode values each keep an explicit-selection marker per Thread. A missing/non-explicit
Thread override inherits the current global/catalog default for display and submission, so a stale
legacy value cannot be shown as selected while the request silently omits it.

The main composer and Sidecar resolve the Thread Agent's current `model_ref` before submission:
an exact-model Agent displays a locked model picker and omits `model_name`, while a `default`-
bound Agent keeps the user's explicit thread/global choice. Locking an exact Agent must never
overwrite or clear that persisted preference. An existing Thread fails closed when Agent or
version resolution fails, a System binding/current published version is missing, the active model
catalog is loading/unavailable/empty, or an exact model is absent from that catalog: submit, human
input, edit-and-rerun, and regenerate remain disabled while an actionable retry is shown. A
`/new` draft is not blocked merely because server-issued Thread metadata does not exist yet.

Upload messages carry only ready-file metadata and opaque file IDs, never browser-created image
data URLs. For an exact admitted vision model, Worker derives current-message images from its
server-owned file authority and injects them ephemerally into lead model requests. Text-only Runs
keep the same files available as ordinary uploads but receive no automatic pixel input. The
browser must not claim that setting the catalog's vision flag alone proves provider compatibility;
that flag gates the execution path, while a real provider vision request remains the target-
environment smoke test.

## Shared assets and credentials

### Asset catalog and Agent authoring

Project asset pages group visible system and project Agent, Skill, MCP, and Credential rows.
Queries are keyed by account, project, and kind. UI actions use per-item capabilities and
optimistic revisions; no role-based inference is allowed.

Project Agent details expose four fixed logical Markdown entries: `AGENTS.md`, `SOUL.md`,
`IDENTITY.md`, and `USER.md`. They are an asset-level editor over Agent-version fields, not a
filesystem browser: do not add directory, create, rename, delete, breadcrumb, or independent
file-version controls. New Agents open this editor even before any runtime version exists.
The editor always uses the current published revision, otherwise the latest Draft. Agent
revisions remain an internal Run-snapshot boundary: the detail sheet exposes no publish/version
summary card, selector, status, history, technical metadata, or manual version action. Its header
shows the formatted update time rather than repeating the slug. One explicit save submits all
four values with the asset revision; dirty state blocks close, while a `409` keeps the local
draft for retry.

New project Agents are designed through `/projects/{project_slug}/agents/new` and the resumable
`/agents/new/{session_id}` workspace. The name step creates only a private Builder session.
The workspace keeps the page shell fixed, scrolls only its conversation region, renders bounded
clarification cards and four fixed logical-document progress items, and allows preview/edit before
one final confirmation. Completion creates a published version 1 but leaves the Agent suspended;
cards and details expose the capability-checked activate action. Never fall back to the former
generic Agent create dialog or sequence a bare Agent create before the Builder commit.

### Skill authoring and package import

Project Skill creation offers AI conversation, blank creation, and archive import from one menu.
Conversation creation uses `/projects/{project_slug}/skills/new` and the resumable
`/skills/new/{session_id}` workspace. The name step creates only a private Builder session.
The workspace preserves the selected file while generated files change, selects `SKILL.md` only
when the first candidate package appears, locks conversation while local file edits are unsaved,
and requires an explicit checksum-bound validation before one atomic commit. Warnings require
acknowledgement; commit publishes version 1 while leaving the Skill suspended and unbound.

Skill Builder queries and mutations use their own account+project root and are aborted and removed
with the active private-work scope. A message turn clears the composer and renders an optimistic
user bubble immediately; the pending mutation's expected revision scopes that bubble so the
canonical server message replaces it without duplication. Network or failed-session responses
restore the submitted draft, and backend-unavailable errors use localized copy instead of exposing
raw storage or proxy messages.

The candidate workbench reconstructs folders from slash-separated file paths, so generated
`scripts/`, `references/`, and `templates/` files remain independently selectable and editable.
It does not persist empty folders or flatten nested paths. Builder currently exposes manual
replacement of existing generated files only; creating, deleting, or renaming files is done by a
subsequent AI candidate update even though the server draft-update contract supports those ops.

Blank Project Skill creation remains one scoped backend mutation that atomically creates the
disabled asset plus version 1 Draft containing a backend-valid root `SKILL.md`; the frontend must
never sequence a separate asset request and version request. The template frontmatter name is the
immutable asset slug. After the catalog refresh, the detail sheet opens the returned asset ID,
loads its version history, and selects Draft version 1. The detail sheet has no blank-version
action: its single “创建新版本” entry starts an editable copy of the currently selected immutable
version, and saving persists that copy through the Skill fork API. Unsaved file-workbench changes
block publish, version switching, and competing version creation until the user saves or
explicitly discards them. Slash suggestions exclude assets without a published version and honor
the server-provided execute capability and system binding. Shared-asset mutations use the active
project scope's abort controller and are removed with their project query/mutation roots during
scope transition.

Project Skill package import accepts `.zip`, `.skill`, `.tar`, `.tar.gz`, and `.tgz` through the
scoped multipart upload API. A successful import creates and publishes the first immutable
version while leaving the new Skill disabled, refreshes the project Skill catalog, and opens the
returned version. Duplicate-name, invalid-package, and size-limit responses must use safe,
actionable messages without exposing parser or storage details.

New project Skills start disabled. Project-owned Skill rows and detail sheets expose the same
enable/disable switch: enabling requires `shared_assets.manage_bindings` plus a published version,
while a disabled Skill remains editable and publishable. The version workbench renders archive
paths as an expandable folder tree and opens only the selected file. New-file creation requires a
target folder and may create a nested folder inline; empty folders are local editor state because
immutable Skill snapshots persist files rather than directory entries.
System Skill binding is list-only: its catalog row keeps the enable/disable switch, while its
detail sheet never exposes enable-to-project or pinned-version switching actions.

### Asset lifecycle, deletion, and platform governance

Project Skill lifecycle has no archive or pause action. A project-owned Skill with
`shared_assets.edit` exposes permanent package deletion from its detail sheet; the confirmation
must state that every version and file will be removed, keep the destructive button disabled for
five seconds, and close the detail plus remove list/version/file caches only after the scoped
DELETE succeeds. System Skills never expose deletion. Agent lifecycle exposes activate/suspend
transitions but no archive mutation; project Agent screens label them as enable/disable. Project Agent
cards never expose deletion. A project-owned Agent with `shared_assets.edit` exposes permanent
deletion only from its detail sheet, using the same five-second delayed confirmation pattern and
removing the Agent plus all of its settings only after the scoped DELETE succeeds. Referenced Agents
remain visible and the API returns conflict rather than deleting Threads, Automations, or Run
snapshots. Reversible Agent enable/disable actions use neutral styling; the detail action is labeled
“删除”, and only that permanent action uses destructive styling. Project MCP details follow the
same reversible enable/disable wording for assets with a current Published version and do not
show archive as a primary action. A project-owned MCP with `shared_assets.edit` exposes permanent
deletion only from the detail danger zone, using the five-second confirmation pattern; System MCP
items never expose deletion. A conflict keeps the MCP visible when an Agent or historical Run
snapshot still references it.

Global `/admin/assets` Agent, Skill, and MCP pages render the packaged PostgreSQL catalog as
read-only governance metadata. They must not expose definition create/edit, new-version,
publish, approval, archive, or suspend controls or client mutations. A published packaged
System MCP may expose only the dedicated Credential-grant configuration flow; that flow sends
Credential version IDs plus expected active grant revisions and does not republish or alter
the MCP definition. System Credential lifecycle controls and project-scoped asset override
authoring are separate surfaces and remain mutable.
Platform asset pages render only exact System-scope rows. The system-admin project-governance
routes render only rows owned by the selected project and never expose the System catalog or its
binding controls there; mixed-scope responses are filtered again at the presentation boundary.
This does not change member-facing project asset pages, which still combine authorized System
bindings with project-owned assets where runtime selection requires both.

### Credentials and Skill Credential binding

Credential create/replace is an imperative authenticated request, not a TanStack mutation.
Secret-bearing form values must never enter QueryCache or MutationCache, must be cleared after
submit, and must not remain in the DOM. Responses and errors may show safe status metadata but
never plaintext, ciphertext, nonce, key ID, storage locator, secret hash, or raw provider
payload. MCP versions with required Credential slots use submit/approve rather than direct
publish.

Replacement only mints a version, so the system Credential detail reports what stayed behind. The
count comes from the replace response's `pending_migration` field and is never recomputed from a
version list; a `null` report is treated exactly like nothing pending. A positive total renders
one conditional `role="status"` notice carrying the number, the identifiable system-model share,
and the migrate action itself, so the administrator never has to remember to find that entry. A
zero total, a successful migration, and any later Credential selection leave the surface silent —
this is a state report, not standing explanatory copy.

Credential deletion is available from the project, admin-project, and system Credential detail
surfaces, never from list rows. It uses a five-second delayed destructive confirmation and sends
the visible Credential revision for optimistic concurrency. Success removes the Credential from
ordinary lists and details because deletion is logical; only the append-only audit event remains
visible. A deleted name may be reused, and no browser cache may retain deleted Credential
metadata, grants, or Skill bindings.

The Skill detail Credential section is reference-only. It renders the selected published
version's declared environment-variable requirements, lets an authorized member bind an eligible
existing project Credential version, and submits the complete binding set with
`expected_revision`. Query keys include account, project, and Skill; a `409` preserves the local
draft and asks the user to reload. The UI never accepts or reads a secret value on this surface,
and a Skill version with no declared requirements shows an explanatory empty state rather than a
secret editor.

### Project MCP authoring

Project MCP authoring exposes only `http` and `sse`. The URL is required and described as a
Worker-reachable HTTP or HTTPS endpoint whose host is exactly `localhost` or a canonical IPv4 or
bracketed IPv6 literal. Ordinary DNS hostnames are rejected and never resolved, avoiding a
validation/connection DNS TOCTOU boundary. Exact `localhost` is matched case-insensitively and
deterministically normalized to `127.0.0.1`; IPv6 loopback must be written explicitly as `[::1]`.
Every IP must belong to an
administrator-configured CIDR range, whose defaults cover common local and private networks.
CIDR policy is platform configuration: the form neither asks the user to select a network nor
tries to infer membership, and the backend remains authoritative. Embedded credentials, query
parameters, and fragments are rejected. Header and query Credentials travel in plaintext over
HTTP, so HTTP is only for trusted private networks and untrusted links must use HTTPS. Project `stdio` and
`streamable_http` are never offered for new versions. Literal env/header and OAuth editors are
not exposed; authentication is configured through encrypted Credential slots targeting either
`headers` or `query`. New forms default to `http`, label it as Streamable HTTP, and persist only
the secret-free base URL. Pasting a query strips it immediately and explains that query values
belong in a Credential; create failures distinguish an operator CIDR-policy/startup-restart issue
from approval failures where the Credential group and field names do not exactly match the slot.

Project Credential create/replace dialogs accept `query` fields without caching or returning
their values. Historical
unsupported versions remain readable with an explicit blocked reason; Project MCP history
exposes only the remote HTTP(S) origin, never a persisted path or query. Publish, binding, and
Agent dependency selection stay disabled. The backend policy remains authoritative, and
mutation failures stay visible in the active dialog without clearing the user's safe inputs.

The dedicated
`GET /api/projects/{project_id}/mcp-servers/{asset_id}/configured` authoring query is the only
exception to origin-only presentation: it requires edit authority, returns the safe current or
actionable pending configuration with its complete validated IP-literal path, and still never
returns userinfo, query parameters, fragments, or Credential values. Edit UI must wait for this
exact account/project/asset-scoped query and must not fall back to a history projection.

Initial project MCP creation is one “添加 MCP” dialog and one configured-create mutation, never
a bare asset create followed by a separate revision create/publish request. The base form shows
name, slug, description, transport, and URL, followed by explicit `headers`, `query`, or no-auth
choices. For an authenticated MCP, the form accepts field names only and shows only active project
Credentials whose current payload schema exactly matches that group and ordered field list; an
authorized Admin may create a fixed-schema Credential through the imperative secret-write API.
That contextual create flow hides the generic type field and fixes `credential_type` to
`mcp_auth`; generic Credential-management create surfaces keep the editable type field. Existing
MCP Credential eligibility remains scope/status/current-schema based and must not filter by type.
`mcp_auth` is display/classification metadata, not an approval or runtime authorization boundary.

Configured create and edit still persist only the secret-free MCP definition. Editing reuses the
same two-column form, authentication choices, compatible-Credential selector, and contextual
Credential creation flow as initial creation. No-auth save publishes immediately. When an Admin
with `mcp.credentials.approve` selects a matching Credential, the same save action follows the
configured create or PUT with approval automatically; approval failure keeps the pending version
and retries approval only, never repeats the configured mutation. An Editor without that capability
submits the pending version for Admin handoff and cannot bypass approval. This approval state is an
internal authorization boundary only: project MCP UI describes it as Credential binding, renders
`pending_approval` as “凭据未绑定 · 尚未生效”, and exposes no approval prompt, status, or separate
“批准并发布配置” action. Selection waits for the invalidated catalog to return
the authoritative `ProjectAssetItem` before opening details and never fabricates capabilities or
bindings from the aggregate response. The dedicated configured PUT atomically creates the next
internal revision and advances it directly to Published or Pending Approval; it must never leave a
user-inaccessible Draft.

### MCP configuration presentation and tool inventory

All MCP surfaces present one logical configuration. They render no history selector, revision
number, rollback target, or user-facing version terminology; project, admin-project, and system
catalog details select only the safe current or actionable pending configuration and fail closed
when the exact pointer cannot be confirmed. System MCP binding has no revision picker: its
`sync-current` mutation resolves and locks the current Published revision on the server while the
persisted binding and Run snapshots continue to pin the exact internal revision. System MCP
binding controls are list-only like System Skill: the row switch enables the server-authoritative
current configuration or disables the existing binding, and an enabled stale binding exposes one
compact inline update action. The detail sheet keeps “当前配置”“项目使用”“最近更新” in one
three-column summary row but never exposes binding actions. A project-owned MCP detail header does
not repeat a “项目自建” badge. Its summary renders current configuration, recent update, and the
lifecycle badge in one desktop row; the action row contains only actions. The connection summary
renders transport, timeout, and URL in one desktop row with a wider URL column. MCP details do not
render Credential slots or grant state; Credential selection and creation remain confined to
configured create/edit flows. System-owned MCP details retain the “系统提供” source badge. Internal
API/type names remain version-based. JSON import is intentionally absent for now.

This restriction is scope-specific: packaged System MCP retains the runtime-supported `stdio`,
`sse`, and `http` transports plus their existing env/header/OAuth credential capabilities;
only transports or definitions that the private runtime cannot execute are blocked.

Published MCP details load the service-tool inventory through a separate project/account/asset/
version query. The table shows original provider tool names and plain-text descriptions from the
last Worker discovery, never endpoint, schema, routing, Credential, or raw error data. Drafts do
not query it. The UI distinguishes initial loading, request failure with retry, never discovered,
testing, ready empty, degraded with last-known tools, failed, and stale after version or
Credential-grant changes. Publishing a project configuration automatically admits one Worker
discovery, including after Credential approval. While it is queued or running, the exact inventory
query polls every two seconds; polling stops at a terminal status. Editable and executable project
MCP details expose “Test service/Retest”, which submits another discovery Job and refreshes only
that exact inventory. System MCP and users missing either capability do not see the action. Opening
the sheet only reads/polls Gateway state and never makes the browser contact MCP. The inventory
remains display-only and cannot be used to infer runtime health or skip Worker discovery.

### Agent defaults and Skill suggestions

Agent cards expose the project default as read-only state to project members; only a member with
`shared_assets.manage_bindings` may set an active, published, executable project Agent as the
default or restore Main. Ordinary new-conversation entry points and Builder continuation share
one project-new-chat path: they omit explicit Agent fields so Gateway resolves the current
default atomically. Agent-card chat remains an explicit override, and a configured but invalid
default fails closed rather than silently switching to Main. Agent cards, the Agent selector,
project-new-chat, Connections, and Automation creation/resume/manual-run use the same fail-closed
MCP dependency assessment for project and ordinary System Agents. Main is the one exception: it is
a project-scoped orchestrator, remains selectable without a project Agent binding, and is never
offered through the System-Agent binding action. The browser must not read Main's static
Skill/MCP references, enable or move bindings, or fan out dependency requests before creating a
Thread, Connection, Automation, or group binding. Gateway resolves Main's effective current-project
System and project-owned Agent/Skill/MCP closure and the Run freezes exact versions; assets from
another project are never eligible. An ordinary project or System Agent continues to use only the
exact Skill/MCP versions referenced by its selected version.

Composer slash-Skill suggestions follow the Thread Agent. Main shows every effective Skill in the
entered project. An ordinary project or System Agent shows only Skills referenced by its selected
Agent version. This runtime catalog is account+project scoped and fails closed while the Agent
version is unresolved. Historical human-message rendering intentionally keeps the broader visible
project catalog so an old `/skill` message does not degrade merely because the current Agent or
version changed.

## Automation

Automation definitions and occurrences are scoped by exact account, project, and owner.
Admin, Editor, and Runner controls appear only with the matching server capability; Viewer is
read-only. Every key begins with the authenticated account and entered project roots.

Create sends a complete payload. Edit sends a sparse PATCH based on normalized semantic
changes only. Equivalent once timestamps such as `Z` and `+00:00` do not count as a schedule
change. Pure cron/once validation and recipes live under
`core/project-automations/schedule/` and contain no URL, fetch, auth, or query-key behavior.
Manual trigger uses a UUID idempotency key and the same durable admission path as Scheduler.
Remote-data starter recipes bound their tool attempts and require an explicit partial-result
fallback instead of retrying an unavailable provider until the Run recursion ceiling.

## Project governance and system administration

Usage and Audit pages mount hooks only after their exact readiness and capability gates pass.
Usage distinguishes configured/effective limit, used/reserved amount, and one 80% warning per
dimension. Audit accepts a closed action enum and action-specific strict metadata, and never
renders private target digests, owner/project internals, or secret content.

The project overview mounts its Token trend query only after `project.usage.read` is present.
Its query key stays under the exact account/project governance usage root, forwards the TanStack
abort signal, and strictly validates 24 consecutive hourly buckets plus independently summed
input/output/total counters. Loading, unavailable, and valid all-zero states remain distinct;
the UI must not fabricate zero usage from a failed or malformed response.

Admin operation pages mount no query until the authenticated identity is confirmed as
`system_admin`. Their strict Zod contracts reject unknown owner, Run, Thread, payload,
exception, locator, or secret fields. Closed/degraded readiness displays unavailable state,
not fabricated zero counts. Safe requeue is shown only when the server returns exact
eligibility for a parentless retention-purge predecessor.

The Job catalog searches by project display name or slug and renders those human fields as the
primary project identity. Project UUID remains available only through an accessible copy action
for support and recovery workflows; it is not a user-facing search input or row label.

Platform administration uses one compact shell for operations, projects, jobs, audit, assets,
model settings, and system settings: a persistent 64px collapsed / 240px expanded desktop rail, a page-context top
bar, collapsed-item tooltips, a localized mobile menu, and a persistent `/workspace` escape action
in desktop and mobile navigation. Catalog pages render dense filterable rows and mount a responsive
detail inspector and version-history query only after explicit selection. The desktop inspector
overlays the catalog without changing its width or table/card presentation; responsive table/card
switching follows only the page's available width. They must not eagerly expand or fetch every
asset. Cursor pages retain reversible local
history, technical identifiers stay copyable, and every admin Dialog/Sheet receives a localized
close label with an accessible hit target.

System settings use strict, secret-free section contracts under the authenticated account query
root. Each section is replaced atomically with its expected revision; conflict responses preserve
the local draft, and the UI renders the server-confirmed effective revision, effect scope, and any
pending runtime roles. Agent model references are limited to active system logical model names.

The ordinary Settings dialog has a separate account-owned Personalization section. Its Memory
switch and reset action use the strict account API and `preferences_version` CAS; they are not
local settings and do not edit the platform runtime policy. Reset confirmation must state that
chats and `/compact` summaries are preserved. Success cancels and removes every project Memory
query for the active account while leaving Thread and other private-work caches intact.

## Component ownership

- `ProjectContextProvider` owns project resolution and enter.
- `ProjectPrivateWorkProvider` owns the scoped client, reconnect state, and teardown.
- `ScopedChatPage` owns project composer busy state, branch/edit/regenerate actions, and navigation.
- `ProjectConversationRail` preserves the server's `updated_at DESC, thread_id DESC` pages and,
  only on the bare project `/chats` route after a settled successful query, replaces the route with
  the first Thread. Empty/error states remain on the landing page and direct Thread URLs never
  redirect.
- `MessageList` owns human-input answered/latest/pending gating, latest-turn edit eligibility,
  and the single group-tail Run-duration display.
- `core/threads/hooks.ts` owns pre-submit upload state, scoped prepare/submit replay, optimistic
  replacement, and replay failure rollback.
- `core/threads/agent-mode.ts` owns the capability-aware composer mode contract. Flash disables
  extended thinking and explicitly requests `none` when the selected model supports effort
  controls; Thinking, Pro, and Ultra request low, medium, and high. Mode no longer grants
  plan or subagent behavior.
- `core/private-work/execution-profile.ts` owns the strict requested/effective profile types and
  removes legacy model/reasoning keys before submit. `core/api/api-client.ts` is the sole SDK
  compatibility adapter that promotes the reserved carrier to top-level `execution_profile`.
- Project Memory and Connection pages own their scoped queries and mutations; the Memory page owns
  the document, Dream, version/detail, restore, and conflict-invalidation roots, while shared
  presentation components remain pure.
- Static demo fixtures and adapters are separate from the production client registry.

Human-input replies are ordinary human messages with `hide_from_ui: true` and the structured
response in the fourth `sendMessage(..., options)` argument under
`options.additionalKwargs`. The normal composer remains available while a request is open; a
visible ordinary HumanMessage closes only the latest unanswered request. An open request still
blocks history-rewriting edit-and-rerun. Gateway treats visibility as server-owned and restores
the hidden flag only after the response exactly matches the latest open `ask_clarification`
request. The transcript also applies the same request/source/option/canonical-text checks when
hiding legacy responses persisted before that server promotion existed. Answered cards are
read-only disclosures: collapsed by default, but expandable to review the original request,
options, and submitted value.

Edit-and-rerun is limited to the latest complete user turn. Its prepare request, optimistic mask,
query invalidation, abort handling, and stream submission stay under the active
`accountId + projectId` private-work scope. Run duration is total wall-clock Run time, not model
thinking time, and renders once after that Run's final visible message group. Voice dictation
belongs to the active `InputBox`; project/thread switches, send, clear, disabled state, and
unmount abort the recognizer before stale transcripts can cross scope.

## Code style and tests

- Server Components are the default; use `"use client"` only for interactive components.
- Imports are grouped and alphabetized; use inline type imports.
- Use `@/*` aliases and `cn()` for conditional Tailwind classes.
- Runtime responses use strict Zod schemas and reject unknown authority/private fields,
  including the server-only Run `origin_trace_id`; no Query cache may retain it.
- The small unit core lives under `tests/unit/`. One deterministic project-route browser test,
  two static-boundary tests, and one real-backend Replay test live under `tests/e2e/`,
  `tests/e2e-static/`, and `tests/e2e-real-backend/` respectively.
- Features and fixes follow TDD: add the failing test, observe the expected failure, implement
  the minimal change, and rerun focused plus full affected gates.

Backend base URLs may be set for split-origin development. Leave them unset for the normal
root `make dev` or Docker flow so all browser calls use same-origin `/api/*` through Nginx.

Historical pass counts do not certify the current checkout. Run `pnpm check`, `pnpm test`, and the
affected Playwright/build gates for the current change. Browser and deployment coverage must be
reported from the current run rather than copied from an earlier milestone.
