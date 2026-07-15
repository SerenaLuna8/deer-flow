# Task 7 Third Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the two third-round Task 7 review findings without entering Task 8: disabled-scheduler startup must still reconcile manual Automation runs, and an enabled scheduler must permanently fail-stop when its PostgreSQL ownership session or advisory lock is lost.

**Architecture:** Startup recovery and automatic polling are separate concerns. A complete M5 runtime always runs `AutomationReconciler` before generic M4 orphan recovery, while only enabled single-worker polling acquires the lifetime ownership lock. `AutomationSchedulerOwnership` becomes a permanent state machine that records the original PostgreSQL backend PID and validates, on the same connection before every poll, both that PID and the existing `pg_locks` row without taking the advisory lock again. `ScheduledTaskService` stops its loop on ownership loss and exposes a scheduler runtime status consumed by Automation readiness.

**Tech Stack:** Python 3.12, asyncio, FastAPI lifespan, SQLAlchemy async PostgreSQL, PostgreSQL session advisory locks/`pg_locks`, pytest/pytest-asyncio, Ruff.

## Global Constraints

- Strict RED -> GREEN TDD for every production behavior change.
- Keep `GATEWAY_WORKERS` as the quick local topology guard.
- Disabled scheduler mode acquires no ownership connection and starts no poller, but manual/project Automation APIs and startup reconciliation remain available.
- Ownership loss is permanent for that object/process; do not silently reacquire or resume polling.
- Heartbeats must not call `pg_try_advisory_lock` or otherwise increment the session lock count.
- Logs and readiness expose only stable status/error codes, never backend PID, database URL, project/owner/task/occurrence/Run IDs, prompts, or titles.
- Do not implement Task 8 membership/project lifecycle behavior.
- Append the existing `.superpowers/sdd/m5-task-7-report.md` and create exactly one separate review-fix commit.

---

### Task 1: Reconcile disabled manual runs before generic M4 recovery

**Files:**
- Modify: `backend/tests/test_automation_reconciliation.py`
- Modify: `backend/app/gateway/deps.py`

**Interfaces:**
- Consumes: `AutomationCutoverGuard.require_project_open()`, `AutomationReconciler.reconcile_restart(now)`, and `RunManager.reconcile_orphaned_inflight_runs(...)`.
- Produces: startup order `automation reconciliation -> generic M4 orphan recovery` whenever the concrete startup configuration contains the M5 scheduler section, regardless of `scheduler.enabled`; lock acquisition remains gated by `scheduler.enabled`.

- [ ] **Step 1: Write a real PostgreSQL failing startup test**

  Seed a one-time, manually-triggered occurrence linked to a pending deterministic private Run. Enter `langgraph_runtime()` with a real `AppConfig` whose scheduler is disabled, real M5 cutover guards/reconciler/repositories, and only non-database infrastructure mocked. Assert the private Run becomes `interrupted`, the occurrence becomes `interrupted`, the parent becomes `cancelled` with `run_count == 1`, no ownership connection is acquired, and no poll task starts.

- [ ] **Step 2: Run the focused test and verify RED**

  Run `POSTGRES_TEST_URL=... uv run pytest -q tests/test_automation_reconciliation.py::<new-test>`.

  Expected: the current generic recovery runs first, leaving the Run/occurrence failed rather than interrupted.

- [ ] **Step 3: Implement the minimal startup-order fix**

  In `langgraph_runtime()`, retain enabled-only lifetime lock acquisition, but gate Automation reconciliation on presence of the concrete scheduler configuration rather than its `enabled` flag. Continue catching only `AutomationCutover`; all availability/database failures remain fail-closed before generic mutation. Partial `SimpleNamespace` fixtures with no scheduler section retain compatibility, while production `AppConfig` always carries the section.

- [ ] **Step 4: Run focused and Gateway recovery tests GREEN**

  Run the new PostgreSQL test plus `tests/test_gateway_run_recovery.py` and `tests/test_scheduled_task_lifecycle.py`.

---

### Task 2: Detect permanent PostgreSQL ownership loss without changing lock count

**Files:**
- Modify: `backend/tests/test_automation_scheduler_ownership.py`
- Modify: `backend/app/automations/ownership.py`

**Interfaces:**
- Produces: `AutomationSchedulerOwnership.backend_pid: int | None`, `is_lost: bool`, and `async verify() -> None`.
- Invariant: `acquire()` records `(pg_backend_pid(), pg_try_advisory_lock(...))` from one physical session; `verify()` queries that same connection for the current PID and matching granted `pg_locks` row. Any SQL error, reconnect/PID drift, or missing lock transitions permanently to lost and raises stable `AutomationUnavailable("automation-scheduler-ownership")`.

