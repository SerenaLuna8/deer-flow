import asyncio
import json
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channel_group_bindings.errors import (
    GroupBindingAgentUnavailable,
    GroupBindingNotFound,
    GroupBindingUnavailable,
)
from app.channels.commands import KNOWN_CHANNEL_COMMANDS, parse_group_bind_command
from app.channels.feishu import FeishuChannel
from app.channels.message_bus import (
    PENDING_CLARIFICATION_METADATA_KEY,
    RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY,
    InboundMessage,
    MessageBus,
    OutboundMessage,
)
from deerflow.runtime.private_scope import PrivateResourceScope


def _pending(
    topic_id: str,
    *,
    thread_id: str | None = None,
    source_message_id: str | None = None,
    card_message_id: str | None = None,
    created_at: float = 9999999999,
) -> dict:
    return {
        "thread_id": thread_id or f"deer-thread-{topic_id}",
        "topic_id": topic_id,
        "source_message_id": source_message_id or topic_id,
        "card_message_id": card_message_id or f"card-{topic_id}",
        "created_at": created_at,
    }


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_feishu_on_message_plain_text():
    bus = MessageBus()
    config = {"app_id": "test", "app_secret": "test"}
    channel = FeishuChannel(bus, config)

    # Create mock event
    event = MagicMock()
    event.event.message.chat_id = "chat_1"
    event.event.message.message_id = "msg_1"
    event.event.message.root_id = None
    event.event.sender.sender_id.open_id = "user_1"

    # Plain text content
    content_dict = {"text": "Hello world"}
    event.event.message.content = json.dumps(content_dict)

    # Call _on_message
    channel._on_message(event)

    # Since main_loop isn't running in this synchronous test, we can't easily assert on bus,
    # but we can intercept _make_inbound to check the parsed text.
    with pytest.MonkeyPatch.context() as m:
        mock_make_inbound = MagicMock()
        m.setattr(channel, "_make_inbound", mock_make_inbound)
        channel._on_message(event)

        mock_make_inbound.assert_called_once()
        assert mock_make_inbound.call_args[1]["text"] == "Hello world"


@pytest.mark.asyncio
async def test_feishu_group_binding_command_is_consumed_without_creating_inbound():
    bus = MessageBus()
    channel = FeishuChannel(
        bus,
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_group_binding_service": MagicMock(),
        },
    )
    channel._main_loop = asyncio.get_running_loop()
    channel._bind_group_from_code = AsyncMock(return_value=True)
    channel._schedule_prepare_inbound = MagicMock()
    event = _make_text_event(
        "/bind-project group-code",
        chat_id="oc-group-1",
        message_id="om-bind-1",
        user_id="ou-admin-1",
        chat_type="group",
    )

    channel._on_message(event)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    channel._bind_group_from_code.assert_awaited_once_with(
        message_id="om-bind-1",
        chat_id="oc-group-1",
        user_id="ou-admin-1",
        code="group-code",
        chat_type="group",
    )
    channel._schedule_prepare_inbound.assert_not_called()


@pytest.mark.asyncio
async def test_feishu_group_binding_command_accepts_leading_bot_mention():
    channel = FeishuChannel(
        MessageBus(),
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_group_binding_service": MagicMock(),
        },
    )
    channel._main_loop = asyncio.get_running_loop()
    channel._bind_group_from_code = AsyncMock(return_value=True)
    channel._schedule_prepare_inbound = MagicMock()
    event = _make_text_event(
        "@ActWeave /bind-project group-code",
        chat_id="oc-group-1",
        message_id="om-bind-mention-1",
        user_id="ou-admin-1",
        chat_type="group",
    )

    channel._on_message(event)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    channel._bind_group_from_code.assert_awaited_once_with(
        message_id="om-bind-mention-1",
        chat_id="oc-group-1",
        user_id="ou-admin-1",
        code="group-code",
        chat_type="group",
    )
    channel._schedule_prepare_inbound.assert_not_called()


@pytest.mark.parametrize(
    "text",
    (
        "/bind-project",
        "/bind-project group-code extra",
        "@ActWeave /bind-project",
        "@ActWeave /bind-project group-code extra",
    ),
)
def test_group_binding_command_parser_rejects_incomplete_or_extra_arguments(text):
    parsed = parse_group_bind_command(text)

    assert parsed.matched is True
    assert parsed.code is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "/bind-project",
        "/bind-project group-code extra",
    ),
)
async def test_feishu_invalid_group_binding_command_is_consumed(text):
    channel = FeishuChannel(
        MessageBus(),
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_group_binding_service": MagicMock(),
        },
    )
    channel._main_loop = asyncio.get_running_loop()
    channel._bind_group_from_code = AsyncMock(return_value=True)
    channel._send_connection_confirmation = AsyncMock()
    channel._schedule_prepare_inbound = MagicMock()
    event = _make_text_event(
        text,
        chat_id="oc-group-1",
        message_id="om-bind-invalid-1",
        user_id="ou-admin-1",
        chat_type="group",
    )

    channel._on_message(event)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    channel._bind_group_from_code.assert_not_awaited()
    channel._send_connection_confirmation.assert_awaited_once_with(
        message_id="om-bind-invalid-1",
        chat_id="oc-group-1",
        text=("The Feishu group connection command is incomplete or malformed. Copy the full command from Project Settings and send it again."),
    )
    channel._schedule_prepare_inbound.assert_not_called()


