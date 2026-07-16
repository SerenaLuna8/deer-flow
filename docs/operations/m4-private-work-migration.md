# M4 project private-work migration runbook

This runbook covers the runnable-first cutover from legacy PostgreSQL private work to the
project-and-owner-scoped M4 schema. It is an operator procedure, not an automatic startup
migration. Replace every example URL, UUID, and path with values from the target environment.

## Supported migration boundary

The current migrator covers legacy PostgreSQL Thread, Run, run-event, feedback, and checkpoint
metadata markers. It requires a direct owner UUID to active project UUID map and never guesses a
default, recent, or unique project. If legacy filesystem, Memory, file/artifact, or connection
sources are non-empty, preflight fails before finalization; migrate those sources with a reviewed
future procedure or keep the installation on the legacy writer.

This command owns revisions only through the M4 final revision when legacy Automation data exists.
After the M4 marker commits, any row in `scheduled_tasks` or `scheduled_task_runs` makes execute
return successfully at `0011`, preserving that domain byte-for-byte for the M5 Automation
migrator. Only when both tables are empty may the command continue the fresh-install bootstrap to
the current head.

`--backup-dir` is a reserved CLI contract in this runnable-first version. The script does not
write a backup there and does not consume `DEER_FLOW_M4_BACKUP_KEY`. The database backup and its
restore proof remain operator-owned and must exist outside the repository before execute.

## 0. Legacy SQLite private rows: stage at 0007 first

Skip this section when the private rows are already in a versioned PostgreSQL database at 0007.
When `threads_meta`, `runs`, `run_events`, or `feedback` still live in legacy SQLite, do **not**
run normal `make setup-db` or `make migrate-db` first: both target the final schema, where the
frozen owner-only rows cannot be inserted.

Freeze every SQLite writer, choose a dedicated new PostgreSQL database (or an existing database
already verified at 0007), and run:

```bash
export POSTGRES_ADMIN_URL='postgresql+asyncpg://postgres:<encoded-password>@127.0.0.1:5432/postgres'
export DATABASE_URL='postgresql+asyncpg://deerflow:<encoded-password>@127.0.0.1:5432/deerflow_m4_cutover'
make setup-m4-migration-db
make migrate-sqlite ARGS="--m4-staging-target --source /path/legacy.db --backup-dir /secure/sqlite-backups --dry-run"
make migrate-sqlite ARGS="--m4-staging-target --source /path/legacy.db --backup-dir /secure/sqlite-backups"
```

`setup-m4-migration-db` creates the named database when needed, applies the application migrations
only through `0007_project_shared_assets`, initializes the LangGraph PostgreSQL tables, and rejects
a non-empty unversioned database or any revision other than 0007. The importer reflects and
validates the actual 0007 target tables against its frozen source contract before any row write;
it still performs ordered cross-source conflict checks, verified backups, semantic read-back, and
ledger writes.

Before continuing, establish or verify the M2/M3 authority already required by M4: every imported
owner must have one active membership in the target project selected by the owner map, and each
legacy `assistant_id` must resolve to a published system or project Agent. Use the deployment's
reviewed M2/M3 provisioning/cutover procedure; do not invent a project or Agent mapping in the M4
command. Then continue with section 1 against the same 0007 database. The execute in section 6 is
the only command that advances that database through 0008 and the final marker.

## 1. Online dry-run

Dry-run is read-only and may run while the service is online. It calculates redacted counts and a
stable source hash, checks the owner map and active memberships, and performs no schema upgrade,
ledger write, marker write, or backup-directory creation.

```bash
export DATABASE_URL='postgresql+asyncpg://m4_operator:<encoded-password>@127.0.0.1:5432/deerflow'
make migrate-private-work ARGS="--dry-run --owner-map /secure/private-work-owner-map.json --backup-dir /secure/m4-backups"
```

Save the command exit code, redacted JSON report, source hash, and review approval. A later execute
must use the same reviewed owner map. Re-run dry-run immediately before the maintenance window if
legacy data may have changed.

## 2. Maintenance window and writer stop

Do not execute while any private-work writer is live. Stop and verify the absence of:

- Gateway/API processes, including every replica;
- Scheduler/background automation processes;
- IM channel workers and inbound webhook consumers;
- embedded Python clients, TUI sessions, migration helpers, and ad-hoc scripts that can create or
  mutate Threads, Runs, Memory, files, artifacts, or connections.

The read-only dry-run does not require this stop. Execute, finalization, and marker decisions do.

## 3. Operator database backup proof

Create a consistent PostgreSQL backup using the deployment's normal backup system. Record at
least the target database identity, backup object/locator, creation time, tool/version, checksum
or provider integrity result, and a successful restore rehearsal or documented restore command.
Keep credentials and backup material outside the repository and application logs.

The M4 command validates migration sources but does not create or restore this backup. Do not
continue from dry-run to execute without independently reviewing the proof.

## 4. Owner map and active membership verification

The owner map is a JSON object containing only UUID-to-UUID entries:

```json
{
  "11111111-1111-4111-8111-111111111111": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "22222222-2222-4222-8222-222222222222": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
}
```

