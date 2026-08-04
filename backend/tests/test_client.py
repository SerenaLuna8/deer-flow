"""Tests for DeerFlowClient."""

import asyncio
import concurrent.futures
import json
import tempfile
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage  # noqa: F401

from app.gateway.routers.models import ModelResponse, ModelsListResponse
from deerflow.agents.thread_state import DeltaThreadState
from deerflow.assets.catalog import AssetCatalogUnavailable
from deerflow.client import DeerFlowClient
from deerflow.config.paths import Paths
from deerflow.uploads.manager import PathTraversalError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_app_config():
    """Provide a minimal AppConfig mock."""
    model = MagicMock()
    model.name = "test-model"
    model.model = "test-model"
    model.supports_thinking = False
    model.supports_reasoning_effort = False
    model.model_dump.return_value = {"name": "test-model", "use": "langchain_openai:ChatOpenAI"}

    config = MagicMock()
    config.models = [model]
    config.token_usage.enabled = False
    config.skills.deferred_discovery = False
    config.skills.container_path = "/mnt/skills"
    config.tool_search.enabled = False
    config.database.checkpoint_channel_mode = "full"
    config.database.checkpoint_delta.snapshot_frequency = 10
    return config


@pytest.fixture
def client(mock_app_config, tmp_path):
    """Create a DeerFlowClient with mocked config loading."""
    import deerflow.skills.storage as _storage_mod
    from deerflow.skills.storage.local_skill_storage import LocalSkillStorage

    _storage_mod._default_skill_storage = LocalSkillStorage(host_path=str(tmp_path))
    with patch("deerflow.client.get_app_config", return_value=mock_app_config):
        return DeerFlowClient()


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


