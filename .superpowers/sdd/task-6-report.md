# M7 Task 6 Report: project-only frontend surfaces

## Scope

Task 6 removes the live legacy workspace routes and global frontend clients.
`/workspace` is now the sole workspace route: live builds render the
multi-project workbench, while static builds render a local no-network demo at
the same URL. Chats, Memory, Connections, assets, and Automation remain only
under an entered project.

The implementation started from exact baseline
`05b31530d2a6a5a693cd7bf9260b9d740fe4774a`. Task 7 was not started and
`.superpowers/sdd/progress.md` was not changed.

## TDD evidence

The route/client absence unit test and browser route test were written before
the implementation.

- Initial unit RED: 17 tests ran, 16 passed and the new absence test failed
  because `/workspace/chats` still existed in production sources.
- Initial browser RED: `/workspace/chats/new` returned 200 instead of the
  required Next not-found 404.
- After moving the pure helpers and removing the legacy surfaces, the focused
  unit gate and live/static browser cases were driven to green.

## Implementation

- Deleted the workspace Chats, Agents, Memory, Scheduled Tasks, Skills, Tools,
  and Projects route trees, the global Memory proxy, global Agent/MCP/Memory/
  Skill clients, legacy settings/sidebar components, and the unused system
  asset compatibility view.
- Moved pure cron validation, recipes, schedule types, and the schedule input
  under project Automation. These modules contain no URL, fetch, auth, or query
  key ownership.
- Moved connection types, polling, provider state, provider icon, and safe
  connect-window helpers under project private-work. Project Connections uses
  only the entered account/project client.
- Made `ScopedChatPage` require `ProjectPrivateWorkScope`; removed the legacy
  route scope, default client registry, mock/default private-work fallback, and
  global sidecar creation. Scope transitions cancel in-flight requests before
  disposing clients and clearing scoped cache state.
- Kept Memory types and all Memory operations in project private-work. Skill
  slash suggestions now derive from the project shared-asset catalog. Artifact
  file actions resolve only project-scoped URLs and no longer offer a global
  Skill install action.
- Split the live workspace layout from the static adapter so static
  `/workspace` imports no authenticated project runtime and makes no API
  request. Static project routes fail closed with `notFound()`.
- Updated frontend architecture guidance, README/README_zh, and the affected
  user docs to describe project-only assets, chats, Memory, Connections, and
  Automation.

## Final gates

Focused Task 6 unit gate:

```text
50 files, 304 passed, 0 failed, 0 skipped
```

Directly changed compatibility-client and sidecar tests:

```text
2 files, 19 passed, 0 failed, 0 skipped
```

Live Chromium gate (`m7-project-only-routes` plus `project-automations`):

```text
14 passed
```

This proves every removed workspace URL renders Next not-found without a
redirect and the selected project's Chats, Memory, Connections, and Automation
surfaces render inside the project shell.

Static Chromium gate:

```text
1 passed
```

This proves static `/workspace` remains at the same URL, renders the local demo,
sends zero `/api/` requests, exposes no project links, and returns 404 for a
direct project Automation URL.

Static checks and builds:

```text
pnpm check: eslint pass; tsc --noEmit pass
BUILD_MODE=production pnpm build: pass; 80 pages generated
BUILD_MODE=static pnpm build: pass; 80 pages generated
git diff --check: pass
```

The final production residue command returned zero matches for all removed
workspace URLs, global Memory/Agent/Skill/MCP API literals, and
`LEGACY_WORKSPACE_CHAT_SCOPE`. The production and static build route tables
contain `/workspace` but no legacy workspace child route.

## Independent review repair

The first independent review of `8428e136` rejected Task 6 on three Important
findings: the static workspace build still reached the authenticated module
graph, the default test suites retained legacy-only coverage instead of the
project successor contract, and shared chat/artifact clients still admitted
global or nullable private-work scope.

The repair was driven from reproduced failures. The strict-scope focused unit
gate initially ran 8 tests with 4 failures, and the static artifact gate found
`AuthProvider` in the `/workspace` client graph. The first migrated project-chat
browser run also exposed five fixture/assertion gaps before reaching 20/20.

