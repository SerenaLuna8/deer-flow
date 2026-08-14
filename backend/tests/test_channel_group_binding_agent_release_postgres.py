from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.audit.models import resolve_system_audit_context
from app.channel_group_bindings.repository import (
    GroupBindingRepositoryConflict,
    PostgresProjectChannelGroupBindingRepository,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkNotFound
from app.private_work.inbound_dedupe import PrivateRunInboundDelivery
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
    PrivateRunInboundAuthority,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.projects.system_lifecycle import SystemProjectLifecycleService
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.agent_service import AgentService
from app.shared_assets.errors import AssetConflict
from app.shared_assets.models import AgentPayload
from deerflow.persistence.channel_connections import (
    ChannelExternalPrincipalRow,
    ProjectChannelGroupBindingChallengeRow,
    ProjectChannelGroupBindingRow,
    ProjectChannelInstanceRow,
)
from deerflow.persistence.channel_connections.model import (
    ChannelConnectionRow,
    ChannelConversationRow,
)
from deerflow.persistence.channel_connections.sql import ChannelConnectionRepository
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user import UserRow
from deerflow.runtime.private_scope import PrivateResourceScope


@dataclass(frozen=True)
class _LifecycleSeed:
    context: ProjectContext
    channel_instance_id: uuid.UUID
    old_agent_id: uuid.UUID
    new_agent_id: uuid.UUID
    binding_id: uuid.UUID
    principal_id: uuid.UUID
    guest_user_id: uuid.UUID
    guest_membership_id: uuid.UUID
    connection_id: str
    external_group_ref: str
    external_account_ref: str


@dataclass(frozen=True)
class _SystemAdmin:
    id: uuid.UUID
    system_role: str = "system_admin"


class _NoopSystemLifecycleAudit:
    async def project_suspended(
        self,
        _session: AsyncSession,
        *,
        project_id: uuid.UUID,
    ) -> None:
        del project_id

    async def project_resumed(
        self,
        _session: AsyncSession,
        *,
        project_id: uuid.UUID,
    ) -> None:
        del project_id


async def _add_published_agent(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    slug: str,
) -> uuid.UUID:
    agent_id = uuid.uuid4()
    version_id = uuid.uuid4()
    payload = AgentPayload(
        description=slug,
        soul="",
        model_ref="default",
        tool_groups=(),
        skill_version_ids=(),
        mcp_version_ids=(),
        payload_schema_version=3,
    )
    agent = AgentRow(
        id=agent_id,
        scope="project",
        project_id=project_id,
        slug=slug,
        display_name=slug,
        status="active",
        version=1,
        created_by_user_id=str(actor_id),
    )
    session.add(agent)
    await session.flush()
    session.add(
        AgentVersionRow(
            id=version_id,
            agent_id=agent_id,
            version_number=1,
            workflow_status="published",
            description=payload.description,
            agents_instructions=payload.agents_instructions,
            soul=payload.soul,
            identity=payload.identity,
            user_context=payload.user_context,
            model_ref=payload.model_ref,
            model_settings={},
            tool_groups=[],
            payload_schema_version=payload.payload_schema_version,
            payload_checksum=agent_payload_checksum(payload),
            created_by_user_id=str(actor_id),
        )
    )
    await session.flush()
    agent.current_published_version_id = version_id
    await session.flush()
    return agent_id


async def _seed_lifecycle(
    factory: async_sessionmaker[AsyncSession],
) -> _LifecycleSeed:
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    channel_instance_id = uuid.uuid4()
    binding_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    guest_user_id = uuid.uuid4()
    guest_membership_id = uuid.uuid4()
    connection_id = principal_id.hex
    external_group_ref = uuid.uuid4().hex * 2
    external_account_ref = uuid.uuid4().hex * 2
    now = datetime.now(UTC)

    async with factory() as session, session.begin():
        session.add(
            UserRow(
                id=str(actor_id),
                email=f"binding-release-{actor_id}@example.com",
                password_hash=None,
                system_role="user",
                needs_setup=False,
                token_version=0,
            )
        )
        session.add(
            UserRow(
                id=str(guest_user_id),
                email=None,
                password_hash=None,
                principal_type="channel_guest",
                system_role="user",
                oauth_provider=None,
                oauth_id=None,
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()
        session.add(
            ProjectRow(
                id=project_id,
                slug=f"binding-release-{project_id.hex[:8]}",
                display_name="Binding release lifecycle",
                created_by_user_id=str(actor_id),
            )
        )
        await session.flush()
        session.add_all(
            [
                ProjectMembershipRow(
                    id=membership_id,
                    project_id=project_id,
                    user_id=str(actor_id),
                    role="admin",
                    status="active",
                    version=1,
                ),
                ProjectMembershipRow(
                    id=guest_membership_id,
                    project_id=project_id,
                    user_id=str(guest_user_id),
                    role="channel_guest",
                    status="active",
                    version=1,
                ),
            ]
        )
        session.add(
            ProjectChannelInstanceRow(
                id=channel_instance_id,
                project_id=project_id,
                provider="lark",
                display_name="Binding release channel",
                desired_status="enabled",
                observed_status="running",
                provider_identity_digest=uuid.uuid4().hex * 2,
                created_by_user_id=str(actor_id),
                updated_by_user_id=str(actor_id),
            )
        )
        await session.flush()
        old_agent_id = await _add_published_agent(
            session,
            project_id=project_id,
            actor_id=actor_id,
            slug=f"binding-old-{project_id.hex[:8]}",
        )
        new_agent_id = await _add_published_agent(
            session,
            project_id=project_id,
            actor_id=actor_id,
            slug=f"binding-new-{project_id.hex[:8]}",
        )
        session.add(
            ProjectChannelGroupBindingRow(
                id=binding_id,
                project_id=project_id,
                channel_instance_id=channel_instance_id,
                provider="lark",
                external_group_ref=external_group_ref,
                external_group_name="Lifecycle group",
                agent_asset_id=old_agent_id,
                agent_scope="project",
                status="active",
                revision=1,
                created_by_user_id=str(actor_id),
                updated_by_user_id=str(actor_id),
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            ChannelExternalPrincipalRow(
                id=principal_id,
                project_id=project_id,
                group_binding_id=binding_id,
                external_account_ref=external_account_ref,
                principal_user_id=str(guest_user_id),
                membership_id=guest_membership_id,
                status="active",
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ChannelConnectionRow(
                id=connection_id,
                project_id=project_id,
                owner_user_id=str(guest_user_id),
                provider="lark",
                channel_instance_id=channel_instance_id,
                status="connected",
                external_account_id=external_account_ref,
                workspace_id=external_group_ref,
                scopes_json=[],
                capabilities_json={},
                metadata_json={
                    "group_binding_id": str(binding_id),
                    "agent_asset_id": str(old_agent_id),
                    "agent_scope": "project",
                },
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
        )

    return _LifecycleSeed(
        context=ProjectContext(
            user_id=actor_id,
            project_id=project_id,
            membership_id=membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="binding-agent-release-postgres",
        ),
        channel_instance_id=channel_instance_id,
        old_agent_id=old_agent_id,
        new_agent_id=new_agent_id,
        binding_id=binding_id,
        principal_id=principal_id,
        guest_user_id=guest_user_id,
        guest_membership_id=guest_membership_id,
        connection_id=connection_id,
        external_group_ref=external_group_ref,
        external_account_ref=external_account_ref,
    )


async def _seed_old_conversation(
    factory: async_sessionmaker[AsyncSession],
    seed: _LifecycleSeed,
) -> tuple[str, str]:
    old_thread_id = uuid.uuid4().hex
    conversation_id = uuid.uuid4().hex
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        session.add(
            ThreadMetaRow(
                thread_id=old_thread_id,
                assistant_id=None,
                owner_user_id=str(seed.guest_user_id),
                display_name="Old Agent group conversation",
                status="idle",
                metadata_json={},
                project_id=seed.context.project_id,
                agent_asset_id=seed.old_agent_id,
                agent_scope="project",
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            ChannelConversationRow(
                id=conversation_id,
                connection_id=seed.connection_id,
                owner_user_id=str(seed.guest_user_id),
                provider="lark",
                external_conversation_id=seed.external_group_ref,
                external_topic_id="",
                thread_id=old_thread_id,
                project_id=seed.context.project_id,
                created_at=now,
                updated_at=now,
            )
        )
    return old_thread_id, conversation_id


async def _seed_ordinary_same_owner_connection(
    factory: async_sessionmaker[AsyncSession],
    seed: _LifecycleSeed,
) -> str:
    connection_id = uuid.uuid4().hex
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        session.add(
            ChannelConnectionRow(
                id=connection_id,
                project_id=seed.context.project_id,
                owner_user_id=str(seed.guest_user_id),
                provider="lark",
                channel_instance_id=seed.channel_instance_id,
                status="connected",
                external_account_id=uuid.uuid4().hex * 2,
                workspace_id=uuid.uuid4().hex * 2,
                scopes_json=[],
                capabilities_json={},
                metadata_json={},
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
        )
    return connection_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_soft_delete_releases_agent_and_rebind_reuses_identity(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresProjectChannelGroupBindingRepository()
    try:
        seed = await _seed_lifecycle(factory)
        deleted_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            await repository.lock_project_context(session, seed.context, read=False)
            await repository.delete_binding(
                session,
                seed.context,
                binding_id=seed.binding_id,
                expected_revision=1,
                now=deleted_at,
            )

        async with factory() as session:
            binding = await session.get(ProjectChannelGroupBindingRow, seed.binding_id)
            principal = await session.get(ChannelExternalPrincipalRow, seed.principal_id)
            membership = await session.get(ProjectMembershipRow, seed.guest_membership_id)
            guest = await session.get(UserRow, str(seed.guest_user_id))
            connection = await session.get(ChannelConnectionRow, seed.connection_id)
        assert binding is not None
        assert binding.status == "disabled"
        assert binding.deleted_at == deleted_at
        assert binding.agent_asset_id is None
        assert binding.agent_scope is None
        assert binding.revision == 2
        assert principal is not None and principal.status == "frozen"
        assert principal.membership_id == seed.guest_membership_id
        assert membership is not None and membership.status == "active"
        assert guest is not None and guest.principal_type == "channel_guest"
        assert connection is not None and connection.status == "frozen"
        assert connection.frozen_at == deleted_at
        assert connection.metadata_json == {"group_binding_id": str(seed.binding_id)}

        await AgentService(factory).delete(
            seed.context,
            seed.old_agent_id,
            expected_asset_version=1,
        )
        async with factory() as session:
            assert await session.get(AgentRow, seed.old_agent_id) is None

        code_digest = uuid.uuid4().hex * 2
        challenged_at = deleted_at + timedelta(seconds=1)
        async with factory() as session, session.begin():
            session.add(
                ProjectChannelGroupBindingChallengeRow(
                    project_id=seed.context.project_id,
                    channel_instance_id=seed.channel_instance_id,
                    provider="lark",
                    code_digest=code_digest,
                    agent_asset_id=seed.new_agent_id,
                    agent_scope="project",
                    membership_id=seed.context.membership_id,
                    membership_version=seed.context.membership_version,
                    created_by_user_id=str(seed.context.user_id),
                    created_at=challenged_at,
                    expires_at=challenged_at + timedelta(minutes=10),
                )
            )

        completed_at = challenged_at + timedelta(seconds=1)
        async with factory() as session, session.begin():
            rebound = await repository.complete_challenge(
                session,
                provider="lark",
                channel_instance_id=seed.channel_instance_id,
                code_digest=code_digest,
                external_group_ref=seed.external_group_ref,
                external_group_refs=(seed.external_group_ref,),
                display_name="Lifecycle group rebound",
                now=completed_at,
            )
            assert rebound is not None and rebound.id == seed.binding_id

        async with factory() as session:
            binding = await session.get(ProjectChannelGroupBindingRow, seed.binding_id)
            principal = await session.get(ChannelExternalPrincipalRow, seed.principal_id)
            connection = await session.get(ChannelConnectionRow, seed.connection_id)
        assert binding is not None
        assert binding.deleted_at is None
        assert binding.status == "active"
        assert binding.agent_asset_id == seed.new_agent_id
        assert binding.agent_scope == "project"
        assert binding.revision == 3
        # Rebinding restores authority but not runtime connectivity. The first
        # authenticated inbound event below is the only unfreeze boundary.
        assert principal is not None and principal.status == "frozen"
        assert connection is not None and connection.status == "frozen"
        assert connection.metadata_json == {"group_binding_id": str(seed.binding_id)}

        inbound_at = completed_at + timedelta(seconds=1)
        async with factory() as session, session.begin():
            authority = await repository.resolve_or_create_guest(
                session,
                provider="lark",
                channel_instance_id=seed.channel_instance_id,
                external_group_refs=(seed.external_group_ref,),
                external_account_refs=(seed.external_account_ref,),
                now=inbound_at,
            )
        assert authority is not None
        assert authority["id"] == seed.connection_id
        assert authority["owner_user_id"] == str(seed.guest_user_id)
        assert authority["membership_version"] == 1

        async with factory() as session:
            principal = await session.get(ChannelExternalPrincipalRow, seed.principal_id)
            membership = await session.get(ProjectMembershipRow, seed.guest_membership_id)
            connection = await session.get(ChannelConnectionRow, seed.connection_id)
        assert principal is not None and principal.status == "active"
        assert principal.membership_id == seed.guest_membership_id
        assert membership is not None and membership.user_id == str(seed.guest_user_id)
        assert connection is not None and connection.status == "connected"
        assert connection.frozen_at is None
        assert connection.metadata_json == {
            "group_binding_id": str(seed.binding_id),
            "agent_asset_id": str(seed.new_agent_id),
            "agent_scope": "project",
        }
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_rebind_to_new_agent_drops_old_conversation_before_lazy_inbound(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresProjectChannelGroupBindingRepository()
    try:
        seed = await _seed_lifecycle(factory)
        old_thread_id, old_conversation_id = await _seed_old_conversation(
            factory,
            seed,
        )
        deleted_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            await repository.lock_project_context(session, seed.context, read=False)
            await repository.delete_binding(
                session,
                seed.context,
                binding_id=seed.binding_id,
                expected_revision=1,
                now=deleted_at,
            )

        async with factory() as session:
            assert await session.get(ChannelConversationRow, old_conversation_id) is None
            old_thread = await session.get(ThreadMetaRow, old_thread_id)
        assert old_thread is not None
        assert old_thread.agent_asset_id == seed.old_agent_id

        code_digest = uuid.uuid4().hex * 2
        challenged_at = deleted_at + timedelta(seconds=1)
        async with factory() as session, session.begin():
            session.add(
                ProjectChannelGroupBindingChallengeRow(
                    project_id=seed.context.project_id,
                    channel_instance_id=seed.channel_instance_id,
                    provider="lark",
                    code_digest=code_digest,
                    agent_asset_id=seed.new_agent_id,
                    agent_scope="project",
                    membership_id=seed.context.membership_id,
                    membership_version=seed.context.membership_version,
                    created_by_user_id=str(seed.context.user_id),
                    created_at=challenged_at,
                    expires_at=challenged_at + timedelta(minutes=10),
                )
            )
        async with factory() as session, session.begin():
            rebound = await repository.complete_challenge(
                session,
                provider="lark",
                channel_instance_id=seed.channel_instance_id,
                code_digest=code_digest,
                external_group_ref=seed.external_group_ref,
                external_group_refs=(seed.external_group_ref,),
                display_name="New Agent group conversation",
                now=challenged_at + timedelta(seconds=1),
            )
            assert rebound is not None and rebound.id == seed.binding_id

        inbound_at = challenged_at + timedelta(seconds=2)
        async with factory() as session, session.begin():
            authority = await repository.resolve_or_create_guest(
                session,
                provider="lark",
                channel_instance_id=seed.channel_instance_id,
                external_group_refs=(seed.external_group_ref,),
                external_account_refs=(seed.external_account_ref,),
                now=inbound_at,
            )
        assert authority is not None
        assert authority["id"] == seed.connection_id
        assert authority["metadata"] == {
            "group_binding_id": str(seed.binding_id),
            "agent_asset_id": str(seed.new_agent_id),
            "agent_scope": "project",
        }

        new_thread_id = uuid.uuid4().hex
        async with factory() as session, session.begin():
            session.add(
                ThreadMetaRow(
                    thread_id=new_thread_id,
                    assistant_id=None,
                    owner_user_id=str(seed.guest_user_id),
                    display_name="New Agent group conversation",
                    status="idle",
                    metadata_json={},
                    project_id=seed.context.project_id,
                    agent_asset_id=seed.new_agent_id,
                    agent_scope="project",
                    created_at=inbound_at,
                    updated_at=inbound_at,
                )
            )
        scope = PrivateResourceScope(
            project_id=str(seed.context.project_id),
            owner_user_id=str(seed.guest_user_id),
            membership_version=1,
        )
        assert await ChannelConnectionRepository(factory).set_thread_id(
            scope=scope,
            connection_id=seed.connection_id,
            provider="lark",
            external_conversation_id=seed.external_group_ref,
            external_topic_id=None,
            thread_id=new_thread_id,
        )

        async with factory() as session:
            conversation = await session.scalar(
                select(ChannelConversationRow).where(
                    ChannelConversationRow.connection_id == seed.connection_id,
                    ChannelConversationRow.external_conversation_id == seed.external_group_ref,
                    ChannelConversationRow.external_topic_id == "",
                )
            )
            old_thread = await session.get(ThreadMetaRow, old_thread_id)
        assert conversation is not None
        assert conversation.thread_id == new_thread_id
        assert old_thread is not None and old_thread.agent_asset_id == seed.old_agent_id
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_rebound_binding_does_not_reactivate_revoked_connection(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresProjectChannelGroupBindingRepository()
    try:
        seed = await _seed_lifecycle(factory)
        old_thread_id, old_conversation_id = await _seed_old_conversation(
            factory,
            seed,
        )
        deleted_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            connection = await session.get(ChannelConnectionRow, seed.connection_id)
            assert connection is not None
            connection.status = "revoked"
            await repository.lock_project_context(session, seed.context, read=False)
            await repository.delete_binding(
                session,
                seed.context,
                binding_id=seed.binding_id,
                expected_revision=1,
                now=deleted_at,
            )

        code_digest = uuid.uuid4().hex * 2
        challenged_at = deleted_at + timedelta(seconds=1)
        async with factory() as session, session.begin():
            session.add(
                ProjectChannelGroupBindingChallengeRow(
                    project_id=seed.context.project_id,
                    channel_instance_id=seed.channel_instance_id,
                    provider="lark",
                    code_digest=code_digest,
                    agent_asset_id=seed.new_agent_id,
                    agent_scope="project",
                    membership_id=seed.context.membership_id,
                    membership_version=seed.context.membership_version,
                    created_by_user_id=str(seed.context.user_id),
                    created_at=challenged_at,
                    expires_at=challenged_at + timedelta(minutes=10),
                )
            )
        async with factory() as session, session.begin():
            rebound = await repository.complete_challenge(
                session,
                provider="lark",
                channel_instance_id=seed.channel_instance_id,
                code_digest=code_digest,
                external_group_ref=seed.external_group_ref,
                external_group_refs=(seed.external_group_ref,),
                display_name="Revoked connection remains terminal",
                now=challenged_at + timedelta(seconds=1),
            )
            assert rebound is not None and rebound.id == seed.binding_id

        async with factory() as session, session.begin():
            authority = await repository.resolve_or_create_guest(
                session,
                provider="lark",
                channel_instance_id=seed.channel_instance_id,
                external_group_refs=(seed.external_group_ref,),
                external_account_refs=(seed.external_account_ref,),
                now=challenged_at + timedelta(seconds=2),
            )
        assert authority is None

        async with factory() as session:
            principal = await session.get(ChannelExternalPrincipalRow, seed.principal_id)
            connection = await session.get(ChannelConnectionRow, seed.connection_id)
            old_thread = await session.get(ThreadMetaRow, old_thread_id)
            old_conversation = await session.get(
                ChannelConversationRow,
                old_conversation_id,
            )
        assert principal is not None and principal.status == "frozen"
        assert connection is not None and connection.status == "revoked"
        assert connection.frozen_at is None
        assert connection.metadata_json == {
            "group_binding_id": str(seed.binding_id),
            "agent_asset_id": str(seed.old_agent_id),
            "agent_scope": "project",
        }
        assert old_thread is not None
        assert old_conversation is not None
        assert old_conversation.thread_id == old_thread_id
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_disabled_agent_change_drops_old_conversation_before_reenable(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresProjectChannelGroupBindingRepository()
    try:
        seed = await _seed_lifecycle(factory)
        old_thread_id, old_conversation_id = await _seed_old_conversation(
            factory,
            seed,
        )
        changed_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            await repository.lock_project_context(session, seed.context, read=False)
            changed = await repository.update_binding(
                session,
                seed.context,
                binding_id=seed.binding_id,
                expected_revision=1,
                enabled=False,
                agent_asset_id=seed.new_agent_id,
                agent_scope="project",
                now=changed_at,
            )
            assert changed.revision == 2

        async with factory() as session:
            connection = await session.get(ChannelConnectionRow, seed.connection_id)
            old_thread = await session.get(ThreadMetaRow, old_thread_id)
            old_conversation = await session.get(
                ChannelConversationRow,
                old_conversation_id,
            )
        assert connection is not None and connection.status == "frozen"
        assert connection.metadata_json == {
            "group_binding_id": str(seed.binding_id),
            "agent_asset_id": str(seed.new_agent_id),
            "agent_scope": "project",
        }
        assert old_thread is not None
        assert old_conversation is None

        async with factory() as session, session.begin():
            await repository.lock_project_context(session, seed.context, read=False)
            enabled = await repository.update_binding(
                session,
                seed.context,
                binding_id=seed.binding_id,
                expected_revision=2,
                enabled=True,
                agent_asset_id=None,
                agent_scope=None,
                now=changed_at + timedelta(seconds=1),
            )
            assert enabled.revision == 3

        async with factory() as session, session.begin():
            authority = await repository.resolve_or_create_guest(
                session,
                provider="lark",
                channel_instance_id=seed.channel_instance_id,
                external_group_refs=(seed.external_group_ref,),
                external_account_refs=(seed.external_account_ref,),
                now=changed_at + timedelta(seconds=2),
            )
        assert authority is not None
        assert authority["id"] == seed.connection_id
        assert authority["metadata"] == {
            "group_binding_id": str(seed.binding_id),
            "agent_asset_id": str(seed.new_agent_id),
            "agent_scope": "project",
        }
    finally:
        await engine.dispose()


@pytest.mark.parametrize("binding_lifecycle", ("disabled", "deleted"))
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_resume_never_restores_group_managed_connections(
    migrated_postgres_database_url: str,
    binding_lifecycle: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresProjectChannelGroupBindingRepository()
    try:
        seed = await _seed_lifecycle(factory)
        old_thread_id, _ = await _seed_old_conversation(factory, seed)
        ordinary_connection_id = await _seed_ordinary_same_owner_connection(
            factory,
            seed,
        )
        changed_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            await repository.lock_project_context(session, seed.context, read=False)
            if binding_lifecycle == "disabled":
                await repository.update_binding(
                    session,
                    seed.context,
                    binding_id=seed.binding_id,
                    expected_revision=1,
                    enabled=False,
                    agent_asset_id=None,
                    agent_scope=None,
                    now=changed_at,
                )
            else:
                await repository.delete_binding(
                    session,
                    seed.context,
                    binding_id=seed.binding_id,
                    expected_revision=1,
                    now=changed_at,
                )

        audit_context = resolve_system_audit_context(
            _SystemAdmin(uuid.uuid4()),
            request_id=f"binding-release-{binding_lifecycle}",
        )
        async with factory() as session, session.begin():
            lifecycle = SystemProjectLifecycleService(
                session,
                audit=_NoopSystemLifecycleAudit(),
            )
            suspended = await lifecycle.suspend(
                audit_context,
                seed.context.project_id,
                now=changed_at + timedelta(seconds=1),
            )
            assert suspended.is_suspended is True
        async with factory() as session, session.begin():
            lifecycle = SystemProjectLifecycleService(
                session,
                audit=_NoopSystemLifecycleAudit(),
            )
            resumed = await lifecycle.resume(
                audit_context,
                seed.context.project_id,
                now=changed_at + timedelta(seconds=2),
            )
            assert resumed.is_suspended is False

        async with factory() as session:
            binding = await session.get(ProjectChannelGroupBindingRow, seed.binding_id)
            principal = await session.get(ChannelExternalPrincipalRow, seed.principal_id)
            connection = await session.get(ChannelConnectionRow, seed.connection_id)
            ordinary_connection = await session.get(
                ChannelConnectionRow,
                ordinary_connection_id,
            )
            old_thread = await session.get(ThreadMetaRow, old_thread_id)
        assert binding is not None
        assert binding.status == "disabled"
        assert (binding.deleted_at is not None) is (binding_lifecycle == "deleted")
        assert principal is not None and principal.status == "frozen"
        assert connection is not None and connection.status == "frozen"
        assert connection.frozen_at is not None
        assert ordinary_connection is not None
        assert ordinary_connection.status == "connected"
        assert ordinary_connection.frozen_at is None
        # Resume may restore retained Thread content, but the group-managed
        # connection remains non-executable until a live binding resolves a
        # fresh authenticated inbound event.
        assert old_thread is not None and old_thread.frozen_at is None

        guest_context = PrivateWorkContext.from_project(
            ProjectContext(
                user_id=seed.guest_user_id,
                project_id=seed.context.project_id,
                membership_id=seed.guest_membership_id,
                role=ProjectRole.CHANNEL_GUEST,
                capabilities=capabilities_for(ProjectRole.CHANNEL_GUEST),
                membership_version=1,
                request_id=f"stale-inbound-{binding_lifecycle}",
            )
        )
        server_context = PrivateRunAdmissionServerContext(
            inbound_authority=PrivateRunInboundAuthority(
                connection_id=seed.connection_id,
                provider="lark",
                external_account_id=seed.external_account_ref,
                workspace_id=seed.external_group_ref,
                external_conversation_id=seed.external_group_ref,
                external_topic_id=None,
                channel_instance_id=str(seed.channel_instance_id),
            ),
            inbound_delivery=PrivateRunInboundDelivery(
                f"stale-delivery-{binding_lifecycle}",
            ),
        )
        async with factory() as session:
            with pytest.raises(PrivateWorkNotFound):
                await PrivateRunAdmissionService._require_inbound_authority(
                    session,
                    guest_context,
                    old_thread_id,
                    server_context,
                )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_complete_challenge_rejects_multiple_alias_tombstones(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresProjectChannelGroupBindingRepository()
    try:
        seed = await _seed_lifecycle(factory)
        aliases = (seed.external_group_ref, uuid.uuid4().hex * 2)
        now = datetime.now(UTC)
        async with factory() as session, session.begin():
            original = await session.get(ProjectChannelGroupBindingRow, seed.binding_id)
            assert original is not None
            original.status = "disabled"
            original.deleted_at = now
            original.agent_asset_id = None
            original.agent_scope = None
            session.add(
                ProjectChannelGroupBindingRow(
                    project_id=seed.context.project_id,
                    channel_instance_id=seed.channel_instance_id,
                    provider="lark",
                    external_group_ref=aliases[1],
                    external_group_name="Legacy alias tombstone",
                    agent_asset_id=None,
                    agent_scope=None,
                    status="disabled",
                    revision=1,
                    created_by_user_id=str(seed.context.user_id),
                    updated_by_user_id=str(seed.context.user_id),
                    deleted_at=now,
                )
            )
            session.add(
                ProjectChannelGroupBindingChallengeRow(
                    project_id=seed.context.project_id,
                    channel_instance_id=seed.channel_instance_id,
                    provider="lark",
                    code_digest=uuid.uuid4().hex * 2,
                    agent_asset_id=seed.new_agent_id,
                    agent_scope="project",
                    membership_id=seed.context.membership_id,
                    membership_version=seed.context.membership_version,
                    created_by_user_id=str(seed.context.user_id),
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                )
            )
        async with factory() as session:
            challenge = await session.scalar(
                select(ProjectChannelGroupBindingChallengeRow).where(
                    ProjectChannelGroupBindingChallengeRow.project_id == seed.context.project_id,
                    ProjectChannelGroupBindingChallengeRow.consumed_at.is_(None),
                )
            )
            assert challenge is not None
            challenge_id = challenge.id
            code_digest = challenge.code_digest

        async with factory() as session, session.begin():
            with pytest.raises(GroupBindingRepositoryConflict):
                await repository.complete_challenge(
                    session,
                    provider="lark",
                    channel_instance_id=seed.channel_instance_id,
                    code_digest=code_digest,
                    external_group_ref=aliases[0],
                    external_group_refs=aliases,
                    display_name="Ambiguous aliases",
                    now=now + timedelta(seconds=1),
                )

        async with factory() as session:
            challenge = await session.get(ProjectChannelGroupBindingChallengeRow, challenge_id)
            tombstones = tuple(
                (
                    await session.execute(
                        select(ProjectChannelGroupBindingRow).where(
                            ProjectChannelGroupBindingRow.channel_instance_id == seed.channel_instance_id,
                            ProjectChannelGroupBindingRow.external_group_ref.in_(aliases),
                            ProjectChannelGroupBindingRow.deleted_at.is_not(None),
                        )
                    )
                ).scalars()
            )
        assert challenge is not None and challenge.consumed_at is None
        assert len(tombstones) == 2
        assert all(row.agent_asset_id is None and row.agent_scope is None for row in tombstones)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_binding_delete_and_challenge_completion_do_not_deadlock(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresProjectChannelGroupBindingRepository()
    try:
        seed = await _seed_lifecycle(factory)
        now = datetime.now(UTC)
        code_digest = uuid.uuid4().hex * 2
        async with factory() as session, session.begin():
            session.add(
                ProjectChannelGroupBindingChallengeRow(
                    project_id=seed.context.project_id,
                    channel_instance_id=seed.channel_instance_id,
                    provider="lark",
                    code_digest=code_digest,
                    agent_asset_id=seed.new_agent_id,
                    agent_scope="project",
                    membership_id=seed.context.membership_id,
                    membership_version=seed.context.membership_version,
                    created_by_user_id=str(seed.context.user_id),
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                )
            )

        async def delete_binding() -> object:
            try:
                async with factory() as session, session.begin():
                    await session.execute(text("SET LOCAL lock_timeout = '4s'"))
                    await repository.lock_project_context(session, seed.context, read=False)
                    await repository.delete_binding(
                        session,
                        seed.context,
                        binding_id=seed.binding_id,
                        expected_revision=1,
                        now=now + timedelta(seconds=1),
                    )
                return "deleted"
            except GroupBindingRepositoryConflict as error:
                return error

        async def complete_challenge() -> object:
            async with factory() as session, session.begin():
                await session.execute(text("SET LOCAL lock_timeout = '4s'"))
                return await repository.complete_challenge(
                    session,
                    provider="lark",
                    channel_instance_id=seed.channel_instance_id,
                    code_digest=code_digest,
                    external_group_ref=seed.external_group_ref,
                    external_group_refs=(seed.external_group_ref,),
                    display_name="Concurrent lifecycle",
                    now=now + timedelta(seconds=2),
                )

        deleted, completed = await asyncio.wait_for(
            asyncio.gather(delete_binding(), complete_challenge()),
            timeout=8,
        )
        assert deleted == "deleted" or isinstance(deleted, GroupBindingRepositoryConflict)
        assert completed is not None and completed.id == seed.binding_id

        async with factory() as session:
            binding = await session.get(ProjectChannelGroupBindingRow, seed.binding_id)
            challenge = await session.scalar(
                select(ProjectChannelGroupBindingChallengeRow).where(
                    ProjectChannelGroupBindingChallengeRow.code_digest == code_digest,
                )
            )
        assert binding is not None
        assert challenge is not None and challenge.consumed_at is not None
        assert binding.deleted_at is None
        assert binding.agent_asset_id == seed.new_agent_id
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_binding_delete_and_agent_delete_serialize_without_deadlock(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresProjectChannelGroupBindingRepository()
    try:
        seed = await _seed_lifecycle(factory)
        now = datetime.now(UTC)

        async def delete_binding() -> object:
            async with factory() as session, session.begin():
                await session.execute(text("SET LOCAL lock_timeout = '4s'"))
                await repository.lock_project_context(session, seed.context, read=False)
                await repository.delete_binding(
                    session,
                    seed.context,
                    binding_id=seed.binding_id,
                    expected_revision=1,
                    now=now,
                )
            return "binding-deleted"

        async def delete_agent() -> object:
            try:
                await AgentService(factory).delete(
                    seed.context,
                    seed.old_agent_id,
                    expected_asset_version=1,
                )
                return "agent-deleted"
            except AssetConflict as error:
                return error

        binding_result, agent_result = await asyncio.wait_for(
            asyncio.gather(delete_binding(), delete_agent()),
            timeout=8,
        )
        assert binding_result == "binding-deleted"

        async with factory() as session:
            binding = await session.get(ProjectChannelGroupBindingRow, seed.binding_id)
            principal = await session.get(ChannelExternalPrincipalRow, seed.principal_id)
            connection = await session.get(ChannelConnectionRow, seed.connection_id)
            old_agent = await session.get(AgentRow, seed.old_agent_id)
        assert binding is not None and binding.deleted_at is not None
        assert binding.status == "disabled"
        assert binding.agent_asset_id is None and binding.agent_scope is None
        assert principal is not None and principal.status == "frozen"
        assert connection is not None and connection.status == "frozen"
        assert connection.metadata_json == {"group_binding_id": str(seed.binding_id)}
        if agent_result == "agent-deleted":
            assert old_agent is None
        else:
            assert isinstance(agent_result, AssetConflict)
            assert old_agent is not None
            await AgentService(factory).delete(
                seed.context,
                seed.old_agent_id,
                expected_asset_version=1,
            )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_two_challenges_serialize_one_tombstone_resurrection(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresProjectChannelGroupBindingRepository()
    try:
        seed = await _seed_lifecycle(factory)
        deleted_at = datetime.now(UTC)
        async with factory() as session, session.begin():
            await repository.lock_project_context(session, seed.context, read=False)
            await repository.delete_binding(
                session,
                seed.context,
                binding_id=seed.binding_id,
                expected_revision=1,
                now=deleted_at,
            )

        challenge_digests = (uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        async with factory() as session, session.begin():
            for digest in challenge_digests:
                session.add(
                    ProjectChannelGroupBindingChallengeRow(
                        project_id=seed.context.project_id,
                        channel_instance_id=seed.channel_instance_id,
                        provider="lark",
                        code_digest=digest,
                        agent_asset_id=seed.new_agent_id,
                        agent_scope="project",
                        membership_id=seed.context.membership_id,
                        membership_version=seed.context.membership_version,
                        created_by_user_id=str(seed.context.user_id),
                        created_at=deleted_at,
                        expires_at=deleted_at + timedelta(minutes=10),
                    )
                )

        async def complete(digest: str, suffix: str) -> uuid.UUID | None:
            async with factory() as session, session.begin():
                await session.execute(text("SET LOCAL lock_timeout = '4s'"))
                row = await repository.complete_challenge(
                    session,
                    provider="lark",
                    channel_instance_id=seed.channel_instance_id,
                    code_digest=digest,
                    external_group_ref=seed.external_group_ref,
                    external_group_refs=(seed.external_group_ref,),
                    display_name=f"Concurrent rebind {suffix}",
                    now=deleted_at + timedelta(seconds=1),
                )
                return None if row is None else row.id

        results = await asyncio.wait_for(
            asyncio.gather(
                complete(challenge_digests[0], "one"),
                complete(challenge_digests[1], "two"),
            ),
            timeout=8,
        )
        assert results == [seed.binding_id, seed.binding_id]

        async with factory() as session:
            live_bindings = tuple(
                (
                    await session.execute(
                        select(ProjectChannelGroupBindingRow).where(
                            ProjectChannelGroupBindingRow.channel_instance_id == seed.channel_instance_id,
                            ProjectChannelGroupBindingRow.external_group_ref == seed.external_group_ref,
                            ProjectChannelGroupBindingRow.deleted_at.is_(None),
                        )
                    )
                ).scalars()
            )
            challenges = tuple((await session.execute(select(ProjectChannelGroupBindingChallengeRow).where(ProjectChannelGroupBindingChallengeRow.code_digest.in_(challenge_digests)))).scalars())
            principals = tuple((await session.execute(select(ChannelExternalPrincipalRow).where(ChannelExternalPrincipalRow.group_binding_id == seed.binding_id))).scalars())
            connection = await session.get(ChannelConnectionRow, seed.connection_id)
        assert len(live_bindings) == 1
        assert live_bindings[0].id == seed.binding_id
        assert live_bindings[0].revision == 4
        assert live_bindings[0].agent_asset_id == seed.new_agent_id
        assert len(challenges) == 2 and all(row.consumed_at is not None for row in challenges)
        assert len(principals) == 1 and principals[0].id == seed.principal_id
        assert principals[0].status == "frozen"
        assert connection is not None and connection.status == "frozen"
        assert connection.metadata_json == {"group_binding_id": str(seed.binding_id)}
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_binding_delete_and_inbound_preserve_terminal_identity_state(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PostgresProjectChannelGroupBindingRepository()
    try:
        seed = await _seed_lifecycle(factory)
        now = datetime.now(UTC)

        async def delete_binding() -> None:
            async with factory() as session, session.begin():
                await session.execute(text("SET LOCAL lock_timeout = '4s'"))
                await repository.lock_project_context(session, seed.context, read=False)
                await repository.delete_binding(
                    session,
                    seed.context,
                    binding_id=seed.binding_id,
                    expected_revision=1,
                    now=now,
                )

        async def inbound() -> dict[str, object] | None:
            async with factory() as session, session.begin():
                await session.execute(text("SET LOCAL lock_timeout = '4s'"))
                return await repository.resolve_or_create_guest(
                    session,
                    provider="lark",
                    channel_instance_id=seed.channel_instance_id,
                    external_group_refs=(seed.external_group_ref,),
                    external_account_refs=(seed.external_account_ref,),
                    now=now + timedelta(seconds=1),
                )

        _, authority = await asyncio.wait_for(
            asyncio.gather(delete_binding(), inbound()),
            timeout=8,
        )

        async with factory() as session:
            binding = await session.get(ProjectChannelGroupBindingRow, seed.binding_id)
            principal = await session.get(ChannelExternalPrincipalRow, seed.principal_id)
            connection = await session.get(ChannelConnectionRow, seed.connection_id)
            memberships = tuple(
                (
                    await session.execute(
                        select(ProjectMembershipRow).where(
                            ProjectMembershipRow.project_id == seed.context.project_id,
                            ProjectMembershipRow.user_id == str(seed.guest_user_id),
                        )
                    )
                ).scalars()
            )
        assert binding is not None and binding.deleted_at is not None
        assert binding.status == "disabled"
        assert binding.agent_asset_id is None and binding.agent_scope is None
        assert principal is not None and principal.id == seed.principal_id
        assert principal.status == "frozen"
        assert principal.membership_id == seed.guest_membership_id
        assert len(memberships) == 1 and memberships[0].id == seed.guest_membership_id
        assert connection is not None and connection.id == seed.connection_id
        assert connection.status == "frozen"
        assert connection.frozen_at == now
        assert connection.metadata_json == {"group_binding_id": str(seed.binding_id)}
        if authority is not None:
            # Inbound may have completed first, but delete must be the terminal
            # writer and revoke its runtime authority before commit returns.
            assert authority["id"] == seed.connection_id
    finally:
        await engine.dispose()
