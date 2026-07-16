# M5 Task 14 Implementation Report

## Status and scope

- Status: complete
- Branch: `codex/m5-project-automation`
- Task base: `08e5cb01470d3ae7948e25c6c9f8702ba85def4d`
- Delivery commit subject: `feat: wire project automation scheduler`
- Scope: Scheduler configuration hardening, Gateway lifecycle wiring, Automation blocking-I/O gates, and matching operator/developer documentation
- Explicit exclusions: Task 15/16 release-gate expansion, frontend work, M6 Worker/SSE ownership, and `.superpowers/sdd/progress.md`

## Delivered contract

- `SchedulerConfig` retains exactly the five M5 fields and conservative disabled defaults. Unknown retry/overlap/misfire policy fields now fail validation instead of being ignored.
- The poll interval and concurrency cap must be positive, the occurrence admission lease must exceed the poll interval, and the one-time minimum delay may be zero but not negative. Existing upper safety bounds remain intact.
- Standard config `$VARIABLE` references are resolved before Scheduler validation; tests cover bool and integer coercion, including a zero one-time delay.
- Gateway constructs one `ScheduledTaskService` and exposes that same object under `scheduled_task_service` and `automation_scheduler`. It never constructs a second poller.
- `automation_scheduler_task` is initialized to `None`, points only to the service-owned poll task while running, is reused by repeated starts for the same app, and is cleared only after shutdown has awaited/cancelled the task.
- `scheduler.enabled=false`, incomplete M4/M5 cutover, missing ownership, and unsupported worker counts leave automatic polling stopped without removing the project Automation dispatcher, readiness service, or manual trigger path.
- Task 7's authoritative PostgreSQL session ownership remains unchanged: `GATEWAY_WORKERS=1` is the topology guard, ownership must already be acquired before polling, and ownership verification remains inside the scheduler service.
- `config.example.yaml` and `backend/AGENTS.md` now state the exact field/validation contract, standard env resolution, single-Gateway M5 topology, and the distinction between occurrence admission lease and M6 Worker execution lease.
- Automation blocking coverage now scans `app.automations`, the project router, Scheduler, and the migration CLI. Request paths cannot import synchronous Alembic; the CLI's sole `command.upgrade` reference must be passed to `asyncio.to_thread`, with a runtime thread-identity assertion.
- The strict Blockbuster smoke gate now proves an unoffloaded blocking call reached through the Scheduler path is detected. Scheduler/reconciliation logs remain aggregate counts, stable codes/status, request IDs, digest prefixes, or exception types; no prompt, title, or private resource ID was added.

## Strict TDD evidence

The clean Task 7/Gateway/config baseline on the supplied PostgreSQL server was:

```text
30 passed in 3.30s
```

Tests were then added before production changes. The focused RED result was:

```text
7 failed, 18 passed in 1.64s
```

The failures were the intended missing contracts: lease not greater than poll, zero one-time delay rejected, unknown Scheduler policy accepted, env-resolved zero rejected, and missing explicit Gateway scheduler/service/task state and singleton task evidence.

After the minimal implementation, the focused suite reached one test-only failure: a heartbeat assertion made an unstable event-loop scheduling assumption even though the Alembic call already used `asyncio.to_thread`. The test was corrected to compare the Alembic execution thread directly with the event-loop thread; no production relaxation was made. Final focused GREEN:

```text
25 passed in 1.53s
```

The pre-existing Task 7 lifecycle/ownership/private-work wiring/reload-boundary gate remained:

```text
30 passed in 2.82s
```

## Verification evidence

All PostgreSQL tests used the explicit isolated server at `127.0.0.1:55435`; fixtures created disposable `deerflow_test_*` databases. No required test skipped.

