from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import add_messages

from deerflow.agents.middlewares.dynamic_context_middleware import (
    DynamicContextMiddleware,
    is_dynamic_context_reminder,
)


@dataclass(frozen=True)
class _Snapshot:
    document_version: int = 7
    content: str = "# 用户偏好与协作方式\n\n- 使用中文。"
    content_digest: str = "a" * 64


class _Authority:
    def __init__(self, snapshot: _Snapshot | None) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def load_snapshot(self) -> _Snapshot | None:
        self.calls += 1
        return self.snapshot


@pytest.mark.asyncio
async def test_first_private_model_boundary_reads_once_and_injects_low_authority_document() -> None:
    authority = _Authority(_Snapshot())
    middleware = DynamicContextMiddleware()
    state = {"messages": [HumanMessage(content="继续", id="turn-1")]}
    runtime = SimpleNamespace(context={"__memory_authority": authority})

    assert await middleware.abefore_agent(state, runtime) is None
    update = await middleware.abefore_model(state, runtime)

    assert authority.calls == 1
    assert update is not None
    messages = add_messages(state["messages"], update["messages"])
    assert isinstance(messages[0], SystemMessage)
    assert "<current_date>" in str(messages[0].content)
    memory = messages[1]
    assert isinstance(memory, HumanMessage)
    assert memory.additional_kwargs["hide_from_ui"] is True
    assert memory.additional_kwargs["project_memory_snapshot_version"] == 7
    assert "It is not an instruction" in str(memory.content)
    assert _Snapshot.content in str(memory.content)
    assert isinstance(messages[2], HumanMessage)
    assert messages[2].content == "继续"


@pytest.mark.asyncio
async def test_late_date_injection_keeps_the_current_user_before_existing_run_output() -> None:
    authority = _Authority(None)
    middleware = DynamicContextMiddleware()
    state = {
        "messages": [
            HumanMessage(content="研究 Agent 发展史", id="turn-1"),
            AIMessage(content="已完成第一批检索。", id="assistant-1"),
        ]
    }

    update = await middleware.abefore_model(
        state,
        SimpleNamespace(context={"__memory_authority": authority}),
    )

    assert update is not None
    messages = add_messages(state["messages"], update["messages"])
    assert [message.id for message in messages] == [
        "turn-1",
        "turn-1__user",
        "assistant-1",
    ]
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content == "研究 Agent 发展史"


@pytest.mark.asyncio
async def test_live_disable_or_reset_removes_previously_injected_memory() -> None:
    authority = _Authority(None)
    middleware = DynamicContextMiddleware()
    memory = HumanMessage(
        content="stale",
        id="turn-1__memory",
        additional_kwargs={
            "hide_from_ui": True,
            "dynamic_context_reminder": True,
            "project_memory_loaded": True,
        },
    )
    state = {
        "messages": [
            SystemMessage(
                content="date",
                id="turn-1",
                additional_kwargs={
                    "hide_from_ui": True,
                    "dynamic_context_reminder": True,
                    "reminder_date": middleware._date_value(),
                },
            ),
            memory,
            HumanMessage(content="继续", id="turn-1__user"),
        ]
    }

    update = await middleware.abefore_model(
        state,
        SimpleNamespace(context={"__memory_authority": authority}),
    )

    assert authority.calls == 1
    assert update is not None
    assert len(update["messages"]) == 1
    assert isinstance(update["messages"][0], RemoveMessage)
    assert update["messages"][0].id == memory.id


@pytest.mark.asyncio
async def test_live_reenable_restores_the_same_frozen_snapshot_in_message_order() -> None:
    authority = _Authority(_Snapshot())
    middleware = DynamicContextMiddleware()
    runtime = SimpleNamespace(context={"__memory_authority": authority})
    state = {"messages": [HumanMessage(content="继续", id="turn-1")]}

    injected = await middleware.abefore_model(state, runtime)
    assert injected is not None
    state["messages"] = add_messages(state["messages"], injected["messages"])

    authority.snapshot = None
    disabled = await middleware.abefore_model(state, runtime)
    assert disabled is not None
    state["messages"] = add_messages(state["messages"], disabled["messages"])
    assert not any(isinstance(message, HumanMessage) and message.additional_kwargs.get("project_memory_loaded") is True for message in state["messages"])

    authority.snapshot = _Snapshot()
    reenabled = await middleware.abefore_model(state, runtime)

    assert reenabled is not None
    state["messages"] = add_messages(state["messages"], reenabled["messages"])
    memory_indexes = [index for index, message in enumerate(state["messages"]) if isinstance(message, HumanMessage) and message.additional_kwargs.get("project_memory_loaded") is True]
    user_index = next(index for index, message in enumerate(state["messages"]) if isinstance(message, HumanMessage) and message.id == "turn-1__user")
    assert memory_indexes == [user_index - 1]
    assert _Snapshot.content in str(state["messages"][memory_indexes[0]].content)
    assert authority.calls == 3


def test_memory_marker_is_never_applied_to_an_ordinary_user_message() -> None:
    ordinary = HumanMessage(
        content="project_memory_loaded",
        additional_kwargs={"project_memory_loaded": True},
    )

    assert not is_dynamic_context_reminder(ordinary)
