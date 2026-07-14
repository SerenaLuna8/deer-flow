from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from deerflow.persistence.channel_connections.identity_lock import (
    ChannelIdentity,
    lock_channel_identities,
)
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
        return await PrivateWorkRetentionService.restore_owners(
            session,
            project_id=project_id,
            owner_user_ids=(owner_user_id,),
            now=now,
        )

    @staticmethod
    async def restore_owners(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_ids: tuple[str, ...],
        now: datetime | None = None,
    ) -> RetentionChange:
        """Restore owners after locking all frozen identities in global order."""

        restored_at = now or datetime.now(UTC)
        owners = tuple(sorted(set(owner_user_ids)))
        if not owners:
            return RetentionChange(thread_ids=(), connection_ids=())
        thread_ids = tuple(
            (
                await session.execute(
                    update(ThreadMetaRow)
                    .where(
                        ThreadMetaRow.project_id == project_id,
                        ThreadMetaRow.owner_user_id.in_(owners),
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

        candidates = (
            await session.execute(
                select(
                    ChannelConnectionRow.id,
                    ChannelConnectionRow.provider,
                    ChannelConnectionRow.external_account_id,
                    ChannelConnectionRow.workspace_id,
                )
                .where(
                    ChannelConnectionRow.project_id == project_id,
                    ChannelConnectionRow.owner_user_id.in_(owners),
                    ChannelConnectionRow.status == "frozen",
                )
                .order_by(
                    ChannelConnectionRow.provider,
                    ChannelConnectionRow.external_account_id,
                    ChannelConnectionRow.workspace_id,
                    ChannelConnectionRow.id,
                )
            )
        ).all()
        identities: tuple[ChannelIdentity, ...] = tuple((row.provider, row.external_account_id, row.workspace_id) for row in candidates)
        await lock_channel_identities(session, identities)

        # Candidate ordering selects one stable winner per identity. The
        # correlated holder check below leaves that winner frozen when another
        # connected owner already holds the global identity.
        winners: dict[ChannelIdentity, str] = {}
        for row in candidates:
            identity = (row.provider, row.external_account_id, row.workspace_id)
            winners.setdefault(identity, row.id)
        if not winners:
            return RetentionChange(thread_ids=thread_ids, connection_ids=())

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
                        ChannelConnectionRow.owner_user_id.in_(owners),
                        ChannelConnectionRow.id.in_(tuple(winners.values())),
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
