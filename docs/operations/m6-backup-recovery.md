# M6 backup and recovery

This runbook covers normal final-M6 encrypted backups, the external deletion
journal, restore into a new database, and the separate restore drill required
for operational acceptance. The exact-`0013` backup proof used during M6
migration is a different workflow; follow
`docs/operations/m6-reliability-migration.md` for that boundary.

## Authority and secret prerequisites

Run backup and recovery only as a trusted operator. Set:

- `DATABASE_URL` to the current authoritative source database;
- `DEER_FLOW_BACKUP_KEY` to an independent base64-encoded 32-byte key;
- `DEER_FLOW_RECOVERY_JOURNAL_KEY` to a different independent base64-encoded
  32-byte key;
- `DEER_FLOW_AUDIT_ACTIVE_KEY_ID` and `DEER_FLOW_AUDIT_KEYRING_JSON` to the
  same retained audit key set used by Gateway, Worker, and Scheduler;
- `AUTH_JWT_SECRET`, or a pre-existing safe `DEER_FLOW_HOME/.jwt_secret`.

Backup, journal, Auth, audit, credential, and database-password material must
all be distinct. Keep keys in the deployment secret manager. Archive and
journal paths must be outside this repository, non-symlink, operator-owned,
and permissioned `0700` for directories and `0600` for files. The database
role must be able to read `pg_control_system()` and the source schema. Restore
also requires administrator authority to create and drop the exact new target.

## Create and retain a final-M6 backup

```bash
make check-db
make backup-db ARGS="--output /secure/deerflow/backups"
```

The command exports one read-only repeatable-read snapshot and passes that
snapshot to fixed `pg_dump --format=custom --no-owner --no-acl` arguments. It
publishes only after complete archive authentication and the trusted
`backup.created` audit transaction commit. Record the public `archive_id`,
schema revision, chunk count, and truncated checksum in the operator ledger;
never copy keys, database URLs, private identifiers, or archive plaintext into
that ledger.

A command failure is not a usable backup. Preserve an already audited archive
after a later process-cleanup error, but do not adopt a staging path or partial
directory. Rotation may remove an old backup key only after every retained
archive encrypted under it has expired or been re-encrypted and drilled.

## External deletion journal

Retention purge appends an authenticated, encrypted, hash-chained tombstone to
the external journal and fsyncs it before the PostgreSQL physical-delete
transaction may commit. The database singleton anchors the same journal ID,
source installation identity, committed sequence, and full head digest.

Never truncate, reorder, concatenate, edit, replace, or recreate the journal
for an existing source. If the journal is missing, has a sequence gap, does not
match the source anchor, or cannot be fsynced, stop physical purge and recovery.
Restore must authenticate the full prefix and replays the frozen suffix after
the archive high-watermark so data already physically purged cannot reappear.

## Restore into a new database

Restore is never in place. The target must not exist, must differ from the
source, and must be named exactly
`deerflow_restore_<pid>_<32-lowercase-hex>`.

```bash
make restore-db ARGS="--archive /secure/deerflow/backups/<archive_id>.dfba --target-url postgresql://operator@db/deerflow_restore_1234_0123456789abcdef0123456789abcdef --journal /secure/deerflow/recovery/tombstones.jsonl --execute"
```

The command authenticates the complete archive, freezes source/journal
authority, creates the target, runs `pg_restore --exit-on-error --no-owner
--no-acl`, replays the exact journal suffix, executes schema/isolation and
M1–M6 probes, removes sensitive workspace files by captured identity, and only
then writes a restore proof bound to archive, source, journal ID, final
sequence, and final head digest. It never changes `DATABASE_URL`, starts an
application process, overwrites a database, or switches traffic.

After a verified result, independently inspect the public proof and run
`make check-db` with `DATABASE_URL` pointed at the restored target. A production
traffic switch is a separate reviewed operator action. Keep the old source
read-only and retained until the new database has passed application smoke and
the rollback window has closed.

## Separate restore drill

Run a restore drill on the schedule required by the deployment's recovery
policy and after any backup/journal/key/process change:

```bash
make drill-restore ARGS="--archive /secure/deerflow/backups/<archive_id>.dfba --journal /secure/deerflow/recovery/tombstones.jsonl"
```

The drill uses the same authentication, journal replay, sensitive cleanup, and
M1–M6 probes as restore. It generates one random new database and drops only
that invocation-owned target after the same `Restorer` hands off a verified
ownership token. A pre-existing or racing database and a forged/stale result
never authorize `DROP`. Retain the public drill proof metadata and timestamp;
do not retain target credentials or private restored content.

## Failure decisions

| Failure | Required action |
| --- | --- |
| `BACKUP_FAILED` | No backup exists unless a prior audited archive is independently present. Correct source authority, key separation, path permissions, or `pg_dump` prerequisites and retry with a new archive ID. |
| `RESTORE_EXECUTE_REQUIRED` | Review the target and add `--execute`; do not work around the explicit execution boundary. |
| `RESTORE_FAILED` before target creation | Leave source and journal unchanged; correct authentication, source anchor, target naming, or administrator authority. |
| `RESTORE_FAILED` after target creation | The command must remove only its invocation-owned target and workspace. Verify absence; if cleanup cannot be proven, quarantine that target and investigate before retrying. |
| Archive tamper, wrong key, journal gap, source mismatch, or proof mismatch | Treat the artifact set as unusable. Never suppress the probe or edit the manifest/journal; select a matching retained key/artifact set or create and drill a new backup. |
| `RESTORE_DRILL_FAILED` | Recovery readiness is not established. Do not delete the source, rotate away required keys, or claim M6 recovery acceptance. Diagnose and repeat a fresh drill. |
| `make check-db` or application smoke fails on a verified target | Do not switch traffic. Repair forward on a new target or create another restore; preserve evidence from the failed target. |

## Forward-only rule

There is **no downgrade** from M6. Never restore over the current database,
manually stamp `0014`/`0015`, edit a restore proof, or point an M5 binary at an
M6 database. Recovery is always authenticated restore to a new database plus
explicit verification and a separate manual traffic switch.
