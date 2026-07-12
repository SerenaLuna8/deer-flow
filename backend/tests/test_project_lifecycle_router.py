from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.deps import project_session
from app.gateway.routers import project_lifecycle
from app.projects.errors import ProjectDeletionStateConflict
from app.projects.models import ProjectRole, ProjectView

USER_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def _view(*, status: str) -> ProjectView:
    return ProjectView(
        id=PROJECT_ID,
        slug="alpha",
        display_name="Alpha",
        description="",
        icon="folder",
        role=ProjectRole.ADMIN,
        capabilities=frozenset(),
        is_pinned=False,
        last_entered_at=None,
        member_count=1,
        agent_count=0,
        skill_count=0,
        mcp_count=0,
        status=status,
        is_suspended=False,
        membership_version=2,
        request_id="req-lifecycle",
        deletion_effective_at=NOW + timedelta(days=30) if status == "pending_deletion" else None,
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(project_lifecycle.router)

    async def fake_session():
        yield object()

    app.dependency_overrides[project_session] = fake_session
    app.dependency_overrides[project_lifecycle.authenticated_project_identity] = lambda: (
        USER_ID,
        "req-lifecycle",
    )
    return TestClient(app)


def test_deletion_resolves_active_project_context(monkeypatch) -> None:
    context = object()
    resolve = AsyncMock(return_value=context)
    request_deletion = AsyncMock(return_value=_view(status="pending_deletion"))
    monkeypatch.setattr(project_lifecycle, "resolve_project_context", resolve)
    monkeypatch.setattr(
        project_lifecycle.ProjectLifecycleService,
        "request_deletion",
        request_deletion,
    )

    response = _client().post(f"/api/projects/{PROJECT_ID}/deletion")

    assert response.status_code == 200
    assert response.json()["status"] == "pending_deletion"
    assert response.json()["deletion_effective_at"] is not None
    assert resolve.await_args.args[1:] == (USER_ID, PROJECT_ID, "req-lifecycle")
    assert request_deletion.await_args.args[0] is context


def test_restore_uses_authenticated_user_without_active_context(monkeypatch) -> None:
    restore = AsyncMock(return_value=_view(status="active"))
    monkeypatch.setattr(project_lifecycle.ProjectLifecycleService, "restore", restore)

    response = _client().post(f"/api/projects/{PROJECT_ID}/restore")

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    restore.assert_awaited_once()
    assert restore.await_args.args[:3] == (USER_ID, PROJECT_ID, "req-lifecycle")


def test_restore_state_conflict_has_stable_error(monkeypatch) -> None:
    monkeypatch.setattr(
        project_lifecycle.ProjectLifecycleService,
        "restore",
        AsyncMock(side_effect=ProjectDeletionStateConflict()),
    )

    response = _client().post(f"/api/projects/{PROJECT_ID}/restore")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "PROJECT_DELETION_STATE_CONFLICT",
        "message": "Project deletion state conflict",
        "request_id": "req-lifecycle",
    }
