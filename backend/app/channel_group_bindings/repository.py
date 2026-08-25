from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, and_, cast, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.account_private_lifecycle import (
    AccountPrivateLifecycle,
    AccountPrivateLifecycleClosed,
    AccountPrivateLifecyclePort,
)
from app.projects.context import ProjectContext
from deerflow.persistence.channel_connections.group_challenge_model import (
    ProjectChannelGroupBindingChallengeRow,
)
from deerflow.persistence.channel_connections.identity_lock import (
    lock_channel_identities,
)
from deerflow.persistence.channel_connections.model import (
    ChannelConnectionRow,
    ChannelConversationRow,
    ProjectChannelInstanceRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionRow,
    ProjectSystemAgentBindingRow,
)

_MAX_BIGINT = 9_223_372_036_854_775_807


@dataclass(frozen=True, slots=True)
class _GuestResolutionCoordinates:
    project_id: uuid.UUID
    channel_instance_revision: int
    binding_id: uuid.UUID
    binding_revision: int
    external_group_ref: str
    agent_asset_id: uuid.UUID
    agent_scope: str
    principal_id: uuid.UUID | None
    principal_user_id: str | None
    membership_id: uuid.UUID | None
    external_account_ref: str | None
    principal_status: str | None
    membership_status: str | None
    membership_role: str | None
    membership_version: int | None
    membership_activation_generation: int | None
    connection_id: str | None
    connection_project_id: uuid.UUID | None
    connection_owner_user_id: str | None
    connection_instance_id: uuid.UUID | None
    connection_status: str | None

    @property
    def has_existing_principal(self) -> bool:
        return self.principal_id is not None


class GroupBindingRepositoryNotFound(Exception):
    pass


class GroupBindingRepositoryConflict(Exception):
    pass


class GroupBindingRepositoryAgentUnavailable(Exception):
    pass


def _group_models():
    # Kept behind one import boundary so unit tests for the application service
    # can use a typed fake while the consolidated schema package is imported.
    from deerflow.persistence.channel_connections.group_model import (
        ChannelExternalPrincipalRow,
        ProjectChannelGroupBindingRow,
    )

    return ProjectChannelGroupBindingRow, ChannelExternalPrincipalRow