@pytest.mark.asyncio
async def test_feishu_bound_group_uses_guest_identity_before_personal_connection():
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    personal_repo = MagicMock()
    personal_repo.find_connection_by_external_identity = AsyncMock(
        return_value={
            "id": "personal-connection",
            "project_id": "11111111-1111-4111-8111-111111111111",
            "owner_user_id": "22222222-2222-4222-8222-222222222222",
            "membership_version": 1,
        }
    )
    group_service = MagicMock()
    group_service.resolve_or_create_guest = AsyncMock(
        return_value={
            "id": "guest-connection",
            "project_id": "33333333-3333-4333-8333-333333333333",
            "owner_user_id": "44444444-4444-4444-8444-444444444444",
            "membership_version": 1,
            "resolved_conversation_id": "group-hmac-ref",
            "resolved_topic_id": "topic-hmac-ref",
        }
    )
    channel = FeishuChannel(
        bus,
        {
            "app_id": "test",
            "app_secret": "test",
            "connection_repo": personal_repo,
            "channel_group_binding_service": group_service,
        },
    )
    channel._add_reaction = AsyncMock()
    channel._ensure_running_card_started = MagicMock()
    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="oc-group-1",
        user_id="ou-member-1",
        text="hello",
        topic_id="om-root-1",
        metadata={
            "chat_type": "group",
            "message_id": "om-message-1",
            "root_id": None,
            "parent_id": None,
            "thread_id": None,
            "topic_id": "om-root-1",
        },
    )

    await channel._prepare_inbound("om-message-1", inbound)

    group_service.resolve_or_create_guest.assert_awaited_once_with(
        provider="feishu",
        channel_instance_id=channel.channel_instance_id,
        chat_id="oc-group-1",
        sender_id="ou-member-1",
        topic_id="om-root-1",
    )
    personal_repo.find_connection_by_external_identity.assert_not_awaited()
    assert inbound.connection_id == "guest-connection"
    assert inbound.private_scope is not None
    assert inbound.private_scope.owner_user_id == "44444444-4444-4444-8444-444444444444"
    assert inbound.private_scope.project_id == "33333333-3333-4333-8333-333333333333"
    assert inbound.resolved_conversation_id == "group-hmac-ref"
    assert inbound.resolved_topic_id == "topic-hmac-ref"


@pytest.mark.asyncio
async def test_feishu_guest_pending_clarification_refreshes_pseudonymous_topic():
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    group_service = MagicMock()
    group_service.resolve_or_create_guest = AsyncMock(
        return_value={
            "id": "guest-connection",
            "project_id": "33333333-3333-4333-8333-333333333333",
            "owner_user_id": "44444444-4444-4444-8444-444444444444",
            "membership_version": 1,
            "resolved_conversation_id": "group-hmac-ref",
            "resolved_topic_id": "initial-topic-hmac-ref",
        }
    )
    group_service.pseudonymize_topic_aliases.return_value = ("pending-topic-hmac-ref",)
    channel = FeishuChannel(
        bus,
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_group_binding_service": group_service,
        },
    )
    channel._add_reaction = AsyncMock()
    channel._ensure_running_card_started = MagicMock()
    channel._pending_clarifications[channel._pending_key("oc-group-1", "ou-member-1")] = [_pending("om-original-topic")]
    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="oc-group-1",
        user_id="ou-member-1",
        text="answer",
        topic_id="om-new-message",
        metadata={
            "chat_type": "group",
            "message_id": "om-new-message",
            "root_id": None,
            "parent_id": None,
            "thread_id": None,
            "topic_id": "om-new-message",
            RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY: False,
        },
    )

    await channel._prepare_inbound("om-new-message", inbound)

    assert inbound.topic_id == "om-original-topic"
    assert inbound.resolved_topic_id == "pending-topic-hmac-ref"
    assert inbound.metadata[RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY] is True
    group_service.pseudonymize_topic_aliases.assert_called_once_with(
        provider="feishu",
        channel_instance_id=channel.channel_instance_id,
        chat_id="oc-group-1",
        resolved_conversation_id="group-hmac-ref",
        topic_ids=("om-original-topic",),
    )


@pytest.mark.asyncio
async def test_feishu_guest_pending_clarification_fails_closed_without_topic_alias():
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    group_service = MagicMock()
    group_service.resolve_or_create_guest = AsyncMock(
        return_value={
            "id": "guest-connection",
            "project_id": "33333333-3333-4333-8333-333333333333",
            "owner_user_id": "44444444-4444-4444-8444-444444444444",
            "membership_version": 1,
            "resolved_conversation_id": "group-hmac-ref",
            "resolved_topic_id": "initial-topic-hmac-ref",
        }
    )
    group_service.pseudonymize_topic_aliases.return_value = ()
    channel = FeishuChannel(
        bus,
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_group_binding_service": group_service,
        },
    )
    channel._add_reaction = AsyncMock()
    channel._ensure_running_card_started = MagicMock()
    channel._pending_clarifications[channel._pending_key("oc-group-1", "ou-member-1")] = [_pending("om-original-topic")]
    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="oc-group-1",
        user_id="ou-member-1",
        text="answer",
        topic_id="om-new-message",
        metadata={
            "chat_type": "group",
            RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY: False,
        },
    )

    await channel._prepare_inbound("om-new-message", inbound)

    assert inbound.resolved_topic_id is None
    assert inbound.metadata["group_binding_unavailable"] is True


