"""Tests for auth type system hardening.

Covers structured error responses, typed decode_token callers,
CSRF middleware path matching, config-driven cookie security,
and unhappy paths / edge cases for all auth boundaries.
"""

import os
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.gateway.auth.config import AuthConfig, set_auth_config
from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse, TokenError
from app.gateway.auth.jwt import decode_token
from app.gateway.csrf_middleware import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    is_auth_endpoint,
    should_check_csrf,
)

# ── Setup ────────────────────────────────────────────────────────────

_TEST_SECRET = "test-secret-for-auth-type-system-tests-min32"
_AUTH_CLIENT: TestClient | None = None


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest_asyncio.fixture()
async def _persistence_engine(migrated_postgres_database_url):
    """Initialise a per-test PostgreSQL engine + reset cached providers.

    The auth tests call real HTTP handlers that go through
    ``SQLUserRepository`` → ``get_session_factory``. Each test gets a
    migrated isolated database plus clean ``deps._cached_*`` state so the provider
    does not hold a dangling reference to the previous test's engine.
    """
    from app.gateway import deps
    from deerflow.config.database_config import DatabaseConfig
    from deerflow.persistence.engine import close_engine, init_engine

    global _AUTH_CLIENT

    deps._cached_local_provider = None
    deps._cached_repo = None
    app = _make_auth_app()
    app.router.lifespan_context = _noop_lifespan
    from app.system_runtime_settings import AuthPolicyValue

    app.state.system_runtime_policy_materializer = SimpleNamespace(
        materialize_current=AsyncMock(return_value=AuthPolicyValue()),
    )
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(init_engine, DatabaseConfig(url=migrated_postgres_database_url))
        _AUTH_CLIENT = client
        try:
            yield
        finally:
            deps._cached_local_provider = None
            deps._cached_repo = None
            client.portal.call(close_engine)
            _AUTH_CLIENT = None


def _setup_config():
    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))


# ── CSRF Middleware Path Matching ────────────────────────────────────


class _FakeRequest:
    """Minimal request mock for CSRF path matching tests."""

    def __init__(self, path: str, method: str = "POST"):
        self.method = method

        class _URL:
            def __init__(self, p):
                self.path = p

        self.url = _URL(path)
        self.cookies = {}
        self.headers = {}


def test_csrf_exempts_login_local():
    """login/local (actual route) should be exempt from CSRF."""
    req = _FakeRequest("/api/v1/auth/login/local")
    assert is_auth_endpoint(req) is True


def test_csrf_exempts_login_local_trailing_slash():
    """Trailing slash should also be exempt."""
    req = _FakeRequest("/api/v1/auth/login/local/")
    assert is_auth_endpoint(req) is True


def test_csrf_exempts_logout():
    req = _FakeRequest("/api/v1/auth/logout")
    assert is_auth_endpoint(req) is True


def test_csrf_exempts_register():
    req = _FakeRequest("/api/v1/auth/register")
    assert is_auth_endpoint(req) is True


def test_csrf_does_not_exempt_old_login_path():
    """Old /api/v1/auth/login (without /local) should NOT be exempt."""
    req = _FakeRequest("/api/v1/auth/login")
    assert is_auth_endpoint(req) is False


def test_csrf_does_not_exempt_me():
    req = _FakeRequest("/api/v1/auth/me")
    assert is_auth_endpoint(req) is False


def test_csrf_skips_get_requests():
    req = _FakeRequest("/api/v1/auth/me", method="GET")
    assert should_check_csrf(req) is False


def test_csrf_checks_post_to_protected():
    req = _FakeRequest("/api/v1/some/endpoint", method="POST")
    assert should_check_csrf(req) is True


# ── Structured Error Response Format ────────────────────────────────


def test_auth_error_response_has_code_and_message():
    """All auth errors should have structured {code, message} format."""
    err = AuthErrorResponse(
        code=AuthErrorCode.INVALID_CREDENTIALS,
        message="Wrong password",
    )
    d = err.model_dump()
    assert "code" in d
    assert "message" in d
    assert d["code"] == "invalid_credentials"


def test_auth_error_response_all_codes_serializable():
    """Every AuthErrorCode should be serializable in AuthErrorResponse."""
    for code in AuthErrorCode:
        err = AuthErrorResponse(code=code, message=f"Test {code.value}")
        d = err.model_dump()
        assert d["code"] == code.value


# ── decode_token Caller Pattern ──────────────────────────────────────


def test_decode_token_expired_maps_to_token_expired_code():
    """TokenError.EXPIRED should map to AuthErrorCode.TOKEN_EXPIRED."""
    _setup_config()
    from datetime import UTC, datetime, timedelta

    import jwt as pyjwt

    expired = {"sub": "u1", "exp": datetime.now(UTC) - timedelta(hours=1), "iat": datetime.now(UTC)}
    token = pyjwt.encode(expired, _TEST_SECRET, algorithm="HS256")
    result = decode_token(token)
    assert result == TokenError.EXPIRED

    # Verify the mapping pattern used in route handlers
    code = AuthErrorCode.TOKEN_EXPIRED if result == TokenError.EXPIRED else AuthErrorCode.TOKEN_INVALID
    assert code == AuthErrorCode.TOKEN_EXPIRED


