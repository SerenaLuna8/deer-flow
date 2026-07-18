# M7 Task 3 Report: remove the global LangGraph/private-work HTTP runtime

## Status

PASS — completed Task 3 only on base commit `bcd3e1ecd61e0d0545596a2bbfbfda3bd2d75915`. The implementation commit is `670d695a` (`refactor: remove global private runtime`). This does not start Task 4, claim M7 completion, or claim release readiness.

## Delivered

- Added `app.private_work.http_runtime` as the project-private HTTP admission boundary, exporting only `format_sse` and `start_private_run`.
- Moved `PrivateRunCreateRequest` and `PrivateThreadTokenUsageResponse` ownership into `gateway.private_work_schemas` and removed client-supplied private-run authority.
- Reduced Gateway lifespan wiring to the PostgreSQL platform services still required by project-private APIs. Removed `RunManager`, the legacy stream bridge, configurable legacy Run/Event stores, scheduled legacy repositories, and orphan-thread migration from Gateway startup.
- Removed the global Thread, Run, Assistant compatibility, Memory, Feedback, Suggestion, Upload, and Artifact routers and their named obsolete tests. Project-private routes remain mounted.
- Made connection inbound admit a project-private durable Run directly and wait on its PostgreSQL terminal state instead of invoking the removed in-process runtime.
- Removed the `/api/langgraph` Nginx rewrite and updated local/Docker/deploy messaging and repository guidance for the M7 topology.

## TDD evidence

Initial Task 3 surface RED before production changes:

```text
5 failed, 1 passed in 1.61s
```

The failures were the intended global route mounts, `RunManager`/stream-bridge wiring, missing project schema/runtime ownership, importable legacy router modules, and Nginx `/api/langgraph` rewrite. The already-passing assertion established that project-private routes existed before the removal.

## Final verification

Required affected PostgreSQL gate:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres \
PYTHONPATH=packages/harness .venv/bin/pytest \
  tests/test_m7_legacy_api_surface.py \
  tests/test_private_work_router.py \
  tests/test_private_work_run_router.py \
  tests/test_private_work_stream_router.py \
  tests/test_private_work_file_router.py \
  tests/test_channel_runtime_identity.py \
  tests/test_m6_private_run_gateway.py \
  tests/test_m6_gateway_reconnect_process.py -q

36 passed in 8.61s
```

PostgreSQL skips: **0**. The final run used unsandboxed localhost access because the sandbox denied TCP access to the designated disposable PostgreSQL listener.

Required blocking-I/O gate:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres \
PYTHONPATH=packages/harness .venv/bin/pytest \
  tests/blocking_io/test_gate_smoke.py \
  tests/blocking_io/test_automations.py -q

9 passed in 0.44s
```

Additional checks:

```text
Ruff check: All checks passed
Ruff format: 105 files already formatted
git diff --check: passed
Production residue scan for /api/langgraph, make_stream_bridge, and RunManager: zero hits
```

## Self-review

- `http_runtime.__all__` exposes exactly `format_sse` and `start_private_run`; `format_sse` emits compact JSON with an optional SSE `id` line first.
- Run admission preserves only the allowed input, command, config, context, and metadata fields while server-owned account/project/owner and non-interactive authority are derived from authenticated scope.
- The standard Gateway no longer mounts a global LangGraph-compatible API or creates an in-process agent runtime. `langgraph_auth` remains only for explicitly separate development tooling.
- The scheduled-task legacy router module remains for Task 4, but it is not mounted and its legacy repositories are not initialized by Gateway lifespan.
- `.superpowers/sdd/progress.md` was not changed.

## Risks and open items

- Task 4 and later M7 cleanup remain intentionally untouched; this report does not claim their modules or tests have been removed.
- The full backend suite was not run. Verification is limited to the exact Task 3 affected and blocking gates plus static/format checks. Legacy-only tests outside the Task 3 deletion list may still refer to later-task surfaces.
- Independent review is still required before Task 3 can be accepted in the milestone ledger.
