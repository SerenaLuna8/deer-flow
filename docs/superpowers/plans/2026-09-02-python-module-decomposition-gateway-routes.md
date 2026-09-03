# Gateway Router Module Decomposition (Batch 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the Private Work and Project Assets Gateway Router monoliths into resource-owned modules while preserving every HTTP, authorization, serialization, secret-masking, transaction, process-ownership, import, and Router-registration contract.

**Architecture:** Keep `private_work.py` and `project_assets.py` as compatibility façades that re-export the exact objects owned by new packages. Leaf modules own contracts, dependencies, and resource-family handlers; one composition module creates each combined Router and registers leaf routes in the current order. `register_asset_routes()` moves as one unchanged dynamic-registration unit. Gateway remains HTTP admission/replay only, and Worker remains the sole Agent-graph executor.

**Tech Stack:** Python 3.12+, FastAPI 0.136.1, Pydantic 2.13.3, SQLAlchemy async APIs, pytest 9, pytest-asyncio, Ruff, PostgreSQL-backed focused tests, and the repository Make targets.

**Spec:** `docs/superpowers/specs/2026-09-02-python-module-decomposition-design.md`, sections 5, 7, 9, and 14-16.

## Global Constraints

### Confirmed execution baseline

- Implement only from the isolated worktree `/Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations` on branch `codex/python-module-decomposition-foundations`.
- The audited production baseline is commit `08b522071774111bdaa37c650cbf1edf929a874f`. Before implementation, require this commit or an explicitly reviewed descendant containing only the accepted Batch 0-1 work. If production files named in this plan have drifted, stop and re-audit them before moving code.
- The worktree currently contains user-owned untracked files:
  - `docs/superpowers/plans/2026-09-02-python-module-decomposition-foundations.md`
  - `docs/superpowers/specs/2026-09-02-python-module-decomposition-design.md`
  Preserve both. Do not stage, rewrite, or delete them as an incidental implementation step.
