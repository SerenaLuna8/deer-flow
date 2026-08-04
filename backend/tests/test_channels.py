"""Tests for the IM channel system (MessageBus, ChannelStore, ChannelManager)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from concurrent.futures import Future
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from app.channels.base import Channel
from app.channels.message_bus import (
    PENDING_CLARIFICATION_METADATA_KEY,
    InboundMessage,
    InboundMessageType,
    MessageBus,
    OutboundMessage,
    ResolvedAttachment,
)
from app.channels.store import ChannelStore
from deerflow.skills.types import Skill, SkillCategory


def test_known_channel_command_detection_only_matches_control_commands():
    from app.channels.commands import is_known_channel_command, is_removed_channel_command

    assert is_known_channel_command("/models")
    assert is_known_channel_command("/HELP now")
    for command in ("/bootstrap", "/goal", "/new", "/status", "/memory"):
        assert not is_known_channel_command(command)
        assert is_removed_channel_command(command)
    assert not is_known_channel_command("/mnt/user-data/uploads/report.pdf")
    assert not is_known_channel_command("/data-analysis analyze uploads/foo.csv")
    assert not is_known_channel_command(" /new")


def _make_channel_skill(tmp_path: Path, name: str, *, enabled: bool = True) -> Skill:
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(f"# {name}\n", encoding="utf-8")
    return Skill(
        name=name,
        description=f"Description for {name}",
        license="MIT",
        skill_dir=skill_dir,
        skill_file=skill_file,
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        enabled=enabled,
    )


def _make_channel_skill_storage(skills: list[Skill]):
    return SimpleNamespace(
        load_skills=lambda *, enabled_only: [skill for skill in skills if skill.enabled] if enabled_only else skills,
        get_container_root=lambda: "/mnt/skills",
    )


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _wait_for(condition, *, timeout=5.0, interval=0.05):
    """Poll *condition* until it returns True, or raise after *timeout* seconds."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"Condition not met within {timeout}s")


# ---------------------------------------------------------------------------
# MessageBus tests
# ---------------------------------------------------------------------------


class TestMessageBus:
    def test_publish_and_get_inbound(self):
        bus = MessageBus()

        async def go():
            msg = InboundMessage(
                channel_name="test",
                chat_id="chat1",
                user_id="user1",
                text="hello",
            )
            await bus.publish_inbound(msg)
            result = await bus.get_inbound()
            assert result.text == "hello"
            assert result.channel_name == "test"
            assert result.chat_id == "chat1"

        _run(go())

    def test_inbound_queue_is_fifo(self):
        bus = MessageBus()

        async def go():
            for i in range(3):
                await bus.publish_inbound(InboundMessage(channel_name="test", chat_id="c", user_id="u", text=f"msg{i}"))
            for i in range(3):
                msg = await bus.get_inbound()
                assert msg.text == f"msg{i}"

        _run(go())

    def test_outbound_callback(self):
        bus = MessageBus()
        received = []

        async def callback(msg):
            received.append(msg)

        async def go():
            bus.subscribe_outbound(callback)
            out = OutboundMessage(channel_name="test", chat_id="c1", thread_id="t1", text="reply")
            await bus.publish_outbound(out)
            assert len(received) == 1
            assert received[0].text == "reply"

        _run(go())

    def test_unsubscribe_outbound(self):
        bus = MessageBus()
        received = []

        async def callback(msg):
            received.append(msg)

        async def go():
            bus.subscribe_outbound(callback)
            bus.unsubscribe_outbound(callback)
            out = OutboundMessage(channel_name="test", chat_id="c1", thread_id="t1", text="reply")
            await bus.publish_outbound(out)
            assert len(received) == 0

        _run(go())

    def test_unsubscribe_outbound_removes_fresh_bound_method_reference(self):
        bus = MessageBus()
        received = []

        class Handler:
            async def callback(self, msg):
                received.append((self, msg))

        handler = Handler()
        other_handler = Handler()

        async def go():
            bus.subscribe_outbound(handler.callback)
            bus.subscribe_outbound(other_handler.callback)
            bus.unsubscribe_outbound(handler.callback)
            out = OutboundMessage(channel_name="test", chat_id="c1", thread_id="t1", text="reply")
            await bus.publish_outbound(out)
            assert received == [(other_handler, out)]

        _run(go())

    def test_outbound_error_does_not_crash(self):
        bus = MessageBus()

        async def bad_callback(msg):
            raise ValueError("boom")

        received = []

        async def good_callback(msg):
            received.append(msg)

        async def go():
            bus.subscribe_outbound(bad_callback)
            bus.subscribe_outbound(good_callback)
            out = OutboundMessage(channel_name="test", chat_id="c1", thread_id="t1", text="reply")
            await bus.publish_outbound(out)
            assert len(received) == 1

        _run(go())

    def test_inbound_message_defaults(self):
        msg = InboundMessage(channel_name="test", chat_id="c", user_id="u", text="hi")
        assert msg.msg_type == InboundMessageType.CHAT
        assert msg.thread_ts is None
        assert msg.files == []
        assert msg.metadata == {}
        assert msg.created_at > 0

    def test_outbound_message_defaults(self):
        msg = OutboundMessage(channel_name="test", chat_id="c", thread_id="t", text="hi")
        assert msg.artifacts == []
        assert msg.is_final is True
        assert msg.thread_ts is None
        assert msg.metadata == {}


# ---------------------------------------------------------------------------
# Channel base class tests
# ---------------------------------------------------------------------------


class DummyChannel(Channel):
    """Concrete test implementation of Channel."""

    def __init__(self, bus, config=None):
        super().__init__(name="dummy", bus=bus, config=config or {})
        self.sent_messages: list[OutboundMessage] = []
        self._running = False

    async def start(self):
        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

    async def stop(self):
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)

    async def send(self, msg: OutboundMessage):
        self.sent_messages.append(msg)


class TestChannelBase:
    def test_make_inbound(self):
        bus = MessageBus()
        ch = DummyChannel(bus)
        msg = ch._make_inbound(
            chat_id="c1",
            user_id="u1",
            text="hello",
            msg_type=InboundMessageType.COMMAND,
        )
        assert msg.channel_name == "dummy"
        assert msg.chat_id == "c1"
        assert msg.text == "hello"
        assert msg.msg_type == InboundMessageType.COMMAND

    def test_on_outbound_routes_to_channel(self):
        bus = MessageBus()
        ch = DummyChannel(bus)

        async def go():
            await ch.start()
            msg = OutboundMessage(channel_name="dummy", chat_id="c1", thread_id="t1", text="hi")
            await bus.publish_outbound(msg)
            assert len(ch.sent_messages) == 1

        _run(go())

    def test_on_outbound_ignores_other_channels(self):
        bus = MessageBus()
        ch = DummyChannel(bus)

        async def go():
            await ch.start()
            msg = OutboundMessage(channel_name="other", chat_id="c1", thread_id="t1", text="hi")
            await bus.publish_outbound(msg)
            assert len(ch.sent_messages) == 0

        _run(go())

    def test_send_with_retry_retries_until_success(self, monkeypatch):
        bus = MessageBus()
        ch = DummyChannel(bus)
        attempts = 0
        sleep = AsyncMock()
        monkeypatch.setattr("app.channels.base.asyncio.sleep", sleep)

        async def flaky_send():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError(f"failure {attempts}")
            return "sent"

        result = _run(ch._send_with_retry(flaky_send, max_retries=3, log_prefix="[Dummy]"))

        assert result == "sent"
        assert attempts == 3
        assert [call.args[0] for call in sleep.await_args_list] == [1, 2]

    def test_log_future_error_handles_cancelled_future(self, caplog):
        bus = MessageBus()
        ch = DummyChannel(bus)
        fut = Future()
        fut.cancel()

        with caplog.at_level(logging.ERROR):
            ch._log_future_error(fut, "prepare_inbound", "m1")

        assert "prepare_inbound" not in caplog.text

    def test_log_future_error_surfaces_future_exception(self, caplog):
        bus = MessageBus()
        ch = DummyChannel(bus)
        fut = Future()
        fut.set_exception(RuntimeError("boom"))

        with caplog.at_level(logging.ERROR):
            ch._log_future_error(fut, "prepare_inbound", "raw-provider-message-id")

        assert "prepare_inbound failed: boom" in caplog.text
        assert "raw-provider-message-id" not in caplog.text

    def test_channel_capabilities_match_channel_defaults(self):
        from app.channels.dingtalk import DingTalkChannel
        from app.channels.discord import DiscordChannel
        from app.channels.feishu import FeishuChannel
        from app.channels.github import GitHubChannel
        from app.channels.manager import CHANNEL_CAPABILITIES
        from app.channels.slack import SlackChannel
        from app.channels.telegram import TelegramChannel
        from app.channels.wechat import WechatChannel
        from app.channels.wecom import WeComChannel

        bus = MessageBus()
        defaults = {
            "dingtalk": DingTalkChannel(bus=bus, config={}).supports_streaming,
            "discord": DiscordChannel(bus=bus, config={}).supports_streaming,
            "feishu": FeishuChannel(bus=bus, config={}).supports_streaming,
            "github": GitHubChannel(bus=bus, config={}).supports_streaming,
            "slack": SlackChannel(bus=bus, config={}).supports_streaming,
            "telegram": TelegramChannel(bus=bus, config={}).supports_streaming,
            "wechat": WechatChannel(bus=bus, config={}).supports_streaming,
            "wecom": WeComChannel(bus=bus, config={}).supports_streaming,
        }

        assert {name: caps["supports_streaming"] for name, caps in CHANNEL_CAPABILITIES.items()} == defaults


# ---------------------------------------------------------------------------
# _extract_response_text tests
# ---------------------------------------------------------------------------


class TestExtractResponseText:
    def test_string_content(self):
        from app.channels.manager import _extract_response_text

        result = {"messages": [{"type": "ai", "content": "hello"}]}
        assert _extract_response_text(result) == "hello"

    def test_list_content_blocks(self):
        from app.channels.manager import _extract_response_text

        result = {"messages": [{"type": "ai", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": " world"}]}]}
        assert _extract_response_text(result) == "hello world"

    def test_picks_last_ai_message(self):
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "ai", "content": "first"},
                {"type": "human", "content": "question"},
                {"type": "ai", "content": "second"},
            ]
        }
        assert _extract_response_text(result) == "second"

    def test_empty_messages(self):
        from app.channels.manager import _extract_response_text

        assert _extract_response_text({"messages": []}) == ""

    def test_no_ai_messages(self):
        from app.channels.manager import _extract_response_text

        result = {"messages": [{"type": "human", "content": "hi"}]}
        assert _extract_response_text(result) == ""

    def test_list_result(self):
        from app.channels.manager import _extract_response_text

        result = [{"type": "ai", "content": "from list"}]
        assert _extract_response_text(result) == "from list"

    def test_skips_empty_ai_content(self):
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "ai", "content": ""},
                {"type": "ai", "content": "actual response"},
            ]
        }
        assert _extract_response_text(result) == "actual response"

    def test_clarification_tool_message(self):
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "human", "content": "健身"},
                {"type": "ai", "content": "", "tool_calls": [{"name": "ask_clarification", "args": {"question": "您想了解哪方面？"}}]},
                {"type": "tool", "name": "ask_clarification", "content": "您想了解哪方面？"},
            ]
        }
        assert _extract_response_text(result) == "您想了解哪方面？"

    def test_clarification_over_empty_ai(self):
        """When AI content is empty but ask_clarification tool message exists, use the tool message."""
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "ai", "content": ""},
                {"type": "tool", "name": "ask_clarification", "content": "Could you clarify?"},
            ]
        }
        assert _extract_response_text(result) == "Could you clarify?"

    def test_does_not_leak_previous_turn_text(self):
        """When current turn AI has no text (only tool calls), do not return previous turn's text."""
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "human", "content": "hello"},
                {"type": "ai", "content": "Hi there!"},
                {"type": "human", "content": "export data"},
                {
                    "type": "ai",
                    "content": "",
                    "tool_calls": [{"name": "present_files", "args": {"filepaths": ["/mnt/user-data/outputs/data.csv"]}}],
                },
                {"type": "tool", "name": "present_files", "content": "ok"},
            ]
        }
        # Should return "" (no text in current turn), NOT "Hi there!" from previous turn
        assert _extract_response_text(result) == ""

    def test_ignores_hidden_human_control_messages(self):
        """Hidden control messages should not terminate current-turn response extraction."""
        from app.channels.manager import _extract_response_text

        result = {
            "messages": [
                {"type": "human", "content": "plan this"},
                {"type": "ai", "content": "Here is the plan."},
                {
                    "type": "human",
                    "name": "todo_reminder",
                    "content": "keep todos updated",
                    "additional_kwargs": {"hide_from_ui": True},
                },
            ]
        }

        assert _extract_response_text(result) == "Here is the plan."


class TestClarificationDetection:
    def test_final_clarification_tool_message_is_pending(self):
        from app.channels.manager import _has_current_turn_clarification

        result = {
            "messages": [
                {"type": "human", "content": "deploy"},
                {"type": "ai", "content": "", "tool_calls": [{"name": "ask_clarification", "args": {}}]},
                {"type": "tool", "name": "ask_clarification", "content": "Which environment?"},
            ]
        }
        assert _has_current_turn_clarification(result) is True

    def test_clarification_followed_by_regular_ai_is_not_pending(self):
        from app.channels.manager import _has_current_turn_clarification

        result = {
            "messages": [
                {"type": "human", "content": "deploy"},
                {"type": "ai", "content": "", "tool_calls": [{"name": "ask_clarification", "args": {}}]},
                {"type": "tool", "name": "ask_clarification", "content": "Which environment?"},
                {"type": "ai", "content": "I will continue without pending clarification."},
            ]
        }
        assert _has_current_turn_clarification(result) is False

    def test_previous_turn_clarification_does_not_mark_current_turn(self):
        from app.channels.manager import _has_current_turn_clarification

        result = {
            "messages": [
                {"type": "human", "content": "deploy"},
                {"type": "ai", "content": "", "tool_calls": [{"name": "ask_clarification", "args": {}}]},
                {"type": "tool", "name": "ask_clarification", "content": "Which environment?"},
                {"type": "human", "content": "prod"},
                {"type": "ai", "content": "Deploying to prod."},
            ]
        }
        assert _has_current_turn_clarification(result) is False