class PostgresProjectChannelGroupBindingRepository:
    def __init__(
        self,
        *,
        account_private_lifecycle: AccountPrivateLifecyclePort | None = None,
    ) -> None:
        self._account_private_lifecycle = account_private_lifecycle or AccountPrivateLifecycle()

    @staticmethod
    def _group_connection_ids(principal_model, binding):
        return select(
            func.replace(cast(principal_model.id, String), "-", ""),
        ).where(principal_model.group_binding_id == binding.id)

    async def lock_project_context(
        self,
        session: AsyncSession,
        context: ProjectContext,
        *,
        read: bool,
    ) -> None:
        statement = (
            select(ProjectRow.id)
            .join(
                ProjectMembershipRow,
                ProjectMembershipRow.project_id == ProjectRow.id,
            )
            .where(
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.project_id == context.project_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
            )
            .with_for_update(read=read, of=[ProjectRow, ProjectMembershipRow])
        )
        if (await session.execute(statement)).scalar_one_or_none() is None:
            raise GroupBindingRepositoryNotFound

    async def get_runtime_instance(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        provider: str,
        for_update: bool,
    ) -> ProjectChannelInstanceRow | None:
        statement = select(ProjectChannelInstanceRow).where(
            ProjectChannelInstanceRow.project_id == project_id,
            ProjectChannelInstanceRow.provider == provider,
            ProjectChannelInstanceRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update(of=ProjectChannelInstanceRow)
        return (await session.execute(statement)).scalar_one_or_none()

    async def list_bindings(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
    ) -> tuple[object, ...]:
        ProjectChannelGroupBindingRow, ChannelExternalPrincipalRow = _group_models()
        rows = await session.execute(
            select(ProjectChannelGroupBindingRow)
            .where(
                ProjectChannelGroupBindingRow.project_id == project_id,
                ProjectChannelGroupBindingRow.deleted_at.is_(None),
            )
            .order_by(
                ProjectChannelGroupBindingRow.updated_at.desc(),
                ProjectChannelGroupBindingRow.id.desc(),
            )
        )
        return tuple(rows.scalars())

    async def create_challenge(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        channel_instance_id: uuid.UUID,
        provider: str,
        code_digest: str,
        agent_asset_id: uuid.UUID,
        agent_scope: str,
        membership_id: uuid.UUID,
        membership_version: int,
        created_by_user_id: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        await session.execute(
            delete(ProjectChannelGroupBindingChallengeRow).where(
                ProjectChannelGroupBindingChallengeRow.project_id == project_id,
                ProjectChannelGroupBindingChallengeRow.provider == provider,
                ProjectChannelGroupBindingChallengeRow.expires_at <= now,
            )
        )
        session.add(
            ProjectChannelGroupBindingChallengeRow(
                project_id=project_id,
                channel_instance_id=channel_instance_id,
                provider=provider,
                code_digest=code_digest,
                agent_asset_id=agent_asset_id,
                agent_scope=agent_scope,
                membership_id=membership_id,
                membership_version=membership_version,
                created_by_user_id=created_by_user_id,
                expires_at=expires_at,
                created_at=now,
            )
        )
        await session.flush()

    async def update_binding(
        self,
        session: AsyncSession,
        context: ProjectContext,
        *,
        binding_id: uuid.UUID,
        expected_revision: int,
        enabled: bool | None,
        agent_asset_id: uuid.UUID | None,
        agent_scope: str | None,
        now: datetime,
    ) -> object:
        ProjectChannelGroupBindingRow, ChannelExternalPrincipalRow = _group_models()
        row = (
            await session.execute(
                select(ProjectChannelGroupBindingRow)
                .where(
                    ProjectChannelGroupBindingRow.id == binding_id,
                    ProjectChannelGroupBindingRow.project_id == context.project_id,
                    ProjectChannelGroupBindingRow.deleted_at.is_(None),
                )
                .with_for_update(of=ProjectChannelGroupBindingRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise GroupBindingRepositoryNotFound
        if row.revision != expected_revision or row.revision >= _MAX_BIGINT:
            raise GroupBindingRepositoryConflict
        agent_changed = agent_asset_id is not None and agent_scope is not None and (row.agent_asset_id != agent_asset_id or row.agent_scope != agent_scope)
        if enabled is not None:
            row.status = "active" if enabled else "disabled"
        if agent_asset_id is not None and agent_scope is not None:
            row.agent_asset_id = agent_asset_id
            row.agent_scope = agent_scope
        row.revision += 1
        row.updated_by_user_id = str(context.user_id)
        row.updated_at = now
        if row.status == "disabled":
            await session.execute(
                update(ChannelExternalPrincipalRow)
                .where(
                    ChannelExternalPrincipalRow.group_binding_id == row.id,
                    ChannelExternalPrincipalRow.status == "active",
                )
                .values(status="frozen", updated_at=now)
            )
            await self._set_group_connections_status(
                session,
                ChannelExternalPrincipalRow,
                row,
                status="frozen",
                now=now,
            )
            if agent_changed:
                # Agent routing changes invalidate conversation-to-Thread
                # affinity even while the binding remains disabled. Connection
                # activation still waits for a later authenticated inbound.
                await self._update_group_connection_agents(
                    session,
                    ChannelExternalPrincipalRow,
                    row,
                    now=now,
                )
        elif agent_changed:
            await self._update_group_connection_agents(
                session,
                ChannelExternalPrincipalRow,
                row,
                now=now,
            )
        await session.flush()
        return row

    async def delete_binding(
        self,
        session: AsyncSession,
        context: ProjectContext,
        *,
        binding_id: uuid.UUID,
        expected_revision: int,
        now: datetime,
    ) -> None:
        ProjectChannelGroupBindingRow, ChannelExternalPrincipalRow = _group_models()
        row = (
            await session.execute(
                select(ProjectChannelGroupBindingRow)
                .where(
                    ProjectChannelGroupBindingRow.id == binding_id,
                    ProjectChannelGroupBindingRow.project_id == context.project_id,
                    ProjectChannelGroupBindingRow.deleted_at.is_(None),
                )
                .with_for_update(of=ProjectChannelGroupBindingRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise GroupBindingRepositoryNotFound
        if row.revision != expected_revision or row.revision >= _MAX_BIGINT:
            raise GroupBindingRepositoryConflict
        # Freeze every retained identity before releasing the Agent pair. The
        # lifecycle constraint makes this order significant: once deleted_at
        # is visible, both Agent columns must already be NULL.
        await session.execute(
            update(ChannelExternalPrincipalRow)
            .where(
                ChannelExternalPrincipalRow.group_binding_id == row.id,
                ChannelExternalPrincipalRow.status == "active",
            )
            .values(status="frozen", updated_at=now)
        )
        await self._set_group_connections_status(
            session,
            ChannelExternalPrincipalRow,
            row,
            status="frozen",
            now=now,
            clear_agent_reference=True,
        )
        await self._delete_group_conversations(
            session,
            ChannelExternalPrincipalRow,
            row,
        )
        row.status = "disabled"
        row.deleted_at = now
        row.agent_asset_id = None
        row.agent_scope = None
        row.updated_at = now
        row.updated_by_user_id = str(context.user_id)
        row.revision += 1
        await session.flush()

    @staticmethod
    async def _set_group_connections_status(
        session: AsyncSession,
        principal_model,
        binding,
        *,
        status: str,
        now: datetime,
        clear_agent_reference: bool = False,
    ) -> None:
        connection_ids = PostgresProjectChannelGroupBindingRepository._group_connection_ids(
            principal_model,
            binding,
        )
        values: dict[str, object] = {
            "status": status,
            "frozen_at": now if status == "frozen" else None,
            "updated_at": now,
        }
        if clear_agent_reference:
            values["metadata_json"] = {"group_binding_id": str(binding.id)}
        await session.execute(
            update(ChannelConnectionRow)
            .where(
                ChannelConnectionRow.project_id == binding.project_id,
                ChannelConnectionRow.channel_instance_id == binding.channel_instance_id,
                ChannelConnectionRow.id.in_(connection_ids),
                ChannelConnectionRow.status != "revoked",
            )
            .values(**values)
        )

    @staticmethod
    async def _delete_group_conversations(
        session: AsyncSession,
        principal_model,
        binding,
    ) -> None:
        owners = select(principal_model.principal_user_id).where(principal_model.group_binding_id == binding.id)
        principal_connection_ids = PostgresProjectChannelGroupBindingRepository._group_connection_ids(
            principal_model,
            binding,
        )
        connection_ids = select(ChannelConnectionRow.id).where(
            ChannelConnectionRow.project_id == binding.project_id,
            ChannelConnectionRow.channel_instance_id == binding.channel_instance_id,
            ChannelConnectionRow.id.in_(principal_connection_ids),
            ChannelConnectionRow.status != "revoked",
        )
        await session.execute(
            delete(ChannelConversationRow).where(
                ChannelConversationRow.project_id == binding.project_id,
                ChannelConversationRow.owner_user_id.in_(owners),
                ChannelConversationRow.connection_id.in_(connection_ids),
            )
        )

    @classmethod
    async def _update_group_connection_agents(
        cls,
        session: AsyncSession,
        principal_model,
        binding,
        *,
        now: datetime,
    ) -> None:
        connection_ids = cls._group_connection_ids(principal_model, binding)
        await cls._delete_group_conversations(
            session,
            principal_model,
            binding,
        )
        await session.execute(
            update(ChannelConnectionRow)
            .where(
                ChannelConnectionRow.project_id == binding.project_id,
                ChannelConnectionRow.channel_instance_id == binding.channel_instance_id,
                ChannelConnectionRow.id.in_(connection_ids),
                ChannelConnectionRow.status != "revoked",
            )
            .values(
                metadata_json={
                    "group_binding_id": str(binding.id),
                    "agent_asset_id": str(binding.agent_asset_id),
                    "agent_scope": binding.agent_scope,
                },
                updated_at=now,
            )
        )

    async def complete_challenge(
        self,
        session: AsyncSession,
        *,
        provider: str,
        channel_instance_id: uuid.UUID,
        code_digest: str,
        external_group_ref: str,
        external_group_refs: tuple[str, ...],
        display_name: str,
        now: datetime,
    ) -> object | None:
        ProjectChannelGroupBindingRow, ChannelExternalPrincipalRow = _group_models()
        # Discover only immutable authority keys first. Locks are then acquired
        # in the repository-wide parent-before-child order: Project,
        # Membership, Instance, Agent, and finally the challenge row. The final
        # challenge query repeats every discovered key so a concurrent consume
        # or out-of-band mutation fails closed.
        challenge_snapshot = (
            await session.execute(
                select(
                    ProjectChannelGroupBindingChallengeRow.id,
                    ProjectChannelGroupBindingChallengeRow.project_id,
                    ProjectChannelGroupBindingChallengeRow.membership_id,
                    ProjectChannelGroupBindingChallengeRow.membership_version,
                    ProjectChannelGroupBindingChallengeRow.created_by_user_id,
                    ProjectChannelGroupBindingChallengeRow.agent_asset_id,
                    ProjectChannelGroupBindingChallengeRow.agent_scope,
                ).where(
                    ProjectChannelGroupBindingChallengeRow.code_digest == code_digest,
                    ProjectChannelGroupBindingChallengeRow.provider == provider,
                    ProjectChannelGroupBindingChallengeRow.channel_instance_id == channel_instance_id,
                    ProjectChannelGroupBindingChallengeRow.consumed_at.is_(None),
                    ProjectChannelGroupBindingChallengeRow.expires_at > now,
                )
            )
        ).one_or_none()
        if challenge_snapshot is None:
            return None

        locked_project_id = (
            await session.execute(
                select(ProjectRow.id)
                .where(
                    ProjectRow.id == challenge_snapshot.project_id,
                    ProjectRow.status == "active",
                    ProjectRow.is_suspended.is_(False),
                )
                .with_for_update(read=True, of=ProjectRow)
            )
        ).scalar_one_or_none()
        if locked_project_id is None:
            return None

        locked_membership_id = (
            await session.execute(
                select(ProjectMembershipRow.id)
                .where(
                    ProjectMembershipRow.id == challenge_snapshot.membership_id,
                    ProjectMembershipRow.project_id == locked_project_id,
                    ProjectMembershipRow.user_id == challenge_snapshot.created_by_user_id,
                    ProjectMembershipRow.role == "admin",
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.version == challenge_snapshot.membership_version,
                )
                .with_for_update(read=True, of=ProjectMembershipRow)
            )
        ).scalar_one_or_none()
        if locked_membership_id is None:
            return None

        locked_instance_id = (
            await session.execute(
                select(ProjectChannelInstanceRow.id)
                .where(
                    ProjectChannelInstanceRow.project_id == locked_project_id,
                    ProjectChannelInstanceRow.id == channel_instance_id,
                    ProjectChannelInstanceRow.provider == provider,
                    ProjectChannelInstanceRow.desired_status == "enabled",
                    ProjectChannelInstanceRow.observed_status == "running",
                    ProjectChannelInstanceRow.deleted_at.is_(None),
                )
                # Binding completion is rare and must serialize distinct
                # challenges for the same instance before either inspects the
                # shared live/tombstone identity set.
                .with_for_update(of=ProjectChannelInstanceRow)
            )
        ).scalar_one_or_none()
        if locked_instance_id is None or not await self._agent_is_available(
            session,
            project_id=locked_project_id,
            agent_asset_id=challenge_snapshot.agent_asset_id,
            agent_scope=challenge_snapshot.agent_scope,
        ):
            return None

        challenge = (
            await session.execute(
                select(ProjectChannelGroupBindingChallengeRow)
                .where(
                    ProjectChannelGroupBindingChallengeRow.id == challenge_snapshot.id,
                    ProjectChannelGroupBindingChallengeRow.project_id == locked_project_id,
                    ProjectChannelGroupBindingChallengeRow.membership_id == locked_membership_id,
                    ProjectChannelGroupBindingChallengeRow.membership_version == challenge_snapshot.membership_version,
                    ProjectChannelGroupBindingChallengeRow.created_by_user_id == challenge_snapshot.created_by_user_id,
                    ProjectChannelGroupBindingChallengeRow.agent_asset_id == challenge_snapshot.agent_asset_id,
                    ProjectChannelGroupBindingChallengeRow.agent_scope == challenge_snapshot.agent_scope,
                    ProjectChannelGroupBindingChallengeRow.code_digest == code_digest,
                    ProjectChannelGroupBindingChallengeRow.provider == provider,
                    ProjectChannelGroupBindingChallengeRow.channel_instance_id == locked_instance_id,
                    ProjectChannelGroupBindingChallengeRow.consumed_at.is_(None),
                    ProjectChannelGroupBindingChallengeRow.expires_at > now,
                )
                .with_for_update(of=ProjectChannelGroupBindingChallengeRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if challenge is None:
            return None

        live_rows = tuple(
            (
                await session.execute(
                    select(ProjectChannelGroupBindingRow)
                    .where(
                        ProjectChannelGroupBindingRow.project_id == challenge.project_id,
                        ProjectChannelGroupBindingRow.channel_instance_id == channel_instance_id,
                        ProjectChannelGroupBindingRow.provider == provider,
                        ProjectChannelGroupBindingRow.external_group_ref.in_(external_group_refs),
                        ProjectChannelGroupBindingRow.deleted_at.is_(None),
                    )
                    .order_by(ProjectChannelGroupBindingRow.id)
                    .with_for_update(of=ProjectChannelGroupBindingRow)
                )
            ).scalars()
        )
        if len(live_rows) > 1:
            raise GroupBindingRepositoryConflict
        row = live_rows[0] if live_rows else None
        resurrected = False
        if row is None:
            tombstones = tuple(
                (
                    await session.execute(
                        select(ProjectChannelGroupBindingRow)
                        .where(
                            ProjectChannelGroupBindingRow.project_id == challenge.project_id,
                            ProjectChannelGroupBindingRow.channel_instance_id == channel_instance_id,
                            ProjectChannelGroupBindingRow.provider == provider,
                            ProjectChannelGroupBindingRow.external_group_ref.in_(external_group_refs),
                            ProjectChannelGroupBindingRow.deleted_at.is_not(None),
                        )
                        .order_by(ProjectChannelGroupBindingRow.id)
                        .with_for_update(of=ProjectChannelGroupBindingRow)
                    )
                ).scalars()
            )
            if len(tombstones) > 1:
                raise GroupBindingRepositoryConflict
            if tombstones:
                row = tombstones[0]
                if row.revision >= _MAX_BIGINT:
                    raise GroupBindingRepositoryConflict
                row.external_group_ref = external_group_ref
                row.external_group_name = display_name
                row.agent_asset_id = challenge.agent_asset_id
                row.agent_scope = challenge.agent_scope
                row.status = "active"
                row.deleted_at = None
                row.revision += 1
                row.updated_by_user_id = challenge.created_by_user_id
                row.updated_at = now
                resurrected = True
                # A tombstone can predate the v13 cleanup, or be restored from
                # an older backup. Never let its retained identity route a new
                # Agent into a Thread created for the previous Agent.
                await self._delete_group_conversations(
                    session,
                    ChannelExternalPrincipalRow,
                    row,
                )
        if row is None:
            row = ProjectChannelGroupBindingRow(
                project_id=challenge.project_id,
                channel_instance_id=channel_instance_id,
                provider=provider,
                external_group_ref=external_group_ref,
                external_group_name=display_name,
                agent_asset_id=challenge.agent_asset_id,
                agent_scope=challenge.agent_scope,
                status="active",
                revision=1,
                created_by_user_id=challenge.created_by_user_id,
                updated_by_user_id=challenge.created_by_user_id,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        elif not resurrected:
            if row.project_id != challenge.project_id or row.revision >= _MAX_BIGINT:
                raise GroupBindingRepositoryConflict
            agent_changed = row.agent_asset_id != challenge.agent_asset_id or row.agent_scope != challenge.agent_scope
            row.external_group_name = display_name
            row.agent_asset_id = challenge.agent_asset_id
            row.agent_scope = challenge.agent_scope
            row.status = "active"
            row.revision += 1
            row.updated_by_user_id = challenge.created_by_user_id
            row.updated_at = now
            if agent_changed:
                await self._update_group_connection_agents(
                    session,
                    ChannelExternalPrincipalRow,
                    row,
                    now=now,
                )
        challenge.consumed_at = now
        await session.flush()
        return row

    @staticmethod
    async def _agent_is_available(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        agent_asset_id: uuid.UUID,
        agent_scope: str,
    ) -> bool:
        statement = select(AgentRow.id).join(
            AgentVersionRow,
            AgentVersionRow.id == AgentRow.current_version_id,
        )
        if agent_scope == "project":
            statement = statement.where(
                AgentRow.id == agent_asset_id,
                AgentRow.scope == "project",
                AgentRow.project_id == project_id,
                AgentRow.status == "active",
            ).with_for_update(read=True, of=[AgentRow, AgentVersionRow])
        elif agent_scope == "system":
            statement = (
                statement.join(
                    ProjectSystemAgentBindingRow,
                    (ProjectSystemAgentBindingRow.system_agent_id == AgentRow.id) & (ProjectSystemAgentBindingRow.project_id == project_id),
                )
                .where(
                    AgentRow.id == agent_asset_id,
                    AgentRow.scope == "system",
                    AgentRow.project_id.is_(None),
                    AgentRow.status == "active",
                    ProjectSystemAgentBindingRow.enabled.is_(True),
                    AgentVersionRow.version_number == 1,
                )
                .with_for_update(
                    read=True,
                    of=[AgentRow, AgentVersionRow, ProjectSystemAgentBindingRow],
                )
            )
        else:
            return False
        return (await session.execute(statement)).scalar_one_or_none() is not None

    async def resolve_or_create_guest(
        self,
        session: AsyncSession,
        *,
        provider: str,
        channel_instance_id: uuid.UUID,
        external_group_refs: tuple[str, ...],
        external_account_refs: tuple[str, ...],
        now: datetime,
    ) -> dict[str, object] | None:
        ProjectChannelGroupBindingRow, ChannelExternalPrincipalRow = _group_models()
        coordinates = await self._discover_guest_coordinates(
            session,
            ProjectChannelGroupBindingRow,
            ChannelExternalPrincipalRow,
            provider=provider,
            channel_instance_id=channel_instance_id,
            external_group_refs=external_group_refs,
            external_account_refs=external_account_refs,
        )
        if coordinates is None:
            return None
        if coordinates.connection_status == "revoked":
            # Explicit disconnect is terminal. An unlocked negative answer is
            # safe; a concurrent reconnect can be observed by a later inbound.
            return None
        locked_project_id = (
            await session.execute(
                select(ProjectRow.id)
                .where(
                    ProjectRow.id == coordinates.project_id,
                    ProjectRow.status == "active",
                    ProjectRow.is_suspended.is_(False),
                )
                .with_for_update(read=True, of=ProjectRow)
            )
        ).scalar_one_or_none()
        if locked_project_id is None:
            raise GroupBindingRepositoryConflict

        membership = None
        if coordinates.has_existing_principal:
            membership = await self._lock_existing_guest_membership(
                session,
                coordinates,
            )
            if membership is None:
                raise GroupBindingRepositoryConflict
            try:
                await self._account_private_lifecycle.reactivate_after_membership(
                    session,
                    coordinates.principal_user_id,
                )
            except AccountPrivateLifecycleClosed:
                raise GroupBindingRepositoryConflict from None

        locked_instance_id = (
            await session.execute(
                select(ProjectChannelInstanceRow.id)
                .where(
                    ProjectChannelInstanceRow.project_id == locked_project_id,
                    ProjectChannelInstanceRow.id == channel_instance_id,
                    ProjectChannelInstanceRow.provider == provider,
                    ProjectChannelInstanceRow.revision == coordinates.channel_instance_revision,
                    ProjectChannelInstanceRow.desired_status == "enabled",
                    ProjectChannelInstanceRow.observed_status == "running",
                    ProjectChannelInstanceRow.deleted_at.is_(None),
                )
                .with_for_update(read=True, of=ProjectChannelInstanceRow)
            )
        ).scalar_one_or_none()
        if locked_instance_id is None:
            raise GroupBindingRepositoryConflict
        binding = (
            await session.execute(
                select(ProjectChannelGroupBindingRow)
                .where(
                    ProjectChannelGroupBindingRow.id == coordinates.binding_id,
                    ProjectChannelGroupBindingRow.project_id == locked_project_id,
                    ProjectChannelGroupBindingRow.channel_instance_id == locked_instance_id,
                    ProjectChannelGroupBindingRow.provider == provider,
                    ProjectChannelGroupBindingRow.external_group_ref == coordinates.external_group_ref,
                    ProjectChannelGroupBindingRow.revision == coordinates.binding_revision,
                    ProjectChannelGroupBindingRow.agent_asset_id == coordinates.agent_asset_id,
                    ProjectChannelGroupBindingRow.agent_scope == coordinates.agent_scope,
                    ProjectChannelGroupBindingRow.status == "active",
                    ProjectChannelGroupBindingRow.deleted_at.is_(None),
                )
                .with_for_update(of=ProjectChannelGroupBindingRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if binding is None:
            raise GroupBindingRepositoryConflict
        if not await self._agent_is_available(
            session,
            project_id=binding.project_id,
            agent_asset_id=binding.agent_asset_id,
            agent_scope=binding.agent_scope,
        ):
            raise GroupBindingRepositoryAgentUnavailable
        await lock_channel_identities(
            session,
            tuple((provider, account_ref, group_ref) for account_ref in external_account_refs for group_ref in external_group_refs),
        )

        if coordinates.has_existing_principal:
            principal = await self._lock_existing_guest_principal(
                session,
                ChannelExternalPrincipalRow,
                coordinates,
            )
            if principal is None:
                raise GroupBindingRepositoryConflict
        else:
            unexpected_principal = await self._get_active_principal(
                session,
                ChannelExternalPrincipalRow,
                binding.id,
                external_account_refs,
                for_update=True,
            )
            if unexpected_principal is not None:
                # The unlocked discovery saw no principal. Reusing one that
                # appeared after the Project lock would skip its mandatory
                # Membership -> User lifecycle prefix, so retry from discovery.
                raise GroupBindingRepositoryConflict
            principal, membership = await self._create_guest_principal(
                session,
                ChannelExternalPrincipalRow,
                binding,
                external_account_refs[0],
                now,
            )

        if membership is None:
            raise GroupBindingRepositoryConflict
        connection = await self._ensure_guest_connection(
            session,
            binding=binding,
            principal=principal,
            external_account_ref=principal.external_account_ref,
            external_group_ref=binding.external_group_ref,
            expected_connection_id=coordinates.connection_id,
            expected_status=coordinates.connection_status,
            now=now,
        )
        principal.status = "active"
        principal.last_seen_at = now
        principal.updated_at = now
        binding.last_activity_at = now
        if binding.first_activity_at is None:
            binding.first_activity_at = now
        binding.updated_at = now
        await session.flush()
        return self._runtime_connection_mapping(
            connection,
            membership_version=membership.version,
        )

    @staticmethod
    async def _discover_guest_coordinates(
        session: AsyncSession,
        binding_model,
        principal_model,
        *,
        provider: str,
        channel_instance_id: uuid.UUID,
        external_group_refs: tuple[str, ...],
        external_account_refs: tuple[str, ...],
    ) -> _GuestResolutionCoordinates | None:
        rows = (
            await session.execute(
                select(
                    binding_model.project_id.label("project_id"),
                    ProjectChannelInstanceRow.revision.label(
                        "channel_instance_revision",
                    ),
                    binding_model.id.label("binding_id"),
                    binding_model.revision.label("binding_revision"),
                    binding_model.external_group_ref.label("external_group_ref"),
                    binding_model.agent_asset_id.label("agent_asset_id"),
                    binding_model.agent_scope.label("agent_scope"),
                    principal_model.id.label("principal_id"),
                    principal_model.principal_user_id.label("principal_user_id"),
                    principal_model.membership_id.label("membership_id"),
                    principal_model.external_account_ref.label(
                        "external_account_ref",
                    ),
                    principal_model.status.label("principal_status"),
                    ProjectMembershipRow.status.label("membership_status"),
                    ProjectMembershipRow.role.label("membership_role"),
                    ProjectMembershipRow.version.label("membership_version"),
                    ProjectMembershipRow.activation_generation.label(
                        "membership_activation_generation",
                    ),
                    ChannelConnectionRow.id.label("connection_id"),
                    ChannelConnectionRow.project_id.label(
                        "connection_project_id",
                    ),
                    ChannelConnectionRow.owner_user_id.label(
                        "connection_owner_user_id",
                    ),
                    ChannelConnectionRow.channel_instance_id.label(
                        "connection_instance_id",
                    ),
                    ChannelConnectionRow.status.label("connection_status"),
                )
                .join(
                    ProjectChannelInstanceRow,
                    and_(
                        ProjectChannelInstanceRow.project_id == binding_model.project_id,
                        ProjectChannelInstanceRow.id == binding_model.channel_instance_id,
                    ),
                )
                .outerjoin(
                    principal_model,
                    and_(
                        principal_model.project_id == binding_model.project_id,
                        principal_model.group_binding_id == binding_model.id,
                        principal_model.external_account_ref.in_(
                            external_account_refs,
                        ),
                    ),
                )
                .outerjoin(
                    ProjectMembershipRow,
                    and_(
                        ProjectMembershipRow.id == principal_model.membership_id,
                        ProjectMembershipRow.project_id == principal_model.project_id,
                        ProjectMembershipRow.user_id == principal_model.principal_user_id,
                    ),
                )
                .outerjoin(
                    ChannelConnectionRow,
                    and_(
                        ChannelConnectionRow.id == func.replace(cast(principal_model.id, String), "-", ""),
                        ChannelConnectionRow.project_id == principal_model.project_id,
                        ChannelConnectionRow.owner_user_id == principal_model.principal_user_id,
                        ChannelConnectionRow.channel_instance_id == binding_model.channel_instance_id,
                    ),
                )
                .where(
                    binding_model.channel_instance_id == channel_instance_id,
                    binding_model.provider == provider,
                    binding_model.external_group_ref.in_(external_group_refs),
                    binding_model.status == "active",
                    binding_model.deleted_at.is_(None),
                    ProjectChannelInstanceRow.provider == provider,
                    ProjectChannelInstanceRow.desired_status == "enabled",
                    ProjectChannelInstanceRow.observed_status == "running",
                    ProjectChannelInstanceRow.deleted_at.is_(None),
                )
            )
        ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise GroupBindingRepositoryConflict
        row = rows[0]
        binding_values = (
            row.project_id,
            row.channel_instance_revision,
            row.binding_id,
            row.binding_revision,
            row.external_group_ref,
            row.agent_asset_id,
            row.agent_scope,
        )
        if not (
            isinstance(binding_values[0], uuid.UUID)
            and type(binding_values[1]) is int
            and binding_values[1] >= 1
            and isinstance(binding_values[2], uuid.UUID)
            and type(binding_values[3]) is int
            and binding_values[3] >= 1
            and type(binding_values[4]) is str
            and binding_values[4] in external_group_refs
            and isinstance(binding_values[5], uuid.UUID)
            and binding_values[6] in {"project", "system"}
        ):
            raise GroupBindingRepositoryConflict

        principal_values = (
            row.principal_id,
            row.principal_user_id,
            row.membership_id,
            row.external_account_ref,
            row.principal_status,
            row.membership_status,
            row.membership_role,
            row.membership_version,
            row.membership_activation_generation,
        )
        connection_values = (
            row.connection_id,
            row.connection_project_id,
            row.connection_owner_user_id,
            row.connection_instance_id,
            row.connection_status,
        )
        if all(value is None for value in principal_values):
            if any(value is not None for value in connection_values):
                raise GroupBindingRepositoryConflict
            return _GuestResolutionCoordinates(
                project_id=row.project_id,
                channel_instance_revision=row.channel_instance_revision,
                binding_id=row.binding_id,
                binding_revision=row.binding_revision,
                external_group_ref=row.external_group_ref,
                agent_asset_id=row.agent_asset_id,
                agent_scope=row.agent_scope,
                principal_id=None,
                principal_user_id=None,
                membership_id=None,
                external_account_ref=None,
                principal_status=None,
                membership_status=None,
                membership_role=None,
                membership_version=None,
                membership_activation_generation=None,
                connection_id=None,
                connection_project_id=None,
                connection_owner_user_id=None,
                connection_instance_id=None,
                connection_status=None,
            )
        if not (
            isinstance(row.principal_id, uuid.UUID)
            and type(row.principal_user_id) is str
            and isinstance(row.membership_id, uuid.UUID)
            and type(row.external_account_ref) is str
            and row.external_account_ref in external_account_refs
            and row.principal_status in {"active", "frozen"}
            and row.membership_status == "active"
            and row.membership_role == "channel_guest"
            and type(row.membership_version) is int
            and row.membership_version >= 1
            and type(row.membership_activation_generation) is int
            and row.membership_activation_generation >= 1
        ):
            raise GroupBindingRepositoryConflict
        if not (
            all(value is None for value in connection_values)
            or (
                type(row.connection_id) is str
                and row.connection_id == row.principal_id.hex
                and row.connection_project_id == row.project_id
                and row.connection_owner_user_id == row.principal_user_id
                and row.connection_instance_id == channel_instance_id
                and row.connection_status in {"connected", "frozen", "revoked"}
            )
        ):
            raise GroupBindingRepositoryConflict
        return _GuestResolutionCoordinates(
            project_id=row.project_id,
            channel_instance_revision=row.channel_instance_revision,
            binding_id=row.binding_id,
            binding_revision=row.binding_revision,
            external_group_ref=row.external_group_ref,
            agent_asset_id=row.agent_asset_id,
            agent_scope=row.agent_scope,
            principal_id=row.principal_id,
            principal_user_id=row.principal_user_id,
            membership_id=row.membership_id,
            external_account_ref=row.external_account_ref,
            principal_status=row.principal_status,
            membership_status=row.membership_status,
            membership_role=row.membership_role,
            membership_version=row.membership_version,
            membership_activation_generation=row.membership_activation_generation,
            connection_id=row.connection_id,
            connection_project_id=row.connection_project_id,
            connection_owner_user_id=row.connection_owner_user_id,
            connection_instance_id=row.connection_instance_id,
            connection_status=row.connection_status,
        )

    @staticmethod
    async def _lock_existing_guest_membership(
        session: AsyncSession,
        coordinates: _GuestResolutionCoordinates,
    ):
        if not coordinates.has_existing_principal:
            return None
        return (
            await session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.id == coordinates.membership_id,
                    ProjectMembershipRow.project_id == coordinates.project_id,
                    ProjectMembershipRow.user_id == coordinates.principal_user_id,
                    ProjectMembershipRow.role == coordinates.membership_role,
                    ProjectMembershipRow.status == coordinates.membership_status,
                    ProjectMembershipRow.version == coordinates.membership_version,
                    ProjectMembershipRow.activation_generation == coordinates.membership_activation_generation,
                )
                .with_for_update(read=True, of=ProjectMembershipRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _lock_existing_guest_principal(
        session: AsyncSession,
        model,
        coordinates: _GuestResolutionCoordinates,
    ):
        if not coordinates.has_existing_principal:
            return None
        return (
            await session.execute(
                select(model)
                .where(
                    model.id == coordinates.principal_id,
                    model.project_id == coordinates.project_id,
                    model.group_binding_id == coordinates.binding_id,
                    model.external_account_ref == coordinates.external_account_ref,
                    model.principal_user_id == coordinates.principal_user_id,
                    model.membership_id == coordinates.membership_id,
                    model.principal_type == "channel_guest",
                    model.membership_role == coordinates.membership_role,
                    model.status == coordinates.principal_status,
                )
                .with_for_update(of=model)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _ensure_guest_connection(
        session: AsyncSession,
        *,
        binding,
        principal,
        external_account_ref: str,
        external_group_ref: str,
        expected_connection_id: str | None,
        expected_status: str | None,
        now: datetime,
    ) -> ChannelConnectionRow:
        connection_id = principal.id.hex
        row = (
            await session.execute(
                select(ChannelConnectionRow)
                .where(
                    ChannelConnectionRow.id == connection_id,
                    ChannelConnectionRow.project_id == binding.project_id,
                    ChannelConnectionRow.owner_user_id == principal.principal_user_id,
                    ChannelConnectionRow.channel_instance_id == binding.channel_instance_id,
                )
                .with_for_update(of=ChannelConnectionRow)
            )
        ).scalar_one_or_none()
        metadata = {
            "group_binding_id": str(binding.id),
            "agent_asset_id": str(binding.agent_asset_id),
            "agent_scope": binding.agent_scope,
        }
        if expected_connection_id is None:
            if row is not None:
                raise GroupBindingRepositoryConflict
            row = ChannelConnectionRow(
                id=connection_id,
                project_id=binding.project_id,
                owner_user_id=principal.principal_user_id,
                provider=binding.provider,
                channel_instance_id=binding.channel_instance_id,
                status="connected",
                external_account_id=external_account_ref,
                workspace_id=external_group_ref,
                scopes_json=[],
                capabilities_json={},
                metadata_json=metadata,
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
            session.add(row)
        else:
            if expected_connection_id != connection_id or expected_status not in {"connected", "frozen"} or row is None or row.status != expected_status:
                raise GroupBindingRepositoryConflict
            row.status = "connected"
            row.external_account_id = external_account_ref
            row.workspace_id = external_group_ref
            row.metadata_json = metadata
            row.frozen_at = None
            row.last_seen_at = now
            row.updated_at = now
        return row

    @staticmethod
    def _runtime_connection_mapping(
        row: ChannelConnectionRow,
        *,
        membership_version: int,
    ) -> dict[str, object]:
        return {
            "id": row.id,
            "account_id": row.owner_user_id,
            "project_id": str(row.project_id),
            "owner_user_id": row.owner_user_id,
            "membership_version": membership_version,
            "provider": row.provider,
            "status": row.status,
            "channel_instance_id": str(row.channel_instance_id),
            "external_account_id": row.external_account_id,
            "workspace_id": row.workspace_id,
            "metadata": dict(row.metadata_json or {}),
        }

    @staticmethod
    async def _get_active_principal(
        session: AsyncSession,
        model,
        group_binding_id: uuid.UUID,
        external_account_refs: tuple[str, ...],
        *,
        for_update: bool,
    ):
        statement = select(model).where(
            model.group_binding_id == group_binding_id,
            model.external_account_ref.in_(external_account_refs),
        )
        if for_update:
            statement = statement.with_for_update(of=model)
        return (await session.execute(statement)).scalar_one_or_none()

    async def _create_guest_principal(
        self,
        session: AsyncSession,
        model,
        binding,
        external_account_ref: str,
        now: datetime,
    ):
        from deerflow.persistence.user.model import UserRow

        principal_user_id = uuid.uuid4()
        membership_id = uuid.uuid4()
        principal = model(
            project_id=binding.project_id,
            group_binding_id=binding.id,
            external_account_ref=external_account_ref,
            principal_user_id=str(principal_user_id),
            membership_id=membership_id,
            status="active",
            first_seen_at=now,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        try:
            async with session.begin_nested():
                # These models have no ORM relationships on purpose. Flush each
                # parent explicitly so PostgreSQL sees the composite guest-user
                # and membership keys before the principal row that references
                # them.
                session.add(
                    UserRow(
                        id=str(principal_user_id),
                        email=None,
                        password_hash=None,
                        principal_type="channel_guest",
                        system_role="user",
                        oauth_provider=None,
                        oauth_id=None,
                        needs_setup=False,
                        token_version=0,
                        private_retention_state="active",
                        private_retention_generation=1,
                        private_retention_effective_at=None,
                    )
                )
                await session.flush()
                membership = ProjectMembershipRow(
                    id=membership_id,
                    project_id=binding.project_id,
                    user_id=str(principal_user_id),
                    role="channel_guest",
                    status="active",
                    version=1,
                    activation_generation=1,
                    is_pinned=False,
                    last_entered_at=None,
                )
                session.add(membership)
                await session.flush()
                session.add(principal)
                await session.flush()
            return principal, membership
        except IntegrityError:
            # Identity creation is the L-01 exemption only while the identity
            # is genuinely new. A concurrent winner must be retried through
            # the L-03 Project -> Membership -> User prefix instead of being
            # adopted from this already-entered channel-resource suffix.
            raise GroupBindingRepositoryConflict from None


__all__ = [
    "GroupBindingRepositoryAgentUnavailable",
    "GroupBindingRepositoryConflict",
    "GroupBindingRepositoryNotFound",
    "PostgresProjectChannelGroupBindingRepository",
]
