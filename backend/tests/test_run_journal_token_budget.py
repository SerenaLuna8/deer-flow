from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage

from deerflow.agents.middlewares.token_budget_middleware import (
    TOKEN_BUDGET_STATUS_KEY,
)
from deerflow.runtime.journal import RunJournal


class _RecordingEventStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def put_batch(self, events: list[dict], **_kwargs: object) -> None:
        self.events.extend(events)


@pytest.mark.anyio
async def test_final_token_budget_status_is_reconciled_into_message_history() -> None:
    store = _RecordingEventStore()
    journal = RunJournal(
        "run-budget",
        "thread-budget",
        store,
        flush_threshold=100,
    )
    final_message = AIMessage(
        id="answer-budget",
        content="BUDGET_OK",
        response_metadata={
            TOKEN_BUDGET_STATUS_KEY: {
                "version": 1,
                "status": "exceeded",
                "reason": "total",
            }
        },
    )

    journal.on_chain_end(
        {"messages": [final_message]},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    reconciled = [event for event in store.events if event["event_type"] == "llm.ai.response"]
    assert len(reconciled) == 1
    assert reconciled[0]["category"] == "message"
    assert reconciled[0]["content"]["content"] == "BUDGET_OK"
    assert reconciled[0]["content"]["response_metadata"] == {
        TOKEN_BUDGET_STATUS_KEY: {
            "version": 1,
            "status": "exceeded",
            "reason": "total",
        }
    }
