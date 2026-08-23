"""Helpers for keeping AIMessage tool-call metadata consistent."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from langchain_core.messages import AIMessage


def _raw_tool_call_id(raw_tool_call: Any) -> str | None:
    if not isinstance(raw_tool_call, dict):
        return None

    raw_id = raw_tool_call.get("id")
    return raw_id if isinstance(raw_id, str) and raw_id else None


def clone_ai_message_with_tool_calls(
    message: AIMessage,
    tool_calls: list[dict[str, Any]],
    *,
    content: Any | None = None,
) -> AIMessage:
    """Clone an AIMessage while keeping raw provider tool-call metadata in sync."""
    kept_ids = {tc["id"] for tc in tool_calls if isinstance(tc.get("id"), str) and tc["id"]}

    update: dict[str, Any] = {"tool_calls": tool_calls}
    if content is not None:
        update["content"] = content

    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    raw_tool_calls = additional_kwargs.get("tool_calls")
    if isinstance(raw_tool_calls, list):
        synced_raw_tool_calls = [raw_tc for raw_tc in raw_tool_calls if _raw_tool_call_id(raw_tc) in kept_ids]
        if synced_raw_tool_calls:
            additional_kwargs["tool_calls"] = synced_raw_tool_calls
        else:
            additional_kwargs.pop("tool_calls", None)

    if not tool_calls:
        additional_kwargs.pop("function_call", None)

    update["additional_kwargs"] = additional_kwargs

    response_metadata = dict(getattr(message, "response_metadata", {}) or {})
    if not tool_calls and response_metadata.get("finish_reason") == "tool_calls":
        response_metadata["finish_reason"] = "stop"
    update["response_metadata"] = response_metadata

    return message.model_copy(update=update)


def clone_ai_message_with_tool_call_occurrences(
    message: AIMessage,
    kept_indices: Collection[int],
    *,
    content: Any | None = None,
) -> AIMessage:
    """Clone an AIMessage after retaining exact decoded-call occurrences.

    Tool-call IDs are provider correlation metadata and may be reused.  Exact
    decoded indices are therefore the only safe way to distinguish a retained
    occurrence from a rejected sibling with the same ID.  When raw provider
    metadata cannot be aligned exactly and reused IDs make an ID-only fallback
    ambiguous, the raw list is removed instead of accidentally reintroducing a
    rejected proposal.  Decoded ``invalid_tool_calls`` remain untouched.
    """

    original_calls = list(message.tool_calls or [])
    keep = set(kept_indices)
    if any(not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(original_calls) for index in keep):
        raise ValueError("kept tool-call occurrence index is out of range")

    kept_calls = [call for index, call in enumerate(original_calls) if index in keep]
    update: dict[str, Any] = {"tool_calls": kept_calls}
    if content is not None:
        update["content"] = content

    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    raw_tool_calls = additional_kwargs.get("tool_calls")
    if isinstance(raw_tool_calls, list):
        if len(raw_tool_calls) == len(original_calls):
            synced = [raw_call for index, raw_call in enumerate(raw_tool_calls) if index in keep]
            if synced:
                additional_kwargs["tool_calls"] = synced
            else:
                additional_kwargs.pop("tool_calls", None)
        else:
            kept_ids = {call_id for index, call in enumerate(original_calls) if index in keep and isinstance((call_id := call.get("id")), str) and call_id}
            dropped_ids = {call_id for index, call in enumerate(original_calls) if index not in keep and isinstance((call_id := call.get("id")), str) and call_id}
            ambiguous_ids = kept_ids & dropped_ids
            blocked_raw_ids = dropped_ids | ambiguous_ids
            synced = [raw_call for raw_call in raw_tool_calls if _raw_tool_call_id(raw_call) not in blocked_raw_ids]
            if synced:
                additional_kwargs["tool_calls"] = synced
            else:
                additional_kwargs.pop("tool_calls", None)

    if not kept_calls:
        additional_kwargs.pop("function_call", None)
    update["additional_kwargs"] = additional_kwargs

    response_metadata = dict(getattr(message, "response_metadata", {}) or {})
    if not kept_calls and response_metadata.get("finish_reason") == "tool_calls":
        response_metadata["finish_reason"] = "stop"
    update["response_metadata"] = response_metadata
    return message.model_copy(update=update)
