"""Cycle-free checkpoint protocol for Provider-request metering."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Final

from langchain_core.messages import BaseMessage, message_to_dict

PROVIDER_REQUEST_PROFILE_STATE_KEY: Final[str] = "provider_request_profile"
PROVIDER_REQUEST_MEASUREMENT_STATE_KEY: Final[str] = "provider_request_measurement"
CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY: Final[str] = "context_projection_snapshot"
CONTEXT_COMPACTION_RECEIPT_STATE_KEY: Final[str] = "context_compaction_receipt"


def _checkpoint_message_payload(value: object) -> dict[str, object]:
    if isinstance(value, BaseMessage):
        payload = message_to_dict(value)
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("checkpoint Provider response message is invalid")
    # LangGraph may assign a message id while reducing the model response into
    # state. The id is not Provider response content and therefore cannot be
    # part of the proof computed before that reduction.
    payload.pop("id", None)
    data = payload.get("data")
    if isinstance(data, Mapping):
        normalized_data = dict(data)
        normalized_data.pop("id", None)
        payload["data"] = normalized_data
    return payload


def provider_response_digest(messages: Sequence[object]) -> str:
    """Hash Provider response messages without persisting their bodies."""

    if isinstance(messages, (str, bytes, bytearray)) or not messages:
        raise ValueError("Provider response proof requires at least one message")
    material = {
        "messages": [_checkpoint_message_payload(message) for message in messages],
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CONTEXT_COMPACTION_RECEIPT_STATE_KEY",
    "CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY",
    "PROVIDER_REQUEST_MEASUREMENT_STATE_KEY",
    "PROVIDER_REQUEST_PROFILE_STATE_KEY",
    "provider_response_digest",
]
