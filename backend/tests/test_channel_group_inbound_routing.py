from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.channels.manager import (
    GROUP_BINDING_AGENT_UNAVAILABLE_MESSAGE,
    GROUP_BINDING_REQUIRED_MESSAGE,
    GROUP_BINDING_UNAVAILABLE_MESSAGE,
    ChannelManager,
)
from app.channels.message_bus import InboundMessage, MessageBus


@pytest.mark.asyncio
async def test_unbound_project_group_is_rejected_before_private_dispatch() -> None:
    bus = MessageBus()
    dispatcher = AsyncMock()
    manager = ChannelManager(
        bus=bus,
        store=None,
        private_inbound_dispatcher=dispatcher,
    )
    outbound = []

    async def capture(message) -> None:
        outbound.append(message)

    bus.subscribe_outbound(capture)
    message = InboundMessage(
        channel_name="feishu",
        chat_id="oc-unbound-group",
        user_id="ou-member",
        text="hello",
        metadata={"chat_type": "group", "group_binding_required": True},
    )

    await manager._handle_project_inbound_chat(message)

    dispatcher.dispatch.assert_not_awaited()
    assert len(outbound) == 1
    assert outbound[0].text == GROUP_BINDING_REQUIRED_MESSAGE
    assert outbound[0].connection_id is None
    assert outbound[0].owner_user_id is None


@pytest.mark.asyncio
async def test_unavailable_project_group_has_a_distinct_retryable_message() -> None:
    bus = MessageBus()
    dispatcher = AsyncMock()
    manager = ChannelManager(
        bus=bus,
        store=None,
        private_inbound_dispatcher=dispatcher,
    )
    outbound = []

    async def capture(message) -> None:
        outbound.append(message)

    bus.subscribe_outbound(capture)
    message = InboundMessage(
        channel_name="feishu",
        chat_id="oc-bound-group",
        user_id="ou-member",
        text="hello",
        metadata={"chat_type": "group", "group_binding_unavailable": True},
    )

    await manager._handle_project_inbound_chat(message)

    dispatcher.dispatch.assert_not_awaited()
    assert len(outbound) == 1
    assert outbound[0].text == GROUP_BINDING_UNAVAILABLE_MESSAGE


@pytest.mark.asyncio
async def test_group_with_unavailable_agent_is_rejected_before_dispatch() -> None:
    bus = MessageBus()
    dispatcher = AsyncMock()
    manager = ChannelManager(
        bus=bus,
        store=None,
        private_inbound_dispatcher=dispatcher,
    )
    outbound = []

    async def capture(message) -> None:
        outbound.append(message)

    bus.subscribe_outbound(capture)
    message = InboundMessage(
        channel_name="feishu",
        chat_id="oc-bound-group",
        user_id="ou-member",
        text="hello",
        metadata={
            "chat_type": "group",
            "group_binding_agent_unavailable": True,
        },
    )

    await manager._handle_project_inbound_chat(message)

    dispatcher.dispatch.assert_not_awaited()
    assert len(outbound) == 1
    assert outbound[0].text == GROUP_BINDING_AGENT_UNAVAILABLE_MESSAGE
