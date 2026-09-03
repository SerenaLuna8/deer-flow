# Knowledge test slimming and database architecture design

Date: 2026-09-03
Scope: `backend/tests/knowledge/`, `backend/tests/postgres_utils.py`,
`backend/tests/conftest.py`, `backend/tests/support/run_skill_writer_cohort.py`,
`backend/AGENTS.md`.

## Problem

`backend/tests/knowledge/` contains 74 files, about 44,500 lines and 1,802
collected items. Around 45 files use real PostgreSQL. The current
architecture provisions one database per test: the shared
`postgres_database_url` fixture runs `CREATE DATABASE` plus
`CREATE EXTENSION vector`, then every knowledge harness calls
`_install_full_schema()` (about 6,500 lines of SQL), and teardown runs
`pg_terminate_backend` plus `DROP DATABASE`. Roughly one thousand
create/install/drop cycles dominate the suite's wall-clock time.

The directory also still carries milestone acceptance artifacts (M10 baseline
contract, evaluation corpus, M10/M11 quality evaluation, parsing quality,
offline extraction matrix) that verify test-only evaluation code or duplicate
operator scripts rather than product behavior.

## Decisions

1. Delete milestone artifacts only; keep every behavior test.
2. Use a session-scoped Schema V1 template database and clone it per test
   with `CREATE DATABASE ... TEMPLATE`. Isolation stays one database per test.
3. Limit the fixture change to `tests/knowledge/` via a directory `conftest.py`.
   Root-level tests keep their current fixtures and behavior.

## Database architecture

### `tests/postgres_utils.py`

`temporary_postgres_database(admin_url, *, template=None)` gains an optional
template name. With a template it issues
`CREATE DATABASE "<name>" TEMPLATE "<template>"` and skips the pgvector step
(the template already contains the extension). Without a template it behaves
exactly as today. Generated names keep the `deerflow_test_<pid>_<hex>` shape
and pass the existing safety validation.

### `tests/knowledge/conftest.py` (new)

- `knowledge_schema_template` — session-scoped synchronous fixture. It uses
  `asyncio.run` so it does not depend on pytest-asyncio loop scoping. It
  creates one `deerflow_test_*` database, installs pgvector, runs
  `_install_full_schema()`, disposes every engine (a template must have no
  open connections), and yields the database name. Session teardown
  terminates backends and drops the template.
- `postgres_database_url` — function-scoped override. Clones the template,
  wraps the clone in the same `RunSkillWriterCohortLease` handling as the
  root fixture, and yields a `RedactedURL`.
- `empty_postgres_database_url` — function-scoped, no template. Reserved for
  tests that exercise schema bootstrap itself.

### Shared cohort helper

The lease wrapping currently inlined in the root `postgres_database_url`
fixture moves into `tests/support/run_skill_writer_cohort.py` as an
`asynccontextmanager` (`test_database_with_writer_cohort(url, request)`).
Root and knowledge conftests both call it, so behavior is identical.

### Harness cleanup

All `await _install_full_schema(engine)` calls and their imports are removed
from knowledge tests and helpers (about 40 sites across 22 files, including
`extraction_test_helpers.py`). Installing into an already installed clone
would fail. `bootstrap_schema()` calls stay: they are no-ops on a current
catalog.

`test_schema_repository.py` keeps installing only where the install is the
subject: `test_default_registry_bootstrap_roundtrip_and_conflict`,
`test_missing_registry_seed_fails_bootstrap_without_marker`,
`test_bootstrap_with_explicit_skip_installs_schema_without_seed`, and
`test_default_full_install_seeds_both_providers_with_their_own_keys` switch
to `empty_postgres_database_url`. The remaining constraint and ORM parity
tests use the cloned database.

## Deletions

| Removed | Reason |
| --- | --- |
| `test_m10_baseline_contract.py`, `fixtures/m10_contract_baseline.json` | M10 T0 frozen design samples and M9 premises |
| `test_m10_eval_corpus.py`, `fixtures/m10_retrieval_cases.json`, `_generate_m10_eval_corpus.py`, `eval_metrics.py` | Evaluation corpus contract and metric worked examples |
| `test_m10_quality_eval.py`, `test_m11_quality_eval.py`, `eval_quality.py` | Offline quality gate arithmetic and opt-in real-model evaluation |
| `test_parsing_quality.py`, `parsing_quality.py`, `fixtures/parsing_retrieval_cases.json` | P4-T6 parsing quality evaluation |
| `test_extraction_offline_matrix.py` | Duplicates `scripts/check_extraction_runtime.py --matrix` |

`test_package.py::test_frozen_constants_match_the_t0_baseline_fixture` asserts
literal values instead of reading the deleted fixture.

## Documentation

`backend/AGENTS.md` (Knowledge section) states that knowledge tests clone a
session-scoped Schema V1 template database and that schema bootstrap tests use
`empty_postgres_database_url`. No README change: no user-visible behavior
changes.

## Verification

After implementation (not before):

- `uvx ruff format --check .` and `uvx ruff check .` in `backend/`.
- `uv run pytest tests/test_postgres_utils.py tests/test_reset_postgres.py -q`.
- `uv run pytest tests/knowledge -q` with the development `DATABASE_URL` and
  local MinIO; report collected count and wall-clock time against the
  previous 1,802 items.

Focused runs do not certify the full `make test` core gate; that remains a
separate local run.
