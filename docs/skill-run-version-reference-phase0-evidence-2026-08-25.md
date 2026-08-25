# Skill Run Version Reference Phase 0 evidence

Date: 2026-08-25 (Asia/Shanghai)

This is a content-free baseline captured before Schema V1 recreation and before
the Version Reference implementation changed runtime code. It records only
counts, sizes, topology, and gate results; it contains no database URL, account,
Project, Run, Skill path, or secret value.

## Code and process baseline

- Code: `1aeb024a` (`feat: harden skill lifecycle and run recovery`).
- Runtime processes: Gateway 0, Scheduler 0, Worker 0, Frontend 0, Nginx 0.
- Configured local topology: one Gateway process, one Scheduler process, one
  Worker process; `worker.max_concurrent_jobs=8`.
- Gateway database pool defaults: `pool_size=5`, `max_overflow=10`.
- Configured Sandbox provider: Native AIO with the local container backend.

## Read-only PostgreSQL baseline

- PostgreSQL: 17.11; application marker and catalog reported `schema_v1` ready.
- Database size: 400,176,819 bytes.
- Skill v2 Run parents: 0; Skill v3 Run parents: 0; all Run asset parents: 0.
- `skill_version_files`: 38,848 rows and 238,270,574 declared content bytes.
- Largest Skill file: 1,877,786 bytes.
- Files larger than 64 MiB: 0.
- `skill_version_files` relation size: 228,679,680 bytes.
- `run_asset_versions` relation size: 9,469,952 bytes.
- Worker registry rows: 17 historical rows, 0 fresh Workers, fresh capacity 0.
- Ready private Run Jobs: 0; active leases: 0.
- Initial WAL location: `64/F9C74218` (comparison point only, not a payload or
  throughput result).

## Baseline gates

The pre-change containment/recovery suite passed:

```text
tests/test_agent_runtime_checksum.py
tests/test_worker_service.py
tests/test_serve_daemon_contract.py
83 passed in 2.43s
```

These results prove only that the checked-out v2/v3 codec, Worker retry, and
local supervisor contract tests were green before the new tests were added.
They do not prove a running Worker, a live model, a browser flow, provider
readback, or the target v4 resource envelope.

## Confirmed implementation-plan gap

The pre-change `retention_purge` Job persists only a Project/former-owner scope
and an irreversible SHA-256 idempotency key. The Worker reconstructs the case by
testing whether `owner_user_id` is null, so a durable account-wide purge fence
cannot survive restart and would be misclassified as former-owner work. The
implementation must therefore persist a typed account retention case containing
at least the exact lifecycle generation and effective time, and must route its
claim/Worker reconstruction through that authority. Adding lifecycle fields to
`users` without this durable coordinator would leave the two-phase purge
contract incomplete.

The plan also requires every owner-private claim to revalidate the lifecycle
generation, but the pre-change Job/Run schema persists no expected account
generation. Checking only whether the User is currently `active` would allow a
missed old Job to become claimable after pending deletion is cancelled or the
account is explicitly reactivated. The implementation must freeze the guarded
generation in each owner-private Job (or an equivalent exact execution
coordinate), pass it from every scope-expanding admission path, and compare it
before claim mutation. A late database trigger that first locks User after a
domain row would violate the required Project-before-User lock order and is not
an equivalent fix.

## Incremental provider evidence

After the typed Sandbox lifecycle contract was added, the P-02 Native AIO plus
LocalContainerBackend path was exercised with Apple Container 1.2.2 and the
configured AIO 1.11.0 image. The real non-root guest read the bounded owner
manifest, its write probe failed on the read-only `/mnt/skills` mount, owner
labels matched, release produced exact absence proof, and the test container was
absent afterward. The real probe passed once in 7.06 seconds; the surrounding
typed/AIO/remote/private/security regression set passed 248 tests with one
environmental skip. This is P-02 evidence only: it does not prove Compose DooD,
BoxLite, E2B, Remote Kubernetes, the app materializer, or the final browser Run.
