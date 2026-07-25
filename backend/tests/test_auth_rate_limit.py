from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.auth.config import AuthConfig, set_auth_config
from app.gateway.auth.sessions import AuthSessionUnavailable
from app.projects.errors import ProjectDatabaseUnavailable

_TEST_SECRET = "test-secret-for-auth-rate-limit-minimum-32"


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


class _SessionFactory:
    def __call__(self):
        return _SessionContext()


class _Limiter:
    def __init__(
        self,
        *,
        admitted: bool = True,
        unavailable: bool = False,
        clear_unavailable: bool = False,
    ) -> None:
        self.admitted = admitted
        self.unavailable = unavailable
        self.clear_unavailable = clear_unavailable
        self.admissions: list[tuple[object, str, object]] = []
        self.clears: list[object] = []

    async def admit_attempt(self, action, client_ip, now=None):
        from app.gateway.auth.rate_limit import AuthenticationRateLimitAdmission

        self.admissions.append((action, client_ip, now))
        if self.unavailable:
            raise ProjectDatabaseUnavailable()
        return AuthenticationRateLimitAdmission(
            key_hash="a" * 64,
            admitted=self.admitted,
            failure_count=1,
            window_started_at=now or datetime.now(UTC),
        )

    async def clear(self, admission):
        self.clears.append(admission)
        if self.clear_unavailable:
            raise ProjectDatabaseUnavailable()


@pytest.fixture()
def auth_client(monkeypatch):
    from app.gateway.routers import auth

    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
    app = FastAPI()
    app.include_router(auth.router)
    limiter = _Limiter()
    provider = SimpleNamespace(
        authenticate=AsyncMock(return_value=None),
        create_user=AsyncMock(
            return_value=SimpleNamespace(
                id=uuid.uuid4(),
                email="new@example.com",
                system_role="user",
                oauth_provider=None,
                token_version=0,
                needs_setup=False,
            )
        ),
    )
    monkeypatch.setattr(auth, "get_session_factory", lambda: _SessionFactory())
    monkeypatch.setattr(auth, "AuthenticationRateLimitRepository", lambda _session: limiter)
    monkeypatch.setattr(auth, "get_local_provider", lambda: provider)
    session_issuer = AsyncMock(return_value="durable-session-token")
    monkeypatch.setattr(
        auth,
        "issue_access_session",
        session_issuer,
    )
    provider.session_issuer = session_issuer
    with TestClient(app, client=("192.0.2.44", 40123)) as client:
        yield client, limiter, provider


