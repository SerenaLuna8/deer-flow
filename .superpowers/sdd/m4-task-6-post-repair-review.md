# M4 Task 6 Post-Repair Final Review

## Verdict

**NOT APPROVED — 0 Critical, 2 Important, 0 Minor.**

Reviewed range:

- Fixed base: `068dbe1558048475eee77638ec709090c31ad7da`
- Repair commit: `9f1cafc20271c7a5653d18e03e57b88412bee194`
- Overall baseline used for cumulative diff checks: `872a304c`

The repair closes the original remote-discovery race and the core multi-identity restore concurrency defect. It also makes authoritative status publication atomic. Approval is still blocked by two fail-close/retention defects that are reproducible against a fresh PostgreSQL database.

## Important findings

### I1. Completion persistence can replace the authoritative public authorization reason in memory

Locations:

- `backend/packages/harness/deerflow/runtime/runs/manager.py:366-379`
- `backend/packages/harness/deerflow/runtime/runs/worker.py:756-759`
- Database-side compensation: `backend/packages/harness/deerflow/persistence/run/sql.py:376-400`

`RunManager.update_run_completion()` ignores only the incoming `status`. It still copies a non-null completion `error` onto the in-memory `RunRecord` before calling the backing store. The worker invokes this method after terminal status publication. The PostgreSQL store correctly keeps a revoked run as `interrupted / authorization_revoked`, but it does not return the compensated values to the manager, so the in-memory record can become `interrupted / <completion detail>`.

Fresh PostgreSQL reproduction:

```text
COMPLETION_REASON_DIVERGENCE {'memory': ('interrupted', 'completion detail'), 'database': ('interrupted', 'authorization_revoked', 17)}
```

Impact: after a late authorization marker has won, different readers can observe different public reasons, and the in-memory reader can expose a non-authoritative completion detail instead of the safe `authorization_revoked` reason. This leaves the original authoritative-status Important only partially closed.

Required repair and regression boundary: publish the marker-aware completion result back to memory, or prevent completion from overwriting the already-authoritative terminal reason. Add a test that commits a late marker, publishes the authoritative interrupted state, then submits completion data containing a different error and asserts both memory and PostgreSQL remain `interrupted / authorization_revoked`.

### I2. Normal connect revokes a Task 6 frozen loser and deletes its retained credential

Location: `backend/packages/harness/deerflow/persistence/channel_connections/sql.py:140-157`

When connecting one owner, `_revoke_other_active_owners()` selects every same-identity row owned by another user whose status is not `revoked`. That includes Task 6 `frozen` rows. It then changes those rows to `revoked` and deletes their credentials. The shared identity advisory lock prevents uniqueness races, but the mutation violates the Task 6 freeze contract: a frozen loser and its credential must remain retained when another owner already occupies or reconnects the identity.

Fresh PostgreSQL reproduction used two valid project-scoped rows, avoiding the known Task 10 missing-`project_id` gap:

```text
FROZEN_NORMAL_CONNECT {'connected_id': 'revoked-owner-b', 'rows': [('frozen-owner-a', 'revoked', True, False), ('revoked-owner-b', 'connected', False, False)]}
```

The first row began as `frozen` with credential version 7. After owner B's normal connect, owner A was `revoked` and its credential was absent.

Impact: a normal connection operation can destroy private retention state and retained credentials that Task 6 requires to survive. This is a Task 6 Important, not merely the known staged Task 10 schema compatibility failure.

Required repair and regression boundary: exclude frozen rows from normal ownership transfer/destructive credential cleanup, while preserving the single connected winner rule. Add a real PostgreSQL test with frozen owner A plus credential and same-identity revoked owner B, connect B, and assert A remains frozen with its credential while B becomes connected.

## Original three Important findings

