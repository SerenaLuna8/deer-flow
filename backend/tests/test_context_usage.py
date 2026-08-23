from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from pydantic import Field

from deerflow.agents.middlewares.provider_request_usage import (
    build_provider_request_profile,
    measure_profile_context,
)
from deerflow.agents.middlewares.summarization_middleware import (
    DeerFlowSummarizationMiddleware,
)
from deerflow.agents.provider_request_contract import (
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
)
from deerflow.runtime import context_compaction as context_compaction_module


class _NoCallModel(FakeListChatModel):
    call_count: int = 0
    requests: list[object] = Field(default_factory=list)

    def _call(self, *args: Any, **kwargs: Any) -> str:
        self.call_count += 1
        self.requests.append((args, kwargs))
        return super()._call(*args, **kwargs)


class _NoCallAnthropicModel(_NoCallModel):
    @property
    def _llm_type(self) -> str:
        return "anthropic-chat-test"


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


def test_context_usage_fraction_uses_lead_context_model_window() -> None:
    summary_model = _NoCallModel(
        responses=["must not be called"],
        profile={"max_input_tokens": 200_000},
    )
    lead_model = _NoCallModel(
        responses=["must not be called"],
        profile={"max_input_tokens": 64_000},
    )
    middleware = DeerFlowSummarizationMiddleware(
        model=summary_model,
        context_model=lead_model,
        trigger=("fraction", 0.5),
        keep=("messages", 1),
        token_counter=lambda _messages: 40_000,
    )

    usage = middleware.measure_context_usage(
        [HumanMessage(id="human-1", content="question")],
        summary_text=None,
    )

    assert usage.context_window_tokens == 64_000
    assert usage.triggers[0]["threshold_tokens"] == 32_000
    assert usage.triggers[0]["reached"] is True
    assert summary_model.call_count == 0
    assert lead_model.call_count == 0


def test_context_usage_default_counter_uses_the_lead_model_provider_tuning() -> None:
    summary_model = _NoCallModel(responses=["must not be called"])
    lead_model = _NoCallAnthropicModel(responses=["must not be called"])
    middleware = DeerFlowSummarizationMiddleware(
        model=summary_model,
        context_model=lead_model,
        trigger=("tokens", 10_000),
        keep=("messages", 1),
    )
    messages = [HumanMessage(id="human-1", content="x" * 3_300)]

    usage = middleware.measure_context_usage(messages, summary_text=None)

    assert usage.estimated_tokens == count_tokens_approximately(
        messages,
        chars_per_token=3.3,
        use_usage_metadata_scaling=True,
    )
    assert middleware._partial_token_counter(messages) == count_tokens_approximately(
        messages,
        chars_per_token=3.3,
        use_usage_metadata_scaling=False,
    )


def test_explicit_public_default_counter_still_uses_lead_provider_tuning() -> None:
    middleware = DeerFlowSummarizationMiddleware(
        model=_NoCallModel(responses=["must not be called"]),
        context_model=_NoCallAnthropicModel(responses=["must not be called"]),
        trigger=("tokens", 10_000),
        keep=("messages", 1),
        token_counter=count_tokens_approximately,
    )
    messages = [HumanMessage(id="human-1", content="x" * 3_300)]

    usage = middleware.measure_context_usage(messages, summary_text=None)

    assert usage.estimated_tokens == count_tokens_approximately(
        messages,
        chars_per_token=3.3,
        use_usage_metadata_scaling=True,
    )


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


def test_disabled_summarization_still_returns_all_frozen_profile_components(
    monkeypatch,
) -> None:
    model = _NoCallModel(
        responses=["must not be called"],
        profile={"max_input_tokens": 10_000},
    )
    profile = build_provider_request_profile(
        model=model,
        model_name="lead-model",
        provider_adapter="openai",
        system_prompt="system",
        tools=(),
    )
    monkeypatch.setattr(
        context_compaction_module,
        "create_summarization_middleware",
        lambda **_kwargs: None,
    )

    usage = context_compaction_module.measure_thread_context_usage(
        SimpleNamespace(
            values={
                "messages": [HumanMessage(content="hello")],
                PROVIDER_REQUEST_PROFILE_STATE_KEY: profile.snapshot(),
            }
        ),
        app_config=object(),
        context_model_name="lead-model",
        require_provider_request_profile=True,
    )

    assert usage.enabled is False
    assert set(usage.components) == {"compressible", "fixed", "ephemeral"}
    assert usage.estimator_revision == profile.snapshot()["estimator_revision"]


def test_public_gauge_requirement_fails_closed_without_frozen_profile() -> None:
    with pytest.raises(context_compaction_module.ContextUsageUnsupported):
        context_compaction_module.measure_thread_context_usage(
            SimpleNamespace(values={"messages": []}),
            app_config=object(),
            require_provider_request_profile=True,
        )


