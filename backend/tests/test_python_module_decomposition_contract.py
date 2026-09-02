from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from app.gateway.routers.admin_assets import _admin_actor, _admin_project_actor, admin_project_router, admin_router
from app.gateway.routers.private_work import router as private_work_router
from app.gateway.routers.project_assets import (
    AssetRoute,
    catalog_router,
    project_asset_context,
    project_router,
    register_asset_routes,
)
from app.shared_assets import agent_design_service, skill_design_service
from deerflow.agents.middlewares import provider_request_usage
from deerflow.sandbox import tools as sandbox_tools

BACKEND_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_ROUTE_DIGESTS = {
    "private_work": (45, "9bdab5e8c3ae4d160dcba02823991083075004e8462d747ab05505d58b025071"),
    "project_assets": (58, "4540acd74ce953a8754c5aae9c119e245f28fbf1986b145763e0fda238adb6ab"),
    "asset_catalog": (5, "fe91c789905280f2b3d6f575409f4e8be5edff59a60796a40c105648971459ac"),
    "admin_assets": (10, "fecb2764121b4f4968784d662225050fd9b5b213baf8fa7f6b9dd348eb3eb27b"),
    "admin_project_assets": (32, "582a191b683382b1d2a22b84d2fd0a605806fabea2f1ee2862b521b25dad5c69"),
}

EXPECTED_OPENAPI_ROUTE_DIGESTS = {
    "private_work": (
        45,
        "b45ecf771a9bf0008cb11a251f55a5a119dd6e5ecd9772bf121c3f5757ba4860",
        "afd277aa4ce3f9a7e3a5ff7bdb7c9be8e661ed461bd2ef0dcf9e20f470ba72cc",
    ),
    "project_assets": (
        58,
        "4318232d5564dabf7b7ae9c20fafe8dbec53ee5a11ad3577412f7b0d02105e03",
        "43a82d9b5d4990b11fee50b4171e9cc3f437b81a862f801ca103f3be6b17237e",
    ),
    "asset_catalog": (
        5,
        "bcddb97ab067006f1dba0b6653fb12099828c8dc203c17bf8e04347e9afda70a",
        "85811994377d69210d9ad7be76f192396ac5daf5cd1647d5f8b5854d23f4f4ee",
    ),
}

_OPENAPI_HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})

EXPECTED_EXPORT_DIGESTS = {
    "skill_design": (22, "b9e7397e798f62e2ba3c2c2e58f48939c661c7e937a2c3d687ad321035affed6"),
    "agent_design": (28, "cb915819337a8b5c5ed19d6483393f1e44d68c9bc1b4aa30915138b3fad2f55c"),
    "provider_request": (33, "d0ed55a5319db01842783940394121132548c91b2e537ff0901b41b23d12a030"),
}


def _logical_name(value: object) -> str | None:
    if value is None:
        return None
    return getattr(
        value,
        "__qualname__",
        getattr(value, "__name__", type(value).__name__),
    )


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
                "response_model": _logical_name(route.response_model),
                "route_class": _logical_name(type(route)),
                "dependencies": sorted(_logical_name(getattr(dependency, "call", None)) for dependency in route.dependant.dependencies),
            }
        )
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return len(rows), hashlib.sha256(encoded).hexdigest()


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
    path_methods = [{"method": row["method"], "path": row["path"]} for row in operations]
    operation_ids = [row["operationId"] for row in operations]
    assert all(operation_ids)
    assert len(operation_ids) == len(set(operation_ids))
    return (
        len(operations),
        _sha256_json(operations),
        _sha256_json(path_methods),
    )


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
    assert _route_digest(admin_router) == EXPECTED_ROUTE_DIGESTS["admin_assets"]
    assert _route_digest(admin_project_router) == EXPECTED_ROUTE_DIGESTS["admin_project_assets"]


def test_router_openapi_operations_match_the_pre_split_baseline() -> None:
    assert _openapi_route_digests(private_work_router) == EXPECTED_OPENAPI_ROUTE_DIGESTS["private_work"]
    assert _openapi_route_digests(project_router) == EXPECTED_OPENAPI_ROUTE_DIGESTS["project_assets"]
    assert _openapi_route_digests(catalog_router) == EXPECTED_OPENAPI_ROUTE_DIGESTS["asset_catalog"]


