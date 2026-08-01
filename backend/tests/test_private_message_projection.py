from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.private_work.message_projection import (
    compute_run_durations,
    project_checkpoint_message_durations,
    project_event_message_durations,
)
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope


def test_event_duration_is_projected_only_to_the_authoritative_last_visible_ai() -> None:
    rows = [
        {
            "run_id": "run-1",
            "seq": 1,
            "content": {"type": "ai", "content": "planning"},
            "metadata": {"caller": "lead_agent"},
        },
        {
            "run_id": "run-1",
            "seq": 2,
            "content": {"type": "tool", "content": "result"},
            "metadata": {"caller": "lead_agent"},
        },
        {
            "run_id": "run-1",
            "seq": 3,
            "content": {"type": "ai", "content": "final"},
            "metadata": {"caller": "lead_agent"},
        },
    ]

    projected = project_event_message_durations(
        rows,
        run_durations={"run-1": 7},
        last_visible_ai_seq_by_run={"run-1": 3},
    )

    assert "additional_kwargs" not in projected[0]["content"]
    assert projected[2]["content"]["additional_kwargs"]["turn_duration"] == 7
    assert "additional_kwargs" not in rows[2]["content"]


def test_event_duration_is_not_projected_to_a_subagent_tail() -> None:
    rows = [
        {
            "run_id": "run-1",
            "seq": 1,
            "content": {"type": "ai", "content": "visible lead answer"},
            "metadata": {"caller": "lead_agent"},
        },
        {
            "run_id": "run-1",
            "seq": 2,
            "content": {"type": "ai", "content": "nested answer"},
            "metadata": {"caller": "subagent:research"},
        },
    ]

    projected = project_event_message_durations(
        rows,
        run_durations={"run-1": 7},
        last_visible_ai_seq_by_run={"run-1": 2},
    )

    assert all("additional_kwargs" not in row["content"] for row in projected)


def test_event_duration_preserves_a_legacy_visible_ai_without_reserved_caller() -> None:
    rows = [
        {
            "run_id": "run-1",
            "seq": 1,
            "content": {"type": "ai", "content": "visible legacy answer"},
            "metadata": {},
        },
    ]

    projected = project_event_message_durations(
        rows,
        run_durations={"run-1": 7},
        last_visible_ai_seq_by_run={"run-1": 1},
    )

    assert projected[0]["content"]["additional_kwargs"]["turn_duration"] == 7


def test_checkpoint_duration_uses_human_run_boundaries_and_only_final_ai() -> None:
    messages = [
        {
            "type": "human",
            "content": "question",
            "additional_kwargs": {"run_id": "run-1"},
        },
        {"type": "ai", "content": "planning"},
        {"type": "tool", "content": "result"},
        {"type": "ai", "content": "final"},
    ]

    projected = project_checkpoint_message_durations(
        messages,
        run_durations={"run-1": 9},
    )

    assert "additional_kwargs" not in projected[1]
    assert projected[3]["additional_kwargs"]["turn_duration"] == 9
    assert "additional_kwargs" not in messages[3]


def test_run_duration_is_non_negative_whole_wall_clock_seconds() -> None:
    created_at = datetime(2026, 7, 30, tzinfo=UTC)
    records = [
        SimpleNamespace(
            run_id="run-1",
            status="success",
            created_at=created_at,
            updated_at=created_at + timedelta(seconds=4.9),
        ),
        SimpleNamespace(
            run_id="run-clock-skew",
            status="success",
            created_at=created_at,
            updated_at=created_at - timedelta(seconds=2),
        ),
        SimpleNamespace(
            run_id="run-still-active",
            status="running",
            created_at=created_at,
            updated_at=created_at + timedelta(seconds=8),
        ),
        SimpleNamespace(
            run_id="run-failed",
            status="failed",
            created_at=created_at,
            updated_at=created_at + timedelta(seconds=12),
        ),
    ]

    assert compute_run_durations(records) == {
        "run-1": 4,
        "run-clock-skew": 0,
    }


