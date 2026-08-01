"""Checkpoint representation freeze, markers, and fail-closed compatibility."""

from __future__ import annotations

from typing import Any

from deerflow.config.database_config import (
    DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY,
    CheckpointChannelMode,
)

INTERNAL_CHECKPOINT_MODE_KEY = "__deerflow_checkpoint_channel_mode"
CHECKPOINT_MODE_METADATA_KEY = "deerflow_checkpoint_channel_mode"


class CheckpointModeMismatchError(RuntimeError):
    """Raised before a full-mode process consumes a delta checkpoint."""


class CheckpointModeReconfigurationError(RuntimeError):
    """Raised when a process attempts to hot-switch checkpoint semantics."""


_frozen_checkpoint_channel_mode: CheckpointChannelMode | None = None
_frozen_checkpoint_snapshot_frequency: int | None = None


def frozen_checkpoint_channel_mode() -> CheckpointChannelMode | None:
    return _frozen_checkpoint_channel_mode


def freeze_checkpoint_channel_mode(
    mode: CheckpointChannelMode,
) -> CheckpointChannelMode:
    global _frozen_checkpoint_channel_mode
    if _frozen_checkpoint_channel_mode is None:
        _frozen_checkpoint_channel_mode = mode
    elif _frozen_checkpoint_channel_mode != mode:
        raise CheckpointModeReconfigurationError("checkpoint_channel_mode is restart-required and cannot change in a running process")
    return _frozen_checkpoint_channel_mode


def frozen_checkpoint_snapshot_frequency() -> int | None:
    return _frozen_checkpoint_snapshot_frequency


def freeze_checkpoint_snapshot_frequency(snapshot_frequency: int) -> int:
    global _frozen_checkpoint_snapshot_frequency
    if snapshot_frequency <= 0:
        raise ValueError("snapshot frequency must be positive")
    if _frozen_checkpoint_snapshot_frequency is None:
        _frozen_checkpoint_snapshot_frequency = snapshot_frequency
    elif _frozen_checkpoint_snapshot_frequency != snapshot_frequency:
        raise CheckpointModeReconfigurationError("checkpoint_delta.snapshot_frequency is restart-required and cannot change in a running process")
    return _frozen_checkpoint_snapshot_frequency


def resolve_checkpoint_snapshot_frequency(
    snapshot_frequency: int | None = None,
) -> int:
    if snapshot_frequency is not None:
        return snapshot_frequency
    return _frozen_checkpoint_snapshot_frequency if _frozen_checkpoint_snapshot_frequency is not None else DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY


def inject_checkpoint_mode(
    config: dict[str, Any],
    mode: CheckpointChannelMode,
) -> None:
    configurable = config.setdefault("configurable", {})
    configurable[INTERNAL_CHECKPOINT_MODE_KEY] = mode
    metadata = config.setdefault("metadata", {})
    if mode == "delta":
        metadata[CHECKPOINT_MODE_METADATA_KEY] = "delta"
    else:
        metadata.pop(CHECKPOINT_MODE_METADATA_KEY, None)


def checkpoint_metadata_uses_delta(metadata: Any) -> bool:
    if not metadata:
        return False
    if metadata.get(CHECKPOINT_MODE_METADATA_KEY) == "delta":
        return True
    counters = metadata.get("counters_since_delta_snapshot")
    return isinstance(counters, dict) and "messages" in counters


def checkpoint_tuple_uses_delta(checkpoint_tuple: Any) -> bool:
    if checkpoint_tuple is None:
        return False
    return checkpoint_metadata_uses_delta(getattr(checkpoint_tuple, "metadata", {}) or {})


def state_snapshot_uses_delta(snapshot: Any) -> bool:
    if snapshot is None:
        return False
    return checkpoint_metadata_uses_delta(getattr(snapshot, "metadata", {}) or {})


def raise_if_snapshot_incompatible(
    snapshot: Any,
    mode: CheckpointChannelMode,
) -> None:
    if mode == "full" and state_snapshot_uses_delta(snapshot):
        raise CheckpointModeMismatchError("Thread requires delta mode; materialize and convert its checkpoints before using full mode.")


def ensure_checkpoint_mode_compatible(
    checkpointer: Any,
    config: dict[str, Any],
    mode: CheckpointChannelMode,
) -> None:
    if mode == "delta":
        return
    if checkpoint_tuple_uses_delta(checkpointer.get_tuple(config)):
        raise CheckpointModeMismatchError("Thread requires delta mode; materialize and convert its checkpoints before using full mode.")


async def aensure_checkpoint_mode_compatible(
    checkpointer: Any,
    config: dict[str, Any],
    mode: CheckpointChannelMode,
) -> None:
    if mode == "delta":
        return
    if checkpoint_tuple_uses_delta(await checkpointer.aget_tuple(config)):
        raise CheckpointModeMismatchError("Thread requires delta mode; materialize and convert its checkpoints before using full mode.")
