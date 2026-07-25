from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from app.gateway.auth.models import User
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.agent_service import AgentService
from app.shared_assets.contexts import SystemAssetGovernanceContext, resolve_asset_actor
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.mcp_service import McpService
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    AssetSelection,
    ResolvedAgentSnapshot,
    ResolvedMcpSnapshot,
    ResolvedSkillSnapshot,
    SkillArchiveFile,
    WorkflowStatus,
)
from app.shared_assets.skill_service import SkillService

PROJECT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
SYSTEM_ADMIN_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")
ASSET_ID = uuid.UUID("30000000-0000-0000-0000-000000000003")
VERSION_ID = uuid.UUID("40000000-0000-0000-0000-000000000004")


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def info(self, _message: str, *, extra: dict[str, object]) -> None:
        self.events.append(extra["governance_event"])  # type: ignore[arg-type]


def test_shared_asset_enums_are_stable() -> None:
    assert {item.value for item in AssetScope} == {"system", "project"}
    assert {item.value for item in AssetKind} == {"agent", "skill", "mcp"}
    assert {item.value for item in WorkflowStatus} == {"draft", "pending_approval", "published", "rejected"}


def test_shared_asset_domain_snapshots_are_frozen_and_typed() -> None:
    dependency_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    payload = AgentPayload(
        description="description",
        soul="soul",
        model_ref="model",
        tool_groups=("search",),
        skill_version_ids=(dependency_id,),
        mcp_version_ids=(),
    )
    selection = AssetSelection(kind=AssetKind.AGENT, asset_id=ASSET_ID)
    archive_file = SkillArchiveFile(path="SKILL.md", content=b"body")
    base = {
        "scope": AssetScope.SYSTEM,
        "asset_id": ASSET_ID,
        "version_id": VERSION_ID,
        "checksum": "a" * 64,
        "catalog_generation": 7,
        "dependency_version_ids": (dependency_id,),
    }

    agent = ResolvedAgentSnapshot(kind=AssetKind.AGENT, payload=payload, **base)
    skill = ResolvedSkillSnapshot(
        kind=AssetKind.SKILL,
        files=(archive_file,),
        secret_requirements=("API_TOKEN",),
        **base,
    )
    mcp = ResolvedMcpSnapshot(
        kind=AssetKind.MCP,
        definition={"transport": "stdio"},
        credential_grant_ids=(grant_id,),
        **base,
    )

    assert selection.version_id is None
    assert archive_file.media_type == "application/octet-stream"
    assert agent.payload is payload
    assert skill.files == (archive_file,)
    assert mcp.credential_grant_ids == (grant_id,)
    with pytest.raises(dataclasses.FrozenInstanceError):
        selection.version_id = VERSION_ID  # type: ignore[misc]


def test_system_override_does_not_construct_project_context() -> None:
    system_admin_user = User(email="admin@example.com", system_role="system_admin", id=SYSTEM_ADMIN_ID)

    actor = resolve_asset_actor(system_admin_user, project_id=PROJECT_ID, request_id="r1")

    assert actor == SystemAssetGovernanceContext(user_id=SYSTEM_ADMIN_ID, request_id="r1", project_id=PROJECT_ID)
    assert not isinstance(actor, ProjectContext)
    with pytest.raises(dataclasses.FrozenInstanceError):
        actor.request_id = "changed"  # type: ignore[misc]


def test_non_system_admin_cannot_construct_governance_context() -> None:
    user = User(email="user@example.com", system_role="user")

    with pytest.raises(AssetForbidden) as exc_info:
        resolve_asset_actor(user, project_id=PROJECT_ID, request_id="r2")

    assert exc_info.value.request_id == "r2"


@pytest.mark.parametrize("service_type", [AgentService, SkillService])
def test_runtime_system_catalog_context_has_read_only_definition_capability(service_type) -> None:
    actor = SystemAssetGovernanceContext(
        user_id=SYSTEM_ADMIN_ID,
        request_id="r-bootstrap-only",
    )

    service_type._require_capability(actor, Capability.SHARED_ASSETS_READ)
    for capability in (
        Capability.SHARED_ASSETS_EDIT,
        Capability.SHARED_ASSETS_MANAGE_BINDINGS,
        Capability.MCP_CREDENTIALS_APPROVE,
    ):
        with pytest.raises(AssetForbidden) as exc_info:
            service_type._require_capability(actor, capability)
        assert exc_info.value.request_id == "r-bootstrap-only"


def test_runtime_system_mcp_context_keeps_only_read_and_credential_grant_approval() -> None:
    actor = SystemAssetGovernanceContext(
        user_id=SYSTEM_ADMIN_ID,
        request_id="r-system-mcp-grants",
    )

    for capability in (
        Capability.SHARED_ASSETS_READ,
        Capability.MCP_CREDENTIALS_APPROVE,
    ):
        McpService._require_capability(actor, capability)
    for capability in (
        Capability.SHARED_ASSETS_EDIT,
        Capability.SHARED_ASSETS_MANAGE_BINDINGS,
    ):
        with pytest.raises(AssetForbidden) as exc_info:
            McpService._require_capability(actor, capability)
        assert exc_info.value.request_id == "r-system-mcp-grants"


@pytest.mark.parametrize("service_type", [AgentService, SkillService, McpService])
def test_system_admin_project_override_keeps_asset_write_capability(service_type) -> None:
    actor = SystemAssetGovernanceContext(
        user_id=SYSTEM_ADMIN_ID,
        request_id="r-project-override",
        project_id=PROJECT_ID,
    )

    for capability in (
        Capability.SHARED_ASSETS_READ,
        Capability.SHARED_ASSETS_EDIT,
        Capability.SHARED_ASSETS_MANAGE_BINDINGS,
        Capability.MCP_CREDENTIALS_APPROVE,
    ):
        service_type._require_capability(actor, capability)


@pytest.mark.parametrize(
    ("error_type", "code", "status_code"),
    [
        (AssetNotFound, "asset_not_found", 404),
        (AssetForbidden, "asset_forbidden", 403),
        (AssetConflict, "asset_conflict", 409),
        (AssetValidationFailed, "asset_validation_failed", 422),
        (AssetStorageUnavailable, "asset_storage_unavailable", 503),
    ],
)
def test_shared_asset_errors_only_carry_public_code_and_request_id(
    error_type: type[Exception],
    code: str,
    status_code: int,
) -> None:
    error: Any = error_type("r3")

    assert error.code == code
    assert error.status_code == status_code
    assert error.request_id == "r3"
    assert error.__dict__ == {"request_id": "r3"}
    assert "r3" not in str(error)


def test_override_event_contains_only_governance_metadata() -> None:
    logger = _RecordingLogger()
    event_sink = SharedAssetGovernanceEventSink(logger=logger)

    event_sink.write_override(
        actor=SYSTEM_ADMIN_ID,
        project_id=PROJECT_ID,
        asset_id=ASSET_ID,
        version_id=VERSION_ID,
        action="publish",
        request_id="r1",
    )

    event = logger.events[0]
    assert set(event) == {"actor_user_id", "project_id", "asset_id", "version_id", "action", "request_id"}
    assert event == {
        "actor_user_id": str(SYSTEM_ADMIN_ID),
        "project_id": str(PROJECT_ID),
        "asset_id": str(ASSET_ID),
        "version_id": str(VERSION_ID),
        "action": "publish",
        "request_id": "r1",
    }
