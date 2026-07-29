"""Canonical serialization for LangChain / LangGraph objects.

Provides a single source of truth for converting LangChain message
objects, Pydantic models, and LangGraph state dicts into plain
JSON-serialisable Python structures.

Consumers: ``deerflow.runtime.runs.worker`` (SSE publishing) and
``app.gateway.routers.threads`` (REST responses).
"""

from __future__ import annotations

from typing import Any

_MAX_PUBLIC_VIEWED_IMAGE_BYTES = 20 * 1024 * 1024
_PUBLIC_VIEWED_IMAGE_MIME_TYPES = frozenset(
    {
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


def serialize_lc_object(obj: Any) -> Any:
    """Recursively serialize a LangChain object to a JSON-serialisable dict."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: serialize_lc_object(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize_lc_object(item) for item in obj]
    # Pydantic v2
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    # Pydantic v1 / older objects
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    # Interrupt is a __slots__ class — no model_dump/dict/__dict__, so it
    # would reach str() and produce a malformed payload.
    try:
        from langgraph.types import Interrupt
    except ImportError:
        pass
    else:
        if isinstance(obj, Interrupt):
            return serialize_lc_object(
                {
                    "value": obj.value,
                    "id": getattr(obj, "id", None),
                }
            )
    # Last resort
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def serialize_channel_values(channel_values: dict[str, Any]) -> dict[str, Any]:
    """Serialize channel values, stripping internal LangGraph keys.

    Only ``__pregel_*`` keys are removed — ``__interrupt__`` is deliberately
    preserved so the LangGraph SDK can detect interrupt events from values
    chunks (see issue #3595).
    """
    result: dict[str, Any] = {}
    for key, value in channel_values.items():
        if key.startswith("__pregel_"):
            continue
        result[key] = serialize_lc_object(value)
    return result


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

        # Filter out image_url blocks with data: scheme.
        filtered = [block for block in content if not (isinstance(block, dict) and block.get("type") == "image_url" and isinstance(block.get("image_url"), dict) and str(block["image_url"].get("url", "")).startswith("data:"))]
        result.append({**msg, "content": filtered})
    return result


def _viewed_images_for_api(value: object) -> dict[str, dict[str, object]]:
    """Expose only bounded observation metadata, never bytes or locators."""

    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, object]] = {}
    for image_path, image_data in value.items():
        if not isinstance(image_path, str) or not isinstance(image_data, dict):
            continue
        mime_type = image_data.get("mime_type")
        size = image_data.get("size")
        sha256 = image_data.get("sha256")
        file_ref = image_data.get("file_ref")
        project_id = file_ref.get("project_id") if isinstance(file_ref, dict) else None
        owner_user_id = file_ref.get("owner_user_id") if isinstance(file_ref, dict) else None
        if (
            not any(image_path == root or image_path.startswith(f"{root}/") for root in _PUBLIC_VIEWED_IMAGE_ROOTS)
            or mime_type not in _PUBLIC_VIEWED_IMAGE_MIME_TYPES
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= _MAX_PUBLIC_VIEWED_IMAGE_BYTES
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not isinstance(file_ref, dict)
            or file_ref.get("path") != image_path
            or not isinstance(file_ref.get("sandbox_id"), str)
            or not file_ref["sandbox_id"]
            or not isinstance(file_ref.get("run_id"), str)
            or not file_ref["run_id"]
            or (project_id is None) != (owner_user_id is None)
            or (project_id is not None and (not isinstance(project_id, str) or not project_id or not isinstance(owner_user_id, str) or not owner_user_id))
        ):
            continue
        result[image_path] = {
            "mime_type": mime_type,
            "size": size,
            "sha256": sha256,
        }
    return result


def serialize_channel_values_for_api(channel_values: dict[str, Any]) -> dict[str, Any]:
    """Serialize state while removing all persisted image bytes and locators.

    Convenience wrapper combining :func:`serialize_channel_values` with
    :func:`strip_data_url_image_blocks`.  Use this in all REST endpoints
    that return channel values to the frontend so that ``data:``-scheme
    base64 image payloads are never sent over the wire. Legacy
    ``viewed_images.base64`` entries are dropped entirely; current entries
    expose only MIME, size, and digest metadata.
    """
    result = serialize_channel_values(channel_values)
    if isinstance(result.get("messages"), list):
        result["messages"] = strip_data_url_image_blocks(result["messages"])
    if "viewed_images" in result:
        result["viewed_images"] = _viewed_images_for_api(result["viewed_images"])
    return result


def serialize_messages_tuple(obj: Any) -> Any:
    """Serialize a messages-mode tuple ``(chunk, metadata)``."""
    if isinstance(obj, tuple) and len(obj) == 2:
        chunk, metadata = obj
        return [serialize_lc_object(chunk), metadata if isinstance(metadata, dict) else {}]
    return serialize_lc_object(obj)


def serialize(obj: Any, *, mode: str = "") -> Any:
    """Serialize LangChain objects with mode-specific handling.

    * ``messages`` — obj is ``(message_chunk, metadata_dict)``
    * ``values`` — obj is the full state dict; ``__pregel_*`` keys stripped and
      base64 ``data:`` image blocks dropped from hide_from_ui messages
    * everything else — recursive ``model_dump()`` / ``dict()`` fallback
    """
    if mode == "messages":
        return serialize_messages_tuple(obj)
    if mode == "values":
        # ``values`` snapshots stream the full state to the frontend, so they
        # must drop base64 image payloads the same way the REST endpoints do.
        return serialize_channel_values_for_api(obj) if isinstance(obj, dict) else serialize_lc_object(obj)
    return serialize_lc_object(obj)
