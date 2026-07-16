# M5 Task 12 Implementation Report — Project Automation Workbench

## Status and scope

- Date: 2026-07-16
- Baseline: `288f4b4916413d203dcef26e2e987f0f0fe3195c`
- Branch: `codex/m5-project-automation`
- Commit subject: `feat: add project automation workbench`
- Scope: Task 12 project-scoped Automation form, workbench, route, page states, and component tests
- Explicit exclusions: Task 13 navigation, Chat entry wiring, new locale contracts, static feature flag, backend changes, milestone progress updates, and release claims

Task 12 adds the direct `/projects/[project_slug]/automations` workbench on top of the Task 11
account/project-scoped client. The route consumes the already-entered current project and never
resolves a slug, enters a project, calls the legacy scheduled-task API, or creates client-side
authority.

## Delivered workbench

- Added an Automation form with reusable legacy schedule presentation, project Agent catalog
  selection, recipe starters, trimmed title/prompt submission, UUID Thread validation, future-only
  once validation, five-field Cron validation, non-empty bounded timezone validation, and immutable
  schedule type/context/Agent fields during edit.
- Added list, title/status/type filtering, responsive list/detail layout, create/edit dialogs,
  pause/resume/manual trigger/delete actions, run history, private project Thread links, loading,
  empty, filter-empty, error, migration-required, and retry states.
- Added capability-derived presentation only: `private_work.read_own` enables reads;
  `automation.manage_own` enables edit/pause/delete; create/resume/manual trigger additionally
  require `private_work.create` and `shared_assets.execute`. Viewer renders definitions and history
  without mutation controls. No project role is inspected.
- Added readiness fail-closed behavior. Lists and Agent catalog requests remain disabled until M5
  status is `ready` and both `project_private_work_ready` and `automation_cutover_ready` are true.
  Scheduler-disabled mode displays a banner but intentionally keeps manual trigger available.
- Added safe action feedback for conflicts, concurrency limits, and unavailable/network failures.
  Raw server messages are never rendered; conflicts and unavailable states expose an explicit
  refresh action. The same feedback remains visible inside open create/edit/delete dialogs.
- Added strict `thread_id` query handling for the later Task 13 Chat link: only a UUID can select the
  scoped thread list and prefill a reuse-Thread create form. Prompt is never placed in URL or
  browser storage.

## Security, cache, and interaction boundaries

- Agent authority comes only from the server project catalog and existing executable-Agent filter.
  The form submits the logical Agent ID/scope; the server remains responsible for current binding,
  capability, membership, and version admission.
- All reads and mutations use Task 11 project Automation hooks. Manual trigger passes only the task
  ID to the hook; the hook-owned idempotency registry remains the only holder of the UUID key, so no
  key enters query data, mutation variables, URL, or component state.
- Prompt is local React state until submit. A successful create closes/unmounts the form and rotates
  its generation; failed safe retries keep the form in memory. `key={project.id}` resets dialog,
  prompt, filter, and selection state immediately when project scope changes.
- Selection records the originating project ID, and the Task 11 provider continues to cancel and
  remove account/project Automation queries and mutations during scope transition. The focused gate
  re-ran provider, scoped hook, readiness, and project-shell tests.
- Radix dialogs retain focus trapping, Escape close, labelled title/description, and focus return.
  All filters and inputs have accessible labels, actions are real buttons, run history is structured,
  and reusable schedule toggle buttons now explicitly use `type="button"` so the parent form cannot
  submit accidentally.
- Rejected button action Promises are settled after the page records safe feedback, avoiding
  unhandled rejections without hiding the visible error state.

## Strict TDD evidence

The initial RED added only the three requested component/page test files:

```text
pnpm test -- components/projects/automations
125 files collected; 3 new files failed because the target modules did not exist;
863 existing tests passed; 0 assertion failures.
```

The first workbench implementation reached GREEN:

```text
125 files, 882 tests passed, 0 skipped.
```

Self-review then added fail-closed readiness, bounded form inputs, and stale-project reset tests.
Before the repair, exactly five new assertions failed while 882 tests remained green. After the
repair:

```text
125 files, 887 tests passed, 0 skipped.
```

The interaction review added tests for schedule buttons nested in a form and rejected button
actions. RED was 2 files / 19 tests with 2 expected failures and 17 passes. GREEN was:

```text
2 files, 19 tests passed, 0 skipped.
```

## Final verification

| Gate                                                      |                                 Result |
| --------------------------------------------------------- | -------------------------------------: |
| Task 12 + project-shell + scoped hooks/readiness/provider |               7 files, 47 tests passed |
| Task 12 interaction repair                                |               2 files, 19 tests passed |
| Fresh full frontend unit suite                            | 125 files, 893 tests passed, 0 skipped |
| `pnpm check`                                              |           ESLint and TypeScript passed |
| Task 12 scoped Prettier write/check                       |               all matched files passed |
| `git diff --check`                                        |                                 passed |

