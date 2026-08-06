"""PostgreSQL-shared rate limiting for public authentication endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.auth.invitation_rate_limit import (
    INVITATION_RATE_LIMIT_WINDOW,
    InvitationRateLimitRepository,
    RateLimitAdmission,
    hash_rate_limit_key,
)

AUTH_RATE_LIMIT_WINDOW = INVITATION_RATE_LIMIT_WINDOW


class AuthenticationRateLimitAction(StrEnum):
    """Closed public-auth action set used to domain-separate persisted keys."""

    LOGIN = "login"
    REGISTER = "register"


@dataclass(frozen=True)
class AuthenticationRateLimitAdmission:
    """Opaque admission state used for concurrency-safe success clearing."""

    key_hash: str
    admitted: bool
    failure_count: int
    window_started_at: datetime

    @classmethod
    def from_persisted(
        cls,
        admission: RateLimitAdmission,
    ) -> AuthenticationRateLimitAdmission:
        return cls(
            key_hash=admission.key_hash,
            admitted=admission.admitted,
            failure_count=admission.failure_count,
            window_started_at=admission.window_started_at,
        )

    def as_persisted(self) -> RateLimitAdmission:
        return RateLimitAdmission(
            key_hash=self.key_hash,
            admitted=self.admitted,
            failure_count=self.failure_count,
            window_started_at=self.window_started_at,
        )


def authentication_rate_limit_key(
    action: AuthenticationRateLimitAction,
    client_ip: str,
) -> str:
    """Return a non-reversible, action-separated key for one client address."""

    return hash_rate_limit_key(f"auth-v1\x00{action.value}\x00{client_ip}")


class AuthenticationRateLimitRepository:
    """Use the shared PostgreSQL fixed-window counter for login/registration.

    The physical table keeps its historical ``project_invitation_rate_limits``
    name because the final fresh-install baseline is static. Domain-separated
    hashes make authentication, invitation claim, and invitation redemption
    independent while retaining one concurrency-safe counter implementation.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._repository = InvitationRateLimitRepository(session)

    async def admit_attempt(
        self,
        action: AuthenticationRateLimitAction,
        client_ip: str,
        now: datetime | None = None,
    ) -> AuthenticationRateLimitAdmission:
        return AuthenticationRateLimitAdmission.from_persisted(
            await self._repository.admit_attempt_record(
                authentication_rate_limit_key(action, client_ip),
                now,
            )
        )

    async def clear(
        self,
        admission: AuthenticationRateLimitAdmission,
    ) -> None:
        await self._repository.clear_if_unchanged(admission.as_persisted())
