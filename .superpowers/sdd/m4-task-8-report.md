# M4 Task 8 Implementation Report — Private Sandbox File Authority

## Status

- Date: 2026-07-15
- Baseline: `abe99ce73e8069d68993565d1149434cbb97e9d2`
- Branch: `codex/m4-private-work`
- Scope: Task 8 only
- Implementation: GREEN candidate
- Independent fixed-commit review: pending
- Task 9 / Task 11 / Task 12: not started

Task 8 now implements the project-private sandbox restore, finalization, Thread
lifecycle, and latest-visible-turn branch authority boundary. The implementation
is not marked approved until an independent fixed-commit review reports 0
Critical / 0 Important findings.

## Delivered boundary

### Runtime authority and sandbox lifetime

- Added a harness-owned `RunFileAuthority` protocol and private scope value
  types without importing `app.*` from the harness.
- `start_private_run` builds and injects one server-owned
  `PrivateRunFileAuthority`; project runs cannot obtain it from client input.
- The worker owns private restore -> graph -> finalization -> release ordering.
  Terminal `success` / `interrupted` is written only after the finalizer commit.
- Cancellation joins the finalizer commit and then rethrows
  `CancelledError`. Rollback, authorization revocation, LLM failure, and general
  worker failure durably mark private finalization failed before terminal status.
- Skill mounts are validated and released by the private authority exactly once;
  legacy worker and middleware lifetime remains unchanged.

### Bounded Local secure I/O and projection

- Local private leases key project + owner + Thread + Run and reject concurrent
  leases for the same private Thread. A released lease resets the next Run's
  mutable workspace with anchored no-follow traversal.
- Local secure reads/writes use directory-fd/openat-style traversal,
  `O_NOFOLLOW`, regular/single-link inode validation, bounded 1 MiB reads, atomic
  publish, fsync, and inode-checked abort cleanup.
- Symlinks, ancestor swaps, hardlinks, FIFOs, devices, and other special files
  fail closed without blocking.
- Restore reads only active-Thread `ready` authority, validates ordered chunk
  hashes and whole-file hashes, revalidates membership per chunk page and after
  publication, and removes every partial/published file on failure or
  revocation.
- AIO, E2B, and Boxlite are explicitly unsupported for project-private leases
  in Task 8. Their `acquire_private` path fails before legacy acquisition; there
  is no silent project-to-legacy fallback.

### Finalization and presented artifacts

- Finalization scans regular workspace/output files, stages bounded chunks
  outside long transactions, and performs a second exact hash scan immediately
  before commit. Same-size mutation, path drift, and content drift all fail.
- Staging IDs are allocated before the first insert and exact-ID cleanup covers
  read, chunk, cancellation, and authorization failures.
- The final transaction atomically promotes immutable file versions,
  supersedes/deletes prior versions, creates only trusted presented-file
  artifacts, and marks `finalization_status='complete'`.
- Unpresented outputs remain ready files without artifacts. Unchanged but
  presented outputs receive current-Run artifact records only after verification.
- Project `present_file`, ThreadData, Uploads, and Sandbox middleware use the
  installed authority and fail closed when it is absent. Legacy host-path
  behavior remains on the explicit non-project branch.

### Thread lifecycle and branching

- Alembic `0011_private_artifact_tombstone` adds the artifact tombstone used by
  active-Thread projection reads.
- Thread deletion tombstones Thread files and artifacts in the same application
  transaction; checkpoint cleanup failure leaves the Thread hidden with
  `retry_required`.
- Latest branch classification uses the requested assistant turn versus the
  final visible human/assistant message at the current head. Later title/root/
  metadata-only checkpoints do not make the assistant turn historical.
- Requested and current-head checkpoint reads are marker-validated through the
  raw saver while the source Thread row lock and application transaction remain
  held. Active or finalizing source Runs are rejected.
- Historical or ambiguous lookup records `historical_skip` and copies no
  authority. Current visible turns copy verified ready file/chunk authority
  server-side with new IDs; Python never materializes chunk payloads.
- Branch artifacts are deliberately not copied: artifacts remain Run-owned and
  their source Run foreign key cannot validly cross to a new Thread. A later Run
  may create new artifacts from explicitly presented, verified files.
- Transaction-time branch failures rely on database rollback and never run a
  broad target cleanup that could delete an already-existing target's files.
  Only a post-commit target-checkpoint failure invokes create compensation.

### Bootstrap and legacy Gateway compatibility

- Fresh/legacy databases at version `<= 0007` with no private source may take a
  probe-gated empty install through `0008`, exact zero-ledger finalize receipts,
  and the Task 8 head. Actual private source, unknown revision, or failed probes
  remain fail closed.
- Alembic DDL bootstrap disposes the SQLAlchemy pool before returning so stale
  asyncpg prepared statement caches cannot survive schema changes.
- Gateway recursive private-authority stripping keeps strict project behavior,
  while the legacy Gateway path explicitly preserves only `model_name` and
  `agent_name` runtime selection inputs. Project start disables that exception.

## TDD and review-driven closures

Initial RED evidence was captured before implementation and during resume work.
Focused REDs covered recursive checkpoint locking, visible-turn branch classification,
transaction rollback deleting an existing target, active/finalizing Runs,
post-replace fsync failure, same-size second-scan mutation, and Gateway runtime
selection stripping. Each was observed failing before the minimal production
change and then rerun GREEN.

The resume review also closed:

- branch requested/head selection escaping the source lock;
- exact checkpoint-id comparison misclassifying metadata-only heads;
- misplaced `rollback_branch_authority` protocol method;
- target checkpoint copies losing channel values;
- post-publication atomic-writer cleanup;
- broad transaction rollback deleting unrelated target authority.

## Fresh verification

All PostgreSQL commands used the isolated PostgreSQL 14 cluster at port `55482`;
fixtures created and dropped random `deerflow_test_*` databases. No business
database was used.

| Gate | Result |
| --- | --- |
| Core Task 8: sandbox projection + finalizer + Thread branch + file service | 133 passed, 0 skipped |
| Cumulative `tests/test_private_*.py` authority matrix | 294 passed, 0 skipped |
| Gateway/checkpointer/worker/middleware/context affected group | 312 passed, 0 skipped |
| Local/base sandbox affected group | 107 passed, 1 platform skip |
| Cumulative M1-M4 PostgreSQL schema files | 53 passed, 0 skipped |
| Bootstrap / cumulative upgrade | 33 passed, 0 skipped |
| `tests/integration` | 73 passed, 0 skipped |
| `ruff format --check app packages tests` | 958 files formatted |
| `ruff check app packages tests` | passed |
| `python -m compileall -q app packages tests` | passed |
| `git diff --check` | passed |

The single sandbox skip is
`test_local_sandbox_command_timeout.py:44`, which requires Linux `/proc` fd
links and is not runnable on the current macOS host. Focused Task 8 and every
PostgreSQL gate completed with zero skips.

## Remaining release condition

Create one fixed implementation commit, run an independent review against that
commit, and repair until the review reaches 0 Critical / 0 Important. Task 8
must remain review-pending until then. Do not start Task 9 from this report.
