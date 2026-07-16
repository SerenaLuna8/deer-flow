# M5 Task 13 Implementation Report — Gated Automation Entry

## Status and scope

- Date: 2026-07-16
- Baseline: `76d001b8e3a5c453b3e3f947794647ab6352a01f`
- Branch: `codex/m5-project-automation`
- Commit subject: `feat: gate project automation entry`
- Scope: project navigation, project/workspace Chat links, Automation i18n, compile/static/readiness/capability gates, and disabled direct-route server gate
- Explicit exclusions: Task 14 backend/Scheduler wiring, Task 17 feature enablement, backend files, milestone progress, and release claims

Task 13 connects the already-built Task 11 client and Task 12 workbench to discoverability surfaces without enabling the M5 candidate. `PROJECT_AUTOMATION` remains `false as const`, so the direct route returns server-side not-found and no navigation or Chat entry is exposed until the later release-gate task changes the compile-time flag.

## Delivered gates and navigation

- Added `PROJECT_AUTOMATION = false as const` and the pure `projectAutomationEntryEnabled()` decision function. The entry requires the compile-time feature, a non-static build, `private_work.read_own`, and Automation readiness `ready`.
- Extended `projectNavigationItems()` with explicit Automation readiness, feature, and static inputs. A read-only Viewer can see the future entry because reads depend only on `private_work.read_own`; Task 12 continues to hide mutations unless the server-returned manage/create/execute capabilities are present.
- `ProjectNavigationLinks` enables the Automation readiness query only when the compile-time feature is on, the build is non-static, and the current project can read private work. It exposes the item only when readiness is `ready` and both `project_private_work_ready` and `automation_cutover_ready` are true.
- Kept the M4 Chats, Memory, and Connections decision independent from M5 Automation. Existing private-work readiness and feature gates are unchanged.
- Added a server-component gate to `/projects/[project_slug]/automations`. Compile-time-disabled and static builds call `notFound()` before the client route wrapper can consume project context. Capability and readiness checks remain in the entered project page and scoped backend API, avoiding duplicate slug resolution or client-created authority.

## Chat URL and static boundaries

- Parameterized `ThreadScheduledTasksLink` with caller-owned `href` and `label`; the component no longer embeds a workspace URL or owns locale selection.
- Added scope-owned link builders to the shared Chat route contract. Workspace Chat continues to generate `/workspace/scheduled-tasks?thread_id={uuid}`.
- Project Chat generates only `/projects/{encoded_slug}/automations?thread_id={encoded_uuid}` and uses the localized Automation label. It never falls back to the legacy workspace scheduled-task route.
- Project Chat applies the same compile/static/capability/readiness gates as navigation. The Automation readiness hook is disabled when any pre-query gate is closed, including static mode.
- Static coverage proves there is no Automation navigation link and the readiness hook is called with `enabled=false`; the pure Chat gate and shared-scope tests prove static mode hides the project Chat entry.

## i18n

- Added the strongly typed `project.automations` label and the planned `automation.create`, `runNow`, `schedulerDisabled`, `migrationRequired`, `retry`, and `history` fields.
- Populated every supported locale: English (`en-US`) and Simplified Chinese (`zh-CN`).
- Navigation and Chat consume `project.automations`; workspace Chat continues to consume the existing scheduled-task label.

## Strict TDD evidence

The untouched Task 12 baseline was verified first:

```text
Task 11/12 + existing entry baseline: 125 files, 903 tests passed, 0 skipped.
```

The Task 13 RED changed tests only:

```text
126 files, 911 tests collected; 11 expected failures and 900 existing passes.
```

The failures were specific to the missing Task 13 contracts: the compile-time constant and gate helper, Automation navigation, project/workspace Chat href and label ownership, Viewer/static/readiness gates, shared composer wiring, static readiness disablement, direct-route server not-found, and both locale payloads.

After the minimal implementation:

```text
Initial GREEN: 126 files, 911 tests passed, 0 skipped.
Focused Task 13: 4 files, 19 tests passed, 0 skipped.
```

No production code preceded the failing Task 13 tests. A later check-only import-order/type issue was corrected without changing runtime behavior.

## Final verification

| Gate                                                   |                                 Result |
| ------------------------------------------------------ | -------------------------------------: |
| Task 13 nav/Chat/i18n/static/direct-route focused unit |               4 files, 19 tests passed |
| Task 11/12 client/workbench/cache/shell regression     |              12 files, 78 tests passed |
| Fresh full frontend unit suite                         | 126 files, 911 tests passed, 0 skipped |
| `pnpm check`                                           |           ESLint and TypeScript passed |
| Task-scoped Prettier check                             |               All matched files passed |
| Staged `git diff --check`                              |                                 Passed |

The Task 13 brief does not require Playwright. No new E2E suite was run; the requested navigation, Chat URL, direct-route, i18n, and static no-query boundaries are covered by focused unit and source-boundary tests, while the existing Task 12 Automation interaction E2E remains unchanged.

## Self-review conclusion

- **Authority:** no role inference was added. Viewer visibility comes from `private_work.read_own`; mutations remain owned by Task 12 capability checks and backend admission.
- **Readiness:** both navigation and Project Chat require the full ready tuple. `migration_required`, `unavailable`, missing readiness, inconsistent ready booleans, feature-off, static, and missing-capability states all fail closed.
- **Direct URL:** the page is server-gated for compile/static state before any client hook; current-project capability/readiness and the scoped API enforce the remaining access path without duplicate project lookup.
- **Scope safety:** project slugs and Thread IDs are encoded, Project Chat cannot generate a legacy workspace URL, and workspace behavior remains unchanged.
- **Static behavior:** no Automation link or enabled readiness query is produced; the compile-time flag remains disabled for Task 17.
- **Scope discipline:** no backend, progress, Task 14, or Task 17 enablement changes were made.
- **Review findings:** 0 Critical, 0 Important, 0 Minor.

No Task 13 blocker remains. The M5 candidate is still disabled and the milestone remains incomplete.
