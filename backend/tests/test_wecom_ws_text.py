"""Regression tests for WeCom websocket text quote parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.channels.message_bus import MessageBus
from app.channels.wecom import WeComChannel


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"text": {"content": "hello"}, "quote": None},
        {"text": {"content": "hello"}, "quote": {"text": None}},
        {
            "text": {"content": "hello"},
            "quote": {"text": {"content": None}},
        },
    ],
)
async def test_wecom_text_accepts_null_quote_shapes(body: dict) -> None:
    channel = WeComChannel(MessageBus(), config={})
    channel._publish_ws_inbound = AsyncMock()

    await channel._on_ws_text({"body": body})

    channel._publish_ws_inbound.assert_awaited_once()
    assert channel._publish_ws_inbound.await_args.args[1] == "hello"


@pytest.mark.asyncio
async def test_wecom_quote_only_text_is_preserved() -> None:
    channel = WeComChannel(MessageBus(), config={})
    channel._publish_ws_inbound = AsyncMock()

    frame = {
        "body": {
            "text": None,
            "quote": {"text": {"content": "quoted"}},
        }
    }
    await channel._on_ws_text(frame)

    channel._publish_ws_inbound.assert_awaited_once_with(
        frame,
        "\nQuote message: quoted",
    )


@pytest.mark.asyncio
async def test_wecom_empty_null_text_and_quote_are_ignored() -> None:
    channel = WeComChannel(MessageBus(), config={})
    channel._publish_ws_inbound = AsyncMock()

    await channel._on_ws_text(
        {
            "body": {
                "text": None,
                "quote": None,
            }
        }
    )

    channel._publish_ws_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_wecom_text_and_quote_are_combined() -> None:
    channel = WeComChannel(MessageBus(), config={})
    channel._publish_ws_inbound = AsyncMock()
    frame = {
        "body": {
            "text": {"content": "answer"},
            "quote": {"text": {"content": "question"}},
        }
    }

    await channel._on_ws_text(frame)

    channel._publish_ws_inbound.assert_awaited_once_with(
        frame,
        "answer\nQuote message: question",
    )