# ---------------------------------------------------------------------------
# ChannelManager tests
# ---------------------------------------------------------------------------


def _make_mock_langgraph_client(thread_id="test-thread-123", run_result=None):
    """Create a mock langgraph_sdk async client."""
    mock_client = MagicMock()

    # threads.create() returns a Thread-like dict
    mock_client.threads.create = AsyncMock(return_value={"thread_id": thread_id})
    mock_client.threads.update = AsyncMock(return_value={"thread_id": thread_id})

    # threads.get() returns thread info (succeeds by default)
    mock_client.threads.get = AsyncMock(return_value={"thread_id": thread_id})

    # runs.wait() returns the final state with messages
    if run_result is None:
        run_result = {
            "messages": [
                {"type": "human", "content": "hi"},
                {"type": "ai", "content": "Hello from agent!"},
            ]
        }
    mock_client.runs.wait = AsyncMock(return_value=run_result)

    return mock_client


@asynccontextmanager
async def _channel_connection_repo(database_url: str):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from deerflow.persistence.channel_connections import ChannelConnectionRepository, ChannelCredentialCipher

    engine = create_async_engine(database_url, poolclass=NullPool)
    repo = ChannelConnectionRepository(
        async_sessionmaker(engine, expire_on_commit=False),
        cipher=ChannelCredentialCipher.from_key("test-channel-key"),
    )
    try:
        yield repo
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_channel_connection_repo_disposes_owned_engine_on_failure(monkeypatch):
    engine = MagicMock()
    engine.dispose = AsyncMock()
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", MagicMock(return_value=engine))

    with pytest.raises(RuntimeError, match="body failed"):
        async with _channel_connection_repo("postgresql://unused"):
            raise RuntimeError("body failed")

    engine.dispose.assert_awaited_once()


def _make_stream_part(event: str, data):
    return SimpleNamespace(event=event, data=data)


def _make_async_iterator(items):
    async def iterator():
        for item in items:
            yield item

    return iterator()


class TestChannelManager:
    def test_get_client_includes_csrf_header_and_cookie(self):
        from app.channels.manager import ChannelManager

        bus = MessageBus()
        store = None
        manager = ChannelManager(bus=bus, store=store, langgraph_url="http://localhost:8001")

        with patch("langgraph_sdk.get_client") as get_client:
            get_client.return_value = object()

            manager._get_client()

        get_client.assert_called_once()
        kwargs = get_client.call_args.kwargs
        assert kwargs["url"] == "http://localhost:8001"
        headers = kwargs["headers"]
        csrf_token = headers["X-CSRF-Token"]
        assert csrf_token
        assert headers["Cookie"] == f"csrf_token={csrf_token}"
        assert headers["X-DeerFlow-Internal-Token"]

    def test_fetch_gateway_includes_internal_auth_headers(self, monkeypatch):
        from app.channels.manager import ChannelManager

        class MockResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"models": [{"name": "default"}]}

        class MockAsyncClient:
            def __init__(self, *args, **kwargs):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, **kwargs):
                calls.append({"url": url, **kwargs})
                return MockResponse()

        calls = []
        monkeypatch.setattr("app.channels.manager.httpx.AsyncClient", MockAsyncClient)

        async def go():
            bus = MessageBus()
            store = None
            manager = ChannelManager(bus=bus, store=store, gateway_url="http://gateway:8001")

            reply = await manager._fetch_gateway("/api/models", "models")

            assert reply == "Available models:\n• default"
            assert calls[0]["url"] == "http://gateway:8001/api/models"
            assert calls[0]["timeout"] == 10
            assert calls[0]["headers"]["X-DeerFlow-Internal-Token"]

        _run(go())

    def test_ingest_inbound_files_uses_explicit_owner_bucket(self, tmp_path, monkeypatch):
        from app.channels.manager import INBOUND_FILE_READERS, _ingest_inbound_files
        from deerflow.config.paths import Paths

        paths = Paths(tmp_path)
        monkeypatch.setattr("deerflow.uploads.manager.get_paths", lambda: paths)

        async def read_file(file_info, client):
            del file_info, client
            return b"owner data"

        INBOUND_FILE_READERS["owner-test"] = read_file

        async def go():
            try:
                created = await _ingest_inbound_files(
                    "thread-owner",
                    InboundMessage(
                        channel_name="owner-test",
                        chat_id="C123",
                        user_id="U-platform",
                        text="file",
                        files=[{"filename": "report.txt", "type": "file"}],
                    ),
                    user_id="owner-1",
                )
            finally:
                INBOUND_FILE_READERS.pop("owner-test", None)

            assert created == [
                {
                    "filename": "report.txt",
                    "size": len(b"owner data"),
                    "path": "/mnt/user-data/uploads/report.txt",
                    "is_image": False,
                }
            ]
            assert (paths.sandbox_uploads_dir("thread-owner", user_id="owner-1") / "report.txt").read_bytes() == b"owner data"
            assert not paths.sandbox_uploads_dir("thread-owner").exists()

        _run(go())

    def test_channel_storage_user_id_requires_server_resolved_owner(self):
        """External platform users never become project filesystem authority."""
        from app.channels.manager import _channel_storage_user_id, _safe_user_id_for_run

        unbound = InboundMessage(channel_name="slack", chat_id="C1", user_id="U-platform", text="hi")
        assert _channel_storage_user_id(unbound) is None

        bound = InboundMessage(channel_name="slack", chat_id="C1", user_id="U-platform", text="hi", owner_user_id="owner-1")
        assert _channel_storage_user_id(bound) == _safe_user_id_for_run("owner-1")

        anonymous = InboundMessage(channel_name="slack", chat_id="C1", user_id="", text="hi")
        assert _channel_storage_user_id(anonymous) is None

    def test_inbound_dedupe_key_fails_closed_without_workspace(self):
        """Without a workspace identifier, skip dedupe instead of collapsing workspaces (willem #3)."""
        from app.channels.manager import ChannelManager

        with_workspace = InboundMessage(
            channel_name="slack",
            chat_id="C1",
            user_id="U1",
            text="x",
            metadata={"team_id": "T1", "message_id": "m1"},
        )
        assert ChannelManager._inbound_dedupe_key(with_workspace) == (
            "slack",
            "slack",
            "T1",
            "C1",
            "m1",
        )

        without_workspace = InboundMessage(
            channel_name="slack",
            chat_id="C1",
            user_id="U1",
            text="x",
            metadata={"message_id": "m1"},
        )
        assert ChannelManager._inbound_dedupe_key(without_workspace) is None

    def test_dispatch_loop_dedupes_stable_provider_message_id(self, tmp_path):
        from app.channels.manager import ChannelManager

        async def go():
            bus = MessageBus()
            manager = ChannelManager(bus=bus, store=None)
            manager._handle_project_inbound_chat = AsyncMock()
            await manager.start()
            try:

                def inbound(message_id: str) -> InboundMessage:
                    return InboundMessage(
                        channel_name="slack",
                        chat_id="C123",
                        user_id="U123",
                        workspace_id="T123",
                        text="project prompt",
                        metadata={"message_id": message_id},
                    )

                await bus.publish_inbound(inbound("m-1"))
                await bus.publish_inbound(inbound("m-1"))
                await _wait_for(lambda: manager._handle_project_inbound_chat.await_count == 1)
                await asyncio.sleep(0.05)
                assert manager._handle_project_inbound_chat.await_count == 1

                await bus.publish_inbound(inbound("m-2"))
                await _wait_for(lambda: manager._handle_project_inbound_chat.await_count == 2)
            finally:
                await manager.stop()

        _run(go())

    def test_dispatch_loop_releases_dedupe_key_when_project_dispatch_fails(self, tmp_path):
        from app.channels.manager import ChannelManager

        async def go():
            bus = MessageBus()
            manager = ChannelManager(bus=bus, store=None)
            manager._handle_project_inbound_chat = AsyncMock(side_effect=[RuntimeError("temporary"), None])
            manager._send_error = AsyncMock()
            inbound = InboundMessage(
                channel_name="slack",
                chat_id="C123",
                user_id="U123",
                workspace_id="T123",
                text="project prompt",
                metadata={"message_id": "m-1"},
            )

            await manager.start()
            try:
                await bus.publish_inbound(inbound)
                await _wait_for(lambda: manager._handle_project_inbound_chat.await_count == 1)
                await bus.publish_inbound(inbound)
                await _wait_for(lambda: manager._handle_project_inbound_chat.await_count == 2)
            finally:
                await manager.stop()

        _run(go())

    def test_channel_help_lists_only_final_commands(self):
        from app.channels.manager import ChannelManager

        async def go():
            bus = MessageBus()
            manager = ChannelManager(bus=bus, store=Mock(spec=ChannelStore))
            manager._get_bound_identity_rejection = AsyncMock(return_value=None)
            manager._lookup_thread_id = AsyncMock(return_value=None)
            outbound_received: list[OutboundMessage] = []

            async def capture_outbound(message: OutboundMessage) -> None:
                outbound_received.append(message)

            bus.subscribe_outbound(capture_outbound)

            await manager._handle_command(
                InboundMessage(
                    channel_name="slack",
                    chat_id="C123",
                    user_id="U123",
                    text="/help",
                    msg_type=InboundMessageType.COMMAND,
                )
            )

            reply = outbound_received[0].text
            assert "/models" in reply
            assert "/help" in reply
            assert "/<skill-name>" in reply
            for removed in ("/bootstrap", "/goal", "/new", "/status", "/memory"):
                assert removed not in reply

        _run(go())

    @pytest.mark.parametrize("command", ["/bootstrap", "/goal", "/new", "/status", "/memory"])
    def test_removed_channel_command_returns_stable_unknown_response(self, command):
        from app.channels.manager import ChannelManager

        async def go():
            bus = MessageBus()
            manager = ChannelManager(bus=bus, store=Mock(spec=ChannelStore))
            manager._get_bound_identity_rejection = AsyncMock(side_effect=AssertionError("removed command checked binding"))
            manager._lookup_thread_id = AsyncMock(return_value=None)
            manager._handle_chat = AsyncMock()
            manager._fetch_gateway = AsyncMock()
            outbound_received: list[OutboundMessage] = []

            async def capture_outbound(message: OutboundMessage) -> None:
                outbound_received.append(message)

            bus.subscribe_outbound(capture_outbound)

            await manager._handle_command(
                InboundMessage(
                    channel_name="slack",
                    chat_id="C123",
                    user_id="U123",
                    text=f"{command} ignored arguments",
                    msg_type=InboundMessageType.COMMAND,
                )
            )

            assert outbound_received[0].text == (f"Unknown command: {command}. Available commands: /help | /models")
            manager._handle_chat.assert_not_awaited()
            manager._fetch_gateway.assert_not_awaited()
            manager._get_bound_identity_rejection.assert_not_awaited()

        _run(go())

    def test_handle_command_slash_skill_reports_disabled_skill(self, tmp_path):
        from app.channels.manager import ChannelManager
        from app.private_work.connection_inbound import (
            ProjectInboundDispatcher,
            ResolvedInboundPrivateWork,
        )
        from app.private_work.context import PrivateWorkContext
        from app.private_work.run_admission import PrivateRunInboundAuthority
        from app.projects.capabilities import capabilities_for
        from app.projects.context import ProjectContext
        from app.projects.models import ProjectRole

        async def go():
            bus = MessageBus()
            store = None
            owner_id = uuid.uuid4()
            context = PrivateWorkContext.from_project(
                ProjectContext(
                    user_id=owner_id,
                    project_id=uuid.uuid4(),
                    membership_id=uuid.uuid4(),
                    role=ProjectRole.RUNNER,
                    capabilities=capabilities_for(ProjectRole.RUNNER),
                    membership_version=1,
                    request_id="slash-project-authority",
                )
            )
            resolver = SimpleNamespace(
                resolve=AsyncMock(
                    return_value=ResolvedInboundPrivateWork(
                        account_id=owner_id,
                        context=context,
                        connection_id="connection-a",
                        thread_id="thread-a",
                        created=False,
                        authority=PrivateRunInboundAuthority(
                            connection_id="connection-a",
                            provider="test",
                            external_account_id="user1",
                            workspace_id=None,
                            external_conversation_id="chat1",
                            external_topic_id=None,
                        ),
                    )
                )
            )
            launcher = AsyncMock(return_value={"messages": [{"type": "ai", "content": "project answer"}]})
            manager = ChannelManager(
                bus=bus,
                store=store,
                private_inbound_dispatcher=ProjectInboundDispatcher(
                    resolver,
                    launcher,
                ),
            )
            manager._skill_storage = _make_channel_skill_storage([_make_channel_skill(tmp_path, "data-analysis", enabled=False)])

            mock_client = _make_mock_langgraph_client()
            manager._client = mock_client

            outbound_received = []

            async def capture_outbound(msg):
                outbound_received.append(msg)

            bus.subscribe_outbound(capture_outbound)
            await manager.start()

            inbound = InboundMessage(
                channel_name="test",
                chat_id="chat1",
                user_id="user1",
                text="/data-analysis analyze uploads/foo.csv",
                msg_type=InboundMessageType.COMMAND,
            )
            await bus.publish_inbound(inbound)
            await _wait_for(lambda: len(outbound_received) >= 1)
            await manager.stop()

            mock_client.runs.wait.assert_not_called()
            launcher.assert_awaited_once()
            assert launcher.await_args.args[2].text.startswith("/data-analysis")
            assert outbound_received[0].text == "project answer"

        _run(go())

    def test_handle_command_uninstalled_slash_skill_stays_unknown_command(self, tmp_path):
        from app.channels.manager import ChannelManager
        from app.private_work.connection_inbound import (
            ProjectInboundDispatcher,
            ResolvedInboundPrivateWork,
        )
        from app.private_work.context import PrivateWorkContext
        from app.private_work.run_admission import PrivateRunInboundAuthority
        from app.projects.capabilities import capabilities_for
        from app.projects.context import ProjectContext
        from app.projects.models import ProjectRole

        async def go():
            bus = MessageBus()
            store = None
            owner_id = uuid.uuid4()
            context = PrivateWorkContext.from_project(
                ProjectContext(
                    user_id=owner_id,
                    project_id=uuid.uuid4(),
                    membership_id=uuid.uuid4(),
                    role=ProjectRole.RUNNER,
                    capabilities=capabilities_for(ProjectRole.RUNNER),
                    membership_version=1,
                    request_id="slash-project-authority",
                )
            )
            resolver = SimpleNamespace(
                resolve=AsyncMock(
                    return_value=ResolvedInboundPrivateWork(
                        account_id=owner_id,
                        context=context,
                        connection_id="connection-a",
                        thread_id="thread-a",
                        created=False,
                        authority=PrivateRunInboundAuthority(
                            connection_id="connection-a",
                            provider="test",
                            external_account_id="user1",
                            workspace_id=None,
                            external_conversation_id="chat1",
                            external_topic_id=None,
                        ),
                    )
                )
            )
            launcher = AsyncMock(return_value={"messages": [{"type": "ai", "content": "project answer"}]})
            manager = ChannelManager(
                bus=bus,
                store=store,
                private_inbound_dispatcher=ProjectInboundDispatcher(
                    resolver,
                    launcher,
                ),
            )
            manager._skill_storage = _make_channel_skill_storage([_make_channel_skill(tmp_path, "frontend-design")])

            mock_client = _make_mock_langgraph_client()
            manager._client = mock_client

            outbound_received = []

            async def capture_outbound(msg):
                outbound_received.append(msg)

            bus.subscribe_outbound(capture_outbound)
            await manager.start()

            inbound = InboundMessage(
                channel_name="test",
                chat_id="chat1",
                user_id="user1",
                text="/data-analysis analyze uploads/foo.csv",
                msg_type=InboundMessageType.COMMAND,
            )
            await bus.publish_inbound(inbound)
            await _wait_for(lambda: len(outbound_received) >= 1)
            await manager.stop()

            mock_client.runs.wait.assert_not_called()
            launcher.assert_awaited_once()
            assert launcher.await_args.args[2].text.startswith("/data-analysis")
            assert outbound_received[0].text == "project answer"

        _run(go())


