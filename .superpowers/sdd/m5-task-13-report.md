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

The original Task 13 brief did not require a new Playwright suite. The initial delivery therefore left the existing Task 12 Automation interaction E2E unchanged; the independent-review repair below adds the missing compile-time suite gate and runs that file directly.

## Self-review conclusion

- **Authority:** no role inference was added. Viewer visibility comes from `private_work.read_own`; mutations remain owned by Task 12 capability checks and backend admission.
- **Readiness:** both navigation and Project Chat require the full ready tuple. `migration_required`, `unavailable`, missing readiness, inconsistent ready booleans, feature-off, static, and missing-capability states all fail closed.
- **Direct URL:** the page is server-gated for compile/static state before any client hook; current-project capability/readiness and the scoped API enforce the remaining access path without duplicate project lookup.
- **Scope safety:** project slugs and Thread IDs are encoded, Project Chat cannot generate a legacy workspace URL, and workspace behavior remains unchanged.
- **Static behavior:** no Automation link or enabled readiness query is produced; the compile-time flag remains disabled for Task 17.
- **Scope discipline:** no backend, progress, Task 14, or Task 17 enablement changes were made.
- **Initial self-review findings:** 0 Critical, 0 Important, 0 Minor. The subsequent independent review found the E2E workflow issue repaired below.

The initial delivery kept the M5 candidate disabled and the milestone incomplete.

---

## Review repair — canonical Playwright suite gate

### Review intake and root cause

- Review result: 0 Critical, 1 Important.
- Important finding: the Task 12 `project-automations.spec.ts` file still visited the project Automation route unconditionally, while Task 13 correctly made that server route return 404 under `PROJECT_AUTOMATION=false`.
- Reproduction before the repair ran all three Playwright tests; each timed out after 30 seconds waiting for Automation UI that the server-gated route could not render. The result was 3 failed, confirming the full workflow would fail while the release flag remained disabled.

The root cause was test discovery not consuming the canonical compile-time release state. The server route gate was not weakened.

### Repair

- The Playwright file now imports `PROJECT_AUTOMATION` directly from `@/core/projects/features`; the same runtime alias pattern already exists in the project-private-work E2E suite.
- A file-level `test.skip(!PROJECT_AUTOMATION, "PROJECT_AUTOMATION is disabled; Task 17 enablement will restore this suite.")` annotation skips all three tests while the candidate is disabled.
- The condition is the direct negation of the canonical flag. When Task 17 changes that flag to true, the condition becomes false and the existing three tests execute without any spec edit.
- No environment variable, copied test constant, test-only route enable, production route bypass, Task 14 change, or Task 17 enablement was added. `PROJECT_AUTOMATION` remains `false as const`.

### Strict TDD evidence

The review-repair RED changed only the unit/source contract:

```text
1 focused file, 6 tests: 1 expected failure and 5 passes.
```

The failure proved the Playwright file did not import the canonical flag. After the minimal E2E annotation, the source assertion was adjusted only to accept Prettier's trailing comma; final GREEN was:

```text
1 focused file, 6 tests passed.
```

The source contract requires the runtime canonical import, the exact `!PROJECT_AUTOMATION` skip semantics, and the Task 17 restoration reason. It rejects environment-variable alternatives.

### Final repair verification

| Gate                                          |                                 Result |
| --------------------------------------------- | -------------------------------------: |
| Project Automation Playwright with flag false |                    3 skipped, 0 failed |
| Task 13 focused unit                          |               4 files, 20 tests passed |
| Fresh full frontend unit suite                | 126 files, 912 tests passed, 0 skipped |
| `pnpm check`                                  |           ESLint and TypeScript passed |
| Repair-scoped Prettier check                  |                                 Passed |
| `git diff --check`                            |                                 Passed |

### Review closure

- The workflow no longer tries to exercise a server-disabled surface.
- Task 17 can restore the real E2E coverage by changing only the canonical production flag; the spec has no separate switch to drift.
- The route remains server-gated, and the production flag remains disabled.
- Final review-repair findings: 0 Critical, 0 Important, 0 Minor.

No Task 13 review blocker remains. The M5 candidate is still disabled and the milestone remains incomplete.
