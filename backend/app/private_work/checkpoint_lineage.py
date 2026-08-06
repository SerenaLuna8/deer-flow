"""Safe replay-base resolution on one project-scoped checkpoint lineage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class CheckpointLineageError(RuntimeError):
    """A replay base cannot be resolved without leaving the selected lineage."""


class CheckpointLineageIntegrityError(CheckpointLineageError):
    """A recorded parent link is missing, cyclic, or not addressable."""


def _message_id(message: object) -> str | None:
    value = message.get("id") if isinstance(message, Mapping) else getattr(message, "id", None)
    return str(value) if value else None


def _messages(snapshot: object) -> list[Any]:
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        return []
    messages = values.get("messages")
    return list(messages) if isinstance(messages, list) else []


def _config_identity(config: object) -> tuple[str, str, str] | None:
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    thread_id = configurable.get("thread_id")
    checkpoint_id = configurable.get("checkpoint_id")
    checkpoint_ns = configurable.get("checkpoint_ns", "")
    if not isinstance(thread_id, str) or not thread_id or not isinstance(checkpoint_id, str) or not checkpoint_id:
        return None
    return thread_id, str(checkpoint_ns or ""), checkpoint_id


def _snapshot_identity(snapshot: object) -> tuple[str, str, str] | None:
    return _config_identity(getattr(snapshot, "config", None))


def _snapshot_exists(snapshot: object) -> bool:
    explicit = getattr(snapshot, "checkpoint_exists", None)
    if isinstance(explicit, bool):
        return explicit
    return getattr(snapshot, "metadata", None) is not None or getattr(snapshot, "created_at", None) is not None


def _has_pending_tasks(snapshot: object) -> bool:
    return bool(getattr(snapshot, "next", None))


def _is_duration_only(snapshot: object) -> bool:
    metadata = getattr(snapshot, "metadata", None)
    writes = metadata.get("writes") if isinstance(metadata, Mapping) else None
    return isinstance(writes, Mapping) and "runtime_run_duration" in writes


async def find_settled_checkpoint_before_message(
    accessor: Any,
    head: object,
    message_id: str,
    *,
    max_depth: int,
) -> object:
    """Return the first settled ancestor before ``message_id``.

    The accessor must already be bound to a server-issued
    :class:`PrivateWorkContext`. Following only ``parent_config`` prevents a
    chronological scan from selecting a checkpoint on a regenerate sibling.
    Pending graph states and metadata-only duration checkpoints are never valid
    replay bases.
    """

    if type(message_id) is not str or not message_id or type(max_depth) is not int or max_depth < 1:
        raise CheckpointLineageIntegrityError("invalid checkpoint-lineage request")
    if message_id not in {_message_id(message) for message in _messages(head)}:
        raise CheckpointLineageIntegrityError("target message is absent from the checkpoint head")

    head_identity = _snapshot_identity(head)
    if head_identity is None or not _snapshot_exists(head):
        raise CheckpointLineageIntegrityError("checkpoint head is not addressable")
    visited = {head_identity}
    current = head

    for _ in range(max_depth):
        parent_config = getattr(current, "parent_config", None)
        requested_identity = _config_identity(parent_config)
        if requested_identity is None:
            raise CheckpointLineageIntegrityError("checkpoint lineage ended before the target")
        if requested_identity[:2] != head_identity[:2]:
            raise CheckpointLineageIntegrityError("checkpoint parent leaves the selected thread lineage")

        parent = await accessor.aget(parent_config)
        parent_identity = _snapshot_identity(parent)
        if parent_identity != requested_identity or not _snapshot_exists(parent):
            raise CheckpointLineageIntegrityError("checkpoint parent link is not addressable")
        if parent_identity in visited:
            raise CheckpointLineageIntegrityError("checkpoint lineage contains a cycle")
        visited.add(parent_identity)

        if _is_duration_only(parent):
            current = parent
            continue
        if message_id not in {_message_id(message) for message in _messages(parent)} and not _has_pending_tasks(parent):
            return parent
        current = parent

    raise CheckpointLineageIntegrityError("checkpoint lineage exceeded the bounded scan")


__all__ = [
    "CheckpointLineageError",
    "CheckpointLineageIntegrityError",
    "find_settled_checkpoint_before_message",
]
