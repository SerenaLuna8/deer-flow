# AGENTS.md

This is the source of truth for DeerFlow frontend work. The repository-level
[AGENTS.md](../AGENTS.md) owns monorepo orientation; this guide owns the final M7
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

`pnpm check` runs lint and type checking. The M7 production Playwright gate writes to
`test-results/m7-production`; the static gate builds into `.next-static` and writes to
`test-results/m7-static`, so normal and static artifacts cannot be reused accidentally.
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

```text
frontend/src/
├── app/
│   ├── workspace/                    # live account landing or static local demo
│   ├── projects/[project_slug]/      # only live project shell
│   └── admin/                        # platform administration
├── components/
│   ├── projects/                     # project shell and project-private pages
│   ├── workspace/                    # reusable chat/message/artifact presentation
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
└── env.js                            # environment validation
```

Generated primitives under `components/ui/` and `components/ai-elements/` should not be
edited manually.

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

Project clients use `/api/projects/{project_id}/private-work` for Thread, Run, file,
artifact, input-polish, and durable SSE operations. Project Memory uses
`/api/projects/{project_id}/memory`; Connections use
`/api/projects/{project_id}/connections`; Automation uses
`/api/projects/{project_id}/automations`.

Chats, Memory, Connections, and Automation navigation require a non-static build, server
readiness `ready`, and `private_work.read_own`. Create/run/upload/connect controls additionally
require their exact server capability. Viewer can read/list/export and delete their own
ready upload/workspace/output files, but never sees mutation controls that require
create/manage authority.

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
override only DeerFlow's normalized `messages`, scoped `values`, and local `stop` projection.

Conversation history is a lead-Agent projection. Historical rows tagged with a subagent or
middleware caller are hidden, and their tool results are filtered by the issuing AI
`(run_id, tool_call_id)` pair; internal HumanMessages are hidden unless they are the explicit
Run-admission row. This compatibility filter must remain until every retained Run was written by
the lead-only journal contract. Rendering also associates tool results with the exact issuing
AI group by `(run_id, tool_call_id)`, including late/replayed and result-before-call pagination
order. Legacy rows without a Run ID are isolated to one Human turn. Never attach an unknown
orphan tool result to the most recent or final assistant group, and always synthesize a stable
non-empty group key when legacy messages have no ID.

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
result: the final message's reasoning disclosure first, terminal answer next, then compact
published-file rows. The completed disclosure reads the server-observed
`additional_kwargs.reasoning_duration_ms`, floors it to whole seconds, and uses “under 1 second”
for an observed sub-second interval. Missing or invalid legacy values remain the neutral
“Reasoning” label; the UI never substitutes Run duration. Exact Run duration remains a separate
“Completed in” row after the result because it includes model latency, tools, Subagents, queues,
and wait time. Completed execution detail and `present_files` transition prose are not repeated
beside the result. In-flight, failed-before-answer, and clarification groups retain their original
presentation so active work still has visible progress.

Subtask state folds effective model and cumulative Token metadata from `task_started`,
`task_running`, terminal custom events, and the authoritative terminal ToolMessage. Older delayed
usage snapshots must never reduce a displayed cumulative total. The SubtaskCard resolves a known
model to its configured display name, falls back to the model identifier, and hides per-subtask
Token totals when global `token_usage.enabled` is false. Keep project-scoped historical step
fetching and the `inferred < custom_event < tool_result` status authority ordering intact.

Input polish is project-scoped and never runs without `private_work.create` plus
`shared_assets.execute`. The server revalidates the current Thread Agent snapshot and
Credential-grant closure; the browser never constructs authority fields.

## Shared assets and credentials

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

Project Skill creation offers AI conversation, blank creation, and archive import from one menu.
Conversation creation uses `/projects/{project_slug}/skills/new` and the resumable
`/skills/new/{session_id}` workspace. The name step creates only a private Builder session.
The workspace preserves the selected file while generated files change, selects `SKILL.md` only
when the first candidate package appears, locks conversation while local file edits are unsaved,
and requires an explicit checksum-bound validation before one atomic commit. Warnings require
acknowledgement; commit publishes version 1 while leaving the Skill suspended and unbound.
Skill Builder queries and mutations use their own account+project root and are aborted and removed
with the active private-work scope.

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
“删除”, and only that permanent action uses destructive styling. MCP archive remains independent.

