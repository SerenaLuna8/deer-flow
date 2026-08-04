from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.gateway.channel_schemas import ProjectConnectRequest
from app.gateway.routers import project_connections
from app.private_work.connection_service import ProjectConnectionChallenge
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.config.channel_connections_config import ChannelConnectionsConfig


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.RUNNER,
            capabilities=capabilities_for(ProjectRole.RUNNER),
            membership_version=1,
            request_id="req-project-channel-connect",
        )
    )


@pytest.mark.asyncio
async def test_begin_connection_passes_exact_project_channel_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    instance_id = uuid.uuid4()
    config = ChannelConnectionsConfig.model_validate({"enabled": True})
    monkeypatch.setattr(
        project_connections,
        "_ready_provider",
        AsyncMock(return_value=(config, instance_id, {"app_id": "cli_example"})),
    )
    service = SimpleNamespace(
        begin_connect=AsyncMock(
            return_value=ProjectConnectionChallenge(
                state="binding-code",
                code="binding-code",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        ),
        begin_legacy_connect=AsyncMock(),
    )
    monkeypatch.setattr(project_connections, "_service", lambda _request: service)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    response = await project_connections.begin_project_connection(
        request,
        "feishu",
        ProjectConnectRequest(
            agent_asset_id=uuid.uuid4(),
            agent_scope="project",
        ),
        context,
    )

    assert response.provider == "feishu"
    assert service.begin_connect.await_args.kwargs["channel_instance_id"] == str(instance_id)
    service.begin_legacy_connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_provider_uses_explicit_compatibility_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    config = ChannelConnectionsConfig.model_validate(
        {
            "enabled": True,
            "telegram": {
                "enabled": True,
                "bot_username": "legacy_bot",
            },
        }
    )
    monkeypatch.setattr(
        project_connections,
        "_ready_provider",
        AsyncMock(return_value=(config, None, {})),
    )
    service = SimpleNamespace(
        begin_connect=AsyncMock(),
        begin_legacy_connect=AsyncMock(
            return_value=ProjectConnectionChallenge(
                state="legacy-code",
                code="legacy-code",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        ),
    )
    monkeypatch.setattr(project_connections, "_service", lambda _request: service)

    response = await project_connections.begin_project_connection(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        "telegram",
        ProjectConnectRequest(
            agent_asset_id=uuid.uuid4(),
            agent_scope="system",
        ),
        context,
    )

    service.begin_legacy_connect.assert_awaited_once()
    service.begin_connect.assert_not_awaited()
    assert response.url == "https://t.me/legacy_bot?start=legacy-code"


@pytest.mark.asyncio
async def test_project_provider_remains_connectable_when_legacy_connections_are_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    instance_id = uuid.uuid4()
    config = ChannelConnectionsConfig()
    runtime = project_connections._ProjectProviderRuntime(
        instance_id=instance_id,
        provider="feishu",
        public_config={"app_id": "cli_example"},
        enabled=True,
        configured=True,
        running=True,
        observed_status="running",
    )
    monkeypatch.setattr(
        project_connections,
        "_provider_config",
        AsyncMock(return_value=(config, {})),
    )
    monkeypatch.setattr(
        project_connections,
        "_project_provider_runtimes",
        AsyncMock(return_value={"feishu": runtime}),
    )
    service = SimpleNamespace(list=AsyncMock(return_value=[]))
    monkeypatch.setattr(project_connections, "_service", lambda _request: service)
    coordinator = SimpleNamespace(reconcile=AsyncMock(return_value=True))
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                project_channel_runtime_coordinator=coordinator,
            )
        )
    )

    providers = await project_connections.list_project_connection_providers(
        request,
        context,
    )
    ready_config, ready_instance_id, public_config = await project_connections._ready_provider(request, "feishu", context)

    assert providers.enabled is True
    assert len(providers.providers) == 1
    assert providers.providers[0].provider == "feishu"
    assert providers.providers[0].connectable is True
    assert providers.providers[0].unavailable_reason is None
    assert ready_config is config
    assert ready_instance_id == instance_id
    assert public_config == {"app_id": "cli_example"}
    coordinator.reconcile.assert_awaited_once_with(instance_id)
