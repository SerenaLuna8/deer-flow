# M5 Task 15 Independent Review Fix Report

## Scope

- Review base: `7e735287d47a1b874dbc045eeebbcdd8104078e2`
- Scope kept to Task 15 PostgreSQL tests, test support, and workflow self-verification.
- No production runtime, Task 16, Task 17, migration, or frontend file was changed.

## Findings closed

1. Added a distinct `owner_a_project_b` actor derived from the existing
   `m4.project_b_owner_a` identity. The seed now carries its issued
   `ProjectContext`/`PrivateWorkContext`, project-B task, and project-B Thread.
   Assertions independently prove the same user UUID with different project
   UUIDs, task visibility through the correct scope only, and PostgreSQL
   composite-FK rejection when project-A task coordinates are written through
   the same owner's project-B scope.
2. Expanded the real FastAPI authorization matrix for detail, list, run
   history, Thread reverse lookup, update, and delete. It covers owner-A
   success, same-owner/different-project isolation, different-owner/same-project
   isolation, Viewer own-read and write denial, and `system_admin` denial.
   Owner update and delete succeed on dedicated tasks; the delete probe cannot
   invalidate later assertions.
3. Replaced raw workflow substring checks with `yaml.BaseLoader` parsing, which
   preserves the GitHub Actions `on` key as a string. The test locates
   `jobs.postgres-release-gates.steps`, proves the PostgreSQL URL hard-fail is
   before the pytest step, and requires the pytest command tokens to be exactly
   `uv run pytest`, the seven M1-M5 integration files, and `-q`.

## RED evidence

### Workflow structure

The test was strengthened first. For the RED run only, the M5 path was removed
from the pytest command and left in a YAML comment, which would still satisfy
the former substring test. The new structural assertion rejected it:

```text
uv run pytest tests/integration/test_m5_project_automation_postgres.py::test_release_workflow_has_exact_m1_to_m5_gate_after_hard_fail -q
1 failed in 0.70s
Right contains one more item: tests/integration/test_m5_project_automation_postgres.py
```

The temporary mutation was then reverted. The workflow itself has no net
change in this repair commit.

### Same-owner/different-project fixture

The API assertions were added before the M5 helper carried a second project for
owner A:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55435/postgres \
  uv run pytest tests/integration/test_m5_project_automation_postgres.py::test_project_api_is_project_owner_and_capability_scoped -q
1 failed in 1.16s
KeyError: owner_a_project_b
```

After the missing seed was added, one run reached the final row assertion and
exposed a test-author type mismatch (`owner_user_id` is stored as `str`, while
the issued context uses `UUID`). Only that assertion was corrected; no product
behavior changed.

## GREEN and verification evidence

All PostgreSQL commands used the explicit temporary administrator URL at
`127.0.0.1:55435`; the fixture continued to create and enforce random
`deerflow_test_*` databases.

```text
uv run pytest tests/integration/test_m5_project_automation_postgres.py::test_release_workflow_has_exact_m1_to_m5_gate_after_hard_fail -q
1 passed in 0.66s

POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55435/postgres \
  uv run pytest tests/integration/test_m5_project_automation_postgres.py::test_project_api_is_project_owner_and_capability_scoped -q
1 passed in 1.22s

POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55435/postgres \
  uv run pytest tests/integration/test_m5_project_automation_postgres.py::test_composite_foreign_keys_reject_cross_scope_task_thread_and_run -q
1 passed in 1.21s

POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55435/postgres \
  uv run pytest tests/integration/test_m5_project_automation_postgres.py -q
8 passed in 2.89s

POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55435/postgres \
  uv run pytest \
  tests/integration/test_m1_postgres_cutover.py \
  tests/integration/test_project_isolation_postgres.py \
  tests/integration/test_m2_project_governance_postgres.py \
  tests/integration/test_m3_shared_assets_postgres.py \
  tests/integration/test_m4_private_work_postgres.py \
  tests/integration/test_m4_private_work_migration_postgres.py \
  tests/integration/test_m5_project_automation_postgres.py -q
25 passed in 8.12s

uv run ruff format \
  tests/integration/test_m5_project_automation_postgres.py \
  tests/support/m5_automation.py
2 files left unchanged

uv run ruff check \
  tests/integration/test_m5_project_automation_postgres.py \
  tests/support/m5_automation.py
All checks passed!

git diff --check
passed
```

## Files in the repair

- `backend/tests/integration/test_m5_project_automation_postgres.py`
- `backend/tests/support/m5_automation.py`
- `.superpowers/sdd/m5-task-15-review-fix-report.md`
