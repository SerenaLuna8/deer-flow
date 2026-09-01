from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from fastapi.routing import APIRoute

from app.gateway.routers.private_work import router as private_work_router
from app.gateway.routers.project_assets import catalog_router, project_router
from app.shared_assets import agent_design_service, skill_design_service
from deerflow.agents.middlewares import provider_request_usage
from deerflow.sandbox import tools as sandbox_tools

BACKEND_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_ROUTE_DIGESTS = {
    "private_work": (45, "a81e85093f732414a5ce8edc38040dc6783e85e4d8316c5eb08c2362850ae2e2"),
    "project_assets": (58, "66a88150e12038d66e561a1577456ddb3630e1f3263b31ab77a43f5aaf4d28b6"),
    "asset_catalog": (5, "2bf16801b2f52d284dee015b4b2ade6d15df02ad695816766c94e4cc3fb12a8d"),
}

EXPECTED_EXPORT_DIGESTS = {
    "skill_design": (22, "b9e7397e798f62e2ba3c2c2e58f48939c661c7e937a2c3d687ad321035affed6"),
    "agent_design": (28, "cb915819337a8b5c5ed19d6483393f1e44d68c9bc1b4aa30915138b3fad2f55c"),
    "provider_request": (33, "d0ed55a5319db01842783940394121132548c91b2e537ff0901b41b23d12a030"),
}


def _callable_name(value: object) -> str:
    module = getattr(value, "__module__", "")
    name = getattr(value, "__qualname__", getattr(value, "__name__", type(value).__name__))
    return f"{module}:{name}"


def _response_name(value: object) -> str | None:
    if value is None:
        return None
    return _callable_name(value)


def _route_digest(router) -> tuple[int, str]:
    rows: list[dict[str, object]] = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        rows.append(
            {
                "methods": sorted(route.methods or ()),
                "path": route.path,
                "name": route.name,
                "status_code": route.status_code,
                "response_model": _response_name(route.response_model),
                "route_class": _callable_name(type(route)),
                "dependencies": sorted(_callable_name(getattr(dependency, "call", None)) for dependency in route.dependant.dependencies),
            }
        )
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return len(rows), hashlib.sha256(encoded).hexdigest()


def _export_digest(module) -> tuple[int, str]:
    names = tuple(module.__all__)
    encoded = json.dumps(names, separators=(",", ":")).encode()
    return len(names), hashlib.sha256(encoded).hexdigest()


def _absolute_imports(root: Path) -> set[str]:
    imports: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module)
    return imports


def test_router_manifests_match_the_pre_split_baseline() -> None:
    assert _route_digest(private_work_router) == EXPECTED_ROUTE_DIGESTS["private_work"]
    assert _route_digest(project_router) == EXPECTED_ROUTE_DIGESTS["project_assets"]
    assert _route_digest(catalog_router) == EXPECTED_ROUTE_DIGESTS["asset_catalog"]


def test_public_export_inventories_match_the_pre_split_baseline() -> None:
    assert _export_digest(skill_design_service) == EXPECTED_EXPORT_DIGESTS["skill_design"]
    assert _export_digest(agent_design_service) == EXPECTED_EXPORT_DIGESTS["agent_design"]
    assert _export_digest(provider_request_usage) == EXPECTED_EXPORT_DIGESTS["provider_request"]


def test_sandbox_tools_keep_their_public_shapes() -> None:
    expected = {
        "bash_tool": "bash",
        "ls_tool": "ls",
        "glob_tool": "glob",
        "grep_tool": "grep",
        "read_file_tool": "read_file",
        "write_file_tool": "write_file",
        "str_replace_tool": "str_replace",
    }
    for attribute, tool_name in expected.items():
        tool = getattr(sandbox_tools, attribute)
        assert tool.name == tool_name
        assert callable(tool.func)
        assert callable(tool.coroutine)


