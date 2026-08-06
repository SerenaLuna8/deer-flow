from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.auth.config import get_auth_config
from app.projects.errors import ProjectDatabaseUnavailable
from deerflow.persistence.projects.invitation_rate_limit_model import (
    ProjectInvitationRateLimitRow,
)

MAX_INVITATION_ATTEMPTS = 5
INVITATION_RATE_LIMIT_WINDOW = timedelta(minutes=5)
INVITATION_RATE_LIMIT_CLEANUP_BATCH_SIZE = 100
_RATE_LIMIT_KEY_DERIVATION_CONTEXT = b"deerflow.auth.rate-limit.hmac-key.v1\x00"
_RATE_LIMIT_VALUE_CONTEXT = b"deerflow.auth.rate-limit.value.v1\x00"


@dataclass(frozen=True)
class RateLimitAdmission:
    """Persisted counter state returned by one atomic admission."""

    key_hash: str
    admitted: bool
    failure_count: int
    window_started_at: datetime


def hash_rate_limit_key(value: str) -> str:
    """Pseudonymize a limiter key with a JWT-secret-derived HMAC key.

    Client addresses and invitation emails are low-entropy identifiers, so a
    bare digest would be recoverable by offline enumeration. The first HMAC
    derives a purpose-specific subkey from the existing Auth JWT secret; the
    second HMAC binds the persisted value to the rate-limit domain.
    """

    auth_secret = get_auth_config().jwt_secret.encode("utf-8")
    derived_key = hmac.new(
        auth_secret,
        _RATE_LIMIT_KEY_DERIVATION_CONTEXT,
        hashlib.sha256,
    ).digest()
    return hmac.new(
        derived_key,
        _RATE_LIMIT_VALUE_CONTEXT + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class InvitationRateLimitRepository:
    """PostgreSQL-shared fixed-window invitation attempt limiter."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def admit_attempt(
        self,
        key_hash: str,
        now: datetime | None = None,
    ) -> bool:
        """Atomically count an attempt and admit only the first five in a window.

        The row is cleared after successful validation. Failed or rejected attempts
        retain the increment, so concurrent bursts cannot pass through a separate
        read-before-write decision.
        """
        return (await self.admit_attempt_record(key_hash, now)).admitted

    async def admit_attempt_record(
        self,
        key_hash: str,
        now: datetime | None = None,
    ) -> RateLimitAdmission:
        """Atomically count an attempt and return its exact persisted state.

        Production callers omit ``now`` and use PostgreSQL
        ``statement_timestamp()`` as the shared clock. Tests may inject an
        explicit timestamp to exercise exact fixed-window boundaries.
        """

        rate_limit_now = now if now is not None else func.statement_timestamp()
        expires_at = rate_limit_now + INVITATION_RATE_LIMIT_WINDOW
        statement = insert(ProjectInvitationRateLimitRow).values(
            key_hash=key_hash,
            failure_count=1,
            window_started_at=rate_limit_now,
            expires_at=expires_at,
            updated_at=rate_limit_now,
        )
        expired = ProjectInvitationRateLimitRow.expires_at <= rate_limit_now
        statement = statement.on_conflict_do_update(
            index_elements=[ProjectInvitationRateLimitRow.key_hash],
            set_={
                "failure_count": case(
                    (expired, 1),
                    else_=ProjectInvitationRateLimitRow.failure_count + 1,
                ),
                "window_started_at": case(
                    (expired, rate_limit_now),
                    else_=ProjectInvitationRateLimitRow.window_started_at,
                ),
                "expires_at": case(
                    (expired, expires_at),
                    else_=ProjectInvitationRateLimitRow.expires_at,
                ),
                "updated_at": rate_limit_now,
            },
        ).returning(
            ProjectInvitationRateLimitRow.failure_count,
            ProjectInvitationRateLimitRow.window_started_at,
        )
        expired_keys = (
            select(ProjectInvitationRateLimitRow.key_hash)
            .where(ProjectInvitationRateLimitRow.expires_at <= rate_limit_now)
            .order_by(
                ProjectInvitationRateLimitRow.expires_at,
                ProjectInvitationRateLimitRow.key_hash,
            )
            .limit(INVITATION_RATE_LIMIT_CLEANUP_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        cleanup = delete(ProjectInvitationRateLimitRow).where(ProjectInvitationRateLimitRow.key_hash.in_(expired_keys))
        try:
            async with self.session.begin():
                # Lock/count this attempt first. Concurrent cleanups then skip each
                # other's active admission keys instead of forming a lock cycle.
                row = (await self.session.execute(statement)).one()
                await self.session.execute(cleanup)
                return RateLimitAdmission(
                    key_hash=key_hash,
                    admitted=row.failure_count <= MAX_INVITATION_ATTEMPTS,
                    failure_count=row.failure_count,
                    window_started_at=row.window_started_at,
                )
        except (DBAPIError, SQLAlchemyTimeoutError):
            raise ProjectDatabaseUnavailable() from None

    async def clear(self, key_hash: str) -> None:
        try:
            async with self.session.begin():
                await self.session.execute(delete(ProjectInvitationRateLimitRow).where(ProjectInvitationRateLimitRow.key_hash == key_hash))
        except (DBAPIError, SQLAlchemyTimeoutError):
            raise ProjectDatabaseUnavailable() from None

    async def clear_if_unchanged(self, admission: RateLimitAdmission) -> None:
        """Clear only when no later attempt has advanced or reset the window."""

        statement = delete(ProjectInvitationRateLimitRow).where(
            ProjectInvitationRateLimitRow.key_hash == admission.key_hash,
            ProjectInvitationRateLimitRow.failure_count == admission.failure_count,
            ProjectInvitationRateLimitRow.window_started_at == admission.window_started_at,
        )
        try:
            async with self.session.begin():
                await self.session.execute(statement)
        except (DBAPIError, SQLAlchemyTimeoutError):
            raise ProjectDatabaseUnavailable() from None
