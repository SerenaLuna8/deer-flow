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
