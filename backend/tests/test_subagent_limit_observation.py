from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from deerflow.agents.middlewares.subagent_limit_middleware import (
    SubagentLimitMiddleware,
    SubagentLimitObservation,
)
from deerflow.runtime.journal import RunJournal


class _JournalCapture:
    def __init__(self) -> None:
        self.observations: list[SubagentLimitObservation] = []

    def record_subagent_limit_observation(
        self,
        observation: SubagentLimitObservation,
    ) -> None:
        self.observations.append(observation)


class _EventStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def put_batch(self, events: list[dict], **_kwargs) -> None:
        self.events.extend(events)


def _state() -> dict:
    return {
        "messages": [
            AIMessage(
                id="proposal-1",
                content="",
                tool_calls=[
                    {
                        "id": f"task-{index}",
                        "name": "task",
                        "args": {"description": f"private-{index}"},
                        "type": "tool_call",
                    }
                    for index in range(3)
                ],
            )
        ],
        "delegations": [
            {"id": "prior-a", "run_id": "run-1", "occurrence": 1},
            {"id": "prior-b", "run_id": "run-1", "occurrence": 1},
        ],
    }


def test_total_limit_emits_safe_live_and_owner_journal_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_frames: list[dict] = []
    monkeypatch.setattr(
        "langgraph.config.get_stream_writer",
        lambda: live_frames.append,
    )
    journal = _JournalCapture()
    runtime = SimpleNamespace(
        context={"run_id": "run-1", "__run_journal": journal},
    )

    update = SubagentLimitMiddleware(max_concurrent=3, max_total=3).after_model(
        _state(),
        runtime,
    )

    assert update is not None
    assert len(update["messages"][0].tool_calls) == 1
    assert len(journal.observations) == 1
    observation = journal.observations[0]
    assert observation.reason_code == "subagent_total_limit"
    assert observation.run_id == "run-1"
    assert observation.count_before == 2
    assert observation.proposed == 3
    assert observation.admitted == 1
    assert observation.rejected == 2
    assert observation.count_after == 3
    assert observation.hard_limit == 3
    assert live_frames == [{"type": "subagent_limit", **observation.payload()}]
    assert "args" not in repr(live_frames)
    assert "private-" not in repr(live_frames)


@pytest.mark.asyncio
async def test_run_journal_deduplicates_total_limit_observation() -> None:
    store = _EventStore()
    journal = RunJournal("run-1", "thread-1", store)
    capture = _JournalCapture()
    runtime = SimpleNamespace(
        context={"run_id": "run-1", "__run_journal": capture},
    )
    SubagentLimitMiddleware(max_concurrent=3, max_total=3).after_model(
        _state(),
        runtime,
    )
    observation = capture.observations[0]

    journal.record_subagent_limit_observation(observation)
    journal.record_subagent_limit_observation(observation)
    await journal.flush()

    assert len(store.events) == 1
    event = store.events[0]
    assert event["event_type"] == "middleware:subagent_limit"
    assert event["content"] == observation.payload()
    assert event["metadata"] == {
        "reason_code": "subagent_total_limit",
        "observation_id": observation.observation_id,
    }