def test_package_dependency_direction_is_one_way() -> None:
    harness_imports = _absolute_imports(BACKEND_ROOT / "packages" / "harness" / "deerflow")
    assert not {name for name in harness_imports if name == "app" or name.startswith("app.")}

    knowledge_imports = _absolute_imports(BACKEND_ROOT / "packages" / "knowledge" / "actweave_knowledge")
    forbidden = {name for name in knowledge_imports if name == "app" or name.startswith("app.") or name == "deerflow" or name.startswith("deerflow.")}
    assert not forbidden


def test_skill_package_integrity_is_the_owning_module() -> None:
    from app.shared_assets import skill_package_integrity as owning
    from app.shared_assets import skill_service as legacy

    exact_names = (
        "SkillFileView",
        "SkillFileContentView",
        "SkillFileChange",
        "SkillSecretRequirementView",
        "SkillArchivePreview",
        "SkillDraftSnapshot",
        "normalize_skill_files",
    )
    for name in exact_names:
        assert getattr(legacy, name) is getattr(owning, name)
    assert legacy._analyze_skill_files is owning.analyze_skill_files
    assert legacy.SkillService._verified_archive_files is owning.verified_archive_files


def test_skill_design_contracts_are_exact_reexports() -> None:
    from app.shared_assets import skill_design_contracts as owning
    from app.shared_assets import skill_design_service as legacy

    names = (
        "SkillDesignStatus",
        "SkillDesignProgressStatus",
        "SkillDesignServiceErrorCode",
        "CreateSkillDesignSession",
        "CreateSkillDesignRevisionSession",
        "SkillDesignMessage",
        "SkillDesignProgressItem",
        "SkillDesignClarificationOption",
        "SkillDesignClarificationRequest",
        "SkillDesignClarificationResponse",
        "SkillDesignTurnAttachment",
        "SkillDesignMessageTurn",
        "SkillDesignClarificationTurn",
        "SkillDesignDraftUpdateTurn",
        "SkillDesignTurn",
        "SubmitSkillDesignTurn",
        "ValidateSkillDesignSession",
        "CommitSkillDesignSession",
        "CancelSkillDesignSession",
        "SetSkillDesignExecutionPreference",
        "SkillDesignExecutionPreference",
        "SkillDesignFileView",
        "SkillDesignBaseFile",
        "SkillDesignSecretRequirement",
        "SkillDesignValidation",
        "SkillDesignSessionView",
        "SkillDesignSessionSummary",
        "SkillDesignCommitResult",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owning, name)


def test_agent_design_contracts_are_exact_reexports() -> None:
    from app.shared_assets import agent_design_contracts as owning
    from app.shared_assets import agent_design_service as legacy

    names = (
        "AgentDesignStatus",
        "AgentDesignProgressStatus",
        "AgentDesignServiceErrorCode",
        "CreateAgentDesignSession",
        "AgentDesignBlueprint",
        "AgentDesignMessage",
        "AgentDesignProgressItem",
        "AgentDesignClarificationOption",
        "AgentDesignClarificationRequest",
        "AgentDesignClarificationResponse",
        "AgentDesignMessageTurn",
        "AgentDesignClarificationTurn",
        "AgentDesignBlueprintTurn",
        "AgentDesignTurn",
        "SubmitAgentDesignTurn",
        "SetAgentDesignGenerationPreference",
        "CommitAgentDesignSession",
        "CancelAgentDesignSession",
        "AgentDesignSessionView",
        "AgentDesignSessionSummary",
        "AgentDesignSessionPage",
        "AgentDesignCommitResult",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owning, name)


def test_agent_design_codec_is_the_owning_module() -> None:
    from app.shared_assets import agent_design_codec as owning
    from app.shared_assets.agent_design_service import AgentDesignService

    static_names = (
        "blueprint_checksum",
        "_agent_payload",
        "_encode_session_cursor",
        "_decode_session_cursor",
        "_request_checksum",
        "_jsonable",
        "_message_json",
        "_progress_json",
        "_blueprint_json",
        "_candidate_metadata_from_json",
        "_remaining_conflicts_after_blueprint_update",
        "_blueprint_from_json",
        "_clarification_request",
        "_clarification_json",
        "_clarification_from_json",
        "_clarification_answers",
        "_clarification_history",
        "_session_view",
        "_session_summary",
        "_stable_generation_error_message",
    )
    class_names = (
        "_has_blocking_conflicts",
        "_clarification_set_json",
        "_clarifications_from_json",
    )
    for name in (*static_names, *class_names):
        assert AgentDesignService.__dict__[name].__func__ is getattr(owning, name)


