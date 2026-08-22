"""Private, project-scoped access to materialized checkpoint state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext
from deerflow.config.app_config import AppConfig
from deerflow.runtime.checkpoint_mode import (
    freeze_checkpoint_channel_mode,
    freeze_checkpoint_snapshot_frequency,
)
from deerflow.runtime.checkpoint_state import (
    CheckpointStateAccessor,
    build_state_mutation_graph,
)


def resolve_checkpoint_runtime(
    app_config: AppConfig,
) -> tuple[str, int]:
    """Freeze and return the process-wide checkpoint representation."""
    mode = freeze_checkpoint_channel_mode(app_config.database.checkpoint_channel_mode)
    snapshot_frequency = freeze_checkpoint_snapshot_frequency(app_config.database.checkpoint_delta.snapshot_frequency)
    return mode, snapshot_frequency


def bind_scoped_checkpoint_state(
    project_scoped_checkpointer: ProjectScopedCheckpointer,
    context: PrivateWorkContext,
    app_config: AppConfig,
    *,
    as_node: str,
    graph: Any | None = None,
) -> CheckpointStateAccessor:
    """Create a request-local accessor; never share a mutable graph binding."""
    mode, snapshot_frequency = resolve_checkpoint_runtime(app_config)
    scoped_saver = project_scoped_checkpointer.for_context(context)
    local_graph = graph or build_state_mutation_graph(
        as_node,
        mode,
        snapshot_frequency=snapshot_frequency,
    )
    return CheckpointStateAccessor.bind(
        local_graph,
        scoped_saver,
        mode=mode,
    )


def bind_transaction_checkpoint_state(
    scoped_saver: Any,
    session: AsyncSession,
    app_config: AppConfig,
    *,
    as_node: str,
    graph: Any | None = None,
) -> CheckpointStateAccessor:
    """Bind materialization and writes to an already-held Thread lock."""
    mode, snapshot_frequency = resolve_checkpoint_runtime(app_config)
    local_graph = graph or build_state_mutation_graph(
        as_node,
        mode,
        snapshot_frequency=snapshot_frequency,
    )
    return CheckpointStateAccessor.bind(
        local_graph,
        scoped_saver.already_authorized(session),
        mode=mode,
    )


def checkpoint_config(
    thread_id: str,
    *,
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    configurable: dict[str, Any] = {
        "thread_id": thread_id,
        "checkpoint_ns": "",
    }
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def snapshot_checkpoint_id(snapshot: object | None) -> str | None:
    config = getattr(snapshot, "config", None)
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    value = configurable.get("checkpoint_id")
    return value if isinstance(value, str) and value else None
