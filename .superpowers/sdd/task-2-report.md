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

## Review follow-up

### STATUS

PASS — resolved the 1 Important and 2 Minor review findings.

### Changes

- `AppConfig` now normalizes every `collections.abc.Mapping` input to a copied `dict` before null-section filtering and rejects a top-level `checkpointer` key for `dict`, `UserDict`, and custom Mapping inputs alike.
- Removed tests that locked the deleted `deerflow-harness[postgres]` and `--extra postgres` installation guidance. The default dependency contract remains covered by pyproject assertions.
- Corrected the Console entry in `backend/AGENTS.md`: public configuration is PostgreSQL-only through `database.url`; the memory guard is internal legacy code pending task 3 and cannot be selected through AppConfig.

### RED

The new focused Mapping regression failed before the validator fix:

```text
2 failed, 13 deselected in 0.20s
```

Both `UserDict` and a custom `Mapping` were accepted without raising, confirming that `checkpointer` could escape the dict-only validator.

### GREEN

Focused Mapping regression after the fix:

```text
2 passed, 13 deselected in 0.16s
```

Review-requested coverage suite:

```text
105 passed in 0.76s
```

### Verification

- Related ruff command: `All checks passed!`
- `uv lock --check`: `Resolved 233 packages in 4ms`
- `git diff --check`: passed

### Commit

Follow-up commit subject: `fix(config): reject legacy checkpointer mappings` (final hash reported after commit).

### Concerns

- Provider constants still contain stale optional-extra wording, intentionally left for task 3 as requested; no test now treats that wording as a supported contract.

## Second review follow-up

### STATUS

PASS — removed every remaining test/script fixture that treated `postgres` as a valid optional extra while preserving PostgreSQL database behavior coverage.

### Pre-change rg evidence

The initial audit found improper optional-extra uses in:

- `backend/tests/test_persistence_scaffold.py`: missing-`asyncpg` assertion matched `uv sync --all-packages --extra postgres`.
- `backend/tests/test_detect_uv_extras.py`: parser, formatter, `UV_EXTRAS`, and expected flag fixtures used `postgres` as a legal extra.
- `backend/tests/test_dev_entrypoint.py`: single/multi explicit extra and validation fixtures used `postgres`.
- `backend/tests/test_deploy_uv_extras.py`: Dockerfile passthrough, invalid glob, deploy auto-detection expectations, and Python fallback expected a postgres extra.
- `scripts/deploy.sh`: the flag-conversion comment used `--extra postgres` as its example.

This was a review-contract correction limited to tests and a script comment, so no artificial production RED was created.

### Changes

- Missing-`asyncpg` coverage now asserts only that the error identifies `asyncpg` and supplies installation guidance; it no longer locks a removed extra command.
- Legal explicit extra fixtures now use actual repository extras (`ollama`, `redis`, and `discord`).
- Deploy auto-detection coverage now proves database configuration does not add postgres, while Discord/Redis detection and explicit-extra passthrough remain covered.
- Invalid-name fixtures use neutral/real extra names rather than implying postgres is available as an optional dependency.

### Post-change rg evidence

The directed audit for `UV_EXTRAS.*postgres`, `--extra postgres`, postgres parser/formatter inputs, and postgres optional-dependency assertions returned no matches. Remaining `postgres` occurrences in the covered files are limited to PostgreSQL URL/backend behavior, the config-example negative assertion, and unrelated MCP server names.

### GREEN

Second-review coverage suite:

```text
127 passed in 5.89s
```

### Verification

- Related ruff command: `All checks passed!`
- `uv lock --check`: `Resolved 233 packages in 3ms`
- `git diff --check`: passed

### Commit

Follow-up commit subject: `test(config): stop treating postgres as an extra` (final hash reported after commit).

### Concerns

- Stale provider error prose still mentions the removed postgres extra and remains intentionally deferred to task 3; tests now validate only generic actionable missing-`asyncpg` guidance.