class _BoundIdentityRepo:
    def __init__(self, connections: list[dict[str, str | None]] | None = None) -> None:
        self.connections = list(connections or [])
        self.lookups: list[dict[str, str | None]] = []
        self.thread_sets: list[dict[str, str | None]] = []

    async def find_connection_by_external_identity(
        self,
        *,
        provider: str,
        channel_instance_id: str | None = None,
        external_account_id: str,
        workspace_id: str | None = None,
        expected_connection_id: str | None = None,
        expected_scope: object | None = None,
    ):
        del channel_instance_id
        self.lookups.append(
            {
                "provider": provider,
                "external_account_id": external_account_id,
                "workspace_id": workspace_id,
            }
        )
        for connection in self.connections:
            if connection.get("provider") == provider and connection.get("external_account_id") == external_account_id and connection.get("workspace_id") == workspace_id:
                if expected_connection_id is not None and connection.get("id") != expected_connection_id:
                    continue
                if expected_scope is not None and (connection.get("project_id") != expected_scope.project_id or connection.get("owner_user_id") != expected_scope.owner_user_id):
                    continue
                return connection
        return None

    async def get_thread_id(self, connection_id: str, chat_id: str, topic_id: str | None = None):
        return None

    async def set_thread_id(
        self,
        *,
        connection_id: str,
        owner_user_id: str,
        provider: str,
        external_conversation_id: str,
        external_topic_id: str | None,
        thread_id: str,
    ) -> None:
        self.thread_sets.append(
            {
                "connection_id": connection_id,
                "owner_user_id": owner_user_id,
                "provider": provider,
                "external_conversation_id": external_conversation_id,
                "external_topic_id": external_topic_id,
                "thread_id": thread_id,
            }
        )


class TestChannelManagerBoundIdentityPolicy:
    def test_project_dispatcher_uses_resolved_scope_and_skips_legacy_sdk(self, monkeypatch):
        from app.channels.manager import ChannelManager
        from app.private_work.connection_inbound import (
            ProjectInboundDispatcher,
            ResolvedInboundPrivateWork,
        )
        from app.private_work.context import PrivateWorkContext
        from app.private_work.run_admission import PrivateRunInboundAuthority
        from app.projects.capabilities import capabilities_for
        from app.projects.context import ProjectContext
        from app.projects.models import ProjectRole

        monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)

        async def go():
            owner_id = uuid.uuid4()
            project_id = uuid.uuid4()
            channel_instance_id = str(uuid.uuid4())
            context = PrivateWorkContext.from_project(
                ProjectContext(
                    user_id=owner_id,
                    project_id=project_id,
                    membership_id=uuid.uuid4(),
                    role=ProjectRole.RUNNER,
                    capabilities=capabilities_for(ProjectRole.RUNNER),
                    membership_version=3,
                    request_id="req-channel-project",
                )
            )
            resolved = ResolvedInboundPrivateWork(
                account_id=owner_id,
                context=context,
                connection_id="server-connection",
                thread_id="project-thread",
                created=False,
                authority=PrivateRunInboundAuthority(
                    connection_id="server-connection",
                    channel_instance_id=channel_instance_id,
                    provider="slack",
                    external_account_id="U-platform",
                    workspace_id="T123",
                    external_conversation_id="C123",
                    external_topic_id="1710000000.000100",
                ),
            )
            resolver = SimpleNamespace(resolve=AsyncMock(return_value=resolved))
            launcher = AsyncMock(
                return_value={
                    "messages": [
                        {"type": "human", "content": "question"},
                        {
                            "type": "ai",
                            "content": "",
                            "tool_calls": [{"name": "ask_clarification", "args": {}}],
                        },
                        {
                            "type": "tool",
                            "name": "ask_clarification",
                            "content": "Which environment?",
                        },
                    ]
                }
            )
            dispatcher = ProjectInboundDispatcher(resolver, launcher)
            bus = MessageBus()
            store = None
            manager = ChannelManager(
                bus=bus,
                store=store,
                private_inbound_dispatcher=dispatcher,
            )
            mock_client = _make_mock_langgraph_client()
            manager._client = mock_client
            outbound_received = []

            async def capture(msg):
                outbound_received.append(msg)

            bus.subscribe_outbound(capture)
            await manager.start()
            try:
                inbound = InboundMessage(
                    channel_name="slack",
                    channel_instance_id=channel_instance_id,
                    chat_id="C123",
                    user_id="U-platform",
                    text="question",
                    workspace_id="T123",
                    topic_id="1710000000.000100",
                    resolved_conversation_id="a" * 64,
                    resolved_topic_id="b" * 64,
                    owner_user_id="forged-owner",
                    project_id=str(uuid.uuid4()),
                    connection_id="forged-connection",
                    metadata={"message_id": "m-1", "sender_staff_id": "staff-1"},
                )
                await manager._handle_message(inbound)
            finally:
                await manager.stop()

            launcher.assert_awaited_once_with(
                context,
                "project-thread",
                inbound,
                resolved.authority,
            )
            mock_client.threads.create.assert_not_called()
            mock_client.runs.wait.assert_not_called()
            mock_client.runs.create.assert_not_called()
            assert len(outbound_received) == 1
            assert outbound_received[0].text == "Which environment?"
            assert outbound_received[0].thread_id == "project-thread"
            assert outbound_received[0].connection_id == "server-connection"
            assert outbound_received[0].channel_instance_id == channel_instance_id
            assert outbound_received[0].owner_user_id == str(owner_id)
            assert outbound_received[0].private_scope == context.resource_scope
            assert outbound_received[0].resolved_conversation_id == "a" * 64
            assert outbound_received[0].resolved_topic_id == "b" * 64
            assert outbound_received[0].metadata["project_id"] == str(project_id)
            assert outbound_received[0].metadata["message_id"] == "m-1"
            assert outbound_received[0].metadata["sender_staff_id"] == "staff-1"
            assert outbound_received[0].metadata[PENDING_CLARIFICATION_METADATA_KEY] is True

        _run(go())

    def test_project_dispatcher_not_found_uses_unbound_message(self, monkeypatch):
        from app.channels.manager import BOUND_IDENTITY_REQUIRED_MESSAGE, ChannelManager
        from app.private_work.connection_inbound import ProjectInboundDispatcher
        from app.private_work.errors import PrivateWorkNotFound

        monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)

        async def go():
            resolver = SimpleNamespace(resolve=AsyncMock(side_effect=PrivateWorkNotFound("req-unbound")))
            launcher = AsyncMock()
            bus = MessageBus()
            manager = ChannelManager(
                bus=bus,
                store=None,
                private_inbound_dispatcher=ProjectInboundDispatcher(resolver, launcher),
            )
            manager._client = _make_mock_langgraph_client()
            outbound_received = []

            async def capture(msg):
                outbound_received.append(msg)

            bus.subscribe_outbound(capture)
            await manager.start()
            try:
                await manager._handle_message(
                    InboundMessage(
                        channel_name="slack",
                        chat_id="C123",
                        user_id="U-platform",
                        text="question",
                    )
                )
            finally:
                await manager.stop()

            launcher.assert_not_awaited()
            assert len(outbound_received) == 1
            assert outbound_received[0].text == BOUND_IDENTITY_REQUIRED_MESSAGE

        _run(go())

    def test_unbound_auth_enabled_chat_is_rejected_before_thread_or_run_creation(self, monkeypatch):
        from app.channels.manager import BOUND_IDENTITY_UNAVAILABLE_MESSAGE, ChannelManager

        monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)

        async def go():
            bus = MessageBus()
            store = None
            manager = ChannelManager(bus=bus, store=store)
            mock_client = _make_mock_langgraph_client()
            manager._client = mock_client
            outbound_received = []

            async def capture(msg):
                outbound_received.append(msg)

            bus.subscribe_outbound(capture)
            await manager._handle_chat(
                InboundMessage(
                    channel_name="slack",
                    chat_id="C123",
                    user_id="U-platform",
                    text="hi",
                    thread_ts="1710000000.000100",
                )
            )

            assert len(outbound_received) == 1
            assert outbound_received[0].text == BOUND_IDENTITY_UNAVAILABLE_MESSAGE
            assert outbound_received[0].thread_id == ""
            assert outbound_received[0].connection_id is None
            assert outbound_received[0].owner_user_id is None
            mock_client.threads.create.assert_not_called()
            mock_client.runs.wait.assert_not_called()

        _run(go())

    def test_bound_identity_repo_unavailable_uses_transient_failure_message(self, monkeypatch):
        from app.channels.manager import BOUND_IDENTITY_UNAVAILABLE_MESSAGE, ChannelManager

        monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)

        async def go():
            bus = MessageBus()
            store = None
            manager = ChannelManager(bus=bus, store=store)
            mock_client = _make_mock_langgraph_client()
            manager._client = mock_client
            outbound_received = []

            async def capture(msg):
                outbound_received.append(msg)

            bus.subscribe_outbound(capture)
            await manager._handle_chat(
                InboundMessage(
                    channel_name="slack",
                    chat_id="C123",
                    user_id="U-platform",
                    owner_user_id="deerflow-user-1",
                    connection_id="connection-1",
                    workspace_id="T123",
                    text="hi",
                )
            )

            assert len(outbound_received) == 1
            assert outbound_received[0].text == BOUND_IDENTITY_UNAVAILABLE_MESSAGE
            assert outbound_received[0].connection_id is None
            assert outbound_received[0].owner_user_id is None
            mock_client.threads.create.assert_not_called()
            mock_client.runs.wait.assert_not_called()

        _run(go())

    def test_unbound_auth_enabled_chat_is_rejected_before_semaphore(self, monkeypatch):
        from app.channels.manager import BOUND_IDENTITY_UNAVAILABLE_MESSAGE, ChannelManager

        monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)

        async def go():
            bus = MessageBus()
            store = None
            manager = ChannelManager(bus=bus, store=store)
            outbound_received = []

            async def capture(msg):
                outbound_received.append(msg)

            bus.subscribe_outbound(capture)
            await manager.start()
            assert manager._semaphore is not None
            await manager._semaphore.acquire()
            try:
                await asyncio.wait_for(
                    manager._handle_message(
                        InboundMessage(
                            channel_name="slack",
                            chat_id="C123",
                            user_id="U-platform",
                            text="hi",
                        )
                    ),
                    timeout=0.5,
                )
            finally:
                manager._semaphore.release()
                await manager.stop()

            assert len(outbound_received) == 1
            assert outbound_received[0].text == BOUND_IDENTITY_UNAVAILABLE_MESSAGE
            assert outbound_received[0].connection_id is None
            assert outbound_received[0].owner_user_id is None

        _run(go())

    def test_bound_auth_enabled_chat_is_allowed_when_bound_identity_is_required(self, monkeypatch):
        from app.channels.manager import ChannelManager

        monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)

        async def go():
            bus = MessageBus()
            store = None
            repo = _BoundIdentityRepo(
                [
                    {
                        "id": "connection-1",
                        "owner_user_id": "deerflow-user-1",
                        "provider": "slack",
                        "external_account_id": "U-platform",
                        "workspace_id": "T123",
                    }
                ]
            )
            manager = ChannelManager(bus=bus, store=store, connection_repo=repo)
            mock_client = _make_mock_langgraph_client(thread_id="thread-bound")
            manager._client = mock_client

            await manager._handle_chat(
                InboundMessage(
                    channel_name="slack",
                    chat_id="C123",
                    user_id="U-platform",
                    owner_user_id="deerflow-user-1",
                    connection_id="connection-1",
                    workspace_id="T123",
                    text="hi",
                )
            )

            mock_client.threads.create.assert_not_called()
            mock_client.runs.wait.assert_not_called()

        _run(go())

    def test_bound_auth_enabled_message_checks_bound_identity_once_on_hot_path(self, monkeypatch):
        from app.channels.manager import ChannelManager

        monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)

        async def go():
            bus = MessageBus()
            store = None
            repo = _BoundIdentityRepo(
                [
                    {
                        "id": "connection-1",
                        "owner_user_id": "deerflow-user-1",
                        "provider": "slack",
                        "external_account_id": "U-platform",
                        "workspace_id": "T123",
                    }
                ]
            )
            manager = ChannelManager(bus=bus, store=store, connection_repo=repo)
            mock_client = _make_mock_langgraph_client(thread_id="thread-bound")
            manager._client = mock_client
            await manager.start()
            try:
                await manager._handle_message(
                    InboundMessage(
                        channel_name="slack",
                        chat_id="C123",
                        user_id="U-platform",
                        owner_user_id="deerflow-user-1",
                        connection_id="connection-1",
                        workspace_id="T123",
                        text="hi",
                    )
                )
            finally:
                await manager.stop()

            assert repo.lookups == []
            mock_client.threads.create.assert_not_called()
            mock_client.runs.wait.assert_not_called()

        _run(go())

    def test_auth_enabled_chat_rejects_unverified_bound_identity(self, monkeypatch):
        from app.channels.manager import BOUND_IDENTITY_UNAVAILABLE_MESSAGE, ChannelManager

        monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)

        async def go():
            bus = MessageBus()
            store = None
            repo = _BoundIdentityRepo(
                [
                    {
                        "id": "actual-connection",
                        "owner_user_id": "actual-owner",
                        "provider": "slack",
                        "external_account_id": "U-platform",
                        "workspace_id": None,
                    }
                ]
            )
            manager = ChannelManager(bus=bus, store=store, connection_repo=repo)
            mock_client = _make_mock_langgraph_client()
            manager._client = mock_client
            outbound_received = []

            async def capture(msg):
                outbound_received.append(msg)

            bus.subscribe_outbound(capture)
            await manager._handle_chat(
                InboundMessage(
                    channel_name="slack",
                    chat_id="C123",
                    user_id="U-platform",
                    owner_user_id="forged-owner",
                    connection_id="forged-connection",
                    text="hi",
                )
            )

            assert len(outbound_received) == 1
            assert outbound_received[0].text == BOUND_IDENTITY_UNAVAILABLE_MESSAGE
            assert outbound_received[0].connection_id is None
            assert outbound_received[0].owner_user_id is None
            mock_client.threads.create.assert_not_called()
            mock_client.runs.wait.assert_not_called()

        _run(go())

    def test_auth_disabled_chat_keeps_default_user_when_bound_identity_is_required(self, monkeypatch):
        from app.channels.manager import ChannelManager

        monkeypatch.setenv("DEER_FLOW_AUTH_DISABLED", "1")

        async def go():
            bus = MessageBus()
            store = None
            manager = ChannelManager(bus=bus, store=store)
            mock_client = _make_mock_langgraph_client(thread_id="thread-local")
            manager._client = mock_client

            await manager._handle_chat(
                InboundMessage(
                    channel_name="slack",
                    chat_id="C123",
                    user_id="U-platform",
                    text="hi",
                )
            )

            mock_client.threads.create.assert_not_called()
            mock_client.runs.wait.assert_not_called()

        _run(go())

    def test_legacy_open_bot_mode_fails_closed(self, monkeypatch):
        from app.channels.manager import ChannelManager

        monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED", raising=False)

        async def go():
            bus = MessageBus()
            store = None
            manager = ChannelManager(bus=bus, store=store)
            mock_client = _make_mock_langgraph_client(thread_id="thread-legacy")
            manager._client = mock_client

            await manager._handle_chat(
                InboundMessage(
                    channel_name="slack",
                    chat_id="C123",
                    user_id="U-platform",
                    text="hi",
                )
            )

            mock_client.threads.create.assert_not_called()
            mock_client.runs.wait.assert_not_called()

        _run(go())

    def test_webhook_channel_run_policy_cannot_opt_out_of_project_authority(self):
        from app.channels.run_policy import ChannelRunPolicy

        assert "requires_bound_identity" not in ChannelRunPolicy.__dataclass_fields__