@pytest.mark.asyncio
async def test_feishu_unbound_group_does_not_fall_back_to_personal_connection():
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    personal_repo = MagicMock()
    personal_repo.find_connection_by_external_identity = AsyncMock(
        return_value={
            "id": "personal-connection",
            "project_id": "11111111-1111-4111-8111-111111111111",
            "owner_user_id": "22222222-2222-4222-8222-222222222222",
            "membership_version": 1,
        }
    )
    group_service = MagicMock()
    group_service.resolve_or_create_guest = AsyncMock(return_value=None)
    channel = FeishuChannel(
        bus,
        {
            "app_id": "test",
            "app_secret": "test",
            "connection_repo": personal_repo,
            "channel_group_binding_service": group_service,
        },
    )
    channel._add_reaction = AsyncMock()
    channel._ensure_running_card_started = MagicMock()
    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="oc-group-1",
        user_id="ou-member-1",
        text="hello",
        topic_id="om-root-1",
        metadata={
            "chat_type": "group",
            "message_id": "om-message-1",
            "root_id": None,
            "parent_id": None,
            "thread_id": None,
            "topic_id": "om-root-1",
        },
    )

    await channel._prepare_inbound("om-message-1", inbound)

    personal_repo.find_connection_by_external_identity.assert_not_awaited()
    assert inbound.connection_id is None
    assert inbound.metadata["group_binding_required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "metadata_key"),
    (
        (GroupBindingNotFound("group-not-found"), "group_binding_required"),
        (
            GroupBindingAgentUnavailable("group-agent-unavailable"),
            "group_binding_agent_unavailable",
        ),
        (GroupBindingUnavailable("group-unavailable"), "group_binding_unavailable"),
    ),
)
async def test_feishu_group_resolution_errors_are_preserved_for_clear_rejection(
    error,
    metadata_key,
):
    group_service = MagicMock()
    group_service.resolve_or_create_guest = AsyncMock(side_effect=error)
    channel = FeishuChannel(
        MessageBus(),
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_group_binding_service": group_service,
        },
    )
    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="oc-group-1",
        user_id="ou-member-1",
        text="hello",
        metadata={"chat_type": "group"},
    )

    await channel._attach_connection_identity(inbound)

    assert inbound.connection_id is None
    assert inbound.metadata[metadata_key] is True


@pytest.mark.asyncio
async def test_feishu_group_binding_persists_provider_group_name() -> None:
    service = MagicMock()
    service.complete_challenge = AsyncMock()
    channel = FeishuChannel(
        MessageBus(),
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_group_binding_service": service,
        },
    )
    channel._has_instance_authority = AsyncMock(return_value=True)
    channel._resolve_group_display_name = AsyncMock(return_value="研发群")
    channel._send_connection_confirmation = AsyncMock()

    handled = await channel._bind_group_from_code(
        message_id="om-bind",
        chat_id="oc-group",
        user_id="ou-sender",
        code="valid-binding-code",
        chat_type="group",
    )

    assert handled is True
    service.complete_challenge.assert_awaited_once_with(
        provider="feishu",
        channel_instance_id=channel.channel_instance_id,
        code="valid-binding-code",
        chat_id="oc-group",
        sender_id="ou-sender",
        display_name="研发群",
    )


@pytest.mark.asyncio
async def test_feishu_group_name_lookup_is_best_effort() -> None:
    response = MagicMock()
    response.success.return_value = True
    response.data.name = "  产品讨论群  "
    channel = FeishuChannel(
        MessageBus(),
        {"app_id": "test", "app_secret": "test"},
    )
    channel._api_client = MagicMock()
    channel._api_client.im.v1.chat.aget = AsyncMock(return_value=response)

    assert await channel._resolve_group_display_name("oc-group") == "产品讨论群"


def test_feishu_is_not_running_when_ws_thread_exits():
    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
    channel._running = True
    channel._thread = MagicMock()
    channel._thread.is_alive.return_value = False

    assert channel.is_running is False


def test_feishu_event_handler_ignores_non_content_message_events():
    import lark_oapi as lark

    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})

    event_handler = channel._build_event_handler(lark)

    assert "p2.im.message.receive_v1" in event_handler._processorMap
    assert "p2.im.message.message_read_v1" in event_handler._processorMap
    assert "p2.im.message.reaction.created_v1" in event_handler._processorMap
    assert "p2.im.message.reaction.deleted_v1" in event_handler._processorMap
    assert "p2.im.message.recalled_v1" in event_handler._processorMap


