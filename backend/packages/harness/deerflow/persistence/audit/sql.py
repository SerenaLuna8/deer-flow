from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.projects.model import ProjectRow


class AuditRepository:
    """Session-bound append and scoped reads with caller-owned transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(self, **values: object) -> AuditLogRow:
        row = AuditLogRow(**values)
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_project(
        self,
        project_id: uuid.UUID,
        *,
        limit: int,
        cursor: tuple[datetime, uuid.UUID] | None = None,
        action: str | None = None,
        outcome: str | None = None,
        target_refs: tuple[tuple[str, str], ...] | None = None,
    ) -> tuple[AuditLogRow, ...]:
        statement = select(AuditLogRow).where(AuditLogRow.project_id == project_id)
        return await self._list(
            statement,
            limit=limit,
            cursor=cursor,
            action=action,
            outcome=outcome,
            target_refs=target_refs,
        )

    async def list_platform(
        self,
        *,
        limit: int,
        cursor: tuple[datetime, uuid.UUID] | None = None,
        action: str | None = None,
        outcome: str | None = None,
        target_refs: tuple[tuple[str, str], ...] | None = None,
        project_id: uuid.UUID | None = None,
        project_query: str | None = None,
        platform_only: bool = False,
    ) -> tuple[AuditLogRow, ...]:
        if platform_only and (project_id is not None or project_query is not None):
            raise ValueError("platform_only cannot combine with project filters")
        statement = select(AuditLogRow)
        if platform_only:
            statement = statement.where(AuditLogRow.project_id.is_(None))
        elif project_id is not None:
            statement = statement.where(AuditLogRow.project_id == project_id)
        if project_query is not None:
            if type(project_query) is not str:
                raise TypeError("project_query must be a string")
            normalized_query = project_query.strip().lower()
            if not normalized_query:
                raise ValueError("project_query must not be blank")
            statement = statement.join(
                ProjectRow,
                ProjectRow.id == AuditLogRow.project_id,
            ).where(
                or_(
                    func.lower(ProjectRow.slug).contains(
                        normalized_query,
                        autoescape=True,
                    ),
                    func.lower(ProjectRow.display_name).contains(
                        normalized_query,
                        autoescape=True,
                    ),
                )
            )
        return await self._list(
            statement,
            limit=limit,
            cursor=cursor,
            action=action,
            outcome=outcome,
            target_refs=target_refs,
        )

    async def _list(
        self,
        statement,
        *,
        limit: int,
        cursor: tuple[datetime, uuid.UUID] | None,
        action: str | None,
        outcome: str | None,
        target_refs: tuple[tuple[str, str], ...] | None,
    ) -> tuple[AuditLogRow, ...]:
        if cursor is not None:
            occurred_at, row_id = cursor
            statement = statement.where(
                or_(
                    AuditLogRow.occurred_at < occurred_at,
                    and_(AuditLogRow.occurred_at == occurred_at, AuditLogRow.id < row_id),
                )
            )
        if action is not None:
            statement = statement.where(AuditLogRow.action == action)
        if outcome is not None:
            statement = statement.where(AuditLogRow.outcome == outcome)
        if target_refs is not None:
            if not target_refs:
                return ()
            statement = statement.where(
                or_(
                    *(
                        and_(
                            AuditLogRow.target_ref_key_id == key_id,
                            AuditLogRow.target_ref_hmac == hmac_hex,
                        )
                        for key_id, hmac_hex in target_refs
                    )
                )
            )
        rows = (
            await self.session.execute(
                statement.order_by(
                    AuditLogRow.occurred_at.desc(),
                    AuditLogRow.id.desc(),
                ).limit(limit)
            )
        ).scalars()
        return tuple(rows)


__all__ = ["AuditRepository"]