# ---------------------------------------------------------------------------
# ChannelService tests
# ---------------------------------------------------------------------------


class TestExtractArtifacts:
    def test_extracts_from_present_files_tool_call(self):
        from app.channels.manager import _extract_artifacts

        result = {
            "messages": [
                {"type": "human", "content": "generate report"},
                {
                    "type": "ai",
                    "content": "Here is your report.",
                    "tool_calls": [
                        {"name": "present_files", "args": {"filepaths": ["/mnt/user-data/outputs/report.md"]}},
                    ],
                },
                {"type": "tool", "name": "present_files", "content": "Successfully presented files"},
            ]
        }
        assert _extract_artifacts(result) == ["/mnt/user-data/outputs/report.md"]

    def test_empty_when_no_present_files(self):
        from app.channels.manager import _extract_artifacts

        result = {
            "messages": [
                {"type": "human", "content": "hello"},
                {"type": "ai", "content": "hello"},
            ]
        }
        assert _extract_artifacts(result) == []

    def test_empty_for_list_result_no_tool_calls(self):
        from app.channels.manager import _extract_artifacts

        result = [{"type": "ai", "content": "hello"}]
        assert _extract_artifacts(result) == []

    def test_only_extracts_after_last_human_message(self):
        """Artifacts from previous turns (before the last human message) should be ignored."""
        from app.channels.manager import _extract_artifacts

        result = {
            "messages": [
                {"type": "human", "content": "make report"},
                {
                    "type": "ai",
                    "content": "Created report.",
                    "tool_calls": [
                        {"name": "present_files", "args": {"filepaths": ["/mnt/user-data/outputs/report.md"]}},
                    ],
                },
                {"type": "tool", "name": "present_files", "content": "ok"},
                {"type": "human", "content": "add chart"},
                {
                    "type": "ai",
                    "content": "Created chart.",
                    "tool_calls": [
                        {"name": "present_files", "args": {"filepaths": ["/mnt/user-data/outputs/chart.png"]}},
                    ],
                },
                {"type": "tool", "name": "present_files", "content": "ok"},
            ]
        }
        # Should only return chart.png (from the last turn)
        assert _extract_artifacts(result) == ["/mnt/user-data/outputs/chart.png"]

    def test_multiple_files_in_single_call(self):
        from app.channels.manager import _extract_artifacts

        result = {
            "messages": [
                {"type": "human", "content": "export"},
                {
                    "type": "ai",
                    "content": "Done.",
                    "tool_calls": [
                        {"name": "present_files", "args": {"filepaths": ["/mnt/user-data/outputs/a.txt", "/mnt/user-data/outputs/b.csv"]}},
                    ],
                },
            ]
        }
        assert _extract_artifacts(result) == ["/mnt/user-data/outputs/a.txt", "/mnt/user-data/outputs/b.csv"]

    def test_ignores_hidden_human_control_messages(self):
        """Hidden control messages should not hide current-turn present_files artifacts."""
        from app.channels.manager import _extract_artifacts

        result = {
            "messages": [
                {"type": "human", "content": "export"},
                {
                    "type": "ai",
                    "content": "Done.",
                    "tool_calls": [
                        {"name": "present_files", "args": {"filepaths": ["/mnt/user-data/outputs/plan.md"]}},
                    ],
                },
                {
                    "type": "human",
                    "name": "todo_completion_reminder",
                    "content": "mark tasks complete",
                    "additional_kwargs": {"hide_from_ui": True},
                },
            ]
        }

        assert _extract_artifacts(result) == ["/mnt/user-data/outputs/plan.md"]


class TestFormatArtifactText:
    def test_single_artifact(self):
        from app.channels.manager import _format_artifact_text

        text = _format_artifact_text(["/mnt/user-data/outputs/report.md"])
        assert text == "Created File: 📎 report.md"

    def test_multiple_artifacts(self):
        from app.channels.manager import _format_artifact_text

        text = _format_artifact_text(
            ["/mnt/user-data/outputs/a.txt", "/mnt/user-data/outputs/b.csv"],
        )
        assert text == "Created Files: 📎 a.txt、b.csv"


class TestFeishuChannel:
    def test_prepare_inbound_publishes_without_waiting_for_running_card(self):
        from app.channels.feishu import FeishuChannel

        async def go():
            bus = MessageBus()
            bus.publish_inbound = AsyncMock()
            channel = FeishuChannel(bus, config={})

            reply_started = asyncio.Event()
            release_reply = asyncio.Event()

            async def slow_reply(message_id: str, text: str) -> str:
                reply_started.set()
                await release_reply.wait()
                return "om-running-card"

            channel._add_reaction = AsyncMock()
            channel._reply_card = AsyncMock(side_effect=slow_reply)

            inbound = InboundMessage(
                channel_name="feishu",
                chat_id="chat-1",
                user_id="user-1",
                text="hello",
                thread_ts="om-source-msg",
            )

            prepare_task = asyncio.create_task(channel._prepare_inbound("om-source-msg", inbound))

            await _wait_for(lambda: bus.publish_inbound.await_count == 1)
            await prepare_task

            assert reply_started.is_set()
            assert "om-source-msg" in channel._running_card_tasks
            assert channel._reply_card.await_count == 1

            release_reply.set()
            await _wait_for(lambda: channel._running_card_ids.get("om-source-msg") == "om-running-card")
            await _wait_for(lambda: "om-source-msg" not in channel._running_card_tasks)

        _run(go())

    def test_prepare_inbound_and_send_share_running_card_task(self):
        from app.channels.feishu import FeishuChannel
        from deerflow.runtime.private_scope import PrivateResourceScope

        async def go():
            bus = MessageBus()
            bus.publish_inbound = AsyncMock()
            store = SimpleNamespace(
                get_thread_id=AsyncMock(return_value=None),
                set_thread_id=AsyncMock(return_value=True),
            )
            channel = FeishuChannel(bus, config={"channel_store": store})
            channel._api_client = MagicMock()

            reply_started = asyncio.Event()
            release_reply = asyncio.Event()

            async def slow_reply(message_id: str, text: str) -> str:
                reply_started.set()
                await release_reply.wait()
                return "om-running-card"

            channel._add_reaction = AsyncMock()
            channel._reply_card = AsyncMock(side_effect=slow_reply)
            channel._update_card = AsyncMock()

            inbound = InboundMessage(
                channel_name="feishu",
                chat_id="chat-1",
                user_id="user-1",
                text="hello",
                thread_ts="om-source-msg",
            )

            prepare_task = asyncio.create_task(channel._prepare_inbound("om-source-msg", inbound))
            await _wait_for(lambda: bus.publish_inbound.await_count == 1)
            await _wait_for(reply_started.is_set)

            send_task = asyncio.create_task(
                channel.send(
                    OutboundMessage(
                        channel_name="feishu",
                        chat_id="chat-1",
                        thread_id="thread-1",
                        text="Hello",
                        is_final=False,
                        thread_ts="om-source-msg",
                        connection_id="connection-1",
                        private_scope=PrivateResourceScope(
                            project_id=str(uuid.uuid4()),
                            owner_user_id=str(uuid.uuid4()),
                            membership_version=1,
                        ),
                        metadata={
                            "user_id": "user-1",
                            "root_id": "om-root-msg",
                            "topic_id": "om-root-msg",
                        },
                    )
                )
            )

            await asyncio.sleep(0)
            assert channel._reply_card.await_count == 1

            release_reply.set()
            await prepare_task
            await send_task

            assert channel._reply_card.await_count == 1
            channel._update_card.assert_awaited_once_with("om-running-card", "Hello")
            assert "om-source-msg" not in channel._running_card_tasks
            assert {call.kwargs["topic_id"] for call in store.set_thread_id.await_args_list} == {
                "om-source-msg",
                "om-running-card",
                "om-root-msg",
            }

        _run(go())

    def test_streaming_reuses_single_running_card(self):
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
            PatchMessageRequest,
            PatchMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        from app.channels.feishu import FeishuChannel

        async def go():
            bus = MessageBus()
            channel = FeishuChannel(bus, config={})

            channel._api_client = MagicMock()
            channel._ReplyMessageRequest = ReplyMessageRequest
            channel._ReplyMessageRequestBody = ReplyMessageRequestBody
            channel._PatchMessageRequest = PatchMessageRequest
            channel._PatchMessageRequestBody = PatchMessageRequestBody
            channel._CreateMessageReactionRequest = CreateMessageReactionRequest
            channel._CreateMessageReactionRequestBody = CreateMessageReactionRequestBody
            channel._Emoji = Emoji

            reply_response = MagicMock()
            reply_response.data.message_id = "om-running-card"
            channel._api_client.im.v1.message.reply = MagicMock(return_value=reply_response)
            channel._api_client.im.v1.message.patch = MagicMock()
            channel._api_client.im.v1.message_reaction.create = MagicMock()

            await channel._send_running_reply("om-source-msg")

            await channel.send(
                OutboundMessage(
                    channel_name="feishu",
                    chat_id="chat-1",
                    thread_id="thread-1",
                    text="Hello",
                    is_final=False,
                    thread_ts="om-source-msg",
                )
            )
            await channel.send(
                OutboundMessage(
                    channel_name="feishu",
                    chat_id="chat-1",
                    thread_id="thread-1",
                    text="Hello world",
                    is_final=True,
                    thread_ts="om-source-msg",
                )
            )

            assert channel._api_client.im.v1.message.reply.call_count == 1
            assert channel._api_client.im.v1.message.patch.call_count == 2
            assert channel._api_client.im.v1.message_reaction.create.call_count == 1
            assert "om-source-msg" not in channel._running_card_ids
            assert "om-source-msg" not in channel._running_card_tasks

            first_patch_request = channel._api_client.im.v1.message.patch.call_args_list[0].args[0]
            final_patch_request = channel._api_client.im.v1.message.patch.call_args_list[1].args[0]
            assert first_patch_request.message_id == "om-running-card"
            assert final_patch_request.message_id == "om-running-card"
            assert json.loads(first_patch_request.body.content)["elements"][0]["content"] == "Hello"
            assert json.loads(final_patch_request.body.content)["elements"][0]["content"] == "Hello world"
            assert json.loads(final_patch_request.body.content)["config"]["update_multi"] is True

        _run(go())


