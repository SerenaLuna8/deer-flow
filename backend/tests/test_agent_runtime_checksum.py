from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from app.private_work.asset_runtime import _private_agent_manifest
from app.private_work.snapshot_repository import RunSnapshotAssetStale
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_payload_checksum import (
    agent_payload_checksum,
    legacy_agent_payload_checksum,
)
from app.shared_assets.errors import AssetResolutionUnavailable
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    SkillAssetRef,
)
from app.shared_assets.resolver import ProjectAssetResolver, _ResolvedRecord
from app.shared_assets.run_snapshot_codec import (
    RunAssetSnapshotInvalid,
    decode_run_asset_snapshot,
    encode_run_asset_snapshot,
)
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow, SkillRow, SkillVersionRow

_MODEL_REF = "88888888-8888-4888-8888-888888888891"


class _EmptyScalarResult:
    def __init__(self, rows: list[object] | None = None) -> None:
        self._rows = [] if rows is None else rows

    def scalars(self) -> _EmptyScalarResult:
        return self

    def all(self) -> list[object]:
        return self._rows


class _EmptyRefSession:
    async def execute(self, _statement) -> _EmptyScalarResult:
        return _EmptyScalarResult()


class _ProjectSkillRefSession:
    async def execute(self, _statement) -> _EmptyScalarResult:
        return _EmptyScalarResult([("project", uuid.uuid4())])


class _SequencedRefSession:
    def __init__(self, rows: list[list[object]]) -> None:
        self._rows = iter(rows)

    async def execute(self, _statement) -> _EmptyScalarResult:
        return _EmptyScalarResult(next(self._rows))


def _snapshot() -> ResolvedAgentSnapshot:
    payload = AgentPayload(
        description="original",
        soul="soul",
        model_ref=_MODEL_REF,
        tool_groups=("task",),
        skill_refs=(),
        mcp_version_ids=(),
        agents_instructions="instructions",
        identity="identity",
        user_context="user",
        payload_schema_version=4,
    )
    return ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum=agent_payload_checksum(payload),
        catalog_generation=7,
        dependency_version_ids=(),
        skill_version_ids=(),
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


def _legacy_snapshot() -> ResolvedAgentSnapshot:
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    payload = AgentPayload(
        description="legacy",
        soul="legacy soul",
        model_ref=_MODEL_REF,
        tool_groups=("task",),
        skill_refs=(SkillAssetRef(AssetScope.PROJECT, skill_id),),
        mcp_version_ids=(),
        agents_instructions="legacy instructions",
        identity="legacy identity",
        user_context="legacy user",
        payload_schema_version=3,
    )
    return ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum=legacy_agent_payload_checksum(payload, (skill_version_id,)),
        catalog_generation=9,
        dependency_version_ids=(skill_version_id,),
        skill_version_ids=(skill_version_id,),
        payload=payload,
    )


def test_legacy_agent_snapshot_round_trips_and_builds_manifest() -> None:
    snapshot = _legacy_snapshot()

    decoded = decode_run_asset_snapshot(encode_run_asset_snapshot(snapshot))

    assert decoded == snapshot
    assert _private_agent_manifest(snapshot, skills=(), mcps=()).checksum == snapshot.checksum


