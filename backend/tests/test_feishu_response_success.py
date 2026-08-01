"""Regression tests for Feishu SDK business-level failures."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.feishu import FeishuChannel
from app.channels.message_bus import (
    MessageBus,
    OutboundMessage,
    ResolvedAttachment,
)


class _FluentRequest:
    @classmethod
    def builder(cls) -> _FluentRequest:
        return cls()

    def build(self) -> _FluentRequest:
        return self

    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: self


def _failure_response() -> MagicMock:
    response = MagicMock()
    response.success.return_value = False
    response.code = 99991400
    response.msg = "business failure"
    response.get_log_id.return_value = "log-m14"
    response.data.message_id = "must-not-be-used"
    return response


def _success_response() -> MagicMock:
    response = MagicMock()
    response.success.return_value = True
    response.data.message_id = "message-created"
    return response


def _channel() -> FeishuChannel:
    channel = FeishuChannel(MessageBus(), config={})
    channel._api_client = MagicMock()
    channel._ReplyMessageRequest = _FluentRequest
    channel._ReplyMessageRequestBody = _FluentRequest
    channel._CreateMessageRequest = _FluentRequest
    channel._CreateMessageRequestBody = _FluentRequest
    channel._PatchMessageRequest = _FluentRequest
    channel._PatchMessageRequestBody = _FluentRequest
    channel._CreateMessageReactionRequest = _FluentRequest
    channel._CreateMessageReactionRequestBody = _FluentRequest
    channel._Emoji = _FluentRequest
    return channel


@pytest.mark.asyncio
async def test_feishu_send_file_returns_false_when_reply_business_fails(
    tmp_path,
) -> None:
    channel = _channel()
    channel._upload_image = AsyncMock(return_value="image-key")
    channel._api_client.im.v1.message.reply.return_value = _failure_response()
    path = tmp_path / "image.png"
    path.write_bytes(b"png")

    sent = await channel.send_file(
        OutboundMessage(
            channel_name="feishu",
            chat_id="chat-1",
            thread_id="thread-1",
            text="",
            thread_ts="message-1",
        ),
        ResolvedAttachment(
            virtual_path="/mnt/user-data/outputs/image.png",
            actual_path=path,
            filename="image.png",
            mime_type="image/png",
            size=path.stat().st_size,
            is_image=True,
        ),
    )

    assert sent is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_failure_response(), False),
        (_success_response(), True),
    ],
)
async def test_feishu_send_file_checks_create_business_result(
    tmp_path,
    response: MagicMock,
    expected: bool,
) -> None:
    channel = _channel()
    channel._upload_image = AsyncMock(return_value="image-key")
    channel._api_client.im.v1.message.create.return_value = response
    path = tmp_path / "image.png"
    path.write_bytes(b"png")

    sent = await channel.send_file(
        OutboundMessage(
            channel_name="feishu",
            chat_id="chat-1",
            thread_id="thread-1",
            text="",
        ),
        ResolvedAttachment(
            virtual_path="/mnt/user-data/outputs/image.png",
            actual_path=path,
            filename="image.png",
            mime_type="image/png",
            size=path.stat().st_size,
            is_image=True,
        ),
    )

    assert sent is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "sdk_path"),
    [
        ("_reply_card", "reply"),
        ("_create_card", "create"),
        ("_update_card", "patch"),
    ],
)
async def test_feishu_card_operations_raise_when_sdk_business_fails(
    method_name: str,
    sdk_path: str,
) -> None:
    channel = _channel()
    getattr(channel._api_client.im.v1.message, sdk_path).return_value = _failure_response()

    with pytest.raises(RuntimeError, match="99991400"):
        await getattr(channel, method_name)("message-or-chat-1", "hello")


@pytest.mark.asyncio
async def test_feishu_reaction_business_failure_warns_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = _channel()
    channel._api_client.im.v1.message_reaction.create.return_value = _failure_response()

    with caplog.at_level(logging.WARNING):
        await channel._add_reaction("message-1", "OK")

    assert "99991400" in caplog.text
    assert "log-m14" in caplog.text