- Root `.env` and `config.yaml` are present in the worktree, are ignored by Git, and matched the source checkout by SHA-256 during the 2026-09-02 audit. Do not print their contents. If execution uses a replacement worktree, copy these two files from `/Users/jiangfeng/workspace/deer-flow/` before running environment-dependent gates, verify they remain ignored, and never commit them.
- The current focused Router manifest baseline passed:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py::test_router_manifests_match_the_pre_split_baseline \
    -q
  ```

  Expected result before any Batch 2 production move: `1 passed`.

### Scope boundary

- Production code first. Tests may move their imports and monkeypatch targets with the corresponding owner module and may add focused characterization needed to make the move safe. Do not perform a general test-suite reorganization.
- Batch 2 includes only:
  - `backend/app/gateway/routers/private_work.py`
  - `backend/app/gateway/routers/private_work_routes/**`
  - `backend/app/gateway/routers/project_assets.py`
  - `backend/app/gateway/routers/project_asset_routes/**`
  - their direct Gateway production consumers, focused tests, and the minimal architecture documentation required by repository guidance.
- `app.knowledge.gateway`, database schema, migrations, domain services, repositories, Worker execution, Scheduler admission, Sandbox tools, frontend code, and deployment definitions are out of scope.
- Do not rename endpoints, request/response fields, error codes, public messages, route names, operation IDs, tags, prefixes, dependencies, status codes, response models, or headers while moving code.
- Do not change transaction count, commit ownership, lock order, owner/project authorization, hidden-resource 404 behavior, capability 403 behavior, durable SSE cursor semantics, terminal publication, disconnect handling, or process responsibility.
- Do not add wrappers, proxy modules with mutable state, duplicate DTO classes, a second combined Router, deprecation warnings, or copied redaction/error-mapping implementations.
- Preserve the dependency direction `app.* -> deerflow.*`. New owner packages must never import their legacy façades.
- Use `apply_patch` for edits. Preserve unrelated changes. Never use destructive reset/checkout commands.
- No staging, commit, push, merge, or branch deletion is authorized by this plan. Each task contains an authorization-gated commit checkpoint: use it only if the user separately authorizes local Batch 2 commits. Otherwise leave changes unstaged and use the exact diff/status checkpoint.

### Audited design refinements

The current code exposed two details that were absent from the earlier directory sketch:

1. `GET /readiness` has no valid owner in the approved Private Work leaf list. This plan proposes one explicit design amendment: add `private_work_routes/readiness.py` so `router.py` remains composition-only. User approval of this plan must explicitly include that amendment before Task 5 starts. Do not hide an endpoint in `dependencies.py` and do not put a handler body in `router.py`.
2. A function re-export preserves `is` identity but not legacy-module monkeypatch propagation because the function resolves globals in its defining module. Migrate each focused test's patch target to the new owner in the same task. Do not emulate old patch propagation with wrappers or duplicate mutable globals.

### Final ownership layout

```text
backend/app/gateway/routers/
├── private_work.py                         # compatibility façade; __all__ stays ["router"]
├── private_work_routes/
│   ├── __init__.py
│   ├── contracts.py
│   ├── dependencies.py
│   ├── readiness.py
│   ├── streaming.py
│   ├── context_controls.py
│   ├── files.py
│   ├── approvals.py
│   ├── runs.py
│   ├── feedback.py
│   ├── threads.py
│   └── router.py
├── project_assets.py                       # compatibility façade; no new __all__
└── project_asset_routes/
    ├── __init__.py
    ├── contracts.py
    ├── common.py
    ├── catalog.py
    ├── agents.py
    ├── skills.py
    ├── mcp.py
    ├── bindings.py
    └── router.py
```

Private Work dependency direction:

```text
contracts     dependencies
    ^              ^
    └──── leaf route modules ────┐
context_controls <- runs <- threads
context_controls -> streaming (polling constant only)
streaming       <- runs
leaf route modules -> router -> legacy façade
```

Project Assets dependency direction:

```text
contracts <- common <- catalog/agents/skills/mcp/bindings
                          └──────────┬───────────┘
                                     v
                                   router -> legacy façade
```

`router.py` may import leaves; no leaf may import `router.py` or the legacy façade.

### Frozen Router facts

The old Batch 0 manifest included source-module provenance and therefore must be replaced before moving response classes. The replacement uses logical class/function names plus separate owner-identity tests.

| Router | Routes | Current module-sensitive digest | Stable logical digest | OpenAPI operation digest | OpenAPI path/method digest |
| --- | ---: | --- | --- | --- | --- |
| Private Work | 45 | `a81e85093f732414a5ce8edc38040dc6783e85e4d8316c5eb08c2362850ae2e2` | `9bdab5e8c3ae4d160dcba02823991083075004e8462d747ab05505d58b025071` | `b45ecf771a9bf0008cb11a251f55a5a119dd6e5ecd9772bf121c3f5757ba4860` | `afd277aa4ce3f9a7e3a5ff7bdb7c9be8e661ed461bd2ef0dcf9e20f470ba72cc` |
| Project Assets | 58 | `66a88150e12038d66e561a1577456ddb3630e1f3263b31ab77a43f5aaf4d28b6` | `4540acd74ce953a8754c5aae9c119e245f28fbf1986b145763e0fda238adb6ab` | `4318232d5564dabf7b7ae9c20fafe8dbec53ee5a11ad3577412f7b0d02105e03` | `43a82d9b5d4990b11fee50b4171e9cc3f437b81a862f801ca103f3be6b17237e` |
| Asset Catalog | 5 | `2bf16801b2f52d284dee015b4b2ade6d15df02ad695816766c94e4cc3fb12a8d` | `fe91c789905280f2b3d6f575409f4e8be5edff59a60796a40c105648971459ac` | `bcddb97ab067006f1dba0b6653fb12099828c8dc203c17bf8e04347e9afda70a` | `85811994377d69210d9ad7be76f192396ac5daf5cd1647d5f8b5854d23f4f4ee` |
| Admin Assets | 10 | not previously frozen | `fecb2764121b4f4968784d662225050fd9b5b213baf8fa7f6b9dd348eb3eb27b` | not required | not required |
| Admin Project Assets | 32 | not previously frozen | `582a191b683382b1d2a22b84d2fd0a605806fabea2f1ee2862b521b25dad5c69` | not required | not required |

`register_asset_routes()` must retain these isolated switch manifests:

| Caller shape | Routes | Stable logical digest |
| --- | ---: | --- |
| Project: `include_project_asset_delete=True` | 25 | `5dfe344883f740a2b08b71c72a29fd720fe844412f9dc4cdbdb463537350494f` |
| Admin: `include_shared_asset_mutations=False` | 6 | `41a3a13101a7fef013dbb0d72f741003c9f304a4c379cd1a8b2263bf800cfe24` |
| Admin project: `include_skill_export=False` | 21 | `82e37848b996116c677dd46d8a718d178996f8920007505d40957d65af11ba8b` |

Private Work composition order is exactly: readiness; context controls; files; approvals; runs; feedback; threads.

Project Assets composition order is exactly:

1. Skill preview/fork/import/readiness/secrets routes 1-9.
2. Agent list/runtime-assessment/default routes 10-13.
3. Skill list route 14.
4. MCP list/configured/secret routes 15-21.
5. Binding routes 22-30.
6. The unchanged `register_asset_routes()` output 31-55.
7. MCP inventory/discovery routes 56-58.

### Subagent execution protocol

- Execute tasks sequentially because the two legacy modules and the contract test are shared state.
- For each task: implementation subagent -> specification-compliance reviewer -> code-quality reviewer -> fix/re-review loop -> task checkpoint.
- Reviewers must inspect the actual diff and current test output; summaries from earlier tasks are not evidence.
- Stop the task on any identity, route, OpenAPI, dependency, authorization, error, serialization, secret, SSE, transaction, lock, or import-cycle drift. Revert the whole task through a recoverable task checkpoint; do not patch behavior forward during a structural move.

---

## Task 1: Strengthen Router characterization before moving production code

**Files:**

- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Create: `backend/tests/test_gateway_router_registration_contract.py`

### Steps

- [ ] Confirm the execution baseline without changing files:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations
  git branch --show-current
  git rev-parse HEAD
  git status --short
  git check-ignore -v .env config.yaml
  shasum -a 256 \
    /Users/jiangfeng/workspace/deer-flow/.env .env \
    /Users/jiangfeng/workspace/deer-flow/config.yaml config.yaml
  ```

  Require branch `codex/python-module-decomposition-foundations`, the audited commit or reviewed descendant, both local config files ignored, matching pairs of hashes, and only the already-known user files plus this plan as untracked documentation.

- [ ] In `test_python_module_decomposition_contract.py`, change Router-manifest symbol normalization so a legitimate owner-module move does not alter the behavioral digest:

  ```python
  def _logical_name(value: object) -> str | None:
      if value is None:
          return None
      return getattr(
          value,
          "__qualname__",
          getattr(value, "__name__", type(value).__name__),
      )
  ```

  Use `_logical_name()` for `response_model`, route class, and dependency callable names. Keep row order unsorted so registration order remains frozen. Replace the three existing expected digests with the stable logical digests in the table above, add `admin_router` and `admin_project_router`, and assert their 10/32 route digests.

- [ ] Add the exact isolated OpenAPI normalization and constants:

  ```python
  from fastapi import FastAPI

  _OPENAPI_HTTP_METHODS = frozenset(
      {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
  )


  def _sha256_json(rows: list[dict[str, str | None]]) -> str:
      encoded = json.dumps(
          rows,
          sort_keys=True,
          separators=(",", ":"),
      ).encode("utf-8")
      return hashlib.sha256(encoded).hexdigest()


  def _openapi_route_digests(router) -> tuple[int, str, str]:
      application = FastAPI()
      application.include_router(router)
      schema = application.openapi()
      operations: list[dict[str, str | None]] = []
      for path, path_item in sorted(schema["paths"].items()):
          for method, operation in sorted(path_item.items()):
              if method not in _OPENAPI_HTTP_METHODS:
                  continue
              operations.append(
                  {
                      "method": method.upper(),
                      "path": path,
                      "operationId": operation.get("operationId"),
                  }
              )
      path_methods = [
          {"method": row["method"], "path": row["path"]}
          for row in operations
      ]
      operation_ids = [row["operationId"] for row in operations]
      assert all(operation_ids)
      assert len(operation_ids) == len(set(operation_ids))
      return (
          len(operations),
          _sha256_json(operations),
          _sha256_json(path_methods),
      )
  ```

  Add `EXPECTED_OPENAPI_ROUTE_DIGESTS` with the exact 45/58/5 tuples from the frozen table and assert all three in `test_router_openapi_operations_match_the_pre_split_baseline()`.

- [ ] Add a parameterized `register_asset_routes()` switch characterization. Import `pytest`, `APIRouter`, `_admin_actor`, `_admin_project_actor`, `AssetRoute`, `project_asset_context`, and `register_asset_routes`, then use:

  ```python
  @pytest.mark.parametrize(
      ("actor_dependency", "options", "expected"),
      (
          (
              project_asset_context,
              {"include_project_asset_delete": True},
              (25, "5dfe344883f740a2b08b71c72a29fd720fe844412f9dc4cdbdb463537350494f"),
          ),
          (
              _admin_actor,
              {"include_shared_asset_mutations": False},
              (6, "41a3a13101a7fef013dbb0d72f741003c9f304a4c379cd1a8b2263bf800cfe24"),
          ),
          (
              _admin_project_actor,
              {"include_skill_export": False},
              (21, "82e37848b996116c677dd46d8a718d178996f8920007505d40957d65af11ba8b"),
          ),
      ),
  )
  def test_register_asset_routes_switch_manifests(
      actor_dependency,
      options: dict[str, bool],
      expected: tuple[int, str],
  ) -> None:
      isolated_router = APIRouter(route_class=AssetRoute)
      register_asset_routes(isolated_router, actor_dependency, **options)
      assert _route_digest(isolated_router) == expected
  ```

- [ ] Create `test_gateway_router_registration_contract.py` with an app-level semantic-key check. Use `create_app()` under pytest's deliberately non-sensitive `DATABASE_URL`; do not load or print the developer `.env` in the test:

  ```python
  from __future__ import annotations

  from collections import Counter
  from itertools import chain

  from fastapi.routing import APIRoute

  from app.gateway.app import create_app
  from app.gateway.routers.admin_assets import admin_project_router, admin_router
  from app.gateway.routers.private_work import router as private_work_router
  from app.gateway.routers.project_assets import catalog_router, project_router

  _HTTP_METHODS = frozenset(
      {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
  )


  def _api_routes(router) -> list[APIRoute]:
      return [route for route in router.routes if isinstance(route, APIRoute)]


  def _source_key(route: APIRoute) -> tuple[tuple[str, ...], str, str]:
      return tuple(sorted(route.methods or ())), route.path, route.name


  def test_batch_2_routers_are_registered_once_without_global_duplicates() -> None:
      application = create_app()
      app_routes = _api_routes(application)
      app_source_keys = Counter(_source_key(route) for route in app_routes)
      source_routes = chain(
          _api_routes(private_work_router),
          _api_routes(project_router),
          _api_routes(catalog_router),
          _api_routes(admin_router),
          _api_routes(admin_project_router),
      )
      for route in source_routes:
          assert app_source_keys[_source_key(route)] == 1

      method_paths = Counter(
          (method, route.path)
          for route in app_routes
          for method in route.methods or ()
      )
      assert {key: count for key, count in method_paths.items() if count != 1} == {}

      route_ids = [route.unique_id for route in app_routes]
      assert all(route_ids)
      assert len(route_ids) == len(set(route_ids))

      schema = application.openapi()
      operation_ids = [
          operation["operationId"]
          for path_item in schema["paths"].values()
          for method, operation in path_item.items()
          if method in _HTTP_METHODS
      ]
      assert all(operation_ids)
      assert len(operation_ids) == len(set(operation_ids))
  ```

- [ ] Run the characterization on untouched production code:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_gateway_router_registration_contract.py \
    -q
  ```

  Expected result: all tests pass; route counts and all declared digests match. If a new assertion fails, correct the characterization from the current audited code. Do not change production to fit an incorrect baseline.

- [ ] Run the task checkpoint:

  ```bash
  git diff --check
  git status --short
  git diff -- backend/tests/test_python_module_decomposition_contract.py \
    backend/tests/test_gateway_router_registration_contract.py
  ```

- [ ] If and only if explicit Batch 2 local-commit authorization exists, stage only these two test files and commit with message `test(gateway): freeze batch 2 router contracts`. Otherwise leave them unstaged.

## Task 2: Extract Private Work contracts and shared dependencies

**Files:**

- Create: `backend/app/gateway/routers/private_work_routes/__init__.py`
- Create: `backend/app/gateway/routers/private_work_routes/contracts.py`
- Create: `backend/app/gateway/routers/private_work_routes/dependencies.py`
- Modify: `backend/app/gateway/routers/private_work.py`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Modify: `backend/tests/test_run_execution_profile.py`

### Steps

- [ ] Add `test_private_work_contract_and_dependency_exports_are_exact()` first. Import inside the test so the expected pre-implementation failure is isolated. Assert `is` identity from the legacy module to:

  - `contracts`: `_POSTGRES_BIGINT_MAX`, `PRIVATE_THREAD_TITLE_MAX_LENGTH`, `PrivateThreadCreateRequest`, `PrivateThreadPatchRequest`, `PrivateThreadBranchRequest`, `ExecutionApprovalEnvelopeResponse`, `_public_run_metadata`, `_run_response`.
  - `dependencies`: `_thread_service`, `_execution_approval_service`, `_chat_control_service`, `_file_service`, `_file_streamer`, `_run_service`, `_browser_chat_run_service`, `_run_event_store`, `_run_store`, `_feedback_service`, `_runtime_dependency`, `_scoped_checkpointer`, `_raise_http`.

  Run only this test and confirm it fails with `ModuleNotFoundError` for `private_work_routes`, proving the test precedes the owner modules.

- [ ] Add one nested metadata-redaction characterization to `test_run_execution_profile.py`:

  ```python
  def test_public_run_metadata_removes_nested_authority_and_secret_fields() -> None:
      from app.gateway.routers.private_work_routes.contracts import _public_run_metadata

      assert _public_run_metadata(
          {
              "project_id": "hidden-project",
              "__scope": "hidden-scope",
              "access_token": "hidden-token",
              "safe": {"owner_user_id": "hidden-owner", "label": "visible"},
              "items": [{"password": "hidden-password", "value": 1}, 2],
          }
      ) == {
          "safe": {"label": "visible"},
          "items": [{"value": 1}, 2],
      }
  ```

  Confirm this test also fails before the owner module exists.

- [ ] Create the package marker with no import side effects.

- [ ] Move, without renaming or logic edits, into `contracts.py`:

  - `_POSTGRES_BIGINT_MAX` and `PRIVATE_THREAD_TITLE_MAX_LENGTH`; the former remains the single HTTP pagination/schema bound imported by contract, file, Run, and thread modules.
  - All 47 Pydantic request/response classes from `PrivateThreadCreateRequest` through `PrivateSuggestionsResponse`.
  - `_thread_response`, `_public_run_metadata`, `_timestamp`, `_run_response`, `_execution_approval_response`, `_file_response`, and `_feedback_response`.

- [ ] Move, without changing validation/error behavior, into `dependencies.py`:

  - `_thread_service`, `_execution_approval_service`, `_chat_control_service`.
  - `_file_service`, `_file_streamer`.
  - `_run_service`, `_browser_chat_run_service`, `_run_event_store`, `_run_store`.
  - `_feedback_service`.
  - `_runtime_dependency`, `_scoped_checkpointer`, `_raise_http`.

  `dependencies.py` may import contracts and domain services; it must not import `private_work.py` or any route-family module.

- [ ] In the still-monolithic `private_work.py`, import these exact owner objects and let the remaining handler bodies use them. Do not create local wrapper functions or subclasses. Keep the existing root Router and route decorators in the legacy file during this task.

- [ ] Run the exact identity/redaction tests and direct contract consumers:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_private_thread_title_validation.py \
    tests/test_run_workload_profile.py \
    tests/test_run_token_budget_usage.py \
    tests/test_run_execution_profile.py \
    tests/test_vision_dispatch_budget_postgres.py \
    -q -m "not postgres and not provider_integration"
  ```

  Expected result: pass; the logical Router/OpenAPI digests remain exact, nested metadata stays redacted, and legacy imports are identical owner objects.

- [ ] Run Ruff on the changed slice:

  ```bash
  uvx ruff format --check \
    app/gateway/routers/private_work.py \
    app/gateway/routers/private_work_routes \
    tests/test_python_module_decomposition_contract.py \
    tests/test_run_execution_profile.py
  uvx ruff check \
    app/gateway/routers/private_work.py \
    app/gateway/routers/private_work_routes \
    tests/test_python_module_decomposition_contract.py \
    tests/test_run_execution_profile.py
  git diff --check
  git status --short
  ```

- [ ] If explicitly authorized, commit only this slice with message `refactor(gateway): extract private work contracts`. Otherwise leave it unstaged.

## Task 3: Extract Private Work context, file, and approval route families

**Files:**

- Create: `backend/app/gateway/routers/private_work_routes/context_controls.py`
- Create: `backend/app/gateway/routers/private_work_routes/files.py`
- Create: `backend/app/gateway/routers/private_work_routes/approvals.py`
- Create: `backend/app/gateway/routers/private_work_routes/streaming.py`
- Modify: `backend/app/gateway/routers/private_work.py`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Modify: `backend/tests/test_chat_control_replay_identity.py`
- Modify: `backend/tests/test_private_compact_api.py`
- Modify: `backend/tests/test_private_context_projection_stream.py`
- Modify: `backend/tests/test_private_context_projection_v2_api.py`
- Modify: `backend/tests/test_private_upload_discard_api.py`
- Modify: `backend/tests/test_execution_approval_api.py`

### Steps

- [ ] Add an identity test for the directly imported compatibility seams before moving them:

  ```python
  def test_private_work_context_file_and_approval_exports_are_exact() -> None:
      from app.gateway.routers import private_work as legacy
      from app.gateway.routers.private_work_routes import contracts, context_controls
      from app.gateway.routers.private_work_routes import streaming

      assert legacy._normalize_prepared_edit_replay is context_controls._normalize_prepared_edit_replay
      assert legacy._context_projection_sse_consumer is context_controls._context_projection_sse_consumer
      assert legacy.ExecutionApprovalEnvelopeResponse is contracts.ExecutionApprovalEnvelopeResponse
      assert legacy._PRIVATE_STREAM_POLL_SECONDS is streaming._PRIVATE_STREAM_POLL_SECONDS
  ```

  Confirm the test fails because the context owner does not yet exist.

- [ ] In each new route-family module, create one child `APIRouter(route_class=PrivateWorkRoute)` with no prefix, tags, or root dependency. The combined root supplies the existing prefix, tag, and `Depends(require_project_private_open)`. Setting `route_class` on every child is mandatory because `include_router()` re-adds child routes.

- [ ] Create `streaming.py` in this task with the single shared timing constant `_PRIVATE_STREAM_POLL_SECONDS = 0.25`. Import that exact object into the still-legacy durable stream code and into `context_controls.py`; this lets context projection move before the durable protocol without importing the façade or duplicating the polling value. Characterize `inspect.signature(context_controls._context_projection_sse_consumer).parameters["poll_seconds"].default == streaming._PRIVATE_STREAM_POLL_SECONDS`. Leave the remaining durable stream implementation in `private_work.py` until Task 4.

- [ ] Move into `context_controls.py`, preserving decorator order and handler bodies:

  - `_prepared_history_replay_kind`, `_normalize_prepared_edit_replay`.
  - Goal get/set/clear routes.
  - Compact route.
  - Context-usage read/stream routes plus `_context_projection_sse_consumer` and `_context_projection_stream_cursor`.
  - Branch, regenerate-prepare, edit-regenerate-prepare, and suggestions routes.

  Keep context projection on its current `format_sse` implementation; do not couple it to durable Run streaming. Import dependency functions as module globals so tests can patch `context_controls._chat_control_service` directly.

- [ ] Move into `files.py`, preserving the Builder-thread visibility preflight, response headers, and stream response helpers:

  - `_upload_chunks`, `_upload_limits_response`.
  - Upload, upload-limits, list, delete, file download, and artifact download routes.

  Keep `X-Next-Offset`, `private_streaming_response()`, chunk size, and error mapping byte-for-byte equivalent.

- [ ] Move the three execution-approval routes into `approvals.py`. Keep lifecycle/transaction logic in `app.private_work.execution_approval`; the Router module only resolves the service, invokes it, and projects the existing response.

- [ ] At the exact former decorator positions in the still-legacy root Router, replace the removed route declarations with:

  ```python
  router.include_router(context_controls.router)
  router.include_router(files.router)
  router.include_router(approvals.router)
  ```

  Preserve readiness before these includes and the still-legacy Run routes after them. Never leave old decorators and child includes active together.

- [ ] Immediately import `_normalize_prepared_edit_replay`, `_context_projection_sse_consumer`, and `stream_private_thread_context_usage` from `context_controls.py` and `_PRIVATE_STREAM_POLL_SECONDS` from `streaming.py` back into `private_work.py`. These are same-object compatibility aliases required for this task's checkpoint; do not wait until final façade conversion.

- [ ] Migrate only the affected test seams:

  - `test_chat_control_replay_identity.py`: import `_normalize_prepared_edit_replay` from `context_controls`; retain the identity assertion against the legacy export.
  - `test_private_compact_api.py`, `test_private_context_projection_stream.py`, and `test_private_context_projection_v2_api.py`: patch `context_controls._chat_control_service`; call context handlers through `context_controls` while mounting `legacy.router`.
  - `test_private_upload_discard_api.py`: patch `files._file_service` and `dependencies._run_service`; mount `legacy.router`.
  - `test_execution_approval_api.py`: patch `approvals._execution_approval_service`; use `contracts.ExecutionApprovalEnvelopeResponse` for model validation; mount `legacy.router`.

  Do not add a generic test helper layer or rewrite unrelated fixtures.

- [ ] Run the route-manifest/OpenAPI/app-registration gates and focused family tests:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_gateway_router_registration_contract.py \
    tests/test_private_compact_api.py \
    tests/test_chat_control_replay_identity.py \
    tests/test_private_context_projection_stream.py \
    tests/test_private_context_projection_v2_api.py \
    tests/test_private_upload_discard_api.py \
    tests/test_execution_approval_api.py \
    -q -m "not postgres and not provider_integration"
  ```

  Expected result: pass; Private Work remains 45 routes, every route uses `PrivateWorkRoute`, root authorization appears once per route, and no app-level duplicate appears.

- [ ] Run focused formatting/lint plus `git diff --check` and inspect the three moved handler diffs for code movement only.

- [ ] If explicitly authorized, commit only this slice with message `refactor(gateway): split private work control routes`. Otherwise leave it unstaged.

## Task 4: Extract Private Work durable streaming and Run routes as one protocol slice

**Files:**

- Modify: `backend/app/gateway/routers/private_work_routes/streaming.py`
- Create: `backend/app/gateway/routers/private_work_routes/runs.py`
- Modify: `backend/app/gateway/routers/private_work.py`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Modify: `backend/tests/test_private_checkpoint_token_usage_projection.py`
- Modify: `backend/tests/test_private_stream_replay_compaction.py`
- Modify: `backend/tests/test_private_work_human_input_history.py`
- Modify: `backend/tests/test_private_work_stream_router.py`
- Modify: `backend/tests/test_run_event_notify_wakeup.py`
- Modify: `backend/tests/test_run_execution_state_route.py`
- Modify: `backend/tests/test_skill_builder_private_run_api.py`
- Modify: `backend/tests/test_skill_builder_durable_agent_postgres.py`

### Steps

- [ ] Add exact legacy/owner identity assertions for `_prepend_admitted_human_input_response` and `reconnect_private_run_stream`, then run them to observe the expected missing-owner failure.

- [ ] Move the complete durable stream protocol implementation atomically into the existing `streaming.py`; the only code already there is the shared poll constant moved in Task 3:

  - Keep the already-owned `_PRIVATE_STREAM_POLL_SECONDS`; move `_PRIVATE_STREAM_HEARTBEAT_SECONDS` and `_PRIVATE_STREAM_WAKEUP_WAIT_SECONDS` beside it.
  - `_private_stream_bridge`, `_require_run_runtime`, `_run_event_wakeup`.
  - `_private_stream_cursor`, `_private_stream_headers`.
  - `_await_stream_database_operation`, `_read_private_stream_page`, `_read_private_full_state_horizon`.
  - `_durable_private_sse_consumer`, `_persist_private_disconnect_cancel`, `_wait_for_durable_private_run`.

  Preserve cursor validation, replay horizon, terminal settlement, wakeup-as-hint/poll fallback, heartbeat, cancellation shielding, disconnect cancellation, and database-error mapping. Do not split this list across tasks.

- [ ] Create `runs.router = APIRouter(route_class=PrivateWorkRoute)` and move into `runs.py`:

  - `_public_event`, `_run_message_response`.
  - Event/checkpoint duration and token helpers from `_project_scoped_event_durations` through `_project_scoped_event_token_usage`.
  - `_admitted_failure_message`, `_prepend_admitted_human_input_response`.
  - `_private_rest_feed_cursor`, `_private_run_events`.
  - All 15 Run-family routes from `create_private_run` through `private_thread_token_usage`, including `reconnect_private_run_stream`.

  `runs.py` imports replay normalization one-way from `context_controls.py` and durable primitives from `streaming.py`. Neither module may import `threads.py` or the legacy façade.

- [ ] Replace the old Run decorator block with one `router.include_router(runs.router)` at the same registration position, after approvals and before feedback. Remove old stream/Run definitions only after the include exists; ensure there is never a duplicate registration state in a test run.

- [ ] Immediately re-export from `private_work.py` the complete repository-consumed compatibility inventory moved in this task:

  - From `streaming.py`: `_PRIVATE_STREAM_HEARTBEAT_SECONDS`, `_PRIVATE_STREAM_POLL_SECONDS`, `_PRIVATE_STREAM_WAKEUP_WAIT_SECONDS`, `_durable_private_sse_consumer`, `_private_stream_bridge`, `_private_stream_cursor`, `_read_private_stream_page`, `_require_run_runtime`, `_run_event_wakeup`, `_wait_for_durable_private_run`.
  - From `runs.py`: `_project_scoped_checkpoint_durations`, `_project_scoped_checkpoint_token_usage`, `_project_scoped_event_durations`, `_project_scoped_event_token_usage`, `_prepend_admitted_human_input_response`, `bind_scoped_checkpoint_state`, `list_private_run_events`, `list_private_run_messages`, `list_private_thread_messages`, `private_thread_token_usage`, `reconnect_private_run_stream`, `start_private_run`, `wait_private_run`.

  Keep these as aliases only; do not defer them to Task 5.

- [ ] Retarget test globals according to the executed owner:

  | Existing seam | New patch target |
  | --- | --- |
  | `start_private_run`, Run handlers, `_durable_private_sse_consumer` as imported by a handler | `private_work_routes.runs` |
  | `_private_stream_bridge`, `_read_private_stream_page`, stream cursor/wakeup/constants, direct durable-consumer tests | `private_work_routes.streaming` |
  | `_run_service`, `_browser_chat_run_service`, `_run_event_store`, `_run_store`, `_scoped_checkpointer` when resolved by a dependency helper | `private_work_routes.dependencies` or the exact importing Run-module global exercised by the test |
  | `_require_run_runtime`, `_wait_for_durable_private_run`, and `_durable_private_sse_consumer` when intercepting a Run handler call | the imported globals on `private_work_routes.runs` |
  | `_normalize_prepared_edit_replay` | `private_work_routes.context_controls` or `runs._normalize_prepared_edit_replay` when intercepting the Run call site |
  | event/checkpoint/token projection helpers and Run read handlers | `private_work_routes.runs` |

  In `test_private_checkpoint_token_usage_projection.py`, patch the imported call-site globals on `runs` for `_browser_chat_run_service`, `_run_event_store`, `_run_store`, `_require_run_runtime`, `_normalize_prepared_edit_replay`, `start_private_run`, `_wait_for_durable_private_run`, `_scoped_checkpointer`, `bind_scoped_checkpoint_state`, `_project_scoped_checkpoint_token_usage`, and `_project_scoped_checkpoint_durations`. Specifically update every file listed in this task; keep `legacy.router` for HTTP mounting so façade compatibility remains exercised. Do not create a façade wrapper to make old monkeypatches work.

- [ ] Run the entire offline Run/SSE characterization group:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_gateway_router_registration_contract.py \
    tests/test_private_work_stream_router.py \
    tests/test_run_event_notify_wakeup.py \
    tests/test_private_stream_replay_compaction.py \
    tests/test_run_execution_state_route.py \
    tests/test_private_checkpoint_token_usage_projection.py \
    tests/test_private_work_human_input_history.py \
    tests/test_run_workload_profile.py \
    tests/test_run_token_budget_usage.py \
    tests/test_run_execution_profile.py \
    tests/test_skill_builder_private_run_api.py \
    -q -m "not postgres and not provider_integration"
  ```

  Expected result: pass with the exact 45-route manifest and OpenAPI digests. Any cursor, terminal, disconnect, cancellation, wakeup, or replay failure blocks the task and requires whole-slice rollback.

- [ ] Run the directly related PostgreSQL test only when the non-production development database is available through the copied root environment:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run python tests/support/core_gate_plugin.py \
    tests/test_skill_builder_durable_agent_postgres.py \
    -q -m "not provider_integration"
  ```

  Expected result: pass with zero skips. This proves the selected durable Builder-linked Run path only; it is not full release evidence.

- [ ] Run focused Ruff, `make detect-blocking-io`, `git diff --check`, and inspect the protocol diff before proceeding.

- [ ] If explicitly authorized, commit only this atomic protocol slice with message `refactor(gateway): split private work run routes`. Otherwise leave it unstaged.

## Task 5: Complete Private Work leaf ownership and convert the legacy module to a façade

**Files:**

- Create: `backend/app/gateway/routers/private_work_routes/readiness.py`
- Create: `backend/app/gateway/routers/private_work_routes/feedback.py`
- Create: `backend/app/gateway/routers/private_work_routes/threads.py`
- Create: `backend/app/gateway/routers/private_work_routes/router.py`
- Modify: `backend/app/gateway/routers/private_work.py`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Modify: `backend/tests/test_execution_approval_api.py`
- Modify: `backend/tests/test_private_checkpoint_token_usage_projection.py`

### Steps

- [ ] Confirm the user approved this plan's explicit `readiness.py` amendment. If approval covered only the earlier directory sketch, stop before editing and request confirmation; do not choose a different owner silently.

- [ ] Add `test_private_work_router_facade_is_exact()` before final composition. It must assert:

  - `legacy.router is owner.router`.
  - Exact identities for `PRIVATE_THREAD_TITLE_MAX_LENGTH`, `PrivateThreadCreateRequest`, `PrivateThreadPatchRequest`, `PrivateThreadBranchRequest`, `ExecutionApprovalEnvelopeResponse`, `_run_response`, `_normalize_prepared_edit_replay`, `_context_projection_sse_consumer`, `_prepend_admitted_human_input_response`, and `reconnect_private_run_stream`.
  - `legacy.__all__ == ["router"]`.

  Run only this test and confirm it fails because `private_work_routes.router` does not yet exist.

- [ ] Create `readiness.router = APIRouter(route_class=PrivateWorkRoute)` and move only `get_private_work_readiness`. Keep its service construction, status projection, response model, and dependency unchanged.

- [ ] Create `feedback.router = APIRouter(route_class=PrivateWorkRoute)` and move `_upsert_private_feedback` plus GET/PUT/DELETE/deprecated-POST feedback routes. Preserve the deprecated POST status `201`, DELETE `204`, and shared PUT/POST helper.

- [ ] Create `threads.router = APIRouter(route_class=PrivateWorkRoute)` and move create/search/get/patch/delete/state routes. Import duration/token projection helpers one-way from `runs.py`; preserve `PRIVATE_SCOPE_MARKER` removal and state shaping in its current order.

- [ ] In `private_work_routes/router.py`, create the only combined Router and include leaves explicitly in this order:

  ```python
  from fastapi import APIRouter, Depends

  from app.gateway.deps import require_project_private_open
  from app.gateway.private_work_schemas import PrivateWorkRoute
  from app.gateway.routers.private_work_routes import (
      approvals,
      context_controls,
      feedback,
      files,
      readiness,
      runs,
      threads,
  )

  router = APIRouter(
      prefix="/api/projects/{project_id}/private-work",
      tags=["project-private-work"],
      route_class=PrivateWorkRoute,
      dependencies=[Depends(require_project_private_open)],
  )
  router.include_router(readiness.router)
  router.include_router(context_controls.router)
  router.include_router(files.router)
  router.include_router(approvals.router)
  router.include_router(runs.router)
  router.include_router(feedback.router)
  router.include_router(threads.router)
  ```

  `router.py` contains no endpoint handler, service lookup, error mapping, transaction, or stream logic.

- [ ] Replace `private_work.py` with explicit same-object re-exports only:

  ```python
  from app.gateway.routers.private_work_routes.contracts import (
      _POSTGRES_BIGINT_MAX,
      PRIVATE_THREAD_TITLE_MAX_LENGTH,
      ExecutionApprovalEnvelopeResponse,
      PrivateThreadBranchRequest,
      PrivateThreadCreateRequest,
      PrivateThreadPatchRequest,
      _public_run_metadata,
      _run_response,
  )
  from app.gateway.routers.private_work_routes.dependencies import (
      _browser_chat_run_service,
      _chat_control_service,
      _execution_approval_service,
      _feedback_service,
      _file_service,
      _file_streamer,
      _raise_http,
      _run_event_store,
      _run_service,
      _run_store,
      _runtime_dependency,
      _scoped_checkpointer,
      _thread_service,
  )
  from app.gateway.routers.private_work_routes.context_controls import (
      _context_projection_sse_consumer,
      _normalize_prepared_edit_replay,
      stream_private_thread_context_usage,
  )
  from app.gateway.routers.private_work_routes.router import router
  from app.gateway.routers.private_work_routes.runs import (
      _project_scoped_checkpoint_durations,
      _project_scoped_checkpoint_token_usage,
      _project_scoped_event_durations,
      _project_scoped_event_token_usage,
      _prepend_admitted_human_input_response,
      bind_scoped_checkpoint_state,
      list_private_run_events,
      list_private_run_messages,
      list_private_thread_messages,
      private_thread_token_usage,
      reconnect_private_run_stream,
      start_private_run,
      wait_private_run,
  )
  from app.gateway.routers.private_work_routes.streaming import (
      _PRIVATE_STREAM_HEARTBEAT_SECONDS,
      _PRIVATE_STREAM_POLL_SECONDS,
      _PRIVATE_STREAM_WAKEUP_WAIT_SECONDS,
      _durable_private_sse_consumer,
      _private_stream_bridge,
      _private_stream_cursor,
      _read_private_stream_page,
      _require_run_runtime,
      _run_event_wakeup,
      _wait_for_durable_private_run,
  )
  from app.gateway.routers.private_work_routes.threads import get_thread_state

  __all__ = ["router"]
  ```

  Set the module docstring to `"""Compatibility façade for Private Work Gateway routes."""`. Do not instantiate `APIRouter`, wrap a handler, keep mutable seam state, or emit warnings.

  Extend the final identity test to cover every alias in this snippet. Tests must patch owner/call-site modules after movement even though direct legacy imports remain available.

- [ ] Finish moving affected test calls to their owners: `get_thread_state` to `threads`; Run event/message/token functions to `runs`; direct approval response validation to `contracts`. Continue mounting the exact façade Router.

- [ ] Extend the dependency gate:

  - AST-scan `app/gateway/routers/private_work_routes/**/*.py` and reject absolute imports of `app.gateway.routers.private_work`.
  - In two fresh subprocesses, import owner then legacy and legacy then owner; assert `legacy.router is owner.router` in both orders.
  - Parse `private_work.py` and assert it contains no call whose function name is `APIRouter`.

- [ ] Run all Private Work focused offline tests from Tasks 2-5 plus the app-registration contract. Expected result: pass, 45 exact routes, unique operation IDs, and one app registration.

- [ ] Run focused PostgreSQL authorization/transaction tests when the non-production database is available:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run python tests/support/core_gate_plugin.py \
    tests/test_account_private_lifecycle_postgres.py \
    tests/test_execution_approval_private_lifecycle_postgres.py \
    tests/test_private_upload_discard_postgres.py \
    tests/test_skill_builder_durable_agent_postgres.py \
    -q -m "not provider_integration"
  ```

  Expected result: pass with zero skips; no transaction, lock, ownership, hidden-resource, upload-discard, approval, or durable Run regression.

