from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.auth.config import AuthConfig, set_auth_config
from app.gateway.auth.errors import TokenError
from app.gateway.auth.jwt import create_access_token, decode_token
from app.gateway.auth.sessions import (
    AuthSessionUnavailable,
    generate_session_id,
    hash_session_id,
)

_SECRET = "auth-session-unit-test-secret-at-least-32"


def test_session_id_is_unpredictable_and_only_its_domain_hash_is_stable() -> None:
    first = generate_session_id()
    second = generate_session_id()

    assert first != second
    assert len(first) >= 43
    assert len(hash_session_id(first)) == 64
    assert hash_session_id(first) == hash_session_id(first)
    assert first not in hash_session_id(first)


def test_jwt_requires_exact_session_id_claim() -> None:
    set_auth_config(AuthConfig(jwt_secret=_SECRET))
    session_id = generate_session_id()
    token = create_access_token(
        "11111111-1111-4111-8111-111111111111",
        token_version=3,
        session_id=session_id,
    )

    payload = decode_token(token)
    assert not isinstance(payload, TokenError)
    assert payload.sid == session_id
    assert payload.ver == 3

    stateless = pyjwt.encode(
        {
            "sub": "11111111-1111-4111-8111-111111111111",
            "exp": payload.exp + timedelta(hours=1),
            "ver": 3,
        },
        _SECRET,
        algorithm="HS256",
    )
    assert decode_token(stateless) == TokenError.MALFORMED


def test_logout_storage_failure_is_truthful_but_still_clears_local_cookie() -> None:
    from app.gateway.routers.auth import router

    set_auth_config(AuthConfig(jwt_secret=_SECRET))
    token = create_access_token(
        "11111111-1111-4111-8111-111111111111",
        token_version=0,
        session_id=generate_session_id(),
    )
    app = FastAPI()
    app.include_router(router)
    with (
        TestClient(app) as client,
        patch(
            "app.gateway.routers.auth.revoke_access_session",
            new=AsyncMock(side_effect=AuthSessionUnavailable()),
        ),
    ):
        client.cookies.set(
            "access_token",
            token,
            domain="testserver.local",
            path="/",
        )
        response = client.post("/api/v1/auth/logout")

        assert response.status_code == 503
        assert response.json() == {
            "detail": {
                "code": "DATABASE_UNAVAILABLE",
                "message": "Authentication storage unavailable",
            }
        }
        assert "access_token" not in client.cookies
        assert "max-age=0" in response.headers["set-cookie"].lower()
