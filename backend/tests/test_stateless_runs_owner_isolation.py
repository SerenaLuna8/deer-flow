"""Cross-user isolation for the stateless ``POST /api/runs/stream`` and ``/wait`` endpoints.

These endpoints receive ``thread_id`` in the request body, so the
``@require_permission(owner_check=True)`` decorator — which reads the
``thread_id`` *path* parameter — cannot protect them. The owner check
lives inside ``services.start_run()`` instead; this suite pins it at the
HTTP layer so the gap cannot silently reopen.

Strategy
--------
``app.state.run_manager.create_or_reject`` raises ``ConflictError``, so a
request that *passes* the owner check deterministically short-circuits
with 409 before any agent code runs. The two outcomes:

- 404 + ``create_or_reject`` never awaited -> blocked by the owner check
- 409 + ``create_or_reject`` awaited       -> passed the owner check

The thread store is a real ``MemoryThreadMetaStore`` (not a mock) so the
``check_access`` semantics under test — missing row allows, ``user_id``
NULL allows, foreign owner denies — are exercised through real code.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from _router_auth_helpers import make_authed_test_app
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from app.gateway.auth.models import User
from app.gateway.routers import runs, thread_runs
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.runtime import ConflictError, RunManager, RunStatus
from deerflow.runtime.runs.store.memory import MemoryRunStore

USER_A = User(email="owner-a@example.com", password_hash="x", system_role="user", id=uuid4())
USER_B = User(email="intruder-b@example.com", password_hash="x", system_role="user", id=uuid4())
INTERNAL_USER = SimpleNamespace(id="default", system_role="internal")

THREAD_A = "thread-owned-by-a"
THREAD_SHARED = "thread-shared-null-owner"
INVALID_MODEL = "definitely-not-allowed"


@pytest.fixture(autouse=True)
def _stub_app_config():
    """Inject a minimal AppConfig so the allowed path (which builds a
    RunContext via ``get_config()``) never reads config.yaml from disk."""
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    yield
    reset_app_config()


def _make_thread_store() -> MemoryThreadMetaStore:
    store = MemoryThreadMetaStore(InMemoryStore())

    async def _seed():
        await store.create(THREAD_A, user_id=str(USER_A.id))
        await store.create(THREAD_SHARED, user_id=None)

    asyncio.run(_seed())
    return store


@contextmanager
def _client(user):
    """Yield a ``TestClient`` authenticated as ``user`` plus the stubbed
    ``create_or_reject`` mock, closing the client (and its anyio portal /
    background threads) on exit.

    ``create_or_reject`` raises ``ConflictError`` so a request that passes the
    owner check short-circuits to 409 before any agent code runs.
    """
    app = make_authed_test_app(user_factory=lambda: user)
    app.include_router(runs.router)
    app.state.thread_store = _make_thread_store()
    app.state.stream_bridge = MagicMock()
    app.state.checkpointer = MagicMock()
    app.state.store = MagicMock()
    app.state.run_events_config = None
    app.state.run_event_store = MagicMock()
    run_manager = MagicMock()
    run_manager.create_or_reject = AsyncMock(side_effect=ConflictError("sentinel: owner check passed"))
    app.state.run_manager = run_manager
    with TestClient(app) as client:
        yield client, run_manager.create_or_reject


def _body(thread_id: str | None = None) -> dict:
    if thread_id is None:
        return {}
    return {"config": {"configurable": {"thread_id": thread_id}}}


def _invalid_model_body(thread_id: str) -> dict:
    return {
        "config": {"configurable": {"thread_id": thread_id}},
        "context": {"model_name": INVALID_MODEL},
        "multitask_strategy": "interrupt",
    }


@contextmanager
def _invalid_model_client(user):
    """Yield a real HTTP client plus probes for every run lifecycle boundary."""
    app = make_authed_test_app(user_factory=lambda: user)
    app.include_router(runs.router)
    app.include_router(thread_runs.router)

    thread_store = _make_thread_store()
    thread_status = AsyncMock(wraps=thread_store.update_status)
    thread_store.update_status = thread_status
    app.state.thread_store = thread_store

    checkpointer = MagicMock()
    checkpointer.aget_tuple = AsyncMock()
    app.state.checkpointer = checkpointer
    app.state.stream_bridge = MagicMock()
    app.state.store = MagicMock()
    app.state.run_events_config = None
    app.state.run_event_store = MagicMock()

    run_manager = RunManager(store=MemoryRunStore())
    old_run = asyncio.run(run_manager.create_or_reject(THREAD_A, user_id=str(USER_A.id)))
    admission = AsyncMock(wraps=run_manager.create_or_reject)
    status = AsyncMock(wraps=run_manager.set_status)
    cancel = AsyncMock(wraps=run_manager.cancel)
    run_manager.create_or_reject = admission
    run_manager.set_status = status
    run_manager.cancel = cancel
    app.state.run_manager = run_manager

    with (
        patch.object(AppConfig, "get_model_config", autospec=True, return_value=None) as model_lookup,
        TestClient(app) as client,
    ):
        yield (
            client,
            SimpleNamespace(
                model_lookup=model_lookup,
                checkpointer=checkpointer,
                run_manager=run_manager,
                old_run=old_run,
                admission=admission,
                status=status,
                cancel=cancel,
                thread_status=thread_status,
            ),
        )


def _assert_no_lifecycle_side_effects(probes) -> None:
    probes.checkpointer.aget_tuple.assert_not_awaited()
    probes.admission.assert_not_awaited()
    probes.status.assert_not_awaited()
    probes.cancel.assert_not_awaited()
    probes.thread_status.assert_not_awaited()

    records = asyncio.run(probes.run_manager.list_by_thread(THREAD_A, user_id=None))
    assert [record.run_id for record in records] == [probes.old_run.run_id]
    assert probes.old_run.status is RunStatus.pending
    assert probes.old_run.abort_event.is_set() is False


@pytest.mark.parametrize("route", ["stream", "wait"])
def test_stateless_invalid_model_foreign_thread_authorizes_first(route: str):
    with _invalid_model_client(USER_B) as (client, probes):
        response = client.post(f"/api/runs/{route}", json=_invalid_model_body(THREAD_A))

    assert response.status_code == 404
    assert response.json() == {"detail": f"Thread {THREAD_A} not found"}
    probes.model_lookup.assert_not_called()
    _assert_no_lifecycle_side_effects(probes)


@pytest.mark.parametrize("route", ["stream", "wait"])
def test_stateless_invalid_model_owner_returns_400_without_lifecycle_side_effects(route: str):
    with _invalid_model_client(USER_A) as (client, probes):
        response = client.post(f"/api/runs/{route}", json=_invalid_model_body(THREAD_A))

    assert response.status_code == 400
    assert response.json() == {"detail": f"Model {INVALID_MODEL!r} is not in the configured model allowlist"}
    assert probes.model_lookup.call_count == 1
    assert probes.model_lookup.call_args.args[1] == INVALID_MODEL
    _assert_no_lifecycle_side_effects(probes)


@pytest.mark.parametrize("suffix", ["runs", "runs/stream", "runs/wait"])
def test_thread_scoped_invalid_model_foreign_thread_still_returns_404(suffix: str):
    with _invalid_model_client(USER_B) as (client, probes):
        response = client.post(
            f"/api/threads/{THREAD_A}/{suffix}",
            json=_invalid_model_body(THREAD_A),
        )

    assert response.status_code == 404
    assert response.json() == {"detail": f"Thread {THREAD_A} not found"}
    probes.model_lookup.assert_not_called()
    _assert_no_lifecycle_side_effects(probes)


# ---------------------------------------------------------------------------
# Denied: another user's thread
# ---------------------------------------------------------------------------


def test_stream_cross_user_returns_404():
    """User B cannot start a run on user A's thread via /api/runs/stream."""
    with _client(USER_B) as (client, create_or_reject):
        response = client.post("/api/runs/stream", json=_body(THREAD_A))
    assert response.status_code == 404
    assert response.json()["detail"] == f"Thread {THREAD_A} not found"
    create_or_reject.assert_not_awaited()


