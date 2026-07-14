# M4 Task 6 Authorization Revocation Report

## Scope and outcome

Task 6 adds database-authoritative cancellation for active project runs and
fail-closed authorization checks at every current runtime side-effect boundary.
It does not add Task 7 file authority, retention expiry deletion, SQLite support,
PostgreSQL RLS, or any product cutover route.

The implementation keeps the governance write authoritative: membership and
project lifecycle services lock project, membership(s), then live runs; write
`authorization_cancel_requested_at` and the stable reason
`authorization_revoked` in the same transaction; and only after commit perform a
best-effort process-local `RunManager` cancellation. A missing local task or a
notifier failure cannot roll back governance.

## Governance and retention behavior

- Admin to Editor/Runner keeps active runs alive. Admin/Editor/Runner to Viewer
  revokes live runs without freezing existing private content.
- leave/remove, project suspension, and pending deletion revoke live runs and
  freeze same-project/same-owner Threads and connected channel connections.
- invitation rejoin reactivates the existing membership row and restores only
  that project/owner. Project restore/resume restores only current active members.
- revoked connections never restore; a frozen connection stays frozen when its
  external identity is already held by another connected row.
- completed runs are never marked. Freeze/restore never physically deletes
  Thread, file, Memory, connection credential, or other private content.
- the fresh-schema bootstrap catalog now matches ORM/migration 0009: the global
  active connection identity index predicate is `status = 'connected'`.

## Runtime fail-close boundary

The harness owns an app-independent `AuthorizationBoundary` protocol and
`AuthorizationRevoked` control-flow signal. The app boundary queries the run
marker, active/unsuspended project, active membership, and current executable
role without requiring the admission-time membership version. It sets the local
abort event and fails closed on database errors.

Checks run before every async model invocation/retry, direct title,
summarization, and goal-evaluator model call, tool dispatch, private MCP
discovery/invocation, sandbox exec/write, checkpoint read/write, and the reserved
future file-finalization hook. Private checkpoint and exact MCP paths use this
run-bound rule, so Admin to Editor/Runner version changes continue while Viewer
or marker changes stop at the next boundary.

The worker maps revocation to `interrupted`, publishes only
`authorization_revoked`, and skips interrupted-title model generation. Run SQL
status/completion updates use the marker as the final authority so a racing
generic completion cannot overwrite the interrupted result.

## TDD evidence

The initial focused RED stopped during collection with the expected missing
module:

```text
ModuleNotFoundError: No module named 'app.private_work.authorization'
```

The completed Task 6 PostgreSQL-specific suite passed with no skips:

```text
13 passed in 0.67s
```

It covers active/unmarked-only markers, successful and cross-project runs,
role-version continuity across model/tool/MCP/sandbox/checkpoint boundaries,
remote revocation, same-scope freeze, connection collision restore, stable
public errors, trusted local cancellation, and worker title suppression.

## Release-gate evidence

Every PostgreSQL command used the disposable `/tmp` instance on port `55438`.
Fixtures created only random `deerflow_test_*` databases and all reported
PostgreSQL gates used an explicit `POSTGRES_TEST_URL`.

```text
# Task 1 fresh schema/bootstrap + governance repositories + Task 6 PG
78 passed, 0 failed, 0 skipped in 10.71s

# Gate 2, Tasks 3-6
133 passed, 0 failed, 0 skipped in 14.20s

# Mandatory runtime matrix plus Task 6
188 passed, 6 declared Task 11 failures, 0 skipped in 9.83s

# Task 4 run/snapshot/event + M3 resolver
166 passed, 0 failed, 0 skipped in 12.76s

# Task 1-3 schema/context/import-firewall/Thread/checkpointer
260 passed, 0 failed, 0 skipped in 25.71s

# Affected manager/worker/model/title/summarization/goal/sandbox runtime
381 passed; one PostgreSQL skip rerun separately and passed; one existing
macOS platform skip for Linux /proc behavior
```

The mandatory suite's six failures are the predeclared Task 11 staged cases.
Each stops at legacy `POST /api/threads` with `409 PRIVATE_WORK_CUTOVER` and none
enters Task 6 logic:

1. `test_stream_run_completes_and_persists_runtime_state`
2. `test_stream_run_executes_real_lead_agent_setup_agent_business_path`
3. `test_cancel_interrupt_stops_running_background_run`
4. `test_cancel_interrupt_generates_missing_title_from_checkpoint`
5. `test_cancel_wait_false_generates_title_from_graph_input_before_checkpoint`
6. `test_cancel_rollback_restores_pre_run_checkpoint`

## Quality gates

```text
ruff check: All checks passed
ruff format --check: 27 files already formatted
python -m compileall app/private_work app/projects app/gateway packages/harness/deerflow: passed
git diff --check: passed
```

The Task 6 architecture and operational invariants are recorded in
`backend/AGENTS.md`. The disposable PostgreSQL server was stopped before the
implementation commit.
