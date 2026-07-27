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
pnpm test:e2e:m8:deterministic
pnpm test:e2e:m8
pnpm test:e2e:static
pnpm build:production
pnpm build:static
```

`pnpm check` runs lint and type checking. The M7 production Playwright gate writes to
`test-results/m7-production`; the static gate builds into `.next-static` and writes to
`test-results/m7-static`, so normal and static artifacts cannot be reused accidentally.
`test:e2e:m8:deterministic` 是不调用 live model 的完整 CI Chromium 测试清单，包含隔离矩阵
drift contract 和所有现有 Playwright 回归；`test:e2e:m8` 只由完整宿主机验收在
invocation-owned production stack 上运行，不能单独生成 M8 candidate/final。

## Final route model

- `/workspace` is the authenticated account-wide multi-project landing page. It shows
  project cards, invitations, and recoverable projects without a project sidebar.
- `/projects/[project_slug]` is the only live project shell. Nested pages include project
  overview, members/settings, Chats, Memory, Connections, Automation, shared assets,
  Usage, and Audit according to server-issued capabilities.
- `/admin/assets/*` and `/admin/operations/*` are the platform administration shells.
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

Conversation history is a lead-Agent projection. Historical rows tagged with a subagent or
middleware caller are hidden, and their tool results are filtered by the issuing AI
`(run_id, tool_call_id)` pair; internal HumanMessages are hidden unless they are the explicit
Run-admission row. This compatibility filter must remain until every retained Run was written by
the lead-only journal contract. Rendering also associates tool results with the exact issuing
AI group by `(run_id, tool_call_id)`, including late/replayed and result-before-call pagination
order. Legacy rows without a Run ID are isolated to one Human turn. Never attach an unknown
orphan tool result to the most recent or final assistant group, and always synthesize a stable
non-empty group key when legacy messages have no ID.

During a live project Run, successful lead-Agent `write_file` and `str_replace` tool calls select
the written file in the artifact preview, open the right-hand file panel, and collapse the desktop
project navigation. Trusted stream messages tagged `subagent:<name>` are internal progress and
must be excluded from the lead conversation projection: their steps render only in the matching
SubtaskCard, and their temporary file writes never select or open artifact preview. `present_files`
remains the explicit publication boundary for rendering downloadable file cards inside the
conversation. Terminal Run handling must invalidate the project Thread file list so finalized
UUID-backed file routes are available without a reload.

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

Project Skill list creation is one scoped backend mutation that atomically creates the disabled
asset plus version 1 Draft containing a backend-valid root `SKILL.md`; the frontend must never
sequence a separate asset request and version request. The template frontmatter name is the
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
Agent cards, the Agent selector, Main chat and Builder continuation, Connections, and
Automation creation/resume/manual-run all use the same fail-closed MCP dependency assessment.
Main re-reads the exact published dependency versions on every start and enables or moves the
required System bindings before creating the Thread; an existing Main binding never bypasses
that recheck.

## Automation

Automation definitions and occurrences are scoped by exact account, project, and owner.
Admin, Editor, and Runner controls appear only with the matching server capability; Viewer is
read-only. Every key begins with the authenticated account and entered project roots.

Create sends a complete payload. Edit sends a sparse PATCH based on normalized semantic
changes only. Equivalent once timestamps such as `Z` and `+00:00` do not count as a schedule
change. Pure cron/once validation and recipes live under
`core/project-automations/schedule/` and contain no URL, fetch, auth, or query-key behavior.
Manual trigger uses a UUID idempotency key and the same durable admission path as Scheduler.

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

## Component ownership

- `ProjectContextProvider` owns project resolution and enter.
- `ProjectPrivateWorkProvider` owns the scoped client, reconnect state, and teardown.
- `ScopedChatPage` owns project composer busy state, branch actions, and navigation.
- `MessageList` owns human-input answered/latest/pending gating.
- `core/threads/hooks.ts` owns pre-submit upload state and submission.
- Project Memory and Connection pages own their scoped queries and mutations; shared
  presentation components remain pure.
- Static demo fixtures and adapters are separate from the production client registry.

Human-input replies are ordinary human messages with `hide_from_ui: true` and the structured
response in the fourth `sendMessage(..., options)` argument under
`options.additionalKwargs`. While an open request exists, the normal composer remains disabled.

## Code style and tests

- Server Components are the default; use `"use client"` only for interactive components.
- Imports are grouped and alphabetized; use inline type imports.
- Use `@/*` aliases and `cn()` for conditional Tailwind classes.
- Runtime responses use strict Zod schemas and reject unknown authority/private fields.
- Unit tests live under `tests/unit/`. Deterministic browser tests live under
  `tests/e2e/` and `tests/e2e-static/`; Replay full-stack tests live under
  `tests/e2e-real-backend/`, manual fixture recording under `tests/e2e-record/`,
  and host-owned M8 acceptance under `tests/e2e-release/`.
- Features and fixes follow TDD: add the failing test, observe the expected failure, implement
  the minimal change, and rerun focused plus full affected gates.

Backend base URLs may be set for split-origin development. Leave them unset for the normal
root `make dev` or Docker flow so all browser calls use same-origin `/api/*` through Nginx.

Historical pass counts do not certify the current checkout. Run `pnpm check`, `pnpm test`, and the
affected Playwright/build gates for the current change. Browser and deployment coverage must be
reported from the current run rather than copied from an earlier milestone.