- [ ] Run focused Ruff, `make detect-blocking-io`, `git diff --check`, `git status --short`, and inspect that `private_work.py` is re-export-only.

- [ ] If explicitly authorized, commit only this slice with message `refactor(gateway): compose private work router modules`. Otherwise leave it unstaged.

## Task 6: Extract Project Assets contracts, common HTTP policy, and shared projections

**Files:**

- Create: `backend/app/gateway/routers/project_asset_routes/__init__.py`
- Create: `backend/app/gateway/routers/project_asset_routes/contracts.py`
- Create: `backend/app/gateway/routers/project_asset_routes/common.py`
- Create: `backend/app/gateway/routers/project_asset_routes/mcp.py`
- Modify: `backend/app/gateway/routers/project_assets.py`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Modify: `backend/tests/test_skill_route_error_matrix.py`

### Steps

- [ ] Add `test_project_asset_contract_and_common_exports_are_exact()` first. Import inside the test and assert the legacy module exports the exact objects from their owners.

  Contract-owner names:

  ```python
  (
      "_StrictModel",
      "AgentAssetItemResponse",
      "AgentBindingResponse",
      "AgentCapabilityBindingsRequest",
      "AgentCreateRequest",
      "AgentDefinitionItemResponse",
      "AgentRuntimeAssessmentItemResponse",
      "AssetItemResponse",
      "BindingResponse",
      "CurrentBindingResponse",
      "CurrentSystemBindingRequest",
      "CurrentVersionAssetItemResponse",
      "DisableSystemBindingRequest",
      "MAX_SKILL_ARCHIVE_BASE64_CHARS",
      "MAX_SKILL_BASE64_FILE_CHARS",
      "McpVersionItemResponse",
      "MoveSystemBindingRequest",
      "ProjectAgentItemResponse",
      "ProjectAssetItemResponse",
      "ProjectCurrentVersionSkillItemResponse",
      "ScopedAgentAssetListResponse",
      "ScopedAssetListResponse",
      "ScopedCurrentVersionSkillAssetListResponse",
      "SkillAssetRefRequest",
      "SkillDeleteResponse",
      "SkillFileChangeRequest",
      "SkillVersionItemResponse",
      "SkillVersionResponse",
      "SystemBindingRequest",
  )
  ```

  Common-owner names:

  ```python
  (
      "ASSET_ERRORS",
      "AssetRoute",
      "MAX_SKILL_ARCHIVE_UPLOAD_BYTES",
      "_agent_asset_item",
      "_asset_item",
      "_binding_item_response",
      "_current_version_asset_item",
      "_read_skill_archive_upload",
      "_scoped_assets",
      "_version_call",
      "get_agent_runtime_assessment_service",
      "get_agent_service",
      "get_binding_service",
      "get_mcp_service",
      "get_skill_service",
      "project_asset_context",
      "raise_asset_domain",
  )
  ```

  Confirm the test fails with the expected missing-package import before production movement.