- [ ] **Step 1: Write real PostgreSQL RED tests**

  Add tests that (a) call normal `verify()` repeatedly and prove the owner's advisory-lock row count stays exactly one, and (b) restrict a `pg_terminate_backend` target to the fixture's `deerflow_test_*` database, terminate the recorded dedicated PID, verify permanent loss, allow a second owner to acquire, and prove the first owner cannot reacquire.

- [ ] **Step 2: Run the focused ownership tests and verify RED**

  Expected: `backend_pid`, `is_lost`, and `verify()` do not exist.

- [ ] **Step 3: Implement the ownership state machine**

  Acquire and record PID/lock atomically, add a read-only `pg_locks` heartbeat query, commit the heartbeat's implicit transaction, and centralize cancellation-safe lost-connection invalidation/close. Never use `pg_try_advisory_lock` in `verify()`. Once lost, both `verify()` and `acquire()` fail without opening another connection.

- [ ] **Step 4: Run the ownership suite GREEN**

  Confirm normal release, exceptional startup release, ambiguous acquisition cleanup, lock-count stability, backend termination, second-owner takeover, and permanent first-owner loss.

---

### Task 3: Fail-stop polling and expose scheduler ownership loss in readiness

**Files:**
- Modify: `backend/tests/test_scheduled_task_service.py`
- Modify: `backend/tests/test_automation_scheduler_ownership.py`
- Modify: `backend/tests/test_automation_readiness.py`
- Modify: `backend/app/scheduler/service.py`
- Modify: `backend/app/automations/readiness.py`
- Modify: `backend/app/gateway/deps.py`

**Interfaces:**
- `ScheduledTaskService(..., ownership: AutomationSchedulerOwnership | None)` verifies ownership before reconciliation/start and before each `reserve_due` poll.
- `ScheduledTaskService.status` returns `stopped`, `running`, or `ownership_lost`.
- `AutomationReadiness.scheduler_status` returns `disabled`, `stopped`, `running`, or `ownership_lost`, independent of project/manual API readiness.

- [ ] **Step 1: Write service/readiness RED tests**

  Prove a lost owner causes `run_once()` to raise before `reserve_due`, the background task exits rather than retrying, no claim/dispatch occurs, restart with the permanently lost owner fails, disabled startup has no monitor/poll, and readiness reports `ownership_lost` while project Automation remains otherwise ready.

- [ ] **Step 2: Run focused tests and verify RED**

  Expected: constructor/status/readiness interfaces are missing and the existing loop continues after ownership errors.

- [ ] **Step 3: Implement minimal fail-stop and observability**

  Inject ownership from Gateway wiring, verify before every automatic poll, return permanently from `_run_loop()` on ownership loss with a stable redacted log, and add a readiness status provider bound to the app-owned service. Disabled mode maps to `disabled` without requiring ownership.

- [ ] **Step 4: Run service, ownership, readiness, lifecycle, and wiring suites GREEN**

  Include the real PostgreSQL running-service termination scenario: first owner polls once, target PID is terminated, its next heartbeat marks lost and exits before another reserve/claim, second owner acquires, and the first service never resumes.

---

### Task 4: Documentation, complete regression gates, report, and commit

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `backend/AGENTS.md`
- Modify: `config.example.yaml`
- Modify ignored report: `.superpowers/sdd/m5-task-7-report.md`

- [ ] **Step 1: Update lifecycle documentation**

  Document disabled reconciliation independence, original-session/PID/lock heartbeat semantics, permanent fail-stop, readiness status, and the fact that disabled mode creates no monitor/lock.

- [ ] **Step 2: Run the full requested matrix**

  Run Task 7/Gateway/worker/ownership, Tasks 5/6, affected M4, and Tasks 1-4 PostgreSQL suites using the supplied local server. Run `ruff check .`, `ruff format --check .`, compileall, and `git diff --check`.

- [ ] **Step 3: Self-review the complete repair diff**

  Check startup order, production-versus-partial config boundary, advisory lock count, original backend identity, permanent loss, no post-loss reserve/claim/dispatch, shutdown cancellation cleanup, readiness/manual API separation, redacted logs, and Task 8 exclusion.

- [ ] **Step 4: Append report and create one commit**

  Append exact RED/GREEN/final gate evidence to `.superpowers/sdd/m5-task-7-report.md`, stage only this repair's tracked files, and commit once with subject `fix: fail stop lost automation ownership`.

## Self-Review

- Spec coverage: both Important findings, every required real PostgreSQL scenario, disabled/manual behavior, readiness, logging, shutdown, full gates, report, and one commit are assigned above.
- Placeholder scan: no TBD/TODO or unspecified implementation step remains.
- Type consistency: ownership `verify()/backend_pid/is_lost`, service `ownership/status`, and readiness `scheduler_status` names are consistent across Tasks 2-3.
