"""M7 Task 5 explicit project authority contracts for channels and input polish."""

from __future__ import annotations

import inspect
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.channels import manager
from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus
from app.channels.run_policy import ChannelRunPolicy
from app.channels.store import ChannelStore
from app.gateway.app import app
from app.gateway.github import run_policy as github_run_policy  # noqa: F401
from app.private_work.connection_inbound import ConnectionInboundResolver
from app.private_work.errors import PrivateWorkNotFound
from deerflow.config.channel_connections_config import ChannelConnectionsConfig


def test_global_channel_console_and_input_polish_routes_are_gone() -> None:
    paths = {route.path for route in app.routes}

    for path in (
        "/api/channels",
        "/api/channels/providers",
        "/api/console/stats",
        "/api/input-polish",
    ):
        assert path not in paths
    assert "/api/projects/{project_id}/private-work/input-polish" in paths


def test_inbound_connection_authority_requires_exact_account_coordinate() -> None:
    connection = {
        "id": "connection-a",
        "project_id": str(uuid.uuid4()),
        "owner_user_id": str(uuid.uuid4()),
        "status": "connected",
    }

    with pytest.raises(PrivateWorkNotFound):
        ConnectionInboundResolver._connection_coordinates(connection, "m7-authority")


def test_channel_manager_has_no_auth_disabled_or_external_user_identity_fallback() -> None:
    source = inspect.getsource(manager)

    assert "_auth_disabled_owner_user_id" not in source
    assert "return _safe_user_id_for_run(msg.user_id)" not in source
    assert "_handle_legacy_chat" not in source
    assert not hasattr(ChannelManager, "_handle_legacy_chat")


def _manager_with_project_dispatcher(
    *,
    dispatcher: AsyncMock | None = None,
) -> tuple[ChannelManager, AsyncMock]:
    private_dispatcher = dispatcher or AsyncMock()
    channel_manager = ChannelManager(
        MessageBus(),
        Mock(spec=ChannelStore),
        private_inbound_dispatcher=private_dispatcher,
    )
    channel_manager._semaphore = __import__("asyncio").Semaphore(1)
    return channel_manager, private_dispatcher


def test_disabled_connection_config_cannot_bypass_project_inbound_dispatch() -> None:
    config = ChannelConnectionsConfig.model_validate({"enabled": False, "require_bound_identity": False})
    channel_manager, _ = _manager_with_project_dispatcher()

    assert not hasattr(config, "require_bound_identity")
    assert channel_manager._should_dispatch_project_inbound(
        InboundMessage(
            channel_name="slack",
            chat_id="channel-1",
            user_id="user-1",
            workspace_id="team-1",
            text="hello",
        )
    )


def test_channel_policy_cannot_opt_out_of_project_inbound_dispatch() -> None:
    channel_manager, _ = _manager_with_project_dispatcher()

    assert "requires_bound_identity" not in ChannelRunPolicy.__dataclass_fields__
    assert channel_manager._should_dispatch_project_inbound(
        InboundMessage(
            channel_name="slack",
            chat_id="channel-1",
            user_id="user-1",
            workspace_id="team-1",
            text="hello",
        )
    )


@pytest.mark.asyncio
async def test_github_without_persisted_binding_fails_closed_before_legacy_client() -> None:
    dispatcher = AsyncMock()
    dispatcher.dispatch.side_effect = PrivateWorkNotFound("m7-github-binding")
    channel_manager, dispatcher = _manager_with_project_dispatcher(
        dispatcher=dispatcher,
    )
    channel_manager._handle_chat = AsyncMock()

    await channel_manager._handle_message(
        InboundMessage(
            channel_name="github",
            chat_id="deer-flow/deer-flow",
            user_id="octocat",
            workspace_id="deer-flow/deer-flow",
            text="review this",
        )
    )

    dispatcher.dispatch.assert_awaited_once()
    channel_manager._handle_chat.assert_not_awaited()
