from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.checkpointer import (
    PRIVATE_SCOPE_MARKER,
    ProjectScopedCheckpointer,
)
from app.private_work.errors import PrivateWorkNotFound
from app.private_work.thread_repository import ThreadAgentRef
from app.private_work.thread_service import PrivateThreadService
from deerflow.config.database_config import DatabaseConfig
from deerflow.runtime.checkpoint_mode import (
    CHECKPOINT_MODE_METADATA_KEY,
    CheckpointModeMismatchError,
)
from deerflow.runtime.checkpoint_state import (
    CheckpointStateAccessor,
    build_state_mutation_graph,
)
from deerflow.runtime.checkpointer.async_provider import make_checkpointer
from deerflow.runtime.goal import build_goal_state, write_thread_goal
from deerflow.runtime.runs.worker import (
    _capture_rollback_point,
    _rollback_to_pre_run_checkpoint,
)


def _app_config(
    database_url: str,
    *,
    mode: str,
    snapshot_frequency: int,
) -> Any:
    return SimpleNamespace(
        database=DatabaseConfig.model_validate(
            {
                "url": database_url,
                "checkpoint_channel_mode": mode,
                "checkpoint_delta": {
                    "snapshot_frequency": snapshot_frequency,
                },
            }
        )
    )


def _config(
    thread_id: str,
    *,
    metadata: dict[str, object] | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    if metadata is not None:
        config["metadata"] = metadata
    return config


def _accessor(
    saver: Any,
    *,
    mode: str,
    snapshot_frequency: int,
    as_node: str,
) -> CheckpointStateAccessor:
    return CheckpointStateAccessor.bind(
        build_state_mutation_graph(
            as_node,
            mode,
            snapshot_frequency=snapshot_frequency,
        ),
        saver,
        mode=mode,
    )


def _message_contents(snapshot: object) -> list[str]:
    values = getattr(snapshot, "values")
    return [str(message.content) for message in values["messages"]]


async def _storage_counts(
    engine: AsyncEngine,
    thread_id: str,
) -> tuple[int, int, int]:
    async with engine.connect() as connection:
        checkpoints = await connection.scalar(
            text(
                """SELECT count(*) FROM checkpoints
                WHERE thread_id=:thread_id AND checkpoint_ns=''"""
            ),
            {"thread_id": thread_id},
        )
        message_blobs = await connection.scalar(
            text(
                """SELECT count(*) FROM checkpoint_blobs
                WHERE thread_id=:thread_id AND checkpoint_ns=''
                AND channel='messages'"""
            ),
            {"thread_id": thread_id},
        )
        message_writes = await connection.scalar(
            text(
                """SELECT count(*) FROM checkpoint_writes
                WHERE thread_id=:thread_id AND checkpoint_ns=''
                AND channel='messages'"""
            ),
            {"thread_id": thread_id},
        )
    return int(checkpoints or 0), int(message_blobs or 0), int(message_writes or 0)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_async_postgres_full_to_delta_materializes_after_reopen(
    migrated_postgres_database_url: str,
) -> None:
    thread_id = "checkpoint-postgres-full-to-delta"
    config = _config(thread_id)
    app_config = _app_config(
        migrated_postgres_database_url,
        mode="delta",
        snapshot_frequency=2,
    )

    async with make_checkpointer(app_config) as saver:
        full = _accessor(
            saver,
            mode="full",
            snapshot_frequency=2,
            as_node="full_seed",
        )
        await full.aupdate(
            config,
            {
                "messages": [
                    HumanMessage(
                        content="full seed",
                        id="full-message",
                    )
                ]
            },
            as_node="full_seed",
        )

        delta = _accessor(
            saver,
            mode="delta",
            snapshot_frequency=2,
            as_node="delta_append",
        )
        await delta.aupdate(
            config,
            {
                "messages": [
                    AIMessage(
                        content="first delta",
                        id="delta-message-1",
                    )
                ]
            },
            as_node="delta_append",
        )
        after_first_delta = await delta.aget(config)
        assert _message_contents(after_first_delta) == [
            "full seed",
            "first delta",
        ]

        await delta.aupdate(
            config,
            {
                "messages": [
                    HumanMessage(
                        content="second delta",
                        id="delta-message-2",
                    )
                ]
            },
            as_node="delta_append",
        )
        before_reopen = await delta.aget(config)
        assert _message_contents(before_reopen) == [
            "full seed",
            "first delta",
            "second delta",
        ]
        await write_thread_goal(
            saver,
            thread_id,
            build_goal_state("Keep PostgreSQL delta ancestry intact"),
        )
        after_goal = await delta.aget(config)
        checkpoint_id = after_goal.config["configurable"]["checkpoint_id"]
        assert _message_contents(after_goal) == [
            "full seed",
            "first delta",
            "second delta",
        ]
        assert after_goal.values["goal"]["objective"] == ("Keep PostgreSQL delta ancestry intact")

    async with make_checkpointer(app_config) as reopened_saver:
        reopened = _accessor(
            reopened_saver,
            mode="delta",
            snapshot_frequency=2,
            as_node="delta_reopen",
        )
        after_reopen = await reopened.aget(config)
        assert after_reopen.config["configurable"]["checkpoint_id"] == checkpoint_id
        assert _message_contents(after_reopen) == [
            "full seed",
            "first delta",
            "second delta",
        ]
        assert after_reopen.values["goal"]["objective"] == ("Keep PostgreSQL delta ancestry intact")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_async_postgres_delta_storage_shape_and_snapshot_cadence(
    migrated_postgres_database_url: str,
) -> None:
    thread_id = "checkpoint-postgres-delta-cadence"
    config = _config(thread_id)
    app_config = _app_config(
        migrated_postgres_database_url,
        mode="delta",
        snapshot_frequency=2,
    )

    async with make_checkpointer(app_config) as saver:
        accessor = _accessor(
            saver,
            mode="delta",
            snapshot_frequency=2,
            as_node="delta_cadence",
        )
        for index in range(1, 6):
            await accessor.aupdate(
                config,
                {
                    "messages": [
                        HumanMessage(
                            content=f"message {index}",
                            id=f"cadence-message-{index}",
                        )
                    ]
                },
                as_node="delta_cadence",
            )

        history = list(reversed(await accessor.ahistory(config)))
        assert len(history) == 5
        assert ["messages" in item.values for item in history] == [True, True, True, True, True]
        raw_items = list(
            reversed(
                [
                    item
                    async for item in saver.alist(
                        config,
                    )
                ]
            )
        )
        assert ["messages" in item.checkpoint["channel_values"] for item in raw_items] == [True, False, True, False, True]
        assert ["counters_since_delta_snapshot" in item.metadata for item in raw_items] == [False, True, False, True, False]
        assert all(item.metadata[CHECKPOINT_MODE_METADATA_KEY] == "delta" for item in raw_items)
        assert _message_contents(history[-1]) == [
            "message 1",
            "message 2",
            "message 3",
            "message 4",
            "message 5",
        ]

    engine = create_async_engine(migrated_postgres_database_url)
    try:
        assert await _storage_counts(engine, thread_id) == (5, 3, 4)
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """SELECT
                            (metadata->>'step')::integer AS step,
                            metadata->>'deerflow_checkpoint_channel_mode' AS mode,
                            metadata ? 'counters_since_delta_snapshot' AS has_counter
                        FROM checkpoints
                        WHERE thread_id=:thread_id AND checkpoint_ns=''
                        ORDER BY (metadata->>'step')::integer"""
                    ),
                    {
                        "thread_id": thread_id,
                    },
                )
            ).all()
        assert [(row.step, row.mode, row.has_counter) for row in rows] == [
            (0, "delta", False),
            (1, "delta", True),
            (2, "delta", False),
            (3, "delta", True),
            (4, "delta", False),
        ]
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_scoped_postgres_preserves_both_markers_and_isolates_scope(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        app_config = _app_config(
            migrated_postgres_database_url,
            mode="delta",
            snapshot_frequency=2,
        )
        async with make_checkpointer(app_config) as raw_saver:
            project_checkpointer = ProjectScopedCheckpointer(
                raw_saver,
                seed.factory,
            )
            service = PrivateThreadService(
                seed.factory,
                project_checkpointer,
            )
            thread = await service.create(
                seed.owner_a,
                thread_id="checkpoint-postgres-private-scope",
                agent=ThreadAgentRef(
                    seed.project_agent_id,
                    "project",
                ),
            )
            scoped_owner = project_checkpointer.for_context(seed.owner_a)
            accessor = _accessor(
                scoped_owner,
                mode="delta",
                snapshot_frequency=2,
                as_node="private_delta",
            )
            config = _config(
                thread.thread_id,
                metadata={
                    PRIVATE_SCOPE_MARKER: {
                        "project_id": "forged-project",
                        "owner_user_id": "forged-owner",
                    }
                },
            )
            await accessor.aupdate(
                config,
                {
                    "messages": [
                        HumanMessage(
                            content="private delta",
                            id="private-message",
                        )
                    ]
                },
                as_node="private_delta",
            )

            materialized = await accessor.aget(_config(thread.thread_id))
            assert _message_contents(materialized) == ["private delta"]
            raw_item = await raw_saver.aget_tuple(_config(thread.thread_id))
            assert raw_item is not None
            assert raw_item.metadata[CHECKPOINT_MODE_METADATA_KEY] == "delta"
            assert raw_item.metadata[PRIVATE_SCOPE_MARKER] == {
                "project_id": str(seed.owner_a.project_id),
                "owner_user_id": str(seed.owner_a.user_id),
            }

            with pytest.raises(PrivateWorkNotFound):
                await project_checkpointer.for_context(seed.owner_b).aget_tuple(_config(thread.thread_id))
            with pytest.raises(PrivateWorkNotFound):
                await project_checkpointer.for_context(seed.project_b_owner_a).aget_tuple(_config(thread.thread_id))
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_scoped_postgres_delta_worker_rollback_restores_materialized_history(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        app_config = _app_config(
            migrated_postgres_database_url,
            mode="delta",
            snapshot_frequency=2,
        )
        async with make_checkpointer(app_config) as raw_saver:
            project_checkpointer = ProjectScopedCheckpointer(
                raw_saver,
                seed.factory,
            )
            service = PrivateThreadService(
                seed.factory,
                project_checkpointer,
            )
            thread = await service.create(
                seed.owner_a,
                thread_id="checkpoint-postgres-private-rollback",
                agent=ThreadAgentRef(
                    seed.project_agent_id,
                    "project",
                ),
            )
            scoped_saver = project_checkpointer.for_context(seed.owner_a)
            accessor = _accessor(
                scoped_saver,
                mode="delta",
                snapshot_frequency=2,
                as_node="worker_rollback_test",
            )
            config = _config(thread.thread_id)

            await accessor.aupdate(
                config,
                {
                    "messages": [
                        HumanMessage(
                            content="keep before rollback",
                            id="rollback-before",
                        )
                    ]
                },
                as_node="worker_rollback_test",
            )
            rollback_point = await _capture_rollback_point(
                accessor,
                scoped_saver,
                config,
            )
            assert rollback_point is not None

            await accessor.aupdate(
                config,
                {
                    "messages": [
                        AIMessage(
                            content="discard after failure",
                            id="rollback-after",
                        )
                    ]
                },
                as_node="worker_rollback_test",
            )
            assert _message_contents(await accessor.aget(config)) == [
                "keep before rollback",
                "discard after failure",
            ]

            restored = await _rollback_to_pre_run_checkpoint(
                accessor=accessor,
                checkpointer=scoped_saver,
                thread_id=thread.thread_id,
                run_id="rollback-test-run",
                rollback_point=rollback_point,
                snapshot_capture_failed=False,
                snapshot_frequency=2,
                allow_thread_delete=False,
            )

            assert restored is True
            assert _message_contents(await accessor.aget(config)) == [
                "keep before rollback",
            ]
            assert await service.get(seed.owner_a, thread.thread_id) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_full_accessor_rejects_postgres_delta_without_writing(
    migrated_postgres_database_url: str,
) -> None:
    thread_id = "checkpoint-postgres-full-read-gate"
    config = _config(thread_id)
    app_config = _app_config(
        migrated_postgres_database_url,
        mode="delta",
        snapshot_frequency=2,
    )
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with make_checkpointer(app_config) as saver:
            delta = _accessor(
                saver,
                mode="delta",
                snapshot_frequency=2,
                as_node="delta_seed",
            )
            await delta.aupdate(
                config,
                {
                    "messages": [
                        HumanMessage(
                            content="delta only",
                            id="delta-only-message",
                        )
                    ]
                },
                as_node="delta_seed",
            )
            delta_item = await saver.aget_tuple(config)
            assert delta_item is not None
            delta_checkpoint_id = delta_item.config["configurable"]["checkpoint_id"]
            counts_before = await _storage_counts(engine, thread_id)

            full = _accessor(
                saver,
                mode="full",
                snapshot_frequency=2,
                as_node="full_reader",
            )
            with pytest.raises(
                CheckpointModeMismatchError,
                match="requires delta mode",
            ):
                await full.aget(config)

            unchanged = await saver.aget_tuple(config)
            assert unchanged is not None
            assert unchanged.config["configurable"]["checkpoint_id"] == delta_checkpoint_id
            assert await _storage_counts(engine, thread_id) == counts_before
    finally:
        await engine.dispose()
