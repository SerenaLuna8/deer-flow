"""Deterministic semantic serialization for WorkflowSpec v1."""

from __future__ import annotations

import hashlib
import json
import struct
import unicodedata
from typing import Any

from deerflow.workflows.contracts import (
    MAX_SAFE_JSON_INTEGER,
    JsonValue,
    WorkflowSpecV1,
    workflow_spec_public_projection_v1,
)

# Changing this identifier or its spelling rules is a checksum migration event.
CANONICAL_BINARY64_ALGORITHM = "ieee754-binary64-exact-decimal-v1"

_BINARY64_EXPONENT_MASK = 0x7FF
_BINARY64_FRACTION_MASK = (1 << 52) - 1
_BINARY64_IMPLICIT_BIT = 1 << 52
_CANONICAL_TEXT_CHUNK_SIZE = 1_024
_JSON_CHARACTER_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class CanonicalJsonUtf8BudgetExceeded(ValueError):
    """Raised before a canonical JSON value can exceed its UTF-8 budget."""


class _CanonicalUtf8Writer:
    def __init__(self, max_utf8_bytes: int) -> None:
        if type(max_utf8_bytes) is not int or max_utf8_bytes < 0:
            raise ValueError("canonical JSON UTF-8 byte budget must be a non-negative integer")
        self._max_utf8_bytes = max_utf8_bytes
        self._pieces: list[str] = []
        self.utf8_bytes = 0

    def append(self, value: str) -> None:
        byte_count = len(value.encode("utf-8"))
        if self.utf8_bytes + byte_count > self._max_utf8_bytes:
            raise CanonicalJsonUtf8BudgetExceeded("canonical JSON exceeds the UTF-8 byte budget")
        self._pieces.append(value)
        self.utf8_bytes += byte_count

    def finish(self) -> str:
        return "".join(self._pieces)


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if any(0xD800 <= ord(character) <= 0xDFFF for character in normalized):
        raise ValueError("canonical Workflow JSON supports only Unicode scalar values")
    return normalized


def _canonical_number(value: int | float) -> str:
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_JSON_INTEGER:
            raise ValueError("canonical Workflow JSON integer exceeds the cross-runtime safe range")
        return str(value)

    bits = int.from_bytes(struct.pack(">d", value), byteorder="big")
    negative = bool(bits >> 63)
    exponent_bits = (bits >> 52) & _BINARY64_EXPONENT_MASK
    fraction = bits & _BINARY64_FRACTION_MASK
    if exponent_bits == _BINARY64_EXPONENT_MASK:
        raise ValueError("canonical Workflow JSON does not support non-finite numbers")
    if exponent_bits == 0 and fraction == 0:
        return "0"

    if exponent_bits == 0:
        significand = fraction
        binary_exponent = -1074
    else:
        significand = _BINARY64_IMPLICIT_BIT | fraction
        binary_exponent = exponent_bits - 1023 - 52

    if binary_exponent >= 0:
        integer = significand << binary_exponent
        if integer > MAX_SAFE_JSON_INTEGER:
            raise ValueError("canonical Workflow JSON integer exceeds the cross-runtime safe range")
        return f"{'-' if negative else ''}{integer}"

    denominator_power = -binary_exponent
    trailing_zero_bits = (significand & -significand).bit_length() - 1
    common_power = min(denominator_power, trailing_zero_bits)
    significand >>= common_power
    denominator_power -= common_power

    if denominator_power == 0:
        if significand > MAX_SAFE_JSON_INTEGER:
            raise ValueError("canonical Workflow JSON integer exceeds the cross-runtime safe range")
        return f"{'-' if negative else ''}{significand}"

    digits = str(significand * 5**denominator_power)
    scientific_exponent = len(digits) - 1 - denominator_power
    coefficient = digits[0] if len(digits) == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{'-' if negative else ''}{coefficient}e{scientific_exponent}"


