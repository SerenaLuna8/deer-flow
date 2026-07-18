# M7 Task 4 Report: remove the global Scheduled Tasks API

## Status

PASS — implemented Task 4 only from baseline
`2da9e6a62d37c88cb02cead1cc36f9027abe817a`. This report does not update the
milestone ledger, start Task 5, claim M7 completion, or claim release readiness.

## Delivered

- Removed the global `/api/scheduled-tasks*` router and its Gateway imports,
  dependencies, behavior tests, and legacy workspace E2E contract. The project
  `/api/projects/{project_id}/automations*` surface is now the only Automation HTTP
  API, and the removed URL has no OpenAPI route.
- Deleted the legacy Automation read adapter and marker-derived Automation cutover
  guard. Removed `AUTOMATION_CUTOVER` / `AUTOMATION_MIGRATION_REQUIRED` from the
  public error contract. Project Automation now gates directly on
  `FinalSchemaProbe`.
- Preserved the final `scheduled_tasks` / `scheduled_task_runs` PostgreSQL tables
  and scoped repositories; those names remain private persistence details.
- Renamed `ScheduledTaskService` to `AutomationSchedulerService`. Its only public
  methods are `reconcile_admitted_runs(session)` and
  `admit_due_occurrences(session, *, now)`. The independent Scheduler owns the
  transaction and advisory-lock lifetime; the service never commits independently.
- Added in-session due-definition, occurrence/Run/job admission, and terminal
  reconciliation paths so Scheduler reads and writes use the exact caller-owned
  `AsyncSession`. Manual Gateway admission retains the existing atomic dispatcher
  transaction.
- Preserved non-interactive authority: client Automation schemas still reject
  extra authority, while `AutomationDispatcher` alone writes
  `context.non_interactive=true` in the persisted Run snapshot.
- Updated root/backend architecture guidance and the README Project Automations
  section. `.superpowers/sdd/progress.md` was not changed.

## TDD evidence

Initial RED command:

```text
PYTHONPATH=packages/harness .venv/bin/pytest \
  tests/test_m7_legacy_api_surface.py \
  tests/test_project_automations_router.py \
  tests/test_project_automation_service.py \
  tests/test_automation_scheduler_ownership.py \
  tests/test_scheduled_task_service.py -q

17 failed, 27 passed, 58 skipped
```

The expected failures proved that the global router and deleted modules still
existed, cutover errors remained public, and `AutomationSchedulerService` did not
exist. The service tests were then rewritten around the two caller-transaction
operations and ownership revalidation.

Focused GREEN without PostgreSQL configuration:

```text
38 passed, 58 skipped in 1.78s
```

Expanded affected unit/blocking slice:

```text
74 passed, 110 skipped in 2.28s
9 passed in 0.52s  # strict blocking-I/O Automation gate
```

## PostgreSQL evidence

Using the required isolated test server:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres \
PYTHONPATH=packages/harness .venv/bin/pytest \
  tests/test_m7_legacy_api_surface.py \
  tests/test_project_automations_router.py \
  tests/test_project_automation_service.py \
  tests/test_automation_dispatcher.py \
  tests/test_automation_scheduler_ownership.py \
  tests/test_scheduled_task_service.py \
  tests/test_automation_reconciliation.py \
  tests/test_m6_automation_job_admission_postgres.py -q

152 passed in 34.55s
```

This was 0 skipped. It covers the project-role/owner matrix, manual atomic
occurrence/Run/job admission, server-owned non-interactive snapshot, Scheduler
ownership-loss fail-stop, terminal reconciliation, and the durable PostgreSQL
admission invariants. The sandboxed first attempt could not create a local test
database; the same command passed after granting loopback access to the supplied
local PostgreSQL server.

## Collection and Task 3 lastfailed audit

```text
PYTHONPATH=packages/harness .venv/bin/pytest --collect-only -q
8238 tests collected in 2.99s

PYTHONPATH=packages/harness .venv/bin/pytest --lf --collect-only -q
122/518 tests collected (396 deselected) in 1.18s
```

Task 3 had classified exactly two Task 4 nodes:

- `tests/test_legacy_automation_reads_postgres.py::test_expand_legacy_reads_return_exact_dtos_and_hide_other_owners`
- `tests/test_legacy_automation_reads_postgres.py::test_expand_legacy_mutations_are_409_and_write_nothing`

The obsolete test module and implementation were deleted. The lastfailed selection
fell from 124 to 122 nodes; neither Task 4 node remains, so Task 4 direct residual is
0. The surviving 122 nodes are the previously classified Task 2/5/7/8 work and were
not repaired here.

## Static verification

```text
Ruff check: All checks passed for the affected Automation/Gateway/Scheduler files
Ruff format: 25 files already formatted
git diff --check: passed
Production residue scan for AUTOMATION_CUTOVER, legacy_reads,
/api/scheduled-tasks, and ScheduledTaskService: zero hits under backend/app
Deleted module/import scan: only intentional source-absence assertions remain in
tests/test_m7_legacy_api_surface.py
```

## Scope notes

- The historical M5/M6 migration tables and operator migration scripts remain for
  Task 8; Task 4 removes runtime HTTP/read/cutover authority only.
- Legacy frontend source outside the explicitly deleted E2E contract remains for
  its later M7 cleanup owner. The project Automation page already uses only the
  project API and has a static no-global-API assertion.
- No Task 2, Task 5, Task 7, or Task 8 production failure was repaired.
