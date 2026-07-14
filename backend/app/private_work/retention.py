from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from deerflow.persistence.channel_connections.model import ChannelConnectionRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow


@dataclass(frozen=True, slots=True)
class RetentionChange:
    thread_ids: tuple[str, ...]
    connection_ids: tuple[str, ...]


class PrivateWorkRetentionService:
    """Freeze and restore private rows without deleting user content or credentials."""

    @staticmethod
    async def freeze_owner(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        now: datetime | None = None,
    ) -> RetentionChange:
        frozen_at = now or datetime.now(UTC)
        thread_ids = tuple(
            (
                await session.execute(
                    update(ThreadMetaRow)
                    .where(
                        ThreadMetaRow.project_id == project_id,
                        ThreadMetaRow.owner_user_id == owner_user_id,
                        ThreadMetaRow.frozen_at.is_(None),
                    )
                    .values(frozen_at=frozen_at, updated_at=frozen_at)
                    .returning(ThreadMetaRow.thread_id)
                )
            )
            .scalars()
            .all()
        )
        connection_ids = tuple(
            (
                await session.execute(
                    update(ChannelConnectionRow)
                    .where(
                        ChannelConnectionRow.project_id == project_id,
                        ChannelConnectionRow.owner_user_id == owner_user_id,
                        ChannelConnectionRow.status == "connected",
                    )
                    .values(status="frozen", frozen_at=frozen_at, updated_at=frozen_at)
                    .returning(ChannelConnectionRow.id)
                )
            )
            .scalars()
            .all()
        )
        return RetentionChange(thread_ids=thread_ids, connection_ids=connection_ids)

    @staticmethod
    async def restore_owner(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        now: datetime | None = None,
    ) -> RetentionChange:
        restored_at = now or datetime.now(UTC)
        thread_ids = tuple(
            (
                await session.execute(
                    update(ThreadMetaRow)
                    .where(
                        ThreadMetaRow.project_id == project_id,
                        ThreadMetaRow.owner_user_id == owner_user_id,
                        ThreadMetaRow.frozen_at.is_not(None),
                        ThreadMetaRow.deleted_at.is_(None),
                    )
                    .values(frozen_at=None, updated_at=restored_at)
                    .returning(ThreadMetaRow.thread_id)
                )
            )
            .scalars()
            .all()
        )

        other = aliased(ChannelConnectionRow)
        occupied = exists(
            select(1).where(
                other.id != ChannelConnectionRow.id,
                other.provider == ChannelConnectionRow.provider,
                other.external_account_id == ChannelConnectionRow.external_account_id,
                other.workspace_id == ChannelConnectionRow.workspace_id,
                other.status == "connected",
            )
        )
        connection_ids = tuple(
            (
                await session.execute(
                    update(ChannelConnectionRow)
                    .where(
                        ChannelConnectionRow.project_id == project_id,
                        ChannelConnectionRow.owner_user_id == owner_user_id,
                        ChannelConnectionRow.status == "frozen",
                        ~occupied,
                    )
                    .values(status="connected", frozen_at=None, updated_at=restored_at)
                    .returning(ChannelConnectionRow.id)
                )
            )
            .scalars()
            .all()
        )
        return RetentionChange(thread_ids=thread_ids, connection_ids=connection_ids)
