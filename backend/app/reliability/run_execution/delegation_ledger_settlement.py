"""Trusted checkpoint settlement for parent-Run delegation cancellation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from langgraph.checkpoint.base import (
    CheckpointMetadata,
    copy_checkpoint,
    create_checkpoint,
)

from deerflow.agents.middlewares.delegation_ledger import (
    cancelled_delegation_updates,
)
from deerflow.agents.thread_state import DelegationEntry, merge_delegations

_SETTLEMENT_NODE = "run_cancel_delegations"


def _checkpoint_id(item: object) -> str | None:
    config = getattr(item, "config", None)
    configurable = config.get("configurable") if isinstance(config, Mapping) else None
    value = configurable.get("checkpoint_id") if isinstance(configurable, Mapping) else None
    if isinstance(value, str) and value:
        return value
    checkpoint = getattr(item, "checkpoint", None)
    value = checkpoint.get("id") if isinstance(checkpoint, Mapping) else None
    return value if isinstance(value, str) and value else None


async def settle_run_delegation_ledger_cancelled(
    saver: Any,
    *,
    thread_id: str,
    project_id: str,
    owner_user_id: str,
    run_id: str,
) -> bool:
    """Terminalize exact Run-scoped ledger entries through the cancel façade."""

    config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    item = await saver.aget_tuple_cancel_settlement(config)
    if item is None:
        return False
    raw_checkpoint = getattr(item, "checkpoint", None)
    if not isinstance(raw_checkpoint, dict):
        return False
    raw_channel_values = raw_checkpoint.get("channel_values")
    channel_values = dict(raw_channel_values) if isinstance(raw_channel_values, Mapping) else {}
    raw_entries = channel_values.get("delegations")
    if not isinstance(raw_entries, list):
        return False
    entries = cast(list[DelegationEntry], raw_entries)
    updates = cancelled_delegation_updates(
        entries,
        project_id=project_id,
        owner_user_id=owner_user_id,
        run_id=run_id,
    )
    if not updates:
        return False

    checkpoint = copy_checkpoint(raw_checkpoint)
    channel_values["delegations"] = merge_delegations(entries, updates)
    channel_versions = dict(checkpoint.get("channel_versions", {}) or {})
    next_version = saver.get_next_version(
        channel_versions.get("delegations"),
        "delegations",
    )
    channel_versions["delegations"] = next_version
    checkpoint["channel_values"] = channel_values
    checkpoint["channel_versions"] = channel_versions

    raw_metadata = getattr(item, "metadata", None)
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    raw_step = metadata.get("step")
    next_step = raw_step + 1 if isinstance(raw_step, int) and not isinstance(raw_step, bool) else 0
    checkpoint = create_checkpoint(
        checkpoint,
        None,
        next_step,
    )
    checkpoint["updated_channels"] = ["delegations"]
    metadata.update(
        {
            "source": "update",
            "step": next_step,
            "writes": {
                _SETTLEMENT_NODE: {
                    "delegations": updates,
                }
            },
        }
    )
    source_checkpoint_id = _checkpoint_id(item)
    write_config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    if source_checkpoint_id is not None:
        write_config["configurable"]["checkpoint_id"] = source_checkpoint_id
    await saver.aput_cancel_settlement(
        write_config,
        checkpoint,
        cast(CheckpointMetadata, metadata),
        {"delegations": next_version},
    )
    return True


__all__ = ["settle_run_delegation_ledger_cancelled"]