- [ ] Create an empty package marker with no eager import side effects.

- [ ] Move all request/response Pydantic definitions from `_StrictModel` through `McpToolDiscoveryAttemptResponse` into `contracts.py`. Move `MAX_SKILL_BASE64_FILE_CHARS`, `MAX_SKILL_ARCHIVE_BASE64_CHARS`, and the discriminated-union alias `SkillFileChangeRequest` with the surrounding Skill contracts. Preserve class/alias names, constant formulas, field order/defaults, validators, strictness, inheritance, and schemas. Move `_binding_item_response` to `common.py`, not `contracts.py`, because it projects a domain view.

- [ ] Move into `common.py` as single implementations:

  - `AssetRoute`.
  - `ASSET_ERRORS` and `raise_asset_domain` with the same status/code/message/request-id and `Retry-After` behavior.
  - `authenticated_asset_identity`, `system_asset_catalog_actor`, `asset_session`, `project_asset_context`.
  - `_factory`, `_governance_sink`, `_agent_tool_group_catalog`, and all service dependency getters.
  - `_asset_item`, `_current_version_asset_item`, `_agent_asset_item`, `_agent_definition_response`.
  - `_asset_item_capabilities`, `_scoped_assets`, `_list_assets`, `_asset_call`, `_current_version_asset_call`, `_agent_definition_call`, `_version_call`, `_version_history`.
  - `_is_project_asset_actor`, `_redacted_project_mcp_url`, `_editable_project_mcp_url`, `_response_data`.
  - `_decode_skill_files`, `_read_skill_archive_upload` and `MAX_SKILL_ARCHIVE_UPLOAD_BYTES`.

  Import `MAX_SKILL_ARCHIVE_BASE64_CHARS` from `contracts.py` for `_decode_skill_files`; do not recompute it in `common.py`. The MCP response projection must continue to clear `env` and `headers` for every `McpDefinition`; Project MCP responses must also clear command, args, OAuth, routing, and tool overrides and expose only the currently safe URL form. Do not copy these functions into `mcp.py`, `catalog.py`, or admin routes.