def test_private_work_contract_and_dependency_exports_are_exact() -> None:
    from app.gateway.routers import private_work as legacy
    from app.gateway.routers.private_work_routes import contracts, dependencies

    contract_names = (
        "_POSTGRES_BIGINT_MAX",
        "PRIVATE_THREAD_TITLE_MAX_LENGTH",
        "PrivateThreadCreateRequest",
        "PrivateThreadSearchRequest",
        "PrivateThreadPatchRequest",
        "PrivateThreadResponse",
        "PrivateThreadSearchResponse",
        "PrivateThreadDeleteResponse",
        "PrivateThreadStateResponse",
        "PrivateRunExecutionProfileResponse",
        "PrivateRunExecutionStateResponse",
        "PrivateRunResponse",
        "ExecutionApprovalContinuationRunResponse",
        "ExecutionApprovalDomainResponse",
        "ExecutionApprovalSourceAgentResponse",
        "ExecutionApprovalBaseResponse",
        "ExecutionApprovalPendingResponse",
        "ExecutionApprovalApprovedResponse",
        "ExecutionApprovalClaimedResponse",
        "ExecutionApprovalFinishedResponse",
        "ExecutionApprovalLaunchFailedResponse",
        "ExecutionApprovalUnknownResponse",
        "ExecutionApprovalDeniedResponse",
        "ExecutionApprovalClosedResponse",
        "ExecutionApprovalResponse",
        "PrivateThreadBranchRequest",
        "ExecutionApprovalEnvelopeResponse",
        "ExecutionApprovalDecisionRequest",
        "PrivateRunDeleteResponse",
        "PrivateRunMessageResponse",
        "PrivateRunMessagesPageResponse",
        "PrivateFeedbackCreateRequest",
        "PrivateFeedbackResponse",
        "PrivateFileResponse",
        "PrivateFileDeleteResponse",
        "PrivateUploadProjectStorageResponse",
        "PrivateUploadLimitsResponse",
        "PrivateWorkReadinessResponse",
        "PrivateThreadGoalRequest",
        "PrivateThreadGoalResponse",
        "PrivateCompactKeep",
        "PrivateThreadCompactRequest",
        "PrivateThreadCompactResponse",
        "PrivateThreadBranchResponse",
        "PrivateRegeneratePrepareRequest",
        "PrivateRegeneratePrepareResponse",
        "PrivateEditRegeneratePrepareRequest",
        "PrivateEditRegeneratePrepareResponse",
        "PrivateSuggestionsRequest",
        "PrivateSuggestionsResponse",
        "_thread_response",
        "_public_run_metadata",
        "_timestamp",
        "_run_response",
        "_execution_approval_response",
        "_file_response",
        "_feedback_response",
    )
    dependency_names = (
        "_thread_service",
        "_execution_approval_service",
        "_chat_control_service",
        "_file_service",
        "_file_streamer",
        "_run_service",
        "_browser_chat_run_service",
        "_run_event_store",
        "_run_store",
        "_feedback_service",
        "_runtime_dependency",
        "_scoped_checkpointer",
        "_raise_http",
    )
    for name in contract_names:
        assert getattr(legacy, name) is getattr(contracts, name)
    for name in dependency_names:
        assert getattr(legacy, name) is getattr(dependencies, name)


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


def test_provider_request_profile_is_the_owning_module() -> None:
    from deerflow.agents.middlewares import provider_request_profile as owning
    from deerflow.agents.middlewares import provider_request_usage as legacy

    names = (
        "ProviderRequestUsageUnsupported",
        "ProviderRequestProfileDrift",
        "ContextCapacityExceeded",
        "ProviderRequestComponentSnapshot",
        "ProviderToolSchemaFact",
        "ProviderRequestProfileSnapshot",
        "ProviderRequestMeasurementSnapshot",
        "ProviderRequestComponent",
        "ProviderRequestContextMeasurement",
        "ProviderRequestMaterialMeasurement",
        "ProviderRequestProfile",
        "provider_tool_schema_fact",
        "canonicalize_full_tools",
        "collect_middleware_tools",
        "collect_middleware_system_prompts",
        "collect_custom_middleware_request_contract",
        "contains_visual_material",
        "resolve_provider_adapter",
        "declared_visual_max_tokens_per_image",
        "provider_request_closure_identity",
        "provider_request_runtime_policy_identity",
        "provider_request_runtime_policy_compatibility_identity",
        "build_provider_request_profile",
        "build_provider_request_profile_snapshot_from_facts",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owning, name)


def test_provider_request_measurement_is_the_owning_module() -> None:
    from deerflow.agents.middlewares import provider_request_measurement as owning
    from deerflow.agents.middlewares import provider_request_usage as legacy

    assert legacy.measure_profile_snapshot_context is owning.measure_profile_snapshot_context
    assert legacy.measure_profile_context is owning.measure_profile_context


def test_provider_request_guard_is_the_owner_and_usage_is_a_facade() -> None:
    from deerflow.agents.middlewares import provider_request_guard as owning
    from deerflow.agents.middlewares import provider_request_usage as legacy

    names = (
        "ProviderDispatchOutcomeAmbiguous",
        "ProviderRequestEvidenceObserver",
        "FinalProviderRequestGuard",
        "_join_durable_observer_transition",
        "_record_ambiguity_despite_cancellation",
        "_record_proven_no_response_failure",
        "_record_provider_failed_response",
        "_model_response",
        "_provider_input_tokens",
        "_runtime_run_id",
        "_runtime_token_usage_tracking_enabled",
        "_attach_measurement",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owning, name)
    assert _export_digest(legacy) == EXPECTED_EXPORT_DIGESTS["provider_request"]
