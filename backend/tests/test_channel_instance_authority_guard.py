from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.base import Channel
from app.channels.instance_authority import (
    ChannelInstanceAuthorityGuard,
    ChannelInstanceAuthorityLost,
)
from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage
from app.private_work.connection_inbound import ProjectInboundDispatcher


class _RecordingChannel(Channel):
    def __init__(self, bus: MessageBus, *, channel_instance_id: str) -> None:
        super().__init__(
            "feishu",
            bus,
            {"channel_instance_id": channel_instance_id},
        )
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None:
        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

    async def stop(self) -> None:
        self.bus.unsubscribe_outbound(self._on_outbound)
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


@pytest.mark.asyncio
async def test_legacy_channel_messages_bypass_project_instance_authority() -> None:
    authority = AsyncMock(return_value=False)
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    outbound = AsyncMock()
    bus.subscribe_outbound(outbound)

    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
    )
    reply = OutboundMessage(
        channel_name="feishu",
        chat_id="chat-1",
        thread_id="thread-1",
        text="world",
    )

    await bus.publish_inbound(inbound)
    assert await bus.get_inbound() is inbound
    await bus.publish_outbound(reply)
    outbound.assert_awaited_once_with(reply)
    authority.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_bus_drops_inbound_and_outbound_after_exact_instance_loses_authority() -> None:
    authority = AsyncMock(return_value=False)
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    outbound = AsyncMock()
    bus.subscribe_outbound(outbound)

    inbound = InboundMessage(
        channel_name="feishu",
        channel_instance_id="instance-a",
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
    )
    reply = OutboundMessage(
        channel_name="feishu",
        channel_instance_id="instance-a",
        chat_id="chat-1",
        thread_id="thread-1",
        text="world",
    )

    await bus.publish_inbound(inbound)
    assert bus.inbound_queue.empty()
    await bus.publish_outbound(reply)
    outbound.assert_not_awaited()
    assert authority.await_args_list == [
        (("feishu", "instance-a"),),
        (("feishu", "instance-a"),),
    ]


@pytest.mark.asyncio
async def test_channel_rechecks_authority_immediately_before_provider_send() -> None:
    authority = AsyncMock(side_effect=[True, False])
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    channel = _RecordingChannel(bus, channel_instance_id="instance-a")
    await channel.start()
    try:
        await bus.publish_outbound(
            OutboundMessage(
                channel_name="feishu",
                channel_instance_id="instance-a",
                chat_id="chat-1",
                thread_id="thread-1",
                text="world",
            )
        )
    finally:
        await channel.stop()

    assert authority.await_count == 2
    assert channel.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_factory", "bind_call"),
    [
        (
            lambda bus, config: __import__("app.channels.feishu", fromlist=["FeishuChannel"]).FeishuChannel(bus, config),
            lambda channel: channel._bind_connection_from_connect_code(message_id="message-1", chat_id="chat-1", user_id="user-1", code="code-1"),
        ),
        (
            lambda bus, config: __import__("app.channels.dingtalk", fromlist=["DingTalkChannel"]).DingTalkChannel(bus, config),
            lambda channel: channel._bind_connection_from_connect_code(
                conversation_type="1",
                sender_staff_id="user-1",
                sender_nick="User",
                conversation_id="chat-1",
                code="code-1",
            ),
        ),
        (
            lambda bus, config: __import__("app.channels.slack", fromlist=["SlackChannel"]).SlackChannel(bus, config),
            lambda channel: channel._bind_connection_from_connect_code(event={"channel": "chat-1", "user": "user-1"}, team_id="team-1", code="code-1"),
        ),
        (
            lambda bus, config: __import__("app.channels.telegram", fromlist=["TelegramChannel"]).TelegramChannel(bus, config),
            lambda channel: channel._bind_connection_from_start_token(object(), "code-1"),
        ),
        (
            lambda bus, config: __import__("app.channels.discord", fromlist=["DiscordChannel"]).DiscordChannel(bus, config),
            lambda channel: channel._bind_connection_from_connect_code(object(), "code-1"),
        ),
        (
            lambda bus, config: __import__("app.channels.wechat", fromlist=["WechatChannel"]).WechatChannel(bus, config),
            lambda channel: channel._bind_connection_from_connect_code(chat_id="chat-1", context_token="context-1", code="code-1"),
        ),
        (
            lambda bus, config: __import__("app.channels.wecom", fromlist=["WeComChannel"]).WeComChannel(bus, config),
            lambda channel: channel._bind_connection_from_connect_code(frame={"body": {}}, user_id="user-1", code="code-1"),
        ),
    ],
)
async def test_provider_connection_binding_is_fenced_before_callback(
    channel_factory,
    bind_call,
) -> None:
    authority = AsyncMock(return_value=False)
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    connection_service = SimpleNamespace(complete_callback=AsyncMock())
    channel = channel_factory(
        bus,
        {
            "channel_instance_id": "instance-a",
            "connection_service": connection_service,
        },
    )

    assert await bind_call(channel) is True

    connection_service.complete_callback.assert_not_awaited()
    authority.assert_awaited_once_with(channel.name, "instance-a")


