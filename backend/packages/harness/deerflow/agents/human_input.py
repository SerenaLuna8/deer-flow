"""Structured human-input message metadata helpers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal, NotRequired, TypedDict

HUMAN_INPUT_RESPONSE_KEY = "human_input_response"
type HumanInputFormValue = str | int | float | bool | list[str]


class HumanInputTextResponse(TypedDict):
    version: Literal[1]
    kind: Literal["human_input_response"]
    source: str
    request_id: str
    response_kind: Literal["text"]
    value: str
    form_values: NotRequired[dict[str, HumanInputFormValue]]


class HumanInputOptionResponse(TypedDict):
    version: Literal[1]
    kind: Literal["human_input_response"]
    source: str
    request_id: str
    response_kind: Literal["option"]
    option_id: str
    value: str


HumanInputResponse = HumanInputTextResponse | HumanInputOptionResponse


def _non_empty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _form_values(value: object) -> dict[str, HumanInputFormValue] | None:
    if not isinstance(value, Mapping):
        return None
    parsed: dict[str, HumanInputFormValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            return None
        if isinstance(item, bool) or isinstance(item, str):
            parsed[key] = item
        elif isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
            parsed[key] = item
        elif isinstance(item, list) and item and all(isinstance(entry, str) and entry.strip() for entry in item):
            parsed[key] = list(item)
        else:
            return None
    return parsed


def read_human_input_response(additional_kwargs: Mapping[str, object] | None) -> HumanInputResponse | None:
    """Read a valid human-input response payload from message metadata."""
    if not additional_kwargs:
        return None
    raw = additional_kwargs.get(HUMAN_INPUT_RESPONSE_KEY)
    if not isinstance(raw, Mapping):
        return None
    if raw.get("version") != 1 or raw.get("kind") != "human_input_response":
        return None

    source = _non_empty_string(raw.get("source"))
    request_id = _non_empty_string(raw.get("request_id"))
    value = _non_empty_string(raw.get("value"))
    if source is None or request_id is None or value is None:
        return None

    response_kind = raw.get("response_kind")
    if response_kind == "text":
        raw_form_values = raw.get("form_values")
        form_values = _form_values(raw_form_values)
        if raw_form_values is not None and form_values is None:
            return None
        response: HumanInputTextResponse = {
            "version": 1,
            "kind": "human_input_response",
            "source": source,
            "request_id": request_id,
            "response_kind": "text",
            "value": value,
        }
        if form_values is not None:
            response["form_values"] = form_values
        return response
    if response_kind == "option":
        option_id = _non_empty_string(raw.get("option_id"))
        if option_id is None:
            return None
        return {
            "version": 1,
            "kind": "human_input_response",
            "source": source,
            "request_id": request_id,
            "response_kind": "option",
            "option_id": option_id,
            "value": value,
        }
    return None
