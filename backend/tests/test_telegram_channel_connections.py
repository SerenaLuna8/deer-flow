"""Tests for Telegram deep-link channel connections."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from support.m4_channel_connections import make_m4_channel_connection_runtime

from app.channels.message_bus import MessageBus
from app.channels.telegram import TelegramChannel


@pytest.fixture
async def runtime(migrated_postgres_database_url):
    runtime = await make_m4_channel_connection_runtime(
        migrated_postgres_database_url,
        cipher_key="telegram-secret",
    )
    try:
        yield runtime
    finally:
        await runtime.seed.engine.dispose()


def _telegram_update(*, text: str = "/start", user_id: int = 42, chat_id: int = 100, chat_type: str = "private"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "alice"
    update.effective_user.full_name = "Alice Example"
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.message.text = text
    update.message.message_id = 55
    update.message.reply_to_message = None
    update.message.reply_text = AsyncMock()
    return update


@pytest.mark.anyio
async def test_start_with_deep_link_state_binds_telegram_chat(runtime):
    state = await runtime.begin_connect("telegram")
    channel = TelegramChannel(
        bus=MessageBus(),
        config={
            "bot_token": "test-token",
            "connection_repo": runtime.repository,
            "connection_service": runtime.service,
        },
    )
    update = _telegram_update(text=f"/start {state}")
    context = MagicMock()
    context.args = [state]

    await channel._cmd_start(update, context)

    connections = await runtime.list_connections()
    assert len(connections) == 1
    runtime.assert_owner_a_scope(connections[0])
    assert connections[0]["provider"] == "telegram"
    assert connections[0]["external_account_id"] == "42"
    assert connections[0]["external_account_name"] == "Alice Example"
    assert connections[0]["workspace_id"] == "100"
    assert connections[0]["metadata"]["chat_type"] == "private"
    update.message.reply_text.assert_awaited_once()
    assert "connected" in update.message.reply_text.await_args.args[0].lower()


@pytest.mark.anyio
async def test_start_token_bypasses_allowed_users_filter(runtime):
    # A newly allowlisted-but-unbound user must be able to bootstrap their first
    # bind via the deep-link start token even though their Telegram id is not yet
    # in allowed_users. The allowed_users gate must run after token handling.
    state = await runtime.begin_connect("telegram")
    channel = TelegramChannel(
        bus=MessageBus(),
        config={
            "bot_token": "test-token",
            "connection_repo": runtime.repository,
            "connection_service": runtime.service,
            "allowed_users": [999],  # newcomer (42) is not whitelisted
        },
    )
    update = _telegram_update(text=f"/start {state}", user_id=42)
    context = MagicMock()
    context.args = [state]

    await channel._cmd_start(update, context)

    connections = await runtime.list_connections()
    assert len(connections) == 1
    runtime.assert_owner_a_scope(connections[0])
    assert connections[0]["external_account_id"] == "42"
    assert "connected" in update.message.reply_text.await_args.args[0].lower()


@pytest.mark.anyio
async def test_bound_telegram_message_publishes_connection_identity(runtime):
    connection = await runtime.repository.upsert_connection(
        scope=runtime.seed.owner_a_scope,
        provider="telegram",
        external_account_id="42",
        external_account_name="Alice Example",
        workspace_id="100",
        metadata={"chat_type": "private"},
    )
    bus = MessageBus()
    channel = TelegramChannel(
        bus=bus,
        config={
            "bot_token": "test-token",
            "connection_repo": runtime.repository,
            "connection_service": runtime.service,
        },
    )
    channel._main_loop = __import__("asyncio").get_event_loop()
    channel._send_running_reply = AsyncMock()

    await channel._on_text(_telegram_update(text="hello"), None)
    inbound = await bus.get_inbound()

    runtime.assert_owner_a_scope(connection)
    assert inbound.connection_id == connection["id"]
    assert inbound.owner_user_id == runtime.seed.owner_a_scope.owner_user_id
    assert inbound.project_id == runtime.seed.owner_a_scope.project_id
    assert inbound.workspace_id == "100"
    assert inbound.user_id == "42"
    assert inbound.chat_id == "100"
    assert inbound.text == "hello"
