from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.channels.manager as manager_module
from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage
from app.private_work.connection_inbound import ProjectInboundDispatchResult
from app.private_work.errors import LegacyAdmissionBusy, PrivateWorkTooLarge


def _message() -> InboundMessage:
    return InboundMessage(
        channel_name="test-channel",
        channel_instance_id="test-channel:primary",
        chat_id="external-conversation",
        user_id="external-user",
        workspace_id="external-workspace",
        provider_delivery_id="provider-delivery-1",
        text="Create the presentation.",
    )


class _Dispatcher:
    def __init__(self, outcomes: list[BaseException | ProjectInboundDispatchResult]) -> None:
        self._outcomes = outcomes
        self.calls = 0
        self.completed = asyncio.Event()

    async def dispatch(self, _message: InboundMessage) -> ProjectInboundDispatchResult:
        index = min(self.calls, len(self._outcomes) - 1)
        self.calls += 1
        outcome = self._outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        self.completed.set()
        return outcome


def _duplicate_result() -> ProjectInboundDispatchResult:
    return ProjectInboundDispatchResult(
        resolved=SimpleNamespace(),  # type: ignore[arg-type]
        state={},
        disposition="duplicate_delivery",
    )


async def _start_manager(
    dispatcher: _Dispatcher,
) -> tuple[ChannelManager, list[OutboundMessage]]:
    bus = MessageBus()
    outbound: list[OutboundMessage] = []

    async def capture(message: OutboundMessage) -> None:
        outbound.append(message)

    bus.subscribe_outbound(capture)
    manager = ChannelManager(
        bus,
        None,
        private_inbound_dispatcher=dispatcher,  # type: ignore[arg-type]
    )
    await manager.start()
    return manager, outbound


@pytest.mark.asyncio
async def test_channel_busy_retries_the_same_delivery_without_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        manager_module,
        "LEGACY_ADMISSION_RETRY_DELAY_SECONDS",
        0.01,
        raising=False,
    )
    dispatcher = _Dispatcher(
        [LegacyAdmissionBusy("busy"), _duplicate_result()],
    )
    manager, outbound = await _start_manager(dispatcher)
    try:
        assert await manager.bus.publish_inbound(_message()) is True
        async with asyncio.timeout(2):
            await dispatcher.completed.wait()
        assert dispatcher.calls == 2
        assert outbound == []
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_channel_busy_retry_is_bounded_and_remains_explicitly_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        manager_module,
        "LEGACY_ADMISSION_RETRY_DELAY_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(
        manager_module,
        "LEGACY_ADMISSION_MAX_RETRIES",
        2,
        raising=False,
    )
    dispatcher = _Dispatcher([LegacyAdmissionBusy("busy")])
    manager, outbound = await _start_manager(dispatcher)
    try:
        assert await manager.bus.publish_inbound(_message()) is True
        async with asyncio.timeout(2):
            while not outbound:
                await asyncio.sleep(0)
        assert dispatcher.calls == 3
        assert len(outbound) == 1
        assert outbound[0].text == manager_module.LEGACY_ADMISSION_RETRYABLE_MESSAGE
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_channel_oversize_is_permanent_and_provider_redelivery_is_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        manager_module,
        "LEGACY_ADMISSION_RETRY_DELAY_SECONDS",
        0.01,
        raising=False,
    )
    dispatcher = _Dispatcher([PrivateWorkTooLarge("oversize")])
    manager, outbound = await _start_manager(dispatcher)
    message = _message()
    try:
        assert await manager.bus.publish_inbound(message) is True
        async with asyncio.timeout(2):
            while not outbound:
                await asyncio.sleep(0)
        assert dispatcher.calls == 1
        assert outbound[0].text == PrivateWorkTooLarge.public_message

        assert await manager.bus.publish_inbound(message) is True
        await asyncio.sleep(0.05)
        assert dispatcher.calls == 1
        assert len(outbound) == 1
    finally:
        await manager.stop()
