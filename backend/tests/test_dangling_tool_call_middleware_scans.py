"""Focused scan and comparison contracts for dangling tool-call repair."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.agents.middlewares.dangling_tool_call_middleware import (
    DanglingToolCallMiddleware,
)


def _complete_tool_history(pair_count: int) -> list:
    messages = []
    for index in range(pair_count):
        tool_call_id = f"call-{index}"
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": tool_call_id,
                            "name": "lookup",
                            "args": {"query": index},
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(
                    content="ok",
                    tool_call_id=tool_call_id,
                    name="lookup",
                ),
            ]
        )
    return messages


def test_unchanged_history_list_comparison_uses_message_identity_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuilt list of the same message objects must not call Pydantic equality."""

    equality_calls = 0
    original_eq: Callable = HumanMessage.__eq__

    def counting_eq(self, other):
        nonlocal equality_calls
        equality_calls += 1
        return original_eq(self, other)

    monkeypatch.setattr(HumanMessage, "__eq__", counting_eq)
    messages = [HumanMessage(content=f"message-{index}") for index in range(100)]

    assert DanglingToolCallMiddleware()._build_patched_messages(messages) is None
    assert equality_calls == 0

    # Prove the counter observes Pydantic equality when object identity differs.
    assert [message.model_copy(deep=True) for message in messages] == messages
    assert equality_calls == len(messages)


def test_each_ai_message_is_normalized_once_per_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = DanglingToolCallMiddleware()
    messages = _complete_tool_history(50)
    original = middleware._message_tool_calls
    normalization_calls = 0

    def counted(message):
        nonlocal normalization_calls
        normalization_calls += 1
        return original(message)

    monkeypatch.setattr(middleware, "_message_tool_calls", counted)

    assert middleware._build_patched_messages(messages) is None
    assert normalization_calls == 50


def test_precomputed_tool_calls_are_local_to_one_history() -> None:
    middleware = DanglingToolCallMiddleware()
    first = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "first-call",
                "name": "lookup",
                "args": {},
                "type": "tool_call",
            }
        ],
    )
    complete_second = _complete_tool_history(1)

    patched_first = middleware._build_patched_messages([first])
    assert patched_first is not None
    assert [message.type for message in patched_first] == ["ai", "tool"]
    assert patched_first[1].tool_call_id == "first-call"

    assert middleware._build_patched_messages(complete_second) is None

    patched_first_again = middleware._build_patched_messages([first])
    assert patched_first_again is not None
    assert patched_first_again[1].tool_call_id == "first-call"


def test_precomputation_preserves_tool_call_order_and_unrelated_messages() -> None:
    middleware = DanglingToolCallMiddleware()
    ai_message = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "first-call",
                "name": "lookup",
                "args": {},
                "type": "tool_call",
            },
            {
                "id": "second-call",
                "name": "lookup",
                "args": {},
                "type": "tool_call",
            },
        ],
    )
    interposed = HumanMessage(content="continue")
    second_result = ToolMessage(
        content="second",
        tool_call_id="second-call",
        name="lookup",
    )
    first_result = ToolMessage(
        content="first",
        tool_call_id="first-call",
        name="lookup",
    )
    unrelated = ToolMessage(
        content="unrelated",
        tool_call_id="outside-history",
        name="lookup",
    )

    patched = middleware._build_patched_messages([ai_message, interposed, second_result, first_result, unrelated])

    assert patched == [
        ai_message,
        first_result,
        second_result,
        interposed,
        unrelated,
    ]
