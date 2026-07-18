# M6 reliability migration

This runbook moves a completed M5 database from
`0013_project_automation_finalize` through the forward-only M6 cutover. The
command reaches `0014`, writes resumable evidence, runs every probe, and only
then applies `0015_project_reliability_finalize` and the singleton marker.

## Safety boundary

Stop every Gateway, Worker, Scheduler, channel, and embedded writer before
execute. A running Run or launching/running Automation occurrence fails before
expand DDL. Pending work is not replayed: it is attached to a durable queued Job
and receives the exact quota reservation identity used by online release.

M4 and M5 cutover markers must already be complete. Set `DATABASE_URL`, the
independent base64 32-byte `DEER_FLOW_BACKUP_KEY`, and the deployment audit HMAC
keyring. Keep proofs, archives, and keys outside this repository.

## Authenticate backup evidence

Create the exact-0013 Task16-format archive in explicit pre-M6 mode. Because
0013 has no M6 audit ledger yet, archive publication and a sibling `0600`
no-clobber HMAC commit receipt are one operation: the receipt is derived from
the backup key under a separate HKDF domain and binds manifest digest, source,
revision, event high-watermark, zero tombstone high-watermark, and table count.
Receipt failure removes the archive. Any M6 recovery/audit table already being
present makes this mode fail closed; 0014 and later use normal Task16 database
audit instead.

```bash
make backup-db ARGS="--output /secure/m6 --pre-m6-cutover"
# Use the returned archive_id for both sibling files below.
make migrate-reliability ARGS="--attest-backup --archive /secure/m6/<archive_id>.dfba --backup-commit /secure/m6/<archive_id>.dfba.commit.json --proof-output /secure/m6/pre-cutover-proof.json --restore-verified"
```

Before attestation, restore the authenticated archive into an isolated
operator-owned PostgreSQL target and verify the 0013 schema and source rows.
`--restore-verified` is the explicit operator assertion for that completed
rehearsal; the normal `make drill-restore` command targets the final M6 recovery
schema and is not a substitute at this pre-expand boundary. Attestation fully
authenticates the archive and its external commit again and binds them to the
current source fingerprint. Changing the archive, commit receipt, proof,
database installation, or M5 source rows makes execute fail before `0014`.

## Dry-run and execute

Dry-run performs only reads. It emits aggregate counts and a truncated checksum
without creating catalog objects, controls, counters, ledgers, or sequences.

```bash
make migrate-reliability ARGS="--dry-run --backup-proof /secure/m6/pre-cutover-proof.json"
make migrate-reliability ARGS="--execute --maintenance-acknowledged --backup-proof /secure/m6/pre-cutover-proof.json"
make check-db
```

Execute is resumable. A failed probe leaves revision `0014`; correct the public
error and rerun the same execute command. Exact quota rows, durable Jobs, the
migration run, and domain receipts are idempotent. Never manually stamp `0015`,
edit receipts, or substitute aggregate quota adjustments: member removal, file
deletion, and Run terminalization require their exact migrated reservation.
After expand creates the audit ledger, execute records the committed pre-M6
archive exactly once through `TrustedOperationAuditSink.backup_created()` before
any final marker can be written.

## Required cutover sequence

Do not reorder or combine these checkpoints:

1. While the M5 source is still online, create the exact-`0013` encrypted
   archive and externally committed sibling receipt.
2. Restore that archive into an isolated operator-owned database, verify the
   `0013` schema and source rows, then create the authenticated backup proof
   with `--restore-verified`.
3. Run `migrate-reliability --dry-run` and retain its aggregate report. Any
   source fingerprint or count change invalidates the review; repeat backup,
   restore rehearsal, attestation, and dry-run.
4. Enter the maintenance window. Stop Gateway, Worker, Scheduler, IM/channel
   consumers, embedded/TUI processes, and every other database writer. Keep
   them stopped until post-cutover probes pass.
5. Run the execute command with `--maintenance-acknowledged` and the reviewed
   backup proof. A success must report revision
   `0015_project_reliability_finalize` and `cutover_complete=true`.
6. Run `make check-db`, then the fixed M1–M6 PostgreSQL release gate:

   ```bash
   make check-db
   POSTGRES_TEST_URL="$POSTGRES_TEST_URL" make test-project-foundation-postgres
   ```

   `POSTGRES_TEST_URL` must be an isolated administrator URL that may create
   and drop only random `deerflow_test_*` and `deerflow_restore_*` databases;
   never point it at the migrated business database. The gate must report zero
   skipped tests.
7. Start Gateway, Worker, and the optional Scheduler. Confirm aggregate
   readiness is open, at least one fresh Worker is present, and an enabled
   Scheduler owns its session lock. Do not infer readiness from process exit
   status alone.
8. Create a normal final-M6 backup and perform a separate restore drill using
   `docs/operations/m6-backup-recovery.md`. The drill creates and drops its own
   random target; it is independent of the pre-`0014` restore rehearsal.

## Failure decisions and forward-only boundary

| Failure point | Required action |
| --- | --- |
| Archive, receipt, isolated rehearsal, attestation, or dry-run fails | Do not enter maintenance. Correct the external evidence and restart at step 1. |
| Execute rejects active work or source drift before `0014` | Keep the M5 database unchanged, reconcile or stop the reported writer, create new backup evidence, and repeat dry-run. |
| Execute stops at `0014` | Keep every writer stopped. Diagnose the public error, preserve the archive/proof and migration receipts, then rerun the same execute command. Do not edit ledger rows or stamp a revision. |
| `make check-db` or an M1–M6 probe fails after `0015` | Keep traffic closed. Preserve evidence and repair forward on the migrated database, or restore the authenticated archive into a new database and rehearse the journal suffix before a manual traffic switch. |
| Worker/Scheduler readiness fails | Leave admission closed; repair role configuration, audit keyring parity, Worker freshness, or Scheduler ownership before opening traffic. |
| Separate restore drill fails | Do not claim recovery readiness or M6 operational acceptance. Preserve the source and external journal, diagnose, create a new backup if required, and repeat the drill. |

There is **no downgrade** after `0014` or `0015`. Never run a destructive
Alembic downgrade, point the old application at the expanded/final database,
or re-enable legacy Gateway execution. Recovery means forward repair or an
authenticated restore into a new database followed by a separate, manual
traffic switch. It never means in-place overwrite.

## Process topology and readiness

`make dev` and `make start` launch Gateway and Worker independently, plus
Scheduler only when `scheduler.enabled=true`. Compose has the same required
roles; Scheduler uses the optional `scheduler` profile. A required-role startup
failure terminates only processes created by that launcher invocation.

System-admin readiness returns only role, fresh Worker count/capacity/oldest
heartbeat age, aggregate Scheduler ownership, and cutover state. It never
returns hostnames, PIDs, lock keys, database URLs, or tokens. A missing Worker is
not ready; disabled Scheduler is legal; enabled-unowned or ownership-lost
Scheduler fails closed.

Quota reconciliation preview is all-project aggregate-only. Execute requires one
explicit project and still emits no project/resource identifier:

```bash
make reconcile-usage ARGS="--dry-run"
make reconcile-usage ARGS="--execute --project-id <uuid>"
```