class TestWeComChannel:
    def test_publish_ws_inbound_starts_stream_and_publishes_message(self, monkeypatch):
        from app.channels.wecom import WeComChannel

        async def go():
            bus = MessageBus()
            bus.publish_inbound = AsyncMock()
            channel = WeComChannel(bus, config={})
            channel._ws_client = SimpleNamespace(reply_stream=AsyncMock())

            monkeypatch.setitem(
                __import__("sys").modules,
                "aibot",
                SimpleNamespace(generate_req_id=lambda prefix: "stream-1"),
            )

            frame = {
                "body": {
                    "msgid": "msg-1",
                    "from": {"userid": "user-1"},
                    "aibotid": "bot-1",
                    "chattype": "single",
                }
            }
            files = [{"type": "image", "url": "https://example.com/image.png"}]

            await channel._publish_ws_inbound(frame, "hello", files=files)

            channel._ws_client.reply_stream.assert_awaited_once_with(frame, "stream-1", "Working on it...", False)
            bus.publish_inbound.assert_awaited_once()

            inbound = bus.publish_inbound.await_args.args[0]
            assert inbound.channel_name == "wecom"
            assert inbound.chat_id == "user-1"
            assert inbound.user_id == "user-1"
            assert inbound.text == "hello"
            assert inbound.thread_ts == "msg-1"
            assert inbound.topic_id == "user-1"
            assert inbound.files == files
            assert inbound.metadata == {"aibotid": "bot-1", "chattype": "single", "message_id": "msg-1"}
            assert channel._ws_frames["msg-1"] is frame
            assert channel._ws_stream_ids["msg-1"] == "stream-1"

        _run(go())

    def test_publish_ws_inbound_uses_configured_working_message(self, monkeypatch):
        from app.channels.wecom import WeComChannel

        async def go():
            bus = MessageBus()
            bus.publish_inbound = AsyncMock()
            channel = WeComChannel(bus, config={"working_message": "Please wait..."})
            channel._ws_client = SimpleNamespace(reply_stream=AsyncMock())
            channel._working_message = "Please wait..."

            monkeypatch.setitem(
                __import__("sys").modules,
                "aibot",
                SimpleNamespace(generate_req_id=lambda prefix: "stream-1"),
            )

            frame = {
                "body": {
                    "msgid": "msg-1",
                    "from": {"userid": "user-1"},
                }
            }

            await channel._publish_ws_inbound(frame, "hello")

            channel._ws_client.reply_stream.assert_awaited_once_with(frame, "stream-1", "Please wait...", False)

        _run(go())

    def test_publish_ws_inbound_treats_slash_prefixed_paths_as_chat(self, monkeypatch):
        from app.channels.wecom import WeComChannel

        async def go():
            bus = MessageBus()
            bus.publish_inbound = AsyncMock()
            channel = WeComChannel(bus, config={})
            channel._ws_client = SimpleNamespace(reply_stream=AsyncMock())

            monkeypatch.setitem(
                __import__("sys").modules,
                "aibot",
                SimpleNamespace(generate_req_id=lambda prefix: "stream-1"),
            )

            frame = {
                "body": {
                    "msgid": "msg-1",
                    "from": {"userid": "user-1"},
                }
            }

            await channel._publish_ws_inbound(frame, "/mnt/user-data/uploads/report.pdf")

            inbound = bus.publish_inbound.await_args.args[0]
            assert inbound.text == "/mnt/user-data/uploads/report.pdf"
            assert inbound.msg_type == InboundMessageType.CHAT

        _run(go())

    def test_on_outbound_sends_attachment_before_clearing_context(self, tmp_path):
        from app.channels.wecom import WeComChannel

        async def go():
            bus = MessageBus()
            channel = WeComChannel(bus, config={})

            frame = {"body": {"msgid": "msg-1"}}
            ws_client = SimpleNamespace(
                reply_stream=AsyncMock(),
                reply=AsyncMock(),
            )
            channel._ws_client = ws_client
            channel._ws_frames["msg-1"] = frame
            channel._ws_stream_ids["msg-1"] = "stream-1"
            channel._upload_media_ws = AsyncMock(return_value="media-1")

            attachment_path = tmp_path / "image.png"
            attachment_path.write_bytes(b"png")
            attachment = ResolvedAttachment(
                virtual_path="/mnt/user-data/outputs/image.png",
                actual_path=attachment_path,
                filename="image.png",
                mime_type="image/png",
                size=attachment_path.stat().st_size,
                is_image=True,
            )

            msg = OutboundMessage(
                channel_name="wecom",
                chat_id="user-1",
                thread_id="thread-1",
                text="done",
                attachments=[attachment],
                is_final=True,
                thread_ts="msg-1",
            )

            await channel._on_outbound(msg)

            ws_client.reply_stream.assert_awaited_once_with(frame, "stream-1", "done", True)
            channel._upload_media_ws.assert_awaited_once_with(
                media_type="image",
                filename="image.png",
                path=str(attachment_path),
                size=attachment.size,
            )
            ws_client.reply.assert_awaited_once_with(frame, {"image": {"media_id": "media-1"}, "msgtype": "image"})
            assert "msg-1" not in channel._ws_frames
            assert "msg-1" not in channel._ws_stream_ids

        _run(go())

    def test_send_falls_back_to_send_message_without_thread_context(self):
        from app.channels.wecom import WeComChannel

        async def go():
            bus = MessageBus()
            channel = WeComChannel(bus, config={})
            channel._ws_client = SimpleNamespace(send_message=AsyncMock())

            msg = OutboundMessage(
                channel_name="wecom",
                chat_id="user-1",
                thread_id="thread-1",
                text="hello",
                thread_ts=None,
            )

            await channel.send(msg)

            channel._ws_client.send_message.assert_awaited_once_with(
                "user-1",
                {"msgtype": "markdown", "markdown": {"content": "hello"}},
            )

        _run(go())

    def test_on_ws_task_done_logs_error_on_exception(self, caplog):
        import logging

        from app.channels.wecom import WeComChannel

        channel = WeComChannel(MessageBus(), config={})
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("boom")

        with caplog.at_level(logging.ERROR):
            channel._on_ws_task_done(task)

        assert any("WeCom WebSocket connection task failed" in r.message and r.levelno == logging.ERROR for r in caplog.records)

    def test_on_ws_task_done_silent_when_cancelled(self, caplog):
        import logging

        from app.channels.wecom import WeComChannel

        channel = WeComChannel(MessageBus(), config={})
        task = MagicMock()
        task.cancelled.return_value = True

        with caplog.at_level(logging.ERROR):
            channel._on_ws_task_done(task)

        task.exception.assert_not_called()
        assert caplog.records == []

    def test_on_ws_task_done_silent_when_no_exception(self, caplog):
        import logging

        from app.channels.wecom import WeComChannel

        channel = WeComChannel(MessageBus(), config={})
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = None

        with caplog.at_level(logging.ERROR):
            channel._on_ws_task_done(task)

        assert caplog.records == []

    def test_on_ws_error_logs_error(self, caplog):
        import logging

        from app.channels.wecom import WeComChannel

        channel = WeComChannel(MessageBus(), config={})

        with caplog.at_level(logging.ERROR):
            channel._on_ws_error(RuntimeError("handshake failed"))

        assert any("WeCom WebSocket error" in r.message and r.levelno == logging.ERROR for r in caplog.records)

    def test_on_ws_disconnected_logs_warning(self, caplog):
        import logging

        from app.channels.wecom import WeComChannel

        channel = WeComChannel(MessageBus(), config={})

        with caplog.at_level(logging.WARNING):
            channel._on_ws_disconnected()

        assert any("WeCom WebSocket disconnected" in r.message and r.levelno == logging.WARNING for r in caplog.records)

    def test_on_ws_disconnected_logs_reason_when_present(self, caplog):
        import logging

        from app.channels.wecom import WeComChannel

        channel = WeComChannel(MessageBus(), config={})

        with caplog.at_level(logging.WARNING):
            channel._on_ws_disconnected("connection reset")

        assert any("connection reset" in r.message and r.levelno == logging.WARNING for r in caplog.records)

    def test_start_subscribes_connection_lifecycle_events(self, monkeypatch):
        from app.channels.wecom import WeComChannel

        async def go():
            bus = MessageBus()
            channel = WeComChannel(bus, config={"bot_id": "corp123", "bot_secret": "secret"})

            ws_client = MagicMock()

            async def fake_connect():
                return None

            ws_client.connect = fake_connect

            monkeypatch.setitem(
                __import__("sys").modules,
                "aibot",
                SimpleNamespace(
                    WSClient=lambda options: ws_client,
                    WSClientOptions=lambda **kwargs: SimpleNamespace(**kwargs),
                ),
            )

            await channel.start()

            subscribed_events = {call.args[0] for call in ws_client.on.call_args_list}
            assert "error" in subscribed_events
            assert "disconnected" in subscribed_events
            assert channel._ws_task is not None

            await channel.stop()

        _run(go())


