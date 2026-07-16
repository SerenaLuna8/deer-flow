# M5 Task 10 Implementation Report — Explicit Legacy Automation Migration

## Status and scope

- Date: 2026-07-16
- Baseline: `b1ec884f4292e0027f3120fa8e6698816e1e8080`
- Branch: `codex/m5-project-automation`
- Commit subject: `feat: migrate legacy automations`
- Scope: Task 10 backend migration and operations wiring only
- Explicit exclusions: frontend, Task 11 documentation/release closure, and `.superpowers/sdd/progress.md`

Task 10 adds the explicit `migrate-automations` dry-run/execute workflow, safe setup/check/doctor
integration, staged receipts, failure recovery, and real PostgreSQL coverage. It does not claim that
the overall M5 milestone is complete.

## Delivered contract

- Added `make migrate-automations ARGS="--dry-run|--execute --owner-map <json> --backup-dir <path>"`
  at the root and backend Makefiles. Root help states that dry-run must precede execute.
- The strict owner map is exactly one legacy owner UUID to one active non-Viewer project plus one
  explicit executable Agent fallback for fresh-thread tasks. Reuse-thread tasks derive their Agent
  only from the exact scoped M4 Thread.
- Dry-run accepts 0011/0012/0013, validates M4 cutover and all source authority/relations, writes
  nothing, and returns only counts, bounded status aggregates, a truncated source hash, and safe
  mode/cutover flags.
- An unfinished execute accepts 0011 or 0012, requires an existing non-empty operator
  backup/restore proof file, and never presents the CLI as the database backup system.
- The staged path upgrades to 0012, records a stable migration run identity, writes and verifies
  separate `scheduled_tasks` and `scheduled_task_runs` ledgers, runs exact scope/Agent/Thread/Run
  probes, records `migration_ready`, lets 0013 fail before destructive DDL if any receipt or relation
  is invalid, and completes the cutover singleton last.
- Legacy fresh task Thread pointers are cleared. A skipped pre-admission occurrence with a synthetic
  nonexistent Thread is retained with null Thread/Run pointers; every other non-null historical
  Thread/Run pointer must resolve through the exact M4 project+owner relation.
- Completed cutover reruns are safe no-ops with accurate counts. Partial domain-ledger reruns resume
  only when source fingerprint, owner-map digest, completed ledgers, and target values still agree.
- `check-db` requires the three M5 control tables and reports only `ready`,
  `migration_required`, or `unavailable`; `doctor` and `setup-db` preserve only a redacted migration
  instruction and never echo prompt, title, map values, IDs, credentials, or database URLs.

## Strict TDD evidence

The initial RED command was:

```bash
cd backend
POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55435/postgres \
  uv run pytest \
  tests/test_automation_migration.py \
  tests/test_automation_migration_cli.py \
  tests/test_setup_postgres.py \
  tests/test_check_postgres.py \
  tests/test_doctor.py -q
```

Collection failed because `scripts.migrate_automations` did not exist. After the first implementation,
the real PostgreSQL test exposed a schema contradiction: 0012 still kept
`scheduled_task_runs.thread_id` NOT NULL, so the specified skipped pre-admission normalization could
not be written. The 0012 expand migration now relaxes that column during staging; 0013 retains the
intended final nullable contract.

Subsequent RED/GREEN self-review cycles found and fixed:

1. A newly created, empty ledger table was incorrectly treated as evidence that every target domain
   had already been written.
2. A deterministic pointer/status normalization changed the raw source row during staging and could
   make a legitimate interrupted rerun look like source drift; the receipt fingerprint now hashes
   the normalized source semantics while the dry-run inventory still hashes every source field.
3. A task-domain receipt followed by interruption incorrectly compared the not-yet-written run
   domain as target tamper; compatibility checks now cover only domains with completed ledgers.
4. Changing the owner map after the migration run receipt but before the first domain ledger could
   start a second run; any existing run identity now pins source fingerprint and map digest first.
5. Legacy task-level `last_thread_id`/`last_run_id` pointers were not checked; they now require the
   same exact M4 composite authority as occurrence history before expand DDL.

Each repair has an isolated real PostgreSQL regression test.

## Verification

All PostgreSQL commands used the explicitly supplied local server on port 55435. Fixtures created
and dropped random `deerflow_test_*` databases; no business database was used.

| Gate | Result |
| --- | ---: |
| Final migration + CLI suite | 17 passed |
| Final Task 10 migration + setup/check/doctor operations suite | 127 passed |
| M5 Tasks 1–10 plus persistence/default-project bootstrap regression gate | 255 passed |
| Final no-op count end-to-end PostgreSQL regression | 1 passed |
| Full backend Ruff check | all checks passed |
| Full backend Ruff format check | 1051 files already formatted |
| Compileall for backend app/packages/scripts and Task 10 tests | passed |
| `git diff --check` | passed |

The full `pytest tests/` release gate and frontend gates are intentionally left to Task 11, matching
the explicit instruction not to perform Task 11 in this change.

## Self-review conclusion

- Reports and exceptions exposed by the CLI contain no private source value or identifier.
- Dry-run performs no DDL, target, ledger, marker, directory, or proof-file write.
- Invalid Viewer/project/Agent/reuse scope, unsupported status, orphan task/run history, and missing
  backup proof all fail before expand writes.
- Ledger receipts cover both domains, target digests, source/target row counts, and stable run/map
  identity. The marker is never completed before 0013's final probes.
- Completed, pre-ledger, one-domain, two-domain, target-tampered, source-changed, and empty-install
  rerun states are covered.
- `.superpowers/sdd/progress.md` is unchanged.

No blocking Task 10 concern remains. Operational prose and milestone-wide release claims remain
deliberately deferred to Task 11.