def test_feishu_on_message_rich_text():
    bus = MessageBus()
    config = {"app_id": "test", "app_secret": "test"}
    channel = FeishuChannel(bus, config)

    # Create mock event
    event = MagicMock()
    event.event.message.chat_id = "chat_1"
    event.event.message.message_id = "msg_1"
    event.event.message.root_id = None
    event.event.sender.sender_id.open_id = "user_1"

    # Rich text content (topic group / post)
    content_dict = {"content": [[{"tag": "text", "text": "Paragraph 1, part 1."}, {"tag": "text", "text": "Paragraph 1, part 2."}], [{"tag": "at", "text": "@bot"}, {"tag": "text", "text": " Paragraph 2."}]]}
    event.event.message.content = json.dumps(content_dict)

    with pytest.MonkeyPatch.context() as m:
        mock_make_inbound = MagicMock()
        m.setattr(channel, "_make_inbound", mock_make_inbound)
        channel._on_message(event)

        mock_make_inbound.assert_called_once()
        parsed_text = mock_make_inbound.call_args[1]["text"]

        # Expected text:
        # Paragraph 1, part 1. Paragraph 1, part 2.
        #
        # @bot  Paragraph 2.
        assert "Paragraph 1, part 1. Paragraph 1, part 2." in parsed_text
        assert "@bot  Paragraph 2." in parsed_text
        assert "\n\n" in parsed_text


def test_feishu_receive_file_replaces_placeholders_in_order():
    async def go():
        bus = MessageBus()
        channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})

        msg = InboundMessage(
            channel_name="feishu",
            chat_id="chat_1",
            user_id="user_1",
            text="before [image] middle [file] after",
            thread_ts="msg_1",
            files=[{"image_key": "img_key"}, {"file_key": "file_key"}],
        )

        channel._receive_single_file = AsyncMock(side_effect=["/mnt/user-data/uploads/a.png", "/mnt/user-data/uploads/b.pdf"])

        result = await channel.receive_file(msg, "thread_1")

        assert result.text == "before /mnt/user-data/uploads/a.png middle /mnt/user-data/uploads/b.pdf after"

    _run(go())


def test_feishu_receive_file_syncs_sandbox_with_explicit_user_id(tmp_path, monkeypatch):
    async def go():
        from deerflow.config.paths import Paths

        bus = MessageBus()
        channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
        channel._GetMessageResourceRequest = MagicMock()
        builder = MagicMock()
        builder.message_id.return_value = builder
        builder.file_key.return_value = builder
        builder.type.return_value = builder
        builder.build.return_value = object()
        channel._GetMessageResourceRequest.builder.return_value = builder

        response = MagicMock()
        response.success.return_value = True
        response.file = BytesIO(b"file-bytes")
        response.file_name = "report.md"
        channel._api_client = MagicMock()
        channel._api_client.im.v1.message_resource.get.return_value = response

        provider = MagicMock()
        provider.acquire.return_value = "aio-1"
        sandbox = MagicMock()
        provider.get.return_value = sandbox

        monkeypatch.setattr("app.channels.feishu.get_paths", lambda: Paths(base_dir=tmp_path))
        monkeypatch.setattr("app.channels.feishu.get_sandbox_provider", lambda: provider)
        monkeypatch.setattr("app.channels.feishu.get_effective_user_id", lambda: "default")

        virtual_path = await channel._receive_single_file("message-1", "file-key", "file", "thread-1", user_id="ou-user")

        assert virtual_path == "/mnt/user-data/uploads/report.md"
        assert (tmp_path / "users" / "ou-user" / "threads" / "thread-1" / "user-data" / "uploads" / "report.md").read_bytes() == b"file-bytes"
        provider.acquire.assert_called_once_with("thread-1", user_id="ou-user")
        sandbox.update_file.assert_called_once_with("/mnt/user-data/uploads/report.md", b"file-bytes")

    _run(go())


def test_feishu_on_message_extracts_image_and_file_keys():
    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})

    event = MagicMock()
    event.event.message.chat_id = "chat_1"
    event.event.message.message_id = "msg_1"
    event.event.message.root_id = None
    event.event.sender.sender_id.open_id = "user_1"

    # Rich text with one image and one file element.
    event.event.message.content = json.dumps(
        {
            "content": [
                [
                    {"tag": "text", "text": "See"},
                    {"tag": "img", "image_key": "img_123"},
                    {"tag": "file", "file_key": "file_456"},
                ]
            ]
        }
    )

    with pytest.MonkeyPatch.context() as m:
        mock_make_inbound = MagicMock()
        m.setattr(channel, "_make_inbound", mock_make_inbound)
        channel._on_message(event)

        mock_make_inbound.assert_called_once()
        files = mock_make_inbound.call_args[1]["files"]
        assert files == [{"image_key": "img_123"}, {"file_key": "file_456"}]
        assert "[image]" in mock_make_inbound.call_args[1]["text"]
        assert "[file]" in mock_make_inbound.call_args[1]["text"]
        assert channel._pending_inbound_batches == {}


@pytest.mark.asyncio
async def test_feishu_prepare_inbound_reuses_stored_parent_topic_for_card_replies():
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    store = MagicMock()
    store.get_thread_id = AsyncMock(side_effect=[None, "deer-thread-1"])
    connection_repo = MagicMock()
    connection_repo.find_connection_by_external_identity = AsyncMock(
        return_value={
            "id": "connection-a",
            "project_id": "11111111-1111-4111-8111-111111111111",
            "owner_user_id": "22222222-2222-4222-8222-222222222222",
            "membership_version": 1,
        }
    )
    channel = FeishuChannel(
        bus,
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_store": store,
            "connection_repo": connection_repo,
        },
    )
    channel._add_reaction = AsyncMock()
    channel._ensure_running_card_started = MagicMock()
    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="chat_1",
        user_id="user_1",
        text="prod",
        topic_id="om_unknown_root",
        metadata={
            "root_id": "om_unknown_root",
            "parent_id": "om_clarification_card",
            "thread_id": None,
            "topic_id": "om_unknown_root",
        },
    )

    await channel._prepare_inbound("msg_reply", inbound)

    assert inbound.topic_id == "om_clarification_card"
    assert inbound.metadata["topic_id"] == "om_clarification_card"


