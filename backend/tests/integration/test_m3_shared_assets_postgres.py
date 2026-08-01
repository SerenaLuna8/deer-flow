from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import StrEnum

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from support.m3_shared_assets import M3Scenario

from app.audit.service import AuditService
from app.projects.context import ProjectContext
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.agent_service import CreateAgent
from app.shared_assets.audit import DurableSharedAssetGovernanceEventSink
from app.shared_assets.default_agent_service import ProjectDefaultAgentService
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetResolutionUnavailable,
)
from app.shared_assets.models import AgentPayload
from deerflow.persistence.audit.model import AuditLogRow


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_json_value(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


def _assert_sentinel_absent(value: object, sentinel: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            assert sentinel not in str(key)
            _assert_sentinel_absent(item, sentinel)
        return
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in value:
            _assert_sentinel_absent(item, sentinel)
        return
    assert sentinel not in str(value)


def _agent_payload(
    *,
    skill_version_ids: tuple[uuid.UUID, ...] = (),
) -> AgentPayload:
    return AgentPayload(
        description="Default Agent release-gate fixture",
        soul="Keep project authority deterministic.",
        model_ref="default",
        tool_groups=(),
        skill_version_ids=skill_version_ids,
        mcp_version_ids=(),
    )


async def _publish_project_agent(
    scenario: M3Scenario,
    actor: ProjectContext,
    *,
    slug: str,
    skill_version_ids: tuple[uuid.UUID, ...] = (),
):
    asset = await scenario.agents.create_asset(
        actor,
        CreateAgent(slug, slug.replace("-", " ").title()),
    )
    draft = await scenario.agents.create_version(
        actor,
        asset.id,
        _agent_payload(skill_version_ids=skill_version_ids),
        expected_asset_version=asset.version,
    )
    await scenario.agents.publish(
        actor,
        asset.id,
        draft.id,
        expected_asset_version=asset.version + 1,
    )
    return await scenario.agents.get(actor, asset.id)


async def _seed_published_project_skill(
    scenario: M3Scenario,
) -> tuple[uuid.UUID, uuid.UUID]:
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    async with scenario.engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO skills
                (id,scope,project_id,slug,display_name,status,created_by_user_id)
                VALUES (:id,'project',:project,:slug,'Default Agent Skill','active',:user)"""
            ),
            {
                "id": skill_id,
                "project": scenario.project_admin.project_id,
                "slug": f"default-agent-skill-{skill_id.hex[:8]}",
                "user": str(scenario.project_admin.user_id),
            },
        )
        await connection.execute(
            text(
                """INSERT INTO skill_versions
                (id,skill_id,version_number,workflow_status,description,
                 frontmatter,secret_requirements,scan_decision,scan_summary,
                 payload_checksum,created_by_user_id)
                VALUES (:id,:skill,1,'published','','{}'::jsonb,'[]'::jsonb,
                        'allow','{}'::jsonb,:checksum,:user)"""
            ),
            {
                "id": version_id,
                "skill": skill_id,
                "checksum": "3" * 64,
                "user": str(scenario.project_admin.user_id),
            },
        )
        await connection.execute(
            text(
                """UPDATE skills SET current_published_version_id=:version
                WHERE id=:skill"""
            ),
            {"version": version_id, "skill": skill_id},
        )
    return skill_id, version_id


@pytest.mark.asyncio
async def test_m3_end_to_end_shared_asset_governance(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M3Scenario.create(migrated_postgres_database_url)
    try:
        published = await scenario.bootstrap_system_catalog()
        binding = await scenario.bind_system_agent(published.agent_v1)
        with pytest.raises(AssetForbidden):
            await scenario.attempt_runtime_system_agent_version()

        assert binding.version_id == published.agent_v1
        assert (await scenario.resolve_bound_agent()).version_id == published.agent_v1

        with pytest.raises(AssetForbidden):
            await scenario.editor_approve_project_mcp()
        with pytest.raises(AssetNotFound):
            await scenario.other_project_read_project_agent()

        with pytest.raises(AssetForbidden):
            await scenario.suspend_bound_system_agent()
        assert (await scenario.resolve_bound_agent()).version_id == published.agent_v1

        snapshot = await scenario.resolve_project_mcp_before_revoke()
        assert snapshot.credential_grant_ids
        secret_sentinel = scenario.credential_secret_sentinel()
        serialized_snapshot = _json_value(snapshot)
        _assert_sentinel_absent(serialized_snapshot, secret_sentinel)
        assert secret_sentinel not in json.dumps(
            serialized_snapshot,
            ensure_ascii=False,
            sort_keys=True,
        )

        await scenario.revoke_project_credential()
        with pytest.raises(AssetResolutionUnavailable):
            await scenario.resolve_project_mcp()
    finally:
        await scenario.close()


@pytest.mark.asyncio
async def test_m3_project_default_agent_cas_permissions_lifecycle_and_audit(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M3Scenario.create(migrated_postgres_database_url)
    runner = await M3Scenario._seed_member(
        scenario.engine,
        scenario.session_factory,
        project_id=scenario.project_admin.project_id,
        label="m3-default-runner",
        role="runner",
    )
    service = ProjectDefaultAgentService(
        scenario.session_factory,
        governance_sink=DurableSharedAssetGovernanceEventSink(
            AuditService(
                scenario.session_factory,
                AuditHmacKeyring(
                    active_key_id="m3-default-agent-v1",
                    _keys={"m3-default-agent-v1": b"d" * 32},
                ),
            )
        ),
    )
    try:
        asset = await _publish_project_agent(
            scenario,
            scenario.project_admin,
            slug="m3-project-default",
        )

        assert (await service.get(scenario.project_admin)).revision == 0
        selected = await service.replace(
            scenario.project_admin,
            asset.id,
            expected_revision=0,
        )
        assert selected.agent_asset_id == asset.id
        assert selected.revision == 1
        assert await service.get(scenario.project_admin) == selected

        for member in (scenario.project_editor, runner):
            with pytest.raises(AssetForbidden):
                await service.replace(
                    member,
                    None,
                    expected_revision=1,
                )

        with pytest.raises(AssetConflict):
            await service.replace(
                scenario.project_admin,
                None,
                expected_revision=0,
            )
        with pytest.raises(AssetConflict):
            await scenario.agents.suspend(
                scenario.project_admin,
                asset.id,
                expected_asset_version=asset.version,
            )
        with pytest.raises(AssetConflict):
            await scenario.agents.delete(
                scenario.project_admin,
                asset.id,
                expected_asset_version=asset.version,
            )

        cleared = await service.replace(
            scenario.project_admin,
            None,
            expected_revision=1,
        )
        assert cleared.agent_asset_id is None
        assert cleared.revision == 2

        async with scenario.session_factory() as session:
            audit_rows = tuple(
                (
                    await session.execute(
                        select(AuditLogRow).where(
                            AuditLogRow.project_id == scenario.project_admin.project_id,
                            AuditLogRow.action.in_(("asset.bound", "asset.unbound")),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert {row.action for row in audit_rows} == {
            "asset.bound",
            "asset.unbound",
        }
        assert all(row.metadata_json == {"asset_kind": "agent"} for row in audit_rows)
        assert all(str(asset.id) not in repr(row.__dict__) for row in audit_rows)

        suspended = await scenario.agents.suspend(
            scenario.project_admin,
            asset.id,
            expected_asset_version=asset.version,
        )
        assert suspended.status == "suspended"
    finally:
        await scenario.close()


@pytest.mark.asyncio
async def test_m3_project_default_agent_rejects_cross_project_and_broken_closure(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M3Scenario.create(migrated_postgres_database_url)
    service = ProjectDefaultAgentService(scenario.session_factory)
    try:
        outsider = await _publish_project_agent(
            scenario,
            scenario.other_project_admin,
            slug="m3-outsider-default",
        )
        with pytest.raises(AssetNotFound):
            await service.replace(
                scenario.project_admin,
                outsider.id,
                expected_revision=0,
            )

        with pytest.raises(IntegrityError) as cross_project_fk:
            async with scenario.engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO project_default_agents
                        (project_id,agent_asset_id,revision,
                         created_by_user_id,updated_by_user_id)
                        VALUES (:project,:agent,1,:user,:user)"""
                    ),
                    {
                        "project": scenario.project_admin.project_id,
                        "agent": outsider.id,
                        "user": str(scenario.project_admin.user_id),
                    },
                )
        assert "fk_project_default_agents_project_agent" in str(cross_project_fk.value.orig)

        skill_id, skill_version_id = await _seed_published_project_skill(scenario)
        target = await _publish_project_agent(
            scenario,
            scenario.project_admin,
            slug="m3-broken-default",
            skill_version_ids=(skill_version_id,),
        )
        async with scenario.engine.begin() as connection:
            await connection.execute(
                text("UPDATE skills SET status='suspended' WHERE id=:skill"),
                {"skill": skill_id},
            )

        with pytest.raises(AssetConflict):
            await service.replace(
                scenario.project_admin,
                target.id,
                expected_revision=0,
            )
        assert (await service.get(scenario.project_admin)).revision == 0
    finally:
        await scenario.close()