class TestChannelService:
    def test_get_status_no_channels(self):
        from app.channels.service import ChannelService

        async def go():
            service = ChannelService(channels_config={})
            await service.start()

            status = service.get_status()
            assert status["service_running"] is True
            for ch_status in status["channels"].values():
                assert ch_status["enabled"] is False
                assert ch_status["running"] is False

            await service.stop()

        _run(go())

    def test_is_channel_enabled_reflects_live_config(self):
        """``is_channel_enabled`` is the runtime kill-switch read by the GitHub
        webhook router. Verify it tracks the live ``_config`` dict, including
        updates from ``configure_channel`` (which the UI uses to flip the
        enabled flag without rewriting ``config.yaml``).
        """
        from app.channels.service import ChannelService

        async def go():
            service = ChannelService(
                channels_config={
                    "github": {"enabled": True, "default_mention_login": "bot"},
                    "feishu": {"enabled": False},
                }
            )
            await service.start()

            # Configured + enabled → True.
            assert service.is_channel_enabled("github") is True
            # Configured + disabled → False.
            assert service.is_channel_enabled("feishu") is False
            # Not present at all → False (don't fail open).
            assert service.is_channel_enabled("slack") is False
            # Non-dict garbage in config → False (defensive).
            service._config["broken"] = "not a dict"
            assert service.is_channel_enabled("broken") is False

            # Runtime flip via configure_channel must be visible.
            await service.configure_channel("github", {"enabled": False})
            assert service.is_channel_enabled("github") is False

            await service.stop()

        _run(go())

    def test_disabled_channels_are_skipped(self):
        from app.channels.service import ChannelService

        async def go():
            service = ChannelService(
                channels_config={
                    "feishu": {"enabled": False, "app_id": "x", "app_secret": "y"},
                }
            )
            await service.start()
            assert "feishu" not in service._channels
            await service.stop()

        _run(go())

    def test_concurrent_ensure_channel_ready_starts_channel_once(self):
        from app.channels.service import ChannelService

        async def go():
            service = ChannelService(
                channels_config={
                    "telegram": {"enabled": True, "bot_token": "tg-token"},
                }
            )
            await service.manager.start()
            service._running = True
            start_calls = []

            async def fake_start_channel(name, config):
                start_calls.append(name)
                await asyncio.sleep(0.01)
                service._channels[name] = SimpleNamespace(is_running=True, stop=AsyncMock())
                return True

            service._start_channel = fake_start_channel

            results = await asyncio.gather(
                service.ensure_channel_ready("telegram"),
                service.ensure_channel_ready("telegram"),
            )

            assert results == [True, True]
            assert start_calls == ["telegram"]
            await service.stop()

        _run(go())

    def test_session_config_is_forwarded_to_manager(self):
        from app.channels.service import ChannelService

        service = ChannelService(
            channels_config={
                "session": {"context": {"thinking_enabled": False}},
                "telegram": {
                    "enabled": False,
                    "session": {
                        "assistant_id": "mobile_agent",
                        "users": {
                            "vip": {
                                "assistant_id": "vip_agent",
                            }
                        },
                    },
                },
            }
        )

        assert service.manager._default_session["context"]["thinking_enabled"] is False
        assert service.manager._channel_sessions["telegram"]["assistant_id"] == "mobile_agent"
        assert service.manager._channel_sessions["telegram"]["users"]["vip"]["assistant_id"] == "vip_agent"

    def test_service_urls_fall_back_to_env(self, monkeypatch):
        from app.channels.service import ChannelService

        monkeypatch.setenv("DEER_FLOW_CHANNELS_LANGGRAPH_URL", "http://gateway:8001/api")
        monkeypatch.setenv("DEER_FLOW_CHANNELS_GATEWAY_URL", "http://gateway:8001")

        service = ChannelService(channels_config={})

        assert service.manager._langgraph_url == "http://gateway:8001/api"
        assert service.manager._gateway_url == "http://gateway:8001"

    def test_config_service_urls_override_env(self, monkeypatch):
        from app.channels.service import ChannelService

        monkeypatch.setenv("DEER_FLOW_CHANNELS_LANGGRAPH_URL", "http://gateway:8001/api")
        monkeypatch.setenv("DEER_FLOW_CHANNELS_GATEWAY_URL", "http://gateway:8001")

        service = ChannelService(
            channels_config={
                "langgraph_url": "http://custom-gateway:8001/api",
                "gateway_url": "http://custom-gateway:8001",
            }
        )

        assert service.manager._langgraph_url == "http://custom-gateway:8001/api"
        assert service.manager._gateway_url == "http://custom-gateway:8001"

    def test_from_app_config_uses_explicit_config(self):
        from app.channels.service import ChannelService

        app_config = SimpleNamespace(
            model_extra={
                "channels": {
                    "telegram": {"enabled": False},
                }
            }
        )

        with patch("deerflow.config.app_config.get_app_config", side_effect=AssertionError("should not read global config")):
            service = ChannelService.from_app_config(app_config)

        assert service._config == {"telegram": {"enabled": False}}

    def test_from_app_config_does_not_create_runtime_channels_from_channel_connections(
        self,
        monkeypatch,
        tmp_path,
    ):
        from app.channels.service import ChannelService
        from deerflow.config import paths as paths_module
        from deerflow.config.channel_connections_config import ChannelConnectionsConfig

        monkeypatch.setattr("app.channels.service._make_connection_repo", lambda _config: object())
        monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
        monkeypatch.setattr(paths_module, "_paths", None)
        app_config = SimpleNamespace(
            model_extra={},
            channel_connections=ChannelConnectionsConfig.model_validate(
                {
                    "enabled": True,
                    "telegram": {"enabled": True, "bot_username": "deerflow_bot"},
                    "slack": {"enabled": True},
                    "discord": {"enabled": True},
                }
            ),
        )

        service = ChannelService.from_app_config(app_config)

        assert service._config == {}

    def test_start_retries_configured_channel_until_ready(self, monkeypatch):
        from app.channels.service import ChannelService

        class FlakyReadyChannel(Channel):
            starts = 0

            def __init__(self, bus, config):
                super().__init__(name="slack", bus=bus, config=config)

            async def start(self):
                type(self).starts += 1
                self._running = type(self).starts >= 2

            async def stop(self):
                self._running = False

            async def send(self, msg):
                return None

        monkeypatch.setattr(
            "deerflow.reflection.resolve_class",
            lambda import_path, base_class=None: FlakyReadyChannel,
        )

        async def go():
            service = ChannelService(
                channels_config={
                    "slack": {
                        "enabled": True,
                        "bot_token": "xoxb-ui",
                        "app_token": "xapp-ui",
                    },
                }
            )

            try:
                await service.start()

                assert FlakyReadyChannel.starts == 2
                assert service.get_status()["channels"]["slack"]["running"] is True
            finally:
                await service.stop()

        _run(go())

    def test_connection_repo_is_forwarded_to_manager(self):
        from app.channels.service import ChannelService

        repo = object()
        service = ChannelService(channels_config={}, connection_repo=repo)

        assert service.manager._connection_repo is repo

    def test_service_has_no_bound_identity_opt_out(self):
        from app.channels.service import ChannelService

        service = ChannelService(channels_config={})

        assert not hasattr(service.manager, "_require_bound_identity")

    def test_remove_channel_stops_running_channel_and_forgets_config(self):
        from app.channels.service import ChannelService

        async def go():
            service = ChannelService(
                channels_config={
                    "slack": {
                        "enabled": True,
                        "bot_token": "xoxb-ui",
                        "app_token": "xapp-ui",
                    },
                }
            )
            channel = AsyncMock()
            service._channels["slack"] = channel
            service._running = True

            assert await service.remove_channel("slack") is True

            channel.stop.assert_awaited_once()
            assert "slack" not in service._channels
            assert "slack" not in service._config

        _run(go())

    def test_disabled_channel_with_string_creds_emits_warning(self, caplog):
        """Warning is emitted when a channel has string credentials but enabled=false."""
        import logging

        from app.channels.service import ChannelService

        async def go():
            service = ChannelService(
                channels_config={
                    "wecom": {"enabled": False, "bot_id": "corp123", "bot_secret": "secret"},
                }
            )
            with caplog.at_level(logging.WARNING, logger="app.channels.service"):
                await service.start()
            await service.stop()

        _run(go())
        assert any("credentials configured but is disabled" in r.message and r.levelno == logging.WARNING for r in caplog.records)
        assert all("wecom" not in r.message for r in caplog.records)

    def test_disabled_channel_with_int_creds_emits_warning(self, caplog):
        """Warning is emitted even when YAML-parsed integer credentials are present."""
        import logging

        from app.channels.service import ChannelService

        async def go():
            # Simulate YAML parsing a numeric token/ID as an int
            service = ChannelService(
                channels_config={
                    "telegram": {"enabled": False, "bot_token": 123456789},
                }
            )
            with caplog.at_level(logging.WARNING, logger="app.channels.service"):
                await service.start()
            await service.stop()

        _run(go())
        assert any("credentials configured but is disabled" in r.message and r.levelno == logging.WARNING for r in caplog.records)
        assert all("telegram" not in r.message for r in caplog.records)

    def test_disabled_channel_without_creds_emits_info(self, caplog):
        """Only an info log (no warning) is emitted when a channel is disabled with no credentials."""
        import logging

        from app.channels.service import ChannelService

        async def go():
            service = ChannelService(
                channels_config={
                    "telegram": {"enabled": False},
                }
            )
            with caplog.at_level(logging.DEBUG, logger="app.channels.service"):
                await service.start()
            await service.stop()

        _run(go())
        warning_records = [r for r in caplog.records if "telegram" in r.message and r.levelno == logging.WARNING]
        assert not warning_records

    # -- restart_channel config reload tests (issue #3497) --

    def test_restart_channel_reloads_config_from_disk(self, monkeypatch):
        """restart_channel reads the latest config via get_app_config()."""
        from app.channels.service import ChannelService

        initial_config = {"feishu": {"enabled": True, "app_id": "old_id", "app_secret": "old_secret"}}
        updated_config = {"feishu": {"enabled": True, "app_id": "new_id", "app_secret": "new_secret"}}

        service = ChannelService(channels_config=initial_config)

        def mock_get_app_config():
            return SimpleNamespace(model_extra={"channels": updated_config})

        monkeypatch.setattr("deerflow.config.app_config.get_app_config", mock_get_app_config)

        started_configs = {}

        async def mock_start_channel(name, config):
            started_configs[name] = config
            return True

        service._start_channel = mock_start_channel

        async def go():
            await service.restart_channel("feishu")

        _run(go())

        assert started_configs["feishu"]["app_id"] == "new_id"
        assert started_configs["feishu"]["app_secret"] == "new_secret"
        assert service._config["feishu"]["app_id"] == "new_id"

    def test_configure_channel_keeps_explicit_config_over_stale_file_entry(self, monkeypatch):
        """UI-entered runtime credentials must not be clobbered by a config.yaml reload.

        configure_channel() receives the authoritative config (e.g. from the
        browser Connect/Modify dialog, never written to config.yaml), so its
        restart must skip the file reload that restart_channel() performs for
        operator-triggered restarts.
        """
        from app.channels.service import ChannelService

        def fail_get_app_config():
            raise AssertionError("configure_channel must not reload file config")

        monkeypatch.setattr("deerflow.config.app_config.get_app_config", fail_get_app_config)

        service = ChannelService(channels_config={})
        service._running = True

        started_configs = {}

        async def mock_start_channel(name, config):
            started_configs[name] = config
            return True

        service._start_channel = mock_start_channel

        async def go():
            await service.configure_channel("feishu", {"enabled": True, "app_id": "ui_id", "app_secret": "ui_secret"})

        _run(go())

        assert started_configs["feishu"]["app_id"] == "ui_id"
        assert started_configs["feishu"]["app_secret"] == "ui_secret"
        assert service._config["feishu"]["app_id"] == "ui_id"

    def test_restart_channel_falls_back_to_cached_config_on_error(self, monkeypatch):
        """When get_app_config() fails, restart_channel uses cached config."""
        from app.channels.service import ChannelService

        cached_config = {"feishu": {"enabled": True, "app_id": "cached_id", "app_secret": "cached_secret"}}
        service = ChannelService(channels_config=cached_config)

        def _raise():
            raise RuntimeError("config missing")

        monkeypatch.setattr("deerflow.config.app_config.get_app_config", _raise)

        started_configs = {}

        async def mock_start_channel(name, config):
            started_configs[name] = config
            return True

        service._start_channel = mock_start_channel

        async def go():
            await service.restart_channel("feishu")

        _run(go())

        assert started_configs["feishu"]["app_id"] == "cached_id"

    def test_restart_channel_returns_false_for_unknown_channel(self):
        """restart_channel returns False when the channel has no config."""
        from app.channels.service import ChannelService

        service = ChannelService(channels_config={})

        async def go():
            result = await service.restart_channel("nonexistent")
            assert result is False

        _run(go())

    def test_restart_channel_stops_existing_channel_before_restart(self):
        """restart_channel stops the running channel instance before restarting."""
        from app.channels.service import ChannelService

        service = ChannelService(channels_config={"feishu": {"enabled": True, "app_id": "x", "app_secret": "y"}})

        stopped = []

        class FakeChannel:
            is_running = True

            async def stop(self):
                stopped.append(True)

        service._channels["feishu"] = FakeChannel()

        started_configs = {}

        async def mock_start_channel(name, config):
            started_configs[name] = config
            return True

        service._start_channel = mock_start_channel

        async def go():
            await service.restart_channel("feishu", reload_config=False)

        _run(go())

        assert stopped
        assert "feishu" in started_configs

    def test_restart_channel_skips_disabled_channel(self, monkeypatch):
        """restart_channel stops the channel and returns True when config has enabled: false."""
        from app.channels.service import ChannelService

        service = ChannelService(channels_config={"feishu": {"enabled": True, "app_id": "x", "app_secret": "y"}})

        stopped = []

        class FakeChannel:
            is_running = True

            async def stop(self):
                stopped.append(True)

        service._channels["feishu"] = FakeChannel()

        # Simulate config.yaml updated to enabled: false
        disabled_config = {"feishu": {"enabled": False, "app_id": "x", "app_secret": "y"}}

        def mock_get_app_config():
            return SimpleNamespace(model_extra={"channels": disabled_config})

        monkeypatch.setattr("deerflow.config.app_config.get_app_config", mock_get_app_config)

        started = []

        async def mock_start_channel(name, config):
            started.append(name)
            return True

        service._start_channel = mock_start_channel

        async def go():
            result = await service.restart_channel("feishu")
            assert result is True  # successfully stopped (no restart needed)

        _run(go())

        assert stopped  # old channel was stopped
        assert not started  # _start_channel was NOT called


# ---------------------------------------------------------------------------
# Slack send retry tests
# ---------------------------------------------------------------------------


class TestSlackSendRetry:
    def test_retries_on_failure_then_succeeds(self):
        from app.channels.slack import SlackChannel

        async def go():
            bus = MessageBus()
            ch = SlackChannel(bus=bus, config={"bot_token": "xoxb-test", "app_token": "xapp-test"})

            mock_web = MagicMock()
            call_count = 0

            def post_message(**kwargs):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ConnectionError("network error")
                return MagicMock()

            mock_web.chat_postMessage = post_message
            ch._web_client = mock_web

            msg = OutboundMessage(channel_name="slack", chat_id="C123", thread_id="t1", text="hello")
            await ch.send(msg)
            assert call_count == 3

        _run(go())


