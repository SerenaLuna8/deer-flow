"""Discord connection routing tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from support.m4_channel_connections import make_m4_channel_connection_runtime

from app.channels.discord import DiscordChannel
from app.channels.message_bus import InboundMessage, MessageBus


@pytest.fixture
async def runtime(migrated_postgres_database_url):
    runtime = await make_m4_channel_connection_runtime(
        migrated_postgres_database_url,
        cipher_key="discord-secret",
    )
    try:
        yield runtime
    finally:
        await runtime.seed.engine.dispose()


@pytest.mark.anyio
async def test_discord_inbound_attaches_project_identity(runtime):
    connection = await runtime.repository.upsert_connection(
        scope=runtime.seed.owner_a_scope,
        provider="discord",
        external_account_id="987",
        external_account_name="Alice",
        status="connected",
    )
    channel = DiscordChannel(
        bus=MessageBus(),
        config={
            "bot_token": "discord-bot",
            "connection_repo": runtime.repository,
            "connection_service": runtime.service,
        },
    )
    inbound = InboundMessage(
        channel_name="discord",
        chat_id="C123",
        user_id="987",
        text="hello",
    )

    attached = await channel._attach_connection_identity(inbound, guild_id="G123")

    runtime.assert_owner_a_scope(connection)
    assert attached.connection_id == connection["id"]
    assert attached.owner_user_id == runtime.seed.owner_a_scope.owner_user_id
    assert attached.project_id == runtime.seed.owner_a_scope.project_id
    assert attached.workspace_id is None


@pytest.mark.anyio
async def test_discord_connect_command_binds_gateway_identity(runtime):
    state = await runtime.begin_connect("discord")
    channel = DiscordChannel(
        bus=MessageBus(),
        config={
            "bot_token": "discord-bot",
            "connection_repo": runtime.repository,
            "connection_service": runtime.service,
        },
    )
    message = MagicMock()
    message.author.id = 987
    message.author.display_name = "Alice"
    message.guild.id = 123
    message.guild.name = "Deer Guild"
    message.channel.id = 456
    message.channel.send = AsyncMock()

    handled = await channel._bind_connection_from_connect_code(message, state)

    connections = await runtime.list_connections()
    assert handled is True
    assert len(connections) == 1
    runtime.assert_owner_a_scope(connections[0])
    assert connections[0]["provider"] == "discord"
    assert connections[0]["external_account_id"] == "987"
    assert connections[0]["external_account_name"] == "Alice"
    assert connections[0]["workspace_id"] == "123"
    assert connections[0]["workspace_name"] == "Deer Guild"
    assert connections[0]["metadata"]["channel_id"] == "456"
    message.channel.send.assert_awaited_once()