@pytest.mark.asyncio
async def test_slack_feedback_waits_for_guarded_main_loop_prepare() -> None:
    from app.channels.slack import SlackChannel

    authority = AsyncMock(return_value=False)
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    bus.publish_inbound = AsyncMock()
    channel = SlackChannel(bus, {"channel_instance_id": "instance-a"})
    channel._add_reaction = MagicMock()
    channel._send_running_reply = MagicMock()
    inbound = channel._make_inbound(
        chat_id="channel-1",
        user_id="user-1",
        text="hello",
    )

    await channel._prepare_inbound_with_feedback(
        inbound,
        team_id="team-1",
        channel_id="channel-1",
        reaction_ts="message-1",
        thread_ts="message-1",
    )

    channel._add_reaction.assert_not_called()
    channel._send_running_reply.assert_not_called()
    bus.publish_inbound.assert_not_awaited()
    authority.assert_awaited_once_with("slack", "instance-a")


@pytest.mark.asyncio
async def test_discord_checks_authority_before_thread_reaction_or_typing() -> None:
    from app.channels.discord import DiscordChannel

    authority = AsyncMock(return_value=False)
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    channel = DiscordChannel(bus, {"channel_instance_id": "instance-a"})
    channel._running = True
    channel._client = SimpleNamespace(
        user=SimpleNamespace(id=999, mention="<@999>"),
    )
    channel._create_thread = AsyncMock()
    channel._add_reaction = AsyncMock()
    channel._start_typing = AsyncMock()
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=1),
        guild=None,
        content="hello",
    )

    await channel._on_message(message)

    channel._create_thread.assert_not_awaited()
    channel._add_reaction.assert_not_awaited()
    channel._start_typing.assert_not_awaited()
    authority.assert_awaited_once_with("discord", "instance-a")


@pytest.mark.asyncio
async def test_telegram_checks_authority_before_running_reply() -> None:
    from app.channels.telegram import TelegramChannel

    authority = AsyncMock(return_value=False)
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    bus.publish_inbound = AsyncMock()
    channel = TelegramChannel(bus, {"channel_instance_id": "instance-a"})
    channel._send_running_reply = AsyncMock()
    inbound = channel._make_inbound(
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
    )

    await channel._process_incoming_with_reply("chat-1", 1, inbound)

    channel._send_running_reply.assert_not_awaited()
    bus.publish_inbound.assert_not_awaited()
    authority.assert_awaited_once_with("telegram", "instance-a")


@pytest.mark.asyncio
async def test_wecom_checks_authority_before_working_reply() -> None:
    from app.channels.wecom import WeComChannel

    authority = AsyncMock(return_value=False)
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    bus.publish_inbound = AsyncMock()
    channel = WeComChannel(bus, {"channel_instance_id": "instance-a"})
    channel._ws_client = SimpleNamespace(reply_stream=AsyncMock())

    await channel._publish_ws_inbound(
        {
            "body": {
                "msgid": "message-1",
                "from": {"userid": "user-1"},
            }
        },
        "hello",
    )

    channel._ws_client.reply_stream.assert_not_awaited()
    bus.publish_inbound.assert_not_awaited()
    authority.assert_awaited_once_with("wecom", "instance-a")


