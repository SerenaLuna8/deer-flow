# M5 Task 16 implementation report

## Scope

- Added `backend/tests/integration/test_m5_automation_migration_postgres.py`.
- Extended `backend/tests/support/m5_automation.py` with a disposable `0011`
  legacy source fixture and schema fingerprint helper.
- Added the migration gate as the exact eighth file in
  `.github/workflows/project-foundation-postgres-tests.yml` and updated the
  Task 15 workflow structure assertion.
- No production migration, revision, runtime, frontend, Task 17, or Task 18
  code changed.

## TDD evidence

The first integration run exposed two test-author SQL errors: PostgreSQL still
resolves a missing relation in an unused `CASE` branch. I fixed the test helper
to query `to_regclass` first, then reran until the suite exercised product
behavior rather than failing on test construction.

The corrected new migration integration suite passed against existing product
behavior (`6 passed`). The release gate still had the intended Task 16 gap:

```text
POSTGRES_TEST_URL=... uv run pytest \
  tests/integration/test_m5_project_automation_postgres.py::test_release_workflow_has_exact_m1_to_m5_gate_after_hard_fail -q

FAILED ... At index 10 diff: '-q' !=
'tests/integration/test_m5_automation_migration_postgres.py'
1 failed
```

This was the valid RED: the hard-fail CI workflow did not run the new eighth
real PostgreSQL gate. The minimal GREEN change appended that exact file before
`-q`; the structure assertion then passed (`1 passed`).

## Integration coverage

All databases are created through `temporary_postgres_database` and are named
`deerflow_test_*`; no application/business database is read or written.

- `0011` dry-run is zero-write, then execute reaches `0012` staging and `0013`
  final schema with exact counts, two complete digest receipts, final
  constraints, complete cutover marker, and execute rerun `noop=True`.
- Fresh empty bootstrap reaches final schema plus the empty-domain complete
  marker without an owner map or migration receipts.
- Invalid, missing, and extra/conflicting owner maps, reuse Thread cross-scope,
  and an unmapped legacy run relation all fail at `0011` without expand tables
  or a cutover marker.
- Source drift is produced with two independent real PostgreSQL sessions: one
  holds a conflicting table lock, execute reaches a real pending
  `ShareRowExclusiveLock`, the legacy writer commits drift, and execute rejects
  the changed fingerprint with no migration rows or marker.
- Staged owner-map drift and a tampered ledger target digest remain at `0012`
  with `migration_ready`, `final_schema_probe_complete=false`, and no cutover.
- Finalize relation drift is produced by moving the referenced reuse Thread to
  another owner after staging. Alembic fails before the first final DDL;
  revision remains `0012`, the complete schema fingerprint is unchanged, the
  legacy `user_id` column remains, and restoring the relation permits a clean
  retry to `0013` and cutover complete.

## Verification

```text
# New migration integration, twice
6 passed in 2.76s
6 passed in 2.86s

# Task 15 workflow structure assertion after the CI edit
1 passed in 0.67s

# Migration unit, CLI, and PostgreSQL schema suites
62 passed in 12.05s

# Exact M1-M5 eight-file PostgreSQL release gate
31 passed in 9.93s

# Full backend lint/format
All checks passed!
1058 files already formatted
```

The PostgreSQL runs used:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55435/postgres
```

Every focused and combined PostgreSQL run completed with zero skips.

## Self-review

- The fixture retains the existing test administrator safety guard and checks
  the generated disposable database prefix.
- Failure tests assert revision, marker, receipt, and schema state instead of
  accepting an exception alone.
- The race test uses real database locks and independent sessions, not mocks or
  timing-only sleeps.
- The finalization fingerprint covers columns, constraints, indexes, and
  triggers visible to `0013`; retry is demonstrated after repairing only the
  invalid external relation.
- CI order remains hard-fail for missing `POSTGRES_TEST_URL` before pytest, and
  its pytest token list is exact.