The key is the legacy owner UUID; the value is the one active target project UUID. Before execute,
confirm every owner found by dry-run appears exactly once, the project is active and not suspended
or pending deletion, and the same owner has an active membership in that project. The migrator
performs the authoritative database check and fails closed on missing or inactive mappings.

Store the map with least-privilege file permissions. It contains identifiers rather than secrets,
but it is still operationally sensitive tenant-routing data.

## 5. `DEER_FLOW_M4_BACKUP_KEY` handling

If a future reviewed migrator build consumes `DEER_FLOW_M4_BACKUP_KEY`, inject a fresh high-entropy
value from the deployment secret manager into the migration process only; never put it in YAML,
shell history, command arguments, logs, the owner map, or version control. Unset it after the
maintenance window.

```bash
export DEER_FLOW_M4_BACKUP_KEY="$(security find-generic-password -w -s deerflow-m4-backup-key)"
```

For the current runnable-first CLI this variable is deliberately unused and does not replace the
operator PostgreSQL backup proof in step 3. Do not claim that `--backup-dir` is encrypted or even
written by this version.

## 6. Execute, health check, and M4 probes

With all writers stopped and the backup proof approved, run a final dry-run, then execute:

```bash
make migrate-private-work ARGS="--dry-run --owner-map /secure/private-work-owner-map.json --backup-dir /secure/m4-backups"
make migrate-private-work ARGS="--execute --owner-map /secure/private-work-owner-map.json --backup-dir /secure/m4-backups"
make check-db
```

Execute advances through `0008_project_private_work_expand`, writes the per-domain idempotency
ledger, satisfies `0009_project_private_work_finalize`, upgrades through `0010` and `0011`, and
finally writes `private_work_cutover_state.stage=cutover_complete`. A completed cutover rerun is a
no-op. If either legacy Automation table contains rows, the successful report remains at `0011`;
`make check-db` may therefore correctly report that the separate M5 Automation migration is still
required. Keep writers stopped and follow `docs/operations/m5-automation-migration.md`. Do not run
a generic head upgrade across those rows. An empty Automation domain may bootstrap directly to
head.

Run the fixed real-PostgreSQL probes against a disposable clone or the approved test environment,
never by pointing pytest at the production database:

```bash
cd backend
POSTGRES_TEST_URL='postgresql+asyncpg://postgres@127.0.0.1:5432/postgres' \
  uv run pytest \
  tests/integration/test_m1_postgres_cutover.py \
  tests/integration/test_project_isolation_postgres.py \
  tests/integration/test_m2_project_governance_postgres.py \
  tests/integration/test_m3_shared_assets_postgres.py \
  tests/integration/test_m4_private_work_postgres.py \
  tests/integration/test_m4_private_work_migration_postgres.py -q
```

Each test creates a random `deerflow_test_*` database and drops it afterward. Any skip is a failed
release gate.

## 7. Failure before revision `0009`

Keep all writers stopped. A failure at `0007`/`0008` or while staging is expected to be safely
retryable after fixing the reported dependency, owner-map, or source issue. Do not edit ledger or
target rows manually. Re-run dry-run, verify the source fingerprint is unchanged, then re-run
execute with the identical reviewed owner map. The stable per-domain ledger converges completed
work and rejects source drift or target tampering.

If the source fingerprint changed, stop and produce a new inventory, owner-map review, and backup
proof. If safe forward repair cannot be established, restore the complete operator backup; do not
attempt a destructive Alembic downgrade.

## 8. Failure after revision `0009` but before the marker

This is a cutover-decision incident, not a normal retry. Keep every legacy and project writer
stopped. Do not start Gateway: final constraints may be active while the singleton marker is not.
The current CLI intentionally rejects a marker-incomplete database already beyond `0008`, so do
not loop execute or hand-edit the marker.

Capture the Alembic revision, migration run/ledger rows, source fingerprint, check-db output, and
database logs. Choose one reviewed path:

1. forward-repair the exact failed revision/probe and complete the marker using a dedicated,
   tested recovery change; or
2. restore the full operator database backup and verify the legacy revision and source bytes before
   reopening legacy writers.

Never mix a partial target with newly resumed legacy writes.

## 9. Post-cutover writer policy

After `cutover_complete`, start only the M4-aware Gateway and workers. Do not restart an older
Gateway, embedded client, TUI, channel worker, or script that writes through legacy Thread, Run,
Memory, connection, upload, artifact, or shared `start_run` paths. Those HTTP/runtime boundaries
must return `409 PRIVATE_WORK_CUTOVER` and must never read project-scoped rows.

Smoke-test readiness, one permitted owner flow, Viewer read/own-delete behavior, and a cross-owner
not-found response before ending the maintenance window.

## 10. Explicit non-promises

M4 does not provide a general backup/restore product, point-in-time recovery workflow, deletion
tombstone replay, disaster-recovery drill, or physical retention purge. It does not migrate
arbitrary non-empty filesystem/Memory/connection sources. Project Automation uses its separate M5
migration and cutover; independent Workers, durable SSE, quotas, audit, and general backup recovery
remain M6, with M7 legacy cleanup and M8 full release acceptance still separate. The system must
not be described as a complete multi-user SaaS until those gates are delivered.
