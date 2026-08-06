from __future__ import annotations

from typing import Any

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import Field

from deerflow.agents.middlewares.summarization_middleware import (
    DeerFlowSummarizationMiddleware,
)
from deerflow.runtime import context_compaction as context_compaction_module


class _NoCallModel(FakeListChatModel):
    call_count: int = 0
    requests: list[object] = Field(default_factory=list)

    def _call(self, *args: Any, **kwargs: Any) -> str:
        self.call_count += 1
        self.requests.append((args, kwargs))
        return super()._call(*args, **kwargs)


def test_context_usage_matches_trigger_count_semantics_and_selects_closest_or_clause() -> None:
    model = _NoCallModel(
        responses=["must not be called"],
        profile={"max_input_tokens": 1_000},
    )
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        trigger=[
            ("tokens", 400),
            ("fraction", 0.5),
            ("messages", 4),
        ],
        keep=("messages", 1),
        token_counter=lambda messages: len(messages) * 100,
    )

    usage = middleware.measure_context_usage(
        [
            HumanMessage(id="human-1", content="question"),
            AIMessage(id="ai-1", content="answer"),
        ],
        summary_text="retained summary",
    )

    assert usage.estimated_tokens == 300
    assert usage.message_count == 3
    assert usage.summary_present is True
    assert usage.context_window_tokens == 1_000
    assert usage.triggers == (
        {
            "type": "tokens",
            "configured_value": 400,
            "current_value": 300,
            "threshold_value": 400,
            "remaining_value": 100,
            "progress_percent": 75.0,
            "reached": False,
            "threshold_tokens": 400,
        },
        {
            "type": "fraction",
            "configured_value": 0.5,
            "current_value": 0.3,
            "threshold_value": 0.5,
            "remaining_value": 0.2,
            "progress_percent": 60.0,
            "reached": False,
            "context_window_tokens": 1_000,
            "threshold_tokens": 500,
        },
        {
            "type": "messages",
            "configured_value": 4,
            "current_value": 3,
            "threshold_value": 4,
            "remaining_value": 1,
            "progress_percent": 75.0,
            "reached": False,
        },
    )
    assert usage.primary_trigger == usage.triggers[0]
    assert model.call_count == 0


def test_context_usage_does_not_count_an_empty_summary() -> None:
    middleware = DeerFlowSummarizationMiddleware(
        model=_NoCallModel(responses=["must not be called"]),
        trigger=("messages", 3),
        keep=("messages", 1),
        token_counter=lambda messages: len(messages) * 7,
    )

    usage = middleware.measure_context_usage(
        [HumanMessage(id="human-1", content="question")],
        summary_text="",
    )

    assert usage.estimated_tokens == 7
    assert usage.message_count == 1
    assert usage.summary_present is False
    assert usage.context_window_tokens is None
    assert usage.triggers[0]["current_value"] == 1


def test_disabled_context_usage_has_no_fabricated_model_window(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        context_compaction_module,
        "create_summarization_middleware",
        lambda **_kwargs: None,
    )
    snapshot = type(
        "Snapshot",
        (),
        {
            "values": {
                "messages": [HumanMessage(id="human-1", content="question")],
                "summary_text": "retained summary",
            }
        },
    )()

    usage = context_compaction_module.measure_thread_context_usage(
        snapshot,
        app_config=object(),
    )

    assert usage.enabled is False
    assert usage.estimated_tokens == 0
    assert usage.message_count == 0
    assert usage.summary_present is True
    assert usage.context_window_tokens is None
    assert usage.triggers == ()
    assert usage.primary_trigger is None
