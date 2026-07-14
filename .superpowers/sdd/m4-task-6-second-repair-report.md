# M4 Task 6 Second Repair Report

## Scope and disposition

This repair starts from stable Task 6 base
`9f1cafc20271c7a5653d18e03e57b88412bee194` and addresses only the two
Important findings in `m4-task-6-post-repair-review.md`. It does not implement
Task 7 file authority or any later M4 task.

Both reviewed reproductions are fixed and covered by real PostgreSQL tests.
This is implementation evidence, not approval; Task 6 still requires a third
independent review.

## Finding-to-fix mapping

### I1: completion replaced the authoritative authorization reason in memory

- `RunManager.update_run_completion()` now shares the same per-record
  `status_write_lock` as `set_status()`, so completion and authoritative status
  publication have one deterministic write sequence in either arrival order.
- The manager's global registry lock is used only for short record lookup and
  mutation sections; no store I/O occurs while that global lock is held.
- Once a record is authoritatively
  `interrupted/authorization_revoked`, completion normalizes the store-facing
  status/error to that terminal state and refuses to replace the in-memory
  public reason. Token, message, and other non-security completion fields still
  update normally.
- Ordinary completion behavior remains unchanged for legacy stores and
  no-store managers. An incoming `error=None` retains the existing error, while
  a non-null ordinary completion error still replaces it.

### I2: normal connect destroyed a frozen loser and credential

- `_revoke_other_active_owners()` now selects only other owners whose row is
  actually `connected`. Frozen rows are excluded from both the status update
  and credential deletion.
- The project-scoped PostgreSQL regression proves a frozen owner and credential
  version 7 survive while a same-identity revoked row reconnects.
- The same regression preserves normal behavior: repeated upsert is idempotent,
  a genuinely connected old owner transfers to revoked, its old credential is
  removed, and the new owner becomes the sole connected row.
- Existing deterministic identity advisory locking remains in place and is
  covered by the targeted lock-before-lookup tests.

## TDD evidence

### RED

The exact two real PostgreSQL regressions failed before production changes:

- completion left memory at `interrupted/completion detail` while PostgreSQL
  remained `interrupted/authorization_revoked`;
- normal connect changed the frozen row to `revoked` and deleted credential
  version 7.

Combined exact RED result: **2 failed in 0.71s**.

The explicit status-first and completion-first ordering regressions also failed
before the manager change: **2 failed in 0.24s** because the second operation
entered its store write while the first record write was blocked.

### GREEN

- Exact two PostgreSQL regressions: **2 passed in 0.66s**.
- Status-first and completion-first ordering: **2 passed**.
- Ordering plus legacy/no-store/error-`None` and missing-row completion
  compatibility selection: **6 passed**.
- Identity lock and completion compatibility targeted selection: **6 passed**.
- Task 6 focused: **40 passed**.

## PostgreSQL and release-gate evidence

All PostgreSQL tests used the independent disposable cluster at
`/tmp/deer-flow-m4-task6-second-pg` on `127.0.0.1:55453`. Every command supplied
an explicit `POSTGRES_TEST_URL`; fixtures created and dropped only random
`deerflow_test_*` databases. No business database or PostgreSQL skip was used.
The server was stopped after verification.

- Fresh schema/governance exact set: **127 passed**.
- Gate 2 exact set: **99 passed, 6 failed, 1 warning**.
- Mandatory runtime plus Task 6: **144 passed, 6 failed, 1 warning**.
- Task 4 plus M3 resolver: **171 passed, 1 warning**.
- Task 1-3 exact union: **260 passed, 1 warning**.
- Affected runtime: **281 passed**.

The six Gate 2/mandatory failures are exactly the predeclared Task 11 lifecycle
cases stopped at legacy `POST /api/threads` with
`409 PRIVATE_WORK_CUTOVER`; none enters Task 6 behavior.

The legacy channel repository baseline remains exactly **18 failed**. Every
failure is the known staged Task 10 fixture/contract gap in which final-schema
`channel_connections` or OAuth rows are written without required `project_id`.
This set was recorded separately and was not counted as green or used to mask a
new failure.

## Quality gates

- Changed Python files: `ruff check` passed.
- Changed Python files: `ruff format --check` passed.
- Production modules: `python -m compileall -q` exited 0.
- `git diff --check` exited 0.

## Durable documentation

`backend/AGENTS.md` now records the shared per-record completion/status write
sequence, the prohibition on holding the manager global lock across store I/O,
the immutable authorization-revoked terminal reason with non-security
completion persistence, and the rule that normal identity transfer may destroy
only genuinely connected rows, never frozen retention state.
