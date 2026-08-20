from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, cast, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
        binding_conditions = (
            ProjectChannelGroupBindingRow.channel_instance_id == channel_instance_id,
            ProjectChannelGroupBindingRow.provider == provider,
            ProjectChannelGroupBindingRow.external_group_ref.in_(
                external_group_refs,
            ),
            ProjectChannelGroupBindingRow.status == "active",
            ProjectChannelGroupBindingRow.deleted_at.is_(None),
            ProjectChannelInstanceRow.desired_status == "enabled",
            ProjectChannelInstanceRow.observed_status == "running",
            ProjectChannelInstanceRow.deleted_at.is_(None),
        )
        project_id = (
            await session.execute(
                select(ProjectChannelGroupBindingRow.project_id)
                .join(
                    ProjectChannelInstanceRow,
                    (ProjectChannelInstanceRow.project_id == ProjectChannelGroupBindingRow.project_id) & (ProjectChannelInstanceRow.id == ProjectChannelGroupBindingRow.channel_instance_id),
                )
                .where(*binding_conditions)
            )
        ).scalar_one_or_none()
        if project_id is None:
            return None
        locked_project_id = (
            await session.execute(
                select(ProjectRow.id)
                .where(
                    ProjectRow.id == project_id,
                    ProjectRow.status == "active",
                    ProjectRow.is_suspended.is_(False),
                )
                .with_for_update(read=True, of=ProjectRow)
            )
        ).scalar_one_or_none()
        if locked_project_id is None:
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
                .with_for_update(read=True, of=ProjectChannelInstanceRow)
            )
        ).scalar_one_or_none()
        if locked_instance_id is None:
            return None
        binding = (
            await session.execute(
                select(ProjectChannelGroupBindingRow)
                .where(
                    ProjectChannelGroupBindingRow.project_id == locked_project_id,
                    ProjectChannelGroupBindingRow.channel_instance_id == locked_instance_id,
                    ProjectChannelGroupBindingRow.provider == provider,
                    ProjectChannelGroupBindingRow.external_group_ref.in_(
                        external_group_refs,
                    ),
                    ProjectChannelGroupBindingRow.status == "active",
                    ProjectChannelGroupBindingRow.deleted_at.is_(None),
                )
                .with_for_update(of=ProjectChannelGroupBindingRow)
            )
        ).scalar_one_or_none()
        if binding is None:
            return None
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
        principal = await self._get_active_principal(
            session,
            ChannelExternalPrincipalRow,
            binding.id,
            external_account_refs,
            for_update=True,
        )
        if principal is None:
            principal = await self._create_guest_principal(
                session,
                ChannelExternalPrincipalRow,
                binding,
                external_account_refs[0],
                now,
            )
        membership = (
            await session.execute(
                select(ProjectMembershipRow).where(
                    ProjectMembershipRow.id == principal.membership_id,
                    ProjectMembershipRow.project_id == binding.project_id,
                    ProjectMembershipRow.user_id == principal.principal_user_id,
                    ProjectMembershipRow.role == "channel_guest",
                    ProjectMembershipRow.status == "active",
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            return None
        connection = await self._ensure_guest_connection(
            session,
            binding=binding,
            principal=principal,
            external_account_ref=principal.external_account_ref,
            external_group_ref=binding.external_group_ref,
            now=now,
        )
        if connection is None:
            # Explicit disconnect is terminal. Retain the principal and its
            # stable identity anchor, but do not restore authority implicitly.
            return None
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
    async def _ensure_guest_connection(
        session: AsyncSession,
        *,
        binding,
        principal,
        external_account_ref: str,
        external_group_ref: str,
        now: datetime,
    ) -> ChannelConnectionRow | None:
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
        if row is None:
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
            if row.status == "revoked":
                return None
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
                    )
                )
                await session.flush()
                session.add(
                    ProjectMembershipRow(
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
                )
                await session.flush()
                session.add(principal)
                await session.flush()
            return principal
        except IntegrityError:
            existing = await self._get_active_principal(
                session,
                model,
                binding.id,
                (external_account_ref,),
                for_update=True,
            )
            if existing is None:
                raise
            return existing


__all__ = [
    "GroupBindingRepositoryAgentUnavailable",
    "GroupBindingRepositoryConflict",
    "GroupBindingRepositoryNotFound",
    "PostgresProjectChannelGroupBindingRepository",
]
