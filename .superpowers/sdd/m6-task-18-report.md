# M6 Task 18 — Operational cutover and process orchestration

## Outcome

Implemented the explicit M5-to-M6 reliability cutover without opening Task 19
release-gate scope. The command performs a zero-write dry-run, authenticates
externally committed pre-M6 backup evidence, stops at the expand revision for
resumable backfill/probes, and writes the final revision and singleton cutover
marker only after every required domain probe succeeds.

Task 18 also makes Gateway, Worker, and optional Scheduler distinct local and
Compose roles, and extends system-admin readiness with aggregate-only process,
Scheduler ownership, and cutover state. M6 milestone closure and release gates
remain separate work.

## RED evidence and fixes

The initial focused tests failed because the migration CLI, reconciliation CLI,
process-readiness aggregation, Make targets, and Compose/local role contracts
did not exist. The RED suite was strengthened to cover:

- dry-run catalog/counter/ledger/sequence immutability;
- maintenance and authenticated backup evidence before expand DDL;
- running execution rejection while pending Runs remain migratable;
- exact online-compatible reservations for every active membership, non-zero
  ready file, and pending Run, including real online release after cutover;
- aggregate-only quota history rejection;
- recovery-probe failure at `0014`, marker ordering, and idempotent resume;
- backup/commit/attestation tamper and wrong-source rejection;
- missing Worker, Scheduler disabled, enabled-unowned, owned, and ownership-lost
  readiness;
- root Make commands, shell syntax/stub execution, conditional Scheduler profile,
  Compose role health, no inherited role ports, and Gateway independence.

During affected regression, the M5 migration suite exposed two descendant-head
assumptions. Its test support now freezes `upgrade("head")` at the M5 final
boundary, while completed M5 noop inventory accepts the M6-added occurrence
`job_id` column. Fresh bootstrap assertions now use the current M6 final head.

## Migration and evidence contract

`backend/scripts/migrate_reliability.py` owns the forward-only `0013 -> 0014 ->
0015` flow under a PostgreSQL advisory lock. It verifies M4/M5 markers, rejects
active execution, creates deterministic Jobs and exact reservations, initializes
recovery state, runs job/quota/audit/stream/recovery probes, persists resumable
migration/domain receipts, and lets `0015` write the final marker last.

Revision `0013` cannot use the Task 16 database audit table because that table is
created by `0014`. The bounded exception is explicit and fail-closed:

- `backup-db --pre-m6-cutover` is accepted only at exact `0013` and only when
  M6 audit/recovery/tombstone/sequence relations are absent;
- archive publication and a sibling `0600` no-clobber commit receipt settle as
  one operation; receipt failure removes the invocation-owned archive;
- `app/recovery/pre_cutover_backup.py` owns the fd/dev/inode publication,
  file/directory fsync, identity-safe cleanup, cancellation settling, and shared
  commit verification instead of expanding the existing Task 16 archive module;
- the commit receipt and later operator restore attestation use separate,
  archive-ID-bound HKDF keys/domains rather than the raw backup base key;
- attestation re-authenticates the complete archive and commit receipt, binds the
  current installation/source fingerprint, and uses the same no-clobber durable
  external proof writer;
- immediately after `0014`, migration records that committed archive exactly
  once through `TrustedOperationAuditSink.backup_created()` before finalization.

The backup/attestation tests include write, directory-fsync, unlink, replacement,
no-clobber, tamper, wrong-source, and cancellation-settle failures.

## Process and operator contracts

- `make dev/start` starts Gateway and required Worker independently; Scheduler is
  started only when `scheduler.enabled=true`.
- Startup failure recursively terminates only PIDs created by that invocation.
- Production and development Compose define required Worker plus optional
  `scheduler` profile; Worker/Scheduler publish no inherited ports and have
  independent process health checks. Docker launch scripts add the profile only
  when configuration enables it.
- System-admin readiness returns only role, fresh Worker count/capacity/oldest
  heartbeat age, aggregate Scheduler ownership, and cutover state. It never
  exposes PID, hostname, lock key, URL, or token.
- `reconcile-usage --dry-run` is aggregate-only across projects; execute requires
  one explicit project while retaining identifier-free output.

## Fresh verification

```text
Task 18 real PostgreSQL migration/process/admin API:
23 passed in 9.24s, 0 skipped

Task 16 original 98-test affected combination plus new archive coverage:
101 passed in 0.86s

Task 16 plus pre-M6 commit and attestation regression:
111 passed in 0.92s

Task 17 real PostgreSQL restore/purge affected regression:
25 passed in 7.58s, 0 skipped

M5 real PostgreSQL migration compatibility:
7 passed in 3.48s, 0 skipped

check-db / doctor / setup:
112 passed, 1 explicit environment-dependent skip in 0.70s

Root Make/Compose/shell/stub contracts:
5 passed in 1.73s

Production and development Compose:
docker compose ... config -q --no-env-resolution (both passed)

Shell syntax:
bash -n scripts/serve.sh scripts/deploy.sh scripts/docker.sh (passed)

Python static/format gates:
Ruff check: All checks passed
Ruff format --check: 24 files already formatted

Repository whitespace gate:
git diff --check (passed)
```

CLI help smoke passed for `backup_postgres.py --pre-m6-cutover`,
`migrate_reliability.py`, and `reconcile_usage.py` under the Makefile
`PYTHONPATH=.` contract. Documentation now includes the external archive commit,
restore-attestation, dry-run, execute/resume, reconciliation, and process
readiness runbook.

## Scope boundary

Task 19 was not started. No release workflow, multi-process crash/reconnect gate,
frontend static gate, milestone percentage, or M6-complete ledger claim was
added. This report records implementation and fresh gates; independent Task 18
closure review remains a separate acceptance step.
