from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import project_channel_instances
from app.project_channels.errors import ChannelInstanceValidationFailed
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole

PROJECT_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")
INSTANCE_ID = uuid.UUID("20000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _context(role: ProjectRole) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-channel-router",
    )


def _view(**overrides):
    values = {
        "id": INSTANCE_ID,
        "provider": "feishu",
        "display_name": "Feishu",
        "status": "running",
        "enabled": True,
        "configured": True,
        "credential_configured": True,
        "public_config": {"app_id": "cli_example"},
        "updated_at": NOW,
        "last_error": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client(role: ProjectRole, service: object) -> TestClient:
    app = FastAPI()
    app.include_router(project_channel_instances.router)
    app.dependency_overrides[project_channel_instances.project_asset_context] = lambda: _context(role)
    app.dependency_overrides[project_channel_instances.get_project_channel_instance_service] = lambda: service
    return TestClient(app)


def test_admin_configures_channel_without_echoing_secret() -> None:
    service = SimpleNamespace(configure=AsyncMock(return_value=_view()))
    client = _client(ProjectRole.ADMIN, service)
    secret = "never-return-channel-secret"

    response = client.put(
        f"/api/projects/{PROJECT_ID}/channel-instances/feishu",
        json={
            "display_name": "Feishu",
            "public_config": {"app_id": "cli_example"},
            "credentials": {"app_secret": secret},
            "enabled": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["provider"] == "feishu"
    assert response.json()["status"] == "running"
    assert secret not in response.text
    command = service.configure.await_args.args[2]
    assert command.credentials == {"app_secret": secret}
    assert secret not in repr(command)


def test_non_admin_cannot_mutate_channel_instances() -> None:
    for role in (ProjectRole.EDITOR, ProjectRole.RUNNER, ProjectRole.VIEWER):
        service = SimpleNamespace(
            configure=AsyncMock(return_value=_view()),
            set_enabled=AsyncMock(return_value=_view()),
            delete=AsyncMock(),
        )
        client = _client(role, service)
        responses = (
            client.put(
                f"/api/projects/{PROJECT_ID}/channel-instances/feishu",
                json={
                    "public_config": {"app_id": "cli_example"},
                    "credentials": {"app_secret": "secret"},
                    "enabled": True,
                },
            ),
            client.post(f"/api/projects/{PROJECT_ID}/channel-instances/feishu/enable"),
            client.delete(f"/api/projects/{PROJECT_ID}/channel-instances/feishu"),
        )
        for response in responses:
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "CHANNEL_INSTANCE_FORBIDDEN"
            assert "Project Admin" in response.json()["detail"]["message"]
        service.configure.assert_not_awaited()
        service.set_enabled.assert_not_awaited()
        service.delete.assert_not_awaited()


def test_validation_failure_identifies_missing_fields_without_secret_leak() -> None:
    service = SimpleNamespace(
        configure=AsyncMock(
            side_effect=ChannelInstanceValidationFailed(
                "req-channel-router",
                "Feishu App ID and App Secret are required.",
                fields=("public_config.app_id", "credentials.app_secret"),
            )
        )
    )
    response = _client(ProjectRole.ADMIN, service).put(
        f"/api/projects/{PROJECT_ID}/channel-instances/feishu",
        json={"public_config": {}, "credentials": {}, "enabled": True},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "CHANNEL_INSTANCE_INVALID",
        "message": "Feishu App ID and App Secret are required.",
        "request_id": "req-channel-router",
        "fields": ["public_config.app_id", "credentials.app_secret"],
    }


def test_request_schema_failure_never_echoes_secret_bearing_input() -> None:
    service = SimpleNamespace(configure=AsyncMock())
    secret = "never-return-invalid-request-secret"

    response = _client(ProjectRole.ADMIN, service).put(
        f"/api/projects/{PROJECT_ID}/channel-instances/feishu",
        json={
            "public_config": {"app_id": "cli_example"},
            "credentials": {"app_secret": secret},
            "enabled": "yes",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "CHANNEL_INSTANCE_INVALID"
    assert secret not in response.text
    service.configure.assert_not_awaited()


def test_members_can_list_safe_channel_state_and_admin_can_toggle_and_delete() -> None:
    service = SimpleNamespace(
        list=AsyncMock(return_value=(_view(),)),
        set_enabled=AsyncMock(return_value=_view(status="disabled", enabled=False)),
        delete=AsyncMock(),
    )
    viewer = _client(ProjectRole.VIEWER, service)
    listed = viewer.get(f"/api/projects/{PROJECT_ID}/channel-instances")
    assert listed.status_code == 200
    assert listed.json()["instances"][0]["credential_configured"] is True
    assert "credentials" not in listed.text

    admin = _client(ProjectRole.ADMIN, service)
    disabled = admin.post(f"/api/projects/{PROJECT_ID}/channel-instances/feishu/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert service.set_enabled.await_args.args[2] is False

    deleted = admin.delete(f"/api/projects/{PROJECT_ID}/channel-instances/feishu")
    assert deleted.status_code == 204
