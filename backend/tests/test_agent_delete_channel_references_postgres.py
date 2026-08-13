from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_service import AgentService
from app.shared_assets.errors import AssetConflict
from deerflow.persistence.channel_connections import (
    ProjectChannelGroupBindingChallengeRow,
    ProjectChannelGroupBindingRow,
    ProjectChannelInstanceRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow
from deerflow.persistence.user import UserRow

_ReferenceKind = Literal["binding", "challenge"]


@dataclass(frozen=True)
class _ReferenceCase:
    name: str
    kind: _ReferenceKind
    state: str
    blocks_deletion: bool
    reference_survives: bool


@dataclass(frozen=True)
class _Seed:
    actor: ProjectContext
    channel_instance_id: uuid.UUID


async def _seed_project(
    factory: async_sessionmaker[AsyncSession],
) -> _Seed:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    channel_instance_id = uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(
            UserRow(
                id=str(user_id),
                email=f"{user_id}@example.com",
                password_hash=None,
                system_role="user",
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()
        session.add(
            ProjectRow(
                id=project_id,
                slug=f"agent-delete-{project_id.hex[:12]}",
                display_name="Agent delete reference matrix",
                created_by_user_id=str(user_id),
            )
        )
        await session.flush()
        session.add(
            ProjectMembershipRow(
                id=membership_id,
                project_id=project_id,
                user_id=str(user_id),
                role=ProjectRole.ADMIN.value,
                status="active",
                version=1,
            )
        )
        session.add(
            ProjectChannelInstanceRow(
                id=channel_instance_id,
                project_id=project_id,
                provider="lark",
                display_name="Agent deletion test channel",
                provider_identity_digest=uuid.uuid4().hex * 2,
                created_by_user_id=str(user_id),
                updated_by_user_id=str(user_id),
            )
        )

    return _Seed(
        actor=ProjectContext(
            user_id=user_id,
            project_id=project_id,
            membership_id=membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="agent-delete-channel-references-postgres",
        ),
        channel_instance_id=channel_instance_id,
    )


async def _seed_agent_reference(
    factory: async_sessionmaker[AsyncSession],
    seed: _Seed,
    case: _ReferenceCase,
) -> tuple[uuid.UUID, uuid.UUID]:
    agent_id = uuid.uuid4()
    version_id = uuid.uuid4()
    reference_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        session.add(
            AgentRow(
                id=agent_id,
                scope="project",
                project_id=seed.actor.project_id,
                slug=f"delete-{case.name}-{agent_id.hex[:8]}",
                display_name=f"Delete matrix: {case.name}",
                status="suspended",
                version=2,
                created_by_user_id=str(seed.actor.user_id),
            )
        )
        await session.flush()
        session.add(
            AgentVersionRow(
                id=version_id,
                agent_id=agent_id,
                version_number=1,
                workflow_status="draft",
                description="",
                agents_instructions="",
                soul="",
                identity="",
                user_context="",
                model_ref="default",
                model_settings={},
                tool_groups=[],
                payload_schema_version=3,
                payload_checksum="0" * 64,
                created_by_user_id=str(seed.actor.user_id),
            )
        )

        if case.kind == "binding":
            deleted_at = now if case.state == "soft_deleted" else None
            status = "disabled" if case.state != "live" else "active"
            session.add(
                ProjectChannelGroupBindingRow(
                    id=reference_id,
                    project_id=seed.actor.project_id,
                    channel_instance_id=seed.channel_instance_id,
                    provider="lark",
                    external_group_ref=uuid.uuid4().hex * 2,
                    external_group_name=case.name,
                    agent_scope=None if deleted_at is not None else "project",
                    agent_asset_id=None if deleted_at is not None else agent_id,
                    status=status,
                    created_by_user_id=str(seed.actor.user_id),
                    updated_by_user_id=str(seed.actor.user_id),
                    deleted_at=deleted_at,
                )
            )
        else:
            created_at = now - timedelta(minutes=5)
            expires_at = now + timedelta(hours=1)
            consumed_at = None
            if case.state == "expired":
                created_at = now - timedelta(hours=2)
                expires_at = now - timedelta(hours=1)
            elif case.state == "consumed":
                consumed_at = now
            session.add(
                ProjectChannelGroupBindingChallengeRow(
                    id=reference_id,
                    project_id=seed.actor.project_id,
                    channel_instance_id=seed.channel_instance_id,
                    provider="lark",
                    code_digest=uuid.uuid4().hex * 2,
                    agent_asset_id=agent_id,
                    agent_scope="project",
                    membership_id=seed.actor.membership_id,
                    membership_version=seed.actor.membership_version,
                    created_by_user_id=str(seed.actor.user_id),
                    created_at=created_at,
                    expires_at=expires_at,
                    consumed_at=consumed_at,
                )
            )

    return agent_id, reference_id


async def _assert_persisted_state(
    factory: async_sessionmaker[AsyncSession],
    *,
    case: _ReferenceCase,
    agent_id: uuid.UUID,
    reference_id: uuid.UUID,
    agent_should_exist: bool,
) -> None:
    reference_model = ProjectChannelGroupBindingRow if case.kind == "binding" else ProjectChannelGroupBindingChallengeRow
    async with factory() as session:
        agent = await session.scalar(select(AgentRow).where(AgentRow.id == agent_id))
        reference = await session.scalar(select(reference_model).where(reference_model.id == reference_id))
        version = await session.scalar(select(AgentVersionRow).where(AgentVersionRow.agent_id == agent_id))
    assert (agent is not None) is agent_should_exist, case.name
    assert (version is not None) is agent_should_exist, case.name
    assert (reference is not None) is case.reference_survives, case.name


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_agent_delete_channel_reference_matrix(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cases = (
        _ReferenceCase("live-binding", "binding", "live", True, True),
        _ReferenceCase("disabled-binding", "binding", "disabled", True, True),
        _ReferenceCase("soft-deleted-binding", "binding", "soft_deleted", False, True),
        _ReferenceCase("pending-challenge", "challenge", "pending", True, True),
        _ReferenceCase("expired-challenge", "challenge", "expired", False, False),
        _ReferenceCase("consumed-challenge", "challenge", "consumed", False, False),
    )
    try:
        seed = await _seed_project(factory)
        service = AgentService(factory)
        for case in cases:
            agent_id, reference_id = await _seed_agent_reference(
                factory,
                seed,
                case,
            )
            if case.blocks_deletion:
                with pytest.raises(AssetConflict) as exc_info:
                    await service.delete(
                        seed.actor,
                        agent_id,
                        expected_asset_version=2,
                    )
                assert exc_info.value.request_id == seed.actor.request_id, case.name
            else:
                await service.delete(
                    seed.actor,
                    agent_id,
                    expected_asset_version=2,
                )
            await _assert_persisted_state(
                factory,
                case=case,
                agent_id=agent_id,
                reference_id=reference_id,
                agent_should_exist=case.blocks_deletion,
            )
    finally:
        await engine.dispose()
