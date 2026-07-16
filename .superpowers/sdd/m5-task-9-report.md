# M5 Task 9 Implementation Report — Strict Project Automation API

## Scope

Task 9 exposes the project-and-owner-scoped Automation HTTP surface at
`/api/projects/{project_id}/automations`, wires the Task 3–8 application
services into Gateway lifespan state, and removes legacy scheduled-task write
authority during migration/cutover. This changeset intentionally stops before
Task 10 and contains no frontend work.

The implementation also includes one narrowly scoped API-dependency repair:
manual HTTP trigger needs to dispatch the exact occurrence returned by the
idempotent reservation. The pre-existing occurrence service exposed only the
global scheduler `claim_next()` operation, which could claim a different task.
`claim_manual_occurrence()` now revalidates server-issued authority and locks
project/membership -> definition -> that exact manual occurrence. Only the
request that transitions `queued` to `launching` dispatches; replays of an
already launching, running, or terminal occurrence return the same history and
do not dispatch again.

## Delivered Contract

- Added strict request/query/header and public response DTOs. Client-supplied
  authority and internal execution fields are rejected, response DTOs omit
  owner/membership/lease/hash/raw-error fields, and datetimes require offsets.
- Added the exact readiness, collection, definition, pause, resume, delete,
  manual-trigger, history, and thread-filter routes. Readiness is mounted on a
  separate router before dynamic task routes and is not blocked by project-open.
- Every data route uses `AutomationRoute`, authenticated server-derived
  `PrivateWorkContext`, `require_project_automation_open`, and only service or
  occurrence methods—never direct router repository/session access.
- Manual trigger requires a UUID `Idempotency-Key`, is independent of
  `scheduler.enabled`, reserves and claims the exact occurrence, and dispatches
  at most once for an idempotent replay.
- Gateway lifespan now installs the definition service and explicit scheduler
  state used by the API while retaining the existing scheduler start/stop
  ownership order.
- Added stable `409 AUTOMATION_MIGRATION_REQUIRED`. The legacy router freezes
  every mutation once the expand table exists, leaves the read guard open until
  cutover, and returns `409 AUTOMATION_CUTOVER` for every route after cutover.
- Updated README and backend architecture guidance for the public route and
  migration boundary.

## Strict TDD Evidence

Initial RED command:

```bash
cd backend
POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55435/postgres \
  uv run pytest \
  tests/test_project_automations_router.py \
  tests/test_scheduled_task_router.py \
  tests/test_scheduled_task_router_behavior.py \
  tests/test_private_work_route_dependencies.py -q
```

Collection failed because `app.gateway.automation_schemas` and
`AutomationMigrationRequired` did not exist. After the route skeleton was
implemented, the real PostgreSQL trigger test failed with
`AttributeError: AutomationOccurrenceService has no attribute
claim_manual_occurrence`. That second RED exposed the unsafe gap between
idempotent reservation and exact dispatch; the scoped claim repair above made
the test green.

Final focused GREEN, rerun after formatting and all semantic edits:

```text
60 passed, 0 skipped, 1 third-party TestClient deprecation warning
```

## Expanded Verification

All PostgreSQL commands used the explicitly supplied local test server.

| Gate | Result |
| --- | ---: |
| Task 9 focused routes, legacy guard, private dependency, cutover, errors | 60 passed, 0 skipped |
| M5 Tasks 3–8 plus Task 8 authority repair and M2/M4 private runtime | 342 passed, 0 skipped |
| Legacy scheduled-task router/service/repository/lifecycle | 44 passed, 0 skipped |
| M5 Tasks 1–2 schema/repository/model/harness boundary | 33 passed, 0 skipped |
| Private-work API, M4 cutover, private Run authorization, auth middleware | 183 passed, 0 skipped |
| Ruff check | all checks passed |
| Ruff format check | 16 files already formatted |
| `git diff --check` | clean |

The warning-only output is limited to existing Starlette/httpx TestClient cookie
and deprecation notices; no gate failed and no required PostgreSQL test skipped.

## Self-Review

- Verified readiness is registered before `/{task_id}` and is the only project
  Automation route without the project-open dependency.
