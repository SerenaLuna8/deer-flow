# M7 backup and recovery

This runbook covers encrypted backups of the M7 final baseline, the external deletion
journal, restore into a new database, and the separate restore drill required
for operational acceptance. Every usable archive must use schema version 7 and revision
`0001_project_saas_baseline`.

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

## Create and retain an M7 backup

```bash
make check-db
make backup-db ARGS="--output /secure/deerflow/backups"
```

The command first requires the exact M7 baseline revision, canonical schema
digest, and allowed root-object inventory. It then exports one read-only
repeatable-read snapshot, verifies the exact root inventory, revision, and
canonical catalog again inside that same transaction, and passes the snapshot to fixed
`pg_dump --format=custom --no-owner --no-acl` arguments. It publishes only
after complete archive authentication and the trusted
`backup.created` audit transaction commit. Record the public `archive_id`,
archive schema version, schema revision, canonical schema digest, chunk count,
and truncated checksum in the operator ledger; never copy keys, database URLs,
private identifiers, or archive plaintext into that ledger.

New archives use archive schema version `7` and revision
`0001_project_saas_baseline`. The writer and reader share one strict frozen
manifest model: unknown fields, scalar coercion, and non-M7 constants are
rejected. Chunk AAD binds the archive ID, archive schema
version, schema revision, canonical schema digest, source installation ID, and
chunk index. A fully authenticated pre-M7 archive fails closed with
`UNSUPPORTED_ARCHIVE_SCHEMA`; changing and re-signing only manifest schema
fields cannot bypass chunk authentication.

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

The command authenticates the complete archive and rejects pre-M7 schema
versions before target-name resolution, existence checks, creation, or DDL. It
then requires the source itself to remain the exact M7 baseline, freezes
source/journal authority, creates the target, runs `pg_restore --exit-on-error --no-owner
--no-acl`, replays the exact journal suffix, executes schema/isolation and
M7 exact-schema probes, removes sensitive workspace files by captured identity, and only
then writes a restore proof bound to archive schema version and digest, source,
journal ID, final sequence, and final head digest. PostgreSQL constraints require
the exact M7 version, revision, and canonical digest. Catalog hashing breaks the
digest self-reference only for the complete correct constraint containing the
current digest; replacing it with another valid lower-hex value remains visible
as schema drift. It never changes `DATABASE_URL`, starts an
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
M7 exact-schema probes as restore. It generates one random new database and drops only
that invocation-owned target after the same `Restorer` hands off a verified
ownership token. A pre-existing or racing database and a forged/stale result
never authorize `DROP`. Retain the public drill proof metadata and timestamp;
do not retain target credentials or private restored content.

## Failure decisions

| Failure | Required action |
| --- | --- |
| `BACKUP_FAILED` | No backup exists unless a prior audited archive is independently present. Correct source authority, key separation, path permissions, or `pg_dump` prerequisites and retry with a new archive ID. |
| `UNSUPPORTED_ARCHIVE_SCHEMA` | The authenticated artifact is not an M7 version-7 archive with the canonical revision and digest. Do not create a target or attempt compatibility restore; create and drill a new M7 backup. |
| `RESTORE_EXECUTE_REQUIRED` | Review the target and add `--execute`; do not work around the explicit execution boundary. |
| `RESTORE_FAILED` before target creation | Leave source and journal unchanged; correct authentication, source anchor, target naming, or administrator authority. |
| `RESTORE_FAILED` after target creation | The command must remove only its invocation-owned target and workspace. Verify absence; if cleanup cannot be proven, quarantine that target and investigate before retrying. |
| Archive tamper, wrong key, journal gap, source mismatch, or proof mismatch | Treat the artifact set as unusable. Never suppress the probe or edit the manifest/journal; select a matching retained key/artifact set or create and drill a new backup. |
| `RESTORE_DRILL_FAILED` | Recovery readiness is not established. Do not delete the source, rotate away required keys, or claim recovery acceptance. Diagnose and repeat a fresh drill. |
| `make check-db` or application smoke fails on a verified target | Do not switch traffic. Repair forward on a new target or create another restore; preserve evidence from the failed target. |

## Forward-only rule

There is **no downgrade** from the M7 baseline. Never restore over the current database,
manually stamp a revision, or edit a restore proof. Recovery is always authenticated restore to a new database plus
explicit verification and a separate manual traffic switch.