def test_thread_context_usage_builds_the_selected_lead_model_for_measurement(
    monkeypatch,
) -> None:
    lead_model = _NoCallAnthropicModel(
        responses=["must not be called"],
        profile={"max_input_tokens": 64_000},
    )
    built: list[dict[str, object]] = []

    class _Runtime:
        def __init__(self, app_config) -> None:
            built.append({"app_config": app_config})

        def build_chat_model(self, **kwargs):
            built.append(kwargs)
            return lead_model

    captured: dict[str, object] = {}

    class _Middleware:
        def measure_context_usage(self, messages, *, summary_text):
            captured.update(
                messages=messages,
                summary_text=summary_text,
                context_model=self.context_model,
            )
            return SimpleNamespace(
                estimated_tokens=10,
                message_count=1,
                summary_present=False,
                context_window_tokens=64_000,
                triggers=(),
                primary_trigger=None,
            )

    def factory(*, app_config, context_model=None, **_kwargs):
        captured["app_config"] = app_config
        middleware = _Middleware()
        middleware.context_model = context_model
        return middleware

    app_config = object()
    snapshot = SimpleNamespace(
        values={"messages": [HumanMessage(content="hello")]},
    )
    monkeypatch.setattr(
        context_compaction_module,
        "ModelRuntime",
        _Runtime,
        raising=False,
    )
    monkeypatch.setattr(
        context_compaction_module,
        "create_summarization_middleware",
        factory,
    )

    usage = context_compaction_module.measure_thread_context_usage(
        snapshot,
        app_config=app_config,
        context_model_name="11111111-1111-4111-8111-111111111111",
    )

    assert usage.context_window_tokens == 64_000
    assert built == [
        {"app_config": app_config},
        {
            "profile": context_compaction_module.ModelRuntimeProfile.AGENT_GRAPH,
            "model_name": "11111111-1111-4111-8111-111111111111",
            "thinking_enabled": False,
        },
    ]
    assert captured["context_model"] is lead_model
    assert usage.triggers == ()
    assert usage.primary_trigger is None


def test_thread_context_usage_uses_frozen_profile_safety_for_gauge_and_triggers(
    monkeypatch,
) -> None:
    lead_model = _NoCallModel(
        responses=["must not be called"],
        profile={"max_input_tokens": 1_000},
    )
    profile = build_provider_request_profile(
        model=lead_model,
        model_name="lead-model",
        provider_adapter="openai",
        system_prompt="large system prompt " * 80,
        tools=(),
        authority_identity="run-1",
    )
    values = {
        "messages": [HumanMessage(content="hello")],
        PROVIDER_REQUEST_PROFILE_STATE_KEY: profile.snapshot(),
        PROVIDER_REQUEST_MEASUREMENT_STATE_KEY: {
            "version": 1,
            "profile_fingerprint": profile.profile_fingerprint,
            "model_name": "lead-model",
            "authority_identity": "run-1",
            "run_id": "run-1",
            "provider_input_tokens": 321,
        },
    }
    expected = measure_profile_context(profile, values)
    middleware = DeerFlowSummarizationMiddleware(
        model=lead_model,
        context_model=lead_model,
        trigger=("tokens", expected.estimated_tokens + 1),
        keep=("messages", 1),
    )
    monkeypatch.setattr(
        context_compaction_module,
        "create_summarization_middleware",
        lambda **_kwargs: middleware,
    )

    usage = context_compaction_module.measure_thread_context_usage(
        SimpleNamespace(values=values),
        app_config=object(),
        context_model_name="lead-model",
        expected_authority_identity="run-1",
    )

    assert usage.estimated_tokens == expected.estimated_tokens
    assert usage.error_allowance_tokens == expected.error_allowance_tokens
    assert usage.safety_bound_tokens == expected.safety_bound_tokens
    assert usage.provider_input_tokens == 321
    assert usage.estimator_revision == profile.snapshot()["estimator_revision"]
    assert usage.error_contract == profile.snapshot()["error_contract"]
    assert usage.components == {name: component.snapshot() for name, component in expected.components.items()}
    assert usage.fixed_over_trigger is True
    assert usage.triggers[0]["current_value"] == expected.safety_bound_tokens
    assert usage.triggers[0]["reached"] is True


def test_automatic_snip_skips_when_fixed_profile_alone_exceeds_token_trigger() -> None:
    model = _NoCallModel(
        responses=["must not be called"],
        profile={"max_input_tokens": 100_000},
    )
    profile = build_provider_request_profile(
        model=model,
        model_name="lead-model",
        provider_adapter="openai",
        system_prompt="fixed system material " * 1_000,
        tools=(),
    )
    state = {
        "messages": [
            HumanMessage(id="human-1", content="question one"),
            AIMessage(id="ai-1", content="answer one"),
            HumanMessage(id="human-2", content="question two"),
            AIMessage(id="ai-2", content="answer two"),
        ],
        PROVIDER_REQUEST_PROFILE_STATE_KEY: profile.snapshot(),
    }
    measured = measure_profile_context(profile, state)
    fixed_safety = measured.components["fixed"].safety_bound_tokens
    assert fixed_safety < profile.max_input_tokens
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        context_model=model,
        trigger=("tokens", fixed_safety),
        keep=("messages", 1),
    )

    result = middleware.compact_state(
        state,
        SimpleNamespace(context={}),
        force=False,
    )

    assert result is None
    assert model.call_count == 0