- [ ] Create `mcp.py` with only `_mcp_definition` in this task. Move the request-to-domain conversion unchanged so Task 7's dynamic route registrar can depend on an owner module without importing the legacy façade. Task 9 will add MCP route fragments to this same file; it must continue using the one common redaction implementation.

- [ ] Keep `_configured_mcp_response`, `_mcp_secret_response`, `_binding_response`, `register_asset_routes`, both Routers, and all route handlers in the legacy module for now. Replace moved definitions with direct imports from `contracts.py`, `common.py`, and `mcp.py`; remaining legacy code must call the imported exact objects.

- [ ] Update only the archive-size seam in `test_skill_route_error_matrix.py`: import `project_asset_routes.common` and patch `common.MAX_SKILL_ARCHIVE_UPLOAD_BYTES` before calling `common._read_skill_archive_upload`. A patch on the legacy constant would no longer change the moved function's globals.

- [ ] Run the exact compatibility, redaction, error, and consumer suite:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_gateway_router_registration_contract.py \
    tests/test_current_version_api_contract.py \
    tests/test_agent_http_contract.py \
    tests/test_agent_runtime_assessment.py \
    tests/test_skill_create_route_contract.py \
    tests/test_skill_route_error_matrix.py \
    tests/test_mcp_secret_route_contract.py \
    tests/test_agent_builder_contract_version.py \
    tests/test_agent_builder_safety_lifecycle.py \
    tests/test_skill_builder_design_record.py \
    tests/test_skill_builder_revision_contract.py \
    -q -m "not postgres and not provider_integration"
  ```

  Expected result: pass; project/catalog/admin route digests remain exact, the common error envelope is unchanged, and no secret-bearing response projection changes.

- [ ] Run focused Ruff, `make detect-blocking-io`, `git diff --check`, and inspect that `common.py` has no dependency on the legacy module or route-family modules.

- [ ] If explicitly authorized, commit only this slice with message `refactor(gateway): extract project asset contracts`. Otherwise leave it unstaged.

## Task 7: Move `register_asset_routes()` as one unchanged dynamic-registration unit

**Files:**

- Create: `backend/app/gateway/routers/project_asset_routes/router.py`
- Modify: `backend/app/gateway/routers/project_assets.py`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`

