"""Structure-aware public projection for disabled token tracking."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import BaseMessage

MESSAGE_ADDITIONAL_TRACKING_KEYS = frozenset(
    {
        "token_usage_attribution",
        "subagent_token_usage",
        "subagent_usage_completeness",
        "subagent_usage_receipt_id",
        "subagent_usage_receipt_state",
    }
)
RESPONSE_METADATA_TRACKING_KEYS = frozenset(
    {
        "usage",
        "usage_metadata",
        "token_usage",
    }
)
RUN_EVENT_METADATA_TRACKING_KEYS = frozenset(
    {
        *MESSAGE_ADDITIONAL_TRACKING_KEYS,
        *RESPONSE_METADATA_TRACKING_KEYS,
        "usage_completeness",
    }
)
SUBAGENT_EVENT_TRACKING_KEYS = frozenset(
    {
        "usage",
        "usage_completeness",
    }
)
_SERIALIZED_MESSAGE_TYPES = frozenset(
    {
        "AIMessageChunk",
        "ChatMessageChunk",
        "FunctionMessageChunk",
        "HumanMessageChunk",
        "SystemMessageChunk",
        "ToolMessageChunk",
        "ai",
        "chat",
        "function",
        "human",
        "system",
        "tool",
    }
)


def _is_serialized_message(value: Mapping[str, Any]) -> bool:
    """Recognize LangChain's public ``model_dump`` message envelope."""

    return value.get("type") in _SERIALIZED_MESSAGE_TYPES and "content" in value and isinstance(value.get("additional_kwargs"), Mapping) and isinstance(value.get("response_metadata"), Mapping)


def _without_response_usage(value: Any) -> Any:
    """Copy provider response metadata while removing known usage facts."""

    if isinstance(value, Mapping):
        return {key: _without_response_usage(item) for key, item in value.items() if key not in RESPONSE_METADATA_TRACKING_KEYS}
    if isinstance(value, list):
        return [_without_response_usage(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_response_usage(item) for item in value)
    return value


def _project_serialized_message(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Copy one message envelope without touching content or tool arguments."""

    projected = dict(value)
    projected.pop("usage_metadata", None)
    response_metadata = value.get("response_metadata")
    if isinstance(response_metadata, Mapping):
        projected["response_metadata"] = _without_response_usage(response_metadata)
    additional_kwargs = value.get("additional_kwargs")
    if isinstance(additional_kwargs, Mapping):
        projected["additional_kwargs"] = {key: item for key, item in additional_kwargs.items() if key not in MESSAGE_ADDITIONAL_TRACKING_KEYS}
    return projected


def project_public_token_usage(
    value: Any,
    *,
    tracking_enabled: bool,
) -> Any:
    """Copy containers and sanitize only recognized message envelopes.

    Ordinary mappings retain every key, including legitimate business fields
    named ``usage``. Message content, tool-call arguments, and tool results are
    opaque business payloads and are never traversed once their message
    envelope has been recognized.
    """

    if tracking_enabled:
        return value
    if isinstance(value, BaseMessage):
        return _project_serialized_message(value.model_dump())
    if isinstance(value, Mapping):
        if _is_serialized_message(value):
            return _project_serialized_message(value)
        return {key: project_public_token_usage(item, tracking_enabled=False) for key, item in value.items()}
    if isinstance(value, list):
        return [project_public_token_usage(item, tracking_enabled=False) for item in value]
    if isinstance(value, tuple):
        return tuple(project_public_token_usage(item, tracking_enabled=False) for item in value)
    return value


def _without_transport_metadata_usage(value: Any) -> Any:
    """Copy the non-business metadata lane of a ``messages`` SSE frame."""

    if isinstance(value, Mapping):
        return {key: _without_transport_metadata_usage(item) for key, item in value.items() if key not in RUN_EVENT_METADATA_TRACKING_KEYS}
    if isinstance(value, list):
        return [_without_transport_metadata_usage(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_transport_metadata_usage(item) for item in value)
    return value


def project_public_subagent_event(
    value: Any,
    *,
    tracking_enabled: bool,
) -> Any:
    """Project only recognized ``task_*`` custom-event envelope fields."""

    if tracking_enabled:
        return value
    projected = project_public_token_usage(value, tracking_enabled=False)
    if not isinstance(projected, Mapping):
        return projected
    event_type = projected.get("type")
    if not isinstance(event_type, str) or not event_type.startswith("task_"):
        return projected
    return {key: item for key, item in projected.items() if key not in SUBAGENT_EVENT_TRACKING_KEYS}


def project_public_sse_payload(
    event: str,
    payload: Any,
    *,
    tracking_enabled: bool,
) -> Any:
    """Apply the structure owned by one Worker SSE lane."""

    if tracking_enabled:
        return payload
    mode = event.partition("|")[0]
    if mode == "custom":
        return project_public_subagent_event(
            payload,
            tracking_enabled=False,
        )
    if mode == "messages" and isinstance(payload, (list, tuple)) and len(payload) == 2:
        return [
            project_public_token_usage(
                payload[0],
                tracking_enabled=False,
            ),
            _without_transport_metadata_usage(payload[1]),
        ]
    return project_public_token_usage(payload, tracking_enabled=False)


def project_public_run_event_metadata(
    value: Mapping[str, Any],
    *,
    tracking_enabled: bool,
) -> dict[str, Any]:
    """Remove tracking keys only from a RunJournal metadata envelope."""

    if tracking_enabled:
        return dict(value)
    return {key: project_public_token_usage(item, tracking_enabled=False) for key, item in value.items() if key not in RUN_EVENT_METADATA_TRACKING_KEYS}


def project_public_persisted_run_event(
    value: Mapping[str, Any],
    *,
    tracking_enabled: bool,
) -> dict[str, Any]:
    """Project token metadata from one persisted public Run Event envelope."""

    if tracking_enabled:
        return dict(value)
    projected = dict(value)
    content = project_public_token_usage(
        value.get("content"),
        tracking_enabled=False,
    )
    if value.get("event_type") == "subagent.end" and isinstance(content, Mapping):
        content = {key: item for key, item in content.items() if key not in SUBAGENT_EVENT_TRACKING_KEYS}
    projected["content"] = content
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping):
        projected["metadata"] = project_public_run_event_metadata(
            metadata,
            tracking_enabled=False,
        )
    return projected


__all__ = [
    "MESSAGE_ADDITIONAL_TRACKING_KEYS",
    "RESPONSE_METADATA_TRACKING_KEYS",
    "RUN_EVENT_METADATA_TRACKING_KEYS",
    "SUBAGENT_EVENT_TRACKING_KEYS",
    "project_public_persisted_run_event",
    "project_public_run_event_metadata",
    "project_public_sse_payload",
    "project_public_subagent_event",
    "project_public_token_usage",
]
