"""Canonical serialization for LangChain / LangGraph objects.

Provides a single source of truth for converting LangChain message
objects, Pydantic models, and LangGraph state dicts into plain
JSON-serialisable Python structures.

Consumers: the ``deerflow.runtime.runs`` owners ``worker``, ``stream_delivery``,
and ``goal_continuation`` (SSE publishing) and ``app.gateway.routers.threads``
(REST responses).
"""

from __future__ import annotations

import math
from typing import Any

from deerflow.agents.context_compaction_warning import (
    CONTEXT_COMPACTION_WARNING_STATE_KEY,
)
from deerflow.agents.memory.snip import MEMORY_ARCHIVE_RECEIPT_KEY
from deerflow.agents.middlewares.output_limit_recovery_middleware import (
    OUTPUT_LIMIT_RECOVERY_STATE_KEY,
)
from deerflow.agents.middlewares.token_budget_middleware import (
    OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY,
    TOKEN_BUDGET_USAGE_STATE_KEY,
)
from deerflow.agents.provider_request_contract import (
    CONTEXT_COMPACTION_RECEIPT_STATE_KEY,
    CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
)

_MAX_PUBLIC_VIEWED_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_PUBLIC_SERIALIZATION_DEPTH = 128
_MAX_PUBLIC_SERIALIZATION_NODES = 10_000
_MAX_PUBLIC_SERIALIZATION_ITEMS = 10_000
_MAX_PUBLIC_SERIALIZATION_STRING_CHARS = 1_000_000
_MAX_PUBLIC_SERIALIZATION_TOTAL_STRING_CHARS = 4_000_000
_MAX_PUBLIC_SERIALIZATION_KEY_CHARS = 1_024
_BUDGET_EXHAUSTED = object()
_INTERNAL_STATE_KEYS = frozenset(
    {
        CONTEXT_COMPACTION_RECEIPT_STATE_KEY,
        CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
        CONTEXT_COMPACTION_WARNING_STATE_KEY,
        MEMORY_ARCHIVE_RECEIPT_KEY,
        OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY,
        OUTPUT_LIMIT_RECOVERY_STATE_KEY,
        PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
        PROVIDER_REQUEST_PROFILE_STATE_KEY,
        TOKEN_BUDGET_USAGE_STATE_KEY,
    }
)
_PUBLIC_VIEWED_IMAGE_MIME_TYPES = frozenset(
    {
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
_PUBLIC_VIEWED_IMAGE_ROOTS = (
    "/mnt/user-data/workspace",
    "/mnt/user-data/uploads",
    "/mnt/user-data/outputs",
)


class _TraversalState:
    __slots__ = ("active_ids", "items", "nodes", "string_chars")

    def __init__(self) -> None:
        self.active_ids: set[int] = set()
        self.items = 0
        self.nodes = 0
        self.string_chars = 0

    def reserve_node(self) -> bool:
        if self.nodes >= _MAX_PUBLIC_SERIALIZATION_NODES:
            return False
        self.nodes += 1
        return True

    def reserve_item(self) -> bool:
        if self.items >= _MAX_PUBLIC_SERIALIZATION_ITEMS:
            return False
        self.items += 1
        return True

    def enter_container(self, value: object) -> bool:
        if id(value) in self.active_ids:
            return False
        self.active_ids.add(id(value))
        return True

    def leave(self, value: object) -> None:
        self.active_ids.discard(id(value))

    def bound_string(self, value: str, *, key: bool = False) -> str:
        remaining = max(
            0,
            _MAX_PUBLIC_SERIALIZATION_TOTAL_STRING_CHARS - self.string_chars,
        )
        per_value_limit = _MAX_PUBLIC_SERIALIZATION_KEY_CHARS if key else _MAX_PUBLIC_SERIALIZATION_STRING_CHARS
        bounded = value[: min(per_value_limit, remaining)]
        self.string_chars += len(bounded)
        return bounded


def _is_data_url_image_block(block: object) -> bool:
    if not isinstance(block, dict) or block.get("type") != "image_url":
        return False
    image_url = block.get("image_url")
    raw_url = image_url.get("url") if isinstance(image_url, dict) else image_url
    return isinstance(raw_url, str) and raw_url.lstrip().lower().startswith("data:")


def _is_hidden_message(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    additional_kwargs = value.get("additional_kwargs")
    return isinstance(additional_kwargs, dict) and additional_kwargs.get("hide_from_ui") is True


def _serialize_lc_object(
    obj: Any,
    *,
    state: _TraversalState,
    depth: int,
    strip_data_url_blocks: bool = False,
) -> Any:
    if not state.reserve_node():
        return _BUDGET_EXHAUSTED
    if depth > _MAX_PUBLIC_SERIALIZATION_DEPTH:
        return _BUDGET_EXHAUSTED
    if obj is None:
        return None
    if isinstance(obj, str):
        return state.bound_string(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (int, bool)):
        return obj

    if not state.enter_container(obj):
        return None
    try:
        if isinstance(obj, dict):
            result: dict[str, Any] = {}
            hidden_message = _is_hidden_message(obj)
            for key, value in obj.items():
                if not state.reserve_item():
                    break
                if not isinstance(key, str):
                    continue
                if key in _INTERNAL_STATE_KEYS:
                    continue
                bounded_key = state.bound_string(key, key=True)
                if bounded_key in result:
                    continue
                serialized = _serialize_lc_object(
                    value,
                    state=state,
                    depth=depth + 1,
                    strip_data_url_blocks=hidden_message and key == "content",
                )
                if serialized is _BUDGET_EXHAUSTED:
                    break
                result[bounded_key] = serialized
            return result
        if isinstance(obj, (list, tuple)):
            result_list: list[Any] = []
            for item in obj:
                if not state.reserve_item():
                    break
                if strip_data_url_blocks and _is_data_url_image_block(item):
                    continue
                serialized = _serialize_lc_object(
                    item,
                    state=state,
                    depth=depth + 1,
                )
                if serialized is _BUDGET_EXHAUSTED:
                    break
                if strip_data_url_blocks and _is_data_url_image_block(serialized):
                    continue
                result_list.append(serialized)
            return result_list
        # Pydantic v2
        if hasattr(obj, "model_dump"):
            try:
                dumped = obj.model_dump()
            except Exception:
                pass
            else:
                serialized = _serialize_lc_object(
                    dumped,
                    state=state,
                    depth=depth + 1,
                    strip_data_url_blocks=strip_data_url_blocks,
                )
                return None if serialized is _BUDGET_EXHAUSTED else serialized
        # Pydantic v1 / older objects
        if hasattr(obj, "dict"):
            try:
                dumped = obj.dict()
            except Exception:
                pass
            else:
                serialized = _serialize_lc_object(
                    dumped,
                    state=state,
                    depth=depth + 1,
                    strip_data_url_blocks=strip_data_url_blocks,
                )
                return None if serialized is _BUDGET_EXHAUSTED else serialized
        # Interrupt is a __slots__ class — no model_dump/dict/__dict__, so it
        # would reach str() and produce a malformed payload.
        try:
            from langgraph.types import Interrupt
        except ImportError:
            pass
        else:
            if isinstance(obj, Interrupt):
                serialized = _serialize_lc_object(
                    {
                        "value": obj.value,
                        "id": getattr(obj, "id", None),
                    },
                    state=state,
                    depth=depth + 1,
                )
                return None if serialized is _BUDGET_EXHAUSTED else serialized
        # Last resort
        try:
            rendered = str(obj)
        except Exception:
            rendered = repr(obj)
        return state.bound_string(rendered)
    finally:
        state.leave(obj)


def serialize_lc_object(
    obj: Any,
    *,
    _state: _TraversalState | None = None,
    _depth: int = 0,
) -> Any:
    """Recursively serialize a LangChain object to a JSON-serialisable dict."""

    state = _state or _TraversalState()
    serialized = _serialize_lc_object(obj, state=state, depth=_depth)
    return None if serialized is _BUDGET_EXHAUSTED else serialized


def serialize_channel_values(channel_values: dict[str, Any]) -> dict[str, Any]:
    """Serialize channel values, stripping internal LangGraph keys.

    Only ``__pregel_*`` keys are removed — ``__interrupt__`` is deliberately
    preserved so the LangGraph SDK can detect interrupt events from values
    chunks (see issue #3595).
    """
    filtered: dict[str, Any] = {}
    for index, (key, value) in enumerate(channel_values.items()):
        if index >= _MAX_PUBLIC_SERIALIZATION_ITEMS:
            break
        if not isinstance(key, str) or key.startswith("__pregel_") or key in _INTERNAL_STATE_KEYS:
            continue
        filtered[key] = value
    result = serialize_lc_object(filtered)
    return result if isinstance(result, dict) else {}


def strip_data_url_image_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove ``data:``-scheme ``image_url`` blocks from *hide_from_ui* messages.

    The history and run-wait endpoints return checkpoint-persisted messages to
    the frontend. Current ``ViewImageMiddleware`` injects image bytes only into
    an ephemeral model request, but legacy checkpoints and custom middleware
    may still contain hidden base64 image messages. Keep this API-boundary
    filter as defense in depth so those payloads are never sent over the wire.

    Only content blocks of type ``image_url`` whose URL starts with ``data:``
    are stripped.  Text blocks, ``https://`` image URLs, and non-hidden
    messages are left untouched so that message ordering and count are
    preserved.
    """
    result: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            result.append(msg)
            continue

        # Only touch messages explicitly flagged as hidden from the UI.
        additional_kwargs = msg.get("additional_kwargs")
        if not (isinstance(additional_kwargs, dict) and additional_kwargs.get("hide_from_ui") is True):
            result.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        # Filter out image_url blocks with a case-insensitive data: scheme.
        filtered = [block for block in content if not _is_data_url_image_block(block)]
        result.append({**msg, "content": filtered})
    return result


def _viewed_images_for_api(value: object) -> dict[str, dict[str, object]]:
    """Expose only bounded observation metadata, never bytes or locators."""

    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, object]] = {}
    for index, (image_path, image_data) in enumerate(value.items()):
        if index >= _MAX_PUBLIC_SERIALIZATION_ITEMS:
            break
        if not isinstance(image_path, str) or not isinstance(image_data, dict):
            continue
        mime_type = image_data.get("mime_type")
        size = image_data.get("size")
        sha256 = image_data.get("sha256")
        file_ref = image_data.get("file_ref")
        project_id = file_ref.get("project_id") if isinstance(file_ref, dict) else None
        owner_user_id = file_ref.get("owner_user_id") if isinstance(file_ref, dict) else None
        already_projected = file_ref is None and set(image_data) <= {
            "mime_type",
            "size",
            "sha256",
        }
        if (
            not _is_public_viewed_image_path(image_path)
            or mime_type not in _PUBLIC_VIEWED_IMAGE_MIME_TYPES
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= _MAX_PUBLIC_VIEWED_IMAGE_BYTES
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or (
                not already_projected
                and (
                    not isinstance(file_ref, dict)
                    or file_ref.get("path") != image_path
                    or not isinstance(file_ref.get("sandbox_id"), str)
                    or not file_ref["sandbox_id"]
                    or not isinstance(file_ref.get("run_id"), str)
                    or not file_ref["run_id"]
                    or (project_id is None) != (owner_user_id is None)
                    or (project_id is not None and (not isinstance(project_id, str) or not project_id or not isinstance(owner_user_id, str) or not owner_user_id))
                )
            )
        ):
            continue
        result[image_path] = {
            "mime_type": mime_type,
            "size": size,
            "sha256": sha256,
        }
    return result


def _is_public_viewed_image_path(image_path: str) -> bool:
    if len(image_path) > _MAX_PUBLIC_SERIALIZATION_KEY_CHARS or "\x00" in image_path or "\\" in image_path:
        return False
    for root in _PUBLIC_VIEWED_IMAGE_ROOTS:
        prefix = f"{root}/"
        if not image_path.startswith(prefix):
            continue
        suffix = image_path[len(prefix) :]
        return bool(suffix) and all(part not in {"", ".", ".."} for part in suffix.split("/"))
    return False


def _project_serialized_payload_for_api(
    value: Any,
    *,
    _state: _TraversalState | None = None,
    _depth: int = 0,
    _strip_data_url_blocks: bool = False,
) -> Any:
    """Recursively remove private image bytes and locators from stream frames."""

    state = _state or _TraversalState()
    if not state.reserve_node():
        return _BUDGET_EXHAUSTED
    if _depth > _MAX_PUBLIC_SERIALIZATION_DEPTH:
        return _BUDGET_EXHAUSTED
    if value is None:
        return None
    if isinstance(value, str):
        return state.bound_string(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (int, bool)):
        return value
    if not isinstance(value, (dict, list, tuple)):
        return state.bound_string(str(value))
    if not state.enter_container(value):
        return None
    try:
        if isinstance(value, (list, tuple)):
            result_list: list[Any] = []
            for item in value:
                if not state.reserve_item():
                    break
                if _strip_data_url_blocks and _is_data_url_image_block(item):
                    continue
                projected = _project_serialized_payload_for_api(
                    item,
                    _state=state,
                    _depth=_depth + 1,
                )
                if projected is _BUDGET_EXHAUSTED:
                    break
                if _strip_data_url_blocks and _is_data_url_image_block(projected):
                    continue
                result_list.append(projected)
            return result_list

        result: dict[str, Any] = {}
        hidden_message = _is_hidden_message(value)
        for key, item in value.items():
            if not state.reserve_item():
                break
            if not isinstance(key, str):
                continue
            if key in _INTERNAL_STATE_KEYS:
                continue
            bounded_key = state.bound_string(key, key=True)
            if bounded_key in result:
                continue
            if key == "viewed_images":
                projected = _project_serialized_payload_for_api(
                    _viewed_images_for_api(item),
                    _state=state,
                    _depth=_depth + 1,
                )
            else:
                projected = _project_serialized_payload_for_api(
                    item,
                    _state=state,
                    _depth=_depth + 1,
                    _strip_data_url_blocks=hidden_message and key == "content",
                )
            if projected is _BUDGET_EXHAUSTED:
                break
            result[bounded_key] = projected

        content = result.get("content")
        if hidden_message and isinstance(content, list):
            result["content"] = [block for block in content if not _is_data_url_image_block(block)]
        return result
    finally:
        state.leave(value)


def serialize_channel_values_for_api(channel_values: dict[str, Any]) -> dict[str, Any]:
    """Serialize state while removing all persisted image bytes and locators.

    Convenience wrapper combining :func:`serialize_channel_values` with
    :func:`strip_data_url_image_blocks`.  Use this in all REST endpoints
    that return channel values to the frontend so that ``data:``-scheme
    base64 image payloads are never sent over the wire. Legacy
    ``viewed_images.base64`` entries are dropped entirely; current entries
    expose only MIME, size, and digest metadata.
    """
    projected = _project_serialized_payload_for_api(serialize_channel_values(channel_values))
    return projected if isinstance(projected, dict) else {}


def _message_envelope_state() -> _TraversalState:
    """Pre-charge the fixed two-slot messages envelope.

    The metadata root is reserved before serializing the potentially wide
    chunk. This keeps the public ``[chunk, metadata]`` shape intact without
    letting either side obtain an independent traversal budget.
    """

    state = _TraversalState()
    state.reserve_node()  # outer list
    state.reserve_item()  # chunk slot
    state.reserve_item()  # metadata slot
    state.reserve_node()  # metadata fallback root
    return state


def serialize_messages_tuple(obj: Any) -> Any:
    """Serialize a messages-mode tuple ``(chunk, metadata)``."""
    if isinstance(obj, tuple) and len(obj) == 2:
        chunk, metadata = obj
        state = _message_envelope_state()
        serialized_chunk = _serialize_lc_object(
            chunk,
            state=state,
            depth=1,
        )
        serialized_metadata = _serialize_lc_object(
            metadata if isinstance(metadata, dict) else {},
            state=state,
            depth=1,
        )
        return [
            None if serialized_chunk is _BUDGET_EXHAUSTED else serialized_chunk,
            serialized_metadata if isinstance(serialized_metadata, dict) else {},
        ]
    return serialize_lc_object(obj)


def _project_messages_tuple_for_api(value: list[Any]) -> list[Any]:
    state = _message_envelope_state()
    projected_chunk = _project_serialized_payload_for_api(
        value[0],
        _state=state,
        _depth=1,
    )
    projected_metadata = _project_serialized_payload_for_api(
        value[1],
        _state=state,
        _depth=1,
    )
    return [
        None if projected_chunk is _BUDGET_EXHAUSTED else projected_chunk,
        projected_metadata if isinstance(projected_metadata, dict) else {},
    ]


def serialize(obj: Any, *, mode: str = "") -> Any:
    """Serialize LangChain objects with mode-specific handling.

    * ``messages`` — obj is ``(message_chunk, metadata_dict)``
    * ``values`` — obj is the full state dict; ``__pregel_*`` keys stripped and
      base64 ``data:`` image blocks dropped from hide_from_ui messages
    * everything else — recursive ``model_dump()`` / ``dict()`` fallback

    Every public mode then receives the same recursive image-state projection,
    including nested ``updates``, ``debug``, ``tasks``, ``checkpoints``, and
    ``custom`` frames.
    """
    if mode == "messages":
        is_messages_tuple = isinstance(obj, tuple) and len(obj) == 2
        result = serialize_messages_tuple(obj)
        if is_messages_tuple and isinstance(result, list) and len(result) == 2:
            return _project_messages_tuple_for_api(result)
        projected = _project_serialized_payload_for_api(result)
        return None if projected is _BUDGET_EXHAUSTED else projected
    if mode == "values":
        # ``values`` snapshots stream the full state to the frontend, so they
        # must drop base64 image payloads the same way the REST endpoints do.
        if isinstance(obj, dict):
            return serialize_channel_values_for_api(obj)
        projected = _project_serialized_payload_for_api(serialize_lc_object(obj))
        return None if projected is _BUDGET_EXHAUSTED else projected
    projected = _project_serialized_payload_for_api(serialize_lc_object(obj))
    return None if projected is _BUDGET_EXHAUSTED else projected
