from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    AgentDesignOperationRow,
    AgentDesignSessionRow,
)


class AgentDesignRepository:
    """Owner-scoped persistence for conversational Agent design sessions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _require_context(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))

    @staticmethod
    def _context_exists(context: ProjectContext):
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

    async def lock_context(self, context: ProjectContext) -> None:
        self._require_context(context)
        statement = (
            select(ProjectRow.id)
            .join(ProjectMembershipRow, ProjectMembershipRow.project_id == ProjectRow.id)
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
            .with_for_update(read=True, of=[ProjectRow, ProjectMembershipRow])
        )
        if (await self.session.execute(statement)).scalar_one_or_none() is None:
            raise AssetNotFound(context.request_id)

    async def create(
        self,
        context: ProjectContext,
        row: AgentDesignSessionRow,
    ) -> AgentDesignSessionRow:
        await self.lock_context(context)
        if row.project_id != context.project_id or row.owner_user_id != str(context.user_id):
            raise AssetForbidden(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_by_create_idempotency(
        self,
        context: ProjectContext,
        idempotency_key_hash: str,
        *,
        for_update: bool = False,
    ) -> AgentDesignSessionRow | None:
        self._require_context(context)
        if for_update:
            await self.lock_context(context)
        statement = select(AgentDesignSessionRow).where(
            AgentDesignSessionRow.project_id == context.project_id,
            AgentDesignSessionRow.owner_user_id == str(context.user_id),
            AgentDesignSessionRow.create_idempotency_key_hash == idempotency_key_hash,
            self._context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=AgentDesignSessionRow)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentDesignSessionRow:
        self._require_context(context)
        if for_update:
            await self.lock_context(context)
        statement = select(AgentDesignSessionRow).where(
            AgentDesignSessionRow.id == session_id,
            AgentDesignSessionRow.project_id == context.project_id,
            AgentDesignSessionRow.owner_user_id == str(context.user_id),
            self._context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=AgentDesignSessionRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        return row

    async def list_incomplete(
        self,
        context: ProjectContext,
        *,
        limit: int = 20,
    ) -> tuple[AgentDesignSessionRow, ...]:
        self._require_context(context)
        statement = (
            select(AgentDesignSessionRow)
            .where(
                AgentDesignSessionRow.project_id == context.project_id,
                AgentDesignSessionRow.owner_user_id == str(context.user_id),
                AgentDesignSessionRow.status.notin_(("completed", "cancelled")),
                self._context_exists(context),
            )
            .order_by(
                AgentDesignSessionRow.updated_at.desc(),
                AgentDesignSessionRow.id.desc(),
            )
            .limit(limit)
        )
        return tuple((await self.session.execute(statement)).scalars())

    async def get_operation(
        self,
        context: ProjectContext,
        *,
        operation_kind: str,
        idempotency_key_hash: str,
        for_update: bool = False,
    ) -> AgentDesignOperationRow | None:
        self._require_context(context)
        if for_update:
            await self.lock_context(context)
        statement = select(AgentDesignOperationRow).where(
            AgentDesignOperationRow.project_id == context.project_id,
            AgentDesignOperationRow.owner_user_id == str(context.user_id),
            AgentDesignOperationRow.operation_kind == operation_kind,
            AgentDesignOperationRow.idempotency_key_hash == idempotency_key_hash,
            self._context_exists(context),
        )
        if for_update:
            statement = statement.with_for_update(of=AgentDesignOperationRow)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def create_operation(
        self,
        context: ProjectContext,
        row: AgentDesignOperationRow,
    ) -> AgentDesignOperationRow:
        await self.lock_context(context)
        if row.project_id != context.project_id or row.owner_user_id != str(context.user_id):
            raise AssetForbidden(context.request_id)
        self.session.add(row)
        await self.session.flush()
        return row


__all__ = ["AgentDesignRepository"]