@pytest.mark.asyncio
async def test_feishu_guest_topic_lookup_uses_only_pseudonymous_aliases():
    store = MagicMock()
    store.get_thread_id = AsyncMock(side_effect=[None, "deer-thread-guest"])
    group_service = MagicMock()
    group_service.pseudonymize_topic_aliases.return_value = (
        "1" * 64,
        "2" * 64,
    )
    channel = FeishuChannel(
        MessageBus(),
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_store": store,
            "channel_group_binding_service": group_service,
        },
    )
    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="raw-group-id",
        user_id="raw-sender-id",
        text="answer",
        channel_instance_id="20000000-0000-4000-8000-000000000001",
        topic_id="raw-root-id",
        connection_id="guest-connection",
        private_scope=PrivateResourceScope(
            project_id="11111111-1111-4111-8111-111111111111",
            owner_user_id="22222222-2222-4222-8222-222222222222",
            membership_version=1,
        ),
        resolved_conversation_id="a" * 64,
        resolved_topic_id="1" * 64,
        metadata={
            "root_id": "raw-root-id",
            "parent_id": "raw-parent-card-id",
            "thread_id": None,
            "topic_id": "raw-root-id",
            "chat_type": "group",
        },
    )

    topic_id, found = await channel._resolve_persisted_topic_id(inbound)

    assert (topic_id, found) == ("raw-parent-card-id", True)
    assert inbound.resolved_topic_id == "2" * 64
    assert {(call.args[1], call.kwargs["topic_id"]) for call in store.get_thread_id.await_args_list} == {
        ("a" * 64, "1" * 64),
        ("a" * 64, "2" * 64),
    }
    assert "raw-group-id" not in repr(store.get_thread_id.await_args_list)
    assert "raw-root-id" not in repr(store.get_thread_id.await_args_list)
    assert "raw-parent-card-id" not in repr(store.get_thread_id.await_args_list)


@pytest.mark.asyncio
async def test_feishu_guest_outbound_persists_only_pseudonymous_aliases():
    store = MagicMock()
    store.set_thread_id = AsyncMock(return_value=True)
    aliases = {
        "raw-source-id": "1" * 64,
        "raw-card-id": "2" * 64,
        "raw-message-id": "3" * 64,
        "raw-root-id": "4" * 64,
        "raw-parent-id": "5" * 64,
        "raw-provider-thread-id": "6" * 64,
        "raw-topic-id": "7" * 64,
    }
    group_service = MagicMock()
    group_service.pseudonymize_topic_aliases.side_effect = lambda **kwargs: tuple(aliases[value] for value in kwargs["topic_ids"])
    channel = FeishuChannel(
        MessageBus(),
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_store": store,
            "channel_group_binding_service": group_service,
        },
    )
    message = OutboundMessage(
        channel_name="feishu",
        channel_instance_id="20000000-0000-4000-8000-000000000001",
        chat_id="raw-group-id",
        thread_id="deer-thread-guest",
        text="done",
        thread_ts="raw-source-id",
        connection_id="guest-connection",
        private_scope=PrivateResourceScope(
            project_id="11111111-1111-4111-8111-111111111111",
            owner_user_id="22222222-2222-4222-8222-222222222222",
            membership_version=1,
        ),
        resolved_conversation_id="a" * 64,
        resolved_topic_id="0" * 64,
        metadata={
            "message_id": "raw-message-id",
            "root_id": "raw-root-id",
            "parent_id": "raw-parent-id",
            "thread_id": "raw-provider-thread-id",
            "topic_id": "raw-topic-id",
        },
    )

    await channel._remember_thread_mapping(
        message,
        "raw-source-id",
        "raw-card-id",
    )

    assert {call.args[1] for call in store.set_thread_id.await_args_list} == {
        "a" * 64,
    }
    assert {call.kwargs["topic_id"] for call in store.set_thread_id.await_args_list} == {
        "0" * 64,
        *aliases.values(),
    }
    persisted = repr(store.set_thread_id.await_args_list)
    assert "raw-group-id" not in persisted
    for raw_alias in aliases:
        assert raw_alias not in persisted


def _make_text_event(
    text: str,
    *,
    chat_id: str = "chat_1",
    message_id: str = "msg_1",
    user_id: str = "user_1",
    root_id: str | None = None,
    parent_id: str | None = None,
    thread_id: str | None = None,
    chat_type: str = "group",
):
    event = MagicMock()
    event.event.message.chat_id = chat_id
    event.event.message.message_id = message_id
    event.event.message.root_id = root_id
    event.event.message.parent_id = parent_id
    event.event.message.thread_id = thread_id
    event.event.message.chat_type = chat_type
    event.event.sender.sender_id.open_id = user_id
    event.event.message.content = json.dumps({"text": text})
    return event


