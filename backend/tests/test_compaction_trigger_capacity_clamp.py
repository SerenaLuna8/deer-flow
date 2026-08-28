"""Joint invariant between the compaction trigger and Provider Model capacity.

The automatic summarization trigger is one global absolute token threshold,
while every model declares its own ``max_input_tokens`` capacity. Without a
runtime clamp, any model whose capacity sits strictly below the configured
trigger leaves a dead zone (``capacity < occupancy < trigger``): the final
Provider guard rejects the request before the trigger is ever reached, so
automatic compaction cannot participate. These tests pin the clamp at every
site that freezes a trigger value next to a known model capacity.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import Field

from app.reliability.run_execution.executor import (
    _context_compaction_threshold_tokens,
)
from deerflow.agents.middlewares import summarization_middleware as summarization_module
from deerflow.agents.middlewares.assembly import append_final_provider_request_guard
from deerflow.agents.middlewares.manifest import (
    MiddlewarePhase,
    assign_middleware_layer,
)
from deerflow.agents.middlewares.provider_request_usage import (
    ContextCapacityExceeded,
    FinalProviderRequestGuard,
    build_provider_request_profile,
    measure_profile_snapshot_context,
)
from deerflow.agents.provider_request_contract import (
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
)
from deerflow.config.summarization_config import (
    CompactionPolicyIncompatible,
    effective_compaction_trigger_tokens,
    resolve_effective_compaction_policy,
)
from deerflow.runtime.context_evidence import WindowOpenedV1


class _ProfileModel(FakeListChatModel):
    prompts: list[str] = Field(default_factory=list)


def _factory_config(
    trigger_tokens: int | None,
    *,
    keep_tokens: int = 8_000,
) -> SimpleNamespace:
    return SimpleNamespace(
        summarization=SimpleNamespace(
            enabled=True,
            model_name=None,
            trigger_tokens=trigger_tokens,
            keep=SimpleNamespace(to_tuple=lambda: ("tokens", keep_tokens)),
            trim_tokens_to_summarize=20_000,
            summary_prompt=None,
        )
    )


def _summary_model() -> _ProfileModel:
    return _ProfileModel(responses=["<continuity>\nx\n</continuity>\n(nothing)"])


def test_joint_policy_clamps_trigger_without_reducing_authorized_keep() -> None:
    policy = resolve_effective_compaction_policy(
        trigger_tokens=320_000,
        keep_tokens=8_000,
        context_window_tokens=50_000,
        fixed_noncompressible_safety_tokens=4_000,
        retained_context_safety_tokens=10_000,
    )

    assert policy.trigger_tokens == 50_000
    assert policy.keep_tokens == 8_000
    assert policy.noncompressible_safety_tokens == 4_000
    assert policy.retained_context_safety_tokens == 10_000


def test_window_evidence_marks_profile_qualified_retention_as_dynamic() -> None:
    window = WindowOpenedV1(
        model_identity_digest="a" * 64,
        context_window_tokens=1_000_000,
        compaction_enabled=True,
        compaction_threshold_tokens=320_000,
        compaction_keep_tokens=64_000,
        compaction_fixed_safety_tokens=1_000,
        compaction_summary_headroom_tokens=4_096,
        compaction_retained_safety_tokens=0,
        compaction_authority="frozen_run",
    )

    assert window.compaction_retained_safety_tokens == 0


def test_joint_policy_keeps_trigger_at_equal_capacity() -> None:
    policy = resolve_effective_compaction_policy(
        trigger_tokens=100_000,
        keep_tokens=8_000,
        context_window_tokens=100_000,
    )

    assert policy.trigger_tokens == 100_000
    assert policy.keep_tokens == 8_000


def test_joint_policy_preserves_authorized_values_when_capacity_is_unknown() -> None:
    policy = resolve_effective_compaction_policy(
        trigger_tokens=100_000,
        keep_tokens=8_000,
        context_window_tokens=None,
    )

    assert policy.trigger_tokens == 100_000
    assert policy.keep_tokens == 8_000
    assert policy.context_window_tokens is None


def test_joint_policy_rejects_keep_and_fixed_floor_at_effective_trigger() -> None:
    with pytest.raises(CompactionPolicyIncompatible):
        resolve_effective_compaction_policy(
            trigger_tokens=320_000,
            keep_tokens=40_000,
            context_window_tokens=50_000,
            fixed_noncompressible_safety_tokens=10_000,
            retained_context_safety_tokens=40_000,
        )


def test_frozen_profile_rejects_fixed_context_that_cannot_fit_after_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    context_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 8_000},
    )
    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(8_000, keep_tokens=500),
        context_model=context_model,
    )
    assert middleware is not None
    profile = build_provider_request_profile(
        model=context_model,
        model_name="context-model",
        provider_adapter="openai",
        system_prompt="x" * 64_000,
        tools=(),
    )

    with pytest.raises(CompactionPolicyIncompatible):
        summarization_module.freeze_summarization_profile(
            (middleware,),
            profile,
        )


def test_final_profile_binding_rejects_impossible_compaction_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    context_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 8_000},
    )
    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(8_000, keep_tokens=500),
        context_model=context_model,
    )
    assert middleware is not None
    profile = build_provider_request_profile(
        model=context_model,
        model_name="context-model",
        provider_adapter="openai",
        system_prompt="x" * 64_000,
        tools=(),
    )

    with pytest.raises(ContextCapacityExceeded):
        append_final_provider_request_guard(
            [
                assign_middleware_layer(
                    middleware,
                    layer_id="summarization",
                    phase=MiddlewarePhase.COMPACTION,
                    slot=10,
                    why="Test the public final-profile binding seam.",
                )
            ],
            FinalProviderRequestGuard(profile),
        )


def test_frozen_profile_defers_content_dependent_tail_safety_to_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    context_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 100_000},
    )
    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(100_000, keep_tokens=80_000),
        context_model=context_model,
    )
    assert middleware is not None
    profile = build_provider_request_profile(
        model=context_model,
        model_name="context-model",
        provider_adapter="openai",
        system_prompt="small",
        tools=(),
    )

    policy = summarization_module.freeze_summarization_profile(
        (middleware,),
        profile,
    )

    assert policy is not None
    assert policy.retained_context_safety_tokens == 0


def test_profile_safe_cutoff_drops_cjk_tail_that_approximate_keep_underprices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    context_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 100_000},
    )
    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(100_000, keep_tokens=64_000),
        context_model=context_model,
    )
    assert middleware is not None
    profile = build_provider_request_profile(
        model=context_model,
        model_name="context-model",
        provider_adapter="openai",
        system_prompt="small",
        tools=(),
    )
    snapshot = profile.snapshot()
    messages = [
        HumanMessage(id="old-user", content="old"),
        AIMessage(id="old-ai", content="old answer"),
        HumanMessage(id="large-user", content="界" * 250_000),
        AIMessage(id="large-ai", content="large answer"),
        HumanMessage(id="latest-user", content="latest"),
        AIMessage(id="latest-ai", content="latest answer"),
        HumanMessage(id="open-user", content="follow up"),
    ]
    state = {
        "messages": messages,
        PROVIDER_REQUEST_PROFILE_STATE_KEY: snapshot,
    }

    # LangChain's character counter prices the full CJK tail below keep even
    # though the Provider-wire estimator prices it above the model trigger.
    assert middleware._partial_token_counter(messages) < 64_000
    assert measure_profile_snapshot_context(snapshot, state).safety_bound_tokens >= 100_000

    cutoff = middleware._provider_safe_retention_cutoff(
        state,
        snapshot,
        protect_latest_complete_turn=True,
    )

    assert cutoff == 4
    projected = measure_profile_snapshot_context(
        snapshot,
        {**state, "messages": messages[cutoff:], "summary_text": None},
    )
    assert projected.safety_bound_tokens + summarization_module.MIN_SNIP_SUMMARY_OUTPUT_TOKENS < 100_000


def test_profile_safe_cutoff_archives_oversized_latest_turn_for_open_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    context_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 100_000},
    )
    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(100_000, keep_tokens=64_000),
        context_model=context_model,
    )
    assert middleware is not None
    snapshot = build_provider_request_profile(
        model=context_model,
        model_name="context-model",
        provider_adapter="openai",
        system_prompt="small",
        tools=(),
    ).snapshot()
    messages = [
        HumanMessage(id="old-user", content="old"),
        AIMessage(id="old-ai", content="old answer"),
        HumanMessage(id="latest-user", content="🙂" * 250_000),
        AIMessage(id="latest-ai", content="latest answer"),
        HumanMessage(id="open-user", content="follow up"),
    ]
    state = {
        "messages": messages,
        PROVIDER_REQUEST_PROFILE_STATE_KEY: snapshot,
    }

    assert (
        middleware._provider_safe_retention_cutoff(
            state,
            snapshot,
            protect_latest_complete_turn=True,
        )
        == 4
    )


def test_compacted_result_is_remeasured_with_actual_summary_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    context_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 7_000},
    )
    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(7_000, keep_tokens=1_000),
        context_model=context_model,
    )
    assert middleware is not None
    snapshot = build_provider_request_profile(
        model=context_model,
        model_name="context-model",
        provider_adapter="openai",
        system_prompt="small",
        tools=(),
    ).snapshot()
    preserved = (HumanMessage(id="open-user", content="follow up"),)
    state = {
        "messages": list(preserved),
        PROVIDER_REQUEST_PROFILE_STATE_KEY: snapshot,
    }
    oversized = summarization_module.ContextCompactionResult(
        summary_text="界" * 250_000,
        messages_to_summarize=(),
        preserved_messages=preserved,
        total_tokens=120_000,
        memory_archive_receipt=None,
    )

    assert not middleware._compacted_result_fits_provider_profile(
        state,
        oversized,
    )


def test_before_model_uses_profile_safe_cjk_cutoff_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    context_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 10_000},
    )
    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(10_000, keep_tokens=8_000),
        context_model=context_model,
    )
    assert middleware is not None
    profile = build_provider_request_profile(
        model=context_model,
        model_name="context-model",
        provider_adapter="openai",
        system_prompt="small",
        tools=(),
    )
    summarization_module.freeze_summarization_profile((middleware,), profile)
    messages = [
        HumanMessage(id="old-user", content="old"),
        AIMessage(id="old-ai", content="old answer"),
        HumanMessage(id="large-user", content="界" * 25_000),
        AIMessage(id="large-ai", content="large answer"),
        HumanMessage(id="latest-user", content="latest"),
        AIMessage(id="latest-ai", content="latest answer"),
        HumanMessage(id="open-user", content="follow up"),
    ]
    state = {
        "messages": messages,
        PROVIDER_REQUEST_PROFILE_STATE_KEY: profile.snapshot(),
    }

    update = middleware.before_model(
        state,
        SimpleNamespace(context={}, execution_info=None),  # type: ignore[arg-type]
    )

    assert update is not None
    assert update["summary_text"] == "x"
    assert [message.id for message in update["messages"][1:]] == [
        "latest-user",
        "latest-ai",
        "open-user",
    ]


def test_frozen_profile_exposes_fixed_and_summary_headroom_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    context_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 200_000},
    )
    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(100_000, keep_tokens=8_000),
        context_model=context_model,
    )
    assert middleware is not None
    profile = build_provider_request_profile(
        model=context_model,
        model_name="context-model",
        provider_adapter="openai",
        system_prompt="small",
        tools=(),
    )

    policy = summarization_module.freeze_summarization_profile(
        (middleware,),
        profile,
    )

    assert policy is not None
    assert policy.fixed_noncompressible_safety_tokens > 0
    assert policy.summary_headroom_tokens == summarization_module.MIN_SNIP_SUMMARY_OUTPUT_TOKENS


@pytest.mark.parametrize(
    ("trigger", "capacity", "expected"),
    [
        (320_000, 50_000, 50_000),
        (100_000, 200_000, 100_000),
        (100_000, 100_000, 100_000),
        (100_000, None, 100_000),
        (None, 50_000, None),
        (100_000, 0, 100_000),
    ],
)
def test_effective_trigger_clamps_to_capacity(
    trigger: int | None,
    capacity: int | None,
    expected: int | None,
) -> None:
    assert effective_compaction_trigger_tokens(trigger, capacity) == expected


def test_factory_clamps_trigger_to_context_model_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    lead_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 50_000},
    )

    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(320_000),
        context_model=lead_model,
    )

    assert middleware is not None
    assert list(middleware._trigger_conditions) == [("tokens", 50_000)]


def test_factory_rejects_keep_at_the_capacity_clamped_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    context_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 50_000},
    )

    with pytest.raises(CompactionPolicyIncompatible):
        summarization_module.create_summarization_middleware(
            app_config=_factory_config(320_000, keep_tokens=50_000),
            context_model=context_model,
        )


def test_factory_keeps_trigger_already_below_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )
    lead_model = _ProfileModel(
        responses=["unused"],
        profile={"max_input_tokens": 200_000},
    )

    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(100_000),
        context_model=lead_model,
    )

    assert middleware is not None
    assert list(middleware._trigger_conditions) == [("tokens", 100_000)]


def test_factory_without_capacity_keeps_configured_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        summarization_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: _summary_model(),
    )

    middleware = summarization_module.create_summarization_middleware(
        app_config=_factory_config(320_000),
        context_model=_ProfileModel(responses=["unused"]),
    )

    assert middleware is not None
    assert list(middleware._trigger_conditions) == [("tokens", 320_000)]
    assert middleware.keep == ("tokens", 8_000)


def _executor_app_config(
    trigger_tokens: Any,
    *,
    keep_tokens: int = 8_000,
    enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        summarization=SimpleNamespace(
            enabled=enabled,
            trigger_tokens=trigger_tokens,
            keep=SimpleNamespace(value=keep_tokens),
        ),
    )


def test_executor_threshold_clamps_to_model_capacity() -> None:
    assert (
        _context_compaction_threshold_tokens(
            _executor_app_config(320_000),
            context_window_tokens=50_000,
        )
        == 50_000
    )
    assert (
        _context_compaction_threshold_tokens(
            _executor_app_config(100_000),
            context_window_tokens=200_000,
        )
        == 100_000
    )
    assert (
        _context_compaction_threshold_tokens(
            _executor_app_config(None),
            context_window_tokens=50_000,
        )
        is None
    )
    assert (
        _context_compaction_threshold_tokens(
            _executor_app_config(320_000),
        )
        == 320_000
    )


def test_executor_rejects_frozen_keep_at_model_capacity() -> None:
    with pytest.raises(CompactionPolicyIncompatible):
        _context_compaction_threshold_tokens(
            _executor_app_config(320_000, keep_tokens=50_000),
            context_window_tokens=50_000,
        )


def test_executor_ignores_incompatible_compaction_policy_when_disabled() -> None:
    assert (
        _context_compaction_threshold_tokens(
            _executor_app_config(
                320_000,
                keep_tokens=64_000,
                enabled=False,
            ),
            context_window_tokens=64_000,
        )
        is None
    )