def test_login_rate_limit_is_postgresql_admitted_before_password_check(auth_client) -> None:
    from app.gateway.auth.rate_limit import AuthenticationRateLimitAction

    client, limiter, provider = auth_client
    limiter.admitted = False

    response = client.post(
        "/api/v1/auth/login/local",
        data={"username": "person@example.com", "password": "wrong"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "300"
    assert response.json() == {
        "detail": {
            "code": "rate_limited",
            "message": "Too many authentication attempts. Try again later.",
        }
    }
    assert [item[:2] for item in limiter.admissions] == [(AuthenticationRateLimitAction.LOGIN, "192.0.2.44")]
    assert limiter.admissions[0][2] is None
    provider.authenticate.assert_not_awaited()


def test_successful_login_clears_only_its_shared_counter(auth_client) -> None:
    client, limiter, provider = auth_client
    provider.authenticate.return_value = SimpleNamespace(
        id=uuid.uuid4(),
        token_version=0,
        needs_setup=False,
    )

    response = client.post(
        "/api/v1/auth/login/local",
        data={"username": "person@example.com", "password": "correct"},
    )

    assert response.status_code == 200
    assert len(limiter.clears) == 1
    assert limiter.clears[0].key_hash == "a" * 64


def test_successful_login_clear_database_failure_is_sanitized(auth_client) -> None:
    client, limiter, provider = auth_client
    limiter.clear_unavailable = True
    provider.authenticate.return_value = SimpleNamespace(
        id=uuid.uuid4(),
        token_version=0,
        needs_setup=False,
    )

    response = client.post(
        "/api/v1/auth/login/local",
        data={"username": "person@example.com", "password": "correct"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "DATABASE_UNAVAILABLE",
            "message": "Project storage unavailable",
        }
    }
    assert "postgresql" not in response.text.lower()
    assert len(limiter.clears) == 1


def test_registration_is_rate_limited_and_success_does_not_clear_counter(auth_client) -> None:
    from app.gateway.auth.rate_limit import AuthenticationRateLimitAction

    client, limiter, provider = auth_client

    response = client.post(
        "/api/v1/auth/register",
        json={"email": "new@example.com", "password": "Str0ng!Register99"},
    )

    assert response.status_code == 201
    assert [item[:2] for item in limiter.admissions] == [(AuthenticationRateLimitAction.REGISTER, "192.0.2.44")]
    assert limiter.admissions[0][2] is None
    assert limiter.clears == []
    provider.create_user.assert_awaited_once()


@pytest.mark.parametrize("action", ["login", "register"])
def test_successful_credentials_do_not_issue_cookie_when_session_write_fails(
    auth_client,
    action: str,
) -> None:
    client, _limiter, provider = auth_client
    provider.session_issuer.side_effect = AuthSessionUnavailable()
    if action == "login":
        provider.authenticate.return_value = SimpleNamespace(
            id=uuid.uuid4(),
            token_version=0,
            needs_setup=False,
        )
        response = client.post(
            "/api/v1/auth/login/local",
            data={"username": "person@example.com", "password": "correct"},
        )
    else:
        response = client.post(
            "/api/v1/auth/register",
            json={"email": "new@example.com", "password": "Str0ng!Register99"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "DATABASE_UNAVAILABLE",
            "message": "Authentication storage unavailable",
        }
    }
    assert "access_token" not in response.cookies


def test_registration_limit_rejects_before_password_hashing(auth_client) -> None:
    client, limiter, provider = auth_client
    limiter.admitted = False

    response = client.post(
        "/api/v1/auth/register",
        json={"email": "new@example.com", "password": "Str0ng!Register99"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "300"
    provider.create_user.assert_not_awaited()


def test_auth_rate_limit_database_failure_is_sanitized_and_fails_closed(auth_client) -> None:
    client, limiter, provider = auth_client
    limiter.unavailable = True

    response = client.post(
        "/api/v1/auth/login/local",
        data={"username": "person@example.com", "password": "wrong"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "DATABASE_UNAVAILABLE",
            "message": "Project storage unavailable",
        }
    }
    assert "postgresql" not in response.text.lower()
    provider.authenticate.assert_not_awaited()


def test_auth_rate_limit_key_is_keyed_domain_separated_and_not_bare_sha256() -> None:
    from app.gateway.auth.rate_limit import (
        AuthenticationRateLimitAction,
        authentication_rate_limit_key,
    )

    client_ip = "192.0.2.44"
    login_key = authentication_rate_limit_key(
        AuthenticationRateLimitAction.LOGIN,
        client_ip,
    )
    register_key = authentication_rate_limit_key(
        AuthenticationRateLimitAction.REGISTER,
        client_ip,
    )

    assert login_key != register_key
    assert len(login_key) == len(register_key) == 64
    assert client_ip not in login_key
    assert client_ip not in register_key
    assert login_key != hashlib.sha256(f"auth-v1\x00login\x00{client_ip}".encode()).hexdigest()
    assert register_key != hashlib.sha256(f"auth-v1\x00register\x00{client_ip}".encode()).hexdigest()

    set_auth_config(AuthConfig(jwt_secret="different-test-secret-for-rate-limit-32"))
    try:
        assert login_key != authentication_rate_limit_key(
            AuthenticationRateLimitAction.LOGIN,
            client_ip,
        )
    finally:
        set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
