from __future__ import annotations

from typing import Literal

import pytest
from langchain_core.messages import HumanMessage
from langgraph.channels import DeltaChannel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Overwrite

from app.private_work.checkpoint_state import replacement_values
from deerflow.agents.thread_state import DeltaThreadState, get_thread_state_schema
from deerflow.runtime.checkpoint_mode import (
    CHECKPOINT_MODE_METADATA_KEY,
    CheckpointModeMismatchError,
)
from deerflow.runtime.checkpoint_state import (
    CheckpointStateAccessor,
    build_state_mutation_graph,
)
from deerflow.runtime.goal import build_goal_state, write_thread_goal

CheckpointMode = Literal["full", "delta"]


def _config(thread_id: str) -> dict[str, object]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }


def _message_contents(snapshot: object) -> list[str]:
    values = getattr(snapshot, "values")
    return [message.content for message in values["messages"]]


def test_delta_mutation_graph_uses_delta_thread_state() -> None:
    graph = build_state_mutation_graph("state_migration", "delta")

    assert get_thread_state_schema("delta") is DeltaThreadState
    assert isinstance(graph.channels["messages"], DeltaChannel)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["full", "delta"])
async def test_accessor_materializes_get_history_and_overwrite_update(
    mode: CheckpointMode,
) -> None:
    saver = InMemorySaver()
    graph = build_state_mutation_graph("state_migration", mode)
    accessor = CheckpointStateAccessor.bind(graph, saver, mode=mode)
    config = _config(f"materialized-{mode}")

    await accessor.aupdate(
        config,
        {
            "messages": Overwrite(
                [
                    HumanMessage(content="first", id="message-1"),
                    HumanMessage(content="second", id="message-2"),
                ]
            ),
            "title": "before",
        },
        as_node="state_migration",
    )
    first = await accessor.aget(config)

    await accessor.aupdate(
        config,
        {
            "messages": Overwrite([HumanMessage(content="replacement", id="message-3")]),
            "title": "after",
        },
        as_node="state_migration",
    )
    latest = await accessor.aget(config)
    history = await accessor.ahistory(config, limit=2)

    assert _message_contents(first) == ["first", "second"]
    assert first.values["title"] == "before"
    assert _message_contents(latest) == ["replacement"]
    assert latest.values["title"] == "after"
    assert [item.values["title"] for item in history] == ["after", "before"]
    assert [_message_contents(item) for item in history] == [
        ["replacement"],
        ["first", "second"],
    ]
    assert not isinstance(latest.values["messages"], Overwrite)

    stored = await saver.aget_tuple(config)
    assert stored is not None
    if mode == "delta":
        assert stored.metadata[CHECKPOINT_MODE_METADATA_KEY] == "delta"
    else:
        assert CHECKPOINT_MODE_METADATA_KEY not in stored.metadata
    assert config == _config(f"materialized-{mode}")


@pytest.mark.anyio
async def test_full_accessor_fails_closed_when_reading_delta_checkpoint() -> None:
    saver = InMemorySaver()
    config = _config("delta-read-gate")
    delta_accessor = CheckpointStateAccessor.bind(
        build_state_mutation_graph("delta_seed", "delta"),
        saver,
        mode="delta",
    )
    await delta_accessor.aupdate(
        config,
        {"messages": Overwrite([HumanMessage(content="delta-only", id="message-delta")])},
        as_node="delta_seed",
    )

    seeded = await saver.aget_tuple(config)
    assert seeded is not None
    assert seeded.metadata[CHECKPOINT_MODE_METADATA_KEY] == "delta"
    seeded_checkpoint_id = seeded.config["configurable"]["checkpoint_id"]

    full_accessor = CheckpointStateAccessor.bind(
        build_state_mutation_graph("full_reader", "full"),
        saver,
        mode="full",
    )
    with pytest.raises(CheckpointModeMismatchError, match="requires delta mode"):
        await full_accessor.aget(config)
    with pytest.raises(CheckpointModeMismatchError, match="requires delta mode"):
        await full_accessor.ahistory(config)

    unchanged = await saver.aget_tuple(config)
    assert unchanged is not None
    assert unchanged.config["configurable"]["checkpoint_id"] == seeded_checkpoint_id


def test_private_state_replacement_rejects_unknown_channels() -> None:
    accessor = CheckpointStateAccessor.bind(
        build_state_mutation_graph("state_migration", "delta"),
        InMemorySaver(),
        mode="delta",
    )

    with pytest.raises(
        RuntimeError,
        match="does not expose source channels: middleware_extension",
    ):
        replacement_values(
            accessor,
            {
                "messages": [],
                "middleware_extension": {"must": "not be dropped"},
            },
        )


@pytest.mark.anyio
async def test_raw_goal_writer_preserves_delta_message_ancestry() -> None:
    saver = InMemorySaver()
    config = _config("delta-goal-ancestry")
    accessor = CheckpointStateAccessor.bind(
        build_state_mutation_graph(
            "delta_seed",
            "delta",
            snapshot_frequency=2,
        ),
        saver,
        mode="delta",
    )
    await accessor.aupdate(
        config,
        {
            "messages": Overwrite(
                [
                    HumanMessage(
                        content="keep me",
                        id="message-before-goal",
                    )
                ]
            )
        },
        as_node="delta_seed",
    )

    await write_thread_goal(
        saver,
        "delta-goal-ancestry",
        build_goal_state("Preserve the delta message history"),
    )

    materialized = await accessor.aget(config)
    assert _message_contents(materialized) == ["keep me"]
    assert materialized.values["goal"]["objective"] == ("Preserve the delta message history")
    assert len(await accessor.ahistory(config)) == 2
