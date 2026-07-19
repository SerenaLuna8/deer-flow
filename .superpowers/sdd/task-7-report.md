# M7 Task 7 Report: remove legacy runtime configuration

## Scope

Task 7 removes legacy runtime configuration, file-backed extensions authority,
production memory/Redis fallbacks, and local channel configuration overlays.
The implementation started from exact baseline
`e1533d6fe3bd017c38f0589956062ef5dcd82a1c`. Task 8 was not started and
`.superpowers/sdd/progress.md` was not changed.

## TDD evidence

The strict configuration/source-absence tests were written before the
implementation.

- Initial focused RED: 46 tests ran, 20 failed and 26 passed because legacy
  keys, config probes, modules, factories, and Docker settings still existed.
- A separate null-valued tombstone RED ran 39 tests with 9 failures, proving
  that presence rather than truthiness must reject removed keys.
- Final focused gate: 59 passed, 0 failed, 0 skipped.

## Implementation

- `AppConfig` now resolves only an explicit path, `DEER_FLOW_CONFIG_PATH`, or
  the repository-root `config.yaml`. It no longer probes the current working
  directory, home directory, backend directory, or extensions/MCP JSON files.
- An exact top-level validator rejects `agents_api`, `run_events`,
  `stream_bridge`, `extensions`, `extensions_config`, `mcp_config`,
  `mcp_config_path`, `legacy_run_store`, and `legacy_event_store`, including
  keys whose value is `null`. Unknown non-tombstone keys continue to use the
  normal strict Pydantic error.
- Deleted the four legacy config modules, the file-backed extensions loader,
  the local channel runtime-config overlay, the memory event/run stores, and
  the complete `runtime/stream_bridge` package.
- Moved the final PostgreSQL durable stream implementation under
  `runtime.events` and wired Gateway and Worker directly to PostgreSQL stores.
  Test-only memory stores now live under `backend/tests/support` and cannot be
  selected by production configuration or factories.
- Kept MCP request/config models as pure typed values under `deerflow.mcp`.
  They perform no environment, global-file, or current-directory loading.
- Removed `extensions_config.example.json`, Redis stream services, volumes,
  environment variables, extras, and dependencies from the root setup,
  Docker Compose, image, entrypoint, deployment helpers, and `uv.lock`.
- Support-bundle and asset-migration code no longer discovers or imports
  extensions/MCP JSON authority. The M3 catalog finalization was updated for
  the marker-free final catalog state introduced by M7 Task 2.
- Replay now starts a real independent Worker beside Gateway. The replay
  fixture submits only the final private-run admission schema and validates
  the durable terminal stream event instead of relying on Gateway-local
  execution.
- Updated the Task 7 architecture/operation documentation in root/backend
  agent guidance, README, and replay instructions. Historical plans and the
  wider frontend/Helm documentation cleanup remain assigned to later M7
  source-absence/documentation tasks.

## Final gates

Focused Task 7 gate, using only the required local PostgreSQL listener on
`127.0.0.1:55437`:

```text
6 files, 59 passed, 0 failed, 0 skipped
```

Affected PostgreSQL integration gate, including durable stream, reconnect,
independent Worker, private SSE, quota, crash recovery, asset migration,
lifecycle, and replay:

```text
10 files, 106 passed, 0 failed, 0 skipped, 1 deprecation warning
```

Final script/config/Docker-adjacent gate:

```text
6 files, 93 passed, 0 failed, 0 skipped
```

Additional affected-suite evidence:

```text
expanded non-PostgreSQL affected suite: 861 passed, 53 skipped
full collection: 8065 tests collected, 0 collection errors
MCP/ACP adjacent suite: 91 passed, 1 skipped
asset bootstrap and M3 migration: 11 passed
```

Static and operational checks:

```text
ruff check: 75 changed Python files passed
ruff format --check: 75 changed Python files already formatted
bash/sh syntax checks: passed
uv lock --check --offline: resolved 232 packages
git diff --check: passed
make doctor: Ready, with 2 non-blocking warnings
```

The doctor gate used a temporary random `deerflow_test_m7_task7_doctor`
database on port 55437 and a generated ignored local config. The database and
generated config files were removed after the gate. Port 5432 was never used.

The Task 7 production residue command returns only the two explicit tombstone
names `agents_api` and `extensions_config` in the strict validator. It returns
no live config section, factory, memory/Redis backend, or removed-module
reference in the requested production paths.

Task 7 is ready for the required independent review. It is not marked accepted
or complete in the M7 progress ledger by this implementation commit.