@pytest.mark.asyncio
async def test_wecom_custom_outbound_rechecks_authority_before_send() -> None:
    from app.channels.wecom import WeComChannel

    authority = AsyncMock(side_effect=[True, False])
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    channel = WeComChannel(bus, {"channel_instance_id": "instance-a"})
    channel.send = AsyncMock()
    bus.subscribe_outbound(channel._on_outbound)

    await bus.publish_outbound(
        OutboundMessage(
            channel_name="wecom",
            channel_instance_id="instance-a",
            chat_id="chat-1",
            thread_id="thread-1",
            text="reply",
        )
    )

    assert authority.await_count == 2
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_manager_rechecks_authority_for_an_already_queued_exact_instance_message() -> None:
    authority = AsyncMock(return_value=True)
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    manager = ChannelManager(bus=bus, store=None)
    manager._handle_project_inbound_chat = AsyncMock()
    inbound = InboundMessage(
        channel_name="feishu",
        channel_instance_id="instance-a",
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
    )

    await bus.publish_inbound(inbound)
    authority.return_value = False
    queued = await bus.get_inbound()
    await manager._handle_message(queued)

    manager._handle_project_inbound_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_loop_drops_stale_queue_entry_before_local_dedupe() -> None:
    authority = AsyncMock(return_value=True)
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    manager = ChannelManager(bus=bus, store=None)
    manager._handle_project_inbound_chat = AsyncMock()
    inbound = InboundMessage(
        channel_name="feishu",
        channel_instance_id="instance-a",
        chat_id="chat-1",
        user_id="user-1",
        workspace_id="tenant-1",
        provider_delivery_id="delivery-1",
        text="hello",
    )

    await bus.publish_inbound(inbound)
    authority.return_value = False
    await manager.start()
    try:
        for _ in range(100):
            if bus.inbound_queue.empty():
                break
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)
    finally:
        await manager.stop()

    assert manager._recent_inbound_events == {}
    manager._handle_project_inbound_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_manager_rechecks_authority_after_waiting_for_dispatch_capacity() -> None:
    authority = AsyncMock(side_effect=[True, False])
    bus = MessageBus(
        instance_authority_guard=ChannelInstanceAuthorityGuard(authority),
    )
    manager = ChannelManager(bus=bus, store=None)
    manager._handle_project_inbound_chat = AsyncMock()
    manager._semaphore = asyncio.Semaphore(1)
    await manager._semaphore.acquire()
    inbound = InboundMessage(
        channel_name="feishu",
        channel_instance_id="instance-a",
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
    )

    task = asyncio.create_task(manager._handle_message(inbound))
    for _ in range(100):
        if authority.await_count == 1:
            break
        await asyncio.sleep(0.001)
    manager._semaphore.release()
    await task

    assert authority.await_count == 2
    manager._handle_project_inbound_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_dispatcher_rechecks_authority_after_resolution_before_run_launch() -> None:
    authority = AsyncMock(side_effect=[True, False])
    guard = ChannelInstanceAuthorityGuard(authority)
    resolver = AsyncMock(
        return_value=SimpleNamespace(
            context=object(),
            thread_id="thread-1",
            authority=object(),
        )
    )
    launcher = AsyncMock()
    dispatcher = ProjectInboundDispatcher(
        SimpleNamespace(resolve=resolver),
        launcher,
        instance_authority_guard=guard,
    )
    inbound = InboundMessage(
        channel_name="feishu",
        channel_instance_id="instance-a",
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
    )

    with pytest.raises(ChannelInstanceAuthorityLost):
        await dispatcher.dispatch(inbound)

    resolver.assert_awaited_once()
    launcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_authority_callback_failure_is_fail_closed_without_leaking_error() -> None:
    async def authority(_provider: str, _channel_instance_id: str) -> bool:
        raise RuntimeError("credential-like-private-detail")

    guard = ChannelInstanceAuthorityGuard(authority)

    assert await guard.allows("feishu", "instance-a") is False