def test_decode_token_invalid_sig_maps_to_token_invalid_code():
    """TokenError.INVALID_SIGNATURE should map to AuthErrorCode.TOKEN_INVALID."""
    _setup_config()
    from datetime import UTC, datetime, timedelta

    import jwt as pyjwt

    payload = {"sub": "u1", "exp": datetime.now(UTC) + timedelta(hours=1), "iat": datetime.now(UTC)}
    token = pyjwt.encode(payload, "wrong-key", algorithm="HS256")
    result = decode_token(token)
    assert result == TokenError.INVALID_SIGNATURE

    code = AuthErrorCode.TOKEN_EXPIRED if result == TokenError.EXPIRED else AuthErrorCode.TOKEN_INVALID
    assert code == AuthErrorCode.TOKEN_INVALID


def test_decode_token_malformed_maps_to_token_invalid_code():
    """TokenError.MALFORMED should map to AuthErrorCode.TOKEN_INVALID."""
    _setup_config()
    result = decode_token("garbage")
    assert result == TokenError.MALFORMED

    code = AuthErrorCode.TOKEN_EXPIRED if result == TokenError.EXPIRED else AuthErrorCode.TOKEN_INVALID
    assert code == AuthErrorCode.TOKEN_INVALID


# ── Login Response Format ────────────────────────────────────────────


def test_login_response_model_has_no_access_token():
    """LoginResponse should NOT contain access_token field (RFC-001)."""
    from app.gateway.routers.auth import LoginResponse

    resp = LoginResponse(expires_in=604800)
    d = resp.model_dump()
    assert "access_token" not in d
    assert "expires_in" in d
    assert d["expires_in"] == 604800


def test_login_response_model_fields():
    """LoginResponse has expires_in and needs_setup."""
    from app.gateway.routers.auth import LoginResponse

    fields = set(LoginResponse.model_fields.keys())
    assert fields == {"expires_in", "needs_setup"}


# ── AuthConfig in Route ──────────────────────────────────────────────


def test_auth_config_token_expiry_used_in_login_response():
    """LoginResponse.expires_in should come from config.token_expiry_days."""
    from app.gateway.routers.auth import LoginResponse

    expected_seconds = 14 * 24 * 3600
    resp = LoginResponse(expires_in=expected_seconds)
    assert resp.expires_in == expected_seconds


# ── UserResponse Type Preservation ───────────────────────────────────


def test_user_response_system_role_literal():
    """UserResponse.system_role should only accept 'system_admin' or 'user'."""
    from app.gateway.auth.models import UserResponse

    # Valid roles
    resp = UserResponse(id="1", email="a@b.com", system_role="system_admin")
    assert resp.system_role == "system_admin"

    resp = UserResponse(id="1", email="a@b.com", system_role="user")
    assert resp.system_role == "user"


def test_user_response_rejects_invalid_role():
    """UserResponse should reject invalid system_role values."""
    from app.gateway.auth.models import UserResponse

    with pytest.raises(ValidationError):
        UserResponse(id="1", email="a@b.com", system_role="superadmin")


# ══════════════════════════════════════════════════════════════════════
# UNHAPPY PATHS / EDGE CASES
# ══════════════════════════════════════════════════════════════════════


# ── get_current_user structured 401 responses ────────────────────────


def test_get_current_user_no_cookie_returns_not_authenticated():
    """No cookie → 401 with code=not_authenticated."""
    import asyncio

    from fastapi import HTTPException

    from app.gateway.deps import get_current_user_from_request

    mock_request = type("MockRequest", (), {"cookies": {}})()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_current_user_from_request(mock_request))
    assert exc_info.value.status_code == 401
    detail = exc_info.value.detail
    assert detail["code"] == "not_authenticated"


def test_get_current_user_expired_token_returns_token_expired():
    """Expired token → 401 with code=token_expired."""
    import asyncio

    from fastapi import HTTPException

    from app.gateway.deps import get_current_user_from_request

    _setup_config()
    expired = {"sub": "u1", "exp": datetime.now(UTC) - timedelta(hours=1), "iat": datetime.now(UTC)}
    token = pyjwt.encode(expired, _TEST_SECRET, algorithm="HS256")

    mock_request = type("MockRequest", (), {"cookies": {"access_token": token}})()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_current_user_from_request(mock_request))
    assert exc_info.value.status_code == 401
    detail = exc_info.value.detail
    assert detail["code"] == "token_expired"


def test_get_current_user_invalid_token_returns_token_invalid():
    """Bad signature → 401 with code=token_invalid."""
    import asyncio

    from fastapi import HTTPException

    from app.gateway.deps import get_current_user_from_request

    _setup_config()
    payload = {"sub": "u1", "exp": datetime.now(UTC) + timedelta(hours=1), "iat": datetime.now(UTC)}
    token = pyjwt.encode(payload, "wrong-secret", algorithm="HS256")

    mock_request = type("MockRequest", (), {"cookies": {"access_token": token}})()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_current_user_from_request(mock_request))
    assert exc_info.value.status_code == 401
    detail = exc_info.value.detail
    assert detail["code"] == "token_invalid"


