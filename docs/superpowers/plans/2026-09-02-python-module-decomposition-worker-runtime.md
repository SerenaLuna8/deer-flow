# Worker Execution Module Decomposition (Batch 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 3,515-line Harness Worker module and the 1,345-line private Run executor into explicit stream-delivery, runtime-binding, goal-continuation, checkpoint-rollback, preparation, and outcome-mapping owners while preserving `run_agent()` as the sole orchestrator of preparation, graph streaming, terminal priority, and cleanup, and `RunAgentPrivateExecutor` as the sole owner of lease boundary, Worker invocation, exception priority, and final cleanup.

**Architecture:** Move existing top-level Worker helpers into four leaf modules under `deerflow.runtime.runs`, then extract four contiguous non-terminal phases of `run_agent()` into typed phase functions; `worker.py` keeps every legacy private name importable and keeps calling the monkeypatched seams from its own globals. On the application side, `preparation.py` freezes policy/models, materializes assets, and builds File Authority, Checkpointer, and `RunContext` behind frozen dataclasses; `outcome_mapping.py` owns pure usage/outcome mapping; `executor.py` retains boundary construction, record registration, the runner call, the exception ladder, and `finally` cleanup in their current order.

**Tech Stack:** Python 3.12+, LangGraph `astream`/`Runtime`/checkpoint state, asyncio, dataclasses, SQLAlchemy async sessions (executor only), pytest, Ruff, repository Make targets.

**Spec:** `docs/superpowers/specs/2026-09-02-python-module-decomposition-design.md`, sections 1-6, 12, and 14-18.

## Global Constraints

### Confirmed execution baseline

- Generate and execute this plan only in `/Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations` on branch `codex/python-module-decomposition-foundations`. This plan does not authorize another branch or worktree.
- The audited Batch 5 production baseline is commit `eb3dd904df46dd08534fd9aff1a23cd93c72a33e` plus the accepted Batch 4 changes currently present in the worktree (sixteen modified tracked files, the untracked `backend/packages/harness/deerflow/sandbox/tooling/` package, and `backend/tests/test_python_module_decomposition_sandbox_tools.py`). Before Task 1, confirm Batch 4 is either committed on this branch or still present byte-for-byte; never sweep Batch 4 paths, the P1 Skill Builder PostgreSQL test change, or the five user-owned untracked documents into a Batch 5 commit.
- `backend/packages/harness/deerflow/runtime/runs/worker.py` is exactly 3,515 lines and byte-unchanged from design base `0d421ede2d52a2cf22a5c8fedfdfbb10e6e1394c`: 47 top-level functions, 9 top-level classes, ten module constants plus `logger`, no `__all__`. `run_agent()` spans lines 894-2182 (1,289 lines).
- `backend/app/reliability/run_execution/executor.py` is 1,345 lines and differs from the design base only by the two Batch 3 import changes in commits `716a9af5` and `85d44b03` (`SkillDesignDraftSink`, `execution_approval_policy`, `execution_approval_worker`). `_execute_with_trace()` spans lines 661-1342 (682 lines). `__all__ == ["RunAgentPrivateExecutor"]`.
- No `stream_delivery.py`, `runtime_binding.py`, `goal_continuation.py`, `checkpoint_rollback.py`, `preparation.py`, or `outcome_mapping.py` exists yet.
- Root `.env` and `config.yaml` are present and ignored. Never print their values. Use `uv run --env-file ../.env` for database-backed gates unless the invoking shell already exports the same approved non-production environment.
- The focused Batch 5 offline baseline for the 23-file set listed in Task 1 Step 5 is `438 passed, 7 deselected in 24.10s`. The selected PostgreSQL gate in Task 10 Step 3 currently reports `collected=4 passed=4 failed=0 skipped=0` in 3.48s. These are plan-generation baselines, not completion evidence; every task requires fresh results.
- The full backend gate takes about 16 minutes. Tasks 1-9 use focused gates; Task 10 runs the full backend gate once and repeats it only if a later fix invalidates that evidence.

### Scope boundary

- Production code first. Tests may move imports and monkeypatch targets only with their corresponding production owner, and may add focused compatibility or characterization coverage. Do not split `test_run_worker_rollback.py`, `test_run_event_text_batching.py`, or any other large test file; do not reorganize test directories or the test framework.
- Batch 5 production scope is limited to:
  - `backend/packages/harness/deerflow/runtime/runs/worker.py`;
  - new `backend/packages/harness/deerflow/runtime/runs/{checkpoint_rollback,stream_delivery,runtime_binding,goal_continuation}.py`;
  - `backend/app/reliability/run_execution/executor.py`;
  - new `backend/app/reliability/run_execution/{outcome_mapping,preparation}.py`;
  - two ownership docstring lines in `deerflow/runtime/serialization.py` and `deerflow/subagents/step_events.py`;
  - focused tests and `backend/AGENTS.md`.
- Do not modify `runtime/runs/{manager,schemas,naming,private_file_lifecycle,execution_contracts}.py`, `deerflow/runtime/goal.py`, `deerflow/runtime/checkpoint_state.py`, `host_execution_runner.py`, `run_execution/{boundary,handler,contracts,errors,ports,settlement,stream_authority}.py`, `app/reliability/execution.py`, Schema/DDL, frontend, deployment, or Batch 6 targets.
- Do not change business rules, stream event names or payloads, `RunStatus` transitions, error codes or error text, log message text, `RunAgentOutcome`/`AgentExecutionResult` values, `PermanentExecutionError`/`TransientExecutionError` codes, checkpoint configs, lock scopes, `ContextVar` push/pop pairing, or the order of any `await` in `run_agent()` or `_execute_with_trace()`.
- `run_agent()` must keep: the single `try/except/finally` ladder and its handler order; the root `values` lane as the only LLM-fallback authority; durable terminal precedence (`model_output_limit` over Stop/rollback/authorization revocation); one-time `resource_ownership.transfer_to_runner()`; File Finalization before status; the cleanup order `subagent flush -> workspace changes -> journal flush/completion -> file release -> private runtime close -> finalizing clear -> title sync -> thread status -> completion hook -> mount release -> approval seal -> publish_end -> deferred CancelledError`.
- `_execute_with_trace()` must keep: boundary construction first; `record` registration and abort/cancel binding; the runner call inside the `set_current_user`/`set_runtime_storage_user_id` scope; `lease_lost` and `type(outcome)` checks before usage mapping; `record_settled()` before outcome mapping; the complete `except` ladder; and the `finally` order `pop_current_app_config -> file_authority.release -> private_runtime.aclose` guarded by `resource_ownership.transferred`.
- `deerflow.runtime.runs.worker` keeps no `__all__` and stays a real module that defines `run_agent()`; it is not an imports-only façade. `deerflow.runtime`/`deerflow.runtime.runs` continue to expose `RunContext` and `run_agent` lazily through `worker`.
- Compatibility re-exports are exact objects. Never wrap, subclass, copy, or emit deprecation warnings. Re-export aliases cannot propagate monkeypatch assignment into another module's globals: every test that patches a moved helper's own globals moves to the owner in the same task, and every seam that `run_agent()` must keep resolving from `worker.py` globals is listed in `RUN_AGENT_MODULE_SEAMS` and never moved into an extracted phase function.
- New Harness owners must never import `deerflow.runtime.runs.worker`, `app.*`, or SQLAlchemy. New executor owners must never import `app.reliability.run_execution.executor`.
- Use `apply_patch` for edits and preserve unrelated changes. No task authorizes destructive reset/checkout, staging, commit, push, merge, or publication. Suggested commits are conditional on explicit local-commit authorization and must add only the listed Batch 5 paths.
- Stop the current task on any identity drift, changed terminal/exception order, changed error code or text, import cycle, new blocking-I/O finding, unexpected production consumer, or unexplained test failure. Revert the complete task with a reviewable patch or discard only its isolated uncommitted paths; never hide a structural regression with a behavior fix.
- If moving code reveals a real behavior defect, record it and request a separate design. Batch 5 must not repair it while changing ownership.

### Audited design refinements

1. `checkpoint_rollback.py` is the Harness leaf. Besides `RollbackPoint`, capture, legacy adaptation, delta linearization, pending-write restore, rollback, and rollback settlement, it owns the generic materialized-checkpoint reading primitives (`_checkpoint_id`, `_snapshot_values`, `_materialized_checkpoint_snapshot`, `_materialized_checkpoint_messages`, `_read_checkpoint_messages`) and the pre-existing message boundary (`_message_id`, `_checkpoint_messages_from_values_or_snapshot`, `_collect_pre_existing_message_ids`, `_collect_private_pre_existing_message_ids`), because rollback capture, resume linearization, goal continuation, and LLM-fallback masking all read the same state.
2. `stream_delivery.py` owns SSE-boundary frame handling: token-usage projection bridge, tool-argument batching, text-delta coalescing, deadline iteration, subagent step buffering, mode/namespace naming, frame publishing, stream item unpacking, stream-mode resolution, and root-lane semantic markers (LLM fallback extraction and the current-Run host-approval anchor). It imports only `_message_id` from `checkpoint_rollback`.
3. `runtime_binding.py` owns `RunContext`, `PrivateAgentRuntime`, `PrivateRuntimeFactoryUnavailable`, trace-user resolution, runtime-context build/install, checkpoint runtime settings, agent-factory capability detection, off-loop factory invocation, and the extracted `bind_run_runtime_context()` phase.
4. `goal_continuation.py` owns goal reads/writes, durable-receipt detection, stand-down reasons, evaluation persistence, and `_prepare_goal_continuation_input()`. It imports reading primitives from `checkpoint_rollback` and never imports `worker`.
5. `run_agent()` extraction is limited to four contiguous, non-terminal phases: stream-mode resolution (Task 6), runtime-context binding (Task 6), legacy checkpoint baseline capture (Task 7), and rollback-point capture (Task 7). The `_stream_once` closure, approval-gate refresh, goal loop, terminal ladder, exception ladder, and `finally` stay inline and unchanged.
6. Frozen `run_agent()` module seams: `_prepare_goal_continuation_input`, `_rollback_to_pre_run_checkpoint`, `_settle_rollback`, and `get_sandbox_provider` are called by name from `run_agent()` in `worker.py`. Tests keep patching them on `deerflow.runtime.runs.worker`. Tests that patch a moved helper's own globals (`time` for `_TextDeltaCoalescer`, `inspect.signature` for `_agent_factory_supports_app_config`) move to the owner.
7. Executor preparation returns frozen dataclasses through staged functions. Every step that acquires a releasable resource (`materialize_private_runtime()`) returns immediately after acquisition so the executor assigns the local the `finally` block already releases; `push_current_app_config()`/`pop_current_app_config()` remain paired inside the executor. Executor-owned collaborators are passed as one frozen `RunPreparationDependencies` bundle, not `self`, keyword soup, or an untyped dict.
8. `outcome_mapping.py` receives boundary facts as booleans read after `record_settled()` completes; it never receives the boundary, a session, or an authority object. The three flags are read at one point with no `await` between the reads and the mapping, which matches the inline code where no `await` separated them.
9. Legacy `RunAgentPrivateExecutor` private helpers consumed by tests (`_graph_input`, `_runner_config`, `_required_current_upload_snapshot`, `_terminal_failure_result`, `_output_limit_error`, `_usage_snapshot`, `_outcome_usage_snapshot`) remain class attributes as `staticmethod(owner_function)` aliases of the exact owner functions. `_resolve_agent_factory`, `_admitted`, `_default_agent_factory`, `execute`, and `_execute_with_trace` stay defined on the class.
10. The Batch 3 contract that requires the executor to import `execution_approval_policy` and `execution_approval_worker` moves to `preparation.py` because that file becomes the sole constructor of `WorkerHostExecutionApprovalPort`; the Batch 3 consumer inventory changes `EXECUTOR_PATH` to `PREPARATION_PATH` in the same task.

### Final ownership layout

```text
backend/packages/harness/deerflow/runtime/runs/
├── worker.py                 # run_agent(): preparation orchestration, graph streaming,
│                             # terminal priority, cleanup order; re-exports legacy names
├── checkpoint_rollback.py    # checkpoint reads, message boundary, RollbackPoint, capture,
│                             # linearize, restore, rollback, settlement, pre-run captures
├── stream_delivery.py        # batching, coalescing, publishing, stream modes, unpack,
│                             # LLM fallback + host-approval anchors
├── runtime_binding.py        # RunContext, runtime context build/install, factory call,
│                             # bind_run_runtime_context()
└── goal_continuation.py      # goal read/write/evaluate/continue

backend/app/reliability/run_execution/
├── executor.py               # RunAgentPrivateExecutor: boundary, record, runner call,
│                             # exception ladder, cleanup; compat aliases
├── preparation.py            # frozen policy/models, archive context, materialization,
│                             # authorities, checkpointer, RunContext, runner inputs
└── outcome_mapping.py        # pure usage snapshots and outcome -> result mapping
```

Dependency direction:

```text
checkpoint_rollback (leaf)
├── stream_delivery      (imports _message_id)
└── goal_continuation    (imports checkpoint reading primitives)
runtime_binding (leaf)

worker ──> checkpoint_rollback, stream_delivery, runtime_binding, goal_continuation

outcome_mapping (leaf, app)
preparation (leaf, app; imports deerflow.runtime + app.private_work owners)
executor ──> preparation, outcome_mapping
```

No arrow may point from an owner back to `worker.py` or `executor.py`.

### Frozen compatibility surfaces

- `deerflow.runtime.RunContext is deerflow.runtime.runs.worker.RunContext` and `deerflow.runtime.run_agent is deerflow.runtime.runs.worker.run_agent` remain true through the lazy `__getattr__` in both packages.
- `run_agent()` parameter names remain exactly:

  ```python
  EXPECTED_RUN_AGENT_PARAMETERS = (
      "bridge", "run_manager", "record", "ctx", "agent_factory", "graph_input", "config",
      "stream_modes", "stream_subgraphs", "interrupt_before", "interrupt_after",
  )
  ```

- `RunContext` field order remains exactly the 24 names in `EXPECTED_RUN_CONTEXT_FIELDS` (Task 1) and `RollbackPoint` fields remain `("config", "state_values", "messages", "metadata", "pending_writes")`.
- Every name in `WORKER_COMPATIBILITY_NAMES` (Task 1) stays importable from `deerflow.runtime.runs.worker` and is the same object as its owner.
- `RunAgentPrivateExecutor.__init__` parameter names remain exactly the 17 names in `EXPECTED_EXECUTOR_INIT_PARAMETERS` (Task 1); `executor.__all__` remains `["RunAgentPrivateExecutor"]`; `_context_compaction_threshold_tokens` remains importable from `executor`.
- The outer `except` ladders of `run_agent()` and `_execute_with_trace()` remain the exact tuples frozen in Task 1.

---

## Task 1: Freeze Batch 5 Worker and executor contracts

**Files:**

- Create: `backend/tests/test_python_module_decomposition_worker_runtime.py`
- Verify: `backend/packages/harness/deerflow/runtime/runs/worker.py`
- Verify: `backend/app/reliability/run_execution/executor.py`

**Interfaces:**

- Consumes: the current `deerflow.runtime.runs.worker` and `app.reliability.run_execution.executor` modules, `deerflow.runtime` lazy exports, and the repository test tree.
- Produces: signature/field freezes, compatibility-name and test-consumer inventories, the `run_agent()` module-seam scanner, exception-ladder freezes, and owner import-direction helpers used by Tasks 2-10.

