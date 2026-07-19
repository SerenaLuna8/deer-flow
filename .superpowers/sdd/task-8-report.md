# M7 Task 8 Implementation Report — Final Database Baseline

## Scope and outcome

Task 8 resets the PostgreSQL schema history to a single, fresh-install-only M7
baseline. The implementation does not enter Task 9 and does not update
`.superpowers/sdd/progress.md`; independent review remains the next governance
step.

- Alembic now has one revision only:
  `0001_project_saas_baseline.py`, with `down_revision = None` and an explicit
  unsupported downgrade error.
- The revision is static Alembic/SQLAlchemy DDL. It imports no application or
  DeerFlow model modules at migration runtime.
- The baseline installs the 52 final application tables plus final indexes,
  constraints, foreign keys, functions, and triggers. It does not install a
  migration ledger, domain cutover state, migration proof tables, or legacy
  owner/source columns.
- Bootstrap classifies before mutation: an empty schema installs the baseline;
  an exact M7 schema is verified idempotently; an old revision or unknown
  non-empty schema raises `M7_RECREATE_REQUIRED` before DDL.
- The official setup path then idempotently seeds the builtin catalog,
  LangGraph schema, and default project under its setup coordination lock.
- `PRE_RESET_SCHEMA_REVISION`, staged migration models, guards, CLIs, Make
  targets, tests, and M4-M6 migration runbooks were removed.
- Setup, check, doctor, root/backend Makefiles, README files, and both AGENTS
  guides now describe the fresh M7 database contract.

## Main implementation

- `backend/packages/harness/deerflow/persistence/migrations/versions/0001_project_saas_baseline.py`
- `backend/packages/harness/deerflow/persistence/bootstrap.py`
- `backend/packages/harness/deerflow/persistence/migrations/env.py`
- `backend/scripts/setup_postgres.py`
- `backend/scripts/check_postgres.py`
- `backend/app/final_schema.py`
- `backend/tests/test_m7_final_baseline_postgres.py`

The old 15-revision chain and migration-only persistence domains were deleted.
The old SQLite, asset, private-work, Automation, and reliability migration
commands were also deleted rather than retained as dormant paths.

## Verification evidence

The initial real-PostgreSQL RED baseline suite produced 8 expected failures
against the old migration chain.

Final evidence:

| Gate | Result |
| --- | --- |
| M1-M7 real PostgreSQL release gate | 137 passed, 0 skipped |
| M7 final baseline plus bootstrap concurrency | 15 passed |
| Focused setup/check/bootstrap/doctor/autogen/scaffold tests | 168 passed; 5 environment-conditioned PostgreSQL skips in this non-PG invocation |
| Builtin catalog real PostgreSQL tests | 39 passed |
| M4/M5 retained runtime integration tests | 16 passed |
| Ruff format check | 1,046 files already formatted |
| Ruff lint | All checks passed |
| Full backend collection | Completed with 0 collection errors |
| `git diff --check` | Clean |

The M7 PostgreSQL tests cover empty install, old/unknown schema rejection with
unchanged catalog digest, concurrent bootstrap, one forward-only head, final
catalog equality against SQLAlchemy metadata, required functions/triggers, and
builtin catalog idempotency.

## Residue audit

The Task 8 production scan for old revisions, migration ledgers, cutover state,
and deleted migration Make targets/CLIs has no Task 8 legacy implementation
residue. Its only production matches are the new M7 baseline constant and the
`PRE_M6_SCHEMA_REVISION` compatibility branch in recovery backup code. That
branch is explicitly assigned to Task 9 by the M7 plan and was not changed in
Task 8. The only `PRE_RESET_SCHEMA_REVISION` match is a negative test asserting
that the symbol no longer exists.

The migration versions directory contains only `.gitkeep` and
`0001_project_saas_baseline.py`. A source scan of the baseline finds no
`app`/`deerflow` runtime imports.

## Temporary database cleanup

The disposable generator database
`deerflow_test_m7_gen_019f7494` was dropped from the dedicated local PostgreSQL
instance on `127.0.0.1:55437`. A final catalog query returned no remaining
`deerflow_test_*` or `deerflow_autogen_*` databases. No database on the normal
PostgreSQL port was accessed.

## Handoff

Task 8 is implementation-complete and ready for independent review. Task 9 has
not started, and milestone progress must remain unchanged until the independent
review is accepted.
