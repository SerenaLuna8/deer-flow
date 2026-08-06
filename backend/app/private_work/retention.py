from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.automations.execution_authority import (
    AUTOMATION_AUTHORIZATION_REVOKED_ERROR_CODE,
)
from deerflow.persistence.channel_connections.identity_lock import (
    ChannelIdentity,
    lock_channel_identities,
)
from deerflow.persistence.channel_connections.model import ChannelConnectionRow
from deerflow.persistence.scheduled_task_runs.model import ScheduledTaskRunRow
from deerflow.persistence.scheduled_tasks.model import ScheduledTaskRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow

if TYPE_CHECKING:
    from app.private_work.retention_purge import (
        RetentionCandidate,
        RetentionPurger,
        RetentionPurgeResult,
    )


@dataclass(frozen=True, slots=True)
class RetentionChange:
    thread_ids: tuple[str, ...]
    connection_ids: tuple[str, ...]
    automation_ids: tuple[str, ...]
    occurrence_ids: tuple[str, ...]


class PrivateWorkRetentionService:
    """Freeze and restore private rows without deleting user content or credentials."""

    @staticmethod
    async def purge_expired(
        purger: RetentionPurger,
        candidate: RetentionCandidate,
        *,
        now: datetime | None = None,
    ) -> RetentionPurgeResult:
        """Enter the transactional physical-purge boundary for expired data."""

        return await purger.purge(candidate, now=now)

    @staticmethod
    async def restrict_owner_to_viewer(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        now: datetime | None = None,
    ) -> RetentionChange:
        """Stop executable Automations while preserving the active member's readable data."""

        restricted_at = now or datetime.now(UTC)
        automation_ids = tuple(
            sorted(
                (
                    await session.execute(
                        update(ScheduledTaskRow)
                        .where(
                            ScheduledTaskRow.project_id == project_id,
                            ScheduledTaskRow.owner_user_id == owner_user_id,
                            ScheduledTaskRow.deleted_at.is_(None),
                            ScheduledTaskRow.frozen_at.is_(None),
                        )
                        .values(
                            status="paused",
                            next_run_at=None,
                            version=ScheduledTaskRow.version + 1,
                            updated_at=restricted_at,
                        )
                        .returning(ScheduledTaskRow.id)
                    )
                )
                .scalars()
                .all()
            )
        )
        occurrence_ids: tuple[str, ...] = ()
        if automation_ids:
            occurrence_ids = tuple(
                sorted(
                    (
                        await session.execute(
                            update(ScheduledTaskRunRow)
                            .where(
                                ScheduledTaskRunRow.project_id == project_id,
                                ScheduledTaskRunRow.owner_user_id == owner_user_id,
                                ScheduledTaskRunRow.task_id.in_(automation_ids),
                                ScheduledTaskRunRow.status == "queued",
                            )
                            .values(
                                status="cancelled",
                                error_code=AUTOMATION_AUTHORIZATION_REVOKED_ERROR_CODE,
                                error_message=None,
                                finished_at=restricted_at,
                                lease_owner=None,
                                lease_expires_at=None,
                                updated_at=restricted_at,
                            )
                            .returning(ScheduledTaskRunRow.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            )
        return RetentionChange(
            thread_ids=(),
            connection_ids=(),
            automation_ids=automation_ids,
            occurrence_ids=occurrence_ids,
        )

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
        automation_ids = tuple(
            sorted(
                (
                    await session.execute(
                        update(ScheduledTaskRow)
                        .where(
                            ScheduledTaskRow.project_id == project_id,
                            ScheduledTaskRow.owner_user_id == owner_user_id,
                            ScheduledTaskRow.deleted_at.is_(None),
                            ScheduledTaskRow.frozen_at.is_(None),
                        )
                        .values(
                            status="paused",
                            next_run_at=None,
                            frozen_at=frozen_at,
                            version=ScheduledTaskRow.version + 1,
                            updated_at=frozen_at,
                        )
                        .returning(ScheduledTaskRow.id)
                    )
                )
                .scalars()
                .all()
            )
        )
        occurrence_ids: tuple[str, ...] = ()
        if automation_ids:
            occurrence_ids = tuple(
                sorted(
                    (
                        await session.execute(
                            update(ScheduledTaskRunRow)
                            .where(
                                ScheduledTaskRunRow.project_id == project_id,
                                ScheduledTaskRunRow.owner_user_id == owner_user_id,
                                ScheduledTaskRunRow.task_id.in_(automation_ids),
                                ScheduledTaskRunRow.status == "queued",
                            )
                            .values(
                                status="cancelled",
                                error_code=AUTOMATION_AUTHORIZATION_REVOKED_ERROR_CODE,
                                error_message=None,
                                finished_at=frozen_at,
                                lease_owner=None,
                                lease_expires_at=None,
                                updated_at=frozen_at,
                            )
                            .returning(ScheduledTaskRunRow.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            )
        return RetentionChange(
            thread_ids=thread_ids,
            connection_ids=connection_ids,
            automation_ids=automation_ids,
            occurrence_ids=occurrence_ids,
        )

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
            return RetentionChange(
                thread_ids=(),
                connection_ids=(),
                automation_ids=(),
                occurrence_ids=(),
            )
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
        connection_ids: tuple[str, ...] = ()
        if winners:
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
                        .values(
                            status="connected",
                            frozen_at=None,
                            updated_at=restored_at,
                        )
                        .returning(ChannelConnectionRow.id)
                    )
                )
                .scalars()
                .all()
            )
        automation_ids = tuple(
            sorted(
                (
                    await session.execute(
                        update(ScheduledTaskRow)
                        .where(
                            ScheduledTaskRow.project_id == project_id,
                            ScheduledTaskRow.owner_user_id.in_(owners),
                            ScheduledTaskRow.frozen_at.is_not(None),
                            ScheduledTaskRow.deleted_at.is_(None),
                        )
                        .values(
                            frozen_at=None,
                            status="paused",
                            next_run_at=None,
                            updated_at=restored_at,
                        )
                        .returning(ScheduledTaskRow.id)
                    )
                )
                .scalars()
                .all()
            )
        )
        return RetentionChange(
            thread_ids=thread_ids,
            connection_ids=connection_ids,
            automation_ids=automation_ids,
            occurrence_ids=(),
        )
