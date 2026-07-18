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
- The scheduled-task legacy router remains mounted for Task 4's RED boundary, while Gateway lifespan does not initialize its legacy repositories or service singleton.
- `.superpowers/sdd/progress.md` was not changed.

## Risks and open items

- Task 4 and later M7 cleanup remain intentionally untouched; this report does not claim their modules or tests have been removed.
- The full backend execution suite was not run. The independent-review repair below adds complete collection plus the frozen affected, blocking, reviewer-targeted, lifecycle, and delayed-import slices.
- Milestone-ledger acceptance remains owned by the parent review flow; this report does not edit the ledger.

## Independent review repair (2026-07-18)

### Status

PASS — repaired the independent review result of 0 Critical, 2 Important, and 1 Minor in commit `c003cca8` (`fix: repair M7 task 3 review findings`). This remains Task 3-only work; `.superpowers/sdd/progress.md` is unchanged and Task 4 has not started.

### Findings closed

- Restored the `/api/scheduled-tasks` router mount so Task 4 retains its required RED starting point. The Task 3 global `/api/threads` removal still applies, so the nested `/api/threads/{thread_id}/scheduled-tasks` compatibility route is absent. Gateway runtime creates no legacy scheduled repository/service singleton.
- Repaired all collection-time imports of deleted routers/runtime helpers. Tests whose only subject was a deleted global Thread/Run/Memory/upload/artifact or in-Gateway execution surface were removed; surviving SSE, config-sanitization, file-limit, lifecycle, project-channel scope, Worker authority, embedded-client, and harness-drain assertions were migrated to their live modules.
- Migrated `test_gateway_services.py` to `app.private_work.http_runtime.format_sse` and `app.private_work.runtime_context.prepare_private_run_config`; delayed imports of deleted global normalization/start/run services were removed rather than hidden through collection ignores.
- Updated Docker/deploy runtime messaging and backend architecture guidance to describe Gateway durable admission plus independent Worker execution.

### Repair RED evidence

Scheduled-task mount regression before restoring the router:

```text
1 failed, 6 passed in 1.55s
E   AssertionError: assert '/api/scheduled-tasks' in paths
```

Initial complete backend collection after the Task 3 implementation:

```text
8278 tests collected, 12 errors in 3.21s
```

The errors were imports of deleted `thread_runs`, `memory`, `langgraph_runtime`, `build_run_config`, and related global router/service symbols. The review snapshot had reported seven; the fresh branch state exposed twelve.

A focused scheduled/router run then exposed three delayed stale assertions after collection was clean:

```text
3 failed, 179 passed, 2 deselected in 3.06s
```

The final delayed-import slice initially exposed one remaining obsolete Nginx rewrite assertion:

```text
1 failed, 188 passed, 1 skipped, 28 deselected in 7.41s
```

### Final repair verification

Complete backend collection:

```text
PYTHONPATH=packages/harness .venv/bin/pytest --collect-only -q
8285 tests collected in 3.16s
```

Collection errors: **0**.

Reviewer-targeted files:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres \
PYTHONPATH=packages/harness .venv/bin/pytest \
  tests/test_private_work_cutover_guard.py \
  tests/test_client.py \
  tests/test_gateway_services.py -q -rs

133 passed in 2.10s
```

Required Task 3 affected PostgreSQL gate, including the new scheduled-task OpenAPI regression:

```text
37 passed in 9.35s
PostgreSQL skips: 0
```

The original implementation gate had 36 tests; the repair adds one required OpenAPI assertion, so the final count is 37.

Required blocking-I/O gate:

```text
9 passed in 0.45s
```

Scheduled/OpenAPI and runtime-authority focused gate:

```text
62 passed, 1 deselected in 2.54s
```

Project-private Gateway lifecycle/cutover gate:

```text
8 passed in 2.34s
```

Delayed-import and migrated surviving-contract slice:

```text
189 passed, 1 skipped, 28 deselected in 7.20s
```

The single skip is the existing optional Docker CLI/Compose availability test; it is outside the zero-skip PostgreSQL gate.

Additional final checks:

```text
Ruff: All checks passed for 24 modified Python files
Ruff format: 24 files already formatted
git diff --check: passed
Production /api/langgraph, make_stream_bridge, and RunManager scan: zero hits
Deleted router/service test-import scan: zero unexpected hits
Gateway embedded-runtime wording scan: zero hits
```

### Repair scope and remaining concerns

- Deleted tests were limited to global HTTP/runtime behavior that no longer exists: global Run cancel/messages/events/token usage/regenerate/wait, Gateway orphan recovery, and Gateway-auth injection into file-backed setup/update Agent tools. Project-private Run/stream/file/cutover coverage and independent Worker authority remain collected and green.
- `tests/test_channel_runtime_worker_scope.py` remains in place for Task 5 and now asserts project admission strips message authority and the Worker rebuilds exact scope from issued `PrivateWorkContext`.
- `tests/test_multi_worker_postgres_gate.py` remains in place and now pins that Gateway has no embedded runner while `RunAgentPrivateExecutor` owns agent execution.
- The complete backend execution suite was not run. Completion evidence is the full zero-error collection plus the frozen Task 3 PostgreSQL, blocking, reviewer-targeted, scheduled/OpenAPI, lifecycle, and direct delayed-import slices above.
- Task 4 still owns complete removal of the mounted legacy scheduled-task router and implementation.
