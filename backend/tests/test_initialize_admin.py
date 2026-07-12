"""Tests for the POST /api/v1/auth/initialize endpoint.

Covers: first-boot admin creation, rejection when system already
initialized, password strength validation,
and public accessibility (no auth cookie required).
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InvalidRequestError

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-key-initialize-admin-min-32")

from app.gateway.auth.config import AuthConfig, set_auth_config

_TEST_SECRET = "test-secret-key-initialize-admin-min-32"


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest_asyncio.fixture()
async def _setup_auth(migrated_postgres_database_url):
    """Reset auth state and provide one migrated PostgreSQL database."""
    from app.gateway import deps
    from app.gateway.routers.auth import _SETUP_STATUS_CACHE, _SETUP_STATUS_INFLIGHT

    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
    deps._cached_local_provider = None
    deps._cached_repo = None
    _SETUP_STATUS_CACHE.clear()
    _SETUP_STATUS_INFLIGHT.clear()
    try:
        yield migrated_postgres_database_url
    finally:
        deps._cached_local_provider = None
        deps._cached_repo = None
        _SETUP_STATUS_CACHE.clear()
        _SETUP_STATUS_INFLIGHT.clear()


@pytest.fixture()
def client(_setup_auth):
    from app.gateway.app import create_app
    from app.gateway.auth.config import AuthConfig, set_auth_config

    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
    app = create_app()
    app.router.lifespan_context = _noop_lifespan

    from deerflow.config.database_config import DatabaseConfig
    from deerflow.persistence.engine import close_engine, init_engine

    with TestClient(app) as test_client:
        assert test_client.portal is not None
        test_client.portal.call(init_engine, DatabaseConfig(url=_setup_auth))
        try:
            yield test_client
        finally:
            test_client.portal.call(close_engine)


def _init_payload(**extra):
    """Build a valid /initialize payload."""
    return {
        "email": "admin@example.com",
        "password": "Str0ng!Pass99",
        **extra,
    }


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/v1/auth/initialize", "headers": []})


@pytest.mark.asyncio
async def test_initialize_lock_uses_autocommit_and_releases_on_body_error(monkeypatch):
    from app.gateway.routers import auth
    from app.projects.bootstrap import _BOOTSTRAP_LOCK_KEY

    assert auth._INITIALIZE_ADMIN_LOCK_KEY != _BOOTSTRAP_LOCK_KEY

    connection = AsyncMock()
    connection.execution_options.return_value = connection
    connection.scalar.return_value = True
    context = AsyncMock()
    context.__aenter__.return_value = connection
    lock_engine = MagicMock()
    lock_engine.connect.return_value = context
    lock_engine.dispose = AsyncMock()
    runtime_engine = MagicMock()
    monkeypatch.setattr(auth, "get_engine", lambda: runtime_engine)
    factory = MagicMock(return_value=lock_engine)
    monkeypatch.setattr(auth, "create_async_engine", factory)

    class BodyError(Exception):
        pass

    with pytest.raises(BodyError):
        async with auth._initialize_admin_lock():
            raise BodyError

    assert factory.call_args.kwargs["isolation_level"] == "AUTOCOMMIT"
    assert factory.call_args.kwargs["poolclass"] is auth.NullPool
    assert any("pg_advisory_lock" in str(call.args[0]) for call in connection.execute.await_args_list)
    assert "pg_advisory_unlock" in str(connection.scalar.await_args.args[0])
    context.__aexit__.assert_awaited_once()
    lock_engine.dispose.assert_awaited_once()
    connection.begin.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_lock_dbapi_failure_is_sanitized_and_closes_connection(monkeypatch):
    from app.gateway.routers import auth
    from app.projects.errors import ProjectDatabaseUnavailable

    connection = AsyncMock()
    connection.execution_options.return_value = connection
    connection.execute.side_effect = DBAPIError(
        "SELECT pg_advisory_lock secret",
        {"url": "postgresql://owner:password@db/private"},
        Exception("driver failed"),
        False,
    )
    context = AsyncMock()
    context.__aenter__.return_value = connection
    lock_engine = MagicMock()
    lock_engine.connect.return_value = context
    lock_engine.dispose = AsyncMock()
    monkeypatch.setattr(auth, "get_engine", MagicMock())
    monkeypatch.setattr(auth, "create_async_engine", MagicMock(return_value=lock_engine))

    with pytest.raises(ProjectDatabaseUnavailable) as exc_info:
        async with auth._initialize_admin_lock():
            pass
    assert str(exc_info.value) == "Project storage unavailable"
    assert "postgresql" not in str(exc_info.value)
    context.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_initialize_lock_does_not_disguise_programming_error(monkeypatch):
    from app.gateway.routers import auth

    connection = AsyncMock()
    connection.execution_options.return_value = connection
    connection.execute.side_effect = InvalidRequestError("programming misuse")
    context = AsyncMock()
    context.__aenter__.return_value = connection
    lock_engine = MagicMock()
    lock_engine.connect.return_value = context
    lock_engine.dispose = AsyncMock()
    monkeypatch.setattr(auth, "get_engine", MagicMock())
    monkeypatch.setattr(auth, "create_async_engine", MagicMock(return_value=lock_engine))

    with pytest.raises(InvalidRequestError, match="programming misuse"):
        async with auth._initialize_admin_lock():
            pass
    context.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_initialize_unavailable_engine_is_503_before_admin_query(monkeypatch):
    from app.gateway.routers import auth

    provider = AsyncMock()
    monkeypatch.setattr(auth, "get_local_provider", lambda: provider)
    monkeypatch.setattr(auth, "get_engine", lambda: None)
    with pytest.raises(HTTPException) as exc_info:
        await auth.initialize_admin(_request(), Response(), auth.InitializeAdminRequest(**_init_payload()))
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "DATABASE_UNAVAILABLE"
    provider.count_admin_users.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialize_uninitialized_session_factory_is_stable_503(monkeypatch):
    from app.gateway.auth.models import User
    from app.gateway.routers import auth

    @asynccontextmanager
    async def acquired_lock():
        yield

    provider = AsyncMock()
    provider.count_admin_users.return_value = 0
    provider.create_user.return_value = User(id=uuid.uuid4(), email="admin@example.com", system_role="system_admin")
    monkeypatch.setattr(auth, "get_local_provider", lambda: provider)
    monkeypatch.setattr(auth, "_initialize_admin_lock", acquired_lock)

    def unavailable_factory():
        raise RuntimeError("Persistence engine is not initialized")

    monkeypatch.setattr(auth, "get_session_factory", unavailable_factory)
    with pytest.raises(HTTPException) as exc_info:
        await auth.initialize_admin(_request(), Response(), auth.InitializeAdminRequest(**_init_payload()))
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "DATABASE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_initialize_unlock_failure_does_not_override_body_error(monkeypatch):
    from app.gateway.routers import auth

    connection = AsyncMock()
    connection.scalar.side_effect = DBAPIError("unlock", {}, Exception("lost"), False)
    context = AsyncMock()
    context.__aenter__.return_value = connection
    lock_engine = MagicMock()
    lock_engine.connect.return_value = context
    lock_engine.dispose = AsyncMock()
    monkeypatch.setattr(auth, "get_engine", MagicMock())
    monkeypatch.setattr(auth, "create_async_engine", MagicMock(return_value=lock_engine))

    class BodyError(Exception):
        pass

    with pytest.raises(BodyError):
        async with auth._initialize_admin_lock():
            raise BodyError
    connection.invalidate.assert_awaited_once()
    lock_engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_initialize_lock_is_not_sticky_after_failed_flow(
    migrated_postgres_database_url,
):
    from app.gateway.routers.auth import _initialize_admin_lock
    from deerflow.config.database_config import DatabaseConfig
    from deerflow.persistence.engine import close_engine, init_engine

    await init_engine(DatabaseConfig(url=migrated_postgres_database_url))

    class FailedFlow(Exception):
        pass

    try:
        with pytest.raises(FailedFlow):
            async with _initialize_admin_lock():
                raise FailedFlow

        async def acquire_again():
            async with _initialize_admin_lock():
                return True

        assert await asyncio.wait_for(acquire_again(), timeout=2) is True
    finally:
        await close_engine()


# ── Happy path ────────────────────────────────────────────────────────────


def test_initialize_creates_admin_and_sets_cookie(client):
    """POST /initialize when no admin exists → 201, session cookie set."""
    resp = client.post("/api/v1/auth/initialize", json=_init_payload())
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "admin@example.com"
    assert data["system_role"] == "system_admin"
    assert "access_token" in resp.cookies


def test_initialize_needs_setup_false(client):
    """Newly created admin via /initialize has needs_setup=False."""
    client.post("/api/v1/auth/initialize", json=_init_payload())
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["needs_setup"] is False


def test_initialize_bootstraps_default_project_and_real_csrf_flow(client):
    initialized = client.post("/api/v1/auth/initialize", json=_init_payload())
    assert initialized.status_code == 201

    projects = client.get("/api/projects")
    assert projects.status_code == 200
    assert [item["slug"] for item in projects.json()["items"]] == ["default-project"]
    assert projects.json()["items"][0]["role"] == "admin"

    missing_csrf = client.post("/api/projects", json={"slug": "csrf-project", "display_name": "CSRF"})
    assert missing_csrf.status_code == 403
    csrf_token = client.cookies.get("csrf_token")
    created = client.post(
        "/api/projects",
        json={"slug": "csrf-project", "display_name": "CSRF"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert created.status_code == 201
    assert created.json()["slug"] == "csrf-project"


def test_register_does_not_grant_default_project_membership(client):
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": "regular@example.com", "password": "Tr0ub4dor3a"},
    )
    assert registered.status_code == 201
    projects = client.get("/api/projects")
    assert projects.status_code == 200
    assert projects.json() == {"items": [], "next_cursor": None}


def test_initialize_bootstrap_failure_is_sanitized_and_does_not_issue_session(client, monkeypatch):
    from app.projects.errors import ProjectBootstrapFailed

    bootstrap = AsyncMock(side_effect=ProjectBootstrapFailed("AMBIGUOUS_BOOTSTRAP_ADMIN"))
    monkeypatch.setattr("app.projects.bootstrap.bootstrap_default_project", bootstrap)
    response = client.post("/api/v1/auth/initialize", json=_init_payload())
    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "AMBIGUOUS_BOOTSTRAP_ADMIN",
            "message": "Project bootstrap failed",
        }
    }
    assert "access_token" not in response.cookies
    assert "postgresql" not in response.text
    bootstrap.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_initialize_creates_exactly_one_admin_and_default_project(
    migrated_postgres_database_url,
):
    from app.gateway import deps
    from app.gateway.app import create_app
    from app.gateway.auth.config import AuthConfig, set_auth_config
    from app.gateway.routers.auth import _SETUP_STATUS_CACHE, _SETUP_STATUS_INFLIGHT
    from deerflow.config.database_config import DatabaseConfig
    from deerflow.persistence.engine import close_engine, get_engine, init_engine

    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
    deps._cached_local_provider = None
    deps._cached_repo = None
    _SETUP_STATUS_CACHE.clear()
    _SETUP_STATUS_INFLIGHT.clear()
    await init_engine(DatabaseConfig(url=migrated_postgres_database_url))
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    payloads = (
        {"email": "first-admin@example.com", "password": "Str0ng!First99"},
        {"email": "second-admin@example.com", "password": "Str0ng!Second99"},
    )
    try:
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as first,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as second,
        ):
            responses = await asyncio.gather(
                first.post("/api/v1/auth/initialize", json=payloads[0]),
                second.post("/api/v1/auth/initialize", json=payloads[1]),
            )
            assert sorted(response.status_code for response in responses) == [201, 409]
            winner_index = 0 if responses[0].status_code == 201 else 1
            loser_index = 1 - winner_index
            assert responses[loser_index].json()["detail"]["code"] == "system_already_initialized"

            engine = get_engine()
            assert engine is not None
            async with engine.connect() as connection:
                assert (await connection.scalar(text("SELECT count(*) FROM users WHERE system_role='system_admin'"))) == 1
                assert (await connection.scalar(text("SELECT count(*) FROM projects WHERE slug='default-project'"))) == 1
                assert (
                    await connection.scalar(
                        text(
                            """SELECT count(*) FROM project_memberships m
                            JOIN projects p ON p.id=m.project_id
                            WHERE p.slug='default-project' AND m.role='admin'"""
                        )
                    )
                ) == 1
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM users WHERE email=:email"),
                        {"email": payloads[loser_index]["email"]},
                    )
                ) == 0

            loser_login = await second.post(
                "/api/v1/auth/login",
                data={
                    "username": payloads[loser_index]["email"],
                    "password": payloads[loser_index]["password"],
                },
            )
            assert loser_login.status_code == 401
    finally:
        await close_engine()
        deps._cached_local_provider = None
        deps._cached_repo = None
        _SETUP_STATUS_CACHE.clear()
        _SETUP_STATUS_INFLIGHT.clear()


@pytest.mark.asyncio
async def test_failed_initialize_can_recover_through_real_setup_bootstrap(migrated_postgres_database_url, monkeypatch):
    from app.gateway import deps
    from app.gateway.app import create_app
    from app.gateway.auth.config import AuthConfig, set_auth_config
    from app.gateway.routers.auth import _SETUP_STATUS_CACHE, _SETUP_STATUS_INFLIGHT
    from app.projects.errors import ProjectBootstrapFailed
    from deerflow.config.database_config import DatabaseConfig
    from deerflow.persistence.engine import close_engine, get_engine, init_engine
    from scripts.setup_postgres import _bootstrap_default_project_schema

    set_auth_config(AuthConfig(jwt_secret=_TEST_SECRET))
    deps._cached_local_provider = None
    deps._cached_repo = None
    _SETUP_STATUS_CACHE.clear()
    _SETUP_STATUS_INFLIGHT.clear()
    await init_engine(DatabaseConfig(url=migrated_postgres_database_url))
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    payload = _init_payload()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            from app.projects import bootstrap as bootstrap_module

            real_bootstrap = bootstrap_module.bootstrap_default_project
            monkeypatch.setattr(
                bootstrap_module,
                "bootstrap_default_project",
                AsyncMock(side_effect=ProjectBootstrapFailed("DEFAULT_PROJECT_CONFLICT")),
            )
            failed = await client.post("/api/v1/auth/initialize", json=payload)
            assert failed.status_code == 503
            assert failed.json()["detail"]["code"] == "DEFAULT_PROJECT_CONFLICT"
            assert "access_token" not in failed.cookies

            engine = get_engine()
            assert engine is not None
            async with engine.connect() as connection:
                assert (await connection.scalar(text("SELECT count(*) FROM users WHERE system_role='system_admin'"))) == 1
                assert (await connection.scalar(text("SELECT count(*) FROM projects"))) == 0

            monkeypatch.setattr(bootstrap_module, "bootstrap_default_project", real_bootstrap)
            await _bootstrap_default_project_schema(engine)

            login = await client.post(
                "/api/v1/auth/login",
                data={"username": payload["email"], "password": payload["password"]},
            )
            assert login.status_code == 200
            projects = await client.get("/api/projects")
            assert projects.status_code == 200
            assert [item["slug"] for item in projects.json()["items"]] == ["default-project"]
    finally:
        await close_engine()
        deps._cached_local_provider = None
        deps._cached_repo = None
        _SETUP_STATUS_CACHE.clear()
        _SETUP_STATUS_INFLIGHT.clear()


# ── Rejection when already initialized ───────────────────────────────────


def test_initialize_rejected_when_admin_exists(client):
    """Second call to /initialize after admin exists → 409 system_already_initialized."""
    client.post("/api/v1/auth/initialize", json=_init_payload())
    resp2 = client.post(
        "/api/v1/auth/initialize",
        json={**_init_payload(), "email": "other@example.com"},
    )
    assert resp2.status_code == 409
    body = resp2.json()
    assert body["detail"]["code"] == "system_already_initialized"


def test_initialize_register_does_not_block_initialization(client):
    """/register creating a user before /initialize doesn't block admin creation."""
    # Register a regular user first
    client.post("/api/v1/auth/register", json={"email": "regular@example.com", "password": "Tr0ub4dor3a"})
    # /initialize should still succeed (checks admin_count, not total user_count)
    resp = client.post("/api/v1/auth/initialize", json=_init_payload())
    assert resp.status_code == 201
    assert resp.json()["system_role"] == "system_admin"


