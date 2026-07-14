# M4 Task 6 Repair Report

## Scope and result

Repaired the three Important findings in `m4-task-6-final-review.md` against
Task 6 commit `068dbe1558048475eee77638ec709090c31ad7da`. The repair stays inside
Task 6 authorization fail-close behavior; it does not implement Task 7 file
authority. One adjacent one-line middleware compatibility regression exposed by
the affected-runtime gate was also closed after explicit authorization.

**Repair result: all three reviewed races now pass their real PostgreSQL
reproductions.** This report records repair evidence only. The original review
remains `NOT APPROVED` until an independent post-repair review signs off.

## Finding-to-fix mapping

### I1: initial MCP discovery preceded the authorization boundary

- `start_private_run()` now creates the `PrivateRunAuthorizationBoundary`
  immediately after admission and passes it through private asset materialization.
- `PrivateAgentRuntime` receives the boundary before its first MCP discovery, and
  the manager record later binds the same abort event. Rebinding a different event
  fails instead of silently replacing the established boundary.
- No PostgreSQL transaction remains open across remote MCP network I/O.
- The real race regression pauses after materialization revalidation commits,
  commits the revocation marker, then releases discovery. Discovery raises
  `AuthorizationRevoked`, never calls remote `get_tools()`, and launch-failure
  compensation persists `interrupted/authorization_revoked`.

### I2: a late marker could leave the live record at success

- `RunStore` now exposes an authoritative status-write contract while preserving
  the legacy `update_status()` API for lightweight/custom stores.
- PostgreSQL status writes use one marker-aware `UPDATE ... RETURNING status,
  error`, so the database returns the final authoritative state selected by the
  revocation `CASE` expression.
- `RunManager.set_status()` serializes writes per record and does not publish the
  requested in-memory status before persistence completes. It synchronizes the
  `RunRecord` from the returned authoritative status/error and triggers the abort
  state for `interrupted/authorization_revoked`.
- The regression blocks the generic success write, commits the marker first, and
  proves both memory and PostgreSQL finish as
  `interrupted/authorization_revoked`.

### I3: concurrent connection restore could roll back governance

- Connection identities now share deterministic signed-int64 PostgreSQL advisory
  transaction locks derived from length-prefixed identity fields. Multiple locks
  are de-duplicated and acquired in numeric order.
- Project restore/rejoin collects all affected owners, acquires all identity locks
  before updates, picks one stable winner per identity, and restores only winners.
  Project lifecycle paths use this bulk restore instead of per-owner lock ordering.
- Normal connection upsert acquires the same identity lock before lookup/revoke/write,
  so normal connects and restore operations use one collision protocol.
- The concurrent real PostgreSQL regression restores two projects containing the
  same two identities in reverse insertion order. Both governance transactions
  commit; exactly one row per identity is connected and the losers remain frozen.

## Adjacent middleware compatibility repair

The affected-runtime gate found three legacy tests whose lightweight tool request
doubles do not define `request.tool`. The middleware now reads tool metadata through
one nested `getattr`, preserving production behavior while accepting the existing
request contract. The exact three tests went from **3 failed** to **3 passed**, and
the full affected-runtime selection finished at **605 passed**.

## Test-driven evidence

### RED

The three real PostgreSQL race regressions failed before production changes:

- I1: expected `AuthorizationRevoked`, but initial discovery reached the remote
  transport.
- I2: the live record remained `success` while PostgreSQL was
  `interrupted/authorization_revoked`.
- I3: one concurrent restore raised `UniqueViolationError` and rolled back.

Combined RED result: **3 failed**.

The adjacent middleware compatibility selection separately produced **3 failed**
before the one-line repair.

### GREEN

- Exact three real PostgreSQL race regressions: **3 passed**.
- Expanded race/service/RunManager selection: **5 passed**.
- Focused Task 6 governance/authorization gate: **39 passed**.
- Final Task 6 plus middleware selection: **73 passed**.
- Final expanded Task 6/private-runtime/middleware selection after the last
  compatibility-only dataclass adjustment: **150 passed in 5.51s**.
- Full affected runtime selection after the compatibility repair: **605 passed**.

## Required PostgreSQL gates

All PostgreSQL verification used the disposable cluster at
`/tmp/deer-flow-m4-task6-fix-pg` on localhost port `55449` with explicit
`POSTGRES_TEST_URL`. Repository fixtures created only random `deerflow_test_*`
databases. No business database was used and no PostgreSQL skip was accepted as
success. The disposable server was stopped after verification.

- Task 1 fresh schema/bootstrap plus governance repositories and Task 6:
  **126 passed**.
- Task 4 Run/snapshot/event/feedback plus M3 resolver: **166 passed, 1 warning**.
- Task 1-3 exact gate: **260 passed, 1 warning**.
- Plan Task 6 Gate 2: **94 passed, 6 failed, 1 warning**.
- Mandatory six-suite runtime gate plus Task 6: **143 passed, 6 failed,
  1 warning**.

The six failures in both applicable gates are the predeclared Task 11 cases that
stop at legacy `POST /api/threads` with `409 PRIVATE_WORK_CUTOVER`:

1. stream completion and persistence
2. real lead-agent business path
3. cancel interrupt
4. interrupt title from checkpoint
5. wait-false title generation
6. rollback checkpoint restore

An expanded legacy channel-connection repository selection also exposed **18
known staged Task 10 failures**: those old tests do not provide the final-schema
`project_id NOT NULL` field. They are recorded here and were not counted as a
Task 6 green gate or reclassified as Task 6/Task 11 defects.

The warnings are the existing Starlette `httpx` deprecation.

## Quality gates

- `ruff check` for all changed Python files: **All checks passed**.
- `ruff format --check` for all changed Python files: **16 files already
  formatted**.
- `python -m compileall -q` for the changed production modules: exited 0.
- `git diff --check`: exited 0.

## Durable documentation

`backend/AGENTS.md` now records that the authorization boundary must exist before
first MCP discovery, that remote I/O must not be enclosed by a database
transaction, that authoritative run status is returned from PostgreSQL before
publishing a process-local terminal state, and that restore and normal connection
writes share deterministic ordered identity locks.
