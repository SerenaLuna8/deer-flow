from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.audit.model import AuditLogRow


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
    ) -> tuple[AuditLogRow, ...]:
        return await self._list(
            select(AuditLogRow),
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
