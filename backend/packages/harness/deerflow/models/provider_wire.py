"""Provider-owned message serialization for Context request measurement.

This module is the sole bridge from Agent request metering to Provider SDK
serialization. Keeping it under ``deerflow.models`` preserves the Provider SDK
import boundary while letting the final guard, idle Profile, and Context cost
adapter share exactly one versioned wire projection.
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage

OPENAI_CHAT_WIRE_ADAPTERS = frozenset(
    {
        "deepseek",
        "openai",
        "vllm",
    }
)
OPENAI_RESPONSES_WIRE_ADAPTERS = frozenset({"openai_responses"})
SUPPORTED_PROVIDER_WIRE_ADAPTERS = frozenset(
    {
        *OPENAI_CHAT_WIRE_ADAPTERS,
        *OPENAI_RESPONSES_WIRE_ADAPTERS,
        "anthropic",
    }
)


def _openai_wire_message_payload(
    message: BaseMessage,
    *,
    provider_adapter: str,
) -> dict[str, object]:
    from langchain_openai.chat_models.base import _convert_message_to_dict

    payload = _convert_message_to_dict(message)
    if not isinstance(payload, dict):
        raise TypeError("OpenAI-family message conversion returned a non-mapping")
    if not isinstance(message, AIMessage):
        return payload
    if provider_adapter == "deepseek":
        from deerflow.models.assistant_payload_replay import (
            restore_reasoning_content,
        )

        restore_reasoning_content(payload, message)
    elif provider_adapter == "vllm":
        from deerflow.models.vllm_provider import _restore_reasoning_field

        _restore_reasoning_field(payload, message)
    return payload


def provider_visible_messages_payload(
    messages: Sequence[BaseMessage],
    *,
    provider_adapter: str,
) -> tuple[dict[str, object], ...]:
    """Serialize messages with the frozen Provider adapter's wire semantics."""

    if provider_adapter in OPENAI_CHAT_WIRE_ADAPTERS:
        return tuple(
            _openai_wire_message_payload(
                message,
                provider_adapter=provider_adapter,
            )
            for message in messages
        )
    if provider_adapter in OPENAI_RESPONSES_WIRE_ADAPTERS:
        from langchain_openai.chat_models.base import (
            _construct_responses_api_input,
        )

        return tuple(_construct_responses_api_input(messages))
    if provider_adapter == "anthropic":
        from langchain_anthropic.chat_models import _format_messages

        system, formatted = _format_messages(messages)
        payloads: list[dict[str, object]] = []
        if system is not None:
            payloads.append({"role": "system", "content": system})
        payloads.extend(formatted)
        return tuple(payloads)
    raise ValueError("Provider message projection is unsupported")


def provider_visible_message_payload(
    message: BaseMessage,
    *,
    provider_adapter: str = "openai",
) -> dict[str, object]:
    """Serialize one message with the frozen Provider adapter's wire semantics."""

    payloads = provider_visible_messages_payload(
        (message,),
        provider_adapter=provider_adapter,
    )
    if len(payloads) != 1:
        if provider_adapter in OPENAI_RESPONSES_WIRE_ADAPTERS:
            return {"responses_input": list(payloads)}
        raise ValueError("Provider message projection is not one-to-one")
    return payloads[0]


__all__ = [
    "OPENAI_CHAT_WIRE_ADAPTERS",
    "OPENAI_RESPONSES_WIRE_ADAPTERS",
    "SUPPORTED_PROVIDER_WIRE_ADAPTERS",
    "provider_visible_message_payload",
    "provider_visible_messages_payload",
]
