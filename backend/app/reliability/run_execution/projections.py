"""Pure projections used at the execution boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from app.private_work.context import PrivateWorkContext
from app.private_work.run_repository import PrivateRunRecord


def private_guardrail_attribution(
    context: PrivateWorkContext,
    run: PrivateRunRecord,
) -> dict[str, object]:
    """Build the closed Guardrail identity from the locked private Run."""

    if run.project_id != context.project_id or run.owner_user_id != str(context.user_id):
        raise RuntimeError("Private Run attribution scope mismatch")
    return {
        "user_id": str(context.user_id),
        "user_role": context.role.value,
        "thread_id": run.thread_id,
        "run_id": run.run_id,
        "is_subagent": False,
        "authz_attributes": {
            "project_id": str(context.project_id),
            "project_role": context.role.value,
            "capabilities": tuple(sorted(capability.value for capability in context.capabilities)),
        },
    }


def checkpoint_progress_cursor(
    saver: Any,
    item: Any | None,
) -> str | None:
    """Fingerprint the latest durable checkpoint plus pending writes."""

    if item is None:
        return None
    raw_configurable = item.config.get("configurable")
    checkpoint_id = None
    if isinstance(raw_configurable, Mapping):
        raw_checkpoint_id = raw_configurable.get("checkpoint_id")
        if isinstance(raw_checkpoint_id, str):
            checkpoint_id = raw_checkpoint_id
    pending_writes = getattr(item, "pending_writes", None)
    if not pending_writes:
        return checkpoint_id
    if checkpoint_id is None:
        raise RuntimeError("checkpoint pending writes require a checkpoint id")

    digest = hashlib.sha256()

    def update(part: bytes) -> None:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)

    update(b"deerflow:checkpoint-progress:v1")
    update(checkpoint_id.encode())
    for pending_write in pending_writes:
        if not isinstance(pending_write, (list, tuple)) or len(pending_write) != 3:
            raise RuntimeError("checkpoint pending write is invalid")
        task_id, channel, value = pending_write
        if not isinstance(task_id, str) or not isinstance(channel, str):
            raise RuntimeError("checkpoint pending write identity is invalid")
        try:
            value_type, value_bytes = saver.serde.dumps_typed(value)
        except Exception:
            raise RuntimeError("checkpoint pending write serialization failed") from None
        if not isinstance(value_type, str) or not isinstance(
            value_bytes,
            bytes,
        ):
            raise RuntimeError("checkpoint pending write serialization is invalid")
        update(task_id.encode())
        update(channel.encode())
        update(value_type.encode())
        update(value_bytes)
    return f"pw:{digest.hexdigest()}"


__all__ = [
    "checkpoint_progress_cursor",
    "private_guardrail_attribution",
]
