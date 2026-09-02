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