@pytest.mark.parametrize("tamper", ["content", "skill-version"])
def test_legacy_agent_snapshot_rejects_tampering(tamper: str) -> None:
    snapshot = _legacy_snapshot()
    encoded = encode_run_asset_snapshot(snapshot)
    agent = encoded["agent"]
    assert isinstance(agent, dict)
    if tamper == "content":
        agent["description"] = "tampered"
    else:
        agent["resolved_skill_version_ids"] = [str(uuid.uuid4())]

    with pytest.raises(RunAssetSnapshotInvalid):
        decode_run_asset_snapshot(encoded)


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
        current_version_id=snapshot.version_id,
        revision=1,
        created_by_user_id=str(uuid.uuid4()),
    )
    version = AgentVersionRow(
        id=snapshot.version_id,
        agent_id=snapshot.asset_id,
        version_number=1,
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


@pytest.mark.asyncio
async def test_resolver_rejects_system_agent_project_skill_reference() -> None:
    snapshot = _snapshot()
    asset = AgentRow(
        id=snapshot.asset_id,
        scope="system",
        project_id=None,
        slug="system-agent",
        display_name="System Agent",
        status="active",
        current_version_id=snapshot.version_id,
        revision=1,
        source_key="builtin:agent:system-agent",
        created_by_user_id=str(uuid.uuid4()),
    )
    version = AgentVersionRow(
        id=snapshot.version_id,
        agent_id=snapshot.asset_id,
        version_number=1,
        description=snapshot.payload.description,
        agents_instructions=snapshot.payload.agents_instructions,
        soul=snapshot.payload.soul,
        identity=snapshot.payload.identity,
        user_context=snapshot.payload.user_context,
        model_ref=snapshot.payload.model_ref,
        model_settings={},
        tool_groups=list(snapshot.payload.tool_groups),
        supersedes_version_id=None,
        payload_schema_version=4,
        payload_checksum=snapshot.checksum,
        created_by_user_id=str(uuid.uuid4()),
    )
    context = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(),
        membership_version=1,
        request_id="system-agent-project-skill-ref",
    )

    with pytest.raises(AssetResolutionUnavailable):
        await ProjectAssetResolver(lambda: None)._agent_snapshot(  # noqa: SLF001
            _ProjectSkillRefSession(),  # type: ignore[arg-type]
            context,
            _ResolvedRecord(AssetScope.SYSTEM, asset, version),
            1,
        )


@pytest.mark.asyncio
async def test_resolver_projects_attested_legacy_agent_into_runtime_v4() -> None:
    project_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    current_skill_version_id = uuid.uuid4()
    asset = AgentRow(
        id=agent_id,
        scope="project",
        project_id=project_id,
        slug="legacy-agent",
        display_name="Legacy Agent",
        status="active",
        current_version_id=agent_version_id,
        revision=1,
        created_by_user_id=str(uuid.uuid4()),
    )
    version = AgentVersionRow(
        id=agent_version_id,
        agent_id=agent_id,
        version_number=3,
        description="legacy definition",
        agents_instructions="instructions",
        soul="soul",
        identity="identity",
        user_context="user",
        model_ref=_MODEL_REF,
        model_settings={},
        tool_groups=["task"],
        supersedes_version_id=None,
        payload_schema_version=3,
        payload_checksum="a" * 64,
        created_by_user_id=str(uuid.uuid4()),
    )
    skill = SkillRow(
        id=skill_id,
        scope="project",
        project_id=project_id,
        slug="current-skill",
        display_name="Current Skill",
        status="active",
        current_version_id=current_skill_version_id,
        revision=1,
        created_by_user_id=str(uuid.uuid4()),
    )
    skill_version = SkillVersionRow(
        id=current_skill_version_id,
        skill_id=skill_id,
        version_number=4,
        description="current skill",
        frontmatter={"name": "current-skill"},
        compatibility=None,
        secret_requirements=[],
        scan_decision="allow",
        scan_summary={"rule_ids": []},
        payload_checksum="b" * 64,
        created_by_user_id=str(uuid.uuid4()),
    )
    actor = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=project_id,
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(),
        membership_version=1,
        request_id="legacy-runtime-projection",
    )

    snapshot = await ProjectAssetResolver(lambda: None)._agent_snapshot(  # noqa: SLF001
        _SequencedRefSession([[("project", skill_id)], []]),  # type: ignore[arg-type]
        actor,
        _ResolvedRecord(AssetScope.PROJECT, asset, version),
        3,
        exact_dependency_records=(
            (_ResolvedRecord(AssetScope.PROJECT, skill, skill_version),),
            (),
        ),
    )

    assert snapshot.payload.payload_schema_version == 4
    assert snapshot.skill_version_ids == (current_skill_version_id,)
    assert snapshot.checksum == agent_payload_checksum(snapshot.payload)
    assert snapshot.checksum != version.payload_checksum
    assert decode_run_asset_snapshot(encode_run_asset_snapshot(snapshot)) == snapshot