def _make_file_event(
    file_key: str,
    *,
    chat_id: str = "chat_1",
    message_id: str = "msg_1",
    user_id: str = "user_1",
    root_id: str | None = None,
    parent_id: str | None = None,
    thread_id: str | None = None,
):
    event = MagicMock()
    event.event.message.chat_id = chat_id
    event.event.message.message_id = message_id
    event.event.message.root_id = root_id
    event.event.message.parent_id = parent_id
    event.event.message.thread_id = thread_id
    event.event.sender.sender_id.open_id = user_id
    event.event.message.content = json.dumps({"file_key": file_key})
    return event


def test_feishu_batches_top_level_file_messages_from_same_user(monkeypatch):
    async def go():
        monkeypatch.setattr("app.channels.feishu.FEISHU_INBOUND_BATCH_WINDOW_SECONDS", 0.01)
        bus = MessageBus()
        channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
        channel._main_loop = asyncio.get_running_loop()
        channel._add_reaction = AsyncMock()
        channel._ensure_running_card_started = MagicMock()

        channel._on_message(_make_file_event("file_a", message_id="msg_file_1"))
        channel._on_message(_make_file_event("file_b", message_id="msg_file_2"))

        inbound = await asyncio.wait_for(bus.get_inbound(), timeout=0.5)

        assert inbound.thread_ts == "msg_file_1"
        assert inbound.topic_id == "msg_file_1"
        assert inbound.text == "[file]\n\n[file]"
        assert inbound.files == [{"file_key": "file_a"}, {"file_key": "file_b"}]
        assert inbound.metadata["message_id"] == "msg_file_1"
        assert inbound.metadata["topic_id"] == "msg_file_1"
        assert inbound.metadata["batched_message_ids"] == ["msg_file_1", "msg_file_2"]
        channel._ensure_running_card_started.assert_called_once_with("msg_file_1")
        assert [call.args for call in channel._add_reaction.call_args_list] == [
            ("msg_file_1", "OK"),
            ("msg_file_2", "OK"),
        ]

    _run(go())


def test_feishu_rich_text_file_message_does_not_enter_batch():
    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
    channel._schedule_prepare_inbound = MagicMock()

    event = MagicMock()
    event.event.message.chat_id = "chat_1"
    event.event.message.message_id = "msg_rich_file"
    event.event.message.root_id = None
    event.event.message.parent_id = None
    event.event.message.thread_id = None
    event.event.sender.sender_id.open_id = "user_1"
    event.event.message.content = json.dumps(
        {
            "content": [
                [
                    {"tag": "text", "text": "Review"},
                    {"tag": "file", "file_key": "file_a"},
                ]
            ]
        }
    )

    channel._on_message(event)

    channel._schedule_prepare_inbound.assert_called_once()
    inbound = channel._schedule_prepare_inbound.call_args.args[1]
    assert inbound.text == "Review [file]"
    assert inbound.files == [{"file_key": "file_a"}]
    assert channel._pending_inbound_batches == {}


def test_feishu_file_batch_window_expiry_starts_new_topic(monkeypatch):
    async def go():
        monkeypatch.setattr("app.channels.feishu.FEISHU_INBOUND_BATCH_WINDOW_SECONDS", 0.01)
        bus = MessageBus()
        channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
        channel._main_loop = asyncio.get_running_loop()
        channel._add_reaction = AsyncMock()
        channel._ensure_running_card_started = MagicMock()

        channel._on_message(_make_file_event("file_a", message_id="msg_file_1"))
        first = await asyncio.wait_for(bus.get_inbound(), timeout=0.5)

        channel._on_message(_make_file_event("file_b", message_id="msg_file_2"))
        second = await asyncio.wait_for(bus.get_inbound(), timeout=0.5)

        assert first.topic_id == "msg_file_1"
        assert first.files == [{"file_key": "file_a"}]
        assert second.topic_id == "msg_file_2"
        assert second.files == [{"file_key": "file_b"}]
        assert channel._ensure_running_card_started.call_args_list[0].args == ("msg_file_1",)
        assert channel._ensure_running_card_started.call_args_list[1].args == ("msg_file_2",)

    _run(go())


def test_feishu_explicit_file_reply_does_not_enter_batch(monkeypatch):
    async def go():
        monkeypatch.setattr("app.channels.feishu.FEISHU_INBOUND_BATCH_WINDOW_SECONDS", 10.0)
        bus = MessageBus()
        channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
        channel._main_loop = asyncio.get_running_loop()
        channel._add_reaction = AsyncMock()
        channel._ensure_running_card_started = MagicMock()

        channel._on_message(_make_file_event("file_a", message_id="msg_reply", root_id="msg_root"))

        inbound = await asyncio.wait_for(bus.get_inbound(), timeout=0.5)
        assert inbound.thread_ts == "msg_reply"
        assert inbound.topic_id == "msg_root"
        assert inbound.files == [{"file_key": "file_a"}]
        assert channel._pending_inbound_batches == {}

    _run(go())


