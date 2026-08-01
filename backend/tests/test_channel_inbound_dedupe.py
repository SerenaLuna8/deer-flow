"""Project-scoped inbound delivery dedupe contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus
from app.private_work.connection_inbound import (
    ProjectInboundDispatchResult,
    build_gateway_project_run_launcher,
)
from app.private_work.errors import PrivateWorkInvalid
from app.private_work.inbound_dedupe import (
    DuplicateInboundDelivery,
    PrivateRunInboundDelivery,
)
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunInboundAuthority,
)


def _authority() -> PrivateRunInboundAuthority:
    return PrivateRunInboundAuthority(
        connection_id="connection-a",
        provider="slack",
        external_account_id="external-a",
        workspace_id="workspace-a",
        external_conversation_id="conversation-a",
        external_topic_id="topic-a",
    )


def test_inbound_delivery_id_is_opaque_case_sensitive_and_nonempty() -> None:
    lower = PrivateRunInboundDelivery("delivery-a")
    upper = PrivateRunInboundDelivery("Delivery-A")

    assert lower.digest != upper.digest
    assert "delivery-a" not in repr(lower)
    with pytest.raises(TypeError):
        PrivateRunInboundDelivery("")


def test_server_context_requires_authority_and_delivery_together() -> None:
    delivery = PrivateRunInboundDelivery("delivery-a")

    with pytest.raises(TypeError):
        PrivateRunAdmissionServerContext(inbound_delivery=delivery)
    with pytest.raises(TypeError):
        PrivateRunAdmissionServerContext(inbound_authority=_authority())

    context = PrivateRunAdmissionServerContext(
        inbound_authority=_authority(),
        inbound_delivery=delivery,
    )
    assert context.inbound_delivery is delivery


@pytest.mark.asyncio
async def test_duplicate_dispatch_does_not_publish_a_second_outbound() -> None:
    bus = MessageBus()
    bus.publish_outbound = AsyncMock()
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = ProjectInboundDispatchResult(
        resolved=SimpleNamespace(),
        state={},
        disposition="duplicate_delivery",
    )
    manager = ChannelManager(
        bus,
        store=None,
        private_inbound_dispatcher=dispatcher,
    )

    await manager._handle_project_inbound_chat(
        InboundMessage(
            channel_name="slack",
            chat_id="conversation-a",
            user_id="external-a",
            text="hello",
            provider_delivery_id="delivery-a",
        )
    )

    bus.publish_outbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_launcher_requires_canonical_provider_delivery_id() -> None:
    private_start = AsyncMock()
    launcher = build_gateway_project_run_launcher(
        app=SimpleNamespace(state=SimpleNamespace()),
        start_private_run_fn=private_start,
    )

    with pytest.raises(PrivateWorkInvalid):
        await launcher(
            SimpleNamespace(user_id="owner-a", request_id="request-a"),
            "thread-a",
            InboundMessage(
                channel_name="slack",
                chat_id="conversation-a",
                user_id="external-a",
                text="hello",
                metadata={"message_id": "legacy-only"},
            ),
            _authority(),
        )

    private_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_launcher_returns_duplicate_disposition_without_waiting() -> None:
    private_start = AsyncMock(
        side_effect=DuplicateInboundDelivery("run-a"),
    )
    launcher = build_gateway_project_run_launcher(
        app=SimpleNamespace(state=SimpleNamespace()),
        start_private_run_fn=private_start,
    )

    result = await launcher(
        SimpleNamespace(user_id="owner-a", request_id="request-a"),
        "thread-a",
        InboundMessage(
            channel_name="slack",
            chat_id="conversation-a",
            user_id="external-a",
            text="hello",
            provider_delivery_id="delivery-a",
        ),
        _authority(),
    )

    assert result.disposition == "duplicate_delivery"
    assert result.state == {}