def _canonical_encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int | float):
        return _canonical_number(value)
    if isinstance(value, str):
        return json.dumps(_normalize_text(value), ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical Workflow JSON object keys must be strings")
        normalized_items: list[tuple[str, Any]] = []
        normalized_keys: set[str] = set()
        for key, nested_value in value.items():
            normalized_key = _normalize_text(key)
            if normalized_key in normalized_keys:
                raise ValueError("Unicode normalization produced duplicate JSON keys")
            normalized_keys.add(normalized_key)
            normalized_items.append((normalized_key, nested_value))
        normalized_items.sort(key=lambda item: item[0])
        return "{" + ",".join(f"{_canonical_encode(key)}:{_canonical_encode(nested_value)}" for key, nested_value in normalized_items) + "}"
    raise TypeError(f"unsupported canonical Workflow JSON value: {type(value).__name__}")


def _canonical_encode_text_with_budget(value: str, writer: _CanonicalUtf8Writer) -> None:
    normalized = _normalize_text(value)
    writer.append('"')
    buffered: list[str] = []
    buffered_characters = 0
    for character in normalized:
        escaped = _JSON_CHARACTER_ESCAPES.get(character)
        if escaped is None:
            code_point = ord(character)
            escaped = f"\\u{code_point:04x}" if code_point < 0x20 else character
        buffered.append(escaped)
        buffered_characters += len(escaped)
        if buffered_characters >= _CANONICAL_TEXT_CHUNK_SIZE:
            writer.append("".join(buffered))
            buffered.clear()
            buffered_characters = 0
    if buffered:
        writer.append("".join(buffered))
    writer.append('"')


def _canonical_encode_with_budget(value: Any, writer: _CanonicalUtf8Writer) -> None:
    if value is None:
        writer.append("null")
        return
    if value is True:
        writer.append("true")
        return
    if value is False:
        writer.append("false")
        return
    if isinstance(value, int | float):
        writer.append(_canonical_number(value))
        return
    if isinstance(value, str):
        _canonical_encode_text_with_budget(value, writer)
        return
    if isinstance(value, list):
        writer.append("[")
        for index, item in enumerate(value):
            if index:
                writer.append(",")
            _canonical_encode_with_budget(item, writer)
        writer.append("]")
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical Workflow JSON object keys must be strings")
        normalized_items: list[tuple[str, Any]] = []
        normalized_keys: set[str] = set()
        for key, nested_value in value.items():
            normalized_key = _normalize_text(key)
            if normalized_key in normalized_keys:
                raise ValueError("Unicode normalization produced duplicate JSON keys")
            normalized_keys.add(normalized_key)
            normalized_items.append((normalized_key, nested_value))
        normalized_items.sort(key=lambda item: item[0])
        writer.append("{")
        for index, (key, nested_value) in enumerate(normalized_items):
            if index:
                writer.append(",")
            _canonical_encode_text_with_budget(key, writer)
            writer.append(":")
            _canonical_encode_with_budget(nested_value, writer)
        writer.append("}")
        return
    raise TypeError(f"unsupported canonical Workflow JSON value: {type(value).__name__}")


def _sort_declarations(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_ids: set[str] = set()
    for value in values:
        normalized_id = _normalize_text(str(value.get("id", "")))
        if normalized_id in normalized_ids:
            raise ValueError("Unicode normalization produced duplicate declaration IDs")
        normalized_ids.add(normalized_id)
    return sorted(values, key=lambda value: _normalize_text(str(value.get("id", ""))))


def semantic_payload(spec: WorkflowSpecV1) -> dict[str, Any]:
    """Return the execution-semantic v1 projection used for checksums.

    Only top-level declaration arrays are order-normalized.  Nested arrays are
    kept byte-for-byte in authored order because branch order, aggregate
    candidates, loop variables, templates, and similar arrays may be semantic.
    """

    payload = workflow_spec_public_projection_v1(spec)

    nodes = payload["nodes"]
    for node in nodes:
        node.pop("custom_label", None)
        node.pop("description", None)
    payload["nodes"] = _sort_declarations(nodes)

    transitions = payload["transitions"]
    payload["transitions"] = _sort_declarations(transitions)

    workflow_inputs = payload["workflow_inputs"]
    for declaration in workflow_inputs:
        declaration.pop("description", None)
    payload["workflow_inputs"] = _sort_declarations(workflow_inputs)

    workflow_outputs = payload["workflow_outputs"]
    for declaration in workflow_outputs:
        declaration.pop("description", None)
    payload["workflow_outputs"] = _sort_declarations(workflow_outputs)

    payload["credential_slots"] = _sort_declarations(payload["credential_slots"])
    return payload


def semantic_canonical_json(spec: WorkflowSpecV1) -> str:
    """Serialize a WorkflowSpec to its fixed, Unicode-preserving JSON form."""

    return _canonical_encode(semantic_payload(spec))


def canonical_json_value(value: JsonValue) -> str:
    """Serialize one portable JSON value using the Workflow canonical form."""

    return _canonical_encode(value)


def canonical_json_value_with_utf8_budget(value: JsonValue, *, max_utf8_bytes: int) -> tuple[str, int]:
    """Serialize canonical JSON while stopping as soon as its UTF-8 budget is crossed."""

    writer = _CanonicalUtf8Writer(max_utf8_bytes)
    _canonical_encode_with_budget(value, writer)
    return writer.finish(), writer.utf8_bytes


def semantic_checksum(spec: WorkflowSpecV1) -> str:
    """Return the lowercase SHA-256 digest of semantic canonical UTF-8 JSON."""

    canonical = semantic_canonical_json(spec)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