def test_feishu_expired_file_batch_does_not_get_overwritten(monkeypatch):
    monkeypatch.setattr("app.channels.feishu.FEISHU_INBOUND_BATCH_WINDOW_SECONDS", 0.5)
    now = 0.0
    monkeypatch.setattr("app.channels.feishu.time.time", lambda: now)

    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
    channel._schedule_batch_flush = MagicMock()
    channel._schedule_prepare_inbound = MagicMock()

    first = InboundMessage(
        channel_name="feishu",
        chat_id="chat_1",
        user_id="user_1",
        text="[file]",
        thread_ts="msg_file_1",
        files=[{"file_key": "file_a"}],
    )
    second = InboundMessage(
        channel_name="feishu",
        chat_id="chat_1",
        user_id="user_1",
        text="[file]",
        thread_ts="msg_file_2",
        files=[{"file_key": "file_b"}],
    )

    channel._queue_file_inbound_batch("msg_file_1", first)
    now = 1.0
    channel._queue_file_inbound_batch("msg_file_2", second)

    channel._schedule_prepare_inbound.assert_called_once_with(
        "msg_file_1",
        first,
        source_message_ids=["msg_file_1"],
    )
    assert channel._pop_pending_inbound_batch(channel._pending_key("chat_1", "user_1"), anchor_message_id="msg_file_1") is None

    current = channel._pop_pending_inbound_batch(channel._pending_key("chat_1", "user_1"), anchor_message_id="msg_file_2")
    assert current == ("msg_file_2", second, ["msg_file_2"])


def test_feishu_plain_reply_consumes_pending_clarification_topic():
    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
    channel._pending_clarifications[channel._pending_key("chat_1", "user_1")] = [_pending("om_original", thread_id="deer-thread-1", card_message_id="om_card")]

    with pytest.MonkeyPatch.context() as m:
        mock_make_inbound = MagicMock()
        m.setattr(channel, "_make_inbound", mock_make_inbound)
        channel._on_message(_make_text_event("2", message_id="msg_plain_2"))

        inbound = mock_make_inbound.return_value
        metadata = mock_make_inbound.call_args.kwargs["metadata"]
        assert inbound.topic_id == "om_original"
        assert metadata["topic_id"] == "om_original"
        assert metadata[RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY] is True
        assert channel._pending_key("chat_1", "user_1") not in channel._pending_clarifications


def test_feishu_pending_clarification_is_consumed_once():
    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
    channel._pending_clarifications[channel._pending_key("chat_1", "user_1")] = [_pending("om_original", thread_id="deer-thread-1", card_message_id="om_card")]

    with pytest.MonkeyPatch.context() as m:
        created = []

        def fake_make_inbound(**kwargs):
            inbound = InboundMessage(channel_name="feishu", **kwargs)
            created.append(inbound)
            return inbound

        mock_make_inbound = MagicMock(side_effect=fake_make_inbound)
        m.setattr(channel, "_make_inbound", mock_make_inbound)
        channel._on_message(_make_text_event("2", message_id="msg_first"))
        channel._on_message(_make_text_event("next", message_id="msg_second"))

        first_inbound = created[0]
        second_inbound = created[1]
        first_metadata = mock_make_inbound.call_args_list[0].kwargs["metadata"]
        second_metadata = mock_make_inbound.call_args_list[1].kwargs["metadata"]
        assert first_inbound.topic_id == "om_original"
        assert second_inbound.topic_id == "msg_second"
        assert first_metadata["topic_id"] == "om_original"
        assert first_metadata[RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY] is True
        assert second_metadata["topic_id"] == "msg_second"
        assert second_metadata[RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY] is False


def test_feishu_expired_pending_clarification_is_ignored(monkeypatch):
    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
    monkeypatch.setattr("app.channels.feishu.time.time", lambda: 10_000.0)
    channel._pending_clarifications[channel._pending_key("chat_1", "user_1")] = [_pending("om_original", thread_id="deer-thread-1", card_message_id="om_card", created_at=0.0)]

    with pytest.MonkeyPatch.context() as m:
        mock_make_inbound = MagicMock()
        m.setattr(channel, "_make_inbound", mock_make_inbound)
        channel._on_message(_make_text_event("2", message_id="msg_plain_2"))

        metadata = mock_make_inbound.call_args.kwargs["metadata"]
        assert metadata["topic_id"] == "msg_plain_2"
        assert metadata[RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY] is False
        assert channel._pending_key("chat_1", "user_1") not in channel._pending_clarifications


def test_feishu_command_does_not_consume_pending_clarification():
    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
    key = channel._pending_key("chat_1", "user_1")
    channel._pending_clarifications[key] = [_pending("om_original", thread_id="deer-thread-1", card_message_id="om_card")]

    with pytest.MonkeyPatch.context() as m:
        mock_make_inbound = MagicMock()
        m.setattr(channel, "_make_inbound", mock_make_inbound)
        channel._on_message(_make_text_event("/status", message_id="msg_command"))

        metadata = mock_make_inbound.call_args.kwargs["metadata"]
        # Provider adapters leave tombstones as chat so only the manager owns
        # command dispatch, but must not consume a pending clarification.
        assert mock_make_inbound.call_args.kwargs["msg_type"].value == "chat"
        assert metadata["topic_id"] == "msg_command"
        assert metadata[RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY] is False
        assert key in channel._pending_clarifications