### Steps

- [ ] Add `test_register_asset_routes_is_an_exact_facade_export()` before moving the function:

  ```python
  def test_register_asset_routes_is_an_exact_facade_export() -> None:
      from app.gateway.routers import project_assets as legacy
      from app.gateway.routers.project_asset_routes import router as owning

      assert legacy.register_asset_routes is owning.register_asset_routes
  ```

  Run it alone and confirm the expected missing-owner failure.

- [ ] Create `project_asset_routes/router.py` initially with `register_asset_routes()` and its required imports, including `_mcp_definition` from `mcp.py`. Move the complete function from its definition through the final status-route registration without changing:

  - Signature or defaults.
  - Inner endpoint function names/signatures.
  - Five read routes, nine optional write routes, optional export, three optional deletes, or seven status routes.
  - Tuple order, methods, response models, status codes, explicit names, closure behavior, or `add_api_route()` order.

  This task must not yet create the final `project_router`; the legacy module continues to own the root Router until every explicit route family is movable.

- [ ] Import `register_asset_routes` back into `project_assets.py` and leave all three calls unchanged:

  - Project: `include_project_asset_delete=True`.
  - Admin: `include_shared_asset_mutations=False` through the legacy import.
  - Admin project: `include_skill_export=False` through the legacy import.

  `admin_assets.py` must not change in this task.

- [ ] Run the isolated 25/6/21 switch tests, the 58/5/10/32 full Router manifests, OpenAPI checks, app registration, and admin consumers:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_gateway_router_registration_contract.py \
    tests/test_admin_agent_http_contract.py \
    tests/test_system_skill_revocation_route.py \
    tests/test_skill_export_route_contract.py \
    -q -m "not postgres and not provider_integration"
  ```

  Expected result: pass with the exact dynamic and composed digests. A route-count/order change requires rollback of the entire function move; do not edit individual dynamic routes to compensate.

- [ ] Run focused Ruff, `make detect-blocking-io`, `git diff --check`, and compare the moved function body against its pre-task version.

- [ ] If explicitly authorized, commit only this slice with message `refactor(gateway): move asset route registration`. Otherwise leave it unstaged.

## Task 8: Extract Asset Catalog, Agent, and Skill route families while preserving interleaving

**Files:**

- Create: `backend/app/gateway/routers/project_asset_routes/catalog.py`
- Create: `backend/app/gateway/routers/project_asset_routes/agents.py`
- Create: `backend/app/gateway/routers/project_asset_routes/skills.py`
- Modify: `backend/app/gateway/routers/project_assets.py`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`

### Steps

- [ ] Add `test_asset_catalog_router_facade_is_exact()` before creating `catalog.py`; assert `legacy.catalog_router is catalog.catalog_router`. Confirm the expected import failure.

- [ ] Move the catalog Router construction, `_list_system_catalog`, `_list_system_current_version_catalog`, `_list_system_agent_catalog`, and the five catalog handlers into `catalog.py`. These three list wrappers are catalog-specific orchestration; `common.py` continues to own only shared dependencies/projections. The Router remains the single object:

  ```python
  catalog_router = APIRouter(
      prefix="/api/assets/catalog",
      tags=["asset-catalog"],
      route_class=AssetRoute,
  )
  ```

  Preserve route order: agents list; skills list; agent get; skill versions; MCP servers list.

- [ ] Create `agents.router = APIRouter(route_class=AssetRoute)` and move the four contiguous project routes in current positions 10-13: agent list, runtime assessment, project default Agent GET, project default Agent PUT. Keep service dependencies and `ProjectContext` authority unchanged.

- [ ] Because Skill routes are interleaved around Agent routes, create two child fragments in the one owning module:

  ```python
  primary_router = APIRouter(route_class=AssetRoute)
  listing_router = APIRouter(route_class=AssetRoute)
  ```

  Move to `primary_router` current routes 1-9: file-content preview, fork, archive import, activation readiness, version/project secret reads/replacements, and secret clear. Move only route 14, project Skill list, to `listing_router`. Move `_skill_secret_response` with the Skill routes. Preserve archive closing, secret-safe response shape, `Cache-Control`, and `X-Content-Type-Options` behavior.

- [ ] In the still-legacy `project_router`, replace the removed blocks at their exact order positions:

  ```python
  project_router.include_router(skills.primary_router)
  project_router.include_router(agents.router)
  project_router.include_router(skills.listing_router)
  ```

  Keep existing MCP routes immediately after these includes, then bindings, `register_asset_routes()`, and MCP discovery. Every child must use `AssetRoute`; no prefix belongs on a project child fragment.

- [ ] Add exact identity assertions for repository-consumed moved contracts/helpers only; leave the legacy module as the import path for production consumers during this task.

