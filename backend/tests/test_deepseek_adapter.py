"""M9 DeepSeek convergence: one ``deepseek`` adapter with full reasoning replay.

The catalog exposes a single DeepSeek descriptor backed by
``PatchedChatDeepSeek``. Per the official thinking-mode guide, requests that
carry tools must echo historical assistant ``reasoning_content`` back —
including turns that produced no tool_calls — while requests without tools
ignore the field. The adapter replays it unconditionally, so every outgoing
payload shape is asserted here against unchanged original messages.
"""

from __future__ import annotations

import copy

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from app.system_settings.validation import BUILTIN_PROVIDER_ADAPTERS, PROVIDER_ADAPTERS
from deerflow.models.factory import _DEEPSEEK_PROVIDER_ADAPTERS
from deerflow.models.patched_deepseek import PatchedChatDeepSeek


def _model() -> PatchedChatDeepSeek:
    return PatchedChatDeepSeek(
        api_key="test-only",
        model="deepseek-v4-flash",
        max_retries=0,
    )


def _assistant_payloads(payload: dict) -> list[dict]:
    return [message for message in payload["messages"] if message.get("role") == "assistant"]


_LOOKUP_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look up a value.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


def test_catalog_exposes_single_deepseek_descriptor() -> None:
    assert "patched_deepseek" not in PROVIDER_ADAPTERS
    assert "patched_deepseek" not in BUILTIN_PROVIDER_ADAPTERS
    assert PROVIDER_ADAPTERS["deepseek"].class_path == "deerflow.models.patched_deepseek:PatchedChatDeepSeek"
    assert BUILTIN_PROVIDER_ADAPTERS["deepseek"].class_path == "deerflow.models.patched_deepseek:PatchedChatDeepSeek"
    assert _DEEPSEEK_PROVIDER_ADAPTERS == frozenset({"deepseek"})


def test_serialization_keeps_secret_protection() -> None:
    model = _model()
    assert PatchedChatDeepSeek.is_lc_serializable() is True
    assert model.lc_secrets == {
        "api_key": "DEEPSEEK_API_KEY",
        "openai_api_key": "DEEPSEEK_API_KEY",
    }


def test_request_without_tools_still_replays_reasoning() -> None:
    """The tolerant case: no tools in the request, reasoning is still sent."""
    messages = [
        HumanMessage(content="question"),
        AIMessage(
            content="plain answer",
            additional_kwargs={"reasoning_content": "hidden deliberation"},
        ),
        HumanMessage(content="follow-up"),
    ]

    payload = _model()._get_request_payload(messages)

    assert "tools" not in payload
    (assistant,) = _assistant_payloads(payload)
    assert assistant["reasoning_content"] == "hidden deliberation"


def test_request_with_empty_tool_set_replays_reasoning() -> None:
    """A genuinely empty tool collection must not disable the replay."""
    messages = [
        HumanMessage(content="question"),
        AIMessage(
            content="plain answer",
            additional_kwargs={"reasoning_content": "hidden deliberation"},
        ),
        HumanMessage(content="follow-up"),
    ]

    payload = _model()._get_request_payload(messages, tools=[])

    assert payload["tools"] == []
    (assistant,) = _assistant_payloads(payload)
    assert assistant["reasoning_content"] == "hidden deliberation"


def test_consecutive_tool_subrounds_replay_reasoning_on_every_assistant_turn() -> None:
    messages = [
        HumanMessage(content="orchestrate"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "lookup", "args": {"query": "one"}, "id": "call-1", "type": "tool_call"},
            ],
            additional_kwargs={"reasoning_content": "first tool round"},
        ),
        ToolMessage(content="result-one", tool_call_id="call-1"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "lookup", "args": {"query": "two"}, "id": "call-2", "type": "tool_call"},
            ],
            additional_kwargs={"reasoning_content": "second tool round"},
        ),
        ToolMessage(content="result-two", tool_call_id="call-2"),
    ]

    payload = _model()._get_request_payload(messages, tools=[_LOOKUP_TOOL])

    assert payload["tools"] == [_LOOKUP_TOOL]
    first, second = _assistant_payloads(payload)
    assert first["reasoning_content"] == "first tool round"
    assert first["tool_calls"][0]["id"] == "call-1"
    assert second["reasoning_content"] == "second tool round"
    assert second["tool_calls"][0]["id"] == "call-2"


def test_turn_without_tool_calls_between_tool_rounds_replays_reasoning() -> None:
    """Turns that produced no tool_calls must echo reasoning too (strict case)."""
    messages = [
        HumanMessage(content="orchestrate"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "lookup", "args": {"query": "one"}, "id": "call-1", "type": "tool_call"},
            ],
            additional_kwargs={"reasoning_content": "tool round"},
        ),
        ToolMessage(content="result-one", tool_call_id="call-1"),
        AIMessage(
            content="final answer without further tool calls",
            additional_kwargs={"reasoning_content": "wrap-up thinking"},
        ),
        HumanMessage(content="next question"),
    ]

    payload = _model()._get_request_payload(messages, tools=[_LOOKUP_TOOL])

    tool_turn, plain_turn = _assistant_payloads(payload)
    assert tool_turn["reasoning_content"] == "tool round"
    assert plain_turn.get("tool_calls") in (None, [])
    assert plain_turn["reasoning_content"] == "wrap-up thinking"


def test_reasoning_replays_across_user_turns() -> None:
    messages = [
        HumanMessage(content="first question"),
        AIMessage(
            content="first answer",
            additional_kwargs={"reasoning_content": "first turn thinking"},
        ),
        HumanMessage(content="second question"),
        AIMessage(
            content="second answer",
            additional_kwargs={"reasoning_content": "second turn thinking"},
        ),
        HumanMessage(content="third question"),
    ]

    payload = _model()._get_request_payload(messages, tools=[_LOOKUP_TOOL])

    first, second = _assistant_payloads(payload)
    assert first["reasoning_content"] == "first turn thinking"
    assert second["reasoning_content"] == "second turn thinking"


def test_streamed_chunks_merge_then_replay_complete_reasoning() -> None:
    """Streaming deltas accumulate reasoning; the merged turn replays in full."""
    merged = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "part one, "}) + AIMessageChunk(content="", additional_kwargs={"reasoning_content": "part two"}) + AIMessageChunk(content="streamed answer")
    assert merged.additional_kwargs["reasoning_content"] == "part one, part two"

    messages = [
        HumanMessage(content="question"),
        AIMessage(content=merged.content, additional_kwargs=dict(merged.additional_kwargs)),
        HumanMessage(content="follow-up"),
    ]

    payload = _model()._get_request_payload(messages, tools=[_LOOKUP_TOOL])

    (assistant,) = _assistant_payloads(payload)
    assert assistant["content"] == "streamed answer"
    assert assistant["reasoning_content"] == "part one, part two"


def test_replay_never_mutates_original_messages() -> None:
    messages = [
        HumanMessage(content="question"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "lookup", "args": {"query": "one"}, "id": "call-1", "type": "tool_call"},
            ],
            additional_kwargs={"reasoning_content": "tool thinking"},
        ),
        ToolMessage(content="result-one", tool_call_id="call-1"),
        AIMessage(
            content="answer",
            additional_kwargs={"reasoning_content": "final thinking"},
        ),
        HumanMessage(content="follow-up"),
    ]
    snapshot = copy.deepcopy([message.model_dump() for message in messages])

    _model()._get_request_payload(messages, tools=[_LOOKUP_TOOL])

    assert [message.model_dump() for message in messages] == snapshot