def test_get_current_user_malformed_token_returns_token_invalid():
    """Garbage token → 401 with code=token_invalid."""
    import asyncio

    from fastapi import HTTPException

    from app.gateway.deps import get_current_user_from_request

    _setup_config()
    mock_request = type("MockRequest", (), {"cookies": {"access_token": "not-a-jwt"}})()
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_current_user_from_request(mock_request))
    assert exc_info.value.status_code == 401
    detail = exc_info.value.detail
    assert detail["code"] == "token_invalid"


# ── decode_token edge cases ──────────────────────────────────────────


def test_decode_token_empty_string_returns_malformed():
    _setup_config()
    result = decode_token("")
    assert result == TokenError.MALFORMED


def test_decode_token_whitespace_returns_malformed():
    _setup_config()
    result = decode_token("   ")
    assert result == TokenError.MALFORMED


# ── AuthConfig validation edge cases ─────────────────────────────────


def test_auth_config_missing_jwt_secret_raises():
    """AuthConfig requires jwt_secret — no default allowed."""
    with pytest.raises(ValidationError):
        AuthConfig()


def test_auth_config_token_expiry_zero_raises():
    """token_expiry_days must be >= 1."""
    with pytest.raises(ValidationError):
        AuthConfig(jwt_secret="secret", token_expiry_days=0)


def test_auth_config_token_expiry_31_raises():
    """token_expiry_days must be <= 30."""
    with pytest.raises(ValidationError):
        AuthConfig(jwt_secret="secret", token_expiry_days=31)


def test_auth_config_token_expiry_boundary_1_ok():
    config = AuthConfig(jwt_secret="secret", token_expiry_days=1)
    assert config.token_expiry_days == 1


def test_auth_config_token_expiry_boundary_30_ok():
    config = AuthConfig(jwt_secret="secret", token_expiry_days=30)
    assert config.token_expiry_days == 30


def test_get_auth_config_missing_env_var_generates_ephemeral(caplog):
    """get_auth_config() auto-generates ephemeral secret when AUTH_JWT_SECRET is unset."""
    import logging

    import app.gateway.auth.config as cfg

    old = cfg._auth_config
    cfg._auth_config = None
    try:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("dotenv.load_dotenv", return_value=False),
        ):
            os.environ.pop("AUTH_JWT_SECRET", None)
            with caplog.at_level(logging.WARNING):
                config = cfg.get_auth_config()
            assert config.jwt_secret
            assert any("AUTH_JWT_SECRET" in msg for msg in caplog.messages)
    finally:
        cfg._auth_config = old


# ── CSRF middleware integration (unhappy paths) ──────────────────────


def _make_csrf_app():
    """Create a minimal FastAPI app with CSRFMiddleware for testing."""
    from fastapi import HTTPException as _HTTPException
    from fastapi.responses import JSONResponse as _JSONResponse

    app = FastAPI()

    @app.exception_handler(_HTTPException)
    async def _http_exc_handler(request, exc):
        return _JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.add_middleware(CSRFMiddleware)

    @app.post("/api/v1/test/protected")
    async def protected():
        return {"ok": True}

    @app.post("/api/v1/auth/login/local")
    async def login():
        return {"ok": True}

    @app.get("/api/v1/test/read")
    async def read_endpoint():
        return {"ok": True}

    return app


def test_csrf_middleware_blocks_post_without_token():
    """POST to protected endpoint without CSRF token → 403 with structured detail."""
    client = TestClient(_make_csrf_app())
    resp = client.post("/api/v1/test/protected")
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]
    assert "missing" in resp.json()["detail"].lower()


def test_csrf_middleware_blocks_post_with_mismatched_token():
    """POST with mismatched CSRF cookie/header → 403 with mismatch detail."""
    client = TestClient(_make_csrf_app())
    client.cookies.set(CSRF_COOKIE_NAME, "token-a")
    resp = client.post(
        "/api/v1/test/protected",
        headers={CSRF_HEADER_NAME: "token-b"},
    )
    assert resp.status_code == 403
    assert "mismatch" in resp.json()["detail"].lower()


def test_csrf_middleware_allows_post_with_matching_token():
    """POST with matching CSRF cookie/header → 200."""
    client = TestClient(_make_csrf_app())
    token = secrets.token_urlsafe(64)
    client.cookies.set(CSRF_COOKIE_NAME, token)
    resp = client.post(
        "/api/v1/test/protected",
        headers={CSRF_HEADER_NAME: token},
    )
    assert resp.status_code == 200


def test_csrf_middleware_allows_get_without_token():
    """GET requests bypass CSRF check."""
    client = TestClient(_make_csrf_app())
    resp = client.get("/api/v1/test/read")
    assert resp.status_code == 200


