"""JWT token creation and verification."""

from datetime import UTC, datetime, timedelta

import jwt
from pydantic import BaseModel, Field, ValidationError

from app.gateway.auth.config import get_auth_config
from app.gateway.auth.errors import TokenError


class TokenPayload(BaseModel):
    """JWT token payload."""

    sub: str = Field(min_length=1)  # user_id
    exp: datetime
    iat: datetime
    ver: int = Field(ge=0)  # token_version — must match User.token_version
    sid: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")


def create_access_token(
    user_id: str,
    expires_delta: timedelta | None = None,
    token_version: int = 0,
    *,
    session_id: str,
    issued_at: datetime | None = None,
) -> str:
    """Create a JWT access token.

    Args:
        user_id: The user's UUID as string
        expires_delta: Optional custom expiry, defaults to 7 days
        token_version: User's current token_version for invalidation
        session_id: Unpredictable raw session identifier stored only in the JWT
        issued_at: Optional timezone-aware issuance time

    Returns:
        Encoded JWT string
    """
    config = get_auth_config()
    expiry = expires_delta if expires_delta is not None else timedelta(days=config.token_expiry_days)

    now = issued_at or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("issued_at must be timezone-aware")
    now = now.astimezone(UTC)
    payload = TokenPayload(
        sub=user_id,
        exp=now + expiry,
        iat=now,
        ver=token_version,
        sid=session_id,
    ).model_dump()
    return jwt.encode(payload, config.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> TokenPayload | TokenError:
    """Decode and validate a JWT token.

    Returns:
        TokenPayload if valid, or a specific TokenError variant.
    """
    config = get_auth_config()
    try:
        payload = jwt.decode(token, config.jwt_secret, algorithms=["HS256"])
        return TokenPayload(**payload)
    except jwt.ExpiredSignatureError:
        return TokenError.EXPIRED
    except jwt.InvalidSignatureError:
        return TokenError.INVALID_SIGNATURE
    except (jwt.PyJWTError, ValidationError, TypeError, ValueError):
        return TokenError.MALFORMED