class TestSlackAllowedUsers:
    @staticmethod
    def _submit_coro(coro, loop):
        del loop
        asyncio.run(coro)
        return MagicMock()

    def test_numeric_allowed_users_match_string_event_user_id(self):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        channel = SlackChannel(
            bus=bus,
            config={"allowed_users": [123456]},
        )
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        event = {
            "user": "123456",
            "text": "hello from slack",
            "channel": "C123",
            "ts": "1710000000.000100",
        }

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ) as submit:
            channel._handle_message_event(event)

        channel._add_reaction.assert_called_once_with("C123", "1710000000.000100", "eyes")
        channel._send_running_reply.assert_called_once_with("C123", "1710000000.000100")
        submit.assert_called_once()
        inbound = bus.publish_inbound.call_args.args[0]
        assert inbound.user_id == "123456"
        assert inbound.chat_id == "C123"
        assert inbound.text == "hello from slack"

    def test_string_allowed_users_match_event_user_id(self):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        channel = SlackChannel(
            bus=bus,
            config={"allowed_users": "U123456"},
        )
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        event = {
            "user": "U123456",
            "text": "hello from slack",
            "channel": "C123",
            "ts": "1710000000.000100",
        }

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ) as submit:
            channel._handle_message_event(event)

        channel._add_reaction.assert_called_once_with("C123", "1710000000.000100", "eyes")
        channel._send_running_reply.assert_called_once_with("C123", "1710000000.000100")
        submit.assert_called_once()
        inbound = bus.publish_inbound.call_args.args[0]
        assert inbound.user_id == "U123456"
        assert inbound.chat_id == "C123"
        assert inbound.text == "hello from slack"

    def test_connect_code_bypasses_allowed_users_filter(self):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        channel = SlackChannel(
            bus=bus,
            config={"allowed_users": ["U-allowed"], "connection_repo": object()},
        )
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._bind_connection_from_connect_code = AsyncMock(return_value=True)
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        event = {
            "user": "U-blocked",
            "text": "/connect slack-bind-code",
            "team": "T123",
            "channel": "C123",
            "ts": "1710000000.000100",
        }

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ) as submit:
            channel._handle_message_event(event)

        channel._bind_connection_from_connect_code.assert_called_once()
        submit.assert_called_once()
        bus.publish_inbound.assert_not_awaited()
        channel._add_reaction.assert_not_called()
        channel._send_running_reply.assert_not_called()

    def test_app_mention_strips_leading_bot_mention_before_command_detection(self):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        channel = SlackChannel(bus=bus, config={"bot_user_id": "UBOT"})
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        event = {
            "type": "app_mention",
            "user": "U123456",
            "text": "<@UBOT> /help",
            "channel": "C123",
            "ts": "1710000000.000100",
        }

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ):
            channel._handle_message_event(event)

        inbound = bus.publish_inbound.call_args.args[0]
        assert inbound.text == "/help"
        assert inbound.msg_type == InboundMessageType.COMMAND

    def test_app_mention_strips_labelled_leading_bot_mention(self):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        channel = SlackChannel(bus=bus, config={"bot_user_id": "UBOT"})
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        event = {
            "type": "app_mention",
            "user": "U123456",
            "text": "<@UBOT|deerflow> /help",
            "channel": "C123",
            "ts": "1710000000.000100",
        }

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ):
            channel._handle_message_event(event)

        inbound = bus.publish_inbound.call_args.args[0]
        assert inbound.text == "/help"
        assert inbound.msg_type == InboundMessageType.COMMAND

    def test_app_mention_strips_leading_bot_mention_before_slash_skill(self):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        channel = SlackChannel(bus=bus, config={"bot_user_id": "UBOT"})
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        event = {
            "type": "app_mention",
            "user": "U123456",
            "text": "<@UBOT> /data-analysis analyze uploads/foo.csv",
            "channel": "C123",
            "ts": "1710000000.000100",
        }

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ):
            channel._handle_message_event(event)

        inbound = bus.publish_inbound.call_args.args[0]
        assert inbound.text == "/data-analysis analyze uploads/foo.csv"
        assert inbound.msg_type == InboundMessageType.CHAT

    def test_app_mention_preserves_following_user_mention(self):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        channel = SlackChannel(bus=bus, config={"bot_user_id": "UBOT"})
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        event = {
            "type": "app_mention",
            "user": "U123456",
            "text": "<@UBOT> <@UASSIGNEE> please review this",
            "channel": "C123",
            "ts": "1710000000.000100",
        }

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ):
            channel._handle_message_event(event)

        inbound = bus.publish_inbound.call_args.args[0]
        assert inbound.text == "<@UASSIGNEE> please review this"
        assert inbound.msg_type == InboundMessageType.CHAT

    def test_app_mention_preserves_leading_non_bot_mention_when_bot_id_known(self):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        channel = SlackChannel(bus=bus, config={"bot_user_id": "UBOT"})
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        event = {
            "type": "app_mention",
            "user": "U123456",
            "text": "<@UASSIGNEE> <@UBOT> please review this",
            "channel": "C123",
            "ts": "1710000000.000100",
        }

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ):
            channel._handle_message_event(event)

        inbound = bus.publish_inbound.call_args.args[0]
        assert inbound.text == "<@UASSIGNEE> <@UBOT> please review this"
        assert inbound.msg_type == InboundMessageType.CHAT

    def test_app_mention_preserves_leading_non_bot_mention_when_bot_id_unknown(self):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        channel = SlackChannel(bus=bus, config={})
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        event = {
            "type": "app_mention",
            "user": "U123456",
            "text": "<@UASSIGNEE> /help <@UBOT>",
            "channel": "C123",
            "ts": "1710000000.000100",
        }

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ):
            channel._handle_message_event(event)

        inbound = bus.publish_inbound.call_args.args[0]
        assert inbound.text == "<@UASSIGNEE> /help <@UBOT>"
        assert inbound.msg_type == InboundMessageType.CHAT

    def test_socket_event_resolves_bot_user_id_before_app_mention_command_detection(self):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        channel = SlackChannel(bus=bus, config={})
        channel._SocketModeResponse = lambda envelope_id: SimpleNamespace(envelope_id=envelope_id)
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        client = SimpleNamespace(send_socket_mode_response=MagicMock())
        req = SimpleNamespace(
            envelope_id="env-1",
            type="events_api",
            payload={
                "authorizations": [{"user_id": "UBOT"}],
                "event": {
                    "type": "app_mention",
                    "user": "U123456",
                    "text": "<@UBOT> /help",
                    "channel": "C123",
                    "ts": "1710000000.000100",
                },
            },
        )

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ):
            channel._on_socket_event(client, req)

        inbound = bus.publish_inbound.call_args.args[0]
        assert channel._bot_user_id == "UBOT"
        assert inbound.text == "/help"
        assert inbound.msg_type == InboundMessageType.COMMAND

    def test_scalar_allowed_users_warns_and_matches_stringified_event_user_id(self, caplog):
        from app.channels.slack import SlackChannel

        bus = MessageBus()
        bus.publish_inbound = AsyncMock()
        with caplog.at_level("WARNING"):
            channel = SlackChannel(
                bus=bus,
                config={"allowed_users": 123456},
            )
        channel._loop = MagicMock()
        channel._loop.is_running.return_value = True
        channel._add_reaction = MagicMock()
        channel._send_running_reply = MagicMock()

        event = {
            "user": "123456",
            "text": "hello from slack",
            "channel": "C123",
            "ts": "1710000000.000100",
        }

        with patch(
            "app.channels.slack.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit_coro,
        ) as submit:
            channel._handle_message_event(event)

        assert "Slack allowed_users should be a list" in caplog.text
        submit.assert_called_once()
        inbound = bus.publish_inbound.call_args.args[0]
        assert inbound.user_id == "123456"

    def test_raises_after_all_retries_exhausted(self):
        from app.channels.slack import SlackChannel

        async def go():
            bus = MessageBus()
            ch = SlackChannel(bus=bus, config={"bot_token": "xoxb-test", "app_token": "xapp-test"})

            mock_web = MagicMock()
            mock_web.chat_postMessage = MagicMock(side_effect=ConnectionError("fail"))
            ch._web_client = mock_web

            msg = OutboundMessage(channel_name="slack", chat_id="C123", thread_id="t1", text="hello")
            with pytest.raises(ConnectionError):
                await ch.send(msg)

            assert mock_web.chat_postMessage.call_count == 3

        _run(go())

    def test_raises_runtime_error_when_no_attempts_configured(self):
        from app.channels.slack import SlackChannel

        async def go():
            bus = MessageBus()
            ch = SlackChannel(bus=bus, config={"bot_token": "xoxb-test", "app_token": "xapp-test"})
            ch._web_client = MagicMock()

            msg = OutboundMessage(channel_name="slack", chat_id="C123", thread_id="t1", text="hello")
            with pytest.raises(RuntimeError, match="without an exception"):
                await ch.send(msg, _max_retries=0)

        _run(go())


# ---------------------------------------------------------------------------
# Telegram send retry tests
# ---------------------------------------------------------------------------


class TestTelegramSendRetry:
    def test_start_registers_known_channel_commands(self, monkeypatch):
        import sys
        from types import ModuleType

        from app.channels.commands import KNOWN_CHANNEL_COMMANDS
        from app.channels.telegram import TelegramChannel

        class FakeFilter:
            def __init__(self, expr: str):
                self.expr = expr

            def __and__(self, other):
                return FakeFilter(f"{self.expr}&{other.expr}")

            def __invert__(self):
                return FakeFilter(f"~{self.expr}")

        class FakeApplication:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler):
                self.handlers.append(handler)

        fake_app = FakeApplication()

        class FakeApplicationBuilder:
            def token(self, token):
                assert token == "test-token"
                return self

            def build(self):
                return fake_app

        def fake_command_handler(command, callback):
            return SimpleNamespace(kind="command", command=command, callback=callback)

        def fake_message_handler(filter_expr, callback):
            return SimpleNamespace(kind="message", filter_expr=filter_expr, callback=callback)

        telegram_mod = ModuleType("telegram")
        telegram_ext_mod = ModuleType("telegram.ext")
        telegram_ext_mod.ApplicationBuilder = FakeApplicationBuilder
        telegram_ext_mod.CommandHandler = fake_command_handler
        telegram_ext_mod.MessageHandler = fake_message_handler
        telegram_ext_mod.filters = SimpleNamespace(TEXT=FakeFilter("TEXT"), COMMAND=FakeFilter("COMMAND"))
        telegram_mod.ext = telegram_ext_mod
        monkeypatch.setitem(sys.modules, "telegram", telegram_mod)
        monkeypatch.setitem(sys.modules, "telegram.ext", telegram_ext_mod)

        class FakeThread:
            def __init__(self, *, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                return None

            def join(self, timeout=None):
                return None

        monkeypatch.setattr("app.channels.telegram.threading.Thread", FakeThread)

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})

            await ch.start()
            try:
                registered_commands = {handler.command for handler in fake_app.handlers if handler.kind == "command"}
                expected_commands = {command.removeprefix("/") for command in KNOWN_CHANNEL_COMMANDS}
                assert registered_commands == expected_commands | {"start"}
                message_filters = {handler.filter_expr.expr for handler in fake_app.handlers if handler.kind == "message"}
                assert {"TEXT&COMMAND", "TEXT&~COMMAND"} <= message_filters
            finally:
                await ch.stop()

        _run(go())

    def test_retries_on_failure_then_succeeds(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})

            mock_app = MagicMock()
            mock_bot = AsyncMock()
            call_count = 0

            async def send_message(**kwargs):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ConnectionError("network error")
                result = MagicMock()
                result.message_id = 999
                return result

            mock_bot.send_message = send_message
            mock_app.bot = mock_bot
            ch._application = mock_app

            msg = OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="hello")
            await ch.send(msg)
            assert call_count == 3

        _run(go())

    def test_raises_after_all_retries_exhausted(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})

            mock_app = MagicMock()
            mock_bot = AsyncMock()
            mock_bot.send_message = AsyncMock(side_effect=ConnectionError("fail"))
            mock_app.bot = mock_bot
            ch._application = mock_app

            msg = OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="hello")
            with pytest.raises(ConnectionError):
                await ch.send(msg)

            assert mock_bot.send_message.call_count == 3

        _run(go())

    def test_raises_runtime_error_when_no_attempts_configured(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._application = MagicMock()

            msg = OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="hello")
            with pytest.raises(RuntimeError, match="without an exception"):
                await ch.send(msg, _max_retries=0)

        _run(go())


class TestFeishuSendRetry:
    def test_raises_runtime_error_when_no_attempts_configured(self):
        from app.channels.feishu import FeishuChannel

        async def go():
            bus = MessageBus()
            ch = FeishuChannel(bus=bus, config={"app_id": "id", "app_secret": "secret"})
            ch._api_client = MagicMock()

            msg = OutboundMessage(channel_name="feishu", chat_id="chat", thread_id="t1", text="hello")
            with pytest.raises(RuntimeError, match="without an exception"):
                await ch.send(msg, _max_retries=0)

        _run(go())


# ---------------------------------------------------------------------------
# Telegram private-chat thread context tests
# ---------------------------------------------------------------------------


def _make_telegram_update(chat_type: str, message_id: int, *, reply_to_message_id: int | None = None, text: str = "hello"):
    """Build a minimal mock telegram Update for testing _on_text / _cmd_generic."""
    update = MagicMock()
    update.effective_chat.type = chat_type
    update.effective_chat.id = 100
    update.effective_user.id = 42
    update.message.text = text
    update.message.message_id = message_id
    if reply_to_message_id is not None:
        reply_msg = MagicMock()
        reply_msg.message_id = reply_to_message_id
        update.message.reply_to_message = reply_msg
    else:
        update.message.reply_to_message = None
    return update