- [ ] Run catalog, Agent, Skill, admin, and structural tests:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_gateway_router_registration_contract.py \
    tests/test_agent_definition_contract.py \
    tests/test_agent_http_contract.py \
    tests/test_agent_runtime_assessment.py \
    tests/test_current_version_api_contract.py \
    tests/test_skill_create_route_contract.py \
    tests/test_skill_route_error_matrix.py \
    tests/test_skill_export_route_contract.py \
    tests/test_skill_frontmatter_route_contract.py \
    tests/test_skill_logical_delete_contract.py \
    tests/test_admin_agent_http_contract.py \
    -q -m "not postgres and not provider_integration"
  ```

  Expected result: catalog 5 and project 58 stay exact; project route positions 1-14 remain unchanged and app registration stays singular.

- [ ] Run focused Ruff, `make detect-blocking-io`, `git diff --check`, and inspect that no Skill/Agent handler was behaviorally rewritten.

- [ ] If explicitly authorized, commit only this slice with message `refactor(gateway): split asset catalog routes`. Otherwise leave it unstaged.

## Task 9: Extract MCP and Binding routes, compose the final Project Router, and finish the façade

**Files:**

- Create: `backend/app/gateway/routers/project_asset_routes/mcp.py`
- Create: `backend/app/gateway/routers/project_asset_routes/bindings.py`
- Modify: `backend/app/gateway/routers/project_asset_routes/router.py`
- Modify: `backend/app/gateway/routers/project_assets.py`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Modify: `backend/tests/test_mcp_secret_route_contract.py`

### Steps

- [ ] Add `test_project_asset_router_facade_is_exact()` before final composition. It must assert:

  - `legacy.project_router is owning.project_router`.
  - `legacy.catalog_router is catalog.catalog_router`.
  - `legacy.register_asset_routes is owning.register_asset_routes`.
  - `legacy._mcp_secret_response is mcp._mcp_secret_response`.
  - `legacy._binding_response is bindings._binding_response`.

  Confirm the expected failure because the final owner Router/MCP/Binding modules are incomplete.

- [ ] In `mcp.py`, create two `AssetRoute` child fragments to preserve the current interleaving:

  ```python
  configuration_router = APIRouter(route_class=AssetRoute)
  discovery_router = APIRouter(route_class=AssetRoute)
  ```

  Move to `configuration_router` routes 15-21: project MCP list, configured create/update/get, secret get/replace/clear. Keep the already-owned `_mcp_definition` and move `_configured_mcp_response` and `_mcp_secret_response` beside it. Move to `discovery_router` routes 56-58 plus `_mcp_tool_discovery_attempt_response`.

  Continue delegating all recursive response masking and URL redaction to `common._response_data`, `_redacted_project_mcp_url`, and `_editable_project_mcp_url`. MCP secret responses must expose only slot schema/status/revision and never payload.

- [ ] In `bindings.py`, move `_BINDING_KINDS`, `_binding_response`, and the complete dynamic binding registration. Change the existing helper signature to `def _register_binding_routes(router: APIRouter, segment: str, kind: AssetKind) -> None:` and preserve its full body. Add this complete outer registrar:

  ```python
  def register_binding_routes(router: APIRouter) -> None:
      for segment, kind in _BINDING_KINDS.items():
          _register_binding_routes(router, segment, kind)
  ```

  Inside the moved helper, replace only `project_router.add_api_route` with `router.add_api_route`. Keep nested endpoint names, dependencies, status, response model, path, name, and Agent/Skill/MCP branching unchanged.

- [ ] Complete `project_asset_routes/router.py` by retaining the unchanged `register_asset_routes()` definition and adding the one combined Router plus exact composition order:

  ```python
  project_router = APIRouter(
      prefix="/api/projects/{project_id}",
      tags=["project-assets"],
      route_class=AssetRoute,
  )
  project_router.include_router(skills.primary_router)
  project_router.include_router(agents.router)
  project_router.include_router(skills.listing_router)
  project_router.include_router(mcp.configuration_router)
  bindings.register_binding_routes(project_router)
  register_asset_routes(
      project_router,
      project_asset_context,
      include_project_asset_delete=True,
  )
  project_router.include_router(mcp.discovery_router)
  ```

  This order must produce routes 1-58 exactly. No leaf imports this module.

- [ ] Replace `project_assets.py` with explicit compatibility imports from `contracts`, `common`, `bindings`, `mcp`, `catalog`, and `router`. Preserve this exact repository-consumed inventory:

  - From `contracts`: `_StrictModel`, `AgentAssetItemResponse`, `AgentBindingResponse`, `AgentCapabilityBindingsRequest`, `AgentCreateRequest`, `AgentDefinitionItemResponse`, `AgentRuntimeAssessmentItemResponse`, `AssetItemResponse`, `BindingResponse`, `CurrentBindingResponse`, `CurrentSystemBindingRequest`, `CurrentVersionAssetItemResponse`, `DisableSystemBindingRequest`, `MAX_SKILL_ARCHIVE_BASE64_CHARS`, `MAX_SKILL_BASE64_FILE_CHARS`, `McpVersionItemResponse`, `MoveSystemBindingRequest`, `ProjectAgentItemResponse`, `ProjectAssetItemResponse`, `ProjectCurrentVersionSkillItemResponse`, `ScopedAgentAssetListResponse`, `ScopedAssetListResponse`, `ScopedCurrentVersionSkillAssetListResponse`, `SkillAssetRefRequest`, `SkillDeleteResponse`, `SkillFileChangeRequest`, `SkillVersionItemResponse`, `SkillVersionResponse`, `SystemBindingRequest`.
  - From `common`: `ASSET_ERRORS`, `AssetRoute`, `MAX_SKILL_ARCHIVE_UPLOAD_BYTES`, `_agent_asset_item`, `_asset_item`, `_binding_item_response`, `_current_version_asset_item`, `_read_skill_archive_upload`, `_scoped_assets`, `_version_call`, `get_agent_runtime_assessment_service`, `get_agent_service`, `get_binding_service`, `get_mcp_service`, `get_skill_service`, `project_asset_context`, `raise_asset_domain`.
  - From `bindings`: `_binding_response`.
  - From `mcp`: `_mcp_secret_response`.
  - From `catalog`: `catalog_router`.
  - From `router`: `project_router`, `register_asset_routes`.

  Set the module docstring to `"""Compatibility façade for Project Assets Gateway routes."""`. Do not define a new `__all__`, instantiate a Router, retain dynamic registration calls, wrap functions, duplicate constants, or emit warnings.

- [ ] Update `test_mcp_secret_route_contract.py` to import the owner helper and separately assert the legacy alias is the exact same object. Do not alter the safe expected JSON.

- [ ] Add final cycle/façade structure gates:

  - AST-scan `project_asset_routes/**/*.py` and reject imports of `app.gateway.routers.project_assets`.
  - Fresh subprocess owner-first and legacy-first imports must prove both Router identities.
  - Parse `project_assets.py` and assert it contains no `APIRouter` construction and no call to `register_asset_routes`.

- [ ] Run the complete Project Assets focused offline suite:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_gateway_router_registration_contract.py \
    tests/test_agent_builder_contract_version.py \
    tests/test_agent_builder_safety_lifecycle.py \
    tests/test_agent_definition_contract.py \
    tests/test_agent_http_contract.py \
    tests/test_agent_runtime_assessment.py \
    tests/test_current_version_api_contract.py \
    tests/test_legacy_run_skill_snapshot_writer.py \
    tests/test_mcp_secret_route_contract.py \
    tests/test_skill_builder_design_record.py \
    tests/test_skill_builder_revision_contract.py \
    tests/test_skill_create_route_contract.py \
    tests/test_skill_export_route_contract.py \
    tests/test_skill_frontmatter_route_contract.py \
    tests/test_skill_logical_delete_contract.py \
    tests/test_skill_route_error_matrix.py \
    tests/test_admin_agent_http_contract.py \
    tests/test_system_skill_revocation_route.py \
    -q -m "not postgres and not provider_integration"
  ```

  Expected result: pass; project/catalog/admin manifests and OpenAPI digests stay exact, every dynamic route appears once, and secret/error projections are unchanged.

- [ ] Run selected Project Asset PostgreSQL gates when the non-production database is available:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run python tests/support/core_gate_plugin.py \
    tests/test_agent_archive_run_postgres.py \
    tests/test_skill_secret_lifecycle_postgres.py \
    tests/test_skill_logical_delete_postgres.py \
    tests/test_skill_runtime_name_conflicts_postgres.py \
    tests/test_mcp_secret_lifecycle_postgres.py \
    -q -m "not provider_integration"
  ```

  Expected result: pass with zero skips; authorization, lifecycle, secret generation/masking, deletion, conflict, transaction, and rollback behavior remains unchanged.

- [ ] Run focused Ruff, `make detect-blocking-io`, `git diff --check`, and inspect that the final façade contains imports only.

- [ ] If explicitly authorized, commit only this slice with message `refactor(gateway): compose project asset routers`. Otherwise leave it unstaged.

## Task 10: Migrate internal production imports after both façades are stable

**Files:**

- Modify: `backend/app/gateway/app.py`
- Modify: `backend/app/gateway/routers/admin_assets.py`
- Modify: `backend/app/gateway/routers/project_agent_builder.py`
- Modify: `backend/app/gateway/routers/project_channel_group_bindings.py`
- Modify: `backend/app/gateway/routers/project_channel_instances.py`
- Modify: `backend/app/gateway/routers/project_skill_builder.py`
- Modify: `backend/app/gateway/routers/project_skill_frontmatter.py`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`

### Preconditions

Do not start this task until Tasks 5 and 9 are accepted with all focused offline, app-registration, identity, and cycle tests green. The façades remain permanently available in this batch; this task only removes internal production dependence after stability.

### Steps

- [ ] Add `test_gateway_production_imports_use_owner_modules()` as a static assertion that `backend/app/**` no longer imports `app.gateway.routers.private_work` or `app.gateway.routers.project_assets`, excluding the two façade files themselves. The AST check must detect both direct submodule imports and names imported from the package:

  ```python
  def _legacy_gateway_router_imports(root: Path) -> list[tuple[Path, str]]:
      legacy_names = {"private_work", "project_assets"}
      findings: list[tuple[Path, str]] = []
      for path in root.rglob("*.py"):
          if path.name in {"private_work.py", "project_assets.py"}:
              continue
          tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
          for node in ast.walk(tree):
              if isinstance(node, ast.Import):
                  for alias in node.names:
                      if alias.name in {
                          "app.gateway.routers.private_work",
                          "app.gateway.routers.project_assets",
                      }:
                          findings.append((path, alias.name))
              elif isinstance(node, ast.ImportFrom):
                  if node.module in {
                      "app.gateway.routers.private_work",
                      "app.gateway.routers.project_assets",
                  }:
                      findings.append((path, node.module))
                  elif node.module == "app.gateway.routers":
                      for alias in node.names:
                          if alias.name in legacy_names:
                              findings.append((path, alias.name))
      return findings
  ```

  Assert the returned list is empty. Confirm this test fails before changing consumers.

- [ ] In `app.py`, import the combined Routers from their owners and keep include order unchanged:

  - `private_work_routes.router.router` at the existing Private Work position.
  - `project_asset_routes.catalog.catalog_router` then `project_asset_routes.router.project_router` at the existing Project Assets positions.

  Do not move any neighboring Router include, especially Automation readiness, admin, model registry, or Knowledge includes.

- [ ] Replace `admin_assets.py`'s 33-name legacy import with owner imports:

  - `contracts.py`: all request/response models and `_StrictModel`.
  - `common.py`: `ASSET_ERRORS`, `AssetRoute`, generic item projections, `_binding_item_response`, `_version_call`, service getters, and `raise_asset_domain`.
  - `bindings.py`: `_binding_response`.
  - `router.py`: `register_asset_routes`.

  Keep both admin registration calls and their source order exactly where they are.

- [ ] Migrate the remaining production consumers mechanically:

  - `project_agent_builder.py`: contracts from `contracts.py`; `ASSET_ERRORS`, `AssetRoute`, `project_asset_context`, and `raise_asset_domain` from `common.py`.
  - `project_skill_builder.py`: response contracts from `contracts.py`; shared HTTP/context names from `common.py`.
  - `project_skill_frontmatter.py`: shared HTTP/context names from `common.py`.
  - `project_channel_group_bindings.py` and `project_channel_instances.py`: `project_asset_context` from `common.py`.

  Change imports only. Do not rename local names or alter route/service bodies.

- [ ] Keep compatibility tests importing the legacy modules and asserting exact identity. Passing production-import checks is not authorization to delete either façade.

