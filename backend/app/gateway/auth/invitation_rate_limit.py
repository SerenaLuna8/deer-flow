from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.errors import ProjectDatabaseUnavailable
from deerflow.persistence.projects.invitation_rate_limit_model import (
    ProjectInvitationRateLimitRow,
)

MAX_INVITATION_FAILURES = 5
INVITATION_RATE_LIMIT_WINDOW = timedelta(minutes=5)


def hash_rate_limit_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class InvitationRateLimitRepository:
    """PostgreSQL-shared fixed-window invitation failure limiter."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def is_limited(self, key_hash: str, now: datetime) -> bool:
        try:
            async with self.session.begin():
                row = (
                    await self.session.execute(
                        select(
                            ProjectInvitationRateLimitRow.failure_count,
                            ProjectInvitationRateLimitRow.expires_at,
                        )
                        .where(ProjectInvitationRateLimitRow.key_hash == key_hash)
                        .with_for_update()
                    )
                ).one_or_none()
                return bool(row is not None and row.failure_count >= MAX_INVITATION_FAILURES and row.expires_at > now)
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def record_failure(self, key_hash: str, now: datetime) -> bool:
        expires_at = now + INVITATION_RATE_LIMIT_WINDOW
        statement = insert(ProjectInvitationRateLimitRow).values(
            key_hash=key_hash,
            failure_count=1,
            window_started_at=now,
            expires_at=expires_at,
            updated_at=now,
        )
        expired = ProjectInvitationRateLimitRow.expires_at <= now
        statement = statement.on_conflict_do_update(
            index_elements=[ProjectInvitationRateLimitRow.key_hash],
            set_={
                "failure_count": case(
                    (expired, 1),
                    else_=ProjectInvitationRateLimitRow.failure_count + 1,
                ),
                "window_started_at": case(
                    (expired, now),
                    else_=ProjectInvitationRateLimitRow.window_started_at,
                ),
                "expires_at": case(
                    (expired, expires_at),
                    else_=ProjectInvitationRateLimitRow.expires_at,
                ),
                "updated_at": now,
            },
        ).returning(
            ProjectInvitationRateLimitRow.failure_count,
            ProjectInvitationRateLimitRow.expires_at,
        )
        try:
            async with self.session.begin():
                row = (await self.session.execute(statement)).one()
                return row.failure_count >= MAX_INVITATION_FAILURES and row.expires_at > now
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None

    async def clear(self, key_hash: str) -> None:
        try:
            async with self.session.begin():
                await self.session.execute(delete(ProjectInvitationRateLimitRow).where(ProjectInvitationRateLimitRow.key_hash == key_hash))
        except DBAPIError:
            raise ProjectDatabaseUnavailable() from None
