from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.gateway.routers.private_work import (
    _prepend_admitted_human_input_response,
)


def _record():
    response = {
        "version": 1,
        "kind": "human_input_response",
        "source": "ask_clarification",
        "request_id": "clarification:call-form",
        "response_kind": "text",
        "value": "Environment: staging",
        "form_values": {"environment": "staging"},
    }
    message = {
        "type": "human",
        "id": "human-response",
        "content": [
            {
                "type": "text",
                "text": ('For your clarification "Provide deployment details", my answer is: Environment: staging [values: {"environment":"staging"}]'),
            }
        ],
        "additional_kwargs": {
            "hide_from_ui": True,
            "human_input_response": response,
        },
    }
    return (
        SimpleNamespace(
            run_id="run-form-response",
            kwargs={"input": {"messages": [message]}},
            created_at=datetime(2026, 8, 17, tzinfo=UTC),
        ),
        message,
    )


def test_recovers_admitted_human_input_response_when_journal_row_is_missing() -> None:
    record, message = _record()

    rows = _prepend_admitted_human_input_response(
        record,
        [
            {
                "run_id": record.run_id,
                "seq": 9,
                "content": {
                    "type": "ai",
                    "id": "answer",
                    "content": "Deployment details received.",
                },
                "metadata": {"caller": "lead_agent"},
                "created_at": "2026-08-17T00:00:01+00:00",
            }
        ],
        include_admission=True,
    )

    assert rows[0] == {
        "run_id": record.run_id,
        "seq": 0,
        "content": message,
        "metadata": {"caller": "lead_agent", "source": "run_admission"},
        "created_at": "2026-08-17T00:00:00+00:00",
    }
    assert rows[1]["content"]["id"] == "answer"


def test_does_not_duplicate_a_journaled_human_input_response() -> None:
    record, message = _record()
    existing = {
        "run_id": record.run_id,
        "seq": 3,
        "content": message,
        "metadata": {"caller": "lead_agent"},
        "created_at": "2026-08-17T00:00:00+00:00",
    }

    assert _prepend_admitted_human_input_response(
        record,
        [existing],
        include_admission=True,
    ) == [existing]


def test_does_not_recover_before_the_oldest_history_page() -> None:
    record, _message = _record()

    assert (
        _prepend_admitted_human_input_response(
            record,
            [],
            include_admission=False,
        )
        == []
    )
