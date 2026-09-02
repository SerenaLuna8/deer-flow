"""Checkpoint state reads, the pre-run message boundary, and rollback restore."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.types import Overwrite

from deerflow.error_codes import ROLLBACK_FAILED_ERROR_CODE
from deerflow.runtime.checkpoint_state import (
    CheckpointStateAccessor,
    build_state_mutation_graph,
    graph_state_schema,
)
from deerflow.runtime.context_evidence import ContextRebaseReason
from deerflow.runtime.goal import _call_checkpointer_method

from .manager import RunManager
from .private_file_lifecycle import await_despite_cancellation
from .schemas import RunStatus

__all__ = ["RollbackPoint"]

logger = logging.getLogger(__name__)

_ROLLBACK_SUCCEEDED_ERROR = "Rolled back by user"


def _checkpoint_id(checkpoint_tuple: Any) -> str | None:
    config = getattr(checkpoint_tuple, "config", {}) or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    checkpoint_id = configurable.get("checkpoint_id") if isinstance(configurable, dict) else None
    if isinstance(checkpoint_id, str):
        return checkpoint_id
    checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("id"), str):
        return checkpoint["id"]
    return None


def _snapshot_values(snapshot: Any) -> dict[str, Any]:
    values = getattr(snapshot, "values", None)
    return dict(values) if isinstance(values, dict) else {}


async def _materialized_checkpoint_snapshot(
    accessor: CheckpointStateAccessor,
    thread_id: str,
) -> Any:
    return await accessor.aget(
        {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }
    )


async def _materialized_checkpoint_messages(
    accessor: CheckpointStateAccessor,
    thread_id: str,
) -> list[Any]:
    """Read the complete messages value through the mode-matched graph."""

    values = _snapshot_values(await _materialized_checkpoint_snapshot(accessor, thread_id))
    messages = values.get("messages")
    return list(messages) if isinstance(messages, (list, tuple)) else []


def _read_checkpoint_messages(checkpoint_tuple: Any) -> list[Any]:
    checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
    channel_values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
    messages = channel_values.get("messages", []) if isinstance(channel_values, dict) else []
    return messages if isinstance(messages, list) else []


def _message_id(obj: Any) -> str | None:
    """Best-effort extraction of a stable message id from a message-like object."""
    msg_id = getattr(obj, "id", None)
    if isinstance(msg_id, str) and msg_id:
        return msg_id
    if isinstance(obj, dict):
        raw = obj.get("id")
        if isinstance(raw, str) and raw:
            return raw
    return None


def _checkpoint_messages_from_values_or_snapshot(
    values_or_snapshot: Any,
) -> Any:
    if not isinstance(values_or_snapshot, dict):
        return None
    if "messages" in values_or_snapshot:
        return values_or_snapshot.get("messages")
    checkpoint = values_or_snapshot.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return None
    channel_values = checkpoint.get("channel_values")
    if channel_values is None:
        channel_values = {}
    if not isinstance(channel_values, dict):
        return None
    return channel_values.get("messages", [])


def _collect_pre_existing_message_ids(values_or_snapshot: Any) -> set[str]:
    """Pull stable message IDs from materialized values or a legacy snapshot.

    Used by :func:`run_agent` to mask stale ``deerflow_error_fallback`` markers
    on history messages so they don't trip the current run's failure path.
    Missing or malformed values yield an empty set (best-effort — we
    intentionally never raise from this helper).
    """
    messages = _checkpoint_messages_from_values_or_snapshot(values_or_snapshot)
    if not isinstance(messages, (list, tuple)):
        return set()
    ids: set[str] = set()
    for msg in messages:
        msg_id = _message_id(msg)
        if msg_id is not None:
            ids.add(msg_id)
    return ids


def _collect_private_pre_existing_message_ids(
    values_or_snapshot: Any,
) -> set[str]:
    """Validate an exact private-Run materialized message boundary.

    A present checkpoint with no messages is a valid first-run boundary.
    Historical messages must all carry distinct stable IDs; otherwise a
    resumed Run cannot distinguish old task dispatches from new results.
    """
    messages = _checkpoint_messages_from_values_or_snapshot(values_or_snapshot)
    if messages is None:
        raise ValueError("invalid checkpoint messages")
    if not isinstance(messages, (list, tuple)):
        raise ValueError("invalid checkpoint messages")

    ids: set[str] = set()
    for message in messages:
        message_id = _message_id(message)
        if message_id is None or message_id in ids:
            raise ValueError("unstable checkpoint message identity")
        ids.add(message_id)
    return ids


@dataclass(frozen=True)
class RollbackPoint:
    """Exact materialized state captured before a Run mutates its thread."""

    config: dict[str, Any]
    state_values: dict[str, Any]
    messages: tuple[Any, ...]
    metadata: dict[str, Any]
    pending_writes: tuple[tuple[str, str, Any], ...]


async def _settle_rollback(
    *,
    run_manager: RunManager,
    run_id: str,
    rollback: Callable[[], Awaitable[bool]],
) -> bool:
    """Finish rollback before recording its single authoritative terminal."""

    outcome = await await_despite_cancellation(rollback())
    cancellation_pending = outcome.cancellation_pending
    try:
        restored = outcome.result() is True
    except asyncio.CancelledError:
        restored = False
        logger.warning("Rollback operation was cancelled for run %s", run_id)
    except Exception:
        restored = False
        logger.warning("Failed to rollback checkpoint for run %s", run_id, exc_info=True)

    terminal_error = _ROLLBACK_SUCCEEDED_ERROR if restored else ROLLBACK_FAILED_ERROR_CODE
    status_outcome = await await_despite_cancellation(
        run_manager.set_status(
            run_id,
            RunStatus.error,
            error=terminal_error,
        ),
    )
    cancellation_pending |= status_outcome.cancellation_pending
    status_outcome.result()

    if restored:
        logger.info("Run %s rolled back to its pre-run checkpoint", run_id)
    return cancellation_pending


async def _capture_rollback_point(
    accessor: CheckpointStateAccessor,
    checkpointer: Any,
    read_config: dict[str, Any],
) -> RollbackPoint | None:
    """Capture materialized state plus exact raw pending writes."""

    snapshot = await accessor.aget(read_config)
    snapshot_config = getattr(snapshot, "config", None) or {}
    configurable = snapshot_config.get("configurable", {}) if isinstance(snapshot_config, dict) else {}
    if not isinstance(configurable, dict) or not configurable.get("checkpoint_id"):
        return None

    checkpoint_tuple = await _call_checkpointer_method(
        checkpointer,
        "aget_tuple",
        "get_tuple",
        snapshot_config,
    )
    raw_values = getattr(snapshot, "values", None) or {}
    messages = raw_values.get("messages") if isinstance(raw_values, dict) else None
    state_values = copy.deepcopy({key: value for key, value in raw_values.items() if key != "messages"}) if accessor.mode == "delta" and isinstance(raw_values, dict) else {}
    return RollbackPoint(
        config={
            "configurable": {
                "thread_id": configurable.get("thread_id"),
                "checkpoint_ns": configurable.get("checkpoint_ns") or "",
                "checkpoint_id": configurable.get("checkpoint_id"),
            }
        },
        state_values=state_values,
        messages=tuple(messages or ()),
        metadata=dict(getattr(snapshot, "metadata", None) or {}),
        pending_writes=tuple(getattr(checkpoint_tuple, "pending_writes", ()) or ()),
    )


def _rollback_point_from_legacy_snapshot(
    *,
    thread_id: str,
    checkpoint_id: str | None,
    snapshot: dict[str, Any] | None,
) -> RollbackPoint | None:
    """Adapt full-mode compatibility snapshots used by embedded test graphs."""

    if snapshot is None:
        return None
    checkpoint = snapshot.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return None
    resolved_checkpoint_id = checkpoint_id or checkpoint.get("id")
    if not isinstance(resolved_checkpoint_id, str):
        return None
    channel_values = checkpoint.get("channel_values", {})
    messages = channel_values.get("messages", []) if isinstance(channel_values, dict) else []
    raw_checkpoint_ns = snapshot.get("checkpoint_ns")
    checkpoint_ns = raw_checkpoint_ns if isinstance(raw_checkpoint_ns, str) else ""
    metadata = snapshot.get("metadata")
    pending_writes = snapshot.get("pending_writes")
    return RollbackPoint(
        config={
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": resolved_checkpoint_id,
            }
        },
        state_values={},
        messages=tuple(messages if isinstance(messages, (list, tuple)) else ()),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        pending_writes=tuple(pending_writes if isinstance(pending_writes, (list, tuple)) else ()),
    )


async def _linearize_delta_checkpoint_resume(
    *,
    accessor: CheckpointStateAccessor,
    checkpointer: Any,
    config: dict[str, Any],
    thread_id: str,
    run_id: str,
    snapshot_frequency: int | None = None,
) -> list[Any] | None:
    """Rewrite a historical delta selector as a linear current-head update."""

    if checkpointer is None or accessor.mode != "delta":
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    checkpoint_id = configurable.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        return None
    if configurable.get("checkpoint_ns"):
        return None

    head_config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    head = await accessor.aget(head_config)
    if _checkpoint_id(head) == checkpoint_id:
        return None

    source_config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
        }
    }
    source = await accessor.aget(source_config)
    selected_values = _snapshot_values(source)
    messages = selected_values.get("messages")
    if not isinstance(messages, list):
        raise RuntimeError(f"Run {run_id} could not materialize resume checkpoint {checkpoint_id}")

    mutation_graph = build_state_mutation_graph(
        "checkpoint_resume",
        accessor.mode,
        graph_state_schema(accessor.graph),
        snapshot_frequency=snapshot_frequency,
    )
    mutation_accessor = CheckpointStateAccessor.bind(
        mutation_graph,
        checkpointer,
        mode=accessor.mode,
    )
    replacement_values = mutation_accessor.replacement_values(
        selected_values,
        current_values=_snapshot_values(head),
    )
    await mutation_accessor.aupdate(
        head_config,
        replacement_values,
        as_node="checkpoint_resume",
    )
    configurable.pop("checkpoint_id", None)
    configurable.pop("checkpoint_map", None)
    logger.info(
        "Run %s linearized delta checkpoint %s onto thread %s",
        run_id,
        checkpoint_id,
        thread_id,
    )
    return list(messages)


async def _restore_pending_writes(
    *,
    checkpointer: Any,
    restored_config: dict[str, Any],
    pending_writes: Any,
    run_id: str,
) -> None:
    if not pending_writes:
        return
    writes_by_task: dict[str, list[tuple[str, Any]]] = {}
    for item in pending_writes:
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            raise RuntimeError(f"Run {run_id} rollback failed: pending_write is not a 3-tuple: {item!r}")
        task_id, channel, value = item
        if not isinstance(channel, str):
            raise RuntimeError(f"Run {run_id} rollback failed: pending_write has non-string channel: task_id={task_id!r}, channel={channel!r}")
        writes_by_task.setdefault(str(task_id), []).append((channel, value))

    for task_id, writes in writes_by_task.items():
        await _call_checkpointer_method(
            checkpointer,
            "aput_writes",
            "put_writes",
            restored_config,
            writes,
            task_id=task_id,
        )


async def _rollback_legacy_full_checkpoint(
    *,
    checkpointer: Any,
    thread_id: str,
    run_id: str,
    pre_run_checkpoint_id: str | None,
    pre_run_snapshot: dict[str, Any] | None,
    allow_thread_delete: bool,
) -> bool:
    """Preserve the full-mode helper contract for non-graph test adapters."""

    if pre_run_snapshot is None:
        if not allow_thread_delete:
            logger.warning(
                "Run %s private rollback skipped: no pre-run checkpoint exists and deleting the business Thread is forbidden",
                run_id,
            )
            return False
        await _call_checkpointer_method(
            checkpointer,
            "adelete_thread",
            "delete_thread",
            thread_id,
        )
        logger.info(
            "Run %s rollback reset thread %s to empty state",
            run_id,
            thread_id,
        )
        return True

    checkpoint = pre_run_snapshot.get("checkpoint")
    if not isinstance(checkpoint, dict):
        logger.warning(
            "Run %s rollback skipped: invalid pre-run checkpoint snapshot",
            run_id,
        )
        return False
    checkpoint_to_restore = checkpoint
    if checkpoint_to_restore.get("id") is None and pre_run_checkpoint_id is not None:
        checkpoint_to_restore = {
            **checkpoint_to_restore,
            "id": pre_run_checkpoint_id,
        }
    if checkpoint_to_restore.get("id") is None:
        logger.warning(
            "Run %s rollback skipped: pre-run checkpoint has no checkpoint id",
            run_id,
        )
        return False
    checkpoint_to_restore = {
        **checkpoint_to_restore,
        **_new_checkpoint_marker(),
    }
    metadata = pre_run_snapshot.get("metadata", {})
    metadata_to_restore = metadata if isinstance(metadata, dict) else {}
    raw_checkpoint_ns = pre_run_snapshot.get("checkpoint_ns")
    checkpoint_ns = raw_checkpoint_ns if isinstance(raw_checkpoint_ns, str) else ""
    channel_versions = checkpoint_to_restore.get("channel_versions")
    new_versions = dict(channel_versions) if isinstance(channel_versions, dict) else {}
    restore_config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        }
    }
    restored_config = await _call_checkpointer_method(
        checkpointer,
        "aput",
        "put",
        restore_config,
        checkpoint_to_restore,
        metadata_to_restore,
        new_versions,
    )
    if not isinstance(restored_config, dict):
        raise RuntimeError(f"Run {run_id} rollback restore returned invalid config: expected dict")
    restored_configurable = restored_config.get("configurable", {})
    if not isinstance(restored_configurable, dict):
        raise RuntimeError(f"Run {run_id} rollback restore returned invalid config payload")
    if not restored_configurable.get("checkpoint_id"):
        raise RuntimeError(f"Run {run_id} rollback restore did not return checkpoint_id")
    await _restore_pending_writes(
        checkpointer=checkpointer,
        restored_config=restored_config,
        pending_writes=pre_run_snapshot.get("pending_writes", []),
        run_id=run_id,
    )
    return True


async def _rollback_to_pre_run_checkpoint(
    *,
    checkpointer: Any,
    thread_id: str,
    run_id: str,
    snapshot_capture_failed: bool,
    accessor: CheckpointStateAccessor | None = None,
    rollback_point: RollbackPoint | None = None,
    snapshot_frequency: int | None = None,
    pre_run_checkpoint_id: str | None = None,
    pre_run_snapshot: dict[str, Any] | None = None,
    allow_thread_delete: bool = True,
    context_evidence_observer: object | None = None,
) -> bool:
    """Restore complete pre-run state without replaying delta sibling writes."""

    if checkpointer is None:
        logger.info(
            "Run %s rollback requested but no checkpointer is configured",
            run_id,
        )
        return False
    if snapshot_capture_failed:
        logger.warning(
            "Run %s rollback skipped: pre-run checkpoint capture failed",
            run_id,
        )
        return False

    graph_can_mutate = accessor is not None and callable(getattr(accessor.graph, "aupdate_state", None))
    if not graph_can_mutate:
        return await _rollback_legacy_full_checkpoint(
            checkpointer=checkpointer,
            thread_id=thread_id,
            run_id=run_id,
            pre_run_checkpoint_id=pre_run_checkpoint_id,
            pre_run_snapshot=pre_run_snapshot,
            allow_thread_delete=allow_thread_delete,
        )

    if rollback_point is None:
        if not allow_thread_delete:
            logger.warning(
                "Run %s private rollback skipped: no pre-run checkpoint exists and deleting the business Thread is forbidden",
                run_id,
            )
            return False
        await _call_checkpointer_method(
            checkpointer,
            "adelete_thread",
            "delete_thread",
            thread_id,
        )
        logger.info(
            "Run %s rollback reset thread %s to empty state",
            run_id,
            thread_id,
        )
        return True

    configurable = rollback_point.config.get("configurable", {})
    if not configurable.get("checkpoint_id"):
        logger.warning(
            "Run %s rollback skipped: pre-run checkpoint has no checkpoint id",
            run_id,
        )
        return False

    mutation_graph = build_state_mutation_graph(
        "rollback_restore",
        accessor.mode,
        graph_state_schema(accessor.graph),
        snapshot_frequency=snapshot_frequency,
    )
    mutation_accessor = CheckpointStateAccessor.bind(
        mutation_graph,
        checkpointer,
        mode=accessor.mode,
    )
    current_config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    current = await accessor.aget(current_config)
    current_configurable = getattr(current, "config", {}).get(
        "configurable",
        {},
    )
    source_checkpoint_id = current_configurable.get("checkpoint_id") if isinstance(current_configurable, dict) else None
    if accessor.mode == "delta":
        restore_config = current_config
        selected_values = copy.deepcopy(rollback_point.state_values)
        selected_values["messages"] = list(rollback_point.messages)
        replacement_values = mutation_accessor.replacement_values(
            selected_values,
            current_values=_snapshot_values(current),
        )
    else:
        restore_config = rollback_point.config
        replacement_values = {"messages": Overwrite(list(rollback_point.messages))}

    restored_config = await mutation_accessor.aupdate(
        restore_config,
        replacement_values,
        as_node="rollback_restore",
    )
    if not isinstance(restored_config, dict):
        raise RuntimeError(f"Run {run_id} rollback restore returned invalid config: expected dict")
    restored_configurable = restored_config.get("configurable", {})
    if not isinstance(restored_configurable, dict):
        raise RuntimeError(f"Run {run_id} rollback restore returned invalid config payload")
    if not restored_configurable.get("checkpoint_id"):
        raise RuntimeError(f"Run {run_id} rollback restore did not return checkpoint_id")

    await _restore_pending_writes(
        checkpointer=checkpointer,
        restored_config=restored_config,
        pending_writes=rollback_point.pending_writes,
        run_id=run_id,
    )
    result_checkpoint_id = restored_configurable.get("checkpoint_id")
    record_rebased = getattr(
        context_evidence_observer,
        "record_window_rebased",
        None,
    )
    if callable(record_rebased) and isinstance(source_checkpoint_id, str) and source_checkpoint_id and isinstance(result_checkpoint_id, str) and result_checkpoint_id and source_checkpoint_id != result_checkpoint_id:
        await record_rebased(
            reason=ContextRebaseReason.ROLLBACK,
            source_checkpoint_id=source_checkpoint_id,
            result_checkpoint_id=result_checkpoint_id,
        )
    return True


def _new_checkpoint_marker() -> dict[str, str]:
    marker = empty_checkpoint()
    return {"id": marker["id"], "ts": marker["ts"]}
