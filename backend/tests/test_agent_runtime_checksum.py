from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from app.private_work.asset_runtime import _private_agent_manifest
from app.private_work.snapshot_repository import RunSnapshotAssetStale
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.errors import AssetResolutionUnavailable
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
)
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


def _snapshot() -> ResolvedAgentSnapshot:
    payload = AgentPayload(
        description="original",
        soul="soul",
        model_ref="model-a",
        tool_groups=("task",),
        skill_version_ids=(),
        mcp_version_ids=(),
        agents_instructions="instructions",
        identity="identity",
        user_context="user",
        payload_schema_version=2,
    )
    return ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum=agent_payload_checksum(payload),
        catalog_generation=7,
        dependency_version_ids=(),
        payload=payload,
    )


def test_private_runtime_manifest_recomputes_agent_payload_checksum() -> None:
    snapshot = _snapshot()
    tampered = replace(
        snapshot,
        payload=replace(snapshot.payload, description="tampered"),
    )

    with pytest.raises(RunSnapshotAssetStale):
        _private_agent_manifest(tampered, skills=(), mcps=())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("description", "tool_groups"),
    [
        pytest.param("tampered", ["task"], id="content-mismatch"),
        pytest.param("original", {"task": True}, id="noncanonical-json-shape"),
    ],
)
async def test_resolver_recomputes_database_agent_payload_checksum(
    description: str,
    tool_groups: object,
) -> None:
    snapshot = _snapshot()
    project_id = uuid.uuid4()
    asset = AgentRow(
        id=snapshot.asset_id,
        scope="project",
        project_id=project_id,
        slug="agent-a",
        display_name="Agent A",
        status="active",
        current_published_version_id=snapshot.version_id,
        version=1,
        created_by_user_id=str(uuid.uuid4()),
    )
    version = AgentVersionRow(
        id=snapshot.version_id,
        agent_id=snapshot.asset_id,
        version_number=1,
        workflow_status="published",
        description=description,
        agents_instructions=snapshot.payload.agents_instructions,
        soul=snapshot.payload.soul,
        identity=snapshot.payload.identity,
        user_context=snapshot.payload.user_context,
        model_ref=snapshot.payload.model_ref,
        model_settings={},
        tool_groups=tool_groups,
        supersedes_version_id=None,
        payload_schema_version=snapshot.payload.payload_schema_version,
        payload_checksum=snapshot.checksum,
        created_by_user_id=str(uuid.uuid4()),
    )
    context = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=project_id,
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(),
        membership_version=1,
        request_id="request-agent-checksum",
    )
    resolver = ProjectAssetResolver(lambda: None)  # type: ignore[arg-type]

    with pytest.raises(AssetResolutionUnavailable) as error:
        await resolver._agent_snapshot(  # noqa: SLF001 - exact runtime integrity boundary
            _EmptyRefSession(),  # type: ignore[arg-type]
            context,
            _ResolvedRecord(AssetScope.PROJECT, asset, version),
            snapshot.catalog_generation,
        )

    assert error.value.request_id == context.request_id