| Gate | Result |
| --- | ---: |
| Task 14 focused config/wiring/private wiring/blocking | 25 passed |
| Tasks 3–10, Task 7 ownership/lifecycle, M4 Gateway/private runtime, config reload | 696 passed, 0 skipped |
| Real M4 PostgreSQL private-work + migration integration | 12 passed, 0 skipped |
| Full `make test-blocking-io` | 41 passed |
| Automation static blocking-I/O scan | 0 findings |
| Full backend Ruff check | all checks passed |
| Full backend Ruff format check | 1055 files already formatted |
| Compileall | passed |
| `git diff --check` | passed |

The only large-matrix warning was the existing third-party Starlette `TestClient`/`httpx` deprecation warning.

## Self-review

- Config validation is unconditional at startup, including disabled mode, so a later enable cannot activate an unsafe lease/poll relationship.
- `extra="forbid"` enforces the frozen five-field M5 boundary and prevents accidental Task 15/16/M6 policy expansion through config.
- Gateway keeps the existing no-`app.state.config` hot-reload contract: Scheduler reads only the restart-required startup snapshot already passed into `langgraph_runtime`.
- The Automation alias and legacy compatibility name reference one service; the explicit task field references only that service's task. Repeated startup cannot create a second task.
- Marker-required and multi-worker paths return before reconciliation/poll start and leave manual/project dependencies intact. They do not fall back to legacy polling or shared `start_run()`.
- Shutdown still occurs before channel/runtime teardown. If service shutdown errors or times out, the explicit task is cancelled and awaited before its app-state reference is cleared.
- No database authority, scope predicate, occurrence claim, ownership lock, restart reconciliation, manual API behavior, migration DDL, or frontend code was changed.
- New tests read files only from test/static-detector code; production async request paths contain no synchronous file, HTTP, sleep, or Alembic call.
- `backend/AGENTS.md`, `config.example.yaml`, and this report are the only documentation changes. Milestone/progress status remains untouched.

No blocking Task 14 concern remains.

## Formal review remediation

The Task 14 review returned `0 Critical / 1 Important`. The Important finding
identified that scheduler shutdown followed a bare lifespan `yield`: an
exception or cancellation in the served body skipped the statements after the
yield, allowing the runtime context to release scheduler ownership and database
dependencies while the poll task was still alive.

The remediation keeps the production FastAPI lifespan as the authority. Its
real `yield` is now enclosed by `try/finally`; scheduler stop runs before OIDC,
channel, ownership, store, checkpointer, and engine teardown. The explicit poll
task plus both service aliases are cleared unconditionally. A scheduler cleanup
exception/cancellation cannot bypass state cleanup; if it coincides with an
already-propagating lifespan exception, a `BaseExceptionGroup` preserves both
failures instead of hiding the original body error.

Strict TDD evidence for this review fix:

```text
RED real lifespan regression suite: 3 failed, 3 passed in 1.41s
RED body-error plus cleanup-cancel preservation: 1 failed in 1.09s
GREEN real lifespan regression suite: 7 passed in 1.57s
```

The tests call `app.router.lifespan_context` directly. They cover normal exit
and repeated start, body exception with a failing scheduler stop, body task
cancellation, and simultaneous body error plus cleanup cancellation. Each path
asserts stop-await evidence, a finished poll task, stop-before-ownership/runtime
ordering, and cleared app state.

Fresh post-remediation verification:

| Gate | Result |
| --- | ---: |
| Task 14 focused wiring/config/private wiring/lifespan/blocking | 30 passed |
| Task 7 lifecycle/ownership/Gateway recovery | 23 passed |
| Related M5 Automation + M4 private-work PostgreSQL integration | 24 passed, 0 skipped |
| Full `make test-blocking-io` | 41 passed |
| Full backend Ruff check | all checks passed |
| Full backend Ruff format check | 1055 files already formatted |
| Compileall | passed |
| `git diff --check` | passed |

The remediation changes no scheduler admission, ownership, cutover, manual API,
migration, frontend, Task 15, or Task 16 behavior. `.superpowers/sdd/progress.md`
remains untouched.