### Repair implementation

- Added one canonical `BUILD_MODE=production|static` contract. Static builds
  resolve `#workspace-build-entry` through the `deerflow-static` package
  condition into an isolated local adapter and use `.next-static`; production
  builds resolve the authenticated project workbench. The artifact test walks
  the workspace client manifest, trace, and referenced chunks and rejects
  `AuthProvider`, project providers, private-work clients, or API literals.
- Made `ProjectPrivateWorkScope` the canonical non-null access type for live
  thread, upload, artifact, feedback, subtask-event, and workspace-change
  public APIs. Thread token usage, compact, branch, goal, feedback, follow-up,
  subtask history, files, artifacts, and changes now derive their URLs and
  cache keys from the account/project private-work scope. Production `src`
  contains no `/api/threads`, `/api/langgraph/threads`, nullable-scope fallback,
  or equivalent `getBackendBaseURL()` thread construction.
- Removed default-suite specs whose only subject was a deleted workspace URL or
  compatibility shell. Reusable behavior was moved to project-scoped tests
  instead of being discarded with the old route.

### Deleted-test capability reconciliation

| Deleted legacy area                                                                                | Final project-scoped coverage or disposition                                                                                                                                                   |
| -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chat load, history, streaming, stop, human input, upload, ownership errors                         | `project-private-chat.spec.ts` exercises the entered project and rejects global thread/artifact/upload requests.                                                                               |
| Thread list and infinite scroll                                                                    | Project Chats verifies owner-scoped search and explicit pagination through the selected project private-work client.                                                                           |
| Mermaid, stopped subtasks, persisted subtask steps                                                 | Project history renders Mermaid and stopped state; expanding the task asserts the project run-events URL.                                                                                      |
| Artifact preview and artifact-less stream state                                                    | Project write-file/presented-file coverage preserves preview, header trigger, scoped file transport, safe errors, and state across an artifact-less stream value.                              |
| Workspace changes                                                                                  | The project message badge opens data loaded only from the project private-work run endpoint.                                                                                                   |
| Quoted references and side-chat draft                                                              | Project chat preserves conversation references and opens/closes the project-scoped draft sidecar.                                                                                              |
| Plain-text and nested Markdown edge cases                                                          | Project history retains source text and renders deeply nested AI markers without a crash.                                                                                                      |
| Agent and Skill catalog compatibility pages                                                        | `project-assets.spec.ts` covers the project/system asset catalog and binding workflow; the global compatibility pages are intentionally removed.                                               |
| Scheduled-task workspace page                                                                      | `project-automations.spec.ts` remains the authority for project Automation lifecycle, history, permissions, retry, and scope transitions.                                                      |
| Legacy branch/regenerate, recent-thread sidebar, global settings/pages, legacy new-thread ordering | These controls and routes have no project contract (branch/regenerate are explicitly disabled in project chat); their tests were intentionally retired rather than pointed at a different URL. |

### Fresh repair gates

```text
focused strict-scope unit: 5 files, 16 passed
focused project private chat: 20 passed
full unit: 121 files, 882 passed, 0 skipped
full default Playwright: 67 passed
static artifact/browser Playwright: 2 passed
pnpm check: ESLint and TypeScript passed
pnpm format: passed
BUILD_MODE=production pnpm build: 80/80 pages
BUILD_MODE=static SKIP_ENV_VALIDATION=1 pnpm build: 80/80 pages
git diff --check: passed
production legacy route/global thread residue: zero matches
```

Task 7 was not started, and the M7 progress ledger was not changed by this
repair.

## Second independent-review repair

The second review of `bb18f3ad` found two remaining Important gaps: project
`.skill` artifacts tried to preview a nonexistent `SKILL.md` member URL, and
the deleted legacy Sidecar/Skill suggestion coverage had not yet been fully
reconciled on project private-work routes.

### RED evidence and fixes

- The new artifact unit test failed because the UUID file URL was rewritten to
  `.../files/<uuid>/SKILL.md`. The matching project E2E failed because the UI
  forced the archive into Markdown preview instead of rendering its download
  fallback.
