"""Behavior contract for materialized whole-state checkpoint replacement."""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Overwrite

from deerflow.runtime.checkpoint_state import (
    CheckpointStateAccessor,
    build_state_mutation_graph,
)


@pytest.mark.parametrize("mode", ["full", "delta"])
def test_replacement_restores_schema_defaults_for_current_only_channels(
    mode: str,
) -> None:
    graph = build_state_mutation_graph("replace_state", mode)  # type: ignore[arg-type]
    accessor = CheckpointStateAccessor.bind(
        graph,
        object(),
        mode=mode,  # type: ignore[arg-type]
    )

    replacement = accessor.replacement_values(
        {"messages": []},
        current_values={
            "messages": [],
            "artifacts": ["outputs/stale.txt"],
            "viewed_images": {
                "input.png": {
                    "mime_type": "image/png",
                    "storage_uri": "storage://stale",
                },
            },
            "summary_text": "stale summary",
        },
    )

    assert isinstance(replacement["artifacts"], Overwrite)
    assert replacement["artifacts"].value == []
    assert isinstance(replacement["viewed_images"], Overwrite)
    assert replacement["viewed_images"].value == {}
    assert replacement["summary_text"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "delta"])
async def test_replacement_has_the_same_materialized_result_in_each_mode(
    mode: str,
) -> None:
    graph = build_state_mutation_graph("replace_state", mode)  # type: ignore[arg-type]
    accessor = CheckpointStateAccessor.bind(
        graph,
        InMemorySaver(),
        mode=mode,  # type: ignore[arg-type]
    )
    config = {
        "configurable": {
            "thread_id": f"replacement-{mode}",
            "checkpoint_ns": "",
        },
    }
    initial = {
        "messages": [],
        "artifacts": ["outputs/stale.txt"],
        "viewed_images": {
            "input.png": {
                "mime_type": "image/png",
            },
        },
    }
    await accessor.aupdate(
        config,
        accessor.replacement_values(initial, current_values={}),
        as_node="replace_state",
    )
    current = await accessor.aget(config)

    await accessor.aupdate(
        config,
        accessor.replacement_values(
            {"messages": []},
            current_values=current.values,
        ),
        as_node="replace_state",
    )

    materialized = await accessor.aget(config)
    assert materialized.values["artifacts"] == []
    assert materialized.values["viewed_images"] == {}


def test_replacement_preserves_explicit_none_and_copies_source_values() -> None:
    graph = build_state_mutation_graph("replace_state", "full")
    accessor = CheckpointStateAccessor.bind(graph, object(), mode="full")
    source_artifacts = ["outputs/selected.txt"]

    replacement = accessor.replacement_values(
        {
            "artifacts": source_artifacts,
            "summary_text": None,
        },
        current_values={
            "artifacts": ["outputs/stale.txt"],
            "summary_text": "stale summary",
        },
    )
    source_artifacts.append("outputs/changed-after-projection.txt")

    assert replacement["artifacts"].value == ["outputs/selected.txt"]
    assert replacement["summary_text"] is None