class TestClientInit:
    def test_default_params(self, client):
        assert client._model_name is None
        assert client._thinking_enabled is True
        assert client._subagent_enabled is False
        assert client._plan_mode is False
        assert client._agent_name is None
        assert client._available_skills is None
        assert client._checkpointer is None
        assert client._agent is None

    def test_custom_params(self, mock_app_config):
        mock_middleware = MagicMock()
        with patch("deerflow.client.get_app_config", return_value=mock_app_config):
            c = DeerFlowClient(model_name="gpt-4", thinking_enabled=False, subagent_enabled=True, plan_mode=True, agent_name="test-agent", available_skills={"skill1", "skill2"}, middlewares=[mock_middleware])
        assert c._model_name == "gpt-4"
        assert c._thinking_enabled is False
        assert c._subagent_enabled is True
        assert c._plan_mode is True
        assert c._agent_name == "test-agent"
        assert c._available_skills == {"skill1", "skill2"}
        assert c._middlewares == [mock_middleware]

    def test_invalid_agent_name(self, mock_app_config):
        with patch("deerflow.client.get_app_config", return_value=mock_app_config):
            with pytest.raises(ValueError, match="Invalid agent name"):
                DeerFlowClient(agent_name="invalid name with spaces!")
            with pytest.raises(ValueError, match="Invalid agent name"):
                DeerFlowClient(agent_name="../path/traversal")

    def test_custom_config_path(self, mock_app_config):
        with (
            patch("deerflow.client.reload_app_config") as mock_reload,
            patch("deerflow.client.get_app_config", return_value=mock_app_config),
        ):
            DeerFlowClient(config_path="/tmp/custom.yaml")
            mock_reload.assert_called_once_with("/tmp/custom.yaml")

    def test_checkpointer_stored(self, mock_app_config):
        cp = MagicMock()
        with patch("deerflow.client.get_app_config", return_value=mock_app_config):
            c = DeerFlowClient(checkpointer=cp)
        assert c._checkpointer is cp

    def test_checkpoint_runtime_is_frozen_from_config(
        self,
        mock_app_config,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import deerflow.runtime.checkpoint_mode as checkpoint_mode

        monkeypatch.setattr(
            checkpoint_mode,
            "_frozen_checkpoint_channel_mode",
            None,
        )
        monkeypatch.setattr(
            checkpoint_mode,
            "_frozen_checkpoint_snapshot_frequency",
            None,
        )
        mock_app_config.database.checkpoint_channel_mode = "delta"
        mock_app_config.database.checkpoint_delta.snapshot_frequency = 7

        with patch(
            "deerflow.client.get_app_config",
            return_value=mock_app_config,
        ):
            configured = DeerFlowClient()

        assert configured._checkpoint_channel_mode == "delta"
        assert configured._checkpoint_snapshot_frequency == 7

        mock_app_config.database.checkpoint_channel_mode = "full"
        with (
            patch(
                "deerflow.client.get_app_config",
                return_value=mock_app_config,
            ),
            pytest.raises(
                checkpoint_mode.CheckpointModeReconfigurationError,
                match="restart",
            ),
        ):
            DeerFlowClient()


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


class TestConfigQueries:
    def test_list_models(self, client):
        result = client.list_models()
        assert "models" in result
        assert result["token_usage"] == {"enabled": False}
        assert len(result["models"]) == 1
        assert result["models"][0]["name"] == "test-model"
        # Verify Gateway-aligned fields are present
        assert "model" in result["models"][0]
        assert "display_name" in result["models"][0]
        assert "supports_thinking" in result["models"][0]


# ---------------------------------------------------------------------------
# stream / chat
# ---------------------------------------------------------------------------


def _make_agent_mock(chunks: list[dict]):
    """Create a mock agent whose .stream() yields the given chunks."""
    agent = MagicMock()
    agent.stream.return_value = iter(chunks)
    return agent


def _ai_events(events):
    """Filter messages-tuple events with type=ai and non-empty content."""
    return [e for e in events if e.type == "messages-tuple" and e.data.get("type") == "ai" and e.data.get("content")]


def _tool_call_events(events):
    """Filter messages-tuple events with type=ai and tool_calls."""
    return [e for e in events if e.type == "messages-tuple" and e.data.get("type") == "ai" and "tool_calls" in e.data]


def _tool_result_events(events):
    """Filter messages-tuple events with type=tool."""
    return [e for e in events if e.type == "messages-tuple" and e.data.get("type") == "tool"]


class TestStream:
    def test_basic_message(self, client):
        """stream() emits messages-tuple + values + end for a simple AI reply."""
        ai = AIMessage(content="Hello!", id="ai-1")
        chunks = [
            {"messages": [HumanMessage(content="hi", id="h-1")]},
            {"messages": [HumanMessage(content="hi", id="h-1"), ai]},
        ]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t1"))

        types = [e.type for e in events]
        assert "messages-tuple" in types
        assert "values" in types
        assert types[-1] == "end"
        msg_events = _ai_events(events)
        assert msg_events[0].data["content"] == "Hello!"

    def test_custom_events_are_forwarded(self, client):
        """stream() forwards custom stream events alongside normal values output."""
        ai = AIMessage(content="Hello!", id="ai-1")
        agent = MagicMock()
        agent.stream.return_value = iter(
            [
                ("custom", {"type": "task_started", "task_id": "task-1"}),
                ("values", {"messages": [HumanMessage(content="hi", id="h-1"), ai]}),
            ]
        )

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-custom"))

        agent.stream.assert_called_once()
        call_kwargs = agent.stream.call_args.kwargs
        # ``messages`` enables token-level streaming of AI text deltas;
        # see DeerFlowClient.stream() docstring and GitHub issue #1969.
        assert call_kwargs["stream_mode"] == ["values", "messages", "custom"]

        assert events[0].type == "custom"
        assert events[0].data == {"type": "task_started", "task_id": "task-1"}
        assert any(event.type == "messages-tuple" and event.data["content"] == "Hello!" for event in events)
        assert any(event.type == "values" for event in events)
        assert events[-1].type == "end"

    def test_context_propagation(self, client):
        """stream() passes agent_name to the context."""
        agent = _make_agent_mock([{"messages": [AIMessage(content="ok", id="ai-1")]}])

        client._agent_name = "test-agent-1"
        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            list(client.stream("hi", thread_id="t1"))

        # Verify context passed to agent.stream
        agent.stream.assert_called_once()
        call_kwargs = agent.stream.call_args.kwargs
        assert call_kwargs["context"]["thread_id"] == "t1"
        assert call_kwargs["context"]["agent_name"] == "test-agent-1"

    def test_custom_mode_is_normalized_to_string(self, client):
        """stream() forwards custom events even when the mode is not a plain string."""

        class StreamMode(Enum):
            CUSTOM = "custom"

            def __str__(self):
                return self.value

        agent = _make_agent_mock(
            [
                (StreamMode.CUSTOM, {"type": "task_started", "task_id": "task-1"}),
                {"messages": [AIMessage(content="Hello!", id="ai-1")]},
            ]
        )

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-custom-enum"))

        assert events[0].type == "custom"
        assert events[0].data == {"type": "task_started", "task_id": "task-1"}
        assert any(event.type == "messages-tuple" and event.data["content"] == "Hello!" for event in events)
        assert events[-1].type == "end"

    def test_tool_call_and_result(self, client):
        """stream() emits messages-tuple events for tool calls and results."""
        ai = AIMessage(content="", id="ai-1", tool_calls=[{"name": "bash", "args": {"cmd": "ls"}, "id": "tc-1"}])
        tool = ToolMessage(content="file.txt", id="tm-1", tool_call_id="tc-1", name="bash")
        ai2 = AIMessage(content="Here are the files.", id="ai-2")

        chunks = [
            {"messages": [HumanMessage(content="list files", id="h-1"), ai]},
            {"messages": [HumanMessage(content="list files", id="h-1"), ai, tool]},
            {"messages": [HumanMessage(content="list files", id="h-1"), ai, tool, ai2]},
        ]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("list files", thread_id="t2"))

        assert len(_tool_call_events(events)) >= 1
        assert len(_tool_result_events(events)) >= 1
        assert len(_ai_events(events)) >= 1
        assert events[-1].type == "end"

    def test_values_event_with_title(self, client):
        """stream() emits values event containing title when present in state."""
        ai = AIMessage(content="ok", id="ai-1")
        chunks = [
            {"messages": [HumanMessage(content="hi", id="h-1"), ai], "title": "Greeting"},
        ]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t3"))

        values_events = [e for e in events if e.type == "values"]
        assert len(values_events) >= 1
        assert values_events[-1].data["title"] == "Greeting"
        assert "messages" in values_events[-1].data

    def test_deduplication(self, client):
        """Messages with the same id are not emitted twice."""
        ai = AIMessage(content="Hello!", id="ai-1")
        chunks = [
            {"messages": [HumanMessage(content="hi", id="h-1"), ai]},
            {"messages": [HumanMessage(content="hi", id="h-1"), ai]},  # duplicate
        ]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t4"))

        msg_events = _ai_events(events)
        assert len(msg_events) == 1

    def test_auto_thread_id(self, client):
        """stream() auto-generates a thread_id if not provided."""
        agent = _make_agent_mock([{"messages": [AIMessage(content="ok", id="ai-1")]}])

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi"))

        # Should not raise; end event proves it completed
        assert events[-1].type == "end"

    def test_full_client_rejects_delta_thread_before_agent_invocation(
        self,
        client,
    ):
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Overwrite

        from deerflow.runtime.checkpoint_mode import (
            CheckpointModeMismatchError,
        )
        from deerflow.runtime.checkpoint_state import (
            CheckpointStateAccessor,
            build_state_mutation_graph,
        )

        saver = InMemorySaver()
        config = {
            "configurable": {
                "thread_id": "client-delta-rejection",
                "checkpoint_ns": "",
            }
        }
        delta = CheckpointStateAccessor.bind(
            build_state_mutation_graph("delta_seed", "delta"),
            saver,
            mode="delta",
        )
        asyncio.run(
            delta.aupdate(
                config,
                {"messages": Overwrite([HumanMessage(content="delta", id="delta-message")])},
                as_node="delta_seed",
            )
        )
        checkpoint_count = len(list(saver.list(config)))
        client._checkpointer = saver
        client._checkpoint_channel_mode = "full"

        with (
            patch(
                "deerflow.client.is_trace_correlation_enabled",
                return_value=False,
            ),
            patch.object(client, "_ensure_agent") as ensure_agent,
            pytest.raises(
                CheckpointModeMismatchError,
                match="requires delta mode",
            ),
        ):
            list(
                client.stream(
                    "must not run",
                    thread_id="client-delta-rejection",
                )
            )

        ensure_agent.assert_not_called()
        assert len(list(saver.list(config))) == checkpoint_count

    def test_messages_mode_emits_token_deltas(self, client):
        """stream() forwards LangGraph ``messages`` mode chunks as delta events.

        Regression for bytedance/deer-flow#1969 — before the fix the client
        only subscribed to ``values`` mode, so LLM output was delivered as
        a single cumulative dump after each graph node finished instead of
        token-by-token deltas as the model generated them.
        """
        # Three AI chunks sharing the same id, followed by a terminal
        # values snapshot with the fully assembled message — this matches
        # the shape LangGraph emits when ``stream_mode`` includes both
        # ``messages`` and ``values``.
        assembled = AIMessage(content="Hel lo world!", id="ai-1", usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7})
        agent = MagicMock()
        agent.stream.return_value = iter(
            [
                ("messages", (AIMessageChunk(content="Hel", id="ai-1"), {})),
                ("messages", (AIMessageChunk(content=" lo ", id="ai-1"), {})),
                (
                    "messages",
                    (
                        AIMessageChunk(
                            content="world!",
                            id="ai-1",
                            usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
                        ),
                        {},
                    ),
                ),
                ("values", {"messages": [HumanMessage(content="hi", id="h-1"), assembled]}),
            ]
        )

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-stream"))

        # Three delta messages-tuple events, all with the same id, each
        # carrying only its own delta (not cumulative).
        ai_text_events = [e for e in events if e.type == "messages-tuple" and e.data.get("type") == "ai" and e.data.get("content")]
        assert [e.data["content"] for e in ai_text_events] == ["Hel", " lo ", "world!"]
        assert all(e.data["id"] == "ai-1" for e in ai_text_events)

        # The values snapshot MUST NOT re-synthesize an AI text event for
        # the already-streamed id (otherwise consumers see duplicated text).
        assert len(ai_text_events) == 3

        # Usage metadata attached only to the chunk that actually carried
        # it, and counted into cumulative usage exactly once (the values
        # snapshot's duplicate usage on the assembled AIMessage must not
        # be double-counted).
        events_with_usage = [e for e in ai_text_events if "usage_metadata" in e.data]
        assert len(events_with_usage) == 1
        assert events_with_usage[0].data["usage_metadata"] == {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
        end_event = events[-1]
        assert end_event.type == "end"
        assert end_event.data["usage"] == {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}

        # The values snapshot itself is still emitted.
        assert any(e.type == "values" for e in events)

        # stream_mode includes ``messages`` — the whole point of this fix.
        call_kwargs = agent.stream.call_args.kwargs
        assert "messages" in call_kwargs["stream_mode"]

    def test_stream_emits_additional_kwargs_updates_for_streamed_ai_messages(self, client):
        """stream() emits a follow-up AI event when attribution metadata arrives via values."""
        assembled = AIMessage(
            content="Hello!",
            id="ai-1",
            additional_kwargs={
                "token_usage_attribution": {
                    "version": 1,
                    "kind": "final_answer",
                    "shared_attribution": False,
                    "actions": [],
                }
            },
        )
        agent = MagicMock()
        agent.stream.return_value = iter(
            [
                ("messages", (AIMessageChunk(content="Hello!", id="ai-1"), {})),
                ("values", {"messages": [HumanMessage(content="hi", id="h-1"), assembled]}),
            ]
        )

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-stream-kwargs"))

        ai_events = [event for event in events if event.type == "messages-tuple" and event.data.get("type") == "ai" and event.data.get("id") == "ai-1"]
        assert any(event.data.get("content") == "Hello!" for event in ai_events)
        assert any(event.data.get("additional_kwargs", {}).get("token_usage_attribution", {}).get("kind") == "final_answer" for event in ai_events)

    def test_stream_emits_new_additional_kwargs_after_prior_metadata(self, client):
        """stream() emits later attribution metadata even after earlier kwargs for the same id."""
        attribution = {
            "version": 1,
            "kind": "final_answer",
            "shared_attribution": False,
            "actions": [],
        }
        assembled = AIMessage(
            content="Hello!",
            id="ai-1",
            additional_kwargs={
                "reasoning_content": "Thinking first.",
                "token_usage_attribution": attribution,
            },
        )
        agent = MagicMock()
        agent.stream.return_value = iter(
            [
                (
                    "messages",
                    (
                        AIMessageChunk(
                            content="Hello!",
                            id="ai-1",
                            additional_kwargs={"reasoning_content": "Thinking first."},
                        ),
                        {},
                    ),
                ),
                ("values", {"messages": [HumanMessage(content="hi", id="h-1"), assembled]}),
            ]
        )

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-stream-kwargs-delta"))

        ai_events = [event for event in events if event.type == "messages-tuple" and event.data.get("type") == "ai" and event.data.get("id") == "ai-1"]
        metadata_events = [event for event in ai_events if event.data.get("additional_kwargs")]

        assert metadata_events[0].data["additional_kwargs"] == {"reasoning_content": "Thinking first."}
        assert metadata_events[1].data["content"] == ""
        assert metadata_events[1].data["additional_kwargs"] == {"token_usage_attribution": attribution}

    def test_chat_accumulates_streamed_deltas(self, client):
        """chat() concatenates per-id deltas from messages mode."""
        agent = MagicMock()
        agent.stream.return_value = iter(
            [
                ("messages", (AIMessageChunk(content="Hel", id="ai-1"), {})),
                ("messages", (AIMessageChunk(content="lo ", id="ai-1"), {})),
                ("messages", (AIMessageChunk(content="world!", id="ai-1"), {})),
                ("values", {"messages": [HumanMessage(content="hi", id="h-1"), AIMessage(content="Hello world!", id="ai-1")]}),
            ]
        )

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            result = client.chat("hi", thread_id="t-chat-stream")

        assert result == "Hello world!"

    def test_messages_mode_tool_message(self, client):
        """stream() forwards ToolMessage chunks from messages mode."""
        agent = MagicMock()
        agent.stream.return_value = iter(
            [
                (
                    "messages",
                    (
                        ToolMessage(content="file.txt", id="tm-1", tool_call_id="tc-1", name="bash"),
                        {},
                    ),
                ),
                ("values", {"messages": [HumanMessage(content="ls", id="h-1"), ToolMessage(content="file.txt", id="tm-1", tool_call_id="tc-1", name="bash")]}),
            ]
        )

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("ls", thread_id="t-tool-stream"))

        tool_events = [e for e in events if e.type == "messages-tuple" and e.data.get("type") == "tool"]
        # The tool result must be delivered exactly once (from messages
        # mode), not duplicated by the values-snapshot synthesis path.
        assert len(tool_events) == 1
        assert tool_events[0].data["content"] == "file.txt"
        assert tool_events[0].data["name"] == "bash"
        assert tool_events[0].data["tool_call_id"] == "tc-1"

    def test_list_content_blocks(self, client):
        """stream() handles AIMessage with list-of-blocks content."""
        ai = AIMessage(
            content=[
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "result"},
            ],
            id="ai-1",
        )
        chunks = [{"messages": [ai]}]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t5"))

        msg_events = _ai_events(events)
        assert len(msg_events) == 1
        assert msg_events[0].data["content"] == "result"

    # ------------------------------------------------------------------
    # Refactor regression guards (PR #1974 follow-up safety)
    #
    # The three tests below are not bug-fix tests — they exist to lock
    # the *exact* contract of stream() so a future refactor (e.g. moving
    # to ``agent.astream()``, sharing a core with Gateway's run_agent,
    # changing the dedup strategy) cannot silently change behavior.
    # ------------------------------------------------------------------

    def test_dedup_requires_messages_before_values_invariant(self, client):
        """Canary: locks the order-dependence of cross-mode dedup.

        ``streamed_ids`` is populated only by the ``messages`` branch.
        If a ``values`` snapshot arrives BEFORE its corresponding
        ``messages`` chunks for the same id, the values path falls
        through and synthesizes its own AI text event, then the
        messages chunk emits another delta — consumers see the same
        id twice.

        Under normal LangGraph operation this never happens (messages
        chunks are emitted during LLM streaming, the values snapshot
        after the node completes), so the implicit invariant is safe
        in production.  This test exists as a tripwire for refactors
        that switch to ``agent.astream()`` or share a core with
        Gateway: if the ordering ever changes, this test fails and
        forces the refactor to either (a) preserve the ordering or
        (b) deliberately re-baseline to a stronger order-independent
        dedup contract — and document the new contract here.
        """
        agent = MagicMock()
        agent.stream.return_value = iter(
            [
                # values arrives FIRST — streamed_ids still empty.
                ("values", {"messages": [HumanMessage(content="hi", id="h-1"), AIMessage(content="Hello", id="ai-1")]}),
                # messages chunk for the same id arrives SECOND.
                ("messages", (AIMessageChunk(content="Hello", id="ai-1"), {})),
            ]
        )

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-order-canary"))

        ai_text_events = [e for e in events if e.type == "messages-tuple" and e.data.get("type") == "ai" and e.data.get("content")]
        # Current behavior: 2 events (values synthesis + messages delta).
        # If a refactor makes dedup order-independent, this becomes 1 —
        # update the assertion AND the docstring above to record the
        # new contract, do not silently fix this number.
        assert len(ai_text_events) == 2
        assert all(e.data["id"] == "ai-1" for e in ai_text_events)
        assert [e.data["content"] for e in ai_text_events] == ["Hello", "Hello"]

    def test_messages_mode_golden_event_sequence(self, client):
        """Locks the **exact** event sequence for a canonical streaming turn.

        This is a strong regression guard: any future refactor that
        changes the order, type, or shape of emitted events fails this
        test with a clear list-equality diff, forcing either a
        preserved sequence or a deliberate re-baseline.

        Input shape:
            messages chunk 1 — text "Hel", no usage
            messages chunk 2 — text "lo",  with cumulative usage
            values snapshot  — assembled AIMessage with same usage

        Locked behavior:
            * Two messages-tuple AI text events (one per chunk), each
              carrying ONLY its own delta — not cumulative.
            * ``usage_metadata`` attached only to the chunk that
              delivered it (not the first chunk).
            * The values event is still emitted, but its embedded
              ``messages`` list is the *serialized* form — no
              synthesized messages-tuple events for the already-
              streamed id.
            * ``end`` event carries cumulative usage counted exactly
              once across both modes.
        """
        # Inline the usage literal at construction sites so Pyright can
        # narrow ``dict[str, int]`` to ``UsageMetadata`` (TypedDict
        # narrowing only works on literals, not on bound variables).
        # The local ``usage`` is reused only for assertion comparisons
        # below, where structural dict equality is sufficient.
        usage = {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
        agent = MagicMock()
        agent.stream.return_value = iter(
            [
                ("messages", (AIMessageChunk(content="Hel", id="ai-1"), {})),
                ("messages", (AIMessageChunk(content="lo", id="ai-1", usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}), {})),
                (
                    "values",
                    {
                        "messages": [
                            HumanMessage(content="hi", id="h-1"),
                            AIMessage(content="Hello", id="ai-1", usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}),
                        ]
                    },
                ),
            ]
        )

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-golden"))

        actual = [(e.type, e.data) for e in events]
        expected = [
            ("messages-tuple", {"type": "ai", "content": "Hel", "id": "ai-1"}),
            ("messages-tuple", {"type": "ai", "content": "lo", "id": "ai-1", "usage_metadata": usage}),
            (
                "values",
                {
                    "title": None,
                    "messages": [
                        {"type": "human", "content": "hi", "id": "h-1"},
                        {"type": "ai", "content": "Hello", "id": "ai-1", "usage_metadata": usage},
                    ],
                    "artifacts": [],
                },
            ),
            ("end", {"usage": usage}),
        ]
        assert actual == expected

    def test_chat_accumulates_in_linear_time(self, client):
        """``chat()`` must use a non-quadratic accumulation strategy.

        PR #1974 commit 2 replaced ``buffer = buffer + delta`` with
        ``list[str].append`` + ``"".join`` to fix an O(n²) regression
        introduced in commit 1.  This test guards against a future
        refactor accidentally restoring the quadratic path.

        Threshold rationale (10,000 single-char chunks, 1 second):
            * Current O(n) implementation: ~50-200 ms total, including
              all mock + event yield overhead.
            * O(n²) regression at n=10,000: chat accumulation alone
              becomes ~500 ms-2 s (50 M character copies), reliably
              over the bound on any reasonable CI.

        If this test ever flakes on slow CI, do NOT raise the threshold
        blindly — first confirm the implementation still uses
        ``"".join``, then consider whether the test should move to a
        benchmark suite that excludes mock overhead.
        """
        import time

        n = 10_000
        chunks: list = [("messages", (AIMessageChunk(content="x", id="ai-1"), {})) for _ in range(n)]
        chunks.append(
            (
                "values",
                {
                    "messages": [
                        HumanMessage(content="go", id="h-1"),
                        AIMessage(content="x" * n, id="ai-1"),
                    ]
                },
            )
        )
        agent = MagicMock()
        agent.stream.return_value = iter(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            start = time.monotonic()
            result = client.chat("go", thread_id="t-perf")
            elapsed = time.monotonic() - start

        assert result == "x" * n
        assert elapsed < 1.0, f"chat() took {elapsed:.3f}s for {n} chunks — possible O(n^2) regression (see PR #1974 commit 2 for the original fix)"

    def test_none_id_chunks_produce_duplicates_known_limitation(self, client):
        """Documents a known dedup limitation: ``messages`` chunks with ``id=None``.

        Some LLM providers (vLLM, certain custom backends) emit
        ``AIMessageChunk`` instances without an ``id``.  In that case
        the cross-mode dedup machinery cannot record the chunk in
        ``streamed_ids`` (the implementation guards on ``if msg_id``
        before adding), and a subsequent ``values`` snapshot whose
        reassembled ``AIMessage`` carries a real id will fall through
        the dedup check and synthesize a second AI text event for the
        same logical message — consumers see duplicated text.

        Why this is documented rather than fixed
        ----------------------------------------
        Falling back to ``metadata.get("id")`` does **not** help:
        LangGraph's messages-mode metadata never carries the message
        id (it carries ``langgraph_node`` / ``langgraph_step`` /
        ``checkpoint_ns`` / ``tags`` etc.).  Synthesizing a fallback
        like ``f"_synth_{id(msg_chunk)}"`` only helps if the values
        snapshot uses the same fallback, which it does not.  A real
        fix requires either provider cooperation (always emit chunk
        ids — out of scope for this PR) or content-based dedup (risks
        false positives for two distinct short messages with identical
        text).

        This test makes the limitation **explicit and discoverable**
        so a future contributor debugging "duplicate text in vLLM
        streaming" finds the answer immediately.  If a real fix lands,
        replace this test with a positive assertion that dedup works
        for the None-id case.

        See PR #1974 Copilot review comment on ``client.py:515``.
        """
        agent = MagicMock()
        agent.stream.return_value = iter(
            [
                # Realistic shape: chunk has no id (provider didn't set one),
                # values snapshot's reassembled AIMessage has a fresh id
                # assigned somewhere downstream (langgraph or middleware).
                ("messages", (AIMessageChunk(content="Hello", id=None), {})),
                (
                    "values",
                    {
                        "messages": [
                            HumanMessage(content="hi", id="h-1"),
                            AIMessage(content="Hello", id="ai-1"),
                        ]
                    },
                ),
            ]
        )

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-none-id-limitation"))

        ai_text_events = [e for e in events if e.type == "messages-tuple" and e.data.get("type") == "ai" and e.data.get("content")]
        # KNOWN LIMITATION: 2 events for the same logical message.
        #   1) from messages chunk (id=None, NOT added to streamed_ids
        #      because of ``if msg_id:`` guard at client.py line ~522)
        #   2) from values-snapshot synthesis (ai-1 not in streamed_ids,
        #      so the skip-branch at line ~549 doesn't trigger)
        # If this becomes 1, someone fixed the limitation — update this
        # test to a positive assertion and document the fix.
        assert len(ai_text_events) == 2
        assert ai_text_events[0].data["id"] is None
        assert ai_text_events[1].data["id"] == "ai-1"
        assert all(e.data["content"] == "Hello" for e in ai_text_events)


class TestChat:
    def test_returns_last_message(self, client):
        """chat() returns the last AI message text."""
        ai1 = AIMessage(content="thinking...", id="ai-1")
        ai2 = AIMessage(content="final answer", id="ai-2")
        chunks = [
            {"messages": [HumanMessage(content="q", id="h-1"), ai1]},
            {"messages": [HumanMessage(content="q", id="h-1"), ai1, ai2]},
        ]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            result = client.chat("q", thread_id="t6")

        assert result == "final answer"

    def test_empty_response(self, client):
        """chat() returns empty string if no AI message produced."""
        chunks = [{"messages": []}]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            result = client.chat("q", thread_id="t7")

        assert result == ""


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_list_text_blocks(self):
        content = [
            {"type": "text", "text": "first"},
            {"type": "thinking", "thinking": "skip"},
            {"type": "text", "text": "second"},
        ]
        assert DeerFlowClient._extract_text(content) == "first\nsecond"

    def test_list_plain_strings(self):
        assert DeerFlowClient._extract_text(["a", "b"]) == "a\nb"

    def test_other_type(self):
        assert DeerFlowClient._extract_text(42) == "42"


# ---------------------------------------------------------------------------
# _ensure_agent
# ---------------------------------------------------------------------------


class TestEnsureAgent:
    def test_creates_agent(self, client):
        """_ensure_agent creates an agent on first call."""
        mock_agent = MagicMock()
        config = client._get_runnable_config("t1")

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", return_value=mock_agent),
            patch("deerflow.client.build_middlewares", return_value=[]) as mock_build_middlewares,
            patch("deerflow.client.apply_prompt_template", return_value="prompt") as mock_apply_prompt,
            patch.object(client, "_get_tools", return_value=[]),
            patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=MagicMock()),
        ):
            client._agent_name = "custom-agent"
            client._available_skills = {"test_skill"}
            client._ensure_agent(config)

        assert client._agent is mock_agent
        # Verify agent_name propagation
        mock_build_middlewares.assert_called_once()
        assert mock_build_middlewares.call_args.kwargs.get("agent_name") == "custom-agent"
        mock_apply_prompt.assert_called_once()
        assert mock_apply_prompt.call_args.kwargs.get("agent_name") == "custom-agent"
        assert mock_apply_prompt.call_args.kwargs.get("available_skills") == {"test_skill"}

    def test_delta_mode_uses_delta_state_schema(self, client):
        client._checkpoint_channel_mode = "delta"
        client._checkpoint_snapshot_frequency = 10
        config = client._get_runnable_config("t-delta")

        with (
            patch("deerflow.client.create_chat_model"),
            patch(
                "deerflow.client.create_agent",
                return_value=MagicMock(),
            ) as mock_create_agent,
            patch("deerflow.client.build_middlewares", return_value=[]),
            patch("deerflow.client.apply_prompt_template", return_value="prompt"),
            patch.object(client, "_get_tools", return_value=[]),
            patch(
                "deerflow.runtime.checkpointer.get_checkpointer",
                return_value=None,
            ),
        ):
            client._ensure_agent(config)

        assert mock_create_agent.call_args.kwargs["state_schema"] is DeltaThreadState

    def test_uses_explicit_checkpointer_when_available(self, client):
        mock_agent = MagicMock()
        mock_checkpointer = MagicMock()
        client._checkpointer = mock_checkpointer
        config = client._get_runnable_config("t1")

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", return_value=mock_agent) as mock_create_agent,
            patch("deerflow.client.build_middlewares", return_value=[]),
            patch("deerflow.client.apply_prompt_template", return_value="prompt"),
            patch.object(client, "_get_tools", return_value=[]),
        ):
            client._ensure_agent(config)

        assert mock_create_agent.call_args.kwargs["checkpointer"] is mock_checkpointer

    def test_injects_custom_middlewares(self, client):
        mock_agent = MagicMock()
        mock_custom_middleware = MagicMock()
        client._middlewares = [mock_custom_middleware]
        config = client._get_runnable_config("t1")

        mock_clarification = MagicMock()
        mock_clarification.__class__.__name__ = "ClarificationMiddleware"

        def fake_build_middlewares(*args, **kwargs):
            custom = kwargs.get("custom_middlewares") or []
            return [MagicMock()] + custom + [mock_clarification]

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", return_value=mock_agent) as mock_create_agent,
            patch("deerflow.client.build_middlewares", side_effect=fake_build_middlewares),
            patch("deerflow.client.apply_prompt_template", return_value="prompt"),
            patch.object(client, "_get_tools", return_value=[]),
            patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=MagicMock()),
        ):
            client._ensure_agent(config)

        called_middlewares = mock_create_agent.call_args.kwargs["middleware"]
        assert len(called_middlewares) == 3
        assert called_middlewares[-2] is mock_custom_middleware
        assert called_middlewares[-1] is mock_clarification

    def test_skips_default_checkpointer_when_unconfigured(self, client):
        mock_agent = MagicMock()
        config = client._get_runnable_config("t1")

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", return_value=mock_agent) as mock_create_agent,
            patch("deerflow.client.build_middlewares", return_value=[]),
            patch("deerflow.client.apply_prompt_template", return_value="prompt"),
            patch.object(client, "_get_tools", return_value=[]),
            patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=None),
        ):
            client._ensure_agent(config)

        assert "checkpointer" not in mock_create_agent.call_args.kwargs

    def test_reuses_agent_same_config(self, client):
        """_ensure_agent does not recreate if config key unchanged."""
        mock_agent = MagicMock()
        client._agent = mock_agent
        client._agent_config_key = (
            None,
            True,
            False,
            False,
            None,
            None,
            "full",
            10,
        )

        config = client._get_runnable_config("t1")
        client._ensure_agent(config)

        # Should still be the same mock — no recreation
        assert client._agent is mock_agent

    def test_deferred_skill_discovery_has_no_ambient_skills_when_enabled(self, client, mock_app_config):
        """Embedded runs have no admitted Skill snapshot and expose no names."""
        mock_app_config.skills.deferred_discovery = True
        mock_app_config.skills.container_path = "/mnt/skills"
        mock_app_config.tool_search.enabled = False
        client._app_config = mock_app_config
        config = client._get_runnable_config("t1")

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", return_value=MagicMock()),
            patch("deerflow.client.build_middlewares", return_value=[]),
            patch("deerflow.client.apply_prompt_template", return_value="prompt") as mock_apply_prompt,
            patch.object(client, "_get_tools", return_value=[]),
            patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=None),
        ):
            client._ensure_agent(config)

        assert mock_apply_prompt.call_args.kwargs.get("skill_names") is None

    def test_deferred_skill_discovery_not_wired_when_disabled(self, client, mock_app_config):
        """When discovery is disabled, no Skill names are exposed."""
        mock_app_config.skills.deferred_discovery = False
        mock_app_config.skills.container_path = "/mnt/skills"
        mock_app_config.tool_search.enabled = False
        client._app_config = mock_app_config
        config = client._get_runnable_config("t1")

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", return_value=MagicMock()),
            patch("deerflow.client.build_middlewares", return_value=[]),
            patch("deerflow.client.apply_prompt_template", return_value="prompt") as mock_apply_prompt,
            patch.object(client, "_get_tools", return_value=[]),
            patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=None),
        ):
            client._ensure_agent(config)

        skill_names_arg = mock_apply_prompt.call_args.kwargs.get("skill_names")
        assert skill_names_arg is None, "skill_names must be None when deferred_discovery=False"

    def test_mcp_routing_middleware_wired_when_tool_search_enabled(self, client, mock_app_config):
        """Embedded client builds McpRoutingMiddleware from routed deferred MCP tools.

        RFC §10.3/§12.5 requires verifying the actual embedded-client builder path
        rather than assuming it inherits lead-agent behavior. Exercises the real
        assemble_deferred_tools + build_mcp_routing_middleware wiring and asserts a
        genuine McpRoutingMiddleware reaches build_middlewares.
        """
        from langchain_core.tools import tool as as_tool

        from deerflow.agents.middlewares.mcp_routing_middleware import McpRoutingMiddleware
        from deerflow.tools.mcp_metadata import tag_mcp_routing, tag_mcp_tool

        @as_tool
        def postgres_query(sql: str) -> str:
            "Query Postgres."
            return sql

        tag_mcp_tool(postgres_query)
        tag_mcp_routing(postgres_query, {"mode": "prefer", "priority": 100, "keywords": ["orders"]})

        mock_app_config.tool_search.enabled = True
        mock_app_config.tool_search.auto_promote_top_k = 3
        mock_app_config.skills.deferred_discovery = False
        client._app_config = mock_app_config
        config = client._get_runnable_config("t1")

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", return_value=MagicMock()),
            patch("deerflow.client.build_middlewares", return_value=[]) as mock_build_middlewares,
            patch("deerflow.client.apply_prompt_template", return_value="prompt"),
            patch.object(client, "_get_tools", return_value=[postgres_query]),
            patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=None),
        ):
            client._ensure_agent(config)

        routing_arg = mock_build_middlewares.call_args.kwargs.get("mcp_routing_middleware")
        assert isinstance(routing_arg, McpRoutingMiddleware)
        assert routing_arg._matched_names({"messages": [HumanMessage(content="show orders")]}) == ["postgres_query"]

    def test_mcp_routing_middleware_absent_when_tool_search_disabled(self, client, mock_app_config):
        """No routing middleware is built on the embedded path when tool_search is off."""
        mock_app_config.tool_search.enabled = False
        mock_app_config.skills.deferred_discovery = False
        client._app_config = mock_app_config
        config = client._get_runnable_config("t1")

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", return_value=MagicMock()),
            patch("deerflow.client.build_middlewares", return_value=[]) as mock_build_middlewares,
            patch("deerflow.client.apply_prompt_template", return_value="prompt"),
            patch.object(client, "_get_tools", return_value=[]),
            patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=None),
        ):
            client._ensure_agent(config)

        assert mock_build_middlewares.call_args.kwargs.get("mcp_routing_middleware") is None


