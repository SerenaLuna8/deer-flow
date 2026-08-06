"""Durable PostgreSQL authority for revocable JWT sessions."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError

from app.gateway.auth.config import get_auth_config
from app.gateway.auth.jwt import TokenPayload, create_access_token
from deerflow.persistence.auth_sessions import AuthSessionRepository
from deerflow.persistence.engine import get_session_factory

_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_SESSION_HASH_DOMAIN = b"deerflow:auth-session:v1:\x00"


class AuthSessionUnavailable(RuntimeError):
    """The session authority cannot be read or changed safely."""


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("auth session timestamp must be timezone-aware")
    return value.astimezone(UTC)


def generate_session_id() -> str:
    """Return a 256-bit URL-safe identifier suitable for the JWT ``sid`` claim."""

    session_id = secrets.token_urlsafe(32)
    if _SESSION_ID_PATTERN.fullmatch(session_id) is None:  # pragma: no cover
        raise RuntimeError("generated auth session id is invalid")
    return session_id


def hash_session_id(session_id: str) -> str:
    """Hash a raw ``sid`` with domain separation before persistence."""

    if not isinstance(session_id, str) or _SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ValueError("auth session id is invalid")
    return hashlib.sha256(_SESSION_HASH_DOMAIN + session_id.encode("ascii")).hexdigest()


def _repository() -> AuthSessionRepository:
    try:
        return AuthSessionRepository(get_session_factory())
    except RuntimeError as exc:
        raise AuthSessionUnavailable("auth session storage unavailable") from exc


async def issue_access_session(
    *,
    user_id: str,
    token_version: int,
    expires_delta: timedelta | None = None,
    now: datetime | None = None,
) -> str:
    """Persist a session first, then return its signed JWT."""

    issued_at = _utc_timestamp(now or datetime.now(UTC))
    expiry = expires_delta
    if expiry is None:
        expiry = timedelta(days=get_auth_config().token_expiry_days)
    expires_at = issued_at + expiry
    session_id = generate_session_id()
    try:
        await _repository().create(
            session_id_hash=hash_session_id(session_id),
            user_id=user_id,
            created_at=issued_at,
            expires_at=expires_at,
        )
    except SQLAlchemyError as exc:
        raise AuthSessionUnavailable("auth session storage unavailable") from exc
    return create_access_token(
        user_id,
        expires_delta=expiry,
        token_version=token_version,
        session_id=session_id,
        issued_at=issued_at,
    )


async def validate_access_session(
    payload: TokenPayload,
    *,
    now: datetime | None = None,
) -> bool:
    """Validate the exact JWT session against current PostgreSQL authority."""

    try:
        return await _repository().validate(
            session_id_hash=hash_session_id(payload.sid),
            user_id=payload.sub,
            token_version=payload.ver,
            now=now or datetime.now(UTC),
        )
    except SQLAlchemyError as exc:
        raise AuthSessionUnavailable("auth session storage unavailable") from exc


async def revoke_access_session(
    payload: TokenPayload,
    *,
    now: datetime | None = None,
) -> bool:
    """Revoke only the session named by a verified JWT."""

    try:
        return await _repository().revoke(
            session_id_hash=hash_session_id(payload.sid),
            user_id=payload.sub,
            now=now or datetime.now(UTC),
        )
    except SQLAlchemyError as exc:
        raise AuthSessionUnavailable("auth session storage unavailable") from exc


async def revoke_all_access_sessions(
    user_id: str,
    *,
    now: datetime | None = None,
) -> int:
    """Revoke every active session for a user after global invalidation."""

    try:
        return await _repository().revoke_all(
            user_id=user_id,
            now=now or datetime.now(UTC),
        )
    except SQLAlchemyError as exc:
        raise AuthSessionUnavailable("auth session storage unavailable") from exc


__all__ = [
    "AuthSessionUnavailable",
    "generate_session_id",
    "hash_session_id",
    "issue_access_session",
    "revoke_access_session",
    "revoke_all_access_sessions",
    "validate_access_session",
]
