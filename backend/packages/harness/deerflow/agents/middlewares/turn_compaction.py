"""Complete-turn boundaries, clarification continuation, cutoff candidates, and trigger-count helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from deerflow.agents.human_input import read_human_input_response
from deerflow.agents.middlewares.dynamic_context_middleware import is_dynamic_context_reminder
from deerflow.utils.messages import SUMMARY_MESSAGE_NAME, is_real_user_message

_SUMMARY_TRIGGER_MESSAGE_NAME = "summary"
_ASK_CLARIFICATION_TOOL_NAME = "ask_clarification"


@dataclass(frozen=True)
class _PreparedCompaction:
    source_messages: tuple[AnyMessage, ...]
    snip_messages: tuple[AnyMessage, ...]
    preserved_messages: tuple[AnyMessage, ...]
    previous_summary: str | None
    total_tokens: int


def summary_count_message(summary_text: str) -> HumanMessage:
    return HumanMessage(content=summary_text, name=_SUMMARY_TRIGGER_MESSAGE_NAME)


def messages_for_trigger_count(messages: list[AnyMessage], summary_text: str | None) -> list[AnyMessage]:
    if not summary_text:
        return messages
    return [*messages, summary_count_message(summary_text)]


def context_progress(current: int | float, threshold: int | float) -> float:
    if threshold <= 0:
        raise ValueError("Context trigger threshold must be positive")
    return round(min(100.0, max(0.0, float(current) / float(threshold) * 100.0)), 2)


def is_turn_user(message: AnyMessage) -> bool:
    if is_real_user_message(message):
        return True
    return (
        isinstance(message, HumanMessage)
        and message.name != SUMMARY_MESSAGE_NAME
        and not is_dynamic_context_reminder(message)
        and isinstance(
            message.additional_kwargs.get("human_input_response"),
            dict,
        )
    )


def turn_prefix_start(
    messages: list[AnyMessage],
    user_index: int,
) -> int:
    index = user_index
    while index > 0:
        candidate = messages[index - 1]
        hidden_prefix = isinstance(candidate, (HumanMessage, SystemMessage)) and bool(candidate.additional_kwargs.get("hide_from_ui")) and not is_turn_user(candidate)
        if not is_dynamic_context_reminder(candidate) and not hidden_prefix:
            break
        index -= 1
    return index


def clarification_request_tool_call_id(
    message: AnyMessage,
    request_id: str,
) -> str | None:
    if not isinstance(message, ToolMessage) or message.name != _ASK_CLARIFICATION_TOOL_NAME or message.id != request_id:
        return None
    tool_call_id = message.tool_call_id
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    artifact = message.artifact
    if not isinstance(artifact, Mapping):
        return None
    payload = artifact.get("human_input")
    if not isinstance(payload, Mapping):
        return None
    version = payload.get("version")
    if (
        type(version) is not int
        or version not in (1, 2)
        or payload.get("kind") != "human_input_request"
        or payload.get("source") != _ASK_CLARIFICATION_TOOL_NAME
        or payload.get("request_id") != request_id
        or payload.get("tool_call_id") != tool_call_id
    ):
        return None
    return tool_call_id


def is_clarification_continuation(
    messages: list[AnyMessage],
    *,
    turn_start: int,
    response_index: int,
) -> bool:
    """Match one server-hidden reply to its exact request ToolMessage and tool call."""

    response_message = messages[response_index]
    if not isinstance(response_message, HumanMessage) or response_message.additional_kwargs.get("hide_from_ui") is not True:
        return False
    response = read_human_input_response(response_message.additional_kwargs)
    if response is None or response["source"] != _ASK_CLARIFICATION_TOOL_NAME:
        return False

    request_index = next(
        (index for index in range(response_index - 1, turn_start - 1, -1) if isinstance(messages[index], ToolMessage) and messages[index].name == _ASK_CLARIFICATION_TOOL_NAME),
        None,
    )
    if request_index is None:
        return False
    tool_call_id = clarification_request_tool_call_id(
        messages[request_index],
        response["request_id"],
    )
    if tool_call_id is None:
        return False
    matching_tool_calls = 0
    for message in messages[turn_start:request_index]:
        if not isinstance(message, AIMessage):
            continue
        matching_tool_calls += sum(1 for tool_call in message.tool_calls if isinstance(tool_call, Mapping) and tool_call.get("id") == tool_call_id and tool_call.get("name") == _ASK_CLARIFICATION_TOOL_NAME)
    return matching_tool_calls == 1


def complete_turn_ranges(
    messages: list[AnyMessage],
) -> tuple[tuple[int, int], ...]:
    """Return contiguous complete user turns from the state head."""

    user_indexes = [index for index, message in enumerate(messages) if is_turn_user(message)]
    if not user_indexes:
        return ()

    starts = [0]
    seen_assistant = False
    for index in range(user_indexes[0] + 1, len(messages)):
        message = messages[index]
        if isinstance(message, AIMessage):
            seen_assistant = True
            continue
        if is_turn_user(message) and seen_assistant:
            if is_clarification_continuation(
                messages,
                turn_start=starts[-1],
                response_index=index,
            ):
                continue
            starts.append(turn_prefix_start(messages, index))
            seen_assistant = False

    ranges: list[tuple[int, int]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(messages)
        turn = messages[start:end]
        first_user = next(
            (index for index, message in enumerate(turn) if is_turn_user(message)),
            None,
        )
        if first_user is None:
            break
        assistant_messages = [message for message in turn[first_user + 1 :] if isinstance(message, AIMessage)]
        if not assistant_messages:
            break
        tool_calls = [tool_call for message in assistant_messages for tool_call in message.tool_calls]
        if any(not isinstance(tool_call, dict) or not isinstance(tool_call.get("id"), str) or not tool_call.get("id") for tool_call in tool_calls):
            break
        expected_tool_calls = {tool_call["id"] for tool_call in tool_calls}
        completed_tool_calls = {message.tool_call_id for message in turn[first_user + 1 :] if isinstance(message, ToolMessage) and isinstance(message.tool_call_id, str) and message.tool_call_id}
        response_tail = next(
            (message for message in reversed(turn[first_user + 1 :]) if isinstance(message, (AIMessage, ToolMessage))),
            None,
        )
        if expected_tool_calls != completed_tool_calls or not isinstance(response_tail, AIMessage):
            break
        ranges.append((start, end))
    return tuple(ranges)


def candidate_cutoffs(
    messages: list[AnyMessage],
    requested_cutoff: int,
    *,
    protect_latest_complete_turn: bool = False,
) -> tuple[int, ...]:
    cutoffs: list[int] = []
    expected_start = 0
    complete_turns = complete_turn_ranges(messages)
    if protect_latest_complete_turn and complete_turns:
        complete_turns = complete_turns[:-1]
    for start, end in complete_turns:
        if start != expected_start:
            break
        expected_start = end
        if end <= requested_cutoff:
            cutoffs.append(end)
    return tuple(reversed(cutoffs))


def snip_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    return [message for message in messages if not is_dynamic_context_reminder(message)]
