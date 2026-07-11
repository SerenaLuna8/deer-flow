# Task 2 Report: PostgreSQL-only dependencies and configuration

## STATUS

PASS — PostgreSQL-only configuration and dependency contract implemented and verified. No real database was accessed.

## Changed files

- `backend/packages/harness/pyproject.toml`
- `backend/pyproject.toml`
- `backend/uv.lock`
- `backend/packages/harness/deerflow/config/database_config.py`
- `backend/packages/harness/deerflow/config/app_config.py`
- `backend/packages/harness/deerflow/config/checkpointer_config.py`
- `backend/packages/harness/deerflow/config/reload_boundary.py`
- `config.example.yaml`
- `scripts/detect_uv_extras.py`
- `backend/tests/test_persistence_scaffold.py`
- `backend/tests/test_checkpointer.py`
- `backend/tests/test_detect_uv_extras.py`
- `backend/tests/test_app_config_reload.py`
- `backend/tests/test_reload_boundary.py`
- `README.md`
- `AGENTS.md`
- `backend/AGENTS.md`
- `.superpowers/sdd/task-2-report.md`

Task 1 files `backend/scripts/sqlite_inventory.py` and `backend/tests/test_sqlite_inventory.py` were not modified.

## RED

Before production changes, the required focused command failed for the expected old multi-backend reasons:

```text
29 failed, 77 passed in 0.93s
```

Failures covered the missing `url`/URL derivation API, acceptance of SQLite/memory URLs, absent pool constraints, accepted standalone checkpointer config, PostgreSQL dependencies remaining optional, database-driven postgres extra detection, and the old SQLite example contract.

## GREEN

Required focused suite:

```text
107 passed in 0.74s
```

AppConfig regressions:

```text
18 passed in 0.20s
```

Reload-boundary contract:

```text
10 passed in 0.16s
```

## Ruff

The brief-specified ruff command completed with:

```text
All checks passed!
```

## Lockfile

- `uv lock` completed successfully.
- `uv lock --check` resolved 233 packages successfully.
- Lockfile contains default `asyncpg`, `langgraph-checkpoint-postgres`, and `psycopg` dependencies.
- Lockfile contains neither `aiosqlite` nor `langgraph-checkpoint-sqlite`, and no postgres optional-extra marker remains.

## Commit

`refactor(database): require PostgreSQL configuration` (the enclosing commit; final hash reported to the parent task after creation).

## Self-review

- `DatabaseConfig` rejects empty, SQLite, memory, and non-PostgreSQL schemes and forbids legacy constructor fields.
- URL prefix conversion preserves encoded credentials, host/port/database, and query strings without double-appending `asyncpg`.
- The independent AppConfig checkpointer field and reload path are removed; old input raises a clear validation error.
- The temporary checkpointer shim derives only from `DatabaseConfig`, emits deprecation warnings on compatibility access, and cannot accept legacy backend state through its public setter/loader.
- Read-only PostgreSQL aliases keep task-3 runtime imports usable without reopening backend selection; no engine/provider branch was changed.
- Config sample contains only `$DATABASE_URL` and non-secret pool defaults; no real credentials were added.
- `git diff --check` passed, and task 1 files are untouched.

## Concerns

- Legacy SQLite/memory branches still exist in engine/checkpointer/store providers by design; task 3 must remove them and delete the temporary aliases/shim.
- Verification was limited to the brief-specified focused suites plus reload-boundary regression; the full backend suite was not requested or run.
