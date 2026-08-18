from __future__ import annotations

import asyncio
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

from app.private_work.connection_service import ProjectConnectionService
from app.private_work.errors import PrivateWorkNotFound
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_service import AgentService
from app.shared_assets.errors import AssetConflict
from deerflow.persistence.channel_connections import (
    ChannelConnectionRepository,
    ChannelConnectionRow,
    ChannelOAuthStateRow,
    ProjectChannelGroupBindingChallengeRow,
    ProjectChannelGroupBindingRow,
    ProjectChannelInstanceRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow
from deerflow.persistence.user import UserRow

_ReferenceKind = Literal["binding", "challenge", "connection", "oauth_state"]


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
) -> tuple[uuid.UUID, uuid.UUID | str]:
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
        elif case.kind == "challenge":
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
        elif case.kind == "connection":
            reference_id = uuid.uuid4().hex
            session.add(
                ChannelConnectionRow(
                    id=reference_id,
                    project_id=seed.actor.project_id,
                    owner_user_id=str(seed.actor.user_id),
                    provider="legacy",
                    channel_instance_id=None,
                    status=case.state,
                    external_account_id=uuid.uuid4().hex,
                    workspace_id="",
                    metadata_json={
                        "agent_asset_id": str(agent_id),
                        "agent_scope": "project",
                    },
                )
            )
        else:
            reference_id = uuid.uuid4().hex * 2
            expires_at = now + timedelta(hours=1)
            consumed_at = None
            if case.state == "expired":
                expires_at = now - timedelta(hours=1)
            elif case.state == "consumed":
                consumed_at = now
            session.add(
                ChannelOAuthStateRow(
                    state_hash=reference_id,
                    project_id=seed.actor.project_id,
                    owner_user_id=str(seed.actor.user_id),
                    provider="lark",
                    channel_instance_id=seed.channel_instance_id,
                    metadata_json={
                        "agent_asset_id": str(agent_id),
                        "agent_scope": "project",
                    },
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
    reference_id: uuid.UUID | str,
    agent_should_exist: bool,
) -> None:
    reference_model = {
        "binding": ProjectChannelGroupBindingRow,
        "challenge": ProjectChannelGroupBindingChallengeRow,
        "connection": ChannelConnectionRow,
        "oauth_state": ChannelOAuthStateRow,
    }[case.kind]
    async with factory() as session:
        agent = await session.scalar(select(AgentRow).where(AgentRow.id == agent_id))
        if case.kind == "oauth_state":
            reference = await session.get(ChannelOAuthStateRow, reference_id)
        else:
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
        _ReferenceCase("legacy-connected-connection", "connection", "connected", True, True),
        _ReferenceCase("legacy-frozen-connection", "connection", "frozen", True, True),
        _ReferenceCase("legacy-revoked-connection", "connection", "revoked", False, True),
        _ReferenceCase("pending-oauth-state", "oauth_state", "pending", True, True),
        _ReferenceCase("expired-oauth-state", "oauth_state", "expired", False, True),
        _ReferenceCase("consumed-oauth-state", "oauth_state", "consumed", False, True),
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


async def _seed_executable_agent_with_oauth_state(
    factory: async_sessionmaker[AsyncSession],
    seed: _Seed,
    *,
    state: str,
) -> uuid.UUID:
    agent_id = uuid.uuid4()
    version_id = uuid.uuid4()
    async with factory() as session, session.begin():
        agent = AgentRow(
            id=agent_id,
            scope="project",
            project_id=seed.actor.project_id,
            slug=f"callback-race-{agent_id.hex[:8]}",
            display_name="Callback deletion race",
            status="active",
            version=2,
            created_by_user_id=str(seed.actor.user_id),
        )
        session.add(agent)
        await session.flush()
        session.add(
            AgentVersionRow(
                id=version_id,
                agent_id=agent_id,
                version_number=1,
                workflow_status="published",
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
        await session.flush()
        agent.current_published_version_id = version_id
        session.add(
            ChannelOAuthStateRow(
                state_hash=ChannelConnectionRepository.hash_state(state),
                project_id=seed.actor.project_id,
                owner_user_id=str(seed.actor.user_id),
                provider="lark",
                channel_instance_id=seed.channel_instance_id,
                metadata_json={
                    "agent_asset_id": str(agent_id),
                    "agent_scope": "project",
                    "membership_id": str(seed.actor.membership_id),
                    "membership_version": seed.actor.membership_version,
                    "request_id": seed.actor.request_id,
                },
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    return agent_id


class _PauseAfterOAuthConsumeRepository(ChannelConnectionRepository):
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        consumed: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        super().__init__(factory)
        self._consumed = consumed
        self._resume = resume

    async def consume_oauth_state(self, **kwargs):
        result = await super().consume_oauth_state(**kwargs)
        self._consumed.set()
        await self._resume.wait()
        return result


class _PauseAfterAgentGuardRepository(ChannelConnectionRepository):
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        guarded: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        super().__init__(factory)
        self._guarded = guarded
        self._resume = resume

    async def upsert_connection(self, **kwargs):
        transaction_guard = kwargs.get("transaction_guard")
        if transaction_guard is None:
            raise AssertionError("callback connection upsert requires a transaction guard")

        async def pausing_guard(session: AsyncSession) -> None:
            await transaction_guard(session)
            self._guarded.set()
            await self._resume.wait()

        kwargs["transaction_guard"] = pausing_guard
        return await super().upsert_connection(**kwargs)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_delete_wins_race_with_oauth_callback_without_dangling_connection(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    consumed = asyncio.Event()
    resume = asyncio.Event()
    state = "agent-delete-callback-race-state"
    try:
        seed = await _seed_project(factory)
        agent_id = await _seed_executable_agent_with_oauth_state(
            factory,
            seed,
            state=state,
        )
        repository = _PauseAfterOAuthConsumeRepository(
            factory,
            consumed=consumed,
            resume=resume,
        )
        callback = asyncio.create_task(
            ProjectConnectionService(factory, repository=repository).complete_callback(
                "lark",
                state,
                uuid.uuid4().hex,
                channel_instance_id=str(seed.channel_instance_id),
            )
        )
        await asyncio.wait_for(consumed.wait(), timeout=5)

        await AgentService(factory).delete(
            seed.actor,
            agent_id,
            expected_asset_version=2,
        )
        resume.set()

        with pytest.raises(PrivateWorkNotFound) as exc_info:
            await asyncio.wait_for(callback, timeout=5)
        assert exc_info.value.request_id == seed.actor.request_id
        assert str(exc_info.value) == PrivateWorkNotFound.public_message
        async with factory() as session:
            assert await session.get(AgentRow, agent_id) is None
            connection = await session.scalar(
                select(ChannelConnectionRow).where(
                    ChannelConnectionRow.project_id == seed.actor.project_id,
                )
            )
        assert connection is None
    finally:
        resume.set()
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_oauth_callback_wins_race_and_agent_delete_observes_connection(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    guarded = asyncio.Event()
    resume = asyncio.Event()
    state = "oauth-callback-agent-delete-race-state"
    callback: asyncio.Task[dict] | None = None
    deletion: asyncio.Task[None] | None = None
    try:
        seed = await _seed_project(factory)
        agent_id = await _seed_executable_agent_with_oauth_state(
            factory,
            seed,
            state=state,
        )
        repository = _PauseAfterAgentGuardRepository(
            factory,
            guarded=guarded,
            resume=resume,
        )
        callback = asyncio.create_task(
            ProjectConnectionService(factory, repository=repository).complete_callback(
                "lark",
                state,
                uuid.uuid4().hex,
                channel_instance_id=str(seed.channel_instance_id),
            )
        )
        await asyncio.wait_for(guarded.wait(), timeout=5)

        deletion = asyncio.create_task(
            AgentService(factory).delete(
                seed.actor,
                agent_id,
                expected_asset_version=2,
            )
        )
        done, _pending = await asyncio.wait({deletion}, timeout=0.2)
        assert not done, "Agent deletion must wait for the callback Agent lock"

        resume.set()
        connection = await asyncio.wait_for(callback, timeout=5)
        with pytest.raises(AssetConflict) as exc_info:
            await asyncio.wait_for(deletion, timeout=5)
        assert exc_info.value.request_id == seed.actor.request_id
        async with factory() as session:
            assert await session.get(AgentRow, agent_id) is not None
            persisted_connection = await session.get(
                ChannelConnectionRow,
                connection["id"],
            )
        assert persisted_connection is not None
    finally:
        resume.set()
        for task in (callback, deletion):
            if task is not None and not task.done():
                task.cancel()
        if callback is not None or deletion is not None:
            await asyncio.gather(
                *(task for task in (callback, deletion) if task is not None),
                return_exceptions=True,
            )
        await engine.dispose()
