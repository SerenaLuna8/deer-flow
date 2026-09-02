"""Batch 3 compatibility and process-boundary contracts.

These tests freeze the surfaces that the Skill Builder, Execution Approval,
and Agent Builder decomposition must preserve: ordered public exports, public
signatures, the repository's direct legacy imports, and the Gateway/Worker/
Harness import boundaries. They are characterization tests: they pass on the
untouched baseline and keep passing while owning modules are extracted.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
from pathlib import Path

from app.private_work import execution_approval as execution_approval_legacy
from app.shared_assets import agent_design_service as agent_design_legacy
from app.shared_assets import skill_design_service as skill_design_legacy

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"
TESTS_ROOT = BACKEND_ROOT / "tests"
HARNESS_ROOT = BACKEND_ROOT / "packages" / "harness"
WORKER_ROOT = APP_ROOT / "worker"
RUN_EXECUTION_ROOT = APP_ROOT / "reliability" / "run_execution"

SKILL_DESIGN_LEGACY_MODULE = "app.shared_assets.skill_design_service"
EXECUTION_APPROVAL_LEGACY_MODULE = "app.private_work.execution_approval"
AGENT_DESIGN_LEGACY_MODULE = "app.shared_assets.agent_design_service"
AGENT_DESIGN_GENERATION_LIFECYCLE_MODULE = "app.shared_assets.agent_design_generation_lifecycle"

SKILL_DESIGN_LEGACY_PATH = APP_ROOT / "shared_assets" / "skill_design_service.py"
EXECUTION_APPROVAL_LEGACY_PATH = APP_ROOT / "private_work" / "execution_approval.py"
AGENT_DESIGN_LEGACY_PATH = APP_ROOT / "shared_assets" / "agent_design_service.py"

EXECUTION_APPROVAL_LIFECYCLE_PATH = APP_ROOT / "private_work" / "execution_approval_lifecycle.py"
EXECUTION_APPROVAL_AUDIT_PATH = APP_ROOT / "private_work" / "execution_approval_audit.py"

EXECUTOR_PATH = RUN_EXECUTION_ROOT / "executor.py"
HANDLER_PATH = RUN_EXECUTION_ROOT / "handler.py"
GATEWAY_DEPS_PATH = APP_ROOT / "gateway" / "deps.py"
PRIVATE_WORK_CONTRACTS_PATH = APP_ROOT / "gateway" / "routers" / "private_work_routes" / "contracts.py"
PRIVATE_WORK_DEPENDENCIES_PATH = APP_ROOT / "gateway" / "routers" / "private_work_routes" / "dependencies.py"
PROJECT_AGENT_BUILDER_PATH = APP_ROOT / "gateway" / "routers" / "project_agent_builder.py"

# The Worker executor constructs exactly one Skill Builder draft sink owner.
EXECUTOR_SKILL_BUILDER_CONSTRUCTOR = "SkillDesignService"

EXPECTED_EXPORT_DIGESTS = {
    SKILL_DESIGN_LEGACY_MODULE: (22, "b9e7397e798f62e2ba3c2c2e58f48939c661c7e937a2c3d687ad321035affed6"),
    EXECUTION_APPROVAL_LEGACY_MODULE: (6, "5b0c7bc581882c2606df87a03d0da7023254266b8c73e19acc081009827d5702"),
    AGENT_DESIGN_LEGACY_MODULE: (28, "cb915819337a8b5c5ed19d6483393f1e44d68c9bc1b4aa30915138b3fad2f55c"),
}

SKILL_DESIGN_LEGACY_NAMES = frozenset(
    {
        "CancelSkillDesignSession",
        "CommitSkillDesignSession",
        "CreateSkillDesignRevisionSession",
        "CreateSkillDesignSession",
        "SetSkillDesignExecutionPreference",
        "SkillDesignActivity",
        "SkillDesignClarificationResponse",
        "SkillDesignClarificationTurn",
        "SkillDesignCommitResult",
        "SkillDesignDraftUpdateTurn",
        "SkillDesignGenerationRequest",
        "SkillDesignMessageTurn",
        "SkillDesignService",
        "SkillDesignSessionSummary",
        "SkillDesignSessionView",
        "SkillDesignStatus",
        "SkillDesignTurnAttachment",
        "SubmitSkillDesignTurn",
        "ValidateSkillDesignSession",
    }
)
EXECUTION_APPROVAL_LEGACY_NAMES = frozenset(
    {
        "ExecutionApprovalProjection",
        "ExecutionApprovalService",
        "HostExecutionProviderPolicySnapshot",
        "WorkerHostExecutionApprovalPort",
        "_asset_closure",
        "recover_staged_execution_approval_id",
        "settle_staged_execution_approvals",
    }
)
AGENT_DESIGN_LEGACY_NAMES = frozenset(
    {
        "AGENT_DESIGN_SLUG_MAX_LENGTH",
        "AGENT_DESIGN_SLUG_MIN_LENGTH",
        "AGENT_DESIGN_SLUG_PATTERN",
        "AgentDesignBlueprint",
        "AgentDesignBlueprintTurn",
        "AgentDesignClarificationResponse",
        "AgentDesignClarificationTurn",
        "AgentDesignCommitResult",
        "AgentDesignMessageTurn",
        "AgentDesignService",
        "AgentDesignSessionPage",
        "AgentDesignSessionSummary",
        "AgentDesignSessionView",
        "AgentDesignStatus",
        "CancelAgentDesignSession",
        "CommitAgentDesignSession",
        "CreateAgentDesignSession",
        "MAX_INCOMPLETE_AGENT_DESIGN_SESSIONS_PER_OWNER_PROJECT",
        "SetAgentDesignGenerationPreference",
        "SubmitAgentDesignTurn",
    }
)

LEGACY_INVENTORIES = (
    (SKILL_DESIGN_LEGACY_MODULE, SKILL_DESIGN_LEGACY_PATH, skill_design_legacy, SKILL_DESIGN_LEGACY_NAMES),
    (EXECUTION_APPROVAL_LEGACY_MODULE, EXECUTION_APPROVAL_LEGACY_PATH, execution_approval_legacy, EXECUTION_APPROVAL_LEGACY_NAMES),
    (AGENT_DESIGN_LEGACY_MODULE, AGENT_DESIGN_LEGACY_PATH, agent_design_legacy, AGENT_DESIGN_LEGACY_NAMES),
)

# Production modules that currently import the Execution Approval façade.
EXECUTION_APPROVAL_PRODUCTION_CONSUMERS = frozenset(
    {
        GATEWAY_DEPS_PATH,
        PRIVATE_WORK_CONTRACTS_PATH,
        PRIVATE_WORK_DEPENDENCIES_PATH,
        EXECUTOR_PATH,
        HANDLER_PATH,
    }
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _absolute_imports(path: Path) -> set[str]:
    tree = _parse(path)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    return imports


def _tree_imports(path: Path) -> set[str]:
    return set().union(*(_absolute_imports(candidate) for candidate in sorted(path.rglob("*.py"))))


def _export_digest(module: object) -> tuple[int, str]:
    names = tuple(getattr(module, "__all__"))
    payload = json.dumps(names, separators=(",", ":")).encode()
    return len(names), hashlib.sha256(payload).hexdigest()


def _top_level_runtime_nodes(path: Path) -> tuple[str, ...]:
    tree = _parse(path)
    return tuple(type(node).__name__ for node in tree.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)))


def _imports_module(tree: ast.Module, module_name: str) -> bool:
    """Detect ``import a.b.c``, ``from a.b.c import x``, and ``from a.b import c``."""
    parent, _, tail = module_name.rpartition(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == module_name for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == module_name:
                return True
            if node.module == parent and any(alias.name == tail for alias in node.names):
                return True
    return False


def _module_consumers(module_name: str, defining_path: Path, *roots: Path) -> set[Path]:
    consumers: set[Path] = set()
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path == defining_path:
                continue
            if _imports_module(_parse(path), module_name):
                consumers.add(path)
    return consumers


def _legacy_name_imports(module_name: str, defining_path: Path, *roots: Path) -> set[str]:
    names: set[str] = set()
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path == defining_path:
                continue
            for node in ast.walk(_parse(path)):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == module_name:
                    names.update(alias.name for alias in node.names)
    return names


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _construction_count(path: Path, class_name: str) -> int:
    return sum(1 for node in ast.walk(_parse(path)) if isinstance(node, ast.Call) and _call_name(node) == class_name)


def _relative(paths: set[Path]) -> set[str]:
    return {str(path.relative_to(BACKEND_ROOT)) for path in paths}


def test_batch3_legacy_exports_are_frozen() -> None:
    for module_name, _path, module, _names in LEGACY_INVENTORIES:
        assert _export_digest(module) == EXPECTED_EXPORT_DIGESTS[module_name], module_name


def test_batch3_service_signatures_are_frozen() -> None:
    from app.private_work.execution_approval import (
        ExecutionApprovalService,
        WorkerHostExecutionApprovalPort,
    )
    from app.shared_assets.agent_design_service import AgentDesignService
    from app.shared_assets.skill_design_service import SkillDesignService

    expected = {
        SkillDesignService: {
            "__init__": (
                "self",
                "session_factory",
                "generator",
                "skill_service",
                "repository_factory",
                "run_admission",
                "quota",
                "audit",
                "stale_generating_seconds",
            ),
            "terminal_sink": ("self", "context", "claim"),
        },
        AgentDesignService: {
            "__init__": (
                "self",
                "session_factory",
                "generator",
                "agent_service",
                "repository_factory",
                "default_tool_groups_provider",
                "stale_generating_seconds",
                "generation_control",
            ),
            "submit_turn": ("self", "context", "session_id", "command"),
            "stop_turn": ("self", "context", "session_id"),
        },
    }
    for owner, methods in expected.items():
        for name, parameters in methods.items():
            assert tuple(inspect.signature(getattr(owner, name)).parameters) == parameters, (owner.__name__, name)

    assert "approval_id" in inspect.signature(WorkerHostExecutionApprovalPort.complete_host_execution).parameters
    assert tuple(inspect.signature(ExecutionApprovalService.decide).parameters) == (
        "self",
        "context",
        "thread_id",
        "source_run_id",
        "approval_id",
        "decision",
        "expected_version",
        "idempotency_key",
    )


def test_batch3_legacy_import_inventories_match_the_audited_baseline() -> None:
    for module_name, defining_path, module, expected_names in LEGACY_INVENTORIES:
        for name in sorted(expected_names):
            assert hasattr(module, name), (module_name, name)
        observed = _legacy_name_imports(module_name, defining_path, APP_ROOT, TESTS_ROOT)
        assert observed == expected_names, module_name


def test_worker_executor_constructs_one_skill_builder_sink_without_agent_design() -> None:
    assert _construction_count(EXECUTOR_PATH, EXECUTOR_SKILL_BUILDER_CONSTRUCTOR) == 1
    for root in (WORKER_ROOT, RUN_EXECUTION_ROOT, HARNESS_ROOT):
        for module_name in (AGENT_DESIGN_LEGACY_MODULE, AGENT_DESIGN_GENERATION_LIFECYCLE_MODULE):
            assert _module_consumers(module_name, AGENT_DESIGN_LEGACY_PATH, root) == set(), (root, module_name)


def test_gateway_owns_agent_builder_construction() -> None:
    consumers = _module_consumers(AGENT_DESIGN_LEGACY_MODULE, AGENT_DESIGN_LEGACY_PATH, APP_ROOT)
    assert consumers == {PROJECT_AGENT_BUILDER_PATH}, _relative(consumers)
    assert _construction_count(PROJECT_AGENT_BUILDER_PATH, "AgentDesignService") == 1
    assert _construction_count(EXECUTOR_PATH, "AgentDesignService") == 0


def test_execution_approval_production_consumers_are_exact() -> None:
    consumers = _module_consumers(EXECUTION_APPROVAL_LEGACY_MODULE, EXECUTION_APPROVAL_LEGACY_PATH, APP_ROOT)
    assert consumers == EXECUTION_APPROVAL_PRODUCTION_CONSUMERS, _relative(consumers ^ EXECUTION_APPROVAL_PRODUCTION_CONSUMERS)


def test_harness_never_imports_the_application() -> None:
    offenders = sorted(module for module in _tree_imports(HARNESS_ROOT) if module == "app" or module.startswith("app."))
    assert offenders == []


def test_execution_approval_lifecycle_and_audit_remain_separate_owners() -> None:
    assert EXECUTION_APPROVAL_LIFECYCLE_PATH.is_file()
    assert EXECUTION_APPROVAL_AUDIT_PATH.is_file()
    lifecycle = importlib.import_module("app.private_work.execution_approval_lifecycle")
    audit = importlib.import_module("app.private_work.execution_approval_audit")
    assert lifecycle is not audit
    assert Path(lifecycle.__file__) == EXECUTION_APPROVAL_LIFECYCLE_PATH
    assert Path(audit.__file__) == EXECUTION_APPROVAL_AUDIT_PATH
    for path in (EXECUTION_APPROVAL_LIFECYCLE_PATH, EXECUTION_APPROVAL_AUDIT_PATH):
        assert {"FunctionDef", "AsyncFunctionDef", "ClassDef"} & set(_top_level_runtime_nodes(path)), path