def test_initialize_existing_regular_user_email_reports_email_conflict(client):
    """With no admin, reusing a regular user's email is an email conflict, not initialized."""
    client.post("/api/v1/auth/register", json={"email": "regular@example.com", "password": "Tr0ub4dor3a"})

    resp = client.post(
        "/api/v1/auth/initialize",
        json={**_init_payload(), "email": "regular@example.com"},
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "email_already_exists"
    assert client.get("/api/v1/auth/setup-status").json()["needs_setup"] is True


# ── Endpoint is public (no cookie required) ───────────────────────────────


def test_initialize_accessible_without_cookie(client):
    """No access_token cookie needed for /initialize."""
    resp = client.post(
        "/api/v1/auth/initialize",
        json=_init_payload(),
        cookies={},
    )
    assert resp.status_code == 201


# ── Password validation ───────────────────────────────────────────────────


def test_initialize_rejects_short_password(client):
    """Password shorter than 8 chars → 422."""
    resp = client.post(
        "/api/v1/auth/initialize",
        json={**_init_payload(), "password": "short"},
    )
    assert resp.status_code == 422


def test_initialize_rejects_common_password(client):
    """Common password → 422."""
    resp = client.post(
        "/api/v1/auth/initialize",
        json={**_init_payload(), "password": "password123"},
    )
    assert resp.status_code == 422


# ── setup-status reflects initialization ─────────────────────────────────


def test_setup_status_before_initialization(client):
    """setup-status returns needs_setup=True before /initialize is called."""
    resp = client.get("/api/v1/auth/setup-status")
    assert resp.status_code == 200
    assert resp.json()["needs_setup"] is True


def test_setup_status_after_initialization(client):
    """setup-status returns needs_setup=False after /initialize succeeds."""
    client.post("/api/v1/auth/initialize", json=_init_payload())
    resp = client.get("/api/v1/auth/setup-status")
    assert resp.status_code == 200
    assert resp.json()["needs_setup"] is False


def test_setup_status_true_when_only_regular_user_exists(client):
    """setup-status returns needs_setup=True even when regular users exist (no admin)."""
    client.post("/api/v1/auth/register", json={"email": "regular@example.com", "password": "Tr0ub4dor3a"})
    resp = client.get("/api/v1/auth/setup-status")
    assert resp.status_code == 200
    assert resp.json()["needs_setup"] is True


def test_setup_status_returns_cached_result_on_rapid_calls(client):
    """Rapid /setup-status calls return the cached result (200) instead of 429."""
    client.post("/api/v1/auth/initialize", json=_init_payload())

    # First call succeeds and computes the result.
    resp1 = client.get("/api/v1/auth/setup-status")
    assert resp1.status_code == 200

    # Immediate second call returns cached result, not 429.
    resp2 = client.get("/api/v1/auth/setup-status")
    assert resp2.status_code == 200
    assert resp2.json() == resp1.json()
    assert resp2.json()["needs_setup"] is False


def test_setup_status_does_not_return_stale_true_after_initialize(client):
    """A pre-initialize setup-status response should not stay cached as True."""
    before = client.get("/api/v1/auth/setup-status")
    assert before.status_code == 200
    assert before.json()["needs_setup"] is True

    init = client.post("/api/v1/auth/initialize", json=_init_payload())
    assert init.status_code == 201

    after = client.get("/api/v1/auth/setup-status")
    assert after.status_code == 200
    assert after.json()["needs_setup"] is False


@pytest.mark.asyncio
async def test_setup_status_single_flight_per_ip(monkeypatch):
    """Concurrent requests from same IP share one in-flight DB query."""
    from starlette.requests import Request

    from app.gateway.routers.auth import (
        _SETUP_STATUS_CACHE,
        _SETUP_STATUS_INFLIGHT,
        setup_status,
    )

    class _Provider:
        def __init__(self):
            self.calls = 0

        async def count_admin_users(self):
            self.calls += 1
            await asyncio.sleep(0.05)
            return 0

    provider = _Provider()
    monkeypatch.setattr("app.gateway.routers.auth.get_local_provider", lambda: provider)
    _SETUP_STATUS_CACHE.clear()
    _SETUP_STATUS_INFLIGHT.clear()

    def _request() -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/auth/setup-status",
                "headers": [],
                "client": ("127.0.0.1", 12345),
            }
        )

    results = await asyncio.gather(
        setup_status(_request()),
        setup_status(_request()),
        setup_status(_request()),
    )

    assert all(result["needs_setup"] is True for result in results)
    assert provider.calls == 1