class TestTelegramPrivateChatThread:
    """Verify that private chats use topic_id=None (single thread per chat)."""

    def test_private_chat_no_reply_uses_none_topic(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update("private", message_id=10)
            await ch._on_text(update, None)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.topic_id is None

        _run(go())

    def test_private_chat_slash_skill_text_routes_as_chat(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update("private", message_id=12, text="/data-analysis analyze uploads/foo.csv")
            await ch._on_text(update, None)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.text == "/data-analysis analyze uploads/foo.csv"
            assert msg.msg_type == InboundMessageType.CHAT
            assert msg.topic_id is None

        _run(go())

    def test_slash_skill_addressed_to_telegram_bot_strips_username(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update(
                "group",
                message_id=13,
                text="/data-analysis@DeerFlowBot analyze uploads/foo.csv",
            )
            context = SimpleNamespace(bot=SimpleNamespace(username="DeerFlowBot"))
            await ch._on_text(update, context)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.text == "/data-analysis analyze uploads/foo.csv"
            assert msg.msg_type == InboundMessageType.CHAT
            assert msg.topic_id == "13"

        _run(go())

    def test_private_chat_with_reply_still_uses_none_topic(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update("private", message_id=11, reply_to_message_id=5)
            await ch._on_text(update, None)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.topic_id is None

        _run(go())

    def test_group_chat_no_reply_uses_msg_id_as_topic(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update("group", message_id=20)
            await ch._on_text(update, None)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.topic_id == "20"

        _run(go())

    def test_group_chat_reply_uses_reply_msg_id_as_topic(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update("group", message_id=21, reply_to_message_id=15)
            await ch._on_text(update, None)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.topic_id == "15"

        _run(go())

    def test_supergroup_chat_uses_msg_id_as_topic(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update("supergroup", message_id=25)
            await ch._on_text(update, None)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.topic_id == "25"

        _run(go())

    def test_cmd_generic_private_chat_uses_none_topic(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update("private", message_id=30, text="/help")
            await ch._cmd_generic(update, None)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.topic_id is None
            assert msg.msg_type == InboundMessageType.COMMAND

        _run(go())

    def test_cmd_generic_group_chat_uses_msg_id_as_topic(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update("group", message_id=31, text="/models")
            await ch._cmd_generic(update, None)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.topic_id == "31"
            assert msg.msg_type == InboundMessageType.COMMAND

        _run(go())

    def test_cmd_generic_group_chat_reply_uses_reply_msg_id_as_topic(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update("group", message_id=32, reply_to_message_id=20, text="/help")
            await ch._cmd_generic(update, None)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.topic_id == "20"
            assert msg.msg_type == InboundMessageType.COMMAND

        _run(go())

    def test_cmd_generic_strips_addressed_telegram_bot_username(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
            ch._main_loop = asyncio.get_event_loop()

            update = _make_telegram_update("group", message_id=33, text="/models@DeerFlowBot")
            context = SimpleNamespace(bot=SimpleNamespace(username="DeerFlowBot"))
            await ch._cmd_generic(update, context)

            msg = await asyncio.wait_for(bus.get_inbound(), timeout=2)
            assert msg.text == "/models"
            assert msg.topic_id == "33"
            assert msg.msg_type == InboundMessageType.COMMAND

        _run(go())


class TestTelegramProcessingOrder:
    """Ensure 'working on it...' is sent before inbound is published."""

    def test_running_reply_sent_before_publish(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})

            ch._main_loop = asyncio.get_event_loop()

            order = []

            async def mock_send_running_reply(chat_id, msg_id):
                order.append("running_reply")

            async def mock_publish_inbound(inbound):
                order.append("publish_inbound")

            ch._send_running_reply = mock_send_running_reply
            ch.bus.publish_inbound = mock_publish_inbound

            await ch._process_incoming_with_reply(chat_id="chat1", msg_id=123, inbound=InboundMessage(channel_name="telegram", chat_id="chat1", user_id="user1", text="hello"))

            assert order == ["running_reply", "publish_inbound"]

        _run(go())


# ---------------------------------------------------------------------------
# Slack markdown-to-mrkdwn conversion tests (via markdown_to_mrkdwn library)
# ---------------------------------------------------------------------------


class TestSlackMarkdownConversion:
    """Verify that the SlackChannel.send() path applies mrkdwn conversion."""

    def test_bold_converted(self):
        from app.channels.slack import _slack_md_converter

        result = _slack_md_converter.convert("this is **bold** text")
        assert "*bold*" in result
        assert "**" not in result

    def test_link_converted(self):
        from app.channels.slack import _slack_md_converter

        result = _slack_md_converter.convert("[click](https://example.com)")
        assert "<https://example.com|click>" in result

    def test_heading_converted(self):
        from app.channels.slack import _slack_md_converter

        result = _slack_md_converter.convert("# Title")
        assert "*Title*" in result
        assert "#" not in result


# ---------------------------------------------------------------------------
# Telegram streaming tests
# ---------------------------------------------------------------------------


class TestTelegramStreaming:
    @staticmethod
    def _make_channel_with_bot():
        from app.channels.telegram import TelegramChannel

        bus = MessageBus()
        ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})

        mock_app = MagicMock()
        bot = SimpleNamespace()
        bot.sent = []
        bot.edited = []
        bot.next_message_id = 100

        async def send_message(**kwargs):
            bot.sent.append(kwargs)
            result = MagicMock()
            result.message_id = bot.next_message_id
            bot.next_message_id += 1
            return result

        async def edit_message_text(**kwargs):
            bot.edited.append(kwargs)
            result = MagicMock()
            result.message_id = kwargs["message_id"]
            return result

        bot.send_message = send_message
        bot.edit_message_text = edit_message_text
        mock_app.bot = bot
        ch._application = mock_app
        return ch, bot

    def test_stream_updates_edit_placeholder_in_place(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            await ch._send_running_reply("12345", 42)
            placeholder_id = ch._stream_messages["12345:42"]["message_id"]

            update1 = OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="Hello", is_final=False, thread_ts="42")
            await ch.send(update1)

            clock["now"] += 2.0
            update2 = OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="Hello world", is_final=False, thread_ts="42")
            await ch.send(update2)

            assert len(bot.sent) == 1  # only the placeholder
            assert [e["message_id"] for e in bot.edited] == [placeholder_id, placeholder_id]
            assert [e["text"] for e in bot.edited] == ["Hello", "Hello world"]

        _run(go())

    def test_stream_updates_throttled_within_interval(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            await ch._send_running_reply("12345", 42)

            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="a", is_final=False, thread_ts="42"))
            clock["now"] += 0.3  # within 1s window -> dropped
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="ab", is_final=False, thread_ts="42"))
            clock["now"] += 1.0  # past window -> edited
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="abc", is_final=False, thread_ts="42"))

            assert [e["text"] for e in bot.edited] == ["a", "abc"]

        _run(go())

    def test_stream_updates_in_group_chat_use_wider_throttle(self, monkeypatch):
        """Telegram groups (negative chat_id) are capped at 20 messages/minute,
        so group-chat stream edits throttle at 3s instead of 1s."""

        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            await ch._send_running_reply("-100123", 42)

            await ch.send(OutboundMessage(channel_name="telegram", chat_id="-100123", thread_id="t1", text="a", is_final=False, thread_ts="42"))
            clock["now"] += 1.2  # past the 1s private window, within the 3s group window -> dropped
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="-100123", thread_id="t1", text="ab", is_final=False, thread_ts="42"))
            clock["now"] += 2.0  # 3.2s since last edit -> edited
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="-100123", thread_id="t1", text="abc", is_final=False, thread_ts="42"))

            assert [e["text"] for e in bot.edited] == ["a", "abc"]

        _run(go())

    def test_stream_update_without_placeholder_sends_new_message(self):
        async def go():
            ch, bot = self._make_channel_with_bot()

            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="Hi", is_final=False, thread_ts="42"))

            assert len(bot.sent) == 1
            assert bot.sent[0]["text"] == "Hi"
            # Threads under the user's message that started this turn
            assert bot.sent[0]["reply_to_message_id"] == 42
            assert ch._stream_messages["12345:42"]["message_id"] == 100

        _run(go())

    def test_stream_edit_fallback_message_threads_under_user_message(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            await ch._send_running_reply("12345", 42)

            async def edit_gone(**kwargs):
                raise Exception("Bad Request: message to edit not found")

            bot.edit_message_text = edit_gone
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="Hi", is_final=False, thread_ts="42"))

            # Fallback message threads under the user's message and becomes the new stream target
            assert bot.sent[1]["text"] == "Hi"
            assert bot.sent[1]["reply_to_message_id"] == 42
            assert ch._stream_messages["12345:42"]["message_id"] == 101

        _run(go())

    def test_stream_message_registry_is_bounded(self):
        from app.channels.telegram import MAX_TRACKED_STREAM_MESSAGES

        async def go():
            ch, _bot = self._make_channel_with_bot()

            for i in range(MAX_TRACKED_STREAM_MESSAGES + 1):
                ch._register_stream_message(f"chat:{i}", message_id=i, last_text="x", last_edit_at=0.0)

            assert len(ch._stream_messages) == MAX_TRACKED_STREAM_MESSAGES
            assert "chat:0" not in ch._stream_messages  # oldest evicted
            assert f"chat:{MAX_TRACKED_STREAM_MESSAGES}" in ch._stream_messages

        _run(go())

    def test_stream_update_truncates_long_text(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            await ch._send_running_reply("12345", 42)
            long_text = "x" * 5000
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text=long_text, is_final=False, thread_ts="42"))

            assert len(bot.edited) == 1
            assert len(bot.edited[0]["text"]) == 4096
            assert bot.edited[0]["text"].endswith("…")

        _run(go())

    def test_stream_update_retry_after_is_dropped(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            await ch._send_running_reply("12345", 42)

            async def edit_rate_limited(**kwargs):
                exc = Exception("Flood control exceeded")
                exc.retry_after = 5
                raise exc

            bot.edit_message_text = edit_rate_limited
            # Must not raise, must not send a new message
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="Hi", is_final=False, thread_ts="42"))
            assert len(bot.sent) == 1  # placeholder only

        _run(go())

    def test_telegram_reports_streaming_support(self):
        from app.channels.manager import CHANNEL_CAPABILITIES
        from app.channels.telegram import TelegramChannel

        bus = MessageBus()
        ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})
        assert ch.supports_streaming is True
        assert CHANNEL_CAPABILITIES["telegram"]["supports_streaming"] is True

    def test_running_reply_registers_stream_placeholder(self):
        from app.channels.telegram import TelegramChannel

        async def go():
            bus = MessageBus()
            ch = TelegramChannel(bus=bus, config={"bot_token": "test-token"})

            mock_app = MagicMock()
            mock_bot = AsyncMock()
            sent = MagicMock()
            sent.message_id = 777
            mock_bot.send_message = AsyncMock(return_value=sent)
            mock_app.bot = mock_bot
            ch._application = mock_app

            await ch._send_running_reply("12345", 42)

            state = ch._stream_messages["12345:42"]
            assert state["message_id"] == 777
            assert state["last_edit_at"] == 0.0
            assert state["last_text"] == "Working on it..."
            mock_bot.send_message.assert_awaited_once_with(
                chat_id=12345,
                text="Working on it...",
                reply_to_message_id=42,
            )

        _run(go())

    def test_final_message_edits_stream_message_and_clears_state(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            await ch._send_running_reply("12345", 42)
            placeholder_id = ch._stream_messages["12345:42"]["message_id"]

            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="partial", is_final=False, thread_ts="42"))
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="full answer", is_final=True, thread_ts="42"))

            assert [e["text"] for e in bot.edited] == ["partial", "full answer"]
            assert len(bot.sent) == 1  # placeholder only — final edited, not re-sent
            assert "12345:42" not in ch._stream_messages
            assert ch._last_bot_message["12345"] == placeholder_id

        _run(go())

    def test_final_message_splits_long_text(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            await ch._send_running_reply("12345", 42)
            long_text = "a" * 4096 + "b" * 100

            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text=long_text, is_final=True, thread_ts="42"))

            assert len(bot.edited) == 1
            assert bot.edited[0]["text"] == "a" * 4096
            follow_ups = bot.sent[1:]  # bot.sent[0] is the placeholder
            assert [m["text"] for m in follow_ups] == ["b" * 100]
            # Fake bot assigns ids sequentially: placeholder=100, follow-up chunk=101
            assert ch._last_bot_message["12345"] == 101
            assert "12345:42" not in ch._stream_messages

        _run(go())

    def test_final_message_not_modified_error_is_ignored(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            await ch._send_running_reply("12345", 42)
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="done", is_final=False, thread_ts="42"))

            async def edit_not_modified(**kwargs):
                raise Exception("Bad Request: message is not modified")

            bot.edit_message_text = edit_not_modified
            # Same text again as final — skipped via the equal-text guard:
            # must not raise, must not send a new message
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="done", is_final=True, thread_ts="42"))

            assert len(bot.sent) == 1  # placeholder only
            assert "12345:42" not in ch._stream_messages

        _run(go())

    def test_final_edit_raising_not_modified_is_swallowed(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            await ch._send_running_reply("12345", 42)
            placeholder_id = ch._stream_messages["12345:42"]["message_id"]

            async def edit_not_modified(**kwargs):
                raise Exception("Bad Request: message is not modified")

            bot.edit_message_text = edit_not_modified
            # Final text differs from last_text, so the edit IS attempted and
            # raises not-modified — must be swallowed, no fallback send.
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="done", is_final=True, thread_ts="42"))

            assert len(bot.sent) == 1  # placeholder only
            assert "12345:42" not in ch._stream_messages
            assert ch._last_bot_message["12345"] == placeholder_id

        _run(go())

    def test_final_without_stream_state_sends_plain_message(self):
        async def go():
            ch, bot = self._make_channel_with_bot()

            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="direct", is_final=True, thread_ts=None))

            assert len(bot.sent) == 1
            assert bot.sent[0]["text"] == "direct"
            assert len(bot.edited) == 0

        _run(go())

    def test_final_edit_retries_once_after_rate_limit(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            sleeps = []

            async def fake_sleep(delay):
                sleeps.append(delay)

            monkeypatch.setattr("app.channels.telegram.asyncio.sleep", fake_sleep)

            await ch._send_running_reply("12345", 42)
            placeholder_id = ch._stream_messages["12345:42"]["message_id"]

            real_edit = bot.edit_message_text
            calls = {"n": 0}

            async def edit_flaky(**kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    exc = Exception("Flood control exceeded")
                    exc.retry_after = 3
                    raise exc
                return await real_edit(**kwargs)

            bot.edit_message_text = edit_flaky
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="final", is_final=True, thread_ts="42"))

            assert sleeps == [3.0]
            assert [e["text"] for e in bot.edited] == ["final"]
            assert len(bot.sent) == 1  # placeholder only
            assert ch._last_bot_message["12345"] == placeholder_id
            assert "12345:42" not in ch._stream_messages

        _run(go())

    def test_final_edit_double_rate_limit_falls_back_to_new_message(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            sleeps = []

            async def fake_sleep(delay):
                sleeps.append(delay)

            monkeypatch.setattr("app.channels.telegram.asyncio.sleep", fake_sleep)

            await ch._send_running_reply("12345", 42)

            async def edit_rate_limited(**kwargs):
                exc = Exception("Flood control exceeded")
                exc.retry_after = 2
                raise exc

            bot.edit_message_text = edit_rate_limited
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text="final", is_final=True, thread_ts="42"))

            # Fallback delivered the final text as a new message (after the placeholder)
            assert [m["text"] for m in bot.sent] == ["Working on it...", "final"]
            assert ch._last_bot_message["12345"] == 101
            assert "12345:42" not in ch._stream_messages

        _run(go())

    def test_final_overflow_chunk_send_is_retried(self, monkeypatch):
        async def go():
            ch, bot = self._make_channel_with_bot()

            clock = {"now": 1000.0}
            monkeypatch.setattr("app.channels.telegram._monotonic", lambda: clock["now"])

            sleeps = []

            async def fake_sleep(delay):
                sleeps.append(delay)

            monkeypatch.setattr("app.channels.telegram.asyncio.sleep", fake_sleep)

            await ch._send_running_reply("12345", 42)

            real_send = bot.send_message
            failures = {"left": 1}

            async def send_flaky(**kwargs):
                if failures["left"] > 0:
                    failures["left"] -= 1
                    raise ConnectionError("transient")
                return await real_send(**kwargs)

            bot.send_message = send_flaky
            long_text = "a" * 4096 + "b" * 10
            await ch.send(OutboundMessage(channel_name="telegram", chat_id="12345", thread_id="t1", text=long_text, is_final=True, thread_ts="42"))

            assert bot.edited[0]["text"] == "a" * 4096
            assert [m["text"] for m in bot.sent] == ["Working on it...", "b" * 10]
            assert ch._last_bot_message["12345"] == 101

        _run(go())