def test_csrf_middleware_exempts_login_local():
    """POST to login/local is exempt from CSRF (no token yet)."""
    client = TestClient(_make_csrf_app())
    resp = client.post("/api/v1/auth/login/local")
    assert resp.status_code == 200


def test_csrf_middleware_sets_cookie_on_auth_endpoint():
    """Auth endpoints should receive a CSRF cookie in response."""
    client = TestClient(_make_csrf_app())
    resp = client.post("/api/v1/auth/login/local")
    assert CSRF_COOKIE_NAME in resp.cookies


# ── UserResponse edge cases ──────────────────────────────────────────


def test_user_response_missing_required_fields():
    """UserResponse with missing fields → ValidationError."""
    from app.gateway.auth.models import UserResponse

    with pytest.raises(ValidationError):
        UserResponse(id="1")  # missing email, system_role

    with pytest.raises(ValidationError):
        UserResponse(id="1", email="a@b.com")  # missing system_role


def test_user_response_empty_string_role_rejected():
    """Empty string is not a valid role."""
    from app.gateway.auth.models import UserResponse

    with pytest.raises(ValidationError):
        UserResponse(id="1", email="a@b.com", system_role="")


# ══════════════════════════════════════════════════════════════════════
# HTTP-LEVEL API CONTRACT TESTS
# ══════════════════════════════════════════════════════════════════════


def _make_auth_app():
    """Create FastAPI app with auth routes for contract testing."""
    from app.gateway.app import create_app

    return create_app()


def _get_auth_client():
    """Get TestClient for auth API contract tests."""
    assert _AUTH_CLIENT is not None
    return _AUTH_CLIENT


def test_api_auth_me_no_cookie_returns_structured_401(_persistence_engine):
    """/api/v1/auth/me without cookie → 401 with {code: 'not_authenticated'}."""
    _setup_config()
    client = _get_auth_client()
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"]["code"] == "not_authenticated"
    assert "message" in body["detail"]


def test_api_auth_me_auth_disabled_returns_synthetic_user(monkeypatch, _persistence_engine):
    _setup_config()
    monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")
    client = _get_auth_client()

    resp = client.get("/api/v1/auth/me")

    assert resp.status_code == 200
    from app.gateway.auth_disabled import AUTH_DISABLED_USER_ID

    body = resp.json()
    assert body["id"] == AUTH_DISABLED_USER_ID
    assert body["oauth_provider"] is None


def test_api_auth_me_expired_token_returns_structured_401(_persistence_engine):
    """/api/v1/auth/me with expired token → 401 with {code: 'token_expired'}."""
    _setup_config()
    expired = {"sub": "u1", "exp": datetime.now(UTC) - timedelta(hours=1), "iat": datetime.now(UTC)}
    token = pyjwt.encode(expired, _TEST_SECRET, algorithm="HS256")

    client = _get_auth_client()
    client.cookies.set("access_token", token)
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"]["code"] == "token_expired"


def test_api_auth_me_invalid_sig_returns_structured_401(_persistence_engine):
    """/api/v1/auth/me with bad signature → 401 with {code: 'token_invalid'}."""
    _setup_config()
    payload = {"sub": "u1", "exp": datetime.now(UTC) + timedelta(hours=1), "iat": datetime.now(UTC)}
    token = pyjwt.encode(payload, "wrong-key", algorithm="HS256")

    client = _get_auth_client()
    client.cookies.set("access_token", token)
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"]["code"] == "token_invalid"


def test_api_login_bad_credentials_returns_structured_401(_persistence_engine):
    """Login with wrong password → 401 with {code: 'invalid_credentials'}."""
    _setup_config()
    client = _get_auth_client()
    resp = client.post(
        "/api/v1/auth/login/local",
        data={"username": "nonexistent@test.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"]["code"] == "invalid_credentials"


def test_api_login_success_no_token_in_body(_persistence_engine):
    """Successful login → response body has expires_in but NOT access_token."""
    _setup_config()
    client = _get_auth_client()
    # Register first
    client.post(
        "/api/v1/auth/register",
        json={"email": "contract-test@test.com", "password": "securepassword123"},
    )
    # Login
    resp = client.post(
        "/api/v1/auth/login/local",
        data={"username": "contract-test@test.com", "password": "securepassword123"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "expires_in" in body
    assert "access_token" not in body
    # Token should be in cookie, not body
    assert "access_token" in resp.cookies


def test_logout_revokes_copied_session_token_across_requests(
    _persistence_engine,
):
    """Logout revokes PostgreSQL session authority, not only the browser cookie."""

    _setup_config()
    client = _get_auth_client()
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": _unique_email("logout-revoke"),
            "password": "Tr0ub4dor3a",
        },
    )
    assert response.status_code == 201
    copied_token = response.cookies["access_token"]
    assert client.get("/api/v1/auth/me").status_code == 200

    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 200

    client.cookies.set("access_token", copied_token)
    rejected = client.get("/api/v1/auth/me")
    assert rejected.status_code == 401
    assert rejected.json()["detail"]["code"] == "token_invalid"


def test_session_database_stores_hash_not_raw_jwt_sid(_persistence_engine):
    import hashlib

    from app.gateway.auth.errors import TokenError
    from app.gateway.auth.jwt import decode_token
    from app.gateway.auth.sessions import hash_session_id
    from deerflow.persistence.auth_sessions.model import AuthSessionRow
    from deerflow.persistence.engine import get_session_factory

    _setup_config()
    client = _get_auth_client()
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": _unique_email("session-hash"),
            "password": "Tr0ub4dor3a",
        },
    )
    token = response.cookies["access_token"]
    payload = decode_token(token)
    assert not isinstance(payload, TokenError)

    async def stored_hashes() -> tuple[str, ...]:
        async with get_session_factory()() as session:
            rows = await session.execute(select(AuthSessionRow.session_id_hash))
            return tuple(rows.scalars())

    hashes = client.portal.call(stored_hashes)
    assert payload.sid not in hashes
    assert hash_session_id(payload.sid) in hashes
    assert hashlib.sha256(payload.sid.encode()).hexdigest() not in hashes


