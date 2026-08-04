from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.channel_group_bindings.errors import (
    GroupBindingAgentUnavailable,
    GroupBindingConflict,
)
from app.gateway.routers import project_channel_group_bindings
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole

PROJECT_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")
BINDING_ID = uuid.UUID("20000000-0000-4000-8000-000000000001")
AGENT_ID = uuid.UUID("30000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _context(role: ProjectRole) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-group-binding-router",
    )


def _binding(**overrides):
    values = {
        "id": BINDING_ID,
        "provider": "feishu",
        "display_name": "研发群",
        "status": "active",
        "agent_asset_id": AGENT_ID,
        "agent_scope": "system",
        "last_activity_at": None,
        "revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client(role: ProjectRole, service: object) -> TestClient:
    app = FastAPI()
    app.include_router(project_channel_group_bindings.router)
    app.dependency_overrides[project_channel_group_bindings.project_asset_context] = lambda: _context(role)
    app.dependency_overrides[project_channel_group_bindings.get_project_channel_group_binding_service] = lambda: service
    return TestClient(app)


def test_router_reuses_gateway_group_binding_service() -> None:
    service = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_channel_group_binding_service=service)))

    assert project_channel_group_bindings.get_project_channel_group_binding_service(request) is service


def test_admin_lists_safe_group_binding_dto_without_runtime_coordinates() -> None:
    service = SimpleNamespace(list=AsyncMock(return_value=(_binding(),)))
    response = _client(ProjectRole.ADMIN, service).get(f"/api/projects/{PROJECT_ID}/channel-group-bindings")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "bindings": [
            {
                "id": str(BINDING_ID),
                "provider": "feishu",
                "display_name": "研发群",
                "status": "active",
                "agent_asset_id": str(AGENT_ID),
                "agent_scope": "system",
                "last_activity_at": None,
                "revision": 1,
                "created_at": NOW.isoformat().replace("+00:00", "Z"),
                "updated_at": NOW.isoformat().replace("+00:00", "Z"),
            }
        ]
    }
    assert "channel_instance" not in response.text
    assert "external_group_ref" not in response.text
    assert "chat_id" not in response.text


def test_admin_creates_challenge_updates_and_revokes_binding() -> None:
    challenge = SimpleNamespace(
        provider="feishu",
        code="safe-one-time-code",
        command="/bind-project safe-one-time-code",
        expires_at=NOW,
        expires_in=600,
    )
    service = SimpleNamespace(
        create_challenge=AsyncMock(return_value=challenge),
        update=AsyncMock(return_value=_binding(status="disabled", revision=2)),
        delete=AsyncMock(),
    )
    client = _client(ProjectRole.ADMIN, service)

    created = client.post(
        f"/api/projects/{PROJECT_ID}/channel-group-bindings/challenge",
        json={
            "provider": "feishu",
            "agent_asset_id": str(AGENT_ID),
            "agent_scope": "system",
        },
    )
    assert created.status_code == 201
    assert created.json()["command"] == "/bind-project safe-one-time-code"
    command = service.create_challenge.await_args.args[1]
    assert command.provider == "feishu"
    assert command.agent_asset_id == AGENT_ID

    updated = client.patch(
        f"/api/projects/{PROJECT_ID}/channel-group-bindings/{BINDING_ID}",
        json={"expected_revision": 1, "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "disabled"

    deleted = client.delete(
        f"/api/projects/{PROJECT_ID}/channel-group-bindings/{BINDING_ID}",
        params={"expected_revision": 2},
    )
    assert deleted.status_code == 204
    service.delete.assert_awaited_once()


def test_non_admin_mutations_fail_before_service_call() -> None:
    for role in (ProjectRole.EDITOR, ProjectRole.RUNNER, ProjectRole.VIEWER):
        service = SimpleNamespace(
            list=AsyncMock(),
            create_challenge=AsyncMock(),
            update=AsyncMock(),
            delete=AsyncMock(),
        )
        client = _client(role, service)
        responses = (
            client.get(f"/api/projects/{PROJECT_ID}/channel-group-bindings"),
            client.post(
                f"/api/projects/{PROJECT_ID}/channel-group-bindings/challenge",
                json={
                    "provider": "feishu",
                    "agent_asset_id": str(AGENT_ID),
                    "agent_scope": "system",
                },
            ),
            client.patch(
                f"/api/projects/{PROJECT_ID}/channel-group-bindings/{BINDING_ID}",
                json={"expected_revision": 1, "enabled": False},
            ),
            client.delete(
                f"/api/projects/{PROJECT_ID}/channel-group-bindings/{BINDING_ID}",
                params={"expected_revision": 1},
            ),
        )
        assert [response.status_code for response in responses] == [403, 403, 403, 403]
        assert all(response.json()["detail"]["code"] == "GROUP_BINDING_FORBIDDEN" for response in responses)
        service.list.assert_not_awaited()
        service.create_challenge.assert_not_awaited()
        service.update.assert_not_awaited()
        service.delete.assert_not_awaited()


def test_invalid_request_has_stable_secret_free_422() -> None:
    service = SimpleNamespace(create_challenge=AsyncMock())
    secret = "should-never-be-echoed"
    response = _client(ProjectRole.ADMIN, service).post(
        f"/api/projects/{PROJECT_ID}/channel-group-bindings/challenge",
        json={
            "provider": secret,
            "agent_asset_id": "not-a-uuid",
            "agent_scope": "owner",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "GROUP_BINDING_INVALID"
    assert secret not in response.text
    service.create_challenge.assert_not_awaited()


def test_revision_conflict_has_stable_409() -> None:
    service = SimpleNamespace(update=AsyncMock(side_effect=GroupBindingConflict("req-group-binding-router")))
    response = _client(ProjectRole.ADMIN, service).patch(
        f"/api/projects/{PROJECT_ID}/channel-group-bindings/{BINDING_ID}",
        json={"expected_revision": 1, "enabled": False},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "GROUP_BINDING_CONFLICT",
        "message": "Group connection changed. Refresh and try again.",
        "request_id": "req-group-binding-router",
    }


def test_agent_unavailable_has_stable_secret_free_409() -> None:
    service = SimpleNamespace(update=AsyncMock(side_effect=GroupBindingAgentUnavailable("req-group-binding-router")))
    response = _client(ProjectRole.ADMIN, service).patch(
        f"/api/projects/{PROJECT_ID}/channel-group-bindings/{BINDING_ID}",
        json={"expected_revision": 1, "enabled": False},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "GROUP_BINDING_AGENT_UNAVAILABLE",
        "message": "The selected Agent is unavailable.",
        "request_id": "req-group-binding-router",
    }