The Task 12 brief does not require Playwright, and Task 13 owns the gated navigation/Chat/static
entry E2E boundary, so no Task 13 Playwright or feature-flag work was pulled into this commit.

## Self-review conclusion

- **Plan alignment:** every Task 12 workbench operation and page state is present; Task 13 and
  backend scope remain untouched.
- **Responsive and accessibility:** compact/mobile stacking, bounded dialogs, desktop split view,
  labelled form/filter controls, keyboard-capable native controls, and Radix dialog semantics are
  present. The nested schedule-button submit bug found during review is covered by a regression test.
- **Authority and privacy:** capabilities and current project scope are server-derived; no role
  inference, legacy endpoint, global API client, prompt persistence, client authority, or cached
  idempotency key was introduced.
- **Stale scope and concurrency:** project-keyed local reset, scope-tagged selection, Task 11
  cancellation/invalidation, stable trigger-key lifecycle, and settled event Promises prevent stale
  scope UI or late-action leakage.
- **Review findings:** 0 Critical, 0 Important, 0 Minor.
- `.superpowers/sdd/progress.md`, backend files, and milestone status documents are unchanged.

No blocking Task 12 concern remains. Task 13 still owns discoverability, translations, Chat entry,
and compile-time/static gates; the broader M5 milestone remains incomplete.

---

## Review repair — schedule synchronization, terminal actions, and feedback lifecycle

### Review intake and scope

- Date: 2026-07-16
- Review result received: 0 Critical, 2 Important, 1 Minor
- Repair commit subject: `fix: harden project automation interactions`
- Scope remained frontend Task 12 only. Task 13 navigation/feature-gate work, backend code,
  milestone progress, and release status were not changed.

The review findings were reproduced before implementation. Recipe selection updated the parent
draft without updating the schedule component's mount-owned state and copied an empty recipe
timezone. Manual trigger presentation did not inspect terminal status. A single page feedback value
was rendered globally and in every mutation dialog without action or project identity.

### Repairs

- Recipe application now creates a fresh schedule value, preserves the current timezone only when
  it is valid, falls back to the browser timezone (and finally UTC), and increments a stable schedule
  revision so the reusable control remounts from the same value held by the parent draft. Subsequent
  custom schedule edits continue to update the parent and are not overwritten by the recipe.
- Manual trigger is now admitted only for `enabled` and `paused`. `completed`, `failed`, and
  `cancelled` definitions render no manual action and cannot reach the UI callback.
- Safe feedback now carries the originating action and project ID. Create/update/delete feedback is
  rendered only in its matching dialog; pause/resume/trigger feedback is the only global feedback.
  Closing a dialog, opening another action, refreshing, or changing `project.id` clears the visible
  feedback. Late failures from a prior project remain tagged with that old project and cannot render
  in the new scope.

### Strict TDD and interaction evidence

The first repair RED added recipe, five-status trigger, action-scope, and project-scope assertions.
Before implementation, the suite reported 13 expected failures and 890 existing passes. A further
invalid-timezone case was also demonstrated RED (1 failed, 14 passed) before adding timezone
validation. The final focused unit result is:

```text
Task 12 focused: 3 files, 40 tests passed, 0 skipped.
```

A new Playwright fixture exercises the actual project Automation page and scoped HTTP client:

- click a daily recipe and submit immediately; visible preset/timezone equal the POST body and no
  timezone validation error appears;
- click the weekly recipe, switch to custom Cron, edit later form fields, and confirm the visible
  Cron/timezone still equal the POST body;
- select all five statuses; enabled and paused send exactly two trigger requests, while all terminal
  statuses expose no trigger action;
- fail edit and delete, close/switch dialogs, then fail a global trigger and change project; only the
  current action and current project show feedback.

```text
Playwright project Automation interactions: 3 tests passed.
```

### Final repair verification

| Gate                                      |                                 Result |
| ----------------------------------------- | -------------------------------------: |
| Task 12 focused unit                      |               3 files, 40 tests passed |
| Task 12 real interaction                  |                    3 Playwright passed |
| Task 11 scoped client/provider regression |               8 files, 31 tests passed |
| Fresh full frontend unit suite            | 125 files, 903 tests passed, 0 skipped |
| `pnpm check`                              |           ESLint and TypeScript passed |
| Repair-scoped Prettier check              |                                 passed |
| `git diff --check`                        |                                 passed |

### Repair self-review

- Accessibility remains intact: all new interactions use the existing labelled form controls,
  semantic buttons, alert roles, and Radix dialog focus/Escape/focus-return behavior. No hidden
  terminal action remains keyboard- or pointer-reachable.
- Stale-state review found no cross-action or cross-project rendering path: action filtering occurs
  before presentation, project filtering occurs before the workbench receives feedback, and scope
  change also clears retained state.
- Recipe data is copied rather than mutated; prompt, timezone, feedback, and idempotency data are not
  added to URLs, storage, or query data.
- Review closure: 0 Critical, 0 Important, 0 Minor.

No Task 12 review blocker remains. Task 13 and the wider M5 milestone remain incomplete.
