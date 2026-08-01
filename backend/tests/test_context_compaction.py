from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.runtime import context_compaction
from deerflow.runtime.context_compaction import compact_thread_context


class _FakeCheckpointer:
    def __init__(self, checkpoint: dict, metadata: dict | None = None) -> None:
        self.checkpoint = checkpoint
        self.metadata = metadata or {"step": 4, "created_at": "2026-07-06T00:00:00+00:00"}
        self.update_args = None

    async def aget(self, config):
        return SimpleNamespace(
            values=self.checkpoint.get("channel_values", {}),
            metadata=self.metadata,
            config={"configurable": {"thread_id": config["configurable"]["thread_id"], "checkpoint_id": "ckpt-old", "checkpoint_ns": ""}},
        )

    async def aupdate(self, config, values, *, as_node=None):
        self.update_args = (config, values, as_node)
        return {"configurable": {"checkpoint_id": "ckpt-new"}}


class _FakeCompactionMiddleware:
    def __init__(self, *, should_compact: bool = True) -> None:
        self.should_compact = should_compact
        self.prepare_calls = 0
        self.runtime_contexts: list[dict] = []

    def _prepare_compaction(self, state, *, force=False):
        self.prepare_calls += 1
        if not self.should_compact:
            return None
        return (state["messages"][:-1], state["messages"][-1:], state.get("summary_text"), 123)

    async def acompact_state(self, state, runtime, *, force=False):
        self.runtime_contexts.append(dict(runtime.context))
        prepared = self._prepare_compaction(state, force=force)
        if prepared is None:
            return None
        messages_to_summarize, preserved_messages, _previous_summary, total_tokens = prepared
        return SimpleNamespace(
            summary_text="COMPRESSED SUMMARY",
            messages_to_summarize=tuple(messages_to_summarize),
            preserved_messages=tuple(preserved_messages),
            total_tokens=total_tokens,
        )


class _RejectDeepcopy:
    def __deepcopy__(self, _memo):
        raise AssertionError("compact_thread_context must not deepcopy unrelated channel values")


@pytest.mark.asyncio
async def test_compact_thread_context_writes_summary_and_bumps_changed_channels(monkeypatch):
    messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="latest question"),
    ]
    checkpointer = _FakeCheckpointer(
        {
            "id": "ckpt-old",
            "channel_values": {"messages": messages, "summary_text": "OLD SUMMARY", "sandbox": _RejectDeepcopy()},
            "channel_versions": {"messages": 7, "summary_text": 3, "title": 2},
        }
    )
    middleware = _FakeCompactionMiddleware()
    monkeypatch.setattr(
        context_compaction,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )

    result = await compact_thread_context(
        checkpointer,
        "thread-1",
        app_config=SimpleNamespace(),
        user_id="user-1",
        agent_name="research-agent",
    )

    assert result.compacted is True
    assert result.removed_message_count == 2
    assert result.preserved_message_count == 1
    assert result.summary_updated is True
    assert result.total_tokens == 123

    assert checkpointer.update_args is not None
    _config, written_values, as_node = checkpointer.update_args
    assert written_values["messages"].value == [messages[-1]]
    assert written_values["summary_text"] == "COMPRESSED SUMMARY"
    assert "sandbox" not in written_values
    assert as_node == "manual_compaction"
    assert middleware.prepare_calls == 1
    assert middleware.runtime_contexts == [
        {"thread_id": "thread-1", "user_id": "user-1", "agent_name": "research-agent"},
    ]


@pytest.mark.asyncio
async def test_compaction_can_prepare_without_writing_then_commit(monkeypatch):
    messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="latest question"),
    ]
    checkpointer = _FakeCheckpointer(
        {
            "id": "ckpt-old",
            "channel_values": {"messages": messages},
            "channel_versions": {"messages": 2},
        }
    )
    monkeypatch.setattr(
        context_compaction,
        "_create_compaction_middleware",
        lambda **_kwargs: _FakeCompactionMiddleware(),
    )

    prepared = await context_compaction.prepare_thread_compaction(
        checkpointer,
        "thread-1",
        app_config=SimpleNamespace(),
    )

    assert prepared.source_checkpoint_id == "ckpt-old"
    assert prepared.result.compacted is True
    assert checkpointer.update_args is None

    result = await context_compaction.commit_thread_compaction(
        checkpointer,
        prepared,
    )

    assert result.compacted is True
    assert result.checkpoint_id is not None
    assert checkpointer.update_args is not None


@pytest.mark.asyncio
async def test_compact_thread_context_returns_noop_without_writing(monkeypatch):
    checkpointer = _FakeCheckpointer(
        {
            "id": "ckpt-old",
            "channel_values": {"messages": [HumanMessage(content="latest only")]},
            "channel_versions": {"messages": 1},
        }
    )
    middleware = _FakeCompactionMiddleware(should_compact=False)
    monkeypatch.setattr(
        context_compaction,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )

    result = await compact_thread_context(checkpointer, "thread-1", app_config=SimpleNamespace())

    assert result.compacted is False
    assert result.reason == "not_enough_messages"
    assert checkpointer.update_args is None
    assert middleware.prepare_calls == 1


@pytest.mark.asyncio
async def test_compact_thread_context_uses_materialized_accessor(monkeypatch):
    messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="latest question"),
    ]
    checkpointer = _FakeCheckpointer(
        {
            "id": "ckpt-old",
            "channel_values": {"messages": messages, "summary_text": "OLD SUMMARY"},
            "channel_versions": {"messages": 7, "summary_text": 3},
        }
    )
    monkeypatch.setattr(
        context_compaction,
        "_create_compaction_middleware",
        lambda **_kwargs: _FakeCompactionMiddleware(),
    )

    result = await compact_thread_context(checkpointer, "thread-1", app_config=SimpleNamespace())

    assert result.compacted is True
    assert checkpointer.update_args is not None
