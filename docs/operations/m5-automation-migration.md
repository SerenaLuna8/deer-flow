# M5 Project Automation Migration Runbook

This runbook cuts legacy PostgreSQL scheduled tasks and runs over to the final
project/owner-scoped M5 Automation schema. It is a trusted maintenance operation,
not an online application path or a general backup/restore product.

## Preconditions and safety boundary

- Schedule a maintenance window and block new traffic.
- Stop every writer: Gateway, embedded Scheduler, channel workers, provisioner-side
  callers, maintenance scripts, and any other process that can create or update
  scheduled tasks, occurrences, Threads, runs, projects, memberships, or assets.
- Keep PostgreSQL available only to the migration operator and monitoring needed for
  the window. Do not point the command at an unverified database.
- Create and authenticate a full database backup outside this repository. Preserve
  immutable backup metadata, restore instructions, and a successful restore proof in
  `/secure/m5-backup-proof`; the migration CLI validates proof but is not the backup
  engine.
- Prepare the owner/project/Agent mapping at `/secure/m5-owner-map.json` with least
  privilege. Never print or copy the map into tickets, chat, CI output, or logs.
- Confirm M4 private-work cutover is ready and the source revision is supported.

Examples and operator notes must contain only aggregate counts, status buckets, and
truncated hashes. Never log Automation titles or prompts, owner-map contents, or full
user, project, task, Thread, run, Agent, membership, or migration identifiers.

## Dry-run

With all writers stopped and the external backup proof present, run:

```bash
make migrate-automations ARGS="--dry-run --owner-map /secure/m5-owner-map.json --backup-dir /secure/m5-backup-proof"
```

Review the fail-before-DDL preflight, mapping coverage, aggregate source/target counts,
scope and Agent resolution, Thread/run composite relationships, and truncated digest.
A dry-run error is a stop condition: make no schema changes, repair the source data or
map offline, refresh the backup proof when required, and repeat dry-run.

## Execute and verify

Execute only after the dry-run for the exact source and map is clean:

```bash
make migrate-automations ARGS="--execute --owner-map /secure/m5-owner-map.json --backup-dir /secure/m5-backup-proof"
make check-db
```

Keep writers stopped while running the full M1-M5 real-PostgreSQL release probes,
including project foundation/isolation, governance, shared assets, private work,
private-work migration, project Automation, and Automation migration. Also run the
Backend, blocking-I/O, Frontend account/project isolation, normal browser, and static
browser gates documented in the M5 implementation plan. Reopen traffic only after the
final revision, migration ledger, cutover marker, readiness, aggregate counts, and all
probes are clean.

## Failure recovery

- Failure before DDL or before a committed batch leaves the final schema unopened.
  Preserve the redacted error and aggregate evidence, repair the cause, and rerun
  dry-run before another execute attempt.
- A failed transaction is rolled back by PostgreSQL. Do not manually stamp Alembic,
  edit the ledger, forge a cutover marker, guess a default project, or bypass scope
  validation.
- If an execute attempt committed resumable ledger state but did not complete cutover,
  keep every writer stopped and follow the CLI's idempotent resume path only after a
  new clean dry-run and backup check.
- If validation after execute fails, do not reopen traffic. Capture only redacted
  aggregates, retain the database and external backup proof, and diagnose against the
  failed probe.

## Rollback boundary

The cutover is a forward migration. Before the final cutover marker, recover by fixing
the source/map and rerunning the idempotent migration or, when required, restoring the
externally authenticated backup into a separately verified database. After the final
constraints and cutover marker are committed, do not downgrade in place or re-enable
legacy writes: restore the full external backup as an operator-controlled incident
procedure, or ship a reviewed forward repair. General point-in-time recovery, disaster
recovery automation, and deletion-tombstone replay remain M6 work.
