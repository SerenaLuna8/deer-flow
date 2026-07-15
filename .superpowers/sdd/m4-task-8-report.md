# M4 Task 8 Implementation Report — Private Sandbox File Authority

## Status

- Date: 2026-07-15
- Baseline: `abe99ce73e8069d68993565d1149434cbb97e9d2`
- Branch: `codex/m4-private-work`
- Scope: Task 8 only
- Implementation: repaired GREEN candidate
- Initial fixed-commit review: changes requested (2 Critical / 11 unique Important)
- Frozen repair checklist: closed; final fixed-commit check pending
- Task 9 / Task 11 / Task 12: not started

Task 8 implements the project-private sandbox restore, finalization, Thread
lifecycle, latest-visible-turn branch authority boundary, and provider-specific
private leases. The initial fixed-commit review did not approve `f017dde8`; the
resulting frozen repair list is now closed and merged verification is green. The
task remains unapproved until one final fixed-commit check confirms the frozen
Critical/Important list is closed.

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
- Skill mounts are validated and released by the private authority. Provider
  destroy failures retain the lease for retry; worker cleanup retries at most
  three times without sleeping and does not report success after exhaustion.
  Legacy worker and middleware lifetime remains unchanged.

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
- Local, AIO LocalContainer, E2B, and Boxlite implement run-scoped private
  leases plus bounded descriptor-anchored binary I/O. AIO LocalContainer uses a
  true read-only bind and rejects overlapping writable aliases/runtime sockets;
  E2B uses a verified non-root/no-sudo execution identity; Boxlite uses native
  hypervisor read-only volumes. AIO RemoteProvisioner cannot attest the required
  hardened runtime and therefore fails before allocating a Pod. No project path
  silently falls back to legacy acquisition.

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

- Only a truly new, unversioned empty database may use the existing empty-install
  bootstrap. Existing/versioned databases stop safely at the `0007` staged
  boundary and require the future explicit Task 13 private-work migrator; normal
  startup never synthesizes migration receipts or crosses `0008/0009`.
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

The initial fixed-commit review and repair wave also closed:

- branch requested/head selection escaping the source lock;
- exact checkpoint-id comparison misclassifying metadata-only heads;
- misplaced `rollback_branch_authority` protocol method;
- target checkpoint copies losing channel values;
- post-publication atomic-writer cleanup;
- broad transaction rollback deleting unrelated target authority.
- mapping-root and ancestor replacement, handle/fd leaks, unbounded directory
  scans, Unicode canonical collisions, and post-publish rollback gaps;
- concurrent authority changes between restore and final commit;
- premature `finalizing` release, incompatible workspace-change events, and
  project `present_files` legacy fallback;
- staged-bootstrap boundary drift, branch chunk TOCTOU/whole-file aggregation,
  cross-scope target enumeration, and partial migration retry;
- AIO/E2B/Boxlite private lease, immutable skill delivery, lifecycle cleanup,
  and provider capability checks.

## Fresh verification

Final merged PostgreSQL commands used the isolated PostgreSQL 14 cluster at port `55483`;
fixtures created and dropped random `deerflow_test_*` databases. No business
database was used.

| Gate | Result |
| --- | --- |
| Merged Task 8 core + worker + middleware + Gateway | 477 passed, 0 skipped |
| Cumulative M1-M4 schema + bootstrap + `tests/integration` | 165 passed, 0 skipped |
| AIO/E2B/Boxlite/remote-contract + provider lifecycle | 210 passed, 0 skipped |
| `ruff format --check app packages tests` | 960 files formatted |
| `ruff check app packages tests` | passed |
| `python -m compileall -q app packages tests` | passed |
| `git diff --check` | passed |

## Remaining release condition

Create one repair commit and run one final fixed-checklist review against the
frozen Critical/Important findings. Task 8 remains review-pending until that
check reaches 0 Critical / 0 Important. Task 9 has not started.