def test_password_change_invalidates_every_old_session_and_issues_one_fresh(
    _persistence_engine,
):
    _setup_config()
    client = _get_auth_client()
    email = _unique_email("password-global-revoke")
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Tr0ub4dor3a"},
    )
    first_token = registered.cookies["access_token"]

    logged_in = client.post(
        "/api/v1/auth/login/local",
        data={"username": email, "password": "Tr0ub4dor3a"},
    )
    second_token = logged_in.cookies["access_token"]
    csrf_token = client.cookies[CSRF_COOKIE_NAME]

    changed = client.post(
        "/api/v1/auth/change-password",
        headers={CSRF_HEADER_NAME: csrf_token},
        json={
            "current_password": "Tr0ub4dor3a",
            "new_password": "N3w-Password-For-Sessions!",
        },
    )
    assert changed.status_code == 200
    fresh_token = changed.cookies["access_token"]

    for stale_token in (first_token, second_token):
        client.cookies.set("access_token", stale_token)
        rejected = client.get("/api/v1/auth/me")
        assert rejected.status_code == 401
        assert rejected.json()["detail"]["code"] == "token_invalid"

    client.cookies.set("access_token", fresh_token)
    assert client.get("/api/v1/auth/me").status_code == 200


def test_register_session_write_failure_is_503_and_account_can_retry_login(
    monkeypatch,
    _persistence_engine,
):
    from app.gateway.auth.sessions import AuthSessionUnavailable
    from app.gateway.routers import auth

    _setup_config()
    client = _get_auth_client()
    email = _unique_email("register-session-retry")
    real_issuer = auth.issue_access_session
    failing_issuer = AsyncMock(side_effect=AuthSessionUnavailable())
    monkeypatch.setattr(auth, "issue_access_session", failing_issuer)

    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Tr0ub4dor3a"},
    )
    assert registered.status_code == 503
    assert registered.json()["detail"] == {
        "code": "DATABASE_UNAVAILABLE",
        "message": "Authentication storage unavailable",
    }
    assert "access_token" not in registered.cookies

    monkeypatch.setattr(auth, "issue_access_session", real_issuer)
    logged_in = client.post(
        "/api/v1/auth/login/local",
        data={"username": email, "password": "Tr0ub4dor3a"},
    )
    assert logged_in.status_code == 200
    assert "access_token" in logged_in.cookies