| Original finding | Result | Evidence |
| --- | --- | --- |
| Remote MCP discovery could occur after revocation | Closed | The run boundary is constructed in gateway launch, reaches runtime materialization before discovery, and is checked before `client.get_tools`. The exact marker race test leaves `remote_calls=0`, raises authorization revocation, compensates the database to `interrupted / authorization_revoked`, and leaves no manager registration. A separate blocked-remote test showed governance marking completed while discovery was blocked, so no database lock is held across remote I/O. |
| Status publication was non-atomic and could transiently publish success | Partially closed; still Important | PostgreSQL now uses one marker-aware `UPDATE ... RETURNING`, and `RunManager.set_status()` serializes and publishes only after persistence. Boolean/`None` legacy stores remain compatible, and the no-transient-success regression passes. Completion persistence can still overwrite the in-memory public reason, as I1 above demonstrates. |
| Multi-identity restore could deadlock or select inconsistent winners | Core concurrency defect closed | Identity keys are deterministic signed int64 values, bulk restore acquires sorted deduplicated locks, lifecycle calls bulk restore once, and normal upsert uses the same identity lock. A real concurrent two-project/two-identity restore completed without deadlock or uniqueness error and produced exactly one connected winner per identity. The separate destructive normal-connect/frozen-row behavior remains Important as I2 above. |

## Race and compatibility boundary review

- Late marker after MCP call materialization but before discovery: fail-closed before remote `get_tools`; no manager residue.
- Governance while remote discovery is blocked: governance commit completes; no database lock spans remote I/O.
- Late marker during status publication: marker-aware single database update prevents a transient successful state.
- Manager publication ordering: per-record locking is present; backing-store result is applied before publication for `set_status`.
- Legacy store compatibility: boolean, `None`, and no-store status paths remain supported.
- Concurrent restore: deterministic lock ordering and stable winner selection work across multiple identities and projects.
- Existing connected winner, revoked-row exclusion, and repeated restore: core restore behavior is stable and idempotent under the exercised cases.
- Normal connect versus frozen retention: **fails**, as described in I2.
- Post-terminal completion versus authoritative public reason: **fails**, as described in I1.
- Task 7 scope: no Task 7 file-authorization implementation was introduced. The reserved file-finalization boundary remains unused.
- Tool-error middleware compatibility: the nested attribute lookup tolerates test doubles without `.tool`; affected production behavior and tests remain intact.

## Verification evidence

All PostgreSQL suites and independent reproducers used a dedicated local PostgreSQL instance on port 55451 and fresh random `deerflow_test_*` databases through an explicit async PostgreSQL URL.

| Verification set | Result |
| --- | --- |
| Exact repair regressions: marker race, manager residue, late marker, concurrent restore, lock ordering/hook, no transient success | **7 passed** |
| Independent PostgreSQL diagnostics: blocked remote/no database lock and completion-reason divergence | **2 passed**; diagnostic output reproduced I1 |
| Task 6 focused: private authorization, membership service, lifecycle service | **39 passed** |
| Fresh schema/governance plus Task 6 | **126 passed** |
| Task 4 plus M3 resolver | **167 passed**, 1 warning |
| Task 1-3 exact union | **260 passed**, 1 warning |
| Gate 2 exact | **94 passed, 6 failed**, 1 warning |
| Expanded mandatory set | **143 passed, 6 failed**, 1 warning |
| Repair-affected direct suites | **150 passed** |
| Affected worker/lead/runtime/sandbox risk selection | **277 passed** |
| Legacy Task 10 channel repository baseline | **18 failed** |

The six Gate 2/mandatory failures are the predeclared Task 11 `/api/threads` `409 PRIVATE_WORK_CUTOVER` failures. The 18 legacy channel repository failures all arise from the known staged Task 10 missing-`project_id` requirement on channel connection/OAuth rows. Neither known staged group was used to create the two Important findings above, and neither masks them.

Static validation on all 16 Python files changed by the repair:

- `ruff check`: passed.
- `ruff format --check`: passed (`16 files already formatted`).
- Production-file `compileall`: passed.
- `git diff --check` for fixed-base-to-repair: passed.
- `git diff --check` for overall-baseline-to-repair: passed.

## Final disposition

The repair is materially improved but cannot be approved until both Important defects have implementation fixes and real PostgreSQL regressions covering their exact boundaries. Minor count is zero.