# ---------------------------------------------------------------------------
# get_model
# ---------------------------------------------------------------------------


class TestGetModel:
    def test_found(self, client):
        model_cfg = MagicMock()
        model_cfg.name = "test-model"
        model_cfg.model = "test-model"
        model_cfg.display_name = "Test Model"
        model_cfg.description = "A test model"
        model_cfg.supports_thinking = True
        model_cfg.supports_reasoning_effort = True
        client._app_config.get_model_config.return_value = model_cfg

        result = client.get_model("test-model")
        assert result == {
            "name": "test-model",
            "model": "test-model",
            "display_name": "Test Model",
            "description": "A test model",
            "supports_thinking": True,
            "supports_reasoning_effort": True,
            "supports_vision": False,
            "is_default": True,
        }

    def test_not_found(self, client):
        client._app_config.get_model_config.return_value = None
        assert client.get_model("nonexistent") is None


# ---------------------------------------------------------------------------
# Thread Queries (list_threads / get_thread)
# ---------------------------------------------------------------------------


class TestThreadQueries:
    def _make_mock_checkpoint_tuple(
        self,
        thread_id: str,
        checkpoint_id: str,
        ts: str,
        title: str | None = None,
        parent_id: str | None = None,
        messages: list = None,
        pending_writes: list = None,
    ):
        cp = MagicMock()
        cp.config = {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}

        channel_values = {}
        if title is not None:
            channel_values["title"] = title
        if messages is not None:
            channel_values["messages"] = messages

        cp.checkpoint = {"ts": ts, "channel_values": channel_values}
        cp.metadata = {"source": "test"}

        if parent_id:
            cp.parent_config = {"configurable": {"thread_id": thread_id, "checkpoint_id": parent_id}}
        else:
            cp.parent_config = {}

        cp.pending_writes = pending_writes or []
        return cp

    def test_list_threads_empty(self, client):
        mock_checkpointer = MagicMock()
        mock_checkpointer.list.return_value = []
        client._checkpointer = mock_checkpointer

        result = client.list_threads()
        assert result == {"thread_list": []}
        mock_checkpointer.list.assert_called_once_with(config=None, limit=10)

    def test_list_threads_basic(self, client):
        mock_checkpointer = MagicMock()
        client._checkpointer = mock_checkpointer

        cp1 = self._make_mock_checkpoint_tuple("t1", "c1", "2023-01-01T10:00:00Z", title="Thread 1")
        cp2 = self._make_mock_checkpoint_tuple("t1", "c2", "2023-01-01T10:05:00Z", title="Thread 1 Updated")
        cp3 = self._make_mock_checkpoint_tuple("t2", "c3", "2023-01-02T10:00:00Z", title="Thread 2")
        cp_empty = self._make_mock_checkpoint_tuple("", "c4", "2023-01-03T10:00:00Z", title="Thread Empty")

        # Mock list returns out of order to test the timestamp sorting/comparison
        # Also includes a checkpoint with an empty thread_id which should be skipped
        mock_checkpointer.list.return_value = [cp2, cp1, cp_empty, cp3]

        result = client.list_threads(limit=5)
        mock_checkpointer.list.assert_called_once_with(config=None, limit=5)

        threads = result["thread_list"]
        assert len(threads) == 2

        # t2 should be first because its created_at (2023-01-02) is newer than t1 (2023-01-01)
        assert threads[0]["thread_id"] == "t2"
        assert threads[0]["created_at"] == "2023-01-02T10:00:00Z"
        assert threads[0]["title"] == "Thread 2"

        assert threads[1]["thread_id"] == "t1"
        assert threads[1]["created_at"] == "2023-01-01T10:00:00Z"
        assert threads[1]["updated_at"] == "2023-01-01T10:05:00Z"
        assert threads[1]["latest_checkpoint_id"] == "c2"
        assert threads[1]["title"] == "Thread 1 Updated"

    def test_list_threads_without_explicit_checkpointer_fails_closed(self, client):
        with pytest.raises(AssetCatalogUnavailable, match="explicitly scoped checkpointer"):
            client.list_threads()

    def test_get_thread(self, client):
        mock_checkpointer = MagicMock()
        client._checkpointer = mock_checkpointer
        client._agent = MagicMock()

        msg1 = HumanMessage(content="Hello", id="m1")
        msg2 = AIMessage(content="Hi there", id="m2")

        cp1 = self._make_mock_checkpoint_tuple("t1", "c1", "2023-01-01T10:00:00Z", messages=[msg1])
        cp2 = self._make_mock_checkpoint_tuple("t1", "c2", "2023-01-01T10:01:00Z", parent_id="c1", messages=[msg1, msg2], pending_writes=[("task_1", "messages", {"text": "pending"})])
        cp3_no_ts = self._make_mock_checkpoint_tuple("t1", "c3", None)

        snapshots = [
            SimpleNamespace(
                values=cp.checkpoint.get("channel_values", {}),
                config=cp.config,
                parent_config=cp.parent_config,
                metadata=cp.metadata,
                created_at=cp.checkpoint.get("ts"),
            )
            for cp in (cp2, cp1, cp3_no_ts)
        ]
        mock_checkpointer.list.return_value = [cp2, cp1, cp3_no_ts]
        accessor = MagicMock()
        accessor.history.return_value = snapshots

        with (
            patch.object(client, "_ensure_agent"),
            patch(
                "deerflow.client.CheckpointStateAccessor.bind",
                return_value=accessor,
            ),
        ):
            result = client.get_thread("t1")

        assert result["thread_id"] == "t1"
        checkpoints = result["checkpoints"]
        assert len(checkpoints) == 3

        # None timestamp remains None but is sorted first via a fallback key
        assert checkpoints[0]["checkpoint_id"] == "c3"
        assert checkpoints[0]["ts"] is None

        # Should be sorted by timestamp globally
        assert checkpoints[1]["checkpoint_id"] == "c1"
        assert checkpoints[1]["ts"] == "2023-01-01T10:00:00Z"
        assert len(checkpoints[1]["values"]["messages"]) == 1

        assert checkpoints[2]["checkpoint_id"] == "c2"
        assert checkpoints[2]["parent_checkpoint_id"] == "c1"
        assert checkpoints[2]["ts"] == "2023-01-01T10:01:00Z"
        assert len(checkpoints[2]["values"]["messages"]) == 2
        # Verify message serialization
        assert checkpoints[2]["values"]["messages"][1]["content"] == "Hi there"

        # Verify pending writes
        assert len(checkpoints[2]["pending_writes"]) == 1
        assert checkpoints[2]["pending_writes"][0]["task_id"] == "task_1"
        assert checkpoints[2]["pending_writes"][0]["channel"] == "messages"

    def test_get_thread_materializes_delta_history(self, client):
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Overwrite

        from deerflow.runtime.checkpoint_state import (
            CheckpointStateAccessor,
            build_state_mutation_graph,
        )

        saver = InMemorySaver()
        graph = build_state_mutation_graph(
            "client_delta",
            "delta",
            snapshot_frequency=10,
        )
        accessor = CheckpointStateAccessor.bind(
            graph,
            saver,
            mode="delta",
        )
        config = {
            "configurable": {
                "thread_id": "client-delta-thread",
                "checkpoint_ns": "",
            }
        }

        async def seed_delta_history():
            await accessor.aupdate(
                config,
                {"messages": Overwrite([HumanMessage(content="first", id="delta-h1")])},
                as_node="client_delta",
            )
            await accessor.aupdate(
                config,
                {"messages": [AIMessage(content="second", id="delta-a1")]},
                as_node="client_delta",
            )

        asyncio.run(seed_delta_history())
        raw_latest = saver.get_tuple(config)
        assert raw_latest is not None
        assert "messages" not in raw_latest.checkpoint["channel_values"]

        client._checkpointer = saver
        client._checkpoint_channel_mode = "delta"
        client._checkpoint_snapshot_frequency = 10
        client._agent = graph
        client._ensure_agent = MagicMock()

        result = client.get_thread("client-delta-thread")

        latest_messages = result["checkpoints"][-1]["values"]["messages"]
        assert [(message["id"], message["content"]) for message in latest_messages] == [
            ("delta-h1", "first"),
            ("delta-a1", "second"),
        ]

    def test_get_thread_without_explicit_checkpointer_fails_closed(self, client):
        with pytest.raises(AssetCatalogUnavailable, match="explicitly scoped checkpointer"):
            client.get_thread("t99")