def test_api_register_duplicate_returns_structured_400(_persistence_engine):
    """Register with duplicate email → 400 with {code: 'email_already_exists'}."""
    _setup_config()
    client = _get_auth_client()
    email = "dup-contract-test@test.com"
    # First register
    client.post("/api/v1/auth/register", json={"email": email, "password": "Tr0ub4dor3a"})
    # Duplicate
    resp = client.post("/api/v1/auth/register", json={"email": email, "password": "AnotherStr0ngPwd!"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "email_already_exists"


def test_email_identity_is_case_insensitive_across_register_and_login(
    _persistence_engine,
):
    """One email address must resolve to exactly one account, regardless of case."""

    _setup_config()
    client = _get_auth_client()
    mixed_case = f"Case-{secrets.token_hex(4)}@Example.COM"
    canonical = mixed_case.lower()

    registered = client.post(
        "/api/v1/auth/register",
        json={"email": mixed_case, "password": "Tr0ub4dor3a"},
    )
    assert registered.status_code == 201
    assert registered.json()["email"] == canonical

    duplicate = client.post(
        "/api/v1/auth/register",
        json={"email": canonical.upper(), "password": "AnotherStr0ngPwd!"},
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["detail"]["code"] == "email_already_exists"

    logged_in = client.post(
        "/api/v1/auth/login/local",
        data={"username": canonical.upper(), "password": "Tr0ub4dor3a"},
    )
    assert logged_in.status_code == 200


def test_registration_gate_rejects_before_account_creation(
    monkeypatch,
    _persistence_engine,
):
    """Closing local self-registration must not persist the denied account."""

    from app.gateway.routers import auth as auth_router

    _setup_config()
    client = _get_auth_client()
    email = _unique_email("registration-gate")

    from app.system_runtime_settings import AuthPolicyValue

    materialize = AsyncMock(
        side_effect=(AuthPolicyValue(allow_registration=False), AuthPolicyValue()),
    )
    client.app.state.system_runtime_policy_materializer = SimpleNamespace(
        materialize_current=materialize,
    )
    denied = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Tr0ub4dor3a"},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "registration_disabled"

    accepted = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Tr0ub4dor3a"},
    )
    assert accepted.status_code == 201
    assert materialize.await_count == 2
    auth_router._SETUP_STATUS_CACHE.clear()


@pytest.mark.parametrize("allowed", [True, False])
def test_setup_status_reports_registration_gate(
    monkeypatch,
    _persistence_engine,
    allowed,
):
    from app.gateway.routers import auth as auth_router

    _setup_config()
    client = _get_auth_client()
    auth_router._SETUP_STATUS_CACHE.clear()
    from app.system_runtime_settings import AuthPolicyValue

    materialize = AsyncMock(
        return_value=AuthPolicyValue(allow_registration=allowed),
    )
    client.app.state.system_runtime_policy_materializer = SimpleNamespace(
        materialize_current=materialize,
    )

    response = client.get("/api/v1/auth/setup-status")

    assert response.status_code == 200
    assert response.json()["registration_enabled"] is allowed
    materialize.assert_awaited_once()


def test_setup_status_rechecks_registration_gate_when_initialized_state_is_cached(
    monkeypatch,
    _persistence_engine,
):
    """Hot-reloaded registration policy must not be hidden by the setup cache."""

    from app.gateway.routers import auth as auth_router

    _setup_config()
    client = _get_auth_client()
    auth_router._SETUP_STATUS_CACHE.clear()
    provider = SimpleNamespace(
        count_admin_users=AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        auth_router,
        "get_local_provider",
        lambda: provider,
    )

    from app.system_runtime_settings import AuthPolicyValue

    materialize = AsyncMock(
        side_effect=(
            AuthPolicyValue(allow_registration=True),
            AuthPolicyValue(allow_registration=False),
        ),
    )
    client.app.state.system_runtime_policy_materializer = SimpleNamespace(
        materialize_current=materialize,
    )
    first = client.get("/api/v1/auth/setup-status")
    assert first.status_code == 200
    assert first.json() == {
        "needs_setup": False,
        "registration_enabled": True,
    }

    second = client.get("/api/v1/auth/setup-status")
    assert second.status_code == 200
    assert second.json() == {
        "needs_setup": False,
        "registration_enabled": False,
    }
    assert provider.count_admin_users.await_count == 1
    assert materialize.await_count == 2


def test_local_registration_defaults_to_enabled():
    from deerflow.config.auth_config import AuthAppConfig, LocalAuthConfig

    assert LocalAuthConfig().allow_registration is True
    assert AuthAppConfig().local.allow_registration is True


@pytest.mark.anyio
async def test_registration_policy_is_materialized_from_current_database_value() -> None:
    from fastapi import Request

    from app.gateway.routers import auth as auth_router
    from app.system_runtime_settings import (
        AuthPolicyValue,
        RuntimePolicySection,
    )

    materialize = AsyncMock(
        return_value=AuthPolicyValue(allow_registration=False),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            system_runtime_policy_materializer=SimpleNamespace(
                materialize_current=materialize,
            ),
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/setup-status",
            "headers": [],
            "app": app,
        }
    )

    assert await auth_router._local_registration_enabled(request) is False
    materialize.assert_awaited_once_with(RuntimePolicySection.AUTH)


@pytest.mark.anyio
async def test_registration_policy_failure_is_a_secret_free_503() -> None:
    from fastapi import HTTPException, Request

    from app.gateway.routers import auth as auth_router

    materialize = AsyncMock(
        side_effect=RuntimeError("postgresql://owner:password@db/private"),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            system_runtime_policy_materializer=SimpleNamespace(
                materialize_current=materialize,
            ),
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/setup-status",
            "headers": [],
            "app": app,
        }
    )

    with pytest.raises(HTTPException) as raised:
        await auth_router._local_registration_enabled(request)

    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "code": "AUTH_POLICY_UNAVAILABLE",
        "message": "Authentication policy unavailable",
    }
    assert "password" not in str(raised.value.detail)


# ── Cookie security: HTTP vs HTTPS ────────────────────────────────────


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}@test.com"


def _get_set_cookie_headers(resp) -> list[str]:
    """Extract all set-cookie header values from a TestClient response."""
    return [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]


def test_register_http_cookie_httponly_true_secure_false(_persistence_engine):
    """HTTP register → access_token cookie is httponly=True, secure=False, no max_age."""
    _setup_config()
    client = _get_auth_client()
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": _unique_email("http-cookie"), "password": "Tr0ub4dor3a"},
    )
    assert resp.status_code == 201
    cookie_header = resp.headers.get("set-cookie", "")
    assert "access_token=" in cookie_header
    assert "httponly" in cookie_header.lower()
    assert "secure" not in cookie_header.lower().replace("samesite", "")