def test_feishu_remembers_pending_clarification_only_after_final_card_success():
    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
    outbound = OutboundMessage(
        channel_name="feishu",
        chat_id="chat_1",
        thread_id="deer-thread-1",
        text="clarify?",
        thread_ts="om_original",
        metadata={
            PENDING_CLARIFICATION_METADATA_KEY: True,
            "user_id": "user_1",
            "topic_id": "om_original",
            "message_id": "om_original",
        },
    )

    channel._remember_pending_clarification(outbound, None)
    assert channel._pending_clarifications == {}

    channel._remember_pending_clarification(outbound, "om_card")
    pending = channel._pending_clarifications[channel._pending_key("chat_1", "user_1")][0]
    assert pending["topic_id"] == "om_original"
    assert pending["thread_id"] == "deer-thread-1"
    assert pending["card_message_id"] == "om_card"


def test_feishu_multiple_pending_clarifications_are_consumed_in_order():
    bus = MessageBus()
    channel = FeishuChannel(bus, {"app_id": "test", "app_secret": "test"})
    key = channel._pending_key("chat_1", "user_1")
    channel._pending_clarifications[key] = [
        _pending("om_first", thread_id="deer-thread-1"),
        _pending("om_second", thread_id="deer-thread-2"),
    ]

    with pytest.MonkeyPatch.context() as m:
        created = []

        def fake_make_inbound(**kwargs):
            inbound = InboundMessage(channel_name="feishu", **kwargs)
            created.append(inbound)
            return inbound

        m.setattr(channel, "_make_inbound", MagicMock(side_effect=fake_make_inbound))
        channel._on_message(_make_text_event("first answer", message_id="msg_first"))
        channel._on_message(_make_text_event("second answer", message_id="msg_second"))

        assert [msg.topic_id for msg in created] == ["om_first", "om_second"]
        assert key not in channel._pending_clarifications


@pytest.mark.asyncio
async def test_feishu_explicit_reply_prefers_stored_mapping_over_pending():
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    store = MagicMock()
    store.get_thread_id = AsyncMock(side_effect=[None, "deer-thread-card"])
    connection_repo = MagicMock()
    connection_repo.find_connection_by_external_identity = AsyncMock(
        return_value={
            "id": "connection-a",
            "project_id": "11111111-1111-4111-8111-111111111111",
            "owner_user_id": "22222222-2222-4222-8222-222222222222",
            "membership_version": 1,
        }
    )
    channel = FeishuChannel(
        bus,
        {
            "app_id": "test",
            "app_secret": "test",
            "channel_store": store,
            "connection_repo": connection_repo,
        },
    )
    channel._add_reaction = AsyncMock()
    channel._ensure_running_card_started = MagicMock()
    key = channel._pending_key("chat_1", "user_1")
    channel._pending_clarifications[key] = [_pending("om_pending", thread_id="deer-thread-pending")]
    inbound = InboundMessage(
        channel_name="feishu",
        chat_id="chat_1",
        user_id="user_1",
        text="answer",
        topic_id="om_unknown",
        metadata={
            "root_id": "om_unknown",
            "parent_id": "om_card",
            "thread_id": None,
            "topic_id": "om_unknown",
            RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY: False,
        },
    )

    await channel._prepare_inbound("msg_reply", inbound)

    assert inbound.metadata["topic_id"] == "om_card"
    assert inbound.metadata[RESOLVED_FROM_PENDING_CLARIFICATION_METADATA_KEY] is False
    assert key in channel._pending_clarifications


@pytest.mark.parametrize("command", sorted(KNOWN_CHANNEL_COMMANDS))
def test_feishu_recognizes_all_known_slash_commands(command):
    """Every entry in KNOWN_CHANNEL_COMMANDS must be classified as a command."""
    bus = MessageBus()
    config = {"app_id": "test", "app_secret": "test"}
    channel = FeishuChannel(bus, config)

    event = MagicMock()
    event.event.message.chat_id = "chat_1"
    event.event.message.message_id = "msg_1"
    event.event.message.root_id = None
    event.event.sender.sender_id.open_id = "user_1"
    event.event.message.content = json.dumps({"text": command})

    with pytest.MonkeyPatch.context() as m:
        mock_make_inbound = MagicMock()
        m.setattr(channel, "_make_inbound", mock_make_inbound)
        channel._on_message(event)

        mock_make_inbound.assert_called_once()
        assert mock_make_inbound.call_args[1]["msg_type"].value == "command", f"{command!r} should be classified as COMMAND"


@pytest.mark.parametrize(
    "text",
    [
        "/unknown",
        "/mnt/user-data/outputs/prd/technical-design.md",
        "/etc/passwd",
        "/not-a-command at all",
    ],
)
def test_feishu_treats_unknown_slash_text_as_chat(text):
    """Slash-prefixed text that is not a known command must be classified as CHAT."""
    bus = MessageBus()
    config = {"app_id": "test", "app_secret": "test"}
    channel = FeishuChannel(bus, config)

    event = MagicMock()
    event.event.message.chat_id = "chat_1"
    event.event.message.message_id = "msg_1"
    event.event.message.root_id = None
    event.event.sender.sender_id.open_id = "user_1"
    event.event.message.content = json.dumps({"text": text})

    with pytest.MonkeyPatch.context() as m:
        mock_make_inbound = MagicMock()
        m.setattr(channel, "_make_inbound", mock_make_inbound)
        channel._on_message(event)

        mock_make_inbound.assert_called_once()
        assert mock_make_inbound.call_args[1]["msg_type"].value == "chat", f"{text!r} should be classified as CHAT"