# ---------------------------------------------------------------------------
# Goal management
# ---------------------------------------------------------------------------


class TestGoalManagement:
    def test_goal_round_trip_uses_checkpoint(self, client):
        from langgraph.checkpoint.memory import InMemorySaver

        client._checkpointer = InMemorySaver()

        set_result = client.set_goal("goal-thread", "finish all tests", max_continuations=3)
        get_result = client.get_goal("goal-thread")
        clear_result = client.clear_goal("goal-thread")
        after_clear = client.get_goal("goal-thread")

        assert set_result["goal"]["objective"] == "finish all tests"
        assert set_result["goal"]["max_continuations"] == 3
        assert get_result["goal"]["objective"] == "finish all tests"
        assert clear_result == {"goal": None}
        assert after_clear == {"goal": None}


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


class TestUploads:
    def test_embedded_upload_methods_document_virtual_path_only_contract(self):
        upload_doc = DeerFlowClient.upload_files.__doc__ or ""
        list_doc = DeerFlowClient.list_uploads.__doc__ or ""

        assert "UploadResponse" not in upload_doc
        assert "Gateway API" not in upload_doc
        assert "list_uploaded_files" not in list_doc
        assert "Gateway API" not in list_doc
        assert "virtual_path" in upload_doc
        assert "virtual_path" in list_doc

    def test_upload_files(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Create a source file
            src_file = tmp_path / "test.txt"
            src_file.write_text("hello")

            uploads_dir = tmp_path / "uploads"
            uploads_dir.mkdir()

            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                result = client.upload_files("thread-1", [src_file])

            assert result["success"] is True
            assert len(result["files"]) == 1
            assert result["files"][0]["filename"] == "test.txt"
            assert result["files"][0]["size"] == len("hello")
            assert "artifact_url" not in result["files"][0]
            assert result["files"][0]["virtual_path"] == "/mnt/user-data/uploads/test.txt"
            assert "message" in result
            assert (uploads_dir / "test.txt").exists()

    def test_upload_files_not_found(self, client):
        with pytest.raises(FileNotFoundError):
            client.upload_files("thread-1", ["/nonexistent/file.txt"])

    def test_upload_files_rejects_directory_path(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(ValueError, match="Path is not a file"):
                client.upload_files("thread-1", [tmp])

    def test_upload_files_reuses_single_executor_inside_event_loop(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            uploads_dir = tmp_path / "uploads"
            uploads_dir.mkdir()

            first = tmp_path / "first.pdf"
            second = tmp_path / "second.pdf"
            first.write_bytes(b"%PDF-1.4 first")
            second.write_bytes(b"%PDF-1.4 second")

            created_executors = []
            real_executor_cls = concurrent.futures.ThreadPoolExecutor

            async def fake_convert(path: Path) -> Path:
                md_path = path.with_suffix(".md")
                md_path.write_text(f"converted {path.name}")
                return md_path

            class FakeExecutor:
                def __init__(self, max_workers: int):
                    self.max_workers = max_workers
                    self.shutdown_calls = []
                    self._executor = real_executor_cls(max_workers=max_workers)
                    created_executors.append(self)

                def submit(self, fn, *args, **kwargs):
                    return self._executor.submit(fn, *args, **kwargs)

                def shutdown(self, wait: bool = True):
                    self.shutdown_calls.append(wait)
                    self._executor.shutdown(wait=wait)

            async def call_upload() -> dict:
                return client.upload_files("thread-async", [first, second])

            with (
                patch("deerflow.client.get_uploads_dir", return_value=uploads_dir),
                patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir),
                patch("deerflow.utils.file_conversion.CONVERTIBLE_EXTENSIONS", {".pdf"}),
                patch("deerflow.utils.file_conversion.convert_file_to_markdown", side_effect=fake_convert),
                patch("concurrent.futures.ThreadPoolExecutor", FakeExecutor),
            ):
                result = asyncio.run(call_upload())

            assert result["success"] is True
            assert len(result["files"]) == 2
            assert len(created_executors) == 1
            assert created_executors[0].max_workers == 1
            assert created_executors[0].shutdown_calls == [True]
            assert result["files"][0]["markdown_file"] == "first.md"
            assert result["files"][1]["markdown_file"] == "second.md"

    def test_list_uploads(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            uploads_dir = Path(tmp)
            (uploads_dir / "a.txt").write_text("a")
            (uploads_dir / "b.txt").write_text("bb")

            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                result = client.list_uploads("thread-1")

            assert result["count"] == 2
            assert len(result["files"]) == 2
            names = {f["filename"] for f in result["files"]}
            assert names == {"a.txt", "b.txt"}
            sizes = {f["filename"]: f["size"] for f in result["files"]}
            assert sizes == {"a.txt": 1, "b.txt": 2}
            # Downloads are exposed only through project-scoped file records.
            for f in result["files"]:
                assert "artifact_url" not in f
                assert f["virtual_path"].startswith("/mnt/user-data/uploads/")

    def test_delete_upload(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            uploads_dir = Path(tmp)
            (uploads_dir / "delete-me.txt").write_text("gone")

            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                result = client.delete_upload("thread-1", "delete-me.txt")

            assert result["success"] is True
            assert "delete-me.txt" in result["message"]
            assert not (uploads_dir / "delete-me.txt").exists()

    def test_delete_upload_not_found(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("deerflow.client.get_uploads_dir", return_value=Path(tmp)):
                with pytest.raises(FileNotFoundError):
                    client.delete_upload("thread-1", "nope.txt")

    def test_delete_upload_path_traversal(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            uploads_dir = Path(tmp)
            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                with pytest.raises(PathTraversalError):
                    client.delete_upload("thread-1", "../../etc/passwd")


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_get_artifact(self, client):
        from deerflow.runtime.user_context import get_effective_user_id

        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(base_dir=tmp)
            user_id = get_effective_user_id()
            outputs = paths.sandbox_outputs_dir("t1", user_id=user_id)
            outputs.mkdir(parents=True)
            (outputs / "result.txt").write_text("artifact content")

            with patch("deerflow.client.get_paths", return_value=paths):
                content, mime = client.get_artifact("t1", "mnt/user-data/outputs/result.txt")

            assert content == b"artifact content"
            assert "text" in mime

    def test_get_artifact_not_found(self, client):
        from deerflow.runtime.user_context import get_effective_user_id

        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(base_dir=tmp)
            user_id = get_effective_user_id()
            paths.sandbox_outputs_dir("t1", user_id=user_id).mkdir(parents=True)

            with patch("deerflow.client.get_paths", return_value=paths):
                with pytest.raises(FileNotFoundError):
                    client.get_artifact("t1", "mnt/user-data/outputs/nope.txt")

    def test_get_artifact_bad_prefix(self, client):
        with pytest.raises(ValueError, match="must start with"):
            client.get_artifact("t1", "bad/path/file.txt")

    def test_get_artifact_path_traversal(self, client):
        from deerflow.runtime.user_context import get_effective_user_id

        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(base_dir=tmp)
            user_id = get_effective_user_id()
            paths.sandbox_outputs_dir("t1", user_id=user_id).mkdir(parents=True)

            with patch("deerflow.client.get_paths", return_value=paths):
                with pytest.raises(PathTraversalError):
                    client.get_artifact("t1", "mnt/user-data/../../../etc/passwd")


# ===========================================================================
# Scenario-based integration tests
# ===========================================================================
# These tests simulate realistic user workflows end-to-end, exercising
# multiple methods in sequence to verify they compose correctly.


class TestScenarioMultiTurnConversation:
    """Scenario: User has a multi-turn conversation within a single thread."""

    def test_two_turn_conversation(self, client):
        """Two sequential chat() calls on the same thread_id produce
        independent results (without checkpointer, each call is stateless)."""
        ai1 = AIMessage(content="I'm a helpful assistant.", id="ai-1")
        ai2 = AIMessage(content="Python is great!", id="ai-2")

        agent = MagicMock()
        agent.stream.side_effect = [
            iter([{"messages": [HumanMessage(content="who are you?", id="h-1"), ai1]}]),
            iter([{"messages": [HumanMessage(content="what language?", id="h-2"), ai2]}]),
        ]

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            r1 = client.chat("who are you?", thread_id="thread-multi")
            r2 = client.chat("what language?", thread_id="thread-multi")

        assert r1 == "I'm a helpful assistant."
        assert r2 == "Python is great!"
        assert agent.stream.call_count == 2

    def test_stream_collects_all_event_types_across_turns(self, client):
        """A full turn emits messages-tuple (tool_call, tool_result, ai text) + values + end."""
        ai_tc = AIMessage(
            content="",
            id="ai-1",
            tool_calls=[
                {"name": "web_search", "args": {"query": "LangGraph"}, "id": "tc-1"},
            ],
        )
        tool_r = ToolMessage(content="LangGraph is a framework...", id="tm-1", tool_call_id="tc-1", name="web_search")
        ai_final = AIMessage(content="LangGraph is a framework for building agents.", id="ai-2")

        chunks = [
            {"messages": [HumanMessage(content="search", id="h-1"), ai_tc]},
            {"messages": [HumanMessage(content="search", id="h-1"), ai_tc, tool_r]},
            {"messages": [HumanMessage(content="search", id="h-1"), ai_tc, tool_r, ai_final], "title": "LangGraph Search"},
        ]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("search", thread_id="t-full"))

        # Verify expected event types
        types = set(e.type for e in events)
        assert types == {"messages-tuple", "values", "end"}
        assert events[-1].type == "end"

        # Verify tool_call data
        tc_events = _tool_call_events(events)
        assert len(tc_events) == 1
        assert tc_events[0].data["tool_calls"][0]["name"] == "web_search"
        assert tc_events[0].data["tool_calls"][0]["args"] == {"query": "LangGraph"}

        # Verify tool_result data
        tr_events = _tool_result_events(events)
        assert len(tr_events) == 1
        assert tr_events[0].data["tool_call_id"] == "tc-1"
        assert "LangGraph" in tr_events[0].data["content"]

        # Verify AI text
        msg_events = _ai_events(events)
        assert any("framework" in e.data["content"] for e in msg_events)

        # Verify values event contains title
        values_events = [e for e in events if e.type == "values"]
        assert any(e.data.get("title") == "LangGraph Search" for e in values_events)


class TestScenarioToolChain:
    """Scenario: Agent chains multiple tool calls in sequence."""

    def test_multi_tool_chain(self, client):
        """Agent calls bash → reads output → calls write_file → responds."""
        ai_bash = AIMessage(
            content="",
            id="ai-1",
            tool_calls=[
                {"name": "bash", "args": {"cmd": "ls /mnt/user-data/workspace"}, "id": "tc-1"},
            ],
        )
        bash_result = ToolMessage(content="README.md\nsrc/", id="tm-1", tool_call_id="tc-1", name="bash")
        ai_write = AIMessage(
            content="",
            id="ai-2",
            tool_calls=[
                {"name": "write_file", "args": {"path": "/mnt/user-data/outputs/listing.txt", "content": "README.md\nsrc/"}, "id": "tc-2"},
            ],
        )
        write_result = ToolMessage(content="File written successfully.", id="tm-2", tool_call_id="tc-2", name="write_file")
        ai_final = AIMessage(content="I listed the workspace and saved the output.", id="ai-3")

        chunks = [
            {"messages": [HumanMessage(content="list and save", id="h-1"), ai_bash]},
            {"messages": [HumanMessage(content="list and save", id="h-1"), ai_bash, bash_result]},
            {"messages": [HumanMessage(content="list and save", id="h-1"), ai_bash, bash_result, ai_write]},
            {"messages": [HumanMessage(content="list and save", id="h-1"), ai_bash, bash_result, ai_write, write_result]},
            {"messages": [HumanMessage(content="list and save", id="h-1"), ai_bash, bash_result, ai_write, write_result, ai_final]},
        ]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("list and save", thread_id="t-chain"))

        tool_calls = _tool_call_events(events)
        tool_results = _tool_result_events(events)
        messages = _ai_events(events)

        assert len(tool_calls) == 2
        assert tool_calls[0].data["tool_calls"][0]["name"] == "bash"
        assert tool_calls[1].data["tool_calls"][0]["name"] == "write_file"
        assert len(tool_results) == 2
        assert len(messages) == 1
        assert events[-1].type == "end"


class TestScenarioFileLifecycle:
    """Scenario: Upload files → list them → use in chat → download artifact."""

    def test_upload_list_delete_lifecycle(self, client):
        """Upload → list → verify → delete → list again."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            uploads_dir = tmp_path / "uploads"
            uploads_dir.mkdir()

            # Create source files
            (tmp_path / "report.txt").write_text("quarterly report data")
            (tmp_path / "data.csv").write_text("a,b,c\n1,2,3")

            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                # Step 1: Upload
                result = client.upload_files(
                    "t-lifecycle",
                    [
                        tmp_path / "report.txt",
                        tmp_path / "data.csv",
                    ],
                )
                assert result["success"] is True
                assert len(result["files"]) == 2
                assert {f["filename"] for f in result["files"]} == {"report.txt", "data.csv"}

                # Step 2: List
                listed = client.list_uploads("t-lifecycle")
                assert listed["count"] == 2
                assert all("virtual_path" in f for f in listed["files"])

                # Step 3: Delete one
                del_result = client.delete_upload("t-lifecycle", "report.txt")
                assert del_result["success"] is True

                # Step 4: Verify deletion
                listed = client.list_uploads("t-lifecycle")
                assert listed["count"] == 1
                assert listed["files"][0]["filename"] == "data.csv"

    def test_upload_then_read_artifact(self, client):
        """Upload a file, simulate agent producing artifact, read it back."""
        from deerflow.runtime.user_context import get_effective_user_id

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            uploads_dir = tmp_path / "uploads"
            uploads_dir.mkdir()

            paths = Paths(base_dir=tmp_path)
            user_id = get_effective_user_id()
            outputs_dir = paths.sandbox_outputs_dir("t-artifact", user_id=user_id)
            outputs_dir.mkdir(parents=True)

            # Upload phase
            src_file = tmp_path / "input.txt"
            src_file.write_text("raw data to process")

            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                uploaded = client.upload_files("t-artifact", [src_file])
                assert len(uploaded["files"]) == 1

            # Simulate agent writing an artifact
            (outputs_dir / "analysis.json").write_text('{"result": "processed"}')

            # Retrieve artifact
            with patch("deerflow.client.get_paths", return_value=paths):
                content, mime = client.get_artifact("t-artifact", "mnt/user-data/outputs/analysis.json")

            assert json.loads(content) == {"result": "processed"}
            assert "json" in mime


# Global asset configuration scenarios are covered by fail-closed client tests.
class TestScenarioAgentRecreation:
    """Scenario: Config changes trigger agent recreation at the right times."""

    def test_different_model_triggers_rebuild(self, client):
        """Switching model_name between calls forces agent rebuild."""
        agents_created = []

        def fake_create_agent(**kwargs):
            agent = MagicMock()
            agents_created.append(agent)
            return agent

        config_a = client._get_runnable_config("t1", model_name="gpt-4")
        config_b = client._get_runnable_config("t1", model_name="claude-3")

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", side_effect=fake_create_agent),
            patch("deerflow.client.build_middlewares", return_value=[]),
            patch("deerflow.client.apply_prompt_template", return_value="prompt"),
            patch.object(client, "_get_tools", return_value=[]),
            patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=MagicMock()),
        ):
            client._ensure_agent(config_a)
            first_agent = client._agent

            client._ensure_agent(config_b)
            second_agent = client._agent

        assert len(agents_created) == 2
        assert first_agent is not second_agent

    def test_same_config_reuses_agent(self, client):
        """Repeated calls with identical config do not rebuild."""
        agents_created = []

        def fake_create_agent(**kwargs):
            agent = MagicMock()
            agents_created.append(agent)
            return agent

        config = client._get_runnable_config("t1", model_name="gpt-4")

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", side_effect=fake_create_agent),
            patch("deerflow.client.build_middlewares", return_value=[]),
            patch("deerflow.client.apply_prompt_template", return_value="prompt"),
            patch.object(client, "_get_tools", return_value=[]),
            patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=MagicMock()),
        ):
            client._ensure_agent(config)
            client._ensure_agent(config)
            client._ensure_agent(config)

        assert len(agents_created) == 1

    def test_reset_agent_forces_rebuild(self, client):
        """reset_agent() clears cache, next call rebuilds."""
        agents_created = []

        def fake_create_agent(**kwargs):
            agent = MagicMock()
            agents_created.append(agent)
            return agent

        config = client._get_runnable_config("t1")

        with (
            patch("deerflow.client.create_chat_model"),
            patch("deerflow.client.create_agent", side_effect=fake_create_agent),
            patch("deerflow.client.build_middlewares", return_value=[]),
            patch("deerflow.client.apply_prompt_template", return_value="prompt"),
            patch.object(client, "_get_tools", return_value=[]),
            patch("deerflow.runtime.checkpointer.get_checkpointer", return_value=MagicMock()),
        ):
            client._ensure_agent(config)
            client.reset_agent()
            client._ensure_agent(config)

        assert len(agents_created) == 2

    def test_per_call_override_triggers_rebuild(self, client):
        """stream() with model_name override creates a different agent config."""
        ai = AIMessage(content="ok", id="ai-1")
        agent = _make_agent_mock([{"messages": [ai]}])

        agents_created = []

        def fake_ensure(config):
            key = tuple(config.get("configurable", {}).get(k) for k in ["model_name", "thinking_enabled", "is_plan_mode", "subagent_enabled"])
            agents_created.append(key)
            client._agent = agent

        with patch.object(client, "_ensure_agent", side_effect=fake_ensure):
            list(client.stream("hi", thread_id="t1"))
            list(client.stream("hi", thread_id="t1", model_name="other-model"))

        # Two different config keys should have been created
        assert len(agents_created) == 2
        assert agents_created[0] != agents_created[1]


class TestScenarioThreadIsolation:
    """Scenario: Operations on different threads don't interfere."""

    def test_uploads_isolated_per_thread(self, client):
        """Files uploaded to thread-A are not visible in thread-B."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            uploads_a = tmp_path / "thread-a" / "uploads"
            uploads_b = tmp_path / "thread-b" / "uploads"
            uploads_a.mkdir(parents=True)
            uploads_b.mkdir(parents=True)

            src_file = tmp_path / "secret.txt"
            src_file.write_text("thread-a only")

            def get_dir(thread_id):
                return uploads_a if thread_id == "thread-a" else uploads_b

            with patch("deerflow.client.get_uploads_dir", side_effect=get_dir), patch("deerflow.client.ensure_uploads_dir", side_effect=get_dir):
                client.upload_files("thread-a", [src_file])

                files_a = client.list_uploads("thread-a")
                files_b = client.list_uploads("thread-b")

            assert files_a["count"] == 1
            assert files_b["count"] == 0

    def test_artifacts_isolated_per_thread(self, client):
        """Artifacts in thread-A are not accessible from thread-B."""
        from deerflow.runtime.user_context import get_effective_user_id

        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(base_dir=tmp)
            user_id = get_effective_user_id()
            outputs_a = paths.sandbox_outputs_dir("thread-a", user_id=user_id)
            outputs_a.mkdir(parents=True)
            paths.sandbox_outputs_dir("thread-b", user_id=user_id).mkdir(parents=True)
            (outputs_a / "result.txt").write_text("thread-a artifact")

            with patch("deerflow.client.get_paths", return_value=paths):
                content, _ = client.get_artifact("thread-a", "mnt/user-data/outputs/result.txt")
                assert content == b"thread-a artifact"

                with pytest.raises(FileNotFoundError):
                    client.get_artifact("thread-b", "mnt/user-data/outputs/result.txt")


class TestScenarioEdgeCases:
    """Scenario: Edge cases and error boundaries in realistic workflows."""

    def test_empty_stream_response(self, client):
        """Agent produces no messages — only values + end events."""
        agent = _make_agent_mock([{"messages": []}])

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-empty"))

        # values event (empty messages) + end
        assert len(events) == 2
        assert events[0].type == "values"
        assert events[-1].type == "end"

    def test_chat_on_empty_response(self, client):
        """chat() returns empty string for no-message response."""
        agent = _make_agent_mock([{"messages": []}])

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            result = client.chat("hi", thread_id="t-empty-chat")

        assert result == ""

    def test_multiple_title_changes(self, client):
        """Title changes are carried in values events."""
        ai = AIMessage(content="ok", id="ai-1")
        chunks = [
            {"messages": [ai], "title": "First Title"},
            {"messages": [], "title": "First Title"},  # same title repeated
            {"messages": [], "title": "Second Title"},  # different title
        ]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-titles"))

        # Every chunk produces a values event with the title
        values_events = [e for e in events if e.type == "values"]
        assert len(values_events) == 3
        assert values_events[0].data["title"] == "First Title"
        assert values_events[1].data["title"] == "First Title"
        assert values_events[2].data["title"] == "Second Title"

    def test_concurrent_tool_calls_in_single_message(self, client):
        """Agent produces multiple tool_calls in one AIMessage — emitted as single messages-tuple."""
        ai = AIMessage(
            content="",
            id="ai-1",
            tool_calls=[
                {"name": "web_search", "args": {"q": "a"}, "id": "tc-1"},
                {"name": "web_search", "args": {"q": "b"}, "id": "tc-2"},
                {"name": "bash", "args": {"cmd": "echo hi"}, "id": "tc-3"},
            ],
        )
        chunks = [{"messages": [ai]}]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("do things", thread_id="t-parallel"))

        tc_events = _tool_call_events(events)
        assert len(tc_events) == 1  # One messages-tuple event for the AIMessage
        tool_calls = tc_events[0].data["tool_calls"]
        assert len(tool_calls) == 3
        assert {tc["id"] for tc in tool_calls} == {"tc-1", "tc-2", "tc-3"}

    def test_upload_convertible_file_conversion_failure(self, client):
        """Upload a .pdf file where conversion fails — file still uploaded, no markdown."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            uploads_dir = tmp_path / "uploads"
            uploads_dir.mkdir()

            pdf_file = tmp_path / "doc.pdf"
            pdf_file.write_bytes(b"%PDF-1.4 fake content")

            with (
                patch("deerflow.client.get_uploads_dir", return_value=uploads_dir),
                patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir),
                patch("deerflow.utils.file_conversion.CONVERTIBLE_EXTENSIONS", {".pdf"}),
                patch("deerflow.utils.file_conversion.convert_file_to_markdown", side_effect=Exception("conversion failed")),
            ):
                result = client.upload_files("t-pdf-fail", [pdf_file])

            assert result["success"] is True
            assert len(result["files"]) == 1
            assert result["files"][0]["filename"] == "doc.pdf"
            assert "markdown_file" not in result["files"][0]  # Conversion failed gracefully
            assert (uploads_dir / "doc.pdf").exists()  # File still uploaded


# ---------------------------------------------------------------------------
# Gateway conformance — validate client output against Gateway Pydantic models
# ---------------------------------------------------------------------------


class TestGatewayConformance:
    """Validate that DeerFlowClient return dicts conform to Gateway Pydantic response models.

    Each test calls a client method, then parses the result through the
    corresponding Gateway response model. If the client drifts (missing or
    wrong-typed fields), Pydantic raises ``ValidationError`` and CI catches it.
    """

    def test_list_models(self, mock_app_config):
        model = MagicMock()
        model.name = "test-model"
        model.model = "gpt-test"
        model.display_name = "Test Model"
        model.description = "A test model"
        model.supports_thinking = False
        model.supports_reasoning_effort = False
        mock_app_config.models = [model]
        mock_app_config.token_usage.enabled = True

        with patch("deerflow.client.get_app_config", return_value=mock_app_config):
            client = DeerFlowClient()

        result = client.list_models()
        parsed = ModelsListResponse(**result)
        assert len(parsed.models) == 1
        assert parsed.models[0].name == "test-model"
        assert parsed.models[0].model == "test-model"
        assert parsed.token_usage.enabled is True

    def test_get_model(self, mock_app_config):
        model = MagicMock()
        model.name = "test-model"
        model.model = "gpt-test"
        model.display_name = "Test Model"
        model.description = "A test model"
        model.supports_thinking = True
        mock_app_config.models = [model]
        mock_app_config.get_model_config.return_value = model

        with patch("deerflow.client.get_app_config", return_value=mock_app_config):
            client = DeerFlowClient()

        result = client.get_model("test-model")
        assert result is not None
        parsed = ModelResponse(**result)
        assert parsed.name == "test-model"
        assert parsed.model == "test-model"

    def test_upload_files(self, client, tmp_path):
        uploads_dir = tmp_path / "uploads"
        uploads_dir.mkdir()

        src_file = tmp_path / "hello.txt"
        src_file.write_text("hello")

        with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
            result = client.upload_files("t-conform", [src_file])

        assert result["success"] is True
        assert len(result["files"]) == 1
        assert result["files"][0]["size"] == len("hello")

    def test_goal_methods(self, client):
        from langgraph.checkpoint.memory import InMemorySaver

        client._checkpointer = InMemorySaver()

        result = client.set_goal("t-goal", "ship it")

        assert result["goal"] is not None
        assert result["goal"]["objective"] == "ship it"
        assert client.clear_goal("t-goal")["goal"] is None


class TestAtomicWriteJson:
    def test_temp_file_cleaned_on_serialization_failure(self):
        """If json.dump raises, the temp file is removed."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"

            # An object that cannot be serialized to JSON.
            bad_data = {"key": object()}

            with pytest.raises(TypeError):
                DeerFlowClient._atomic_write_json(target, bad_data)

            # Target should not have been created.
            assert not target.exists()
            # No stray .tmp files should remain.
            tmp_files = list(Path(tmp).glob("*.tmp"))
            assert tmp_files == []

    def test_happy_path_writes_atomically(self):
        """Normal write produces correct JSON and no temp files."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            data = {"key": "value", "nested": [1, 2, 3]}

            DeerFlowClient._atomic_write_json(target, data)

            assert target.exists()
            with open(target) as f:
                loaded = json.load(f)
            assert loaded == data
            # No temp files left behind.
            assert list(Path(tmp).glob("*.tmp")) == []

    def test_original_preserved_on_failure(self):
        """If write fails, the original file is not corrupted."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.json"
            target.write_text('{"original": true}')

            bad_data = {"key": object()}
            with pytest.raises(TypeError):
                DeerFlowClient._atomic_write_json(target, bad_data)

            # Original content must survive.
            with open(target) as f:
                assert json.load(f) == {"original": True}


# ===========================================================================
# Hardening — config update error paths
# ===========================================================================


# File-backed asset update errors no longer have a client path.
class TestStreamHardening:
    def test_agent_exception_propagates(self, client):
        """Exceptions from agent.stream() propagate to caller."""
        agent = MagicMock()
        agent.stream.side_effect = RuntimeError("model quota exceeded")

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            with pytest.raises(RuntimeError, match="model quota exceeded"):
                list(client.stream("hi", thread_id="t-err"))

    def test_messages_without_id(self, client):
        """Messages without id attribute are emitted without crashing."""
        ai = AIMessage(content="no id here")
        # Forcibly remove the id attribute to simulate edge case.
        object.__setattr__(ai, "id", None)
        chunks = [{"messages": [ai]}]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-noid"))

        # Should produce events without error.
        assert events[-1].type == "end"
        ai_events = _ai_events(events)
        assert len(ai_events) == 1
        assert ai_events[0].data["content"] == "no id here"

    def test_tool_calls_only_no_text(self, client):
        """chat() returns empty string when agent only emits tool calls."""
        ai = AIMessage(
            content="",
            id="ai-1",
            tool_calls=[{"name": "bash", "args": {"cmd": "ls"}, "id": "tc-1"}],
        )
        tool = ToolMessage(content="output", id="tm-1", tool_call_id="tc-1", name="bash")
        chunks = [
            {"messages": [ai]},
            {"messages": [ai, tool]},
        ]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            result = client.chat("do it", thread_id="t-tc-only")

        assert result == ""

    def test_duplicate_messages_without_id_not_deduplicated(self, client):
        """Messages with id=None are NOT deduplicated (each is emitted)."""
        ai1 = AIMessage(content="first")
        ai2 = AIMessage(content="second")
        object.__setattr__(ai1, "id", None)
        object.__setattr__(ai2, "id", None)

        chunks = [
            {"messages": [ai1]},
            {"messages": [ai2]},
        ]
        agent = _make_agent_mock(chunks)

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            events = list(client.stream("hi", thread_id="t-dup-noid"))

        ai_msgs = _ai_events(events)
        assert len(ai_msgs) == 2


# ===========================================================================
# Hardening — _serialize_message coverage
# ===========================================================================


class TestSerializeMessage:
    def test_system_message(self):
        msg = SystemMessage(content="You are a helpful assistant.", id="sys-1")
        result = DeerFlowClient._serialize_message(msg)
        assert result["type"] == "system"
        assert result["content"] == "You are a helpful assistant."
        assert result["id"] == "sys-1"

    def test_unknown_message_type(self):
        """Non-standard message types serialize as 'unknown'."""
        msg = MagicMock()
        msg.id = "unk-1"
        msg.content = "something"
        # Not an instance of AIMessage/ToolMessage/HumanMessage/SystemMessage
        type(msg).__name__ = "CustomMessage"
        result = DeerFlowClient._serialize_message(msg)
        assert result["type"] == "unknown"
        assert result["id"] == "unk-1"

    def test_ai_message_with_tool_calls(self):
        msg = AIMessage(
            content="",
            id="ai-tc",
            tool_calls=[{"name": "bash", "args": {"cmd": "ls"}, "id": "tc-1"}],
        )
        result = DeerFlowClient._serialize_message(msg)
        assert result["type"] == "ai"
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "bash"

    def test_tool_message_non_string_content(self):
        msg = ToolMessage(content={"key": "value"}, id="tm-1", tool_call_id="tc-1", name="tool")
        result = DeerFlowClient._serialize_message(msg)
        assert result["type"] == "tool"
        assert isinstance(result["content"], str)


# ===========================================================================
# Hardening — upload / delete symlink attack
# ===========================================================================


class TestUploadDeleteSymlink:
    def test_delete_upload_symlink_outside_dir(self, client):
        """A symlink in uploads dir pointing outside is caught by path traversal check."""
        with tempfile.TemporaryDirectory() as tmp:
            uploads_dir = Path(tmp) / "uploads"
            uploads_dir.mkdir()

            # Create a target file outside uploads dir.
            outside = Path(tmp) / "secret.txt"
            outside.write_text("sensitive data")

            # Create a symlink inside uploads dir pointing to outside file.
            link = uploads_dir / "harmless.txt"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    pytest.skip("symlink creation requires Developer Mode or elevated privileges on Windows")
                raise

            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                # The resolved path of the symlink escapes uploads_dir,
                # so path traversal check should catch it.
                with pytest.raises(PathTraversalError):
                    client.delete_upload("thread-1", "harmless.txt")

            # The outside file must NOT have been deleted.
            assert outside.exists()

    def test_upload_filename_with_spaces_and_unicode(self, client):
        """Files with spaces and unicode characters in names upload correctly."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            uploads_dir = tmp_path / "uploads"
            uploads_dir.mkdir()

            weird_name = "report 2024 数据.txt"
            src_file = tmp_path / weird_name
            src_file.write_text("data")

            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                result = client.upload_files("thread-1", [src_file])

            assert result["success"] is True
            assert result["files"][0]["filename"] == weird_name
            assert (uploads_dir / weird_name).exists()


# ===========================================================================
# Hardening — artifact edge cases
# ===========================================================================


class TestArtifactHardening:
    def test_artifact_directory_rejected(self, client):
        """get_artifact rejects paths that resolve to a directory."""
        from deerflow.runtime.user_context import get_effective_user_id

        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(base_dir=tmp)
            user_id = get_effective_user_id()
            subdir = paths.sandbox_outputs_dir("t1", user_id=user_id) / "subdir"
            subdir.mkdir(parents=True)

            with patch("deerflow.client.get_paths", return_value=paths):
                with pytest.raises(ValueError, match="not a file"):
                    client.get_artifact("t1", "mnt/user-data/outputs/subdir")

    def test_artifact_leading_slash_stripped(self, client):
        """Paths with leading slash are handled correctly."""
        from deerflow.runtime.user_context import get_effective_user_id

        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(base_dir=tmp)
            user_id = get_effective_user_id()
            outputs = paths.sandbox_outputs_dir("t1", user_id=user_id)
            outputs.mkdir(parents=True)
            (outputs / "file.txt").write_text("content")

            with patch("deerflow.client.get_paths", return_value=paths):
                content, _mime = client.get_artifact("t1", "/mnt/user-data/outputs/file.txt")

            assert content == b"content"


# ===========================================================================
# BUG DETECTION — tests that expose real bugs in client.py
# ===========================================================================


class TestUploadDuplicateFilenames:
    """Regression: upload_files must auto-rename duplicate basenames.

    Previously it silently overwrote the first file with the second,
    then reported both in the response while only one existed on disk.
    Now duplicates are renamed (data.txt → data_1.txt) and the response
    includes original_filename so the agent / caller can see what happened.
    """

    def test_duplicate_filenames_auto_renamed(self, client):
        """Two files with same basename → second gets _1 suffix."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            uploads_dir = tmp_path / "uploads"
            uploads_dir.mkdir()

            dir_a = tmp_path / "a"
            dir_b = tmp_path / "b"
            dir_a.mkdir()
            dir_b.mkdir()
            (dir_a / "data.txt").write_text("version A")
            (dir_b / "data.txt").write_text("version B")

            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                result = client.upload_files("t-dup", [dir_a / "data.txt", dir_b / "data.txt"])

            assert result["success"] is True
            assert len(result["files"]) == 2

            # Both files exist on disk with distinct names.
            disk_files = sorted(p.name for p in uploads_dir.iterdir())
            assert disk_files == ["data.txt", "data_1.txt"]

            # First keeps original name, second is renamed.
            assert result["files"][0]["filename"] == "data.txt"
            assert "original_filename" not in result["files"][0]

            assert result["files"][1]["filename"] == "data_1.txt"
            assert result["files"][1]["original_filename"] == "data.txt"

            # Content preserved correctly.
            assert (uploads_dir / "data.txt").read_text() == "version A"
            assert (uploads_dir / "data_1.txt").read_text() == "version B"

    def test_triple_duplicate_increments_counter(self, client):
        """Three files with same basename → _1, _2 suffixes."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            uploads_dir = tmp_path / "uploads"
            uploads_dir.mkdir()

            for name in ["x", "y", "z"]:
                d = tmp_path / name
                d.mkdir()
                (d / "report.csv").write_text(f"from {name}")

            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                result = client.upload_files(
                    "t-triple",
                    [tmp_path / "x" / "report.csv", tmp_path / "y" / "report.csv", tmp_path / "z" / "report.csv"],
                )

            filenames = [f["filename"] for f in result["files"]]
            assert filenames == ["report.csv", "report_1.csv", "report_2.csv"]
            assert len(list(uploads_dir.iterdir())) == 3

    def test_different_filenames_no_rename(self, client):
        """Non-duplicate filenames upload normally without rename."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            uploads_dir = tmp_path / "uploads"
            uploads_dir.mkdir()

            (tmp_path / "a.txt").write_text("aaa")
            (tmp_path / "b.txt").write_text("bbb")

            with patch("deerflow.client.get_uploads_dir", return_value=uploads_dir), patch("deerflow.client.ensure_uploads_dir", return_value=uploads_dir):
                result = client.upload_files("t-ok", [tmp_path / "a.txt", tmp_path / "b.txt"])

            assert result["success"] is True
            assert len(result["files"]) == 2
            assert all("original_filename" not in f for f in result["files"])
            assert len(list(uploads_dir.iterdir())) == 2


class TestBugArtifactPrefixMatchTooLoose:
    """Regression: get_artifact must reject paths like ``mnt/user-data-evil/...``.

    Previously ``startswith("mnt/user-data")`` matched ``"mnt/user-data-evil"``
    because it was a string prefix, not a path-segment check.
    """

    def test_non_canonical_prefix_rejected(self, client):
        """Paths that share a string prefix but differ at segment boundary are rejected."""
        with pytest.raises(ValueError, match="must start with"):
            client.get_artifact("t1", "mnt/user-data-evil/secret.txt")

    def test_exact_prefix_without_subpath_accepted(self, client):
        """Bare 'mnt/user-data' is accepted (will later fail as directory, not at prefix)."""
        from deerflow.runtime.user_context import get_effective_user_id

        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(base_dir=tmp)
            user_id = get_effective_user_id()
            paths.sandbox_outputs_dir("t1", user_id=user_id).mkdir(parents=True)

            with patch("deerflow.client.get_paths", return_value=paths):
                # Accepted at prefix check, but fails because it's a directory.
                with pytest.raises(ValueError, match="not a file"):
                    client.get_artifact("t1", "mnt/user-data")


class TestBugListUploadsDeadCode:
    """Regression: list_uploads works even when called on a fresh thread
    (directory does not exist yet — returns empty without creating it).
    """

    def test_list_uploads_on_fresh_thread(self, client):
        """list_uploads on a thread that never had uploads returns empty list."""
        with tempfile.TemporaryDirectory() as tmp:
            non_existent = Path(tmp) / "does-not-exist" / "uploads"
            assert not non_existent.exists()

            mock_paths = MagicMock()
            mock_paths.sandbox_uploads_dir.return_value = non_existent

            with patch("deerflow.uploads.manager.get_paths", return_value=mock_paths):
                result = client.list_uploads("thread-fresh")

            # Read path should NOT create the directory
            assert not non_existent.exists()
            assert result == {"files": [], "count": 0}