def test_wait_cross_user_returns_404_without_channel_values():
    """User B cannot read user A's checkpoint state via /api/runs/wait."""
    with _client(USER_B) as (client, create_or_reject):
        response = client.post("/api/runs/wait", json=_body(THREAD_A))
    assert response.status_code == 404
    assert response.json() == {"detail": f"Thread {THREAD_A} not found"}
    create_or_reject.assert_not_awaited()


# ---------------------------------------------------------------------------
# Allowed: owner, fresh/untracked/shared threads, internal role
# ---------------------------------------------------------------------------


def test_stream_owner_passes_owner_check():
    """User A reaches run creation on their own thread (409 sentinel)."""
    with _client(USER_A) as (client, create_or_reject):
        response = client.post("/api/runs/stream", json=_body(THREAD_A))
    assert response.status_code == 409
    create_or_reject.assert_awaited()


def test_wait_owner_passes_owner_check():
    with _client(USER_A) as (client, create_or_reject):
        response = client.post("/api/runs/wait", json=_body(THREAD_A))
    assert response.status_code == 409
    create_or_reject.assert_awaited()


def test_stream_without_thread_id_passes_owner_check():
    """Stateless run with no thread_id auto-creates a thread — never blocked."""
    with _client(USER_B) as (client, create_or_reject):
        response = client.post("/api/runs/stream", json=_body())
    assert response.status_code == 409
    create_or_reject.assert_awaited()


