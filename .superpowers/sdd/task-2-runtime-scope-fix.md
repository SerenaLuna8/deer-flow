# Task 2 Runtime Storage Scope Fix Implementation Plan and Report

> **For agentic workers:** REQUIRED SUB-SKILL: use systematic debugging, test-driven development, and verification-before-completion for every step below. This fix remains inside M4 Task 2 and must not introduce Task 3 repository/checkpointer scoping.

**Goal:** Keep the trusted channel runtime bucket available to filesystem, workspace, memory, skills, artifacts, and sandbox execution without changing repository authorization identity or persisting the bucket in run events/checkpoint metadata.

**Architecture:** `deerflow.runtime.user_context` owns two independent task-local channels: `_current_user` for authenticated repository authorization and a string-only runtime-storage override for execution storage. `start_run()` copies only the trusted internal runtime header into the storage channel when creating `run_agent()`; worker Langfuse attribution resolves from repository identity (with record/default fallback), never from runtime context.

**Tech Stack:** Python 3.12, `contextvars`, asyncio, FastAPI Gateway services, LangGraph `StateGraph`/`InMemorySaver`, pytest, Ruff.

## Global Constraints

- Work only in `/Users/jiangfeng/deer-flow/.worktrees/m4-private-work` from baseline `da2271cabea12f98f281ac404660f81212a9bc0b`.
- Do not start Task 3 or modify repository/checkpointer schemas.
- Do not monkeypatch `run_agent()` in the real-worker regression.
- Runtime header remains trusted only for internal-token + `AUTH_SOURCE_INTERNAL` + internal role callers.
- PostgreSQL tests, if needed, may create only disposable `deerflow_test_*` databases; this fix requires no database.

## Implementation Tasks

- [x] Add a real `start_run()` -> production `run_agent()` regression using a minimal real `StateGraph`, capturing `DbRunEventStore` scope, `MemoryThreadMetaStore` finalization, real `InMemorySaver` checkpoint metadata, run persistence inputs, and two distinct unbound runtime buckets.
- [x] Run the regression before production changes and record the complete RED chain.
- [x] Add `user_context` tests specifying `set_runtime_storage_user_id(str)`, `reset_runtime_storage_user_id(Token)`, and `get_runtime_storage_user_id()`, including repository separation, concurrent task isolation, failure cleanup, and a later-task probe.
- [x] Implement the storage ContextVar and make `get_effective_user_id()` prefer it while leaving `get_current_user()`, `require_current_user()`, and `resolve_user_id()` repository-only.
- [x] Replace `start_run()`'s runtime call to `set_current_user()` with storage override set/reset around `asyncio.create_task()`.
- [x] Change worker Langfuse user attribution to repository identity with `RunRecord.user_id`/`DEFAULT_USER_ID` fallback and update focused tracing tests.
- [x] Verify channel/Gateway/auth/Task 2 regressions, Ruff, formatting, diff, docs, and final commit.

## RED Evidence

Command:

```text
cd backend && uv run pytest tests/test_channel_runtime_worker_scope.py -vv --maxfail=1
```

Result: `1 failed in 0.64s`.

The real graph correctly observed storage identities `platform-runtime-a` and `platform-runtime-b`, but the same worker observed those values through `get_current_user()`. Run creation owners remained `default`; event writes were instead attributed to the two runtime buckets. Both default-owned threads remained `running` with no title after successful runs. With Langfuse enabled, each real checkpoint stored its runtime bucket in `metadata.langfuse_user_id`; checkpoint configurable and persisted run kwargs/metadata did not contain the bucket.

The focused user-context API RED was:

```text
cd backend && uv run pytest tests/test_user_context.py -q
```

Result: collection failed because `get_runtime_storage_user_id` did not yet exist, confirming the independent storage channel API was absent.

## GREEN Implementation

Implementation commit: `7543ae1c08e78e4512fa851901e52cb95db5b0b7`

