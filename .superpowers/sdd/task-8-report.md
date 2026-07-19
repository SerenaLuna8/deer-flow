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
| M1-M7 real PostgreSQL release gate | 150 passed, 0 skipped |
| M7 final baseline/default/setup/check/bootstrap bounded group | 87 passed, 0 skipped |
| Focused setup/check/bootstrap/doctor/autogen/scaffold tests | 168 passed; 5 environment-conditioned PostgreSQL skips in this non-PG invocation |
| Builtin catalog real PostgreSQL tests | 39 passed |
| M4/M5 retained runtime integration tests | 16 passed |
| Ruff format check | 1,048 files already formatted |
| Ruff lint | All checks passed |
| Full backend collection | Completed with 0 collection errors |
| `git diff --check` | Clean |

The M7 PostgreSQL tests cover empty install, old/unknown schema rejection with
unchanged catalog digest, concurrent bootstrap, one forward-only head, final
catalog equality against SQLAlchemy metadata, required functions/triggers, and
builtin catalog idempotency.

## First bounded review repair

The first independent review froze four Important findings. This repair stays
inside that checklist and does not enter Task 9.

- Truly-empty classification now inventories non-extension-owned PostgreSQL
  root objects, including relations, owned sequences/indexes, routines,
  standalone types, collations, conversions, operators/opclasses/opfamilies,
  text-search objects, statistics, rules, policies, and triggers. A database
  containing only a user sequence, function, or enum is rejected before
  Alembic. Extension-owned objects remain allowed; a real `hstore` bootstrap
  regression proves that boundary.
- `bootstrap_schema`, setup, and `check-db` now share one read-only canonical
  M7 catalog verifier. Its six audited categories cover 52 relations, 613
  columns, 382 constraints, 161 indexes, 10 functions, and 67 triggers. The
  column contract includes type/null/default/identity/generated/collation;
  constraints and indexes use canonical PostgreSQL definitions; functions and
  triggers include full bodies and event identity. LangGraph tables and their
  owned sequences/indexes are the only explicit non-app domain.
- RED evidence was 8 expected real-PostgreSQL failures: three object-only
  schemas and five representative final-schema drifts. The first GREEN rerun
  was 8 passed. The final suite additionally covers a missing trigger and
  extension ownership.
- The baseline test now compares native `pg_catalog` rows against a second
  random database built independently with `Base.metadata.create_all()`; it
  does not call the production verifier. Dedicated behavior tests cover stream
  late/duplicate terminal rejection, append-only usage/dead-job/audit/
  tombstone/restore-proof rows, updated-at behavior, and shared asset version/
  binding invariants. Function bodies and trigger event/function identities
  are checked independently so same-name empty replacements cannot pass.
- The retained final-runtime portion of the removed default-project test was
  restored as `test_default_project_final_runtime.py`: 8 tests cover
  NO_USERS/WAITING/CREATED/EXISTING and concurrency, quota rollback, unique
  admin selection, slug/partial conflict, and database-error sanitization.

Fresh repair evidence is 150 passed/0 skipped for the fixed 16-file release
gate, 87 passed/0 skipped for the bounded baseline/default/setup/check/
bootstrap group, full collection with zero errors, Ruff format/lint clean, and
`git diff --check` clean.

## Second bounded review repair

The second rereview found one remaining Important inventory gap: sequences and
indexes were accepted by owner table alone, while the canonical application
signature did not cover owned sequences or LangGraph indexes. This repair is
limited to that finding.

- Independent catalog audit locked the application-owned sequence inventory to
  two exact name/owner pairs: `deletion_tombstones_journal_sequence_seq` owned
  by `deletion_tombstones`, and `run_events_id_seq` owned by `run_events`.
  The Alembic index is also locked to `alembic_version_pkc`.
- LangGraph owns zero sequences and eleven exact name/owner indexes across its
  six tables. Root-object validation now permits only two states: the complete
  application baseline with no LangGraph objects, or that baseline plus the
  complete six-table/eleven-index LangGraph inventory. Partial, missing, or
  extra LangGraph objects fail closed. Application index definitions remain
  covered by the existing canonical full-definition digest.
- The two required real-PostgreSQL RED tests failed as expected: an unexpected
  sequence owned by `projects.membership_version` and an unexpected index on
  `checkpoints(thread_id)` were accepted by classify/bootstrap/setup/check.
  The GREEN rerun was 2 passed. Both tests also prove that failure does not
  delete or repair the unknown object.
- Independent positive/negative stage coverage verifies the exact app-only and
  full LangGraph inventories, plus partial installation, missing index, and
  extra-object refusal. That focused group is 5 passed; the retained
  object-only/schema-drift group is 9 passed; the complete baseline file is
  26 passed.

Fresh second-repair evidence is 155 passed/0 skipped for the fixed 16-file
release gate and 92 passed for the bounded baseline/default/setup/check/
bootstrap group. Full backend collection completes with zero collection
errors, Ruff format/lint are clean, and `git diff --check` is clean.

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

The repair-only canonical-signature database
`deerflow_test_99999_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` was also dropped. The
same final catalog query remained empty after the repair gates.

The second repair used a new dedicated disposable PostgreSQL cluster at
`/private/tmp/deerflow_m7_task8b.Q0HJbG`, listening only on
`127.0.0.1:55438`, because the retained 55437 cluster was not responsive. No
normal-port database was accessed. After the final gate, a catalog query
returned zero `deerflow_test_*`/`deerflow_autogen_*` databases; the 55438
server was stopped, its exact data directory was removed, and `pg_isready`
confirmed no response.

## Handoff

Task 8 plus both bounded repairs is ready for independent rereview. Task 9 has
not started, and milestone progress must remain unchanged until the independent
review is accepted.
