from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage

from deerflow.runtime.journal import RunJournal


class _RecordingEventStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def put_batch(self, events: list[dict], **_kwargs: object) -> None:
        self.events.extend(events)


@pytest.mark.anyio
async def test_fatal_llm_fallback_cannot_journal_a_successful_run_end() -> None:
    store = _RecordingEventStore()
    journal = RunJournal(
        "run-fallback",
        "thread-fallback",
        store,
        flush_threshold=100,
    )
    journal.on_chain_end(
        {
            "messages": [
                AIMessage(
                    content="The image could not be read.",
                    additional_kwargs={
                        "deerflow_error_fallback": True,
                        "error_detail": "CURRENT_UPLOAD_UNAVAILABLE",
                    },
                )
            ]
        },
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    terminal_events = [event for event in store.events if event["event_type"] == "run.end"]
    assert len(terminal_events) == 1
    assert terminal_events[0]["metadata"] == {"status": "error"}
    assert journal.had_llm_error_fallback is True
    assert journal.llm_error_fallback_message == "CURRENT_UPLOAD_UNAVAILABLE"


@pytest.mark.anyio
async def test_stale_fallback_cannot_poison_a_later_successful_run_end() -> None:
    store = _RecordingEventStore()
    journal = RunJournal(
        "run-success",
        "thread-success",
        store,
        flush_threshold=100,
    )
    journal.on_chain_end(
        {
            "messages": [
                AIMessage(
                    id="old-fallback",
                    content="The image could not be read.",
                    additional_kwargs={
                        "deerflow_error_fallback": True,
                        "error_detail": "CURRENT_UPLOAD_UNAVAILABLE",
                    },
                ),
                AIMessage(id="current-success", content="Current answer"),
            ]
        },
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    terminal_events = [event for event in store.events if event["event_type"] == "run.end"]
    assert len(terminal_events) == 1
    assert terminal_events[0]["metadata"] == {"status": "success"}
    assert journal.had_llm_error_fallback is False
    assert journal.llm_error_fallback_message is None