- [ ] **Step 1: verify the exact branch, baseline, preserved Batch 4 state, and local configuration**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations
  git branch --show-current
  git rev-parse HEAD
  git status --short
  git diff --quiet 0d421ede2d52a2cf22a5c8fedfdfbb10e6e1394c -- \
    backend/packages/harness/deerflow/runtime/runs/worker.py && echo worker-unchanged
  git diff --stat 0d421ede2d52a2cf22a5c8fedfdfbb10e6e1394c -- \
    backend/app/reliability/run_execution/executor.py
  wc -l backend/packages/harness/deerflow/runtime/runs/worker.py \
    backend/app/reliability/run_execution/executor.py
  ls backend/packages/harness/deerflow/runtime/runs backend/app/reliability/run_execution
  git check-ignore -v .env config.yaml
  ```

  Require branch `codex/python-module-decomposition-foundations`, `eb3dd904…` or a reviewed descendant that contains Batch 4, `worker-unchanged`, an executor diff limited to `15 ++++++++-------` import lines, `3515` and `1345` lines, no Batch 5 owner module present, and ignored configuration. Identify every pre-existing modified/untracked path as Batch 4, the P1 test, or a user-owned document. Stop on any unexplained path.

- [ ] **Step 2: create the contract module with shape freezes**

  ```python
  """Batch 5 Worker Execution compatibility contracts.

  Characterization tests that pass on the untouched Worker/executor baseline and
  keep passing while ``runtime/runs/worker.py`` and
  ``reliability/run_execution/executor.py`` delegate to owning modules.
  """

  from __future__ import annotations

  import ast
  import dataclasses
  import inspect
  from pathlib import Path

  import deerflow.runtime as runtime_package
  from app.reliability.run_execution import executor as executor_legacy
  from deerflow.runtime.runs import worker as worker_legacy

  BACKEND_ROOT = Path(__file__).resolve().parents[1]
  TESTS_ROOT = BACKEND_ROOT / "tests"
  RUNS_ROOT = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "runtime" / "runs"
  RUN_EXECUTION_ROOT = BACKEND_ROOT / "app" / "reliability" / "run_execution"
  WORKER_PATH = RUNS_ROOT / "worker.py"
  EXECUTOR_PATH = RUN_EXECUTION_ROOT / "executor.py"
  WORKER_MODULE = "deerflow.runtime.runs.worker"
  EXECUTOR_MODULE = "app.reliability.run_execution.executor"
  WORKER_OWNER_MODULES = ("checkpoint_rollback", "stream_delivery", "runtime_binding", "goal_continuation")
  EXECUTOR_OWNER_MODULES = ("outcome_mapping", "preparation")

  EXPECTED_RUN_AGENT_PARAMETERS = (
      "bridge",
      "run_manager",
      "record",
      "ctx",
      "agent_factory",
      "graph_input",
      "config",
      "stream_modes",
      "stream_subgraphs",
      "interrupt_before",
      "interrupt_after",
  )
  EXPECTED_RUN_CONTEXT_FIELDS = (
      "checkpointer",
      "store",
      "event_store",
      "run_events_config",
      "thread_store",
      "app_config",
      "on_run_completed",
      "private_scope",
      "authorization_checker",
      "authorization_boundary",
      "file_authority",
      "memory_authority",
      "memory_archive_context",
      "guardrail_attribution",
      "private_agent_runtime",
      "host_execution_approval_port",
      "channel_user_id",
      "vision_dispatch_authority",
      "token_budget_usage_recorder",
      "resource_ownership",
      "tool_call_control_policy",
      "context_evidence_observer",
      "max_concurrent_subagents",
      "max_total_subagents",
  )
  EXPECTED_ROLLBACK_POINT_FIELDS = ("config", "state_values", "messages", "metadata", "pending_writes")
  EXPECTED_EXECUTOR_INIT_PARAMETERS = (
      "self",
      "session_factory",
      "app_config",
      "bridge",
      "project_checkpointer",
      "store",
      "event_store",
      "asset_runtime",
      "model_materializer",
      "runtime_policy_materializer",
      "agent_factory",
      "runner",
      "quota",
      "audit",
      "host_execution_domain",
      "skill_builder_activity_emitter_factory",
      "knowledge_module",
  )


  def test_batch5_worker_public_shapes_are_frozen() -> None:
      assert tuple(inspect.signature(worker_legacy.run_agent).parameters) == EXPECTED_RUN_AGENT_PARAMETERS
      assert tuple(field.name for field in dataclasses.fields(worker_legacy.RunContext)) == EXPECTED_RUN_CONTEXT_FIELDS
      assert tuple(field.name for field in dataclasses.fields(worker_legacy.RollbackPoint)) == EXPECTED_ROLLBACK_POINT_FIELDS
      assert runtime_package.RunContext is worker_legacy.RunContext
      assert runtime_package.run_agent is worker_legacy.run_agent
      assert not hasattr(worker_legacy, "__all__")


  def test_batch5_executor_public_shapes_are_frozen() -> None:
      assert executor_legacy.__all__ == ["RunAgentPrivateExecutor"]
      assert tuple(inspect.signature(executor_legacy.RunAgentPrivateExecutor.__init__).parameters) == EXPECTED_EXECUTOR_INIT_PARAMETERS
      assert callable(executor_legacy._context_compaction_threshold_tokens)
  ```

- [ ] **Step 3: freeze compatibility names and the repository test-consumer inventories**

  Add these constants and scanners. The Worker inventory is every name a repository test imports from, or monkeypatches on, the legacy Worker module today; the executor inventory is every name a repository test imports from, or patches by string on, the legacy executor module today.

  ```python
  WORKER_COMPATIBILITY_NAMES = frozenset(
      {
          "RollbackPoint",
          "RunContext",
          "run_agent",
          "_TEXT_DELTA_FLUSH_DUE",
          "_TextDeltaCoalescer",
          "_ToolCallChunkBatcher",
          "_iter_with_text_delta_deadline",
          "_publish_stream_item",
          "_agent_factory_supports_app_config",
          "_build_runtime_context",
          "_call_agent_factory_off_loop",
          "_install_runtime_context",
          "_collect_pre_existing_message_ids",
          "_extract_llm_error_fallback",
          "_linearize_delta_checkpoint_resume",
          "_rollback_to_pre_run_checkpoint",
          "_settle_rollback",
          "_prepare_goal_continuation_input",
          "get_sandbox_provider",
          "time",
          "inspect",
      }
  )
  # run_agent() must keep calling these by name from worker.py globals so the
  # repository's monkeypatches on deerflow.runtime.runs.worker keep working.
  RUN_AGENT_MODULE_SEAMS = frozenset(
      {
          "_prepare_goal_continuation_input",
          "_rollback_to_pre_run_checkpoint",
          "_settle_rollback",
          "get_sandbox_provider",
      }
  )
  EXECUTOR_CLASS_COMPATIBILITY_NAMES = frozenset(
      {
          "execute",
          "_execute_with_trace",
          "_default_agent_factory",
          "_admitted",
          "_resolve_agent_factory",
          "_graph_input",
          "_runner_config",
          "_required_current_upload_snapshot",
          "_usage_snapshot",
          "_outcome_usage_snapshot",
          "_terminal_failure_result",
          "_output_limit_error",
      }
  )
  EXECUTOR_MODULE_COMPATIBILITY_NAMES = frozenset(
      {
          "RunAgentPrivateExecutor",
          "_context_compaction_threshold_tokens",
          "PrivateRunExecutionBoundary",
          "PrivateRunContextEvidenceObserver",
          "PrivateRunFileAuthority",
          "SkillBuilderAgentFactory",
          "WorkerSkillBuilderAuthoringCatalog",
          "SkillDesignDraftSink",
      }
  )


  def _parse(path: Path) -> ast.Module:
      return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


  def _legacy_test_consumers(module_name: str) -> frozenset[str]:
      """Names imported from, or monkeypatched on, ``module_name`` across tests/."""

      observed: set[str] = set()
      for path in TESTS_ROOT.rglob("*.py"):
          tree = _parse(path)
          aliases: set[str] = set()
          for node in ast.walk(tree):
              if isinstance(node, ast.Import):
                  aliases.update(alias.asname or alias.name for alias in node.names if alias.name == module_name)
              elif isinstance(node, ast.ImportFrom) and node.module == module_name:
                  observed.update(alias.name for alias in node.names)
          for node in ast.walk(tree):
              if not isinstance(node, ast.Call):
                  continue
              callee = node.func
              is_setattr = isinstance(callee, ast.Attribute) and callee.attr == "setattr"
              is_patch = (isinstance(callee, ast.Name) and callee.id == "patch") or (isinstance(callee, ast.Attribute) and callee.attr == "patch")
              if not (is_setattr or is_patch) or not node.args:
                  continue
              target = node.args[0]
              if isinstance(target, ast.Constant) and isinstance(target.value, str) and target.value.startswith(f"{module_name}."):
                  observed.add(target.value.removeprefix(f"{module_name}.").split(".")[0])
              elif isinstance(target, ast.Name) and target.id in aliases and len(node.args) > 1:
                  attribute = node.args[1]
                  if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
                      observed.add(attribute.value)
      return frozenset(observed)


  def test_batch5_worker_compatibility_names_remain_exact_objects() -> None:
      for name in sorted(WORKER_COMPATIBILITY_NAMES):
          assert hasattr(worker_legacy, name), name


  def test_batch5_worker_test_consumers_stay_within_the_frozen_inventory() -> None:
      observed = _legacy_test_consumers(WORKER_MODULE)
      assert observed <= WORKER_COMPATIBILITY_NAMES, observed - WORKER_COMPATIBILITY_NAMES


  def test_batch5_executor_compatibility_names_remain_exact() -> None:
      for name in sorted(EXECUTOR_CLASS_COMPATIBILITY_NAMES):
          assert callable(getattr(executor_legacy.RunAgentPrivateExecutor, name)), name
      for name in sorted(EXECUTOR_MODULE_COMPATIBILITY_NAMES):
          assert hasattr(executor_legacy, name), name


  def test_batch5_executor_test_consumers_stay_within_the_frozen_inventory() -> None:
      observed = _legacy_test_consumers(EXECUTOR_MODULE)
      assert observed <= EXECUTOR_MODULE_COMPATIBILITY_NAMES, observed - EXECUTOR_MODULE_COMPATIBILITY_NAMES
  ```

  `tests/test_subagent_sdk_runner_profile.py` binds `executor_module` to a different (subagent) module; the alias resolution above is per file, so it is correctly excluded.

- [ ] **Step 4: freeze the `run_agent()` module seams and both exception ladders**

  ```python
  def _function_node(path: Path, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
      for node in ast.walk(_parse(path)):
          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
              return node
      raise AssertionError(f"{function_name} not found in {path.name}")


  def _called_names(function: ast.AST) -> frozenset[str]:
      """Names called directly or passed as the first argument to ``partial``."""

      names: set[str] = set()
      for node in ast.walk(function):
          if not isinstance(node, ast.Call):
              continue
          if isinstance(node.func, ast.Name):
              names.add(node.func.id)
              if node.func.id == "partial" and node.args and isinstance(node.args[0], ast.Name):
                  names.add(node.args[0].id)
      return frozenset(names)


  def _dotted(node: ast.expr) -> str:
      if isinstance(node, ast.Name):
          return node.id
      if isinstance(node, ast.Attribute):
          return f"{_dotted(node.value)}.{node.attr}"
      raise AssertionError(ast.dump(node))


  def _outer_except_ladder(path: Path, function_name: str) -> tuple[tuple[str, ...], ...]:
      function = _function_node(path, function_name)
      try_node = next(child for child in function.body if isinstance(child, ast.Try))
      ladder: list[tuple[str, ...]] = []
      for handler in try_node.handlers:
          if handler.type is None:
              ladder.append(("*",))
          elif isinstance(handler.type, ast.Tuple):
              ladder.append(tuple(_dotted(item) for item in handler.type.elts))
          else:
              ladder.append((_dotted(handler.type),))
      return tuple(ladder)


  EXPECTED_RUN_AGENT_EXCEPT_LADDER = (
      ("asyncio.CancelledError",),
      ("AuthorizationRevoked",),
      ("GraphRecursionError", "ToolCallControlLoopFinalizationFailed", "ToolCallControlStateInvalid"),
      ("PublicRunError",),
      ("ContextProviderCallAmbiguousError", "MemoryAuthorityUnavailable"),
      ("Exception",),
  )
  EXPECTED_EXECUTOR_EXCEPT_LADDER = (
      ("asyncio.CancelledError",),
      ("ContextProviderCallAmbiguousError",),
      ("CheckpointModeMismatchError",),
      ("PrivateWorkAssetStale",),
      ("CurrentUploadSnapshotStale",),
      ("AgentModelSettingsUnsupported",),
      ("SkillDesignActivityLimitExceeded",),
      ("TransientExecutionError",),
      ("PermanentExecutionError",),
      ("AmbiguousExternalSideEffect",),
      ("PrivateWorkMcpQuotaExceeded",),
      ("MemoryAuthorityUnavailable",),
      ("PublicRunError",),
      ("AuthorizationRevoked",),
      ("Exception",),
  )


  def test_batch5_run_agent_calls_frozen_module_seams_by_name() -> None:
      called = _called_names(_function_node(WORKER_PATH, "run_agent"))
      assert RUN_AGENT_MODULE_SEAMS <= called, RUN_AGENT_MODULE_SEAMS - called


  def test_batch5_terminal_exception_ladders_are_frozen() -> None:
      assert _outer_except_ladder(WORKER_PATH, "run_agent") == EXPECTED_RUN_AGENT_EXCEPT_LADDER
      assert _outer_except_ladder(EXECUTOR_PATH, "_execute_with_trace") == EXPECTED_EXECUTOR_EXCEPT_LADDER
  ```

- [ ] **Step 5: add the owner import-direction gate and run everything on the untouched baseline**

  ```python
  def _module_imports(path: Path) -> set[str]:
      """Absolute imports plus relative imports rendered as ``.module`` / ``.name``."""

      imports: set[str] = set()
      for node in ast.walk(_parse(path)):
          if isinstance(node, ast.Import):
              imports.update(alias.name for alias in node.names)
          elif isinstance(node, ast.ImportFrom):
              if node.level == 0 and node.module:
                  imports.add(node.module)
              elif node.level > 0:
                  prefix = "." * node.level
                  imports.add(f"{prefix}{node.module or ''}")
                  if not node.module:
                      imports.update(f"{prefix}{alias.name}" for alias in node.names)
      return imports


  def test_batch5_owner_modules_never_import_facades_or_forbidden_packages() -> None:
      for name in WORKER_OWNER_MODULES:
          path = RUNS_ROOT / f"{name}.py"
          if not path.exists():
              continue
          imports = _module_imports(path)
          assert not imports & {WORKER_MODULE, ".worker"}, (name, imports & {WORKER_MODULE, ".worker"})
          forbidden = {module for module in imports if module == "app" or module.startswith("app.") or module == "sqlalchemy" or module.startswith("sqlalchemy.")}
          assert forbidden == set(), (name, forbidden)
      for name in EXECUTOR_OWNER_MODULES:
          path = RUN_EXECUTION_ROOT / f"{name}.py"
          if not path.exists():
              continue
          imports = _module_imports(path)
          assert not imports & {EXECUTOR_MODULE, ".executor"}, (name, imports & {EXECUTOR_MODULE, ".executor"})
  ```

  Then run the new module and the complete focused Batch 5 baseline:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest tests/test_python_module_decomposition_worker_runtime.py -q

  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_worker_rollback.py \
    tests/test_run_worker_rollback_settlement.py \
    tests/test_run_worker_host_execution_pause.py \
    tests/test_run_worker_private_file_lifecycle.py \
    tests/test_run_agent_outcome.py \
    tests/test_run_event_text_batching.py \
    tests/test_memory_error_boundaries.py \
    tests/test_skill_builder_agent_runtime.py \
    tests/test_tool_call_control_scope_checkpoint_acceptance.py \
    tests/test_host_execution_approval.py \
    tests/test_run_execution_modules.py \
    tests/test_run_execution_profile.py \
    tests/test_model_output_limit_settlement.py \
    tests/test_context_provider_ambiguity_terminal.py \
    tests/test_chat_control_replay_identity.py \
    tests/test_current_upload_vision.py \
    tests/test_compaction_trigger_capacity_clamp.py \
    tests/test_skill_builder_provider_execution.py \
    tests/test_private_agent_mcp_discovery.py \
    tests/test_worker_execution_approval_composition.py \
    tests/knowledge/test_agent_tool.py \
    -q -m "not postgres and not provider_integration"
  ```

  Expected: all new contract nodes PASS before any production movement (characterization, so RED is not expected here), and the full set reports zero failures. The audited count without the new file is 438; record the new exact count, duration, deselections, skips, and warnings.

- [ ] **Step 6: checkpoint the characterization only**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check tests/test_python_module_decomposition_worker_runtime.py
  uvx ruff format --check tests/test_python_module_decomposition_worker_runtime.py
  cd ..
  git diff --check
  git status --short
  ```

  If explicit local commits are authorized, commit only the new test with message `test(worker): freeze batch 5 execution contracts`.

## Task 2: Extract checkpoint reading, message boundary, and rollback

**Files:**

- Create: `backend/packages/harness/deerflow/runtime/runs/checkpoint_rollback.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py:149,2190-2199,2211-2238,2309-2313,2678-3133,3255-3264,3419-3477`
- Modify: `backend/tests/test_python_module_decomposition_worker_runtime.py`
- Modify: `backend/tests/test_run_worker_rollback.py:33-43`
- Modify: `backend/tests/test_run_worker_rollback_settlement.py:7,117-123`

**Interfaces:**

- Consumes: `CheckpointStateAccessor`, `build_state_mutation_graph`, `graph_state_schema`, `_call_checkpointer_method`, `empty_checkpoint`, `Overwrite`, `ContextRebaseReason`, `ROLLBACK_FAILED_ERROR_CODE`, `RunManager`, `RunStatus`, `await_despite_cancellation`.
- Produces: `RollbackPoint`, `_checkpoint_id`, `_snapshot_values`, `_materialized_checkpoint_snapshot`, `_materialized_checkpoint_messages`, `_read_checkpoint_messages`, `_message_id`, `_checkpoint_messages_from_values_or_snapshot`, `_collect_pre_existing_message_ids`, `_collect_private_pre_existing_message_ids`, `_settle_rollback`, `_capture_rollback_point`, `_rollback_point_from_legacy_snapshot`, `_linearize_delta_checkpoint_resume`, `_restore_pending_writes`, `_rollback_legacy_full_checkpoint`, `_rollback_to_pre_run_checkpoint`, `_new_checkpoint_marker`, `_ROLLBACK_SUCCEEDED_ERROR`.

- [ ] **Step 1: add the failing owner identity test**

  Add `import importlib` to the contract module and:

  ```python
  CHECKPOINT_ROLLBACK_NAMES = (
      "_ROLLBACK_SUCCEEDED_ERROR",
      "_checkpoint_id",
      "_snapshot_values",
      "_materialized_checkpoint_snapshot",
      "_materialized_checkpoint_messages",
      "_read_checkpoint_messages",
      "_message_id",
      "_checkpoint_messages_from_values_or_snapshot",
      "_collect_pre_existing_message_ids",
      "_collect_private_pre_existing_message_ids",
      "RollbackPoint",
      "_settle_rollback",
      "_capture_rollback_point",
      "_rollback_point_from_legacy_snapshot",
      "_linearize_delta_checkpoint_resume",
      "_restore_pending_writes",
      "_rollback_legacy_full_checkpoint",
      "_rollback_to_pre_run_checkpoint",
      "_new_checkpoint_marker",
  )


  def test_checkpoint_rollback_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.runtime.runs.checkpoint_rollback")
      for name in CHECKPOINT_ROLLBACK_NAMES:
          assert getattr(worker_legacy, name) is getattr(owner, name), name
      assert owner.__all__ == ["RollbackPoint"]
  ```

  Run only this node. Expected: RED with `ModuleNotFoundError: No module named 'deerflow.runtime.runs.checkpoint_rollback'`.

- [ ] **Step 2: move the exact definitions into the leaf owner**

  Create `checkpoint_rollback.py` with docstring `"""Checkpoint state reads, the pre-run message boundary, and rollback restore."""`, its own `logger = logging.getLogger(__name__)`, and move every definition in Step 1 verbatim from the baseline coordinates: `_ROLLBACK_SUCCEEDED_ERROR` (149), `_checkpoint_id` (2190-2199), `_snapshot_values`/`_materialized_checkpoint_snapshot`/`_materialized_checkpoint_messages` (2211-2238), `_read_checkpoint_messages` (2309-2313), `RollbackPoint` through `_new_checkpoint_marker` (2678-3133), `_message_id` (3255-3264), `_checkpoint_messages_from_values_or_snapshot`/`_collect_pre_existing_message_ids`/`_collect_private_pre_existing_message_ids` (3419-3477). Do not change branches, messages, `copy.deepcopy` calls, `RuntimeError` texts, `logger.info`/`warning` texts, or `as_node` names (`"checkpoint_resume"`, `"rollback_restore"`).

  Owner imports are exactly what the moved bodies use:

  ```python
  from __future__ import annotations

  import asyncio
  import copy
  import logging
  from collections.abc import Awaitable, Callable
  from dataclasses import dataclass
  from typing import Any

  from langgraph.checkpoint.base import empty_checkpoint
  from langgraph.types import Overwrite

  from deerflow.error_codes import ROLLBACK_FAILED_ERROR_CODE
  from deerflow.runtime.checkpoint_state import (
      CheckpointStateAccessor,
      build_state_mutation_graph,
      graph_state_schema,
  )
  from deerflow.runtime.context_evidence import ContextRebaseReason
  from deerflow.runtime.goal import _call_checkpointer_method

  from .manager import RunManager
  from .private_file_lifecycle import await_despite_cancellation
  from .schemas import RunStatus

  __all__ = ["RollbackPoint"]
  ```

  `_collect_pre_existing_message_ids` keeps its docstring reference to `run_agent`; it describes the consumer, not an import.

- [ ] **Step 3: re-export exact objects from `worker.py`**

  Delete the moved definitions and add one import block per owner, next to the existing relative imports. Ruff's isort merges duplicate `from .module import` statements into one, so each owner gets exactly one block and the repository's compatibility comment on its first line covers every name, whether `run_agent()` still uses it or not:

  ```python
  from .checkpoint_rollback import (  # noqa: F401 - compatibility exports
      _ROLLBACK_SUCCEEDED_ERROR,
      RollbackPoint,
      _capture_rollback_point,
      _checkpoint_id,
      _checkpoint_messages_from_values_or_snapshot,
      _collect_pre_existing_message_ids,
      _collect_private_pre_existing_message_ids,
      _linearize_delta_checkpoint_resume,
      _materialized_checkpoint_messages,
      _materialized_checkpoint_snapshot,
      _message_id,
      _new_checkpoint_marker,
      _read_checkpoint_messages,
      _restore_pending_writes,
      _rollback_legacy_full_checkpoint,
      _rollback_point_from_legacy_snapshot,
      _rollback_to_pre_run_checkpoint,
      _settle_rollback,
      _snapshot_values,
  )
  ```

  Let `uvx ruff format`/`ruff check --fix` settle the member order; the set of names is the contract. `run_agent()` keeps referencing `RollbackPoint`, `_capture_rollback_point`, `_collect_*`, `_linearize_delta_checkpoint_resume`, `_materialized_checkpoint_messages`, `_rollback_point_from_legacy_snapshot`, `_rollback_to_pre_run_checkpoint`, and `_settle_rollback`; the goal helpers still living in `worker.py` (until Task 5) keep resolving the reading primitives; `_try_extract_llm_error_fallback` (until Task 3) keeps resolving `_message_id`. Remove `empty_checkpoint`, `ROLLBACK_FAILED_ERROR_CODE`, `ContextRebaseReason`, and `await_despite_cancellation` from `worker.py` imports only if no remaining reference exists; `Overwrite`, `build_state_mutation_graph`, and `graph_state_schema` remain used by the goal helpers until Task 5.

- [ ] **Step 4: migrate rollback test imports, keep `run_agent()` seam patches on `worker`**

  - In `test_run_worker_rollback.py`, import `RollbackPoint`, `_collect_pre_existing_message_ids`, `_linearize_delta_checkpoint_resume`, and `_rollback_to_pre_run_checkpoint` from `deerflow.runtime.runs.checkpoint_rollback`; keep `RunContext`, `run_agent`, `_agent_factory_supports_app_config`, `_build_runtime_context`, `_extract_llm_error_fallback`, and `_install_runtime_context` on `worker` until their owners exist. Keep `patch("deerflow.runtime.runs.worker._rollback_to_pre_run_checkpoint", ...)` at line 756 unchanged: that test drives `run_agent()`, which resolves the seam from `worker` globals.
  - In `test_run_worker_rollback_settlement.py`, add `import deerflow.runtime.runs.checkpoint_rollback as checkpoint_rollback` and change the direct call at line 118 to `checkpoint_rollback._settle_rollback(...)`. Keep both `monkeypatch.setattr(run_worker, "_rollback_to_pre_run_checkpoint", rollback)` calls on `run_worker`.

- [ ] **Step 5: run rollback, settlement, and contract gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_worker_rollback.py \
    tests/test_run_worker_rollback_settlement.py \
    tests/test_run_agent_outcome.py \
    tests/test_run_worker_private_file_lifecycle.py \
    -q
  ```

  Require the owner identity node, both settlement seams (`ROLLBACK_FAILED` and `Rolled back by user` exactly once), deferred-cancellation settlement, private-boundary rejection of unstable message ids, delta linearization, legacy full-mode restore, and `record_window_rebased` on rollback to pass unchanged.

- [ ] **Step 6: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/checkpoint_rollback.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_worker_rollback.py \
    tests/test_run_worker_rollback_settlement.py
  uvx ruff format --check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/checkpoint_rollback.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_worker_rollback.py \
    tests/test_run_worker_rollback_settlement.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 2 paths with message `refactor(worker): extract checkpoint rollback`.

## Task 3: Extract stream delivery

**Files:**

- Create: `backend/packages/harness/deerflow/runtime/runs/stream_delivery.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py:141,147,151-152,155-497,813-891,3136-3252,3267-3416,3480-3515`
- Modify: `backend/packages/harness/deerflow/runtime/serialization.py:7`
- Modify: `backend/packages/harness/deerflow/subagents/step_events.py:13`
- Modify: `backend/tests/test_python_module_decomposition_worker_runtime.py`
- Modify: `backend/tests/test_run_event_text_batching.py:27-35,51-55`
- Modify: `backend/tests/test_run_worker_rollback.py:33-43`

**Interfaces:**

- Consumes: `StreamBridge`, `project_public_sse_payload`, `project_public_subagent_event`, `serialize`, `ToolMessage`, `LLM_PUBLIC_ERROR_CODES`, `llm_error_code_for_reason`, and `_message_id` from `checkpoint_rollback`.
- Produces: `_VALID_LG_MODES`, `_LLM_ERROR_FALLBACK_AUTHORITY_MODES`, `_TOOL_CALL_CHUNK_BATCH_SIZE`, `_MESSAGE_TRANSPORT_METADATA_KEYS`, `_PublicTokenUsageBridge`, `_ToolCallChunkBatcher`, `_TEXT_DELTA_FLUSH_BYTES`, `_TEXT_DELTA_FINISH_KEYS`, `_TEXT_DELTA_FLUSH_DUE`, `_TextDeltaCoalescer`, `_iter_with_text_delta_deadline`, `_SubagentEventBuffer`, `_lg_mode_to_sse_event`, `_namespaced_sse_event`, `_publish_stream_item`, `_LLMErrorFallback`, `_error_fallback_from_metadata`, `_current_run_host_execution_approval_id`, `_contains_current_run_host_execution_approval`, `_try_extract_llm_error_fallback`, `_extract_llm_error_fallback`, `_unpack_stream_item`, `_normalize_stream_namespace`.

- [ ] **Step 1: add the failing owner identity test**

  ```python
  STREAM_DELIVERY_NAMES = (
      "_VALID_LG_MODES",
      "_LLM_ERROR_FALLBACK_AUTHORITY_MODES",
      "_TOOL_CALL_CHUNK_BATCH_SIZE",
      "_MESSAGE_TRANSPORT_METADATA_KEYS",
      "_PublicTokenUsageBridge",
      "_ToolCallChunkBatcher",
      "_TEXT_DELTA_FLUSH_BYTES",
      "_TEXT_DELTA_FINISH_KEYS",
      "_TEXT_DELTA_FLUSH_DUE",
      "_TextDeltaCoalescer",
      "_iter_with_text_delta_deadline",
      "_SubagentEventBuffer",
      "_lg_mode_to_sse_event",
      "_namespaced_sse_event",
      "_publish_stream_item",
      "_LLMErrorFallback",
      "_error_fallback_from_metadata",
      "_current_run_host_execution_approval_id",
      "_contains_current_run_host_execution_approval",
      "_try_extract_llm_error_fallback",
      "_extract_llm_error_fallback",
      "_unpack_stream_item",
      "_normalize_stream_namespace",
  )


  def test_stream_delivery_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.runtime.runs.stream_delivery")
      for name in STREAM_DELIVERY_NAMES:
          assert getattr(worker_legacy, name) is getattr(owner, name), name
      assert owner._TEXT_DELTA_FLUSH_DUE is worker_legacy._TEXT_DELTA_FLUSH_DUE
      assert "time" in vars(owner)
  ```

  Run only this node. Expected: RED with `ModuleNotFoundError` for `stream_delivery`.

- [ ] **Step 2: move the stream-boundary definitions verbatim**

  Create `stream_delivery.py` with docstring `"""Worker stream delivery: frame batching, publishing, modes, and root-lane markers."""`, its own `logger`, and move every Step 1 definition from the baseline coordinates without changing batch sizes, flush keys, `_TEXT_DELTA_FLUSH_DUE = object()` (one sentinel object, created once), `time.monotonic()` calls, the `asyncio.wait` timeout race, the lazy `from deerflow.subagents.step_events import subagent_run_event` inside `_SubagentEventBuffer.add()` with its cycle-avoidance comment, `put_batch` scope handling, event-name suffixing, tool-batcher `finish()` versus `flush()` on `values`, fallback message truncation (`[:2000]`), the `schema_version == 1`/`kind == "local_shell"`/`source_run_id` anchor checks, or namespace parsing.

  Owner imports:

  ```python
  from __future__ import annotations

  import asyncio
  import logging
  import time
  from dataclasses import dataclass, field
  from typing import Any

  from langchain_core.messages import ToolMessage

  from deerflow.public_error_codes import (
      LLM_PUBLIC_ERROR_CODES,
      llm_error_code_for_reason,
  )
  from deerflow.runtime.events.stream_base import StreamBridge
  from deerflow.runtime.public_token_usage import (
      project_public_sse_payload,
      project_public_subagent_event,
  )
  from deerflow.runtime.serialization import serialize

  from .checkpoint_rollback import _message_id
  ```

  Declare no `__all__` yet; Task 6 adds the public phase surface. Do not import `worker`.

- [ ] **Step 3: re-export exact objects from `worker.py`**

  Replace the moved block with one owner import block:

  ```python
  from .stream_delivery import (  # noqa: F401 - compatibility exports
      _LLM_ERROR_FALLBACK_AUTHORITY_MODES,
      _LLMErrorFallback,
      _MESSAGE_TRANSPORT_METADATA_KEYS,
      _PublicTokenUsageBridge,
      _SubagentEventBuffer,
      _TEXT_DELTA_FINISH_KEYS,
      _TEXT_DELTA_FLUSH_BYTES,
      _TEXT_DELTA_FLUSH_DUE,
      _TOOL_CALL_CHUNK_BATCH_SIZE,
      _TextDeltaCoalescer,
      _ToolCallChunkBatcher,
      _VALID_LG_MODES,
      _contains_current_run_host_execution_approval,
      _current_run_host_execution_approval_id,
      _error_fallback_from_metadata,
      _extract_llm_error_fallback,
      _iter_with_text_delta_deadline,
      _lg_mode_to_sse_event,
      _namespaced_sse_event,
      _normalize_stream_namespace,
      _publish_stream_item,
      _try_extract_llm_error_fallback,
      _unpack_stream_item,
  )
  ```

  `run_agent()` keeps referencing `_LLM_ERROR_FALLBACK_AUTHORITY_MODES`, `_PublicTokenUsageBridge`, `_SubagentEventBuffer`, `_TEXT_DELTA_FLUSH_DUE`, `_TextDeltaCoalescer`, `_ToolCallChunkBatcher`, `_VALID_LG_MODES` (until Task 6), `_current_run_host_execution_approval_id`, `_extract_llm_error_fallback`, `_iter_with_text_delta_deadline`, `_lg_mode_to_sse_event`, `_publish_stream_item`, and `_unpack_stream_item`. Remove `time`, `ToolMessage`, `LLM_PUBLIC_ERROR_CODES`, `project_public_sse_payload`, and `project_public_subagent_event` from `worker.py` only if no remaining reference exists (`llm_error_code_for_reason` and `serialize` remain used by `run_agent()`; `dataclass`/`field` remain used by `RunContext` until Task 4). `_message_id` is no longer referenced by `worker.py` bodies after this step and stays in the Task 2 block.

- [ ] **Step 4: update the two ownership docstrings**

  - `deerflow/runtime/serialization.py` line 7: `Consumers: ``deerflow.runtime.runs.stream_delivery`` (SSE publishing) and`.
  - `deerflow/subagents/step_events.py` line 13: replace `(``runtime/runs/worker.py``)` with `(``runtime/runs/stream_delivery.py``)`.

- [ ] **Step 5: migrate the batching test to the owner's globals**

  In `test_run_event_text_batching.py`, replace lines 27-35 with:

  ```python
  import deerflow.runtime.runs.stream_delivery as stream_module
  from deerflow.config.worker_config import DEFAULT_TEXT_DELTA_FLUSH_MS, WorkerStreamConfig
  from deerflow.runtime.runs.stream_delivery import (
      _TEXT_DELTA_FLUSH_DUE,
      _iter_with_text_delta_deadline,
      _publish_stream_item,
      _TextDeltaCoalescer,
      _ToolCallChunkBatcher,
  )
  ```

  and change the `clock` fixture to `monkeypatch.setattr(stream_module, "time", SimpleNamespace(monotonic=fake.monotonic))`. `_TextDeltaCoalescer` now resolves `time` from `stream_delivery` globals; patching `worker.time` would silently stop controlling the clock. Change nothing else in the file.

  In `test_run_worker_rollback.py`, import `_extract_llm_error_fallback` from `deerflow.runtime.runs.stream_delivery`.

- [ ] **Step 6: run stream, batching, and end-to-end Worker gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_event_text_batching.py \
    tests/test_run_worker_rollback.py \
    tests/test_run_worker_host_execution_pause.py \
    tests/test_run_agent_outcome.py \
    tests/test_tool_call_control_scope_checkpoint_acceptance.py \
    -q
  ```

  Require every leading-edge/window/byte-bound/finish-marker coalescing node, tool-batch ordering node, deadline-timer node, subagent event batching node, namespaced publish node, LLM fallback (fresh versus stale history) node, and host-approval suspension node to pass with the clock fixture controlling `stream_delivery.time`.

- [ ] **Step 7: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/stream_delivery.py \
    packages/harness/deerflow/runtime/serialization.py \
    packages/harness/deerflow/subagents/step_events.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_event_text_batching.py \
    tests/test_run_worker_rollback.py
  uvx ruff format --check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/stream_delivery.py \
    packages/harness/deerflow/runtime/serialization.py \
    packages/harness/deerflow/subagents/step_events.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_event_text_batching.py \
    tests/test_run_worker_rollback.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 3 paths with message `refactor(worker): extract stream delivery`.

## Task 4: Extract runtime binding

**Files:**

- Create: `backend/packages/harness/deerflow/runtime/runs/runtime_binding.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py:500-810`
- Modify: `backend/tests/test_python_module_decomposition_worker_runtime.py`
- Modify: `backend/tests/test_run_worker_rollback.py:33-43,2070`
- Modify: `backend/tests/test_host_execution_approval.py:38`
- Modify: `backend/tests/test_skill_builder_agent_runtime.py:85-89`

**Interfaces:**

- Consumes: `RuntimeContextCarrier`, `RuntimeContextKeys`, `trusted_runtime_agent_catalog`, `get_current_user`, `DEFAULT_USER_ID`, `AppConfig`, `CheckpointChannelMode`, `RunFileAuthority`, `ResolvedGraphToolCallControlProfile`, `TokenBudgetUsageRecorder`, `RunAgentResourceOwnership`, `RunSemanticStopRecorder`, `RunRecord`.
- Produces: `_repository_trace_user_id`, `_build_runtime_context`, `PrivateAgentRuntime`, `PrivateRuntimeFactoryUnavailable`, `RunContext`, `_checkpoint_runtime_settings`, `_install_runtime_context`, `_compute_agent_factory_supports_app_config`, `_cached_agent_factory_supports_app_config`, `_agent_factory_supports_app_config`, `_call_agent_factory_off_loop`.

- [ ] **Step 1: add the failing owner identity test**

  ```python
  RUNTIME_BINDING_NAMES = (
      "_repository_trace_user_id",
      "_build_runtime_context",
      "PrivateAgentRuntime",
      "PrivateRuntimeFactoryUnavailable",
      "RunContext",
      "_checkpoint_runtime_settings",
      "_install_runtime_context",
      "_compute_agent_factory_supports_app_config",
      "_cached_agent_factory_supports_app_config",
      "_agent_factory_supports_app_config",
      "_call_agent_factory_off_loop",
  )


  def test_runtime_binding_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.runtime.runs.runtime_binding")
      for name in RUNTIME_BINDING_NAMES:
          assert getattr(worker_legacy, name) is getattr(owner, name), name
      assert owner.__all__ == ["PrivateAgentRuntime", "PrivateRuntimeFactoryUnavailable", "RunContext"]
      assert runtime_package.RunContext is owner.RunContext
      assert "inspect" in vars(owner)
  ```

  Run only this node. Expected: RED with `ModuleNotFoundError` for `runtime_binding`.

- [ ] **Step 2: move lines 500-810 verbatim**

  Create `runtime_binding.py` with docstring `"""Run-local runtime context, RunContext dependencies, and Agent factory binding."""` and move every Step 1 definition. Preserve the `@lru_cache(maxsize=128)` decorator and the unhashable-callable fallback, the `PrivateRuntimeFactoryUnavailable` messages, `asyncio.to_thread(_build)`, the `CHANNEL_USER_ID = None` fail-closed rule for private Runs, the `INSTALL_KEYS - public_identity_keys - {RUNTIME_AGENT_CATALOG}` install set, and the `("lead",)` host-execution agent path. Keep `RunContext` a frozen dataclass with the exact field order.

  Owner imports:

  ```python
  from __future__ import annotations

  import asyncio
  import inspect
  from collections.abc import Awaitable, Callable, Mapping
  from dataclasses import dataclass, field
  from functools import lru_cache
  from typing import Any, Protocol, cast

  from deerflow.agents.middlewares.tool_call_control import (
      ResolvedGraphToolCallControlProfile,
  )
  from deerflow.config.app_config import AppConfig
  from deerflow.config.database_config import CheckpointChannelMode
  from deerflow.file_authority import RunFileAuthority
  from deerflow.runtime.context_carrier import RuntimeContextCarrier
  from deerflow.runtime.context_keys import RuntimeContextKeys
  from deerflow.runtime.user_context import DEFAULT_USER_ID, get_current_user
  from deerflow.subagents.runtime_catalog import trusted_runtime_agent_catalog
  from deerflow.token_budget_usage import TokenBudgetUsageRecorder

  from .execution_contracts import RunAgentResourceOwnership, RunSemanticStopRecorder
  from .manager import RunRecord

  __all__ = ["PrivateAgentRuntime", "PrivateRuntimeFactoryUnavailable", "RunContext"]
  ```

- [ ] **Step 3: re-export exact objects from `worker.py`**

  ```python
  from .runtime_binding import (  # noqa: F401 - compatibility exports
      PrivateAgentRuntime,
      PrivateRuntimeFactoryUnavailable,
      RunContext,
      _agent_factory_supports_app_config,
      _build_runtime_context,
      _cached_agent_factory_supports_app_config,
      _call_agent_factory_off_loop,
      _checkpoint_runtime_settings,
      _compute_agent_factory_supports_app_config,
      _install_runtime_context,
      _repository_trace_user_id,
  )
  ```

  `run_agent()` keeps referencing `RunContext` (signature), `_build_runtime_context` (until Task 6), `_call_agent_factory_off_loop`, `_checkpoint_runtime_settings`, `_install_runtime_context`, and `_repository_trace_user_id`. Remove `inspect`, `lru_cache`, `Protocol`, `dataclass`, `field`, `get_current_user`, `DEFAULT_USER_ID`, `CheckpointChannelMode`, and `RunFileAuthority` from `worker.py` only if no remaining reference exists. `deerflow.runtime.__getattr__` and `deerflow.runtime.runs.__getattr__` continue to resolve `RunContext` through `worker`, so `runtime_package.RunContext is owner.RunContext` holds without editing either package.

- [ ] **Step 4: migrate runtime-binding tests and the `inspect.signature` patch**

  - `test_run_worker_rollback.py`: import `_agent_factory_supports_app_config`, `_build_runtime_context`, and `_install_runtime_context` from `deerflow.runtime.runs.runtime_binding`; change line 2070 to `monkeypatch.setattr("deerflow.runtime.runs.runtime_binding.inspect.signature", lambda _obj: (_ for _ in ()).throw(ValueError("boom")))`. The patch still targets the global `inspect` module; the path must resolve through the module that imports `inspect`.
  - `test_host_execution_approval.py` line 38: import `_build_runtime_context` from `deerflow.runtime.runs.runtime_binding`.
  - `test_skill_builder_agent_runtime.py` lines 85-89: import `RunContext` and `_call_agent_factory_off_loop` from `deerflow.runtime.runs.runtime_binding` and keep `run_agent` on `deerflow.runtime.runs.worker`. Keep `monkeypatch.setattr(run_worker, "get_sandbox_provider", ...)` at line 1346 on `run_worker`; `run_agent()` still calls `get_sandbox_provider()` from `worker` globals.

- [ ] **Step 5: run runtime-context, factory, and Worker end-to-end gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_worker_rollback.py \
    tests/test_host_execution_approval.py \
    tests/test_skill_builder_agent_runtime.py \
    tests/test_tool_call_control_scope_checkpoint_acceptance.py \
    tests/test_memory_error_boundaries.py \
    -q -m "not postgres and not provider_integration"
  ```

  Require reserved/server-owned key stripping, private channel identity clearing, `agent_name` passthrough, non-dict caller context handling, signature-lookup fallback, private-runtime factory rejection messages, tool-call-control scope binding, and Run-scoped mount validation to pass unchanged.

- [ ] **Step 6: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/runtime_binding.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_worker_rollback.py \
    tests/test_host_execution_approval.py \
    tests/test_skill_builder_agent_runtime.py
  uvx ruff format --check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/runtime_binding.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_worker_rollback.py \
    tests/test_host_execution_approval.py \
    tests/test_skill_builder_agent_runtime.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 4 paths with message `refactor(worker): extract runtime binding`.

## Task 5: Extract goal continuation

**Files:**

- Create: `backend/packages/harness/deerflow/runtime/runs/goal_continuation.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py:2202-2208,2241-2306,2316-2354,2357-2675`
- Modify: `backend/tests/test_python_module_decomposition_worker_runtime.py`

**Interfaces:**

- Consumes: `deerflow.runtime.goal` helpers (`GoalWriteConflict`, `_call_checkpointer_method`, `_is_visible_message`, `_message_type`, `attach_goal_evaluation`, `compute_no_progress_count`, `evaluate_goal_completion`, `goal_thread_lock`, `latest_visible_assistant_signature`, `make_goal_continuation_message`, `read_thread_goal`, `should_continue_goal`, `visible_conversation_signature`, `write_thread_goal`, both default caps), `GoalEvaluation`, `GoalState`, `AuthorizationRevoked`, `serialize`, `message_to_text`, `Overwrite`, `CheckpointStateAccessor`, `build_state_mutation_graph`, `graph_state_schema`, and `_checkpoint_id`, `_snapshot_values`, `_materialized_checkpoint_snapshot`, `_materialized_checkpoint_messages`, `_read_checkpoint_messages` from `checkpoint_rollback`.
- Produces: `_goal_instance_matches`, `_materialized_checkpoint_goal`, `_build_run_local_mutation_accessor`, `_write_materialized_goal`, `_read_checkpoint_goal`, `_has_durable_goal_turn_receipt`, `_stand_down_reason`, `_persist_goal_evaluation`, `_reread_goal_and_checkpoint`, `_prepare_goal_continuation_input`.

- [ ] **Step 1: add the failing owner identity test and freeze the seam**

  ```python
  GOAL_CONTINUATION_NAMES = (
      "_goal_instance_matches",
      "_materialized_checkpoint_goal",
      "_build_run_local_mutation_accessor",
      "_write_materialized_goal",
      "_read_checkpoint_goal",
      "_has_durable_goal_turn_receipt",
      "_stand_down_reason",
      "_persist_goal_evaluation",
      "_reread_goal_and_checkpoint",
      "_prepare_goal_continuation_input",
  )


  def test_goal_continuation_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.runtime.runs.goal_continuation")
      for name in GOAL_CONTINUATION_NAMES:
          assert getattr(worker_legacy, name) is getattr(owner, name), name
      assert not hasattr(owner, "__all__")
      owner_imports = _module_imports(RUNS_ROOT / "goal_continuation.py")
      assert ".checkpoint_rollback" in owner_imports
      assert not owner_imports & {WORKER_MODULE, ".worker", ".stream_delivery", ".runtime_binding"}
  ```

  Run only this node. Expected: RED with `ModuleNotFoundError` for `goal_continuation`.

- [ ] **Step 2: move the goal helpers verbatim**

  Create `goal_continuation.py` with docstring `"""Hidden goal continuation: evaluate the active goal and queue the next turn."""`, its own `logger`, and move every Step 1 definition from the baseline coordinates. Preserve `goal_thread_lock` scopes, `as_node="goal_evaluator"`, the `max(continuation_count, current_count + 1)` race guard, every stand-down reason string (`"no_durable_end_of_turn"`, `"thread_changed_after_evaluation"`, `"thread_changed_before_continuation"`, `"max_continuations_reached"`, `"no_progress_detected"`, `f"blocked:{...}"`), `AuthorizationRevoked` re-raise, `GoalWriteConflict` stand-down, and every `logger.warning`/`logger.info` text.

  Owner imports:

  ```python
  from __future__ import annotations

  import asyncio
  import copy
  import logging
  from typing import Any

  from langgraph.types import Overwrite

  from deerflow.agents.goal_state import GoalEvaluation, GoalState
  from deerflow.config.app_config import AppConfig
  from deerflow.runtime.checkpoint_state import (
      CheckpointStateAccessor,
      build_state_mutation_graph,
      graph_state_schema,
  )
  from deerflow.runtime.events.stream_base import StreamBridge
  from deerflow.runtime.goal import (
      DEFAULT_MAX_GOAL_CONTINUATIONS,
      DEFAULT_MAX_NO_PROGRESS_CONTINUATIONS,
      GoalWriteConflict,
      _call_checkpointer_method,
      _is_visible_message,
      _message_type,
      attach_goal_evaluation,
      compute_no_progress_count,
      evaluate_goal_completion,
      goal_thread_lock,
      latest_visible_assistant_signature,
      make_goal_continuation_message,
      read_thread_goal,
      should_continue_goal,
      visible_conversation_signature,
      write_thread_goal,
  )
  from deerflow.runtime.serialization import serialize
  from deerflow.sandbox.sandbox import AuthorizationRevoked
  from deerflow.utils.messages import message_to_text

  from .checkpoint_rollback import (
      _checkpoint_id,
      _materialized_checkpoint_messages,
      _materialized_checkpoint_snapshot,
      _read_checkpoint_messages,
      _snapshot_values,
  )
  ```

- [ ] **Step 3: re-export exact objects from `worker.py`**

  ```python
  from .goal_continuation import (  # noqa: F401 - compatibility exports
      _build_run_local_mutation_accessor,
      _goal_instance_matches,
      _has_durable_goal_turn_receipt,
      _materialized_checkpoint_goal,
      _persist_goal_evaluation,
      _prepare_goal_continuation_input,
      _read_checkpoint_goal,
      _reread_goal_and_checkpoint,
      _stand_down_reason,
      _write_materialized_goal,
  )
  ```

  `run_agent()` keeps `create_goal_evaluator_model` and the `_get_goal_evaluator_model()` closure inline and keeps calling `_prepare_goal_continuation_input(...)` by name at the goal loop; do not wrap it. Remove the other `deerflow.runtime.goal` names, `GoalEvaluation`, `GoalState`, `Overwrite`, `build_state_mutation_graph`, `graph_state_schema`, and `message_to_text` from `worker.py` only if no remaining reference exists (`create_goal_evaluator_model` and `copy` remain used by `run_agent()`; Task 7 moves the legacy snapshot deep copies).

- [ ] **Step 4: run goal continuation, pause, and outcome gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_agent_outcome.py \
    tests/test_run_worker_host_execution_pause.py \
    tests/test_tool_call_control_scope_checkpoint_acceptance.py \
    tests/test_run_worker_rollback.py \
    -q -m "not postgres and not provider_integration"
  ```

  Require every `monkeypatch.setattr(run_worker, "_prepare_goal_continuation_input", ...)` test (loop-cap stand-down, approval-suspended continuation, checkpoint-scope acceptance) to observe its patched evaluator, and `test_batch5_run_agent_calls_frozen_module_seams_by_name` to pass. The three files above are the only repository tests that exercise `_prepare_goal_continuation_input`; do not add a new goal test file in this task.

- [ ] **Step 5: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/goal_continuation.py \
    tests/test_python_module_decomposition_worker_runtime.py
  uvx ruff format --check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/goal_continuation.py \
    tests/test_python_module_decomposition_worker_runtime.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 5 paths with message `refactor(worker): extract goal continuation`.

## Task 6: Extract stream-mode resolution and runtime binding phases from `run_agent()`

**Files:**

- Modify: `backend/packages/harness/deerflow/runtime/runs/stream_delivery.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/runtime_binding.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py` (baseline `run_agent()` lines 1164-1267 and 1456-1484; relocate by the `# 3. Build the agent` and `# 6. Build LangGraph stream_mode list` comments)
- Modify: `backend/tests/test_python_module_decomposition_worker_runtime.py`

**Interfaces:**

- Consumes: `_VALID_LG_MODES`, `_build_runtime_context`, `_install_runtime_context`, `RuntimeContextCarrier`, `RuntimeContextKeys`, `trusted_runtime_agent_catalog`, `normalize_trace_id`, `get_current_trace_id`, `RunScopedReadOnlyMount`, `RunRecoveredLLMFailureRecorder`, `RunSemanticStopRecorder`, `RunContext`, `RunRecord`.
- Produces: `ResolvedStreamModes`, `resolve_stream_modes()`, `BoundRunRuntime`, `bind_run_runtime_context()`.

- [ ] **Step 1: write the failing pure stream-mode test**

  ```python
  def test_resolve_stream_modes_always_consumes_values_and_records_published_subset() -> None:
      from deerflow.runtime.runs.stream_delivery import ResolvedStreamModes, resolve_stream_modes

      resolved = resolve_stream_modes({"messages-tuple", "events", "updates", "bogus"})
      assert isinstance(resolved, ResolvedStreamModes)
      assert isinstance(resolved.lg_modes, list)
      assert set(resolved.lg_modes) == {"messages", "updates", "values"}
      assert resolved.lg_modes[-1] == "values"
      assert resolved.published_lg_modes == frozenset({"messages", "updates"})

      only_values = resolve_stream_modes({"values"})
      assert only_values.lg_modes == ["values"]
      assert only_values.published_lg_modes == frozenset({"values"})

      nothing_valid = resolve_stream_modes({"events"})
      assert nothing_valid.lg_modes == ["values"]
      assert nothing_valid.published_lg_modes == frozenset()
  ```

  Run only this node. Expected: RED with `ImportError: cannot import name 'ResolvedStreamModes'`.

- [ ] **Step 2: add the stream-mode owner and switch `run_agent()` to it**

  Append to `stream_delivery.py`:

  ```python
  @dataclass(frozen=True, slots=True)
  class ResolvedStreamModes:
      """LangGraph modes the Worker consumes and the subset the caller receives."""

      lg_modes: list[str]
      published_lg_modes: frozenset[str]


  def resolve_stream_modes(requested_modes: set[str]) -> ResolvedStreamModes:
      """Map requested SSE modes onto LangGraph modes and always consume ``values``.

      ``events`` is not a valid ``astream`` mode and is skipped; ``messages-tuple``
      maps to LangGraph ``messages``. Order is preserved and duplicates removed.
      The parent graph's ``values`` lane is always consumed for semantic
      authority even when the caller did not request it; ``published_lg_modes``
      records the caller-visible subset.
      """
      lg_modes: list[str] = []
      for m in requested_modes:
          if m == "messages-tuple":
              lg_modes.append("messages")
          elif m == "events":
              continue
          elif m in _VALID_LG_MODES:
              lg_modes.append(m)
      seen: set[str] = set()
      deduped: list[str] = []
      for m in lg_modes:
          if m not in seen:
              seen.add(m)
              deduped.append(m)
      published_lg_modes = frozenset(deduped)
      lg_modes = deduped or ["values"]
      if "values" not in lg_modes:
          lg_modes.append("values")
      return ResolvedStreamModes(lg_modes=lg_modes, published_lg_modes=published_lg_modes)


  __all__ = ["ResolvedStreamModes", "resolve_stream_modes"]
  ```

  In `run_agent()`, replace the block from `# 6. Build LangGraph stream_mode list` through `lg_modes.append("values")` with:

  ```python
          # 6. Build LangGraph stream_mode list
          resolved_modes = resolve_stream_modes(requested_modes)
          lg_modes = resolved_modes.lg_modes
          published_lg_modes = resolved_modes.published_lg_modes
  ```

  Keep the following `logger.info("Run %s: streaming with modes %s (requested: %s)", ...)` line inline. `lg_modes` stays a `list[str]` so `agent.astream(stream_mode=lg_modes)` and `_unpack_stream_item(item, lg_modes, ...)` receive the same type as before. Add `resolve_stream_modes` to the `worker.py` `.stream_delivery` import block; `_VALID_LG_MODES` stays listed there as a compatibility export.

- [ ] **Step 3: add the failing runtime-binding phase test**

  ```python
  def test_bind_run_runtime_context_installs_context_runtime_and_model_name() -> None:
      import asyncio
      from types import SimpleNamespace

      from deerflow.runtime.context_keys import RuntimeContextKeys
      from deerflow.runtime.recovered_llm_failures import RunRecoveredLLMFailureRecorder
      from deerflow.runtime.runs.execution_contracts import RunSemanticStopRecorder
      from deerflow.runtime.runs.runtime_binding import BoundRunRuntime, RunContext, bind_run_runtime_context

      record = SimpleNamespace(
          run_id="run-1",
          thread_id="thread-1",
          model_name=None,
          abort_event=asyncio.Event(),
      )
      config: dict[str, object] = {"context": {"agent_name": "ok"}, "metadata": {}}
      bound = bind_run_runtime_context(
          ctx=RunContext(checkpointer=None, store="store-sentinel"),
          record=record,
          config=config,
          private_owner_user_id=None,
          file_authority=None,
          private_files_enabled=False,
          journal=None,
          token_usage_tracking_enabled=True,
          recovered_llm_failure_recorder=RunRecoveredLLMFailureRecorder(),
          semantic_stop_recorder=RunSemanticStopRecorder(),
          pre_existing_message_ids={"m-1"},
      )
      assert isinstance(bound, BoundRunRuntime)
      assert bound.runtime_context[RuntimeContextKeys.THREAD_ID] == "thread-1"
      assert bound.runtime_context[RuntimeContextKeys.RUN_ID] == "run-1"
      assert bound.runtime_context["agent_name"] == "ok"
      assert config["context"]["agent_name"] == "ok"
      runtime = config["configurable"]["__pregel_runtime"]
      assert runtime.context is bound.runtime_context
      assert runtime.store == "store-sentinel"
      assert RuntimeContextKeys.MODEL_NAME not in config["configurable"]
  ```

  Add `import pytest` to the contract module (Task 7 and Task 8 also use it). `bind_run_runtime_context()` is synchronous, so this test is a plain function; `asyncio.Event()` can be created without a running loop on Python 3.12. Run only this node. Expected: RED with `ImportError: cannot import name 'BoundRunRuntime'`.

- [ ] **Step 4: add the runtime-binding phase owner and switch `run_agent()` to it**

  Append to `runtime_binding.py` (add `from deerflow.runtime.recovered_llm_failures import RunRecoveredLLMFailureRecorder`, `from deerflow.sandbox.sandbox_provider import RunScopedReadOnlyMount`, `from deerflow.trace_context import get_current_trace_id, normalize_trace_id`, and `from functools import partial` to its imports):

  ```python
  @dataclass(frozen=True, slots=True)
  class BoundRunRuntime:
      """Runtime context installed into one Run's config before graph construction."""

      runtime_context: dict[str, Any]
      trace_id: str | None


  def bind_run_runtime_context(
      *,
      ctx: RunContext,
      record: RunRecord,
      config: dict[str, Any],
      private_owner_user_id: str | None,
      file_authority: object | None,
      private_files_enabled: bool,
      journal: Any | None,
      token_usage_tracking_enabled: bool,
      recovered_llm_failure_recorder: RunRecoveredLLMFailureRecorder,
      semantic_stop_recorder: RunSemanticStopRecorder,
      pre_existing_message_ids: set[str],
  ) -> BoundRunRuntime:
      """Build and install ``ToolRuntime.context`` and the parent ``Runtime`` for one Run.

      Mutates ``config`` exactly as the inline phase did: installs the sanitized
      runtime context, stores the parent runtime under
      ``configurable["__pregel_runtime"]``, and re-asserts the persisted private
      model name so absent or forged caller config cannot influence the private
      runtime factory.
      """
      from langgraph.runtime import Runtime

      run_id = record.run_id
      thread_id = record.thread_id
      runtime_ctx = _build_runtime_context(
          thread_id,
          run_id,
          config.get("context"),
          ctx.app_config,
          private_scope=ctx.private_scope,
          authorization_checker=ctx.authorization_checker,
          authorization_boundary=ctx.authorization_boundary,
          file_authority=file_authority,
          memory_authority=ctx.memory_authority,
          guardrail_attribution=ctx.guardrail_attribution,
          run_read_only_mounts=(
              (
                  RunScopedReadOnlyMount(
                      run_id=run_id,
                      container_path=ctx.app_config.skills.container_path,
                      host_path=str(ctx.private_agent_runtime.skill_root),
                  ),
              )
              if (not private_files_enabled and ctx.private_agent_runtime is not None and ctx.app_config is not None)
              else ()
          ),
          runtime_owner_user_id=private_owner_user_id,
          memory_archive_context=ctx.memory_archive_context,
          host_execution_approval_port=ctx.host_execution_approval_port,
          channel_user_id=ctx.channel_user_id,
          server_abort_event=record.abort_event,
          vision_dispatch_authority=ctx.vision_dispatch_authority,
          run_semantic_stop_recorder=semantic_stop_recorder,
          token_budget_usage_recorder=ctx.token_budget_usage_recorder,
      )
      # ... baseline lines 1203-1258 verbatim: private-runtime channel pinning,
      # skill_secret_provider partial, incoming metadata trace id, the
      # RuntimeContextCarrier(...).install_into(runtime_ctx) call with
      # current_run_pre_existing_message_ids=frozenset(pre_existing_message_ids),
      # and _install_runtime_context(config, runtime_ctx) ...
      runtime = Runtime(context=cast(Any, runtime_ctx), store=ctx.store)
      configurable = config.setdefault("configurable", {})
      configurable["__pregel_runtime"] = runtime
      if ctx.private_agent_runtime is not None:
          configurable[RuntimeContextKeys.MODEL_NAME] = record.model_name
      return BoundRunRuntime(runtime_context=runtime_ctx, trace_id=deerflow_trace_id)
  ```

  The elided middle is baseline lines 1203-1258 copied without edits (they only reference `ctx`, `record`, `config`, `journal`, `token_usage_tracking_enabled`, `recovered_llm_failure_recorder`, `pre_existing_message_ids`, and the local `runtime_ctx`). Keep the lazy `from langgraph.runtime import Runtime` inside the function so Gateway/Scheduler import cost is unchanged. Extend `__all__` to `["BoundRunRuntime", "PrivateAgentRuntime", "PrivateRuntimeFactoryUnavailable", "RunContext", "bind_run_runtime_context"]` and update the `owner.__all__` assertion in `test_runtime_binding_owner_is_the_exact_legacy_export` to that list in the same step.

  In `run_agent()`, replace the block from `# 3. Build the agent` through `configurable[RuntimeContextKeys.MODEL_NAME] = record.model_name` with:

  ```python
          # 3. Build the agent
          from langchain_core.runnables import RunnableConfig

          bound_runtime = bind_run_runtime_context(
              ctx=ctx,
              record=record,
              config=config,
              private_owner_user_id=private_owner_user_id,
              file_authority=private_files.authority,
              private_files_enabled=private_files.enabled,
              journal=journal,
              token_usage_tracking_enabled=token_usage_tracking_enabled,
              recovered_llm_failure_recorder=recovered_llm_failure_recorder,
              semantic_stop_recorder=semantic_stop_recorder,
              pre_existing_message_ids=pre_existing_message_ids,
          )
          runtime_ctx = bound_runtime.runtime_context
          deerflow_trace_id = bound_runtime.trace_id
  ```

  The following `run_mounts = runtime_ctx.get(RuntimeContextKeys.RUN_READ_ONLY_MOUNTS, ())` block, `get_sandbox_provider()` call, and mount validation stay inline in `run_agent()`. The later `RuntimeContextCarrier(current_run_pre_existing_message_ids=...).install_into(runtime_ctx); _install_runtime_context(config, runtime_ctx)` refresh after rollback capture also stays inline. Remove `normalize_trace_id`, `get_current_trace_id`, `trusted_runtime_agent_catalog`, and `cast` from `worker.py` imports only if no remaining reference exists; `RunScopedReadOnlyMount` remains used by the `run_mounts` annotation.

- [ ] **Step 5: run the complete Worker behavior gate**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_worker_rollback.py \
    tests/test_run_worker_rollback_settlement.py \
    tests/test_run_worker_host_execution_pause.py \
    tests/test_run_worker_private_file_lifecycle.py \
    tests/test_run_agent_outcome.py \
    tests/test_run_event_text_batching.py \
    tests/test_memory_error_boundaries.py \
    tests/test_skill_builder_agent_runtime.py \
    tests/test_tool_call_control_scope_checkpoint_acceptance.py \
    tests/test_host_execution_approval.py \
    -q -m "not postgres and not provider_integration"
  ```

  Require hidden `values` consumption when only `messages`/`updates` were requested, published-subset filtering, private model-name reassertion, Run-scoped mount validation/release, trace-id propagation to Langfuse metadata, skill-secret provider binding, and the pre-existing-message boundary to pass unchanged.

- [ ] **Step 6: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/stream_delivery.py \
    packages/harness/deerflow/runtime/runs/runtime_binding.py \
    tests/test_python_module_decomposition_worker_runtime.py
  uvx ruff format --check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/stream_delivery.py \
    packages/harness/deerflow/runtime/runs/runtime_binding.py \
    tests/test_python_module_decomposition_worker_runtime.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 6 paths with message `refactor(worker): extract stream modes and runtime binding phases`.

## Task 7: Extract legacy baseline and rollback-point capture phases from `run_agent()`

**Files:**

- Modify: `backend/packages/harness/deerflow/runtime/runs/checkpoint_rollback.py`
- Modify: `backend/packages/harness/deerflow/runtime/runs/worker.py` (baseline `run_agent()` lines 1067-1140 and 1370-1407; relocate by `if checkpointer is not None:` following `inject_checkpoint_mode(checkpoint_config, checkpoint_mode)` and by the `# Capture the rollback point only after...` comment)
- Modify: `backend/tests/test_python_module_decomposition_worker_runtime.py`

**Interfaces:**

- Consumes: `CheckpointModeMismatchError`, `aensure_checkpoint_mode_compatible`, `inject_checkpoint_mode`, `PublicRunError`, `PublicRunErrorCode`, `CheckpointChannelMode`, `_capture_rollback_point`, `_rollback_point_from_legacy_snapshot`, `_collect_pre_existing_message_ids`, `_collect_private_pre_existing_message_ids`.
- Produces: `PreRunCheckpointBaseline`, `capture_legacy_pre_run_baseline()`, `PreRunRollbackCapture`, `capture_pre_run_rollback_point()`.

- [ ] **Step 1: write the failing baseline-capture tests**

  ```python
  @pytest.mark.anyio
  async def test_capture_legacy_pre_run_baseline_maps_raw_capture_failure_by_run_kind() -> None:
      from deerflow.error_codes import PublicRunError, PublicRunErrorCode
      from deerflow.runtime.runs.checkpoint_rollback import PreRunCheckpointBaseline, capture_legacy_pre_run_baseline

      class Checkpointer:
          """Head is full-mode compatible; the raw pre-run capture then fails."""

          def __init__(self) -> None:
              self.calls = 0

          async def aget_tuple(self, _config):
              self.calls += 1
              if self.calls == 1:
                  return None
              raise RuntimeError("raw capture unavailable")

      checkpoint_config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
      public_checkpointer = Checkpointer()
      public = await capture_legacy_pre_run_baseline(
          checkpointer=public_checkpointer,
          checkpoint_config=dict(checkpoint_config),
          configurable={"thread_id": "thread-1", "checkpoint_ns": ""},
          checkpoint_mode="full",
          thread_id="thread-1",
          run_id="run-1",
          private_message_boundary_required=False,
      )
      assert isinstance(public, PreRunCheckpointBaseline)
      assert public_checkpointer.calls == 2
      assert public.snapshot_capture_failed is True
      assert public.pre_run_checkpoint_id is None
      assert public.legacy_pre_run_snapshot is None
      assert public.pre_existing_message_ids == set()

      with pytest.raises(PublicRunError) as raised:
          await capture_legacy_pre_run_baseline(
              checkpointer=Checkpointer(),
              checkpoint_config=dict(checkpoint_config),
              configurable={"thread_id": "thread-1", "checkpoint_ns": ""},
              checkpoint_mode="full",
              thread_id="thread-1",
              run_id="run-1",
              private_message_boundary_required=True,
          )
      assert raised.value.code is PublicRunErrorCode.PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE

      with pytest.raises(RuntimeError, match="head unavailable"):

          class BrokenHead:
              async def aget_tuple(self, _config):
                  raise RuntimeError("head unavailable")

          await capture_legacy_pre_run_baseline(
              checkpointer=BrokenHead(),
              checkpoint_config=dict(checkpoint_config),
              configurable={"thread_id": "thread-1", "checkpoint_ns": ""},
              checkpoint_mode="full",
              thread_id="thread-1",
              run_id="run-1",
              private_message_boundary_required=False,
          )
  ```

  The first `aget_tuple` call belongs to `aensure_checkpoint_mode_compatible` (a `None` head is full-mode compatible), the second to the raw full-mode capture. A public Run records `snapshot_capture_failed` for a raw-capture failure but re-raises a head-read failure; a private Run converts either into `PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE`. Run only this node. Expected: RED with `ImportError: cannot import name 'PreRunCheckpointBaseline'`.

- [ ] **Step 2: add the two capture phases to `checkpoint_rollback.py`**

  Extend the owner imports with `from deerflow.config.database_config import CheckpointChannelMode`, `from deerflow.error_codes import ROLLBACK_FAILED_ERROR_CODE, PublicRunError, PublicRunErrorCode`, and `from deerflow.runtime.checkpoint_mode import CheckpointModeMismatchError, aensure_checkpoint_mode_compatible, inject_checkpoint_mode`. Append:

  ```python
  @dataclass(frozen=True, slots=True)
  class PreRunCheckpointBaseline:
      """Checkpoint facts captured before the run-local Agent graph exists."""

      pre_run_checkpoint_id: str | None
      legacy_pre_run_snapshot: dict[str, Any] | None
      snapshot_capture_failed: bool
      pre_existing_message_ids: set[str]


  async def capture_legacy_pre_run_baseline(
      *,
      checkpointer: Any,
      checkpoint_config: dict[str, Any],
      configurable: dict[str, Any],
      checkpoint_mode: CheckpointChannelMode,
      thread_id: str,
      run_id: str,
      private_message_boundary_required: bool,
  ) -> PreRunCheckpointBaseline:
      """Validate checkpoint-mode compatibility, then capture the full-mode raw baseline.

      ``CheckpointModeMismatchError`` propagates unchanged. Any other head or
      historical-selector read failure becomes
      ``PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE`` for private Runs and re-raises
      for public Runs. Full-mode raw capture failures set
      ``snapshot_capture_failed`` for public Runs instead of raising. Delta mode
      never trusts raw ``channel_values``; the caller replaces this baseline with
      an exact materialized ``RollbackPoint`` after graph construction.
      """
      pre_run_checkpoint_id: str | None = None
      legacy_pre_run_snapshot: dict[str, Any] | None = None
      snapshot_capture_failed = False
      pre_existing_message_ids: set[str] = set()
      try:
          # ... baseline lines 1069-1092 verbatim (aensure head, selector build,
          # inject_checkpoint_mode(selected_checkpoint_config, ...), aensure selector) ...
      except CheckpointModeMismatchError:
          raise
      except Exception:
          if private_message_boundary_required:
              logger.warning(
                  "Private Run pre-run message boundary is unavailable for run %s",
                  run_id,
              )
              raise PublicRunError(PublicRunErrorCode.PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE) from None
          raise

      if checkpoint_mode == "full":
          try:
              # ... baseline lines 1110-1127 verbatim (aget_tuple, deep copies,
              # private boundary via _collect_private_pre_existing_message_ids) ...
          except Exception:
              snapshot_capture_failed = True
              if private_message_boundary_required:
                  logger.warning(
                      "Private Run pre-run message boundary is unavailable for run %s",
                      run_id,
                  )
                  raise PublicRunError(PublicRunErrorCode.PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE) from None
              logger.warning(
                  "Could not capture pre-run checkpoint snapshot for run %s",
                  run_id,
                  exc_info=True,
              )
      return PreRunCheckpointBaseline(
          pre_run_checkpoint_id=pre_run_checkpoint_id,
          legacy_pre_run_snapshot=legacy_pre_run_snapshot,
          snapshot_capture_failed=snapshot_capture_failed,
          pre_existing_message_ids=pre_existing_message_ids,
      )


  @dataclass(frozen=True, slots=True)
  class PreRunRollbackCapture:
      """Exact materialized rollback point plus the trusted message boundary."""

      rollback_point: RollbackPoint | None
      snapshot_capture_failed: bool
      pre_run_checkpoint_id: str | None
      pre_existing_message_ids: set[str]


  async def capture_pre_run_rollback_point(
      *,
      agent: Any,
      accessor: CheckpointStateAccessor,
      checkpointer: Any,
      checkpoint_config: dict[str, Any],
      checkpoint_mode: CheckpointChannelMode,
      thread_id: str,
      run_id: str,
      pre_run_checkpoint_id: str | None,
      legacy_pre_run_snapshot: dict[str, Any] | None,
      snapshot_capture_failed: bool,
      pre_existing_message_ids: set[str],
      private_message_boundary_required: bool,
  ) -> PreRunRollbackCapture:
      """Materialize the pre-run RollbackPoint through the compiled graph.

      Delta checkpoints do not contain complete raw ``channel_values``, so
      messages and restorable channels come from graph-materialized state and
      the raw saver is consulted only for exact pending writes. Inputs carry the
      legacy baseline so the full-mode compatibility adapter and the
      ``snapshot_capture_failed`` flag keep their inline semantics.
      """
      rollback_point: RollbackPoint | None = None
      can_materialize_state = callable(getattr(agent, "aget_state", None))
      try:
          # ... baseline lines 1373-1394 verbatim ...
      except Exception:
          snapshot_capture_failed = True
          if private_message_boundary_required:
              logger.warning(
                  "Private Run pre-run message boundary is unavailable for run %s",
                  run_id,
              )
              raise PublicRunError(PublicRunErrorCode.PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE) from None
          logger.warning(
              "Could not materialize pre-run checkpoint for run %s",
              run_id,
              exc_info=True,
          )
      return PreRunRollbackCapture(
          rollback_point=rollback_point,
          snapshot_capture_failed=snapshot_capture_failed,
          pre_run_checkpoint_id=pre_run_checkpoint_id,
          pre_existing_message_ids=pre_existing_message_ids,
      )
  ```

  The elided bodies are the baseline lines copied without edits; the only renames are that `ckpt_tuple`, `ckpt_config`, `selected_configurable`, `selected_checkpoint_config`, `has_historical_selector`, and `materialized_values` stay local to the new functions. In `capture_pre_run_rollback_point`, `snapshot_capture_failed = False` is assigned only on the successful `_capture_rollback_point` branch exactly as before, so the legacy-adapter branch leaves the incoming flag untouched. Extend `__all__` to `["PreRunCheckpointBaseline", "PreRunRollbackCapture", "RollbackPoint", "capture_legacy_pre_run_baseline", "capture_pre_run_rollback_point"]`.

- [ ] **Step 3: switch `run_agent()` to both phases**

  Replace the first `if checkpointer is not None:` body (baseline 1068-1140) with:

  ```python
          if checkpointer is not None:
              baseline = await capture_legacy_pre_run_baseline(
                  checkpointer=checkpointer,
                  checkpoint_config=checkpoint_config,
                  configurable=configurable,
                  checkpoint_mode=checkpoint_mode,
                  thread_id=thread_id,
                  run_id=run_id,
                  private_message_boundary_required=private_message_boundary_required,
              )
              pre_run_checkpoint_id = baseline.pre_run_checkpoint_id
              legacy_pre_run_snapshot = baseline.legacy_pre_run_snapshot
              snapshot_capture_failed = baseline.snapshot_capture_failed
              pre_existing_message_ids = baseline.pre_existing_message_ids
  ```

  Replace the rollback-capture block (baseline 1370-1407, from `can_materialize_state = ...` through the trailing `logger.warning("Could not materialize pre-run checkpoint for run %s", ...)`) with:

  ```python
              capture = await capture_pre_run_rollback_point(
                  agent=agent,
                  accessor=accessor,
                  checkpointer=checkpointer,
                  checkpoint_config=checkpoint_config,
                  checkpoint_mode=checkpoint_mode,
                  thread_id=thread_id,
                  run_id=run_id,
                  pre_run_checkpoint_id=pre_run_checkpoint_id,
                  legacy_pre_run_snapshot=legacy_pre_run_snapshot,
                  snapshot_capture_failed=snapshot_capture_failed,
                  pre_existing_message_ids=pre_existing_message_ids,
                  private_message_boundary_required=private_message_boundary_required,
              )
              rollback_point = capture.rollback_point
              snapshot_capture_failed = capture.snapshot_capture_failed
              pre_run_checkpoint_id = capture.pre_run_checkpoint_id
              pre_existing_message_ids = capture.pre_existing_message_ids
  ```

  Keep the surrounding `if checkpointer is not None:` guard, the preceding `# Capture the rollback point only after...` comment, and the following `_linearize_delta_checkpoint_resume(...)` call plus its `resumed_messages` boundary refresh inline and unchanged. Audited equivalence: when either phase raises `PublicRunError`, the inline code had already set `snapshot_capture_failed = True` while the new code leaves the caller's local unchanged; `snapshot_capture_failed` and `rollback_point` are only read by the rollback settlement path, which is unreachable from the `PublicRunError` handler and from `finally`, so no observable behavior changes.

  Update the `worker.py` `.checkpoint_rollback` import block: add `capture_legacy_pre_run_baseline` and `capture_pre_run_rollback_point`; `_capture_rollback_point` and `_rollback_point_from_legacy_snapshot` stay listed as compatibility exports, and both `_collect_*` functions remain used by the `resumed_messages` refresh. Remove `CheckpointModeMismatchError`, `aensure_checkpoint_mode_compatible`, and `copy` from `worker.py` only if no remaining reference exists; `inject_checkpoint_mode` remains used before the guard.

- [ ] **Step 4: add the final Worker import-shape gate**

  ```python
  def test_batch5_worker_still_defines_run_agent_and_only_reexports_moved_helpers() -> None:
      tree = _parse(WORKER_PATH)
      top_level_functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
      top_level_classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
      assert top_level_functions == {"run_agent"}
      assert top_level_classes == set()
      worker_imports = _module_imports(WORKER_PATH)
      assert {".checkpoint_rollback", ".stream_delivery", ".runtime_binding", ".goal_continuation"} <= worker_imports
      for name in WORKER_OWNER_MODULES:
          assert (RUNS_ROOT / f"{name}.py").is_file(), name
  ```

  Also update the `owner.__all__` assertion in `test_checkpoint_rollback_owner_is_the_exact_legacy_export` to `["PreRunCheckpointBaseline", "PreRunRollbackCapture", "RollbackPoint", "capture_legacy_pre_run_baseline", "capture_pre_run_rollback_point"]` (the runtime-binding assertion was already updated in Task 6).

- [ ] **Step 5: run the complete Worker behavior gate and both import orders**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_worker_rollback.py \
    tests/test_run_worker_rollback_settlement.py \
    tests/test_run_worker_host_execution_pause.py \
    tests/test_run_worker_private_file_lifecycle.py \
    tests/test_run_agent_outcome.py \
    tests/test_run_event_text_batching.py \
    tests/test_memory_error_boundaries.py \
    tests/test_skill_builder_agent_runtime.py \
    tests/test_tool_call_control_scope_checkpoint_acceptance.py \
    tests/test_host_execution_approval.py \
    -q -m "not postgres and not provider_integration"

  PYTHONPATH=. uv run python -c '
  from deerflow.runtime.runs import checkpoint_rollback, stream_delivery, runtime_binding, goal_continuation
  from deerflow.runtime.runs import worker
  import deerflow.runtime as runtime
  assert runtime.RunContext is runtime_binding.RunContext is worker.RunContext
  assert runtime.run_agent is worker.run_agent
  assert worker._prepare_goal_continuation_input is goal_continuation._prepare_goal_continuation_input
  assert worker._rollback_to_pre_run_checkpoint is checkpoint_rollback._rollback_to_pre_run_checkpoint
  assert worker._TextDeltaCoalescer is stream_delivery._TextDeltaCoalescer
  '
  PYTHONPATH=. uv run python -c '
  import deerflow.runtime as runtime
  ctx_type = runtime.RunContext
  from deerflow.runtime.runs import goal_continuation, runtime_binding, stream_delivery, checkpoint_rollback, worker
  assert ctx_type is runtime_binding.RunContext is worker.RunContext
  assert worker._settle_rollback is checkpoint_rollback._settle_rollback
  '
  ```

  Require zero failures, both import orders to exit 0 without starting any process or database connection, and `test_batch5_run_agent_calls_frozen_module_seams_by_name` plus `test_batch5_terminal_exception_ladders_are_frozen` to remain green. Confirm private-boundary `PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE`, public snapshot-capture warnings, delta rollback via materialized state, full-mode legacy adapter, and `rollback_point`-driven `_settle_requested_rollback` behave exactly as before.

- [ ] **Step 6: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/checkpoint_rollback.py \
    tests/test_python_module_decomposition_worker_runtime.py
  uvx ruff format --check \
    packages/harness/deerflow/runtime/runs/worker.py \
    packages/harness/deerflow/runtime/runs/checkpoint_rollback.py \
    tests/test_python_module_decomposition_worker_runtime.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 7 paths with message `refactor(worker): extract pre-run checkpoint capture phases`.

## Task 8: Extract pure executor outcome mapping

**Files:**

- Create: `backend/app/reliability/run_execution/outcome_mapping.py`
- Modify: `backend/app/reliability/run_execution/executor.py:375-433,1141-1183` (usage/outcome helpers and the post-runner mapping)
- Modify: `backend/tests/test_python_module_decomposition_worker_runtime.py`

**Interfaces:**

- Consumes: `PrivateRunUsageSnapshot`, `AgentExecutionResult`, `AmbiguousExternalSideEffect`, `PermanentExecutionError`, `PublicRunErrorCode`, `STREAM_TERMINAL_ERROR_CODES`, `RunAgentOutcome`, `RunAgentUsageSnapshot`, `RunRecord`, `TokenBudgetUsageRecorder`.
- Produces: `usage_snapshot()`, `outcome_usage_snapshot()`, `terminal_failure_result()`, `output_limit_error()`, `map_run_agent_outcome()`.

- [ ] **Step 1: write the failing pure mapping tests**

  ```python
  def _usage(**overrides):
      from app.private_work.run_repository import PrivateRunUsageSnapshot

      values = {
          "total_input_tokens": 10,
          "total_output_tokens": 5,
          "total_tokens": 15,
          "llm_call_count": 2,
          "lead_agent_tokens": 15,
          "subagent_tokens": 0,
          "middleware_tokens": 0,
          "token_usage_by_model": {},
          "token_budget_usage": None,
      }
      values.update(overrides)
      return PrivateRunUsageSnapshot(**values)


  def test_map_run_agent_outcome_keeps_the_inline_priority_order() -> None:
      from app.reliability.run_execution.errors import AmbiguousExternalSideEffect
      from app.reliability.run_execution.outcome_mapping import map_run_agent_outcome
      from deerflow.runtime.runs.execution_contracts import RunAgentOutcome, RunAgentUsageSnapshot

      usage = RunAgentUsageSnapshot(
          total_input_tokens=10,
          total_output_tokens=5,
          total_tokens=15,
          llm_call_count=2,
          lead_agent_tokens=15,
          subagent_tokens=0,
          middleware_tokens=0,
          token_usage_by_model={},
          token_budget_usage=None,
      )
      attempt = _usage()

      revoked = map_run_agent_outcome(
          RunAgentOutcome.succeeded(usage, suspended_approval_id="approval-1"),
          attempt_usage=attempt,
          authorization_revoked=True,
          cancel_requested=False,
          ambiguous_side_effect=False,
      )
      assert revoked.status == "cancelled"

      durable = map_run_agent_outcome(
          RunAgentOutcome.failed(usage, public_error_code="MODEL_OUTPUT_LIMIT"),
          attempt_usage=attempt,
          authorization_revoked=False,
          cancel_requested=True,
          ambiguous_side_effect=True,
      )
      assert durable.status == "failed"
      assert durable.public_error_code == "MODEL_OUTPUT_LIMIT"
      assert durable.retryable is False
      assert durable.durable_terminal is True

      with pytest.raises(AmbiguousExternalSideEffect):
          map_run_agent_outcome(
              RunAgentOutcome.failed(usage, public_error_code="AGENT_EXECUTION_FAILED"),
              attempt_usage=attempt,
              authorization_revoked=False,
              cancel_requested=False,
              ambiguous_side_effect=True,
          )

      cancelled = map_run_agent_outcome(
          RunAgentOutcome.succeeded(usage),
          attempt_usage=attempt,
          authorization_revoked=False,
          cancel_requested=True,
          ambiguous_side_effect=False,
      )
      assert cancelled.status == "cancelled"

      succeeded = map_run_agent_outcome(
          RunAgentOutcome.succeeded(usage, suspended_approval_id="approval-1"),
          attempt_usage=attempt,
          authorization_revoked=False,
          cancel_requested=False,
          ambiguous_side_effect=False,
      )
      assert succeeded.status == "succeeded"
      assert succeeded.suspended_approval_id == "approval-1"
      assert succeeded.attempt_usage == attempt


  def test_executor_outcome_helpers_are_exact_owner_functions() -> None:
      from app.reliability.run_execution import outcome_mapping as owner

      executor_class = executor_legacy.RunAgentPrivateExecutor
      assert executor_class._usage_snapshot is owner.usage_snapshot
      assert executor_class._outcome_usage_snapshot is owner.outcome_usage_snapshot
      assert executor_class._terminal_failure_result is owner.terminal_failure_result
      assert executor_class._output_limit_error is owner.output_limit_error
      assert owner.__all__ == ["map_run_agent_outcome", "outcome_usage_snapshot", "output_limit_error", "terminal_failure_result", "usage_snapshot"]
  ```

  If `RunAgentOutcome.failed`/`succeeded` or `RunAgentUsageSnapshot` take different keyword names in `execution_contracts.py`, match those exact names; do not edit `execution_contracts.py`. Run both nodes. Expected: RED with `ModuleNotFoundError` for `outcome_mapping`.

- [ ] **Step 2: create the pure owner**

  ```python
  """Pure private Run outcome mapping without database or authority side effects."""

  from __future__ import annotations

  from app.private_work.run_repository import PrivateRunUsageSnapshot
  from app.reliability.run_execution.contracts import AgentExecutionResult
  from app.reliability.run_execution.errors import (
      AmbiguousExternalSideEffect,
      PermanentExecutionError,
  )
  from deerflow.error_codes import PublicRunErrorCode
  from deerflow.runtime import RunRecord
  from deerflow.runtime.events.models import STREAM_TERMINAL_ERROR_CODES
  from deerflow.runtime.runs.execution_contracts import (
      RunAgentOutcome,
      RunAgentUsageSnapshot,
  )
  from deerflow.token_budget_usage import TokenBudgetUsageRecorder


  def usage_snapshot(
      record: RunRecord,
      recorder: TokenBudgetUsageRecorder | None = None,
  ) -> PrivateRunUsageSnapshot:
      # baseline executor lines 380-390 verbatim


  def outcome_usage_snapshot(
      usage: RunAgentUsageSnapshot,
      recorder: TokenBudgetUsageRecorder | None = None,
  ) -> PrivateRunUsageSnapshot:
      # baseline executor lines 397-407 verbatim


  def terminal_failure_result(
      public_error_code: str,
      *,
      attempt_usage: PrivateRunUsageSnapshot,
  ) -> AgentExecutionResult:
      return AgentExecutionResult.failed(
          public_error_code,
          retryable=False,
          attempt_usage=attempt_usage,
          durable_terminal=True,
      )


  def output_limit_error(
      record: RunRecord | None,
      *,
      lease_lost: bool,
      recorder: TokenBudgetUsageRecorder | None = None,
  ) -> PermanentExecutionError:
      return PermanentExecutionError(
          PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value,
          attempt_usage=(usage_snapshot(record, recorder) if record is not None and not lease_lost else None),
      )


  def map_run_agent_outcome(
      outcome: RunAgentOutcome,
      *,
      attempt_usage: PrivateRunUsageSnapshot,
      authorization_revoked: bool,
      cancel_requested: bool,
      ambiguous_side_effect: bool,
  ) -> AgentExecutionResult:
      """Map an immutable Worker outcome plus boundary facts onto the Job result.

      Priority is unchanged from the inline executor: revoked authority wins,
      then a failed outcome (durable stream terminal codes are non-retryable and
      an ambiguous external side effect raises), then an ordinary cancel
      request, then the Worker's own success or cancellation.
      """
      if authorization_revoked:
          return AgentExecutionResult.cancelled(attempt_usage=attempt_usage)
      if outcome.status == "failed":
          error_code = outcome.public_error_code
          if error_code is None:
              raise RuntimeError("failed Run Agent outcome has no error code")
          if error_code in STREAM_TERMINAL_ERROR_CODES:
              return terminal_failure_result(error_code, attempt_usage=attempt_usage)
          if ambiguous_side_effect:
              raise AmbiguousExternalSideEffect(attempt_usage=attempt_usage)
          return terminal_failure_result(error_code, attempt_usage=attempt_usage)
      if cancel_requested:
          return AgentExecutionResult.cancelled(attempt_usage=attempt_usage)
      if outcome.status == "succeeded":
          return AgentExecutionResult.succeeded(
              attempt_usage=attempt_usage,
              suspended_approval_id=outcome.suspended_approval_id,
          )
      if outcome.status == "cancelled":
          return AgentExecutionResult.cancelled(attempt_usage=attempt_usage)
      raise RuntimeError("Run Agent returned an unsupported outcome")


  __all__ = [
      "map_run_agent_outcome",
      "outcome_usage_snapshot",
      "output_limit_error",
      "terminal_failure_result",
      "usage_snapshot",
  ]
  ```

  The two elided bodies are the existing `PrivateRunUsageSnapshot(...)` constructions copied verbatim, including the `{model_name: dict(counters) ...}` copy and the `usage.token_budget_usage if ... else recorder.snapshot()` fallback.

- [ ] **Step 3: alias the class helpers and switch the post-runner mapping**

  In `executor.py`, delete the `_usage_snapshot`, `_outcome_usage_snapshot`, `_terminal_failure_result`, and `_output_limit_error` method definitions and add, at the top of the class body after the docstring:

  ```python
      _usage_snapshot = staticmethod(usage_snapshot)
      _outcome_usage_snapshot = staticmethod(outcome_usage_snapshot)
      _terminal_failure_result = staticmethod(terminal_failure_result)
      _output_limit_error = staticmethod(output_limit_error)
  ```

  Import `map_run_agent_outcome, outcome_usage_snapshot, output_limit_error, terminal_failure_result, usage_snapshot` from `app.reliability.run_execution.outcome_mapping`. Replace baseline lines 1143-1183 with:

  ```python
              attempt_usage = outcome_usage_snapshot(
                  outcome.usage,
                  token_budget_usage_recorder,
              )
              if context_evidence_observer is not None and not boundary.authorization_revoked:
                  await context_evidence_observer.record_settled()
              return map_run_agent_outcome(
                  outcome,
                  attempt_usage=attempt_usage,
                  authorization_revoked=boundary.authorization_revoked,
                  cancel_requested=boundary.cancel_requested,
                  ambiguous_side_effect=boundary.ambiguous_side_effect,
              )
  ```

  Keep the preceding `if boundary.lease_lost: raise TransientExecutionError("EXECUTION_AUTHORITY_UNAVAILABLE")` and `if type(outcome) is not RunAgentOutcome: raise TypeError(...)` lines inline. In every `except` handler, replace `self._usage_snapshot(record, token_budget_usage_recorder)` with `usage_snapshot(record, token_budget_usage_recorder)`, `self._terminal_failure_result(...)` with `terminal_failure_result(...)`, and `self._output_limit_error(...)` with `output_limit_error(...)`; the arguments and handler order do not change. Remove `PrivateRunUsageSnapshot` and `RunAgentUsageSnapshot` imports from `executor.py` only if no remaining reference exists.

- [ ] **Step 4: run executor outcome, settlement, and contract gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_model_output_limit_settlement.py \
    tests/test_context_provider_ambiguity_terminal.py \
    tests/test_run_execution_profile.py \
    tests/test_run_execution_modules.py \
    tests/test_skill_builder_provider_execution.py \
    tests/test_private_agent_mcp_discovery.py \
    -q -m "not postgres and not provider_integration"
  ```

  Require output-limit permanent errors with exact usage, non-retryable durable terminals, context-provider ambiguity terminal, revoked-authority cancellation, ambiguous-side-effect propagation, and `test_batch5_terminal_exception_ladders_are_frozen` to pass.

- [ ] **Step 5: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/reliability/run_execution/executor.py \
    app/reliability/run_execution/outcome_mapping.py \
    tests/test_python_module_decomposition_worker_runtime.py
  uvx ruff format --check \
    app/reliability/run_execution/executor.py \
    app/reliability/run_execution/outcome_mapping.py \
    tests/test_python_module_decomposition_worker_runtime.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 8 paths with message `refactor(executor): extract outcome mapping`.

## Task 9: Extract executor preparation

**Files:**

- Create: `backend/app/reliability/run_execution/preparation.py`
- Modify: `backend/app/reliability/run_execution/executor.py` (module helpers 181-279, `__init__` 285-367, staticmethods 484-588, and `_execute_with_trace` 683-1135 at post-Task-8 coordinates)
- Modify: `backend/tests/test_python_module_decomposition_worker_runtime.py`
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py:53,136-144,330-336`
- Modify: `backend/tests/test_worker_execution_approval_composition.py:8,31-36`
- Modify: `backend/tests/test_compaction_trigger_capacity_clamp.py:22-24`
- Modify: `backend/tests/test_run_execution_profile.py:788`
- Modify: `backend/tests/test_context_provider_ambiguity_terminal.py:261`
- Modify: `backend/tests/test_skill_builder_provider_execution.py:725,769`

**Interfaces:**

- Consumes: every `app.private_work.*`, `app.system_*`, `app.shared_assets.models`, `app.reliability.run_execution.{boundary,contracts,errors,ports,projections,stream_authority,tool_call_control_policy,vision_dispatch}` and `deerflow.*` import the moved bodies use today.
- Produces: `RunPreparationDependencies`, `FrozenRunPolicy`, `MaterializedRunAuthorities`, `BoundRunCheckpointer`, `required_current_upload_snapshot()`, `freeze_run_policy()`, `load_memory_archive_context()`, `materialize_private_runtime()`, `build_run_authorities()`, `bind_run_checkpointer()`, `build_run_context()`, `runner_config()`, `graph_input()`, plus `_context_compaction_threshold_tokens`, `_persisted_channel_user_id`, `_persisted_context_rebase_reason`, and `_PrivateRunThreadMetadataStore`.

- [ ] **Step 1: write the failing owner identity and composition tests**

  ```python
  PREPARATION_NAMES = (
      "RunPreparationDependencies",
      "FrozenRunPolicy",
      "MaterializedRunAuthorities",
      "BoundRunCheckpointer",
      "required_current_upload_snapshot",
      "freeze_run_policy",
      "load_memory_archive_context",
      "materialize_private_runtime",
      "build_run_authorities",
      "bind_run_checkpointer",
      "build_run_context",
      "runner_config",
      "graph_input",
      "_context_compaction_threshold_tokens",
      "_persisted_channel_user_id",
      "_persisted_context_rebase_reason",
      "_PrivateRunThreadMetadataStore",
  )


  def test_preparation_owner_is_exact_and_executor_keeps_compat_aliases() -> None:
      import dataclasses as dc

      from app.reliability.run_execution import preparation as owner

      for name in PREPARATION_NAMES:
          assert hasattr(owner, name), name
      executor_class = executor_legacy.RunAgentPrivateExecutor
      assert executor_class._graph_input is owner.graph_input
      assert executor_class._runner_config is owner.runner_config
      assert executor_class._required_current_upload_snapshot is owner.required_current_upload_snapshot
      assert executor_legacy._context_compaction_threshold_tokens is owner._context_compaction_threshold_tokens
      for frozen in (owner.RunPreparationDependencies, owner.FrozenRunPolicy, owner.MaterializedRunAuthorities, owner.BoundRunCheckpointer):
          assert dc.is_dataclass(frozen) and frozen.__dataclass_params__.frozen, frozen
      assert tuple(field.name for field in dc.fields(owner.FrozenRunPolicy)) == (
          "exact_model_name",
          "current_upload_snapshot",
          "runtime_app_config",
          "tool_call_control_policy",
          "vision_model",
          "delegate_model_names",
          "token_budget_usage_recorder",
      )
      assert owner.__all__ == [
          "BoundRunCheckpointer",
          "FrozenRunPolicy",
          "MaterializedRunAuthorities",
          "RunPreparationDependencies",
          "bind_run_checkpointer",
          "build_run_authorities",
          "build_run_context",
          "freeze_run_policy",
          "graph_input",
          "load_memory_archive_context",
          "materialize_private_runtime",
          "required_current_upload_snapshot",
          "runner_config",
      ]


  def test_executor_owns_boundary_record_runner_and_cleanup_while_preparation_owns_construction() -> None:
      executor_source = _function_node(EXECUTOR_PATH, "_execute_with_trace")
      executor_calls = _called_names(executor_source)
      assert {"PrivateRunExecutionBoundary", "RunManager", "freeze_run_policy", "load_memory_archive_context", "materialize_private_runtime", "build_run_authorities", "bind_run_checkpointer", "build_run_context", "map_run_agent_outcome", "push_current_app_config", "pop_current_app_config"} <= executor_calls
      assert not executor_calls & {"WorkerHostExecutionApprovalPort", "PrivateRunFileAuthority", "PrivateRunContextEvidenceObserver", "PrivateRunMemoryAuthority", "RunContext", "LeaseAuthorizedRunEventStore"}
      preparation_path = RUN_EXECUTION_ROOT / "preparation.py"
      authorities_calls = _called_names(_function_node(preparation_path, "build_run_authorities"))
      assert {"WorkerHostExecutionApprovalPort", "PrivateRunFileAuthority", "PrivateFileFinalizer", "resolve_model_ref"} <= authorities_calls
      checkpointer_calls = _called_names(_function_node(preparation_path, "bind_run_checkpointer"))
      assert "PrivateRunContextEvidenceObserver" in checkpointer_calls
      context_calls = _called_names(_function_node(preparation_path, "build_run_context"))
      assert {"RunContext", "PrivateRunMemoryAuthority", "LeaseAuthorizedRunEventStore", "_PrivateRunThreadMetadataStore"} <= context_calls
      assert {"LeaseAuthorizedStreamBridge", "SkillBuilderActivityStreamBridge"} <= executor_calls
  ```

  Run both nodes. Expected: RED with `ModuleNotFoundError` for `preparation`.

- [ ] **Step 2: create `preparation.py` with frozen dependencies and staged owners**

  Module docstring: `"""Private Run preparation: frozen policy, materialized assets, authorities, checkpointer, RunContext."""`. Move `_context_compaction_threshold_tokens`, `_persisted_channel_user_id`, `_persisted_context_rebase_reason`, and `_PrivateRunThreadMetadataStore` verbatim (baseline 181-279). Move `_required_current_upload_snapshot`, `_runner_config`, and `_graph_input` bodies verbatim as module functions `required_current_upload_snapshot(run_kwargs)`, `runner_config(execution, archive_context)`, and `graph_input(execution)`. Move `_memory_archive_context` (baseline 435-482) as `load_memory_archive_context(execution, runtime_app_config, *, session_factory)` replacing `self._factory()` with `session_factory()`. Then add:

  ```python
  @dataclass(frozen=True, slots=True)
  class RunPreparationDependencies:
      """Executor-owned collaborators that preparation reads but never owns."""

      session_factory: async_sessionmaker[AsyncSession]
      app_config: Any
      store: Any
      event_store: Any
      project_checkpointer: ProjectScopedCheckpointer
      model_materializer: SystemModelMaterializationPort | None
      runtime_policy_materializer: SystemRuntimePolicyMaterializationPort | None
      quota: PrivateRunAgentQuotaPort
      file_finalization_audit: PrivateFileFinalizationAuditPort | None
      execution_approval_audit: HostExecutionApprovalAuditPort
      host_execution_domain: HostExecutionDomainSnapshot | None


  @dataclass(frozen=True, slots=True)
  class FrozenRunPolicy:
      """Admitted model, policy, upload, and budget facts frozen before any resource is acquired."""

      exact_model_name: str
      current_upload_snapshot: tuple[CurrentUploadSnapshotEntry, ...]
      runtime_app_config: Any
      tool_call_control_policy: ResolvedRunToolCallControlPolicy | None
      vision_model: ModelConfig | None
      delegate_model_names: dict[uuid.UUID, str]
      token_budget_usage_recorder: TokenBudgetUsageRecorder | None


  @dataclass(frozen=True, slots=True)
  class MaterializedRunAuthorities:
      """Run-scoped ports built on top of the materialized private runtime."""

      host_execution_approval_port: WorkerHostExecutionApprovalPort | None
      file_authority: PrivateRunFileAuthority | None


  @dataclass(frozen=True, slots=True)
  class BoundRunCheckpointer:
      """Project-scoped checkpointer bound to the boundary and Context Evidence observer."""

      checkpointer: Any
      context_evidence_observer: PrivateRunContextEvidenceObserver | None


  async def freeze_run_policy(
      execution: PrivateRunExecution,
      deps: RunPreparationDependencies,
  ) -> FrozenRunPolicy:
      """Freeze upload snapshot, runtime policy, lead/delegate/auxiliary models, and token budget.

      Raises ``PermanentExecutionError`` with the exact inline codes
      (``RUN_CURRENT_UPLOAD_STALE``, ``RUN_ASSET_STALE``, ``RUN_POLICY_STALE``)
      and never acquires a releasable resource.
      """
      current_upload_snapshot = required_current_upload_snapshot(execution.run.kwargs)
      exact_model_name = execution.run.model_name
      if exact_model_name is None:
          raise PermanentExecutionError("RUN_ASSET_STALE")
      runtime_app_config = deps.app_config
      # ... baseline executor lines 691-837 verbatim with ``self._runtime_policy_materializer``
      # -> ``deps.runtime_policy_materializer``, ``self._model_materializer`` ->
      # ``deps.model_materializer``, ``self._app_config`` -> ``deps.app_config`` ...
      return FrozenRunPolicy(
          exact_model_name=exact_model_name,
          current_upload_snapshot=current_upload_snapshot,
          runtime_app_config=runtime_app_config,
          tool_call_control_policy=tool_call_control_policy,
          vision_model=vision_model,
          delegate_model_names=delegate_model_names,
          token_budget_usage_recorder=token_budget_usage_recorder,
      )


  async def materialize_private_runtime(
      execution: PrivateRunExecution,
      admitted: AdmittedPrivateRun,
      policy: FrozenRunPolicy,
      *,
      asset_runtime: PrivateAssetRuntime,
      boundary: PrivateRunExecutionBoundary,
  ) -> PrivateAgentRuntime:
      """Materialize the admitted private runtime and return it without further checks.

      The caller assigns the result before anything else can raise so its
      ``finally`` block releases exactly what was acquired.
      """
      materialize_kwargs: dict[str, object] = {
          "authorization_boundary": boundary,
          "runtime_kind": execution.runtime_kind,
      }
      if policy.delegate_model_names:
          materialize_kwargs["delegate_model_names"] = policy.delegate_model_names
      return await asset_runtime.materialize(
          execution.context,
          admitted,
          **materialize_kwargs,
      )


  def build_run_authorities(
      execution: PrivateRunExecution,
      policy: FrozenRunPolicy,
      private_runtime: PrivateAgentRuntime,
      *,
      claim: JobClaim,
      boundary: PrivateRunExecutionBoundary,
      deps: RunPreparationDependencies,
  ) -> MaterializedRunAuthorities:
      """Verify the materialized model, then build the Host Execution port and File Authority."""
      resolved_runtime_model = resolve_model_ref(
          policy.runtime_app_config,
          private_runtime.model_ref,
      )
      if getattr(resolved_runtime_model, "name", None) != policy.exact_model_name:
          raise PermanentExecutionError("RUN_ASSET_STALE")
      # ... baseline executor lines 863-917 verbatim with ``runtime_app_config`` ->
      # ``policy.runtime_app_config``, ``current_upload_snapshot`` ->
      # ``policy.current_upload_snapshot``, ``self._factory`` -> ``deps.session_factory``,
      # ``self._quota`` -> ``deps.quota``, ``self._file_finalization_audit`` ->
      # ``deps.file_finalization_audit``, ``self._host_execution_domain`` ->
      # ``deps.host_execution_domain``, ``self._execution_approval_audit`` ->
      # ``deps.execution_approval_audit``; ``file_authority`` starts as ``None`` and the
      # ``skill_container_path``/``run_skill_tree`` locals feed only the
      # ``PrivateRunFileAuthority`` constructor exactly as before ...
      return MaterializedRunAuthorities(
          host_execution_approval_port=host_execution_approval_port,
          file_authority=file_authority,
      )


  def bind_run_checkpointer(
      execution: PrivateRunExecution,
      policy: FrozenRunPolicy,
      *,
      boundary: PrivateRunExecutionBoundary,
      deps: RunPreparationDependencies,
  ) -> BoundRunCheckpointer:
      """Bind the project-scoped checkpointer to the boundary and, for chat Runs, Context Evidence."""
      checkpointer = deps.project_checkpointer.for_context(
          execution.context,
          thread_kind=execution.runtime_kind,
      )
      # ... baseline executor lines 947-1024 verbatim with ``runtime_app_config`` ->
      # ``policy.runtime_app_config``, ``exact_model_name`` -> ``policy.exact_model_name``,
      # ``self._factory`` -> ``deps.session_factory``; ``context_evidence_observer``
      # starts as ``None`` ...
      return BoundRunCheckpointer(
          checkpointer=checkpointer,
          context_evidence_observer=context_evidence_observer,
      )


  def build_run_context(
      execution: PrivateRunExecution,
      policy: FrozenRunPolicy,
      *,
      claim: JobClaim,
      boundary: PrivateRunExecutionBoundary,
      checkpointer: Any,
      context_evidence_observer: PrivateRunContextEvidenceObserver | None,
      file_authority: PrivateRunFileAuthority | None,
      memory_archive_context: SnipArchiveContext,
      private_runtime: PrivateAgentRuntime,
      host_execution_approval_port: WorkerHostExecutionApprovalPort | None,
      vision_dispatch_authority: PrivateRunVisionDispatchAuthority | None,
      resource_ownership: RunAgentResourceOwnership,
      deps: RunPreparationDependencies,
  ) -> RunContext:
      """Build the memory authority, channel identity, and the Worker ``RunContext``."""
      # ... baseline executor lines 1025-1082 verbatim with ``runtime_app_config`` ->
      # ``policy.runtime_app_config``, ``tool_call_control_policy`` ->
      # ``policy.tool_call_control_policy``, ``token_budget_usage_recorder`` ->
      # ``policy.token_budget_usage_recorder``, ``archive_context`` ->
      # ``memory_archive_context``, ``self._factory`` -> ``deps.session_factory``,
      # ``self._store`` -> ``deps.store``, ``self._event_store`` -> ``deps.event_store``,
      # ``self._file_finalization_audit`` -> ``deps.file_finalization_audit`` ...
      return run_context


  __all__ = [
      "BoundRunCheckpointer",
      "FrozenRunPolicy",
      "MaterializedRunAuthorities",
      "RunPreparationDependencies",
      "bind_run_checkpointer",
      "build_run_authorities",
      "build_run_context",
      "freeze_run_policy",
      "graph_input",
      "load_memory_archive_context",
      "materialize_private_runtime",
      "required_current_upload_snapshot",
      "runner_config",
  ]
  ```

  Every elided body is the executor's current code with only the listed substitutions. Do not reorder any `await`, materializer call, `PermanentExecutionError` raise, or constructor keyword. Import `PrivateAgentRuntime` from `app.private_work.private_agent_runtime`, `HostExecutionApprovalAuditPort` from `app.private_work.execution_approval_audit`, `AdmittedPrivateRun` from `app.private_work.run_admission`, `JobClaim` from `deerflow.persistence.jobs.sql`, `RunContext` from `deerflow.runtime`, and `RunAgentResourceOwnership` from `deerflow.runtime.runs.execution_contracts`; move the remaining imports the bodies need from `executor.py` unchanged. Never import `app.reliability.run_execution.executor`.

- [ ] **Step 3: rewrite `RunAgentPrivateExecutor` around the staged owners**

  In `executor.py`:

  - Keep `__init__` parameters and every existing `self._*` attribute (tests read `_factory`, `_asset_runtime`, `_knowledge_module`). After `self._execution_approval_audit` is assigned, add:

    ```python
            self._preparation = RunPreparationDependencies(
                session_factory=session_factory,
                app_config=app_config,
                store=store,
                event_store=event_store,
                project_checkpointer=project_checkpointer,
                model_materializer=model_materializer,
                runtime_policy_materializer=runtime_policy_materializer,
                quota=self._quota,
                file_finalization_audit=audit,
                execution_approval_audit=self._execution_approval_audit,
                host_execution_domain=self._host_execution_domain,
            )
    ```

  - Delete `_memory_archive_context`, `_required_current_upload_snapshot`, `_runner_config`, `_graph_input`, and the four module helpers. Add class aliases beside the Task 8 aliases:

    ```python
        _required_current_upload_snapshot = staticmethod(required_current_upload_snapshot)
        _runner_config = staticmethod(runner_config)
        _graph_input = staticmethod(graph_input)
    ```

  - Keep `_default_agent_factory`, `_resolve_agent_factory`, `_admitted`, and `execute` unchanged.
  - Add the compatibility re-export `from app.reliability.run_execution.preparation import _context_compaction_threshold_tokens as _context_compaction_threshold_tokens` alongside the owner imports.
  - Replace the body of `_execute_with_trace` from `try:` through the runner `finally` with:

    ```python
            try:
                policy = await freeze_run_policy(execution, self._preparation)
                token_budget_usage_recorder = policy.token_budget_usage_recorder
                archive_context = await load_memory_archive_context(
                    execution,
                    policy.runtime_app_config,
                    session_factory=self._factory,
                )
                push_current_app_config(policy.runtime_app_config)
                runtime_config_pushed = True
                private_runtime = await materialize_private_runtime(
                    execution,
                    admitted,
                    policy,
                    asset_runtime=self._asset_runtime,
                    boundary=boundary,
                )
                authorities = build_run_authorities(
                    execution,
                    policy,
                    private_runtime,
                    claim=claim,
                    boundary=boundary,
                    deps=self._preparation,
                )
                file_authority = authorities.file_authority
                run_manager = RunManager()
                record = await run_manager.register_persisted(
                    run_id=execution.run.run_id,
                    thread_id=execution.run.thread_id,
                    assistant_id=execution.run.assistant_id,
                    on_disconnect=DisconnectMode.continue_,
                    metadata=execution.run.metadata,
                    kwargs=execution.run.kwargs,
                    multitask_strategy=execution.run.multitask_strategy,
                    model_name=policy.exact_model_name,
                    scope=execution.context.resource_scope,
                    created_at=execution.run.created_at.isoformat(),
                )
                boundary.bind_abort_event(record.abort_event)
                authority.bind_cancel_callback(boundary.request_local_cancel)
                if authority.cancel_requested:
                    boundary.request_local_cancel()
                vision_dispatch_authority = (
                    PrivateRunVisionDispatchAuthority(
                        boundary=boundary,
                    )
                    if policy.vision_model is not None
                    else None
                )
                bound_checkpointer = bind_run_checkpointer(
                    execution,
                    policy,
                    boundary=boundary,
                    deps=self._preparation,
                )
                context_evidence_observer = bound_checkpointer.context_evidence_observer
                run_context = build_run_context(
                    execution,
                    policy,
                    claim=claim,
                    boundary=boundary,
                    checkpointer=bound_checkpointer.checkpointer,
                    context_evidence_observer=context_evidence_observer,
                    file_authority=file_authority,
                    memory_archive_context=archive_context,
                    private_runtime=private_runtime,
                    host_execution_approval_port=authorities.host_execution_approval_port,
                    vision_dispatch_authority=vision_dispatch_authority,
                    resource_ownership=resource_ownership,
                    deps=self._preparation,
                )
                owner_token = set_current_user(
                    SimpleNamespace(id=execution.run.owner_user_id),
                )
                storage_token = set_runtime_storage_user_id(
                    execution.run.owner_user_id,
                )
                try:
                    # ... baseline lines 1090-1132 verbatim except
                    # ``graph_input=graph_input(execution)`` and
                    # ``config=runner_config(execution, archive_context)`` ...
                finally:
                    reset_runtime_storage_user_id(storage_token)
                    reset_current_user(owner_token)
    ```

    Everything after the runner `finally` (lease check, type check, Task 8 mapping, the complete `except` ladder, and the `finally` cleanup) stays exactly as it is after Task 8. The local names `private_runtime`, `file_authority`, `record`, `context_evidence_observer`, `resource_ownership`, `runtime_config_pushed`, and `token_budget_usage_recorder` keep their pre-`try` initializations so every handler and the cleanup read the same values they read today. Remove imports that `executor.py` no longer references (`AccountPersonalizationRepository`, `PrivateRunFileAuthority` and the other `sandbox_files` names, `WorkerHostExecutionApprovalPort`, `HostExecutionProviderPolicySnapshot`, `PrivateFileFinalizer`, `PrivateRunMemoryAuthority`, `PrivateThreadRepository`, `LeaseAuthorizedRunEventStore`, `resolve_run_tool_call_control_policy`, `ResolvedRunToolCallControlPolicy`, `_private_guardrail_attribution`, `AssetKind`, `auxiliary_model_snapshot_ref`, `model_execution_provenance`, `resolve_model_ref`, `MEMORY_ARCHIVE_CONTEXT_KEY`, `SnipArchiveContext`, `resolve_effective_compaction_policy`, `ModelConfig`, `DEFAULT_MEMORY_NAMESPACE`, `DEFAULT_PRIVATE_MEMORY_NAMESPACE`, `ContextRebaseReason`, `ContextSubject`, `HOST_EXECUTION_MAX_CHANNEL_USER_ID_LENGTH`, `TokenBudgetUsageSnapshot`, `RemoveMessage`, `BaseMessage`, `convert_to_messages`, `Command`, `RUN_EXECUTION_PROFILE_KWARG`, `RunExecutionProfileUnsupported`, `effective_run_execution_profile_from_kwargs`, `_strip_client_memory_archive_receipt`, `agent_model_snapshot_purpose`, `copy`, `uuid`; keep `Mapping` because `__init__` still uses it). Keep `PrivateRunContextEvidenceObserver`, `RunRecord`, and `TokenBudgetUsageRecorder` for the local annotations, keep `PrivateRunExecutionBoundary` because the executor still constructs it, and keep `JobClaim`, `AdmittedPrivateRun`, and the `app.reliability.jobs` names for `_admitted`/`_resolve_agent_factory`.

- [ ] **Step 4: migrate test seams that patched construction sites**

  - `test_run_execution_profile.py` line 788 and `test_context_provider_ambiguity_terminal.py` line 261: patch `"app.reliability.run_execution.preparation.PrivateRunContextEvidenceObserver"`. Keep `"app.reliability.run_execution.executor.PrivateRunExecutionBoundary"` at `test_context_provider_ambiguity_terminal.py` line 265 because the executor still constructs the boundary.
  - `test_skill_builder_provider_execution.py` lines 725 and 769: patch `"app.reliability.run_execution.preparation.PrivateRunFileAuthority"`.
  - `test_compaction_trigger_capacity_clamp.py` lines 22-24: import `_context_compaction_threshold_tokens` from `app.reliability.run_execution.preparation`.
  - `test_worker_execution_approval_composition.py`: import `build_run_authorities` from `app.reliability.run_execution.preparation` and change the port inspection to `_call_keywords(build_run_authorities, "WorkerHostExecutionApprovalPort")`; keep the `RunAgentPrivateExecutor` import for the `run_worker` composition assertion.
  - `tests/knowledge/test_agent_tool.py` patches of `executor_module.SkillBuilderAgentFactory`, `WorkerSkillBuilderAuthoringCatalog`, and `SkillDesignDraftSink` stay on the executor because `_resolve_agent_factory` stays there.
  - `test_python_module_decomposition_process_boundaries.py`: add `PREPARATION_PATH = RUN_EXECUTION_ROOT / "preparation.py"` after line 53; in `EXECUTION_APPROVAL_PRODUCTION_CONSUMERS` replace `EXECUTOR_PATH` with `PREPARATION_PATH`; in `expected_owner_imports` change the `EXECUTOR_PATH` key to `PREPARATION_PATH` with the same `{"app.private_work.execution_approval_policy", "app.private_work.execution_approval_worker"}` value. `test_worker_executor_constructs_one_skill_builder_sink_without_agent_design` keeps asserting `EXECUTOR_PATH` constructs one `SkillDesignDraftSink`.
  - Leave `EXECUTOR_MODULE_COMPATIBILITY_NAMES` in the Batch 5 contract module unchanged. It is the frozen upper bound of legacy consumers and the scanner asserts `observed <= expected`; after this task the observed executor set shrinks to `{"RunAgentPrivateExecutor", "PrivateRunExecutionBoundary", "SkillBuilderAgentFactory", "WorkerSkillBuilderAuthoringCatalog", "SkillDesignDraftSink"}`.

- [ ] **Step 5: run the complete executor gate**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_execution_modules.py \
    tests/test_run_execution_profile.py \
    tests/test_model_output_limit_settlement.py \
    tests/test_context_provider_ambiguity_terminal.py \
    tests/test_chat_control_replay_identity.py \
    tests/test_current_upload_vision.py \
    tests/test_compaction_trigger_capacity_clamp.py \
    tests/test_skill_builder_provider_execution.py \
    tests/test_private_agent_mcp_discovery.py \
    tests/test_worker_execution_approval_composition.py \
    tests/knowledge/test_agent_tool.py \
    -q -m "not postgres and not provider_integration"

  PYTHONPATH=. uv run python -c '
  from app.reliability.run_execution import preparation, outcome_mapping
  from app.reliability.run_execution import executor
  import app.reliability.execution as legacy
  assert legacy.RunAgentPrivateExecutor is executor.RunAgentPrivateExecutor
  assert executor.RunAgentPrivateExecutor._graph_input is preparation.graph_input
  assert executor.RunAgentPrivateExecutor._terminal_failure_result is outcome_mapping.terminal_failure_result
  assert executor._context_compaction_threshold_tokens is preparation._context_compaction_threshold_tokens
  '
  PYTHONPATH=. uv run python -c '
  import app.reliability.execution as legacy
  from app.reliability.run_execution import executor, preparation
  assert legacy.RunAgentPrivateExecutor is executor.RunAgentPrivateExecutor
  assert executor.RunAgentPrivateExecutor._runner_config is preparation.runner_config
  '
  ```

  Require frozen profile/model staleness codes, upload staleness, policy staleness, title binding, vision model requirements, Skill Builder non-interactive config, `RemoveMessage`/`Command` payload conversion, file-authority release on runner failure, private runtime cleanup order, context-evidence settlement, knowledge tool factory resolution, and both import orders to pass. Confirm `test_batch5_terminal_exception_ladders_are_frozen` and `test_batch5_executor_public_shapes_are_frozen` remain green.

- [ ] **Step 6: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/reliability/run_execution/executor.py \
    app/reliability/run_execution/preparation.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_worker_execution_approval_composition.py \
    tests/test_compaction_trigger_capacity_clamp.py \
    tests/test_run_execution_profile.py \
    tests/test_context_provider_ambiguity_terminal.py \
    tests/test_skill_builder_provider_execution.py
  uvx ruff format --check \
    app/reliability/run_execution/executor.py \
    app/reliability/run_execution/preparation.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_worker_execution_approval_composition.py \
    tests/test_compaction_trigger_capacity_clamp.py \
    tests/test_run_execution_profile.py \
    tests/test_context_provider_ambiguity_terminal.py \
    tests/test_skill_builder_provider_execution.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 9 paths with message `refactor(executor): extract run preparation`.

## Task 10: Document ownership and run complete Batch 5 verification

**Files:**

- Modify: `backend/AGENTS.md:58-66` (add one bullet under `## Where changes live`)
- Modify: `backend/tests/test_python_module_decomposition_worker_runtime.py`
- Verify: every production and test path changed in Tasks 1-9
- Preserve: Batch 4 paths, `backend/tests/test_skill_builder_durable_agent_postgres.py`, and the pre-existing untracked decomposition documents

**Interfaces:**

- Consumes: the four Harness owners, two executor owners, `worker.py`, `executor.py`, migrated tests, and current compatibility/behavior tests.
- Produces: documented maintenance ownership plus current focused, PostgreSQL, import-order, static, and full-backend evidence.

- [ ] **Step 1: document the stable owner boundaries**

  Add this bullet to `backend/AGENTS.md` under `## Where changes live`, after the Sandbox tooling bullet and formatted to the guide width:

  > Harness Worker execution is owned by `packages/harness/deerflow/runtime/runs/`: `worker.py` keeps `run_agent()` (preparation orchestration, graph streaming, business terminal priority, and cleanup order) and re-exports legacy private names; `checkpoint_rollback.py` owns checkpoint reads, the pre-run message boundary, rollback capture/restore, and pre-run captures; `stream_delivery.py` owns frame batching, publishing, stream modes, and root-lane fallback/approval markers; `runtime_binding.py` owns `RunContext`, runtime-context binding, and Agent factory invocation; `goal_continuation.py` owns hidden goal continuation. Tests patch `_prepare_goal_continuation_input`, `_rollback_to_pre_run_checkpoint`, `_settle_rollback`, and `get_sandbox_provider` on `worker`, and patch every other moved helper on its owner. The private Run executor keeps lease boundary, record registration, the runner call, exception priority, and cleanup in `app/reliability/run_execution/executor.py`; `preparation.py` owns frozen policy/models, materialization, authorities, checkpointer, and `RunContext` construction behind frozen dataclasses; `outcome_mapping.py` owns pure usage and outcome mapping.

  Update no README, frontend documentation, or feature changelog because runtime architecture, process responsibility, and public contracts are unchanged.

- [ ] **Step 2: add the final existence and façade-shape gate**

  ```python
  def test_batch5_all_owner_modules_exist_and_executor_is_not_a_facade() -> None:
      for name in WORKER_OWNER_MODULES:
          assert (RUNS_ROOT / f"{name}.py").is_file(), name
      for name in EXECUTOR_OWNER_MODULES:
          assert (RUN_EXECUTION_ROOT / f"{name}.py").is_file(), name
      executor_tree = _parse(EXECUTOR_PATH)
      classes = {node.name for node in executor_tree.body if isinstance(node, ast.ClassDef)}
      assert classes == {"RunAgentPrivateExecutor"}
      assert executor_legacy.__all__ == ["RunAgentPrivateExecutor"]
      worker_functions = {node.name for node in _parse(WORKER_PATH).body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
      assert worker_functions == {"run_agent"}
  ```

- [ ] **Step 3: run the combined focused suite once**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_python_module_decomposition_worker_runtime.py \
    tests/test_run_worker_rollback.py \
    tests/test_run_worker_rollback_settlement.py \
    tests/test_run_worker_host_execution_pause.py \
    tests/test_run_worker_private_file_lifecycle.py \
    tests/test_run_agent_outcome.py \
    tests/test_run_event_text_batching.py \
    tests/test_memory_error_boundaries.py \
    tests/test_skill_builder_agent_runtime.py \
    tests/test_tool_call_control_scope_checkpoint_acceptance.py \
    tests/test_host_execution_approval.py \
    tests/test_run_execution_modules.py \
    tests/test_run_execution_profile.py \
    tests/test_model_output_limit_settlement.py \
    tests/test_context_provider_ambiguity_terminal.py \
    tests/test_chat_control_replay_identity.py \
    tests/test_current_upload_vision.py \
    tests/test_compaction_trigger_capacity_clamp.py \
    tests/test_skill_builder_provider_execution.py \
    tests/test_private_agent_mcp_discovery.py \
    tests/test_worker_execution_approval_composition.py \
    tests/knowledge/test_agent_tool.py \
    -q -m "not postgres and not provider_integration"
  ```

  Record exact count, duration, deselections, skips, and warning categories. Require zero failures. The audited pre-Batch-5 count for this set without the new contract module and the sandbox contract module is 438.

- [ ] **Step 4: run the selected PostgreSQL authority gate**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run --env-file ../.env python \
    tests/support/core_gate_plugin.py \
    tests/test_worker_lease_heartbeat_postgres.py::test_before_sandbox_exec_has_no_side_effect_when_database_lease_expired \
    tests/test_execution_approval_output_delivery_e2e_postgres.py::test_deferred_output_reaches_artifact_and_success_after_one_frozen_execution \
    tests/test_checkpoint_lease_atomicity_postgres.py::test_checkpoint_write_commits_under_the_current_exact_lease \
    -q -m "not provider_integration"
  ```

  Require `failed=0 skipped=0` against disposable `deerflow_test_*` databases (baseline `collected=4 passed=4`). These nodes protect the lease, approval-continuation, and checkpoint-write collaborators that the moved code calls; no repository PostgreSQL test drives `run_agent()` or `RunAgentPrivateExecutor` end to end, so the full backend gate in Step 6 is the database evidence for this batch.

- [ ] **Step 5: run fresh import-order and exact-object smoke without any process start**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run python -c '
  from deerflow.runtime.runs import checkpoint_rollback, stream_delivery, runtime_binding, goal_continuation, worker
  import deerflow.runtime as runtime
  from app.reliability.run_execution import outcome_mapping, preparation, executor
  import app.reliability.execution as legacy
  assert runtime.RunContext is runtime_binding.RunContext is worker.RunContext
  assert runtime.run_agent is worker.run_agent
  assert worker.RollbackPoint is checkpoint_rollback.RollbackPoint
  assert worker._prepare_goal_continuation_input is goal_continuation._prepare_goal_continuation_input
  assert worker._rollback_to_pre_run_checkpoint is checkpoint_rollback._rollback_to_pre_run_checkpoint
  assert worker._settle_rollback is checkpoint_rollback._settle_rollback
  assert worker._publish_stream_item is stream_delivery._publish_stream_item
  assert worker._build_runtime_context is runtime_binding._build_runtime_context
  assert not hasattr(worker, "__all__")
  assert legacy.RunAgentPrivateExecutor is executor.RunAgentPrivateExecutor
  assert executor.RunAgentPrivateExecutor._graph_input is preparation.graph_input
  assert executor.RunAgentPrivateExecutor._output_limit_error is outcome_mapping.output_limit_error
  assert executor.__all__ == ["RunAgentPrivateExecutor"]
  '
  PYTHONPATH=. uv run python -c '
  import app.reliability.execution as legacy
  import deerflow.runtime as runtime
  ctx_type = runtime.RunContext
  from app.reliability.run_execution import executor, preparation
  from deerflow.runtime.runs import worker, runtime_binding
  assert ctx_type is worker.RunContext is runtime_binding.RunContext
  assert legacy.RunAgentPrivateExecutor is executor.RunAgentPrivateExecutor
  assert executor.RunAgentPrivateExecutor._runner_config is preparation.runner_config
  '
  ```

  Both commands must exit 0 without starting a Gateway, Worker, Scheduler, Sandbox, or database connection.

- [ ] **Step 6: run repository-required format, lint, blocking-I/O, and full backend gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  make format
  make lint
  make detect-blocking-io
  uv run --env-file ../.env make test
  ```

  Inspect `git status --short` immediately after `make format`. If formatting touches a file outside the Batch 5 set, the preserved Batch 4 set, or the already-preserved P1 test, stop and separate the drift instead of including it. Require every command to exit 0 and the core suite to report `failed=0 skipped=0`. Do not use a production database or an unapproved external Provider/Sandbox.

- [ ] **Step 7: perform final structural and scope review**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations
  git diff --check
  git status --short
  git diff --stat eb3dd904df46dd08534fd9aff1a23cd93c72a33e
  ! rg -n '^[[:space:]]*(from|import)[[:space:]]+(deerflow[.]runtime[.]runs[.]worker|[.]worker)\b' \
    backend/packages/harness/deerflow/runtime/runs \
    --glob '*.py' --glob '!**/runs/worker.py' --glob '!**/runs/__init__.py'
  ! rg -n '^[[:space:]]*(from|import)[[:space:]]+(app|sqlalchemy)([[:space:].]|$)' \
    backend/packages/harness/deerflow/runtime/runs --glob '*.py'
  ! rg -n '^[[:space:]]*(from|import)[[:space:]]+app[.]reliability[.]run_execution[.]executor\b' \
    backend/app/reliability/run_execution/preparation.py \
    backend/app/reliability/run_execution/outcome_mapping.py
  rg -n '^(async )?def |^class ' backend/packages/harness/deerflow/runtime/runs/worker.py
  rg -n '^(async )?def |^class ' backend/app/reliability/run_execution/executor.py
  wc -l backend/packages/harness/deerflow/runtime/runs/worker.py \
    backend/packages/harness/deerflow/runtime/runs/*.py \
    backend/app/reliability/run_execution/executor.py \
    backend/app/reliability/run_execution/preparation.py \
    backend/app/reliability/run_execution/outcome_mapping.py
  ```

  The three import scans must return no matches. `worker.py` must define exactly one top-level function (`run_agent`) and no class; `executor.py` must define exactly one class (`RunAgentPrivateExecutor`) and no module-level function. Review every changed/untracked path individually and attribute each to Batch 5, Batch 4, the P1 test, or a pre-existing user document. Confirm no Schema/DDL, frontend, `deerflow.runtime.goal`, `checkpoint_state`, `host_execution_runner`, boundary/handler/settlement, or unrelated test refactor changed. Line counts are reported as evidence, not as an acceptance gate.

- [ ] **Step 8: request one final independent review**

  Use `superpowers:requesting-code-review` over the exact Batch 5 path set. If local commits were authorized, review the Batch 5 commit range; otherwise review all tracked and untracked Batch 5 files listed by `git status --short` while excluding Batch 4 paths, the preserved P1 test, and pre-existing documents from the implementation verdict.

  Require the reviewer to inspect:

  - four Harness owner responsibilities, the `checkpoint_rollback -> {stream_delivery, goal_continuation}` direction, and absence of any owner import of `worker`, `app.*`, or SQLAlchemy;
  - `worker.py` still defining `run_agent()` with the frozen signature, the frozen `except` ladder, unchanged terminal priority, unchanged cleanup order, and by-name calls of the four frozen seams;
  - exact identity of every legacy private name on `worker` versus its owner, both import orders, and `deerflow.runtime` lazy exports;
  - the four extracted phases: `resolve_stream_modes` list/frozenset semantics, `bind_run_runtime_context` config mutation order and lazy `Runtime` import, `capture_legacy_pre_run_baseline` mode/selector/raw-capture error mapping, `capture_pre_run_rollback_point` flag semantics and the audited `snapshot_capture_failed` equivalence;
  - migrated monkeypatch targets (`stream_delivery.time`, `runtime_binding.inspect.signature`, `preparation.PrivateRunContextEvidenceObserver`, `preparation.PrivateRunFileAuthority`) and unchanged `worker`/`executor` seams;
  - `preparation.py` staged owners: `freeze_run_policy` raise codes and order, `materialize_private_runtime` returning immediately after acquisition, `build_run_authorities` port keywords, `bind_run_checkpointer` observer binding, `build_run_context` field mapping, frozen dataclasses, and no untyped dict returns;
  - `executor.py`: boundary first, `push`/`pop` pairing, record/abort/cancel binding, runner scope, lease and type checks, `record_settled()` before pure mapping, the frozen handler order, and the `finally` release order;
  - `outcome_mapping.py` purity and priority order;
  - Batch 3 contract inventory update and composition test retargeting;
  - current validation evidence and scope exclusions.

  Resolve every Critical or Important finding, assess every Minor finding, and rerun the smallest affected focused group plus final structural gates before handoff.

- [ ] **Step 9: documentation checkpoint**

  If explicit local commits are authorized, commit only `backend/AGENTS.md` and the final contract-test additions with message `docs(worker): document execution owners`. Otherwise leave them unstaged and report their exact diff.

## Completion Criteria

- `deerflow.runtime.runs.worker` still defines `run_agent()` with the frozen parameter tuple, no other top-level function or class, no `__all__`, and explicit imports (plain plus `# noqa: F401 - compatibility exports`) that make every name in `WORKER_COMPATIBILITY_NAMES` the exact owner object.
- `run_agent()` keeps its single `try/except/finally`, the frozen handler order, the business terminal priority, one-time resource-ownership transfer, and the documented cleanup order; it calls `_prepare_goal_continuation_input`, `_rollback_to_pre_run_checkpoint`, `_settle_rollback`, and `get_sandbox_provider` by name from `worker.py` globals.
- `checkpoint_rollback.py`, `stream_delivery.py`, `runtime_binding.py`, and `goal_continuation.py` own exactly the responsibilities and dependency direction specified above; none imports `worker`, `app.*`, or SQLAlchemy; `deerflow.runtime`/`deerflow.runtime.runs` lazy exports resolve the same `RunContext` and `run_agent` objects.
- The four extracted phases return frozen dataclasses (`ResolvedStreamModes`, `BoundRunRuntime`, `PreRunCheckpointBaseline`, `PreRunRollbackCapture`) with the audited equivalences; stream modes remain a `list[str]`, `Runtime` stays lazily imported, and every log/error text is unchanged.
- `executor.py` keeps `RunAgentPrivateExecutor` as its only class, the frozen `__init__` parameters and `self._*` attributes, boundary construction, record registration, runner invocation inside the identity scope, lease/type checks, `record_settled()`, the frozen handler order, and the `finally` release order; legacy helper names are exact `staticmethod` aliases of owner functions; `_context_compaction_threshold_tokens` remains importable.
- `preparation.py` owns frozen policy/models, archive context, materialization, authorities, checkpointer binding, `RunContext`, and runner inputs behind `RunPreparationDependencies`, `FrozenRunPolicy`, `MaterializedRunAuthorities`, and `BoundRunCheckpointer`; `outcome_mapping.py` is pure and preserves the priority order; neither imports `executor`.
- Tests move only with corresponding owners; the Batch 3 consumer inventory names `preparation.py` as the `WorkerHostExecutionApprovalPort` constructor; no existing large test file or test framework is reorganized.
- Focused, selected PostgreSQL, import-order, dependency, Ruff, blocking-I/O, and full backend zero-skip gates have current passing evidence, and the report states that focused/offline tests do not certify a live Provider, external Sandbox, or target deployment.
- Batch 4 paths, the P1 test change, and earlier untracked documents remain separately attributable and are not staged or committed as Batch 5 work without explicit authorization.

## Execution Handoff

Plan approval does not start implementation. After approval, choose one:

1. **Subagent-Driven (recommended):** execute Tasks 1-10 sequentially in this existing worktree, with a fresh implementation agent and independent specification/code-quality review per task.
2. **Inline Execution:** execute the same tasks with `superpowers:executing-plans` and the same checkpoints.

Do not create another branch or worktree unless the user explicitly changes the existing-worktree requirement.
