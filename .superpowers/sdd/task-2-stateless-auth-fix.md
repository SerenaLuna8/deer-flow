# Task 2 Stateless Authorization Ordering Fix

## Scope and outcome

- Base commit: `7cc13ab24c864ee201a59c03956d5d6ddaf74078`.
- Implementation commit: `5cc42dbf9f99ddc2435185e8ac64ea64890a4089` (`fix: authorize stateless runs before model validation`).
- Closed the final Task 2 approval Important and Minor findings without starting Task 3.
- `start_run()` now performs the model allowlist lookup only after successful thread authorization, while keeping the existing pure request/checkpoint shape preflight and `model_name` coercion in place. Agent/input/config/checkpoint validation and all run admission, status, and interrupt work remain after authorization and allowlist validation.
- `resolve_runtime_user_id()` now documents the actual fallback chain: runtime context, runtime-storage override, repository current user, then default. It explicitly prohibits repository/persistence ownership or authorization from using runtime-storage identity.

## TDD evidence

The first `uv` invocation could not initialize the sandbox-inaccessible default cache at `~/.cache/uv`; all recorded test commands therefore set `UV_CACHE_DIR=/tmp/deer-flow-uv-cache`.

RED command, with tests added before production changes:

```text
cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run pytest tests/test_stateless_runs_owner_isolation.py -k 'stateless_invalid_model_foreign_thread_authorizes_first' -vv
```

Result: `2 failed, 15 deselected, 1 warning in 0.61s`. Both real HTTP requests, `POST /api/runs/stream` and `POST /api/runs/wait`, returned the current `400` response while the regression required the anti-enumeration `404`.

Focused GREEN command after the minimal code move and docstring correction:

```text
cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run pytest tests/test_stateless_runs_owner_isolation.py -k invalid_model -vv
```

Result: `7 passed, 10 deselected, 1 warning in 0.66s`.

The HTTP matrix proves:

- user B targeting user A's thread with an invalid model receives `404` from both stateless routes, with zero model-config lookup;
- user A targeting the authorized thread with the same invalid model still receives `400`, with exactly one allowlist lookup;
- saver access, run admission, run status/cancel, and thread status calls remain zero in both cases;
- a real pre-existing pending run under `multitask_strategy=interrupt` remains pending with an unset abort event and no additional run;
- all three thread-scoped create/stream/wait routes retain authorization-first `404` behavior.

## Final verification

Directed HTTP/Gateway regression:

```text
cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run pytest tests/test_stateless_runs_owner_isolation.py tests/test_gateway_services.py -q
```

Result: `129 passed, 1 warning in 0.78s`.

Final fresh Task 2, channel, auth, real-worker/Langfuse, Gateway, private-context, and project authorization regression after formatting:

```text
cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run pytest tests/test_user_context.py tests/test_channel_runtime_worker_scope.py tests/test_channel_runtime_identity.py tests/test_worker_langfuse_metadata.py tests/test_internal_auth.py tests/test_auth_middleware.py tests/test_auth.py tests/test_channels.py tests/test_gateway_services.py tests/test_stateless_runs_owner_isolation.py tests/test_private_work_context.py tests/test_private_work_error_mapping.py tests/test_private_work_import_firewall.py tests/test_project_context.py tests/test_project_capabilities.py -q
```

Result: `547 passed, 8 skipped, 6 warnings in 22.08s`. The skips are existing PostgreSQL-conditioned cases; this fix did not connect to or create a database. The warnings are existing dependency deprecations.

Static and formatting verification:

```text
cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run ruff check app/gateway/services.py packages/harness/deerflow/runtime/user_context.py tests/test_stateless_runs_owner_isolation.py
```

Result: `All checks passed!`

```text
cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run ruff format --check app/gateway/services.py packages/harness/deerflow/runtime/user_context.py tests/test_stateless_runs_owner_isolation.py
```

Result: `3 files already formatted`.

`git diff --check` exited 0 with no output before the implementation commit.

## Files changed

- `backend/app/gateway/services.py`
- `backend/packages/harness/deerflow/runtime/user_context.py`
- `backend/tests/test_stateless_runs_owner_isolation.py`
- `.superpowers/sdd/task-2-stateless-auth-fix.md` (this report)

## Self-review

- The production delta is a single control-flow move; no validation logic, error payload, agent resolution, input normalization, checkpoint handling, or lifecycle mutation code was rewritten.
- Foreign-thread requests cannot use invalid/valid model candidates to probe allowlist membership because the thread check now fails first.
- Authorized callers retain the same fixed `400` invalid-model contract and validation still precedes agent/input/config/checkpoint and run admission.
- Test probes use a real FastAPI/TestClient request path, real `MemoryThreadMetaStore`, real `RunManager`, and real `MemoryRunStore`; mocks are limited to external boundaries whose non-use is part of the contract.
- No schema, migration, scoped repository/checkpointer, frontend, RLS, business database, or Task 3 code changed.

## Residual risks and concerns

No blocking concern. The only environment issue was the sandbox-inaccessible default `uv` cache, resolved by using a disposable `/tmp` cache. Existing PostgreSQL-conditioned skips remain unchanged and are unrelated to this HTTP control-flow and documentation-only fix.