- `.skill` is now an explicit opaque, download-only artifact contract. The
  loader never derives archive-member routes, the detail view does not fetch or
  render it as Markdown, and both the header and fallback retain the project
  UUID file URL. Error bodies remain hidden by the existing safe loader path.
- Project Sidecar coverage now owns draft close, create/send, visible reference
  metadata, scoped stream, persisted history restore, stale-thread self-heal,
  delete, and in-flight delete locking. Every case observes project
  `/api/projects/{project_id}/private-work/...` requests and rejects global or
  legacy thread routes.
- The create/send E2E exposed a real transition bug: the queued first Sidecar
  message was consumed before `useThreadStream` had bound the newly created
  thread. `useThreadStream` now exposes its bound thread ID, and Sidecar waits
  for that ID before dispatching the queued message.
- Project Skill catalog integration now proves a leading slash shows the
  project Skill, keyboard navigation selects it, and submission enters the
  project-scoped run stream without a global Skill or thread request.

### Fresh second-repair gates

```text
focused artifact unit: 1 file, 4 passed
focused project artifact/Skill/Sidecar E2E: 8 passed
full unit: 121 files, 883 passed, 0 skipped
full default Playwright: 74 passed
static artifact/browser Playwright: 2 passed
pnpm check: ESLint and TypeScript passed
pnpm format: passed
BUILD_MODE=production pnpm build: 80/80 pages
BUILD_MODE=static SKIP_ENV_VALIDATION=1 pnpm build: 80/80 pages
git diff --check: passed
production legacy route/global client residue: zero matches
```

Task 7 was not started, and `.superpowers/sdd/progress.md` remains unchanged by
this second repair.

## Third independent-review repair

The third review of `bd01006f` found one Important race in the project Sidecar:
create/restore completion and the queued first submit were not owned by one
immutable parent identity. Because a closing panel remains mounted briefly, an
old request could adopt its thread or clear/send work after a parent switch,
draft close, or delete transition.

### RED evidence and fix

- Four deferred Playwright cases were added for create-then-parent-switch,
  create-then-close, restore-then-close, and delete-then-parent-switch. They
  also specify that the fresh parent/generation can restore or create and send
  normally, with no global route, duplicate send, or trigger/cache pollution.
- The pure identity state-machine unit was written before its implementation.
  Its RED was the TypeScript error `TS2307: Cannot find module
  '@/core/sidecar/identity'`.
- Sidecar now owns an immutable `{parentThreadId, generation}` identity.
  Parent switch, close, and delete/reset advance the generation; deferred
  restore/create adoption and delete clearing succeed only for the exact
  current identity. The provider is additionally keyed by parent thread.
- A queued first submit records both identity and sidecar thread ID. It sends
  only when that identity is still current, the visible sidecar ID matches,
  and the stream is bound to the same thread; stale work is fail-closed and
  dropped. A fresh identity retains the normal restore/create/send path.

### Fresh third-repair evidence

The pure state-machine harness exercised all four identity decisions, and the
fresh third-repair matrix passed:

```text
sidecar identity state machine: 4 assertions passed
full Rstest: 122 files, 887 passed, 0 skipped
deferred Sidecar race Playwright: 4 passed
full default Playwright: 78 passed
static artifact/browser Playwright: 2 passed
pnpm exec tsc --noEmit: passed
pnpm check: ESLint and TypeScript passed
pnpm format: passed
BUILD_MODE=production pnpm build: 80/80 pages
BUILD_MODE=static SKIP_ENV_VALIDATION=1 pnpm build: 80/80 pages
git diff --check: passed
production legacy route/global client residue: zero matches
```

The first default Playwright run passed 77 of 78 tests and exposed one existing
project Automation parallel-run fluctuation outside this Sidecar diff. That
exact Automation case passed 1/1 in isolation, and the immediate full default
rerun passed 78/78. The deferred Sidecar race gate passed 4/4 before the full
runs, so all four new identity transitions and the existing Sidecar coverage
are exercised by fresh browser evidence.

Task 7 was not started, and `.superpowers/sdd/progress.md` remains unchanged by
this third repair.