- Added `_runtime_storage_user_id` plus `set_runtime_storage_user_id()`, `reset_runtime_storage_user_id()`, and `get_runtime_storage_user_id()` in `user_context.py`.
- `get_effective_user_id()` now prefers that execution-only storage override; `get_current_user()`, `require_current_user()`, and `resolve_user_id()` continue to read only `_current_user`.
- `start_run()` sets the trusted runtime header value only in the storage ContextVar while creating the worker task, resets the caller token immediately in `finally`, and leaves the authenticated/owner repository context for the copied task.
- Worker Langfuse attribution now resolves from repository `_current_user`, then `RunRecord.user_id`, then `default`; it no longer calls the runtime-context/storage resolver.
- The real-worker regression runs two unbound buckets and one bound owner through production `run_agent()`. A minimal real `StateGraph` records the effective and repository identities, invokes real callbacks, and writes real `InMemorySaver` checkpoints. Capturing stores observe production `DbRunEventStore` scope and real `MemoryThreadMetaStore` AUTO finalization.
- Unit coverage runs concurrent storage overrides, raises inside one task, resets both tokens, and proves a later task and the caller remain unpolluted.

Initial GREEN command:

```text
cd backend && uv run pytest tests/test_user_context.py tests/test_channel_runtime_worker_scope.py -q
```

Result: `13 passed in 0.63s` before the bound-owner case was added; the completed real-worker file later passed `2 passed in 0.62s`.

## Final Verification

Channel, Gateway, auth, Langfuse, real-worker, user-context, and Task 2 regressions:

```text
cd backend && uv run pytest tests/test_user_context.py tests/test_channel_runtime_worker_scope.py tests/test_channel_runtime_identity.py tests/test_worker_langfuse_metadata.py tests/test_internal_auth.py tests/test_auth_middleware.py tests/test_auth.py tests/test_channels.py tests/test_gateway_services.py tests/test_private_work_context.py tests/test_private_work_error_mapping.py tests/test_private_work_import_firewall.py tests/test_project_context.py tests/test_project_capabilities.py -q
```

Final fresh result after formatting: `530 passed, 8 skipped, 6 warnings in 22.07s`. The eight skips are existing PostgreSQL-conditioned tests; this fix did not connect to or create a database. The six warnings are existing dependency deprecations.

Static verification:

```text
cd backend && uv run ruff check app/gateway/services.py packages/harness/deerflow/runtime/user_context.py packages/harness/deerflow/runtime/runs/worker.py tests/test_user_context.py tests/test_channel_runtime_worker_scope.py tests/test_channel_runtime_identity.py tests/test_worker_langfuse_metadata.py
```

Result: `All checks passed!`

```text
cd backend && uv run ruff format --check app/gateway/services.py packages/harness/deerflow/runtime/user_context.py packages/harness/deerflow/runtime/runs/worker.py tests/test_user_context.py tests/test_channel_runtime_worker_scope.py tests/test_channel_runtime_identity.py tests/test_worker_langfuse_metadata.py
```

Result: `7 files already formatted`.

`git diff --check` and `git diff --cached --check` both exited 0 with no output before the implementation commit.

## Files Changed

- `backend/packages/harness/deerflow/runtime/user_context.py`
- `backend/app/gateway/services.py`
- `backend/packages/harness/deerflow/runtime/runs/worker.py`
- `backend/tests/test_user_context.py`
- `backend/tests/test_channel_runtime_worker_scope.py`
- `backend/tests/test_worker_langfuse_metadata.py`
- `backend/AGENTS.md`
- `.superpowers/sdd/task-2-runtime-scope-fix.md` (this report)

## Self-Review

- Runtime `config.context.user_id` remains live for agent/tool consumers, and existing channel tests continue to prove non-internal forged headers and body/config `user_id` are ineffective.
- The production worker regression proves two unbound users do not converge on `default` for storage, while repository user, persisted run owner, event owner, thread owner/title/status, checkpoint configurable, and checkpoint metadata retain default ownership semantics.
- The bound-owner regression proves repository, storage, run, event, thread, finalization, and Langfuse attribution all remain on the trusted owner.
- The runtime bucket is absent from persisted run kwargs/metadata, event ownership, checkpoint configurable, and actual checkpoint metadata with Langfuse enabled.
- Storage ContextVar reset/isolation is covered across concurrent tasks, normal completion, an exception, the caller after worker completion, and a subsequent task.
- No repository/checkpointer schema, Task 3 scoped saver/repository, migration, frontend, or database code changed.

## Residual Risks and Concerns

No blocking concern. The intentional API distinction must remain clear for future code: filesystem/workspace/memory/skills/sandbox consumers use `get_effective_user_id()` or runtime context, while repository authorization and durable ownership must use `get_current_user()`/`resolve_user_id()`. The updated backend guide records this boundary, and focused tests pin both sides.
