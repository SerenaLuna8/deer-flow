from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from deerflow.persistence.projects.model import (
    ProjectDefaultAgentRow,
    ProjectMembershipRow,
    ProjectRow,
)
from deerflow.persistence.shared_assets import AgentRow


class ProjectDefaultAgentRepository:
    """Project-scoped default Agent persistence with explicit lock ordering."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _require_actor(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))

    @staticmethod
    def _project_context_exists(context: ProjectContext):
        return exists(
            select(1)
            .select_from(ProjectMembershipRow)
            .join(ProjectRow, ProjectRow.id == ProjectMembershipRow.project_id)
            .where(
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.project_id == context.project_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
            )
        )

    async def lock_project(
        self,
        context: ProjectContext,
        *,
        read: bool = False,
    ) -> None:
        self._require_actor(context)
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
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
            )
            .with_for_update(
                read=read,
                of=[ProjectRow, ProjectMembershipRow],
            )
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def get_in_session(
        self,
        context: ProjectContext,
        *,
        for_update: bool = False,
    ) -> ProjectDefaultAgentRow | None:
        """Read the pointer in the caller transaction.

        Callers using ``for_update=True`` must first lock the project and
        membership so an absent pointer is serialized against its first PUT.
        """

        self._require_actor(context)
        statement = select(ProjectDefaultAgentRow).where(
            ProjectDefaultAgentRow.project_id == context.project_id,
            self._project_context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=ProjectDefaultAgentRow)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def lock_project_agent(
        self,
        context: ProjectContext,
        agent_asset_id: uuid.UUID,
    ) -> AgentRow:
        self._require_actor(context)
        statement = (
            select(AgentRow)
            .where(
                AgentRow.id == agent_asset_id,
                AgentRow.scope == "project",
                AgentRow.project_id == context.project_id,
                self._project_context_exists(context),
            )
            .with_for_update(read=True, of=AgentRow)
        )
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def create(
        self,
        context: ProjectContext,
        agent_asset_id: uuid.UUID | None,
    ) -> ProjectDefaultAgentRow:
        self._require_actor(context)
        row = ProjectDefaultAgentRow(
            project_id=context.project_id,
            agent_asset_id=agent_asset_id,
            revision=1,
            created_by_user_id=str(context.user_id),
            updated_by_user_id=str(context.user_id),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def replace(
        self,
        context: ProjectContext,
        row: ProjectDefaultAgentRow,
        agent_asset_id: uuid.UUID | None,
    ) -> ProjectDefaultAgentRow:
        self._require_actor(context)
        if row.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        row.agent_asset_id = agent_asset_id
        row.revision += 1
        row.updated_by_user_id = str(context.user_id)
        await self.session.flush()
        return row
