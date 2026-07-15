"""Slack connection tests for user-owned channel bindings."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

from support.m4_channel_connections import make_m4_channel_connection_runtime

from app.channels.message_bus import MessageBus, OutboundMessage
from deerflow.runtime.private_scope import PrivateResourceScope


def test_slack_connect_command_binds_socket_mode_identity(migrated_postgres_database_url):
    import anyio

    from app.channels.slack import SlackChannel

    async def go():
        runtime = await make_m4_channel_connection_runtime(
            migrated_postgres_database_url,
            cipher_key="slack-secret",
        )
        try:
            state = await runtime.begin_connect("slack")
            channel = SlackChannel(
                bus=MessageBus(),
                config={
                    "bot_token": "xoxb-operator",
                    "app_token": "xapp-operator",
                    "connection_repo": runtime.repository,
                    "connection_service": runtime.service,
                },
            )
            channel._web_client = MagicMock()

            handled = await channel._bind_connection_from_connect_code(
                event={
                    "user": "U123",
                    "channel": "C123",
                    "ts": "1710000000.000100",
                },
                team_id="T123",
                code=state,
            )

            connections = await runtime.list_connections()
            assert handled is True
            assert len(connections) == 1
            runtime.assert_owner_a_scope(connections[0])
            assert connections[0]["provider"] == "slack"
            assert connections[0]["external_account_id"] == "U123"
            assert connections[0]["workspace_id"] == "T123"
            assert connections[0]["metadata"]["channel_id"] == "C123"
            channel._web_client.chat_postMessage.assert_called_once()
        finally:
            await runtime.seed.engine.dispose()

    anyio.run(go)


def test_slack_send_uses_connection_bot_token_when_connection_id_is_present():
    import anyio

    from app.channels.slack import SlackChannel

    async def go():
        repo = AsyncMock()
        repo.get_credentials.return_value = {"access_token": "xoxb-connection-token"}
        web_client = MagicMock()
        web_client_factory = MagicMock(return_value=web_client)
        channel = SlackChannel(
            bus=MessageBus(),
            config={
                "connection_repo": repo,
                "web_client_factory": web_client_factory,
            },
        )

        scope = PrivateResourceScope(
            project_id="11111111-1111-4111-8111-111111111111",
            owner_user_id="22222222-2222-4222-8222-222222222222",
            membership_version=3,
        )
        msg = OutboundMessage(
            channel_name="slack",
            chat_id="C123",
            thread_id="thread-1",
            text="hello",
            connection_id="connection-1",
            private_scope=scope,
        )
        await channel.send(msg)

        repo.get_credentials.assert_awaited_once_with(
            scope=scope,
            connection_id="connection-1",
        )
        web_client_factory.assert_called_once_with(token="xoxb-connection-token")
        web_client.chat_postMessage.assert_called_once()

    anyio.run(go)


def test_slack_http_events_mode_is_rejected(monkeypatch, caplog):
    import anyio

    from app.channels.slack import SlackChannel

    slack_sdk = ModuleType("slack_sdk")
    slack_sdk.WebClient = object
    socket_mode = ModuleType("slack_sdk.socket_mode")
    socket_mode.SocketModeClient = object
    response = ModuleType("slack_sdk.socket_mode.response")
    response.SocketModeResponse = object
    monkeypatch.setitem(sys.modules, "slack_sdk", slack_sdk)
    monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode", socket_mode)
    monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode.response", response)

    async def go():
        channel = SlackChannel(
            bus=MessageBus(),
            config={
                "bot_token": "xoxb-operator",
                # Provide app_token too so the missing-token early return cannot
                # fire before the HTTP-mode guard — otherwise the state assertions
                # below would hold even if the guard were deleted.
                "app_token": "xapp-token",
                "event_delivery": "http",
                "connection_repo": MagicMock(),
            },
        )

        with caplog.at_level("ERROR", logger="app.channels.slack"):
            await channel.start()

        assert channel._running is False
        assert channel._web_client is None
        assert "Slack HTTP Events mode is not supported" in caplog.text

    anyio.run(go)
