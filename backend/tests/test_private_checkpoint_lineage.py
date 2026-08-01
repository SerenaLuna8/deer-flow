from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.private_work.checkpoint_lineage import (
    CheckpointLineageIntegrityError,
    find_settled_checkpoint_before_message,
)


def _snapshot(
    checkpoint_id: str,
    messages: list[object],
    *,
    parent_id: str | None = None,
    next_tasks: tuple[str, ...] = (),
):
    return SimpleNamespace(
        values={"messages": messages},
        config={
            "configurable": {
                "thread_id": "thread-1",
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
            }
        },
        parent_config=(
            {
                "configurable": {
                    "thread_id": "thread-1",
                    "checkpoint_ns": "",
                    "checkpoint_id": parent_id,
                }
            }
            if parent_id is not None
            else None
        ),
        metadata={},
        next=next_tasks,
        created_at="2026-07-30T00:00:00+00:00",
    )


class _ProjectScopedAccessor:
    def __init__(self, snapshots: list[object]) -> None:
        self._snapshots = {item.config["configurable"]["checkpoint_id"]: item for item in snapshots}
        self.requested: list[dict[str, object]] = []

    async def aget(self, config):
        self.requested.append(config)
        checkpoint_id = config["configurable"]["checkpoint_id"]
        return self._snapshots.get(
            checkpoint_id,
            SimpleNamespace(
                values={},
                config={},
                parent_config=None,
                metadata=None,
                next=(),
                created_at=None,
            ),
        )


@pytest.mark.asyncio
async def test_lineage_returns_the_previous_settled_checkpoint_not_a_pending_input() -> None:
    human_1 = HumanMessage(id="human-1", content="first")
    ai_1 = AIMessage(id="ai-1", content="first answer")
    human_2 = HumanMessage(id="human-2", content="second")
    snapshots = [
        _snapshot(
            "head",
            [human_1, ai_1, human_2, AIMessage(id="ai-2", content="second answer")],
            parent_id="turn-2-mid",
        ),
        _snapshot(
            "turn-2-mid",
            [human_1, ai_1, human_2],
            parent_id="turn-2-input",
            next_tasks=("model",),
        ),
        _snapshot(
            "turn-2-input",
            [human_1, ai_1],
            parent_id="turn-1-tail",
            next_tasks=("__start__",),
        ),
        _snapshot("turn-1-tail", [human_1, ai_1]),
    ]
    accessor = _ProjectScopedAccessor(snapshots)

    base = await find_settled_checkpoint_before_message(
        accessor,
        snapshots[0],
        "human-2",
        max_depth=20,
    )

    assert base.config["configurable"]["checkpoint_id"] == "turn-1-tail"
    assert [config["configurable"]["checkpoint_id"] for config in accessor.requested] == [
        "turn-2-mid",
        "turn-2-input",
        "turn-1-tail",
    ]


@pytest.mark.asyncio
async def test_lineage_rejects_an_unaddressable_parent_instead_of_scanning_siblings() -> None:
    human = HumanMessage(id="human-1", content="question")
    head = _snapshot(
        "head",
        [human, AIMessage(id="ai-1", content="answer")],
        parent_id="missing",
    )
    accessor = _ProjectScopedAccessor([head])

    with pytest.raises(CheckpointLineageIntegrityError):
        await find_settled_checkpoint_before_message(
            accessor,
            head,
            "human-1",
            max_depth=20,
        )
