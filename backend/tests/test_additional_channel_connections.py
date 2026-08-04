"""Connection binding tests for browser-connectable IM channels beyond Telegram/Slack/Discord."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from support.m4_channel_connections import make_m4_channel_connection_runtime

from app.channels.base import Channel
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage


class _StubChannel(Channel):
    """Minimal concrete Channel used to exercise base-class helpers directly."""

    async def start(self) -> None:  # pragma: no cover - not exercised
        pass

    async def stop(self) -> None:  # pragma: no cover - not exercised
        pass

    async def send(self, msg: OutboundMessage) -> None:  # pragma: no cover - not exercised
        pass


def test_pending_connect_code_extracts_code_when_connections_configured():
    channel = _StubChannel(name="stub", bus=MessageBus(), config={"connection_repo": object()})
    # A connect command yields its code; ordinary text does not.
    assert channel._pending_connect_code("/connect abc123") == "abc123"
    assert channel._pending_connect_code("hello world") is None


def test_pending_connect_code_is_none_when_connections_disabled():
    # With no connection repo, binding is not configured and connect codes are
    # ignored so the message falls through to normal handling.
    channel = _StubChannel(name="stub", bus=MessageBus(), config={})
    assert channel._pending_connect_code("/connect abc123") is None


_DATABASE_URL: str | None = None


@pytest_asyncio.fixture()
async def _postgres_database(migrated_postgres_database_url):
    global _DATABASE_URL
    _DATABASE_URL = migrated_postgres_database_url
    try:
        yield
    finally:
        _DATABASE_URL = None


async def _make_runtime(name: str):
    assert _DATABASE_URL is not None
    return await make_m4_channel_connection_runtime(
        _DATABASE_URL,
        cipher_key=f"{name}-secret",
    )


def test_feishu_connect_command_binds_identity(_postgres_database):
    import anyio

    from app.channels.feishu import FeishuChannel

    async def go():
        runtime = await _make_runtime("feishu")
        try:
            state = await runtime.begin_connect("feishu")
            channel = FeishuChannel(
                bus=MessageBus(),
                config={
                    "app_id": "app",
                    "app_secret": "secret",
                    "connection_repo": runtime.repository,
                    "connection_service": runtime.service,
                },
            )
            channel._reply_card = AsyncMock()

            handled = await channel._bind_connection_from_connect_code(
                message_id="om-message-1",
                chat_id="oc-chat-1",
                user_id="ou-user-1",
                code=state,
            )

            connections = await runtime.list_connections()
            assert handled is True
            assert len(connections) == 1
            runtime.assert_owner_a_scope(connections[0])
            assert connections[0]["provider"] == "feishu"
            assert connections[0]["external_account_id"] == "ou-user-1"
            assert connections[0]["workspace_id"] == "oc-chat-1"
            channel._reply_card.assert_awaited_once_with("om-message-1", "Feishu connected to ActWeave.")
        finally:
            await runtime.seed.engine.dispose()

    anyio.run(go)


def test_feishu_connect_confirmation_falls_back_to_new_chat_card():
    import anyio

    from app.channels.feishu import FeishuChannel

    async def go():
        channel = FeishuChannel(
            bus=MessageBus(),
            config={"app_id": "app", "app_secret": "secret"},
        )
        channel._reply_card = AsyncMock(side_effect=RuntimeError("reply failed"))
        channel._create_card = AsyncMock()

        await channel._send_connection_confirmation(
            message_id="om-message-1",
            chat_id="oc-chat-1",
            text="Feishu connected to ActWeave.",
        )

        channel._reply_card.assert_awaited_once_with(
            "om-message-1",
            "Feishu connected to ActWeave.",
        )
        channel._create_card.assert_awaited_once_with(
            "oc-chat-1",
            "Feishu connected to ActWeave.",
        )

    anyio.run(go)


def test_dingtalk_connect_command_binds_identity(_postgres_database):
    import anyio

    from app.channels.dingtalk import _CONVERSATION_TYPE_GROUP, DingTalkChannel

    async def go():
        runtime = await _make_runtime("dingtalk")
        try:
            state = await runtime.begin_connect("dingtalk")
            channel = DingTalkChannel(
                bus=MessageBus(),
                config={
                    "client_id": "client",
                    "client_secret": "secret",
                    "connection_repo": runtime.repository,
                    "connection_service": runtime.service,
                },
            )
            channel._send_connection_reply = AsyncMock()

            handled = await channel._bind_connection_from_connect_code(
                conversation_type=_CONVERSATION_TYPE_GROUP,
                sender_staff_id="staff-user-1",
                sender_nick="Alice",
                conversation_id="cid-group-1",
                code=state,
            )

            connections = await runtime.list_connections()
            assert handled is True
            assert len(connections) == 1
            runtime.assert_owner_a_scope(connections[0])
            assert connections[0]["provider"] == "dingtalk"
            assert connections[0]["external_account_id"] == "staff-user-1"
            assert connections[0]["external_account_name"] == "Alice"
            assert connections[0]["workspace_id"] == "cid-group-1"
            channel._send_connection_reply.assert_awaited_once()
        finally:
            await runtime.seed.engine.dispose()

    anyio.run(go)


def test_wechat_connect_command_binds_identity(_postgres_database):
    import anyio

    from app.channels.wechat import WechatChannel

    async def go():
        runtime = await _make_runtime("wechat")
        try:
            state = await runtime.begin_connect("wechat")
            channel = WechatChannel(
                bus=MessageBus(),
                config={
                    "bot_token": "token",
                    "connection_repo": runtime.repository,
                    "connection_service": runtime.service,
                },
            )
            channel._send_connection_reply = AsyncMock()

            handled = await channel._bind_connection_from_connect_code(
                chat_id="wx-user-1",
                context_token="ctx-1",
                code=state,
            )

            connections = await runtime.list_connections()
            assert handled is True
            assert len(connections) == 1
            runtime.assert_owner_a_scope(connections[0])
            assert connections[0]["provider"] == "wechat"
            assert connections[0]["external_account_id"] == "wx-user-1"
            assert connections[0]["workspace_id"] == "wx-user-1"
            channel._send_connection_reply.assert_awaited_once_with("wx-user-1", "ctx-1", "WeChat connected to ActWeave.")
        finally:
            await runtime.seed.engine.dispose()

    anyio.run(go)


def test_wecom_connect_command_binds_identity(_postgres_database):
    import anyio

    from app.channels.wecom import WeComChannel

    async def go():
        runtime = await _make_runtime("wecom")
        try:
            state = await runtime.begin_connect("wecom")
            channel = WeComChannel(
                bus=MessageBus(),
                config={
                    "bot_id": "bot",
                    "bot_secret": "secret",
                    "connection_repo": runtime.repository,
                    "connection_service": runtime.service,
                },
            )
            channel._ws_client = MagicMock()
            channel._ws_client.reply = AsyncMock()
            frame = {"body": {"aibotid": "bot-1", "chattype": "single"}}

            handled = await channel._bind_connection_from_connect_code(
                frame=frame,
                user_id="wecom-user-1",
                code=state,
            )

            connections = await runtime.list_connections()
            assert handled is True
            assert len(connections) == 1
            runtime.assert_owner_a_scope(connections[0])
            assert connections[0]["provider"] == "wecom"
            assert connections[0]["external_account_id"] == "wecom-user-1"
            assert connections[0]["workspace_id"] == "bot-1"
            channel._ws_client.reply.assert_awaited_once_with(
                frame,
                {"msgtype": "text", "text": {"content": "WeCom connected to ActWeave."}},
            )
        finally:
            await runtime.seed.engine.dispose()

    anyio.run(go)


def test_additional_channels_attach_project_identity(_postgres_database):
    import anyio

    from app.channels.dingtalk import _CONVERSATION_TYPE_GROUP, DingTalkChannel
    from app.channels.feishu import FeishuChannel
    from app.channels.wechat import WechatChannel
    from app.channels.wecom import WeComChannel

    async def go():
        runtime = await _make_runtime("additional-identity")
        repo = runtime.repository
        for provider, external_account_id, workspace_id in (
            ("feishu", "ou-user-1", "oc-chat-1"),
            ("dingtalk", "staff-user-1", "cid-group-1"),
            ("wechat", "wx-user-1", "wx-user-1"),
            ("wecom", "wecom-user-1", "bot-1"),
        ):
            connection = await repo.upsert_connection(
                scope=runtime.seed.owner_a_scope,
                provider=provider,
                external_account_id=external_account_id,
                workspace_id=workspace_id,
            )
            runtime.assert_owner_a_scope(connection)

        cases = [
            (
                FeishuChannel(
                    bus=MessageBus(),
                    config={"connection_repo": repo, "connection_service": runtime.service},
                ),
                InboundMessage(channel_name="feishu", chat_id="oc-chat-1", user_id="ou-user-1", text="hello"),
            ),
            (
                DingTalkChannel(
                    bus=MessageBus(),
                    config={"connection_repo": repo, "connection_service": runtime.service},
                ),
                InboundMessage(
                    channel_name="dingtalk",
                    chat_id="cid-group-1",
                    user_id="staff-user-1",
                    text="hello",
                    metadata={
                        "conversation_type": _CONVERSATION_TYPE_GROUP,
                        "conversation_id": "cid-group-1",
                    },
                ),
            ),
            (
                WechatChannel(
                    bus=MessageBus(),
                    config={"connection_repo": repo, "connection_service": runtime.service},
                ),
                InboundMessage(channel_name="wechat", chat_id="wx-user-1", user_id="wx-user-1", text="hello"),
            ),
            (
                WeComChannel(
                    bus=MessageBus(),
                    config={"connection_repo": repo, "connection_service": runtime.service},
                ),
                InboundMessage(
                    channel_name="wecom",
                    chat_id="wecom-user-1",
                    user_id="wecom-user-1",
                    text="hello",
                    metadata={"aibotid": "bot-1"},
                ),
            ),
        ]

        for channel, inbound in cases:
            attached = await channel._attach_connection_identity(inbound)
            assert attached.owner_user_id == runtime.seed.owner_a_scope.owner_user_id
            assert attached.project_id == runtime.seed.owner_a_scope.project_id
            assert attached.connection_id
            assert (
                attached.workspace_id
                == {
                    "feishu": "oc-chat-1",
                    "dingtalk": "cid-group-1",
                    "wechat": "wx-user-1",
                    "wecom": "bot-1",
                }[channel.name]
            )

        await runtime.seed.engine.dispose()

    anyio.run(go)
