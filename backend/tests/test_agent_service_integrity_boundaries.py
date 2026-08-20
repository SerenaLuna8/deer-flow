from __future__ import annotations

import uuid
from inspect import signature

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.agent_service import AgentService
from app.shared_assets.errors import AssetResolutionUnavailable
from app.shared_assets.models import AgentPayload, AssetScope
from app.shared_assets.resolver import ProjectAssetResolver, _ResolvedRecord
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow


class _EmptyScalarResult:
    def scalars(self) -> _EmptyScalarResult:
        return self

    def all(self) -> list[object]:
        return []


class _EmptyRefSession:
    async def execute(self, _statement) -> _EmptyScalarResult:
        return _EmptyScalarResult()


def _context(role: ProjectRole) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id=f"agent-integrity-{role.value}",
    )


def test_agent_service_has_no_incomplete_shell_create_entrypoint() -> None:
    assert not hasattr(AgentService, "create_asset")


def test_design_create_has_no_implicit_activation_escape_hatch() -> None:
    parameters = signature(
        AgentService.create_project_from_design_in_session,
    ).parameters
    assert "publish" not in parameters
    assert "activate" not in parameters


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ("agents_instructions", "identity", "user_context"),
)
async def test_resolver_rejects_unauthenticated_legacy_v1_fields(
    field: str,
) -> None:
    context = _context(ProjectRole.ADMIN)
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    payload = AgentPayload(
        description="Legacy Agent",
        soul="Legacy soul",
        model_ref="default",
        tool_groups=(),
        skill_refs=(),
        mcp_version_ids=(),
        payload_schema_version=1,
    )
    version_values = {
        "agents_instructions": "",
        "identity": "",
        "user_context": "",
    }
    version_values[field] = "not covered by the v1 checksum"
    asset = AgentRow(
        id=asset_id,
        scope="project",
        project_id=context.project_id,
        slug="legacy-agent",
        display_name="Legacy Agent",
        status="active",
        current_version_id=version_id,
        revision=1,
        created_by_user_id=str(context.user_id),
    )
    version = AgentVersionRow(
        id=version_id,
        agent_id=asset_id,
        version_number=1,
        description=payload.description,
        agents_instructions=version_values["agents_instructions"],
        soul=payload.soul,
        identity=version_values["identity"],
        user_context=version_values["user_context"],
        model_ref=payload.model_ref,
        model_settings={},
        tool_groups=[],
        supersedes_version_id=None,
        payload_schema_version=1,
        payload_checksum=agent_payload_checksum(payload),
        created_by_user_id=str(context.user_id),
    )

    with pytest.raises(AssetResolutionUnavailable):
        await ProjectAssetResolver(lambda: None)._agent_snapshot(  # noqa: SLF001
            _EmptyRefSession(),  # type: ignore[arg-type]
            context,
            _ResolvedRecord(AssetScope.PROJECT, asset, version),
            1,
        )