def test_register_https_cookie_httponly_true_secure_true(_persistence_engine):
    """HTTPS register (x-forwarded-proto) → access_token cookie is httponly=True, secure=True, has max_age."""
    _setup_config()
    client = _get_auth_client()
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": _unique_email("https-cookie"), "password": "Tr0ub4dor3a"},
        headers={"x-forwarded-proto": "https"},
    )
    assert resp.status_code == 201
    cookie_header = resp.headers.get("set-cookie", "")
    assert "access_token=" in cookie_header
    assert "httponly" in cookie_header.lower()
    assert "secure" in cookie_header.lower()
    assert "max-age" in cookie_header.lower()


def test_remember_false_keeps_access_and_csrf_cookies_session_only(
    _persistence_engine,
):
    _setup_config()
    client = _get_auth_client()
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": _unique_email("remember-false"),
            "password": "Tr0ub4dor3a",
            "remember_me": False,
        },
        headers={"host": "localhost:2026"},
    )
    assert response.status_code == 201

    cookies = _get_set_cookie_headers(response)
    access = next(value for value in cookies if "access_token=" in value)
    csrf = next(value for value in cookies if "csrf_token=" in value)
    preference = next(value for value in cookies if "deerflow_session_persistent=" in value)
    assert "max-age" not in access.lower()
    assert "max-age" not in csrf.lower()
    assert "max-age" not in preference.lower()
    assert "deerflow_session_persistent=0" in preference


def test_remember_true_allows_persistent_cookies_on_localhost_http(
    _persistence_engine,
):
    _setup_config()
    client = _get_auth_client()
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": _unique_email("remember-localhost"),
            "password": "Tr0ub4dor3a",
            "remember_me": True,
        },
        headers={"host": "localhost:2026"},
    )
    assert response.status_code == 201

    cookies = _get_set_cookie_headers(response)
    for name in (
        "access_token=",
        "csrf_token=",
        "deerflow_session_persistent=",
    ):
        cookie = next(value for value in cookies if name in value)
        assert "max-age" in cookie.lower()


def test_remember_true_stays_session_only_on_public_http(
    _persistence_engine,
):
    _setup_config()
    client = _get_auth_client()
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": _unique_email("remember-public-http"),
            "password": "Tr0ub4dor3a",
            "remember_me": True,
        },
        headers={"host": "public.example.com"},
    )
    assert response.status_code == 201

    cookies = _get_set_cookie_headers(response)
    for name in (
        "access_token=",
        "csrf_token=",
        "deerflow_session_persistent=",
    ):
        cookie = next(value for value in cookies if name in value)
        assert "max-age" not in cookie.lower()


def test_change_password_reissues_access_preference_and_csrf_with_one_lifetime(
    _persistence_engine,
):
    """Changing remember-me policy must keep the double-submit pair aligned."""

    _setup_config()
    client = _get_auth_client()
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": _unique_email("change-password-remember"),
            "password": "Tr0ub4dor3a",
            "remember_me": False,
        },
        headers={"host": "localhost:2026"},
    )
    assert registered.status_code == 201
    csrf_token = client.cookies[CSRF_COOKIE_NAME]

    changed = client.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "Tr0ub4dor3a",
            "new_password": "N3w-Password-For-Cookies!",
            "remember_me": True,
        },
        headers={
            "host": "localhost:2026",
            CSRF_HEADER_NAME: csrf_token,
        },
    )
    assert changed.status_code == 200

    cookies = _get_set_cookie_headers(changed)
    for name in (
        "access_token=",
        "csrf_token=",
        "deerflow_session_persistent=",
    ):
        cookie = next(value for value in cookies if name in value)
        assert "max-age" in cookie.lower()


def test_logout_revokes_and_deletes_all_browser_session_cookies(
    _persistence_engine,
):
    _setup_config()
    client = _get_auth_client()
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": _unique_email("logout-all-cookies"),
            "password": "Tr0ub4dor3a",
            "remember_me": True,
        },
        headers={"host": "localhost:2026"},
    )
    assert registered.status_code == 201

    logout = client.post(
        "/api/v1/auth/logout",
        headers={"host": "localhost:2026"},
    )
    assert logout.status_code == 200
    cookies = _get_set_cookie_headers(logout)
    for name in (
        "access_token=",
        "csrf_token=",
        "deerflow_session_persistent=",
    ):
        deleted = [value for value in cookies if name in value]
        assert len(deleted) == 1
        assert "max-age=0" in deleted[0].lower()


