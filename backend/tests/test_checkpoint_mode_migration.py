from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from deerflow.agents.thread_state import (
    get_thread_state_schema,
    merge_message_writes,
)
from deerflow.config.database_config import DatabaseConfig
from deerflow.runtime.checkpoint_mode import (
    CHECKPOINT_MODE_METADATA_KEY,
    CheckpointModeMismatchError,
    CheckpointModeReconfigurationError,
    aensure_checkpoint_mode_compatible,
    ensure_checkpoint_mode_compatible,
    freeze_checkpoint_channel_mode,
    freeze_checkpoint_snapshot_frequency,
    inject_checkpoint_mode,
)


def _config() -> dict[str, object]:
    return {
        "configurable": {
            "thread_id": "thread-checkpoint-mode",
            "checkpoint_ns": "",
        }
    }


def test_database_config_defaults_to_full_checkpoint_mode() -> None:
    config = DatabaseConfig(url="postgresql://postgres@localhost/deerflow")

    assert config.checkpoint_channel_mode == "full"
    assert config.checkpoint_delta.snapshot_frequency == 10


def test_database_config_carries_legacy_delta_frequency() -> None:
    config = DatabaseConfig.model_validate(
        {
            "url": "postgresql://postgres@localhost/deerflow",
            "checkpoint_delta_snapshot_frequency": 7,
        }
    )

    assert config.checkpoint_delta.snapshot_frequency == 7


def test_process_checkpoint_mode_and_frequency_are_restart_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deerflow.runtime.checkpoint_mode as checkpoint_mode

    monkeypatch.setattr(checkpoint_mode, "_frozen_checkpoint_channel_mode", None)
    monkeypatch.setattr(checkpoint_mode, "_frozen_checkpoint_snapshot_frequency", None)

    assert freeze_checkpoint_channel_mode("delta") == "delta"
    assert freeze_checkpoint_snapshot_frequency(7) == 7
    with pytest.raises(CheckpointModeReconfigurationError, match="restart"):
        freeze_checkpoint_channel_mode("full")
    with pytest.raises(CheckpointModeReconfigurationError, match="restart"):
        freeze_checkpoint_snapshot_frequency(8)


def test_inject_checkpoint_mode_stamps_only_delta_metadata() -> None:
    delta = _config()
    inject_checkpoint_mode(delta, "delta")
    assert delta["configurable"]["__deerflow_checkpoint_channel_mode"] == "delta"  # type: ignore[index]
    assert delta["metadata"][CHECKPOINT_MODE_METADATA_KEY] == "delta"  # type: ignore[index]

    full = _config()
    inject_checkpoint_mode(full, "full")
    assert full["configurable"]["__deerflow_checkpoint_channel_mode"] == "full"  # type: ignore[index]
    assert CHECKPOINT_MODE_METADATA_KEY not in full.get("metadata", {})  # type: ignore[operator]


def test_full_mode_rejects_delta_tuple_before_write() -> None:
    saver = MagicMock()
    saver.get_tuple.return_value = SimpleNamespace(
        metadata={CHECKPOINT_MODE_METADATA_KEY: "delta"},
    )

    with pytest.raises(CheckpointModeMismatchError, match="requires delta mode"):
        ensure_checkpoint_mode_compatible(saver, _config(), "full")


@pytest.mark.asyncio
async def test_async_full_mode_rejects_legacy_delta_counter_marker() -> None:
    saver = AsyncMock()
    saver.aget_tuple.return_value = SimpleNamespace(
        metadata={"counters_since_delta_snapshot": {"messages": (1, 1)}},
    )

    with pytest.raises(CheckpointModeMismatchError, match="requires delta mode"):
        await aensure_checkpoint_mode_compatible(saver, _config(), "full")


def test_delta_schema_changes_only_messages_channel() -> None:
    full = get_thread_state_schema("full")
    delta = get_thread_state_schema("delta", snapshot_frequency=3)

    assert full is not delta
    assert set(full.__annotations__) == set(delta.__annotations__)
    assert "messages" in delta.__annotations__


def test_merge_message_writes_matches_add_replace_remove_and_remove_all() -> None:
    first = HumanMessage(content="one", id="m1")
    second = HumanMessage(content="two", id="m2")
    replaced = HumanMessage(content="ONE", id="m1")

    merged = merge_message_writes(
        [first],
        [
            [second],
            [replaced],
        ],
    )
    assert [(message.id, message.content) for message in merged] == [
        ("m1", "ONE"),
        ("m2", "two"),
    ]

    removed = merge_message_writes(merged, [[RemoveMessage(id="m2")]])
    assert [(message.id, message.content) for message in removed] == [
        ("m1", "ONE"),
    ]

    reset = merge_message_writes(
        removed,
        [
            [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                HumanMessage(content="fresh", id="m3"),
            ]
        ],
    )
    assert [(message.id, message.content) for message in reset] == [
        ("m3", "fresh"),
    ]


def test_merge_message_writes_rejects_unknown_remove() -> None:
    with pytest.raises(ValueError, match="doesn't exist"):
        merge_message_writes([], [[RemoveMessage(id="missing")]])