- [ ] Run all structural, app-registration, admin, Builder, channel, and frontmatter focused tests:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_gateway_router_registration_contract.py \
    tests/test_admin_agent_http_contract.py \
    tests/test_agent_builder_contract_version.py \
    tests/test_agent_builder_safety_lifecycle.py \
    tests/test_skill_builder_design_record.py \
    tests/test_skill_builder_revision_contract.py \
    tests/test_skill_frontmatter_route_contract.py \
    tests/test_system_skill_revocation_route.py \
    tests/test_project_connection_authorization.py \
    tests/test_channel_group_binding_account_lifecycle.py \
    tests/test_channel_group_binding_agent_release.py \
    -q -m "not postgres and not provider_integration"
  ```

  Expected result: pass; full app includes the same objects once, admin dynamic registration is unchanged, and direct owner imports introduce no cycle.

- [ ] Verify production imports and façade retention:

  ```bash
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py::test_gateway_production_imports_use_owner_modules \
    -q
  ```

  Expected result: pass with no AST findings. Then run both fresh-process identity orders again.

- [ ] Run focused Ruff, `make detect-blocking-io`, `git diff --check`, and inspect import-only diffs.

- [ ] If explicitly authorized, commit only these consumer-import files and the contract-test assertion with message `refactor(gateway): use router owner modules`. Otherwise leave them unstaged.

## Task 11: Document ownership and run the complete Batch 2 verification

**Files:**

- Modify: `README.md`
- Modify: `backend/AGENTS.md`
- Verify: every file changed in Tasks 1-10

### Steps

- [ ] Add one concise paragraph to `README.md` under `## 运行架构` after the service responsibility description:

  > Gateway 的 Private Work 与 Project Assets HTTP 路由按资源族组织在 `private_work_routes/` 与 `project_asset_routes/`；旧的 `private_work.py`、`project_assets.py` 仅保留兼容导入。该组织方式不改变 Gateway、Worker、Scheduler 的运行职责。

- [ ] Add a focused ownership rule to `backend/AGENTS.md` under `## Where changes live`:

  > Private Work and Project Assets Gateway handlers are owned by `app/gateway/routers/private_work_routes/` and `app/gateway/routers/project_asset_routes/`. Their sibling `.py` modules are compatibility façades only. Add handlers to the owning resource module, compose them once in `router.py`, preserve registration order, and patch the owning module in focused tests.

  Do not turn either guide into a task changelog.

- [ ] Run the combined offline Router suite exactly once from the backend directory:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_gateway_router_registration_contract.py \
    tests/test_private_work_stream_router.py \
    tests/test_private_stream_replay_compaction.py \
    tests/test_private_compact_api.py \
    tests/test_private_context_projection_v2_api.py \
    tests/test_private_context_projection_stream.py \
    tests/test_execution_approval_api.py \
    tests/test_private_upload_discard_api.py \
    tests/test_run_execution_state_route.py \
    tests/test_private_checkpoint_token_usage_projection.py \
    tests/test_private_work_human_input_history.py \
    tests/test_run_event_notify_wakeup.py \
    tests/test_agent_http_contract.py \
    tests/test_agent_definition_contract.py \
    tests/test_agent_runtime_assessment.py \
    tests/test_current_version_api_contract.py \
    tests/test_skill_create_route_contract.py \
    tests/test_skill_route_error_matrix.py \
    tests/test_skill_export_route_contract.py \
    tests/test_skill_logical_delete_contract.py \
    tests/test_mcp_secret_route_contract.py \
    tests/test_admin_agent_http_contract.py \
    tests/test_system_skill_revocation_route.py \
    -q -m "not postgres and not provider_integration"
  ```

  Expected result: pass. Record the exact count and duration; do not substitute an earlier task result.

- [ ] Run the selected PostgreSQL authorization/lifecycle/transaction suite through the core gate plugin:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run python \
    tests/support/core_gate_plugin.py \
    tests/test_account_private_lifecycle_postgres.py \
    tests/test_execution_approval_private_lifecycle_postgres.py \
    tests/test_private_upload_discard_postgres.py \
    tests/test_skill_builder_durable_agent_postgres.py \
    tests/test_agent_archive_run_postgres.py \
    tests/test_skill_secret_lifecycle_postgres.py \
    tests/test_skill_logical_delete_postgres.py \
    tests/test_skill_runtime_name_conflicts_postgres.py \
    tests/test_mcp_secret_lifecycle_postgres.py \
    -q -m "not provider_integration"
  ```

  Expected result: pass with zero skips against random disposable `deerflow_test_*` databases. Never point this at a production database.

- [ ] Run the direct import/OpenAPI smoke without starting lifespan services:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run python scripts/run_runtime.py -- python -c '
  from app.gateway.app import app
  from app.gateway.routers import private_work, project_assets
  from app.gateway.routers.private_work_routes import router as private_owner
  from app.gateway.routers.project_asset_routes import catalog, router as asset_owner
  assert private_work.router is private_owner.router
  assert project_assets.project_router is asset_owner.project_router
  assert project_assets.catalog_router is catalog.catalog_router
  assert app.openapi()["paths"]
  '
  ```

  Expected result: exit 0. This proves import/construction/OpenAPI only and necessarily imports Knowledge; it does not prove Gateway lifespan or external infrastructure.

- [ ] Run the repository-required backend gates in this order:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  make lint
  make detect-blocking-io
  make test
  ```

  Expected result: all pass, and `make test` reports zero skips. Record exact output. The full suite requires the repository's configured non-production PostgreSQL and Knowledge test infrastructure; if that infrastructure is unavailable, report the gate as unverified rather than claiming completion.

- [ ] Run the Gateway lifespan/startup contract against intentionally configured non-production infrastructure.

  Terminal 1:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  GATEWAY_WORKERS=1 PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
    uv run python scripts/run_runtime.py -- \
    python -m uvicorn app.gateway.app:app \
    --host 127.0.0.1 --port 8001 --workers 1
  ```

  Terminal 2 after readiness:

  ```bash
  curl --fail --silent http://127.0.0.1:8001/health
  ```

  Expected health result: `{"status":"healthy","service":"deer-flow-gateway"}`. After the successful curl, return to Terminal 1, send `Ctrl-C`/`SIGINT`, wait for Uvicorn and `run_runtime.py` to exit, and require a clean shutdown status before closing the gate. Do not leave a Gateway process running. Do not treat import/OpenAPI as live startup evidence and do not alter Knowledge to make this Router batch pass. If required PostgreSQL/Knowledge infrastructure is unavailable, the code can be handed off as implemented, but Batch 2 remains verification-pending unless the user explicitly accepts that unverified environment gate.

- [ ] Perform final structural review:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations
  git diff --check
  git status --short
  git diff --stat
  rg -n '[T]ODO|[T]BD|[s]imilar to Task|[w]rite tests for above|[e]tc\.' \
    docs/superpowers/plans/2026-09-02-python-module-decomposition-gateway-routes.md
  rg -n '^[[:space:]]*[.][.][.][[:space:]]*$' \
    docs/superpowers/plans/2026-09-02-python-module-decomposition-gateway-routes.md
  ```

  Expected result: clean diff check; status contains only intended Batch 2 files plus preserved user-owned documentation; the unresolved-marker scan returns no matches.

- [ ] Use `superpowers:requesting-code-review` for a final independent review. Require the reviewer to verify:

  - Scope is only Batch 2.
  - Both façades export exact owner objects and create no Router.
  - 45/58/5 and 10/32 manifests, all OpenAPI digests, dynamic 25/6/21 manifests, and app singular-registration gates pass.
  - No owner imports a façade and both fresh import orders pass.
  - No MCP/Skill secret, URL redaction, error envelope, SSE protocol, authorization, transaction, or process boundary changed.
  - Tests were migrated only with their owning modules; no general test reorganization occurred.

- [ ] Resolve every review finding and rerun the smallest affected focused group plus the final structural gates. Do not mark the plan complete with unresolved findings.

- [ ] If explicit local-commit authorization exists, make one documentation/verification checkpoint commit with message `docs(gateway): document router ownership`. Otherwise leave all changes unstaged for user review.

## Completion Criteria

Batch 2 is complete only when all of the following are true:

- `private_work.py` and `project_assets.py` are re-export-only compatibility façades and retain required import identities.
- Private Work has one combined Router with 45 routes in the exact frozen order.
- Project Assets has one combined project Router with 58 routes and one catalog Router with 5 routes in the exact frozen order.
- Admin Routers retain 10 and 32 routes; the three `register_asset_routes()` switch shapes retain 25, 6, and 21 routes.
- OpenAPI operation IDs/path-methods and the composed app's single-registration/uniqueness checks pass.
- All leaf Routers retain the correct custom route class and authorization dependency objects.
- MCP redaction/secret projection and asset error mapping each have one implementation.
- Durable SSE replay/cursor/terminal/disconnect behavior remains unchanged.
- Internal production consumers import owners only, while legacy façades remain available and tested.
- Focused offline, selected PostgreSQL, lint, blocking-I/O, full backend, and live Gateway lifespan/health gates have current passing evidence. Without live startup evidence, implementation may be ready for review but Batch 2 is verification-pending unless the user explicitly accepts the stated environment limitation.
- No Knowledge, schema, Worker, Scheduler, Sandbox, frontend, deployment, or unrelated test refactor appears in the diff.

## Execution Handoff

After the user approves this plan, offer exactly these execution choices:

1. **Subagent-Driven (recommended):** execute sequential tasks in this session with a fresh implementer and two review passes per task.
2. **Inline Execution:** execute the same checkboxes directly in the current session with the same task boundaries and gates.

Do not begin implementation merely because the plan file exists.
