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