class _VisibleAIStore(DbRunEventStore):
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = (
            rows
            if rows is not None
            else [
                {
                    "run_id": "run-1",
                    "seq": 2,
                    "content": {"type": "ai", "content": "visible lead answer"},
                    "metadata": {"caller": "lead_agent"},
                },
                {
                    "run_id": "run-1",
                    "seq": 3,
                    "content": {"type": "ai", "content": "subagent bookkeeping"},
                    "metadata": {"caller": "subagent:research"},
                },
                {
                    "run_id": "run-1",
                    "seq": 4,
                    "content": {"type": "ai", "content": "middleware bookkeeping"},
                    "metadata": {"caller": "middleware:test"},
                },
                {
                    "run_id": "run-1",
                    "seq": 5,
                    "content": {
                        "type": "ai",
                        "content": "hidden answer",
                        "additional_kwargs": {"hide_from_ui": True},
                    },
                    "metadata": {"caller": "lead_agent"},
                },
            ]
        )
        self.before_seqs: list[int | None] = []

    async def list_messages_by_run(
        self,
        thread_id,
        run_id,
        *,
        limit=50,
        before_seq=None,
        after_seq=None,
        user_id=None,
        scope=None,
    ):
        del thread_id, run_id, user_id, scope
        self.before_seqs.append(before_seq)
        rows = self.rows
        if before_seq is not None:
            rows = [row for row in rows if row["seq"] < before_seq]
        if after_seq is not None:
            rows = [row for row in rows if row["seq"] > after_seq]
            return rows[:limit]
        return rows[-limit:]


@pytest.mark.asyncio
async def test_db_last_visible_ai_skips_hidden_middleware_and_subagent_tail() -> None:
    store = _VisibleAIStore()
    scope = PrivateResourceScope(
        project_id="11111111-1111-4111-8111-111111111111",
        owner_user_id="22222222-2222-4222-8222-222222222222",
        membership_version=1,
    )

    assert await store.get_last_visible_ai_seq_by_run(
        "thread-1",
        {"run-1"},
        scope=scope,
    ) == {"run-1": 2}


@pytest.mark.asyncio
async def test_resolved_visible_lead_ai_receives_duration_after_nested_and_hidden_tails() -> None:
    store = _VisibleAIStore()
    scope = PrivateResourceScope(
        project_id="11111111-1111-4111-8111-111111111111",
        owner_user_id="22222222-2222-4222-8222-222222222222",
        membership_version=1,
    )

    last_visible = await store.get_last_visible_ai_seq_by_run(
        "thread-1",
        {"run-1"},
        scope=scope,
    )
    projected = project_event_message_durations(
        store.rows,
        run_durations={"run-1": 13},
        last_visible_ai_seq_by_run=last_visible,
    )

    assert projected[0]["content"]["additional_kwargs"]["turn_duration"] == 13
    assert all("turn_duration" not in row["content"].get("additional_kwargs", {}) for row in projected[1:])


@pytest.mark.asyncio
async def test_last_visible_ai_pages_before_more_than_two_hundred_nested_tail_rows() -> None:
    rows = [
        {
            "run_id": "run-1",
            "seq": 1,
            "content": {"type": "ai", "content": "visible lead answer"},
            "metadata": {"caller": "lead_agent"},
        },
        {
            "run_id": "run-1",
            "seq": 2,
            "content": {"type": "tool", "content": "lead tool result"},
            "metadata": {"caller": "lead_agent"},
        },
        *[
            {
                "run_id": "run-1",
                "seq": seq,
                "content": {"type": "ai", "content": f"nested tail {seq}"},
                "metadata": {"caller": "subagent:research"},
            }
            for seq in range(3, 203)
        ],
    ]
    store = _VisibleAIStore(rows)
    scope = PrivateResourceScope(
        project_id="11111111-1111-4111-8111-111111111111",
        owner_user_id="22222222-2222-4222-8222-222222222222",
        membership_version=1,
    )

    assert await store.get_last_visible_ai_seq_by_run(
        "thread-1",
        {"run-1"},
        scope=scope,
    ) == {"run-1": 1}
    assert store.before_seqs == [None, 3]