def test_agent_design_validation_is_the_owning_module() -> None:
    from app.shared_assets import agent_design_service as legacy
    from app.shared_assets import agent_design_validation as owning

    class_names = (
        "_validate_create",
        "_validate_turn",
        "_validate_generation_preference",
        "_validate_commit",
        "_validate_cancel",
        "_candidate_blueprint",
        "_require_capability",
    )
    static_names = (
        "_validate_blueprint",
        "_require_context",
        "_require_nonterminal",
        "_require_expected_revision",
        "_require_matching_operation",
        "_valid_revision",
        "_validate_uuid",
        "_validate_idempotency_key",
        "_bounded_text",
    )
    for name in (*class_names, *static_names):
        assert legacy.AgentDesignService.__dict__[name].__func__ is getattr(owning, name)
    assert legacy.AGENT_DESIGN_SLUG_MIN_LENGTH is owning.AGENT_DESIGN_SLUG_MIN_LENGTH
    assert legacy.AGENT_DESIGN_SLUG_MAX_LENGTH is owning.AGENT_DESIGN_SLUG_MAX_LENGTH
    assert legacy.AGENT_DESIGN_SLUG_PATTERN is owning.AGENT_DESIGN_SLUG_PATTERN


def test_skill_design_codec_is_the_owning_module() -> None:
    from app.shared_assets import skill_design_codec as owning
    from app.shared_assets.skill_design_service import SkillDesignService

    names = (
        "_conversation_brief",
        "_validation_from_preview",
        "_validation_matches_preview",
        "_validation_json",
        "_validation_from_json",
        "_message_json",
        "_progress_json",
        "_clarification_request",
        "_clarification_json",
        "_clarification_from_json",
        "_session_summary",
        "_idempotency_hash",
        "_request_checksum",
        "_jsonable",
        "_stable_generation_error_message",
    )
    for name in names:
        assert SkillDesignService.__dict__[name].__func__ is getattr(owning, name)


def test_skill_design_validation_is_the_owning_module() -> None:
    from app.shared_assets import skill_design_validation as owning
    from app.shared_assets.skill_design_service import SkillDesignService

    names = (
        "_validate_create",
        "_validate_create_revision",
        "_validate_turn",
        "_validate_execution_preference",
        "_validate_turn_model_name",
        "_validate_turn_reasoning_effort",
        "_validate_turn_attachments",
        "_validate_validation",
        "_validate_commit",
        "_validate_cancel",
        "_require_context",
        "_require_capability",
        "_require_nonterminal",
        "_require_revise_target_live",
        "_require_expected_revision",
        "_require_matching_operation",
        "_require_message_capacity",
        "_require_matching_clarification_response",
        "_candidate_files",
        "_validate_builder_files",
        "_validate_partial_builder_files",
        "_require_preview_name",
        "_valid_revision",
        "_validate_uuid",
        "_validate_idempotency_key",
        "_bounded_text",
    )
    for name in names:
        assert SkillDesignService.__dict__[name].__func__ is getattr(owning, name)


def test_production_consumers_use_skill_package_integrity_owner() -> None:
    paths = (
        BACKEND_ROOT / "app/shared_assets/bootstrap/service.py",
        BACKEND_ROOT / "app/shared_assets/catalog_provider.py",
        BACKEND_ROOT / "app/shared_assets/resolver.py",
        BACKEND_ROOT / "app/shared_assets/skill_design_service.py",
        BACKEND_ROOT / "app/private_work/legacy_run_skill_snapshot_writer.py",
        BACKEND_ROOT / "scripts/generate_public_system_skill_catalog.py",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "SkillService._verified_archive_files" not in source
        assert "_analyze_skill_files" not in source
