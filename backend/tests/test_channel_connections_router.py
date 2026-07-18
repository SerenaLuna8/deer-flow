"""Project-only channel connection router contracts."""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.gateway.channel_schemas import (
    ProjectConnectionProviderResponse,
    ProjectConnectRequest,
)
from app.gateway.routers import project_connections
from deerflow.config.channel_connections_config import ChannelConnectionsConfig


def test_project_router_does_not_import_deleted_global_channel_router() -> None:
    source = inspect.getsource(project_connections)

    assert "routers.channel_connections" not in source
    assert 'prefix="/api/projects/{project_id}/connections"' in source


@pytest.mark.parametrize(
    "authority_field",
    (
        "account_id",
        "owner_user_id",
        "project_id",
        "capabilities",
        "asset_snapshot",
        "credential_grants",
    ),
)
def test_project_connect_request_rejects_client_authority(
    authority_field: str,
) -> None:
    payload = {
        "agent_asset_id": str(uuid.uuid4()),
        "agent_scope": "project",
        authority_field: "forged",
    }

    with pytest.raises(ValidationError):
        ProjectConnectRequest.model_validate(payload)


def test_project_provider_response_has_no_runtime_credentials() -> None:
    value = ProjectConnectionProviderResponse(
        provider="slack",
        display_name="Slack",
        enabled=True,
        configured=True,
        connectable=True,
        auth_mode="binding_code",
        connection_status="not_connected",
    ).model_dump()

    assert set(value) == {
        "provider",
        "display_name",
        "enabled",
        "configured",
        "connectable",
        "unavailable_reason",
        "auth_mode",
        "connection_status",
    }
    assert "credential_fields" not in value
    assert "credential_values" not in value


def test_provider_state_uses_server_runtime_config_only() -> None:
    config = ChannelConnectionsConfig.model_validate({"enabled": True, "slack": {"enabled": True}})
    enabled, configured, reason = project_connections._provider_state(
        config,
        {
            "slack": {
                "enabled": True,
                "bot_token": "bot",
                "app_token": "app",
            }
        },
        "slack",
    )

    assert enabled is True
    assert configured is True
    assert reason is None


@pytest.mark.anyio
async def test_project_connection_list_projects_only_safe_public_fields(
    monkeypatch,
) -> None:
    service = SimpleNamespace(
        list=AsyncMock(
            return_value=[
                {
                    "id": "connection-a",
                    "account_id": str(uuid.uuid4()),
                    "project_id": str(uuid.uuid4()),
                    "owner_user_id": str(uuid.uuid4()),
                    "provider": "slack",
                    "status": "connected",
                    "external_account_id": "external-a",
                    "metadata": {"agent_scope": "project"},
                }
            ]
        )
    )
    monkeypatch.setattr(project_connections, "_service", lambda _request: service)

    response = await project_connections.list_project_connections(
        SimpleNamespace(),
        SimpleNamespace(),
    )

    assert response.model_dump() == {
        "connections": [
            {
                "id": "connection-a",
                "provider": "slack",
                "status": "connected",
                "external_account_id": "external-a",
                "external_account_name": None,
                "workspace_id": None,
                "workspace_name": None,
                "scopes": [],
                "metadata": {"agent_scope": "project"},
            }
        ]
    }
