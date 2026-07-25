"""Session repository that never accepts or returns a raw JWT session id."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.auth_sessions.model import AuthSessionRow
from deerflow.persistence.user.model import UserRow

_SESSION_HASH = re.compile(r"[0-9a-f]{64}")
_LAST_SEEN_WRITE_INTERVAL = timedelta(minutes=5)
_SESSION_PRUNE_GRACE = timedelta(days=7)
_SESSION_PRUNE_BATCH_SIZE = 256


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("auth session timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _hash(value: str) -> str:
    if not isinstance(value, str) or _SESSION_HASH.fullmatch(value) is None:
        raise ValueError("auth session hash is invalid")
    return value


class AuthSessionRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._sessions = session_factory

    async def create(
        self,
        *,
        session_id_hash: str,
        user_id: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        digest = _hash(session_id_hash)
        created = _aware(created_at)
        expires = _aware(expires_at)
        if expires <= created:
            raise ValueError("auth session expiry must follow creation")
        async with self._sessions() as session, session.begin():
            cutoff = created - _SESSION_PRUNE_GRACE
            await session.execute(
                text(
                    """WITH expired AS MATERIALIZED (
                           SELECT session_id_hash
                            FROM auth_sessions
                            WHERE expires_at <= :cutoff
                            ORDER BY expires_at, session_id_hash
                            LIMIT :batch_size
                              FOR UPDATE SKIP LOCKED
                       ), revoked AS MATERIALIZED (
                           SELECT session_id_hash
                             FROM auth_sessions
                            WHERE revoked_at <= :cutoff
                              AND expires_at > :cutoff
                            ORDER BY revoked_at, session_id_hash
                            LIMIT GREATEST(
                                :batch_size - (SELECT count(*) FROM expired),
                                0
                            )
                              FOR UPDATE SKIP LOCKED
                       ), stale AS (
                           SELECT session_id_hash FROM expired
                           UNION ALL
                           SELECT session_id_hash FROM revoked
                       )
                       DELETE FROM auth_sessions AS session_row
                        USING stale
                        WHERE session_row.session_id_hash = stale.session_id_hash"""
                ),
                {
                    "cutoff": cutoff,
                    "batch_size": _SESSION_PRUNE_BATCH_SIZE,
                },
            )
            session.add(
                AuthSessionRow(
                    session_id_hash=digest,
                    user_id=str(user_id),
                    created_at=created,
                    expires_at=expires,
                    last_seen_at=created,
                )
            )

    async def validate(
        self,
        *,
        session_id_hash: str,
        user_id: str,
        token_version: int,
        now: datetime,
    ) -> bool:
        digest = _hash(session_id_hash)
        current = _aware(now)
        if not isinstance(token_version, int) or token_version < 0:
            return False
        async with self._sessions() as session, session.begin():
            row = await session.scalar(
                select(AuthSessionRow)
                .join(UserRow, UserRow.id == AuthSessionRow.user_id)
                .where(
                    AuthSessionRow.session_id_hash == digest,
                    AuthSessionRow.user_id == str(user_id),
                    AuthSessionRow.revoked_at.is_(None),
                    AuthSessionRow.created_at <= current,
                    AuthSessionRow.expires_at > current,
                    UserRow.token_version == token_version,
                )
            )
            if row is None:
                return False
            last_seen = _aware(row.last_seen_at)
            if last_seen <= current - _LAST_SEEN_WRITE_INTERVAL:
                await session.execute(
                    update(AuthSessionRow)
                    .where(
                        AuthSessionRow.session_id_hash == digest,
                        AuthSessionRow.revoked_at.is_(None),
                        AuthSessionRow.created_at <= current,
                        AuthSessionRow.expires_at > current,
                        AuthSessionRow.last_seen_at <= current - _LAST_SEEN_WRITE_INTERVAL,
                    )
                    .values(last_seen_at=current)
                )
            return True

    async def revoke(
        self,
        *,
        session_id_hash: str,
        user_id: str,
        now: datetime,
    ) -> bool:
        digest = _hash(session_id_hash)
        revoked_at = _aware(now)
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(AuthSessionRow)
                .where(
                    AuthSessionRow.session_id_hash == digest,
                    AuthSessionRow.user_id == str(user_id),
                    AuthSessionRow.revoked_at.is_(None),
                )
                .values(revoked_at=revoked_at)
            )
            return result.rowcount == 1

    async def revoke_all(
        self,
        *,
        user_id: str,
        now: datetime,
    ) -> int:
        revoked_at = _aware(now)
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(AuthSessionRow)
                .where(
                    AuthSessionRow.user_id == str(user_id),
                    AuthSessionRow.revoked_at.is_(None),
                )
                .values(revoked_at=revoked_at)
            )
            return int(result.rowcount or 0)


__all__ = ["AuthSessionRepository"]
