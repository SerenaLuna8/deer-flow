"""Batch 5 Worker Execution compatibility contracts.

Characterization tests that pass on the untouched Worker/executor baseline and
keep passing while ``runtime/runs/worker.py`` and
``reliability/run_execution/executor.py`` delegate to owning modules.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
from pathlib import Path

import pytest

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
EXECUTOR_CLASS_COMPATIBILITY_NAMES = frozenset(
    {
        "execute",
        "_execute_with_trace",
        "_default_agent_factory",
        "_memory_archive_context",
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
        "SkillBuilderAgentFactory",
        "WorkerSkillBuilderAuthoringCatalog",
        "SkillDesignDraftSink",
    }
)

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
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == module_name:
                    observed.update(alias.name for alias in node.names)
                else:
                    # ``from <package> import <module>`` binds the module itself under a local name.
                    aliases.update(alias.asname or alias.name for alias in node.names if f"{node.module}.{alias.name}" == module_name)
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


def _attribute_call_names(function: ast.AST) -> frozenset[str]:
    """Attribute names invoked as ``<expr>.<attr>(...)`` anywhere inside ``function``."""

    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
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


def _module_imports(path: Path) -> set[str]:
    """Absolute imports plus relative imports rendered as ``.module`` / ``.name``.

    Every ``from X import Y`` also records ``X.Y`` (with the relative prefix kept)
    so ``from <package> import <module>`` is visible as an import of ``<module>``.
    """

    imports: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            if node.module:
                imports.add(f"{prefix}{node.module}")
                imports.update(f"{prefix}{node.module}.{alias.name}" for alias in node.names)
            elif node.level > 0:
                imports.add(prefix)
                imports.update(f"{prefix}{alias.name}" for alias in node.names)
    return imports


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


def test_batch5_run_agent_calls_frozen_module_seams_by_name() -> None:
    called = _called_names(_function_node(WORKER_PATH, "run_agent"))
    assert RUN_AGENT_MODULE_SEAMS <= called, RUN_AGENT_MODULE_SEAMS - called


def test_batch5_terminal_exception_ladders_are_frozen() -> None:
    assert _outer_except_ladder(WORKER_PATH, "run_agent") == EXPECTED_RUN_AGENT_EXCEPT_LADDER
    assert _outer_except_ladder(EXECUTOR_PATH, "_execute_with_trace") == EXPECTED_EXECUTOR_EXCEPT_LADDER


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


def test_module_imports_helper_sees_every_import_form(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe_lines = (
        "from deerflow.runtime.runs import worker",
        "from ..runs import worker as w2",
        "from ..runs.worker import x",
        "from . import worker as w3",
        "from .worker import run_agent",
    )
    probe.write_text("\n".join(probe_lines) + "\n", encoding="utf-8")
    imports = _module_imports(probe)
    assert {"deerflow.runtime.runs.worker", "..runs.worker", "..runs.worker.x", ".worker", ".worker.run_agent"} <= imports, imports


def test_batch5_owner_modules_never_import_facades_or_forbidden_packages() -> None:
    for name in WORKER_OWNER_MODULES:
        path = RUNS_ROOT / f"{name}.py"
        if not path.exists():
            continue
        imports = _module_imports(path)
        facade_imports = {module for module in imports if module == WORKER_MODULE or module.endswith(".worker")}
        assert facade_imports == set(), (name, facade_imports)
        forbidden = {module for module in imports if module == "app" or module.startswith("app.") or module == "sqlalchemy" or module.startswith("sqlalchemy.")}
        assert forbidden == set(), (name, forbidden)
    for name in EXECUTOR_OWNER_MODULES:
        path = RUN_EXECUTION_ROOT / f"{name}.py"
        if not path.exists():
            continue
        imports = _module_imports(path)
        facade_imports = {module for module in imports if module == EXECUTOR_MODULE or module.endswith(".executor")}
        assert facade_imports == set(), (name, facade_imports)


def test_checkpoint_rollback_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module("deerflow.runtime.runs.checkpoint_rollback")
    for name in CHECKPOINT_ROLLBACK_NAMES:
        assert getattr(worker_legacy, name) is getattr(owner, name), name
    assert owner.__all__ == ["PreRunCheckpointBaseline", "PreRunRollbackCapture", "RollbackPoint", "capture_legacy_pre_run_baseline", "capture_pre_run_rollback_point"]


def test_stream_delivery_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module("deerflow.runtime.runs.stream_delivery")
    for name in STREAM_DELIVERY_NAMES:
        assert getattr(worker_legacy, name) is getattr(owner, name), name
    assert owner._TEXT_DELTA_FLUSH_DUE is worker_legacy._TEXT_DELTA_FLUSH_DUE
    assert "time" in vars(owner)


def test_runtime_binding_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module("deerflow.runtime.runs.runtime_binding")
    for name in RUNTIME_BINDING_NAMES:
        assert getattr(worker_legacy, name) is getattr(owner, name), name
    assert owner.__all__ == ["BoundRunRuntime", "PrivateAgentRuntime", "PrivateRuntimeFactoryUnavailable", "RunContext", "bind_run_runtime_context"]
    assert runtime_package.RunContext is owner.RunContext
    assert "inspect" in vars(owner)


def test_goal_continuation_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module("deerflow.runtime.runs.goal_continuation")
    for name in GOAL_CONTINUATION_NAMES:
        assert getattr(worker_legacy, name) is getattr(owner, name), name
    assert not hasattr(owner, "__all__")
    owner_imports = _module_imports(RUNS_ROOT / "goal_continuation.py")
    assert ".checkpoint_rollback" in owner_imports
    assert not owner_imports & {WORKER_MODULE, ".worker", ".stream_delivery", ".runtime_binding"}


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
    assert {
        "PrivateRunExecutionBoundary",
        "RunManager",
        "freeze_run_policy",
        "materialize_private_runtime",
        "build_run_authorities",
        "bind_run_checkpointer",
        "build_run_context",
        "map_run_agent_outcome",
        "push_current_app_config",
        "pop_current_app_config",
    } <= executor_calls
    assert "_memory_archive_context" in _attribute_call_names(_function_node(EXECUTOR_PATH, "_execute_with_trace"))
    assert "load_memory_archive_context" in _called_names(_function_node(EXECUTOR_PATH, "_memory_archive_context"))
    assert not executor_calls & {"WorkerHostExecutionApprovalPort", "PrivateRunFileAuthority", "PrivateRunContextEvidenceObserver", "PrivateRunMemoryAuthority", "RunContext", "LeaseAuthorizedRunEventStore"}
    preparation_path = RUN_EXECUTION_ROOT / "preparation.py"
    authorities_calls = _called_names(_function_node(preparation_path, "build_run_authorities"))
    assert {"WorkerHostExecutionApprovalPort", "PrivateRunFileAuthority", "PrivateFileFinalizer", "resolve_model_ref"} <= authorities_calls
    checkpointer_calls = _called_names(_function_node(preparation_path, "bind_run_checkpointer"))
    assert "PrivateRunContextEvidenceObserver" in checkpointer_calls
    context_calls = _called_names(_function_node(preparation_path, "build_run_context"))
    assert {"RunContext", "PrivateRunMemoryAuthority", "LeaseAuthorizedRunEventStore", "_PrivateRunThreadMetadataStore"} <= context_calls
    assert {"LeaseAuthorizedStreamBridge", "SkillBuilderActivityStreamBridge"} <= executor_calls


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
