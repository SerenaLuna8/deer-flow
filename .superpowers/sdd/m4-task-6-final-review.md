# M4 Task 6 Final Independent Review

## Verdict

**NOT APPROVED** — 0 Critical, 3 Important, 0 Minor.

Review target: `068dbe1558048475eee77638ec709090c31ad7da`
Base: `872a304c51de73f1c0e525cde24baaad0b4444bc`

## Important findings

### [Important] Initial MCP discovery runs before the authorization boundary exists

Location: `/Users/jiangfeng/deer-flow/.worktrees/m4-private-work/backend/app/private_work/asset_runtime.py:812`

`PrivateAssetRuntime.materialize()` constructs the run runtime and immediately
awaits `runtime.discover_mcp_tools()`. The caller does not create and install the
`PrivateRunAuthorizationBoundary` until after materialization returns at
`/Users/jiangfeng/deer-flow/.worktrees/m4-private-work/backend/app/gateway/services.py:1055`.
Consequently, initial discovery reaches `_with_one_shot_mcp_tools()` with
`authorization_boundary=None`. The DB revalidation inside
`_materialize_mcp_call()` protects decryption, but revocation can commit after
that transaction and before `client.get_tools()`, allowing one remote MCP call
after execute authority has been revoked.

Independent reproduction patched only the remote transport and committed the
logical marker immediately after materialization revalidation. The real
`discover_mcp_tools()` path produced:

```text
MCP_RACE {'revoked_before_remote': True, 'remote_boundaries': [None]}
```

Missing test: revoke after `_materialize_mcp_call()` returns but before the
initial `get_tools()` call; assert discovery does not contact the MCP server and
the run terminates as `interrupted/authorization_revoked`.

### [Important] A late DB marker can leave the live worker reporting success

Location: `/Users/jiangfeng/deer-flow/.worktrees/m4-private-work/backend/packages/harness/deerflow/runtime/runs/worker.py:674`

If revocation commits after the final runtime boundary but before the worker's
success transition, `RunManager.set_status()` first sets the in-memory record to
`success` at
`/Users/jiangfeng/deer-flow/.worktrees/m4-private-work/backend/packages/harness/deerflow/runtime/runs/manager.py:585`.
The SQL `CASE` in
`/Users/jiangfeng/deer-flow/.worktrees/m4-private-work/backend/packages/harness/deerflow/persistence/run/sql.py:212`
correctly keeps the database row `interrupted`, but the manager does not read
the authoritative result back. In-memory records win over the store in current
worker reads, so `/wait`, run reads, or completion consumers can observe
`success` with no public reason even though PostgreSQL says
`interrupted/authorization_revoked`.

Real PostgreSQL reproduction:

```text
LATE_MARKER {
  'memory_status': 'success', 'memory_error': None,
  'db_status': 'interrupted', 'db_error': 'authorization_revoked'
}
```

Missing test: commit the marker between the last boundary and generic success
completion, then assert both the process-local `RunRecord` and persisted row are
`interrupted` with the public reason `authorization_revoked`.

### [Important] Concurrent connection restores can roll back rejoin/restore

Location: `/Users/jiangfeng/deer-flow/.worktrees/m4-private-work/backend/app/private_work/retention.py:106`

`restore_owner()` uses `UPDATE ... WHERE NOT EXISTS(connected identity)` and
then changes every eligible row to `connected`. Two frozen rows for the same
global `(provider, external_account_id, workspace_id)` can both pass the
predicate when restored concurrently. The partial unique index then raises
`UniqueViolationError` for one writer. Because restore runs inside the same
invitation/project governance transaction, the losing collision rolls back the
membership rejoin or project restore instead of completing governance and
leaving only that connection frozen.

This state is reachable: owner A freezes an identity, owner B later connects and
freezes the same identity, then both scopes restore concurrently. Under the
default PostgreSQL isolation level, a test-only trigger synchronized the two
production `restore_owner()` statements after predicate evaluation:

```text
CONCURRENT_RESTORE {
  'outcomes': [
    ('ok', ('task6-review-frozen-a',)),
    ('IntegrityError', 'asyncpg.exceptions.UniqueViolationError ... uq_channel_connection_active_identity')
  ],
  'statuses': [('task6-review-frozen-a', 'connected'), ('task6-review-frozen-b', 'frozen')]
}
```

Missing test: concurrent invitation rejoin/project restore for two frozen rows
sharing the global identity; both governance operations must commit, exactly one
connection may become connected, and the loser must remain frozen without an
exception.

## Minor findings

None beyond the Important defects above.

## Verification evidence

All PostgreSQL checks used an independently initialized disposable cluster under
`/tmp` on port `55447`; repository fixtures created random
`deerflow_test_*` databases.

- Focused Task 6 governance/authorization gate: `47 passed`.
- Adjacent private runtime, admission, runtime-context, scoped-checkpointer,
  fresh-schema, and harness-import-firewall gate: `85 passed`.
- Plan Gate 2 command: `88 passed, 6 failed`; all six are the already-declared
  Task 11 cutover cases that stop at legacy `POST /api/threads` with
  `409 PRIVATE_WORK_CUTOVER` before Task 6 logic.
- Changed Python files: `ruff check` passed; `ruff format --check` reported
  `37 files already formatted`.
- `git diff --check` passed.

Fresh-schema NULL verification does **not** produce a finding:
`channel_connections.workspace_id` is `NOT NULL`, an attempted NULL insert is
rejected, and `uq_channel_connection_active_identity` indexes the raw three
columns with predicate `status = 'connected'` (no `coalesce`). ORM, migration
0009, and bootstrap predicate agree.

The review found no Task 7 file authority implementation in this commit; only
the reserved `before_file_finalization` protocol hook is present. Legacy runs
without an installed boundary remain no-op at the harness check.