Global `/admin/assets` Agent, Skill, and MCP pages render the packaged PostgreSQL catalog as
read-only governance metadata. They must not expose definition create/edit, new-version,
publish, approval, archive, or suspend controls or client mutations. A published packaged
System MCP may expose only the dedicated Credential-grant configuration flow; that flow sends
Credential version IDs plus expected active grant revisions and does not republish or alter
the MCP definition. System Credential lifecycle controls and project-scoped asset override
authoring are separate surfaces and remain mutable.

Credential create/replace is an imperative authenticated request, not a TanStack mutation.
Secret-bearing form values must never enter QueryCache or MutationCache, must be cleared after
submit, and must not remain in the DOM. Responses and errors may show safe status metadata but
never plaintext, ciphertext, nonce, key ID, storage locator, secret hash, or raw provider
payload. MCP versions with required Credential slots use submit/approve rather than direct
publish.

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

Project MCP authoring exposes only `http` and `sse`. The URL is required and described as a
Worker-reachable, operator-approved exact HTTPS endpoint; project `stdio` and
`streamable_http` are never offered for new versions. Literal env/header and OAuth editors are
not exposed; authentication is configured through header Credential slots. Historical
unsupported versions remain readable with an explicit blocked reason; Project MCP history
exposes only the remote HTTPS origin, never a persisted path or query. Publish, binding, and
Agent dependency selection stay disabled. The backend policy remains authoritative, and
mutation failures stay visible in the active dialog without clearing the user's safe inputs.
This restriction is scope-specific: packaged System MCP retains the runtime-supported `stdio`,
`sse`, and `http` transports plus their existing env/header/OAuth credential capabilities;
only transports or definitions that the private runtime cannot execute are blocked.
Agent cards expose the project default as read-only state to project members; only a member with
`shared_assets.manage_bindings` may set an active, published, executable project Agent as the
default or restore Main. Ordinary new-conversation entry points and Builder continuation share
one project-new-chat path: they omit explicit Agent fields so Gateway resolves the current
default atomically. Agent-card chat remains an explicit override, and a configured but invalid
default fails closed rather than silently switching to Main. Agent cards, the Agent selector,
project-new-chat, Connections, and Automation creation/resume/manual-run all use the same
fail-closed MCP dependency assessment. When no project default is configured, Main re-reads the
exact published dependency versions on every start and enables or moves the required System
bindings before creating the Thread; an existing Main binding never bypasses that recheck.

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

## Component ownership

- `ProjectContextProvider` owns project resolution and enter.
- `ProjectPrivateWorkProvider` owns the scoped client, reconnect state, and teardown.
- `ScopedChatPage` owns project composer busy state, branch/edit/regenerate actions, and navigation.
- `MessageList` owns human-input answered/latest/pending gating, latest-turn edit eligibility,
  and the single group-tail Run-duration display.
- `core/threads/hooks.ts` owns pre-submit upload state, scoped prepare/submit replay, optimistic
  replacement, and replay failure rollback.
- `core/threads/agent-mode.ts` owns the single composer mode contract. Flash, Thinking, Pro, and
  Ultra map to minimal, low, medium, and high reasoning effort respectively. The composer and
  Sidecar persist only the mode; submit and replay derive runtime fields after all stored context
  so a legacy standalone reasoning-effort value cannot override the selected mode.
- Project Memory and Connection pages own their scoped queries and mutations; shared
  presentation components remain pure.
- Static demo fixtures and adapters are separate from the production client registry.

Human-input replies are ordinary human messages with `hide_from_ui: true` and the structured
response in the fourth `sendMessage(..., options)` argument under
`options.additionalKwargs`. The normal composer remains available while a request is open; a
visible ordinary HumanMessage closes only the latest unanswered request. An open request still
blocks history-rewriting edit-and-rerun.

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
- Unit tests live under `tests/unit/`. Deterministic browser tests live under
  `tests/e2e/` and `tests/e2e-static/`; Replay full-stack tests live under
  `tests/e2e-real-backend/`, and manual fixture recording under `tests/e2e-record/`.
- Features and fixes follow TDD: add the failing test, observe the expected failure, implement
  the minimal change, and rerun focused plus full affected gates.

Backend base URLs may be set for split-origin development. Leave them unset for the normal
root `make dev` or Docker flow so all browser calls use same-origin `/api/*` through Nginx.

Historical pass counts do not certify the current checkout. Run `pnpm check`, `pnpm test`, and the
affected Playwright/build gates for the current change. Browser and deployment coverage must be
reported from the current run rather than copied from an earlier milestone.