- Verified static `/threads/{thread_id}` registration precedes the dynamic task
  route, preventing `threads` from being parsed as a task ID.
- Verified owner/viewer/outsider behavior with real PostgreSQL: owners manage
  only their namespace, viewers receive read-only empty own history and `403`
  for mutation, and outsiders receive `404`.
- Verified one UUID idempotency key creates one occurrence and invokes the
  dispatcher once across replay; the router never uses global `claim_next()`.
- Verified the exact claim preserves project/membership -> task -> occurrence
  lock order and revalidates manage/create/execute authority before dispatch.
- Verified public models contain no project/owner/membership, lease,
  idempotency hash, occurrence key, or raw error-message fields.
- Verified every legacy mutation is stopped before repository/dispatcher
  access in expand, and every legacy route is stopped before those dependencies
  after cutover.
- Verified `.superpowers/sdd/progress.md` is unchanged.

No blocking concern remains. The intended single commit subject is
`feat: expose project automation API`.

## P1 Review Repair — Expand Legacy Read Projection

The Task 9 review found that the report's expand-read claim was not true in
the production lifespan. Task 9 correctly refused to hand the final
project+owner repositories to the legacy router, but set both legacy
repository states to `None`. Consequently list/get/history/thread-alias reads
returned 503 (or 500 in the `object()` test fixture). The test only asserted
that list was not 409, so both failures were false positives.

The repair installs one lifespan-bound `LegacyAutomationReadAdapter` as both
legacy read dependencies and tears it down before the persistence engine. It
has only `list_by_user`, `get`, `list_by_user_and_thread`, and
owner-qualified `list_by_task`; it deliberately has no create/update/delete,
claim, lease, status-write, or dispatch methods. Expand queries use the
authenticated retained `scheduled_tasks.user_id`, and history joins the parent
task before applying that predicate. If the adapter observes final schema
before the cutover marker closes legacy routes, it uses exact authenticated
`owner_user_id` and projects rows only when that owner resolves to one project;
multiple projects fail closed to an empty view. The global legacy cutover
dependency still runs before every endpoint, so completed cutover never
accesses the adapter.

### Repair TDD Evidence

The first focused RED failed at the production wiring boundary:

```text
1 failed, 6 passed
assert app.state.scheduled_task_repo is not None
```

After minimal lifecycle wiring, that selection passed `7 passed`. The next
real PostgreSQL RED seeded `0012_project_automation_expand` rows and failed on
the first authenticated list request with:

```text
AttributeError: 'LegacyAutomationReadAdapter' object has no attribute
'list_by_user'
1 failed, 1 passed
```

The read projection then made the real PostgreSQL suite green. Final coverage
has three tests: exact legacy task/run DTOs for list/get/history/thread alias,
cross-owner invisibility including direct history projection, zero-write 409
mutations, and final-schema multi-project fail-closed behavior. Unit coverage
also requires explicit 503 only when either adapter dependency is genuinely
missing and spies that every cutover route stops before any adapter or
dispatcher call.

### Repair Verification

All PostgreSQL commands used
`postgresql+asyncpg://postgres@127.0.0.1:55435/postgres`; no required test
skipped.

| Gate | Result |
| --- | ---: |
| Final Task 9 focused routes, adapter, cutover, errors, wiring | 67 passed |
| Legacy router/service/repository/lifecycle compatibility | 58 passed |
| M5 Tasks 3–8 plus M2/M4 governance/private runtime | 342 passed |
| M5 Tasks 1–2 schema/migration/repositories | 59 passed |
| Private-work API, M4 cutover, authorization, auth middleware | 143 passed |
| Full backend Ruff check | all checks passed |
| Full backend Ruff format check | 1048 files already formatted |
| `git diff --check` | clean |

Repair self-review confirmed the adapter cannot mutate or dispatch, every SQL
read carries authenticated ownership, final projection does not aggregate one
owner across projects, history does not trust a naked task ID, lifecycle
teardown clears both state references, and Task 10/frontend remain untouched.
The repair is committed separately from the original Task 9 implementation.
