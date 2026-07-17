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
