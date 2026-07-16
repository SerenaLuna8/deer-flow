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
