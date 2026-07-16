from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.deps import project_session
from app.gateway.routers import project_members
from app.projects.errors import ProjectLastAdmin, ProjectNotFound
from app.projects.membership_models import MembershipView
from app.projects.models import ProjectRole

USER_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
MEMBERSHIP_ID = uuid.uuid4()
NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def _client() -> TestClient:
    app = FastAPI()
    app.state.project_quota_enforcer = object()
    app.include_router(project_members.router)

    async def fake_session():
        yield object()

    app.dependency_overrides[project_session] = fake_session
    app.dependency_overrides[project_members.authenticated_project_identity] = lambda: (
        USER_ID,
        "req-members",
    )
    return TestClient(app)


def _member(*, role: ProjectRole = ProjectRole.EDITOR, version: int = 2) -> MembershipView:
    return MembershipView(
        membership_id=MEMBERSHIP_ID,
        user_id=uuid.uuid4(),
        account_email="member@example.com",
        role=role,
        status="active",
        version=version,
        joined_at=NOW,
    )


def test_member_routes_resolve_context_from_authenticated_identity(monkeypatch) -> None:
    context = object()
    resolve = AsyncMock(return_value=context)
    list_members = AsyncMock(return_value=(_member(),))
    monkeypatch.setattr(project_members, "resolve_project_context", resolve)
    monkeypatch.setattr(project_members.MembershipService, "list_members", list_members)

    response = _client().get(f"/api/projects/{PROJECT_ID}/members")

    assert response.status_code == 200
    assert response.json()[0]["account_email"] == "member@example.com"
    resolve.assert_awaited_once()
    assert resolve.await_args.args[1:] == (USER_ID, PROJECT_ID, "req-members")
    list_members.assert_awaited_once_with(context)


def test_cross_project_member_patch_is_hidden_with_stable_error(monkeypatch) -> None:
    monkeypatch.setattr(project_members, "resolve_project_context", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        project_members.MembershipService,
        "change_role",
        AsyncMock(side_effect=ProjectNotFound()),
    )

    response = _client().patch(
        f"/api/projects/{PROJECT_ID}/members/{uuid.uuid4()}",
        json={"role": "viewer", "version": 1},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "PROJECT_OR_MEMBER_NOT_FOUND",
        "message": "Project or member not found",
        "request_id": "req-members",
    }


def test_remove_and_leave_map_last_admin_and_return_membership(monkeypatch) -> None:
    monkeypatch.setattr(project_members, "resolve_project_context", AsyncMock(return_value=object()))
    remove = AsyncMock(side_effect=ProjectLastAdmin())
    leave = AsyncMock(return_value=_member(role=ProjectRole.VIEWER, version=3))
    monkeypatch.setattr(project_members.MembershipService, "remove", remove)
    monkeypatch.setattr(project_members.MembershipService, "leave", leave)
    client = _client()

    removed = client.request(
        "DELETE",
        f"/api/projects/{PROJECT_ID}/members/{MEMBERSHIP_ID}",
        json={"version": 2},
    )
    left = client.post(f"/api/projects/{PROJECT_ID}/leave", json={"version": 2})

    assert removed.status_code == 409
    assert removed.json()["detail"]["code"] == "PROJECT_LAST_ADMIN"
    assert removed.json()["detail"]["request_id"] == "req-members"
    assert left.status_code == 200
    assert left.json()["version"] == 3


def test_member_validation_has_stable_error_and_request_id() -> None:
    response = _client().patch(
        f"/api/projects/{PROJECT_ID}/members/{MEMBERSHIP_ID}",
        json={"role": "admin", "version": 0},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "PROJECT_VALIDATION_FAILED"
    assert detail["message"] == "Project validation failed"
    assert isinstance(detail["request_id"], str) and detail["request_id"]