def test_stream_untracked_thread_passes_owner_check():
    """A thread_id with no thread_meta row (untracked legacy) stays accessible."""
    with _client(USER_B) as (client, create_or_reject):
        response = client.post("/api/runs/stream", json=_body("never-created-thread"))
    assert response.status_code == 409
    create_or_reject.assert_awaited()


def test_stream_shared_thread_passes_owner_check():
    """A thread_meta row with user_id NULL (shared / pre-auth data) stays accessible."""
    with _client(USER_B) as (client, create_or_reject):
        response = client.post("/api/runs/stream", json=_body(THREAD_SHARED))
    assert response.status_code == 409
    create_or_reject.assert_awaited()


def test_stream_internal_role_scoped_by_owner_header():
    """IM channels run with the internal system role on behalf of the
    connection owner named in X-DeerFlow-Owner-User-Id — the owner check is
    scoped to that owner rather than bypassed."""
    from app.gateway.internal_auth import INTERNAL_OWNER_USER_ID_HEADER_NAME

    with _client(INTERNAL_USER) as (client, create_or_reject):
        response = client.post(
            "/api/runs/stream",
            json=_body(THREAD_A),
            headers={INTERNAL_OWNER_USER_ID_HEADER_NAME: str(USER_A.id)},
        )
    assert response.status_code == 409
    create_or_reject.assert_awaited()


def test_stream_internal_role_with_foreign_owner_header_returns_404():
    """The internal token alone must not grant access to another user's thread."""
    from app.gateway.internal_auth import INTERNAL_OWNER_USER_ID_HEADER_NAME

    with _client(INTERNAL_USER) as (client, create_or_reject):
        response = client.post(
            "/api/runs/stream",
            json=_body(THREAD_A),
            headers={INTERNAL_OWNER_USER_ID_HEADER_NAME: str(USER_B.id)},
        )
    assert response.status_code == 404
    create_or_reject.assert_not_awaited()


def test_stream_internal_role_without_owner_header_is_scoped_to_internal_user():
    """Without an owner header internal callers keep access to their own and
    shared/untracked threads, but not to user-owned threads."""
    with _client(INTERNAL_USER) as (client, create_or_reject):
        denied = client.post("/api/runs/stream", json=_body(THREAD_A))
        allowed = client.post("/api/runs/stream", json=_body(THREAD_SHARED))
    assert denied.status_code == 404
    assert allowed.status_code == 409
    create_or_reject.assert_awaited()