def test_login_https_sets_secure_cookie(_persistence_engine):
    """HTTPS login → access_token cookie has secure flag."""
    _setup_config()
    client = _get_auth_client()
    email = _unique_email("https-login")
    client.post("/api/v1/auth/register", json={"email": email, "password": "Tr0ub4dor3a"})
    resp = client.post(
        "/api/v1/auth/login/local",
        data={"username": email, "password": "Tr0ub4dor3a"},
        headers={"x-forwarded-proto": "https"},
    )
    assert resp.status_code == 200
    cookie_header = resp.headers.get("set-cookie", "")
    assert "access_token=" in cookie_header
    assert "httponly" in cookie_header.lower()
    assert "secure" in cookie_header.lower()


def test_csrf_cookie_secure_on_https(_persistence_engine):
    """HTTPS register → csrf_token cookie has secure flag but NOT httponly."""
    _setup_config()
    client = _get_auth_client()
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": _unique_email("csrf-https"), "password": "Tr0ub4dor3a"},
        headers={"x-forwarded-proto": "https"},
    )
    assert resp.status_code == 201
    csrf_cookies = [h for h in _get_set_cookie_headers(resp) if "csrf_token=" in h]
    assert csrf_cookies, "csrf_token cookie not set on HTTPS register"
    csrf_header = csrf_cookies[0]
    assert "secure" in csrf_header.lower()
    assert "httponly" not in csrf_header.lower()


def test_csrf_cookie_not_secure_on_http(_persistence_engine):
    """HTTP register → csrf_token cookie does NOT have secure flag."""
    _setup_config()
    client = _get_auth_client()
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": _unique_email("csrf-http"), "password": "Tr0ub4dor3a"},
    )
    assert resp.status_code == 201
    csrf_cookies = [h for h in _get_set_cookie_headers(resp) if "csrf_token=" in h]
    assert csrf_cookies, "csrf_token cookie not set on HTTP register"
    csrf_header = csrf_cookies[0]
    assert "secure" not in csrf_header.lower().replace("samesite", "")


def test_csrf_cookie_persistent_on_https(_persistence_engine):
    """HTTPS register → csrf_token cookie is persistent (has max_age), like access_token.

    Regression for iOS Safari home-screen PWAs. When iOS terminates a
    standalone web app it evicts *session* cookies but keeps *persistent*
    ones. The access_token cookie is persistent over HTTPS (carries
    max_age), so the user still appears logged in after reopening — but a
    session-only csrf_token cookie is dropped, so the first state-changing
    request fails with 403 "CSRF token missing. Include X-CSRF-Token
    header." The two cookies represent one session and must share a lifetime.
    """
    _setup_config()
    client = _get_auth_client()
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": _unique_email("csrf-persist"), "password": "Tr0ub4dor3a"},
        headers={"x-forwarded-proto": "https"},
    )
    assert resp.status_code == 201
    set_cookies = _get_set_cookie_headers(resp)
    csrf_cookies = [h for h in set_cookies if "csrf_token=" in h]
    assert csrf_cookies, "csrf_token cookie not set on HTTPS register"
    assert "max-age" in csrf_cookies[0].lower(), "csrf_token must be persistent over HTTPS so iOS PWAs don't drop it as a session cookie"
    # It must pair with the access_token's lifetime: both persistent on HTTPS.
    access_cookies = [h for h in set_cookies if "access_token=" in h]
    assert access_cookies and "max-age" in access_cookies[0].lower()


def test_csrf_cookie_session_only_on_http(_persistence_engine):
    """HTTP register → csrf_token cookie has NO max_age (session cookie).

    Mirrors the access_token's ``... if is_https else None`` guard so the
    pair stays symmetric: persistent together over HTTPS, session-only
    together over plain HTTP (local dev). Keeping them in lockstep is what
    avoids the "logged in but csrf_token gone" state.
    """
    _setup_config()
    client = _get_auth_client()
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": _unique_email("csrf-session"), "password": "Tr0ub4dor3a"},
    )
    assert resp.status_code == 201
    csrf_cookies = [h for h in _get_set_cookie_headers(resp) if "csrf_token=" in h]
    assert csrf_cookies, "csrf_token cookie not set on HTTP register"
    assert "max-age" not in csrf_cookies[0].lower()


def test_oidc_callback_csrf_cookie_persistent_on_https(_persistence_engine):
    """The OIDC-callback CSRF cookie helper is persistent over HTTPS too.

    ``routers.auth._set_csrf_cookie`` is the second place a csrf_token cookie
    is minted (GET OIDC callback, which CSRFMiddleware does not cover). It has
    the same session-vs-persistent asymmetry and the same iOS PWA failure
    mode, so it must also carry max_age over HTTPS.
    """
    from starlette.requests import Request
    from starlette.responses import Response

    from app.gateway.routers.auth import _set_csrf_cookie

    _setup_config()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/auth/callback/example",
        "headers": [(b"x-forwarded-proto", b"https")],
        "scheme": "http",
        "server": ("internal", 8000),
        "query_string": b"",
    }
    response = Response()
    _set_csrf_cookie(response, Request(scope))
    set_cookie = response.headers.get("set-cookie", "").lower()
    assert "csrf_token=" in set_cookie
    assert "secure" in set_cookie
    assert "max-age" in set_cookie
