from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.channels.feishu import FeishuChannel
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage
from app.channels.service import ChannelService
from app.channels.store import ChannelStore
from deerflow.runtime.private_scope import PrivateResourceScope


def _scope() -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        membership_version=3,
    )


def _repository() -> SimpleNamespace:
    return SimpleNamespace(
        get_thread_id=AsyncMock(return_value="thread-a"),
        set_thread_id=AsyncMock(return_value=True),
        remove_thread_ids=AsyncMock(return_value=True),
        list_thread_ids=AsyncMock(return_value=[]),
    )


@pytest.mark.asyncio
async def test_channel_store_reads_from_connected_postgres_scope() -> None:
    repository = _repository()
    store = ChannelStore(repository)
    scope = _scope()

    result = await store.get_thread_id(
        "feishu",
        "chat-a",
        topic_id="message-a",
        connection_id="connection-a",
        scope=scope,
    )

    assert result == "thread-a"
    repository.get_thread_id.assert_awaited_once_with(
        scope=scope,
        connection_id="connection-a",
        provider="feishu",
        external_conversation_id="chat-a",
        external_topic_id="message-a",
    )


@pytest.mark.asyncio
async def test_channel_store_writes_with_exact_private_scope() -> None:
    repository = _repository()
    store = ChannelStore(repository)
    scope = _scope()

    assert await store.set_thread_id(
        "feishu",
        "chat-a",
        "thread-a",
        topic_id="message-a",
        connection_id="connection-a",
        scope=scope,
    )

    repository.set_thread_id.assert_awaited_once_with(
        scope=scope,
        connection_id="connection-a",
        provider="feishu",
        external_conversation_id="chat-a",
        external_topic_id="message-a",
        thread_id="thread-a",
    )


@pytest.mark.asyncio
async def test_channel_store_remove_and_list_remain_private_scoped() -> None:
    repository = _repository()
    store = ChannelStore(repository)
    scope = _scope()

    assert await store.remove(
        "feishu",
        "chat-a",
        connection_id="connection-a",
        scope=scope,
    )
    assert (
        await store.list_entries(
            "feishu",
            connection_id="connection-a",
            scope=scope,
        )
        == []
    )

    repository.remove_thread_ids.assert_awaited_once_with(
        scope=scope,
        connection_id="connection-a",
        provider="feishu",
        external_conversation_id="chat-a",
        external_topic_id=None,
    )
    repository.list_thread_ids.assert_awaited_once_with(
        scope=scope,
        connection_id="connection-a",
        provider="feishu",
    )


def test_channel_service_wires_channel_store_to_the_database_repository() -> None:
    repository = _repository()

    service = ChannelService(connection_repo=repository)

    assert service.store.repository is repository


def test_channel_store_has_no_filesystem_path_api() -> None:
    store = ChannelStore(_repository())

    assert not hasattr(store, "_path")


@pytest.mark.asyncio
async def test_feishu_resolves_message_alias_after_connection_identity() -> None:
    store = SimpleNamespace(
        get_thread_id=AsyncMock(side_effect=[None, "thread-a"]),
    )
    channel = FeishuChannel(
        MessageBus(),
        {"channel_store": store},
    )
    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="chat-a",
        user_id="external-user-a",
        text="hello",
        connection_id="connection-a",
        private_scope=_scope(),
        metadata={
            "root_id": "root-a",
            "parent_id": "parent-a",
            "thread_id": "provider-thread-a",
        },
    )

    topic_id, found = await channel._resolve_persisted_topic_id(inbound)

    assert (topic_id, found) == ("parent-a", True)
    assert store.get_thread_id.await_args_list[0].kwargs["connection_id"] == "connection-a"
    assert store.get_thread_id.await_args_list[1].kwargs["connection_id"] == "connection-a"
    assert store.get_thread_id.await_args_list[0].kwargs["scope"] is inbound.private_scope
    assert store.get_thread_id.await_args_list[1].kwargs["scope"] is inbound.private_scope


@pytest.mark.asyncio
async def test_feishu_persists_outbound_aliases_with_private_scope() -> None:
    store = SimpleNamespace(set_thread_id=AsyncMock(return_value=True))
    channel = FeishuChannel(
        MessageBus(),
        {"channel_store": store},
    )
    scope = _scope()
    outbound = OutboundMessage(
        channel_name="feishu",
        chat_id="chat-a",
        thread_id="thread-a",
        text="answer",
        connection_id="connection-a",
        private_scope=scope,
        metadata={"root_id": "root-a"},
    )

    await channel._remember_thread_mapping(outbound, "message-a")

    assert store.set_thread_id.await_count == 2
    for call in store.set_thread_id.await_args_list:
        assert call.kwargs["connection_id"] == "connection-a"
        assert call.kwargs["scope"] is scope
