"""Compaction receipt basis and automatic-degradation contract.

The compaction receipt invariant (``result_tokens <= source_tokens``) is only
meaningful when both sides are measured on the same basis.  The stored
Provider-request snapshot measures the wire projection at the previous model
call, while the receipt re-measures retained state from full message payloads;
comparing those two bases rejects legitimate compactions.  These tests pin:

1. the receipt re-measures the *source* state with the same estimator used for
   the result state, so the invariant compares like with like;
2. the middleware forwards the exact pre-compaction state to the observer;
3. automatic (``before_model``) compaction degrades receipt-stage failures to
   a typed skip instead of terminating the Run, while explicit force paths
   keep typed errors;
4. static receipt preconditions are checked before any SNIP model call.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import Field, ValidationError

from app.private_work.context_replacement import (
    compaction_checkpoint_receipt,
    remeasure_replacement_checkpoint,
)
from deerflow.agents.context_compaction_warning import (
    CONTEXT_COMPACTION_WARNING_STATE_KEY,
    ContextCompactionFailureReason,
    ContextCompactionMiddlewareState,
    read_context_compaction_warning,
)
from deerflow.agents.memory.snip import SNIP_ARCHIVE_PROMPT
from deerflow.agents.middlewares.provider_request_usage import (
    build_provider_request_profile,
)
from deerflow.agents.middlewares.summarization_middleware import (
    DeerFlowSummarizationMiddleware,
    SnipCompactionFailed,
)
from deerflow.agents.provider_request_contract import (
    CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
)
from deerflow.runtime.context_evidence import (
    CompactionProjection,
    ContextCheckpointEstimator,
    ContextCheckpointProjectionSnapshot,
    ContextContribution,
    ContextLane,
    ContextModelProjection,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    TokenEstimate,
    VisualTokenCostContractError,
)
from deerflow.runtime.serialization import serialize_channel_values

SOURCE_GENERATION = ContextWindowGeneration(
    generation_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
)
RESULT_GENERATION = ContextWindowGeneration(
    generation_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
)


class _RecordingModel(FakeListChatModel):
    prompts: list[str] = Field(default_factory=list)
    call_count: int = 0

    def _call(self, *args: Any, **kwargs: Any) -> str:
        self.call_count += 1
        return super()._call(*args, **kwargs)


def _dual(continuity: str, tagged: str) -> str:
    return f"<continuity>\n{continuity}\n</continuity>\n{tagged}"


def _model() -> _RecordingModel:
    return _RecordingModel(
        responses=[_dual("Compact continuity.", "- [durable] Compact fact.")],
        custom_get_token_ids=lambda text: list(range(len(text))),
    )


def _stale_wire_snapshot(*, projected_tokens: int) -> ContextCheckpointProjectionSnapshot:
    """A stored snapshot whose wire measurement is far below the re-measure."""

    return ContextCheckpointProjectionSnapshot(
        generation=SOURCE_GENERATION,
        model=ContextModelProjection(
            identity_digest="f" * 64,
            context_window_tokens=300_000,
        ),
        measurement=FinalRequestMeasurement(
            request_fingerprint="a" * 64,
            adapter_revision="test-v1",
            contributions=(
                ContextContribution(
                    contribution_id="b" * 64,
                    source_identity_digest="c" * 64,
                    lane=ContextLane.CONVERSATION,
                    model_visible_bytes=projected_tokens * 4,
                    token_estimate=TokenEstimate.bounded(
                        projected_tokens=projected_tokens,
                        lower_bound_tokens=projected_tokens,
                        safety_upper_bound_tokens=projected_tokens + 5,
                    ),
                ),
            ),
        ),
        compaction=CompactionProjection(enabled=False, reached=False),
        estimator=ContextCheckpointEstimator(
            error_allowance_ratio=0.2,
            provider_fixed_overhead_tokens=10,
            provider_per_message_overhead_tokens=2,
            provider_per_tool_overhead_tokens=3,
            fixed_message_count=1,
            tool_count=2,
        ),
    )


def test_receipt_remeasures_source_on_the_result_basis() -> None:
    """A retained tail larger than the stale wire snapshot must not be rejected."""

    archived = HumanMessage(id="archived-head", content="archived " * 200)
    retained = AIMessage(id="retained-tail", content="retained " * 400)
    source_values = {
        "messages": [archived, retained],
        "summary_text": None,
    }
    result_values = {
        "messages": [retained],
        "summary_text": "compact continuity",
    }

    receipt = compaction_checkpoint_receipt(
        _stale_wire_snapshot(projected_tokens=10),
        source_checkpoint_id="checkpoint-source",
        source_values=source_values,
        checkpoint_values=result_values,
        result_generation=RESULT_GENERATION,
    )

    assert receipt.result_tokens <= receipt.source_tokens
    # The source side is a fresh same-basis re-measure, not the stale wire value.
    assert receipt.source_tokens > 10
    assert receipt.result_tokens == receipt.projection_snapshot.measurement.projected_tokens


def test_receipt_still_rejects_a_result_larger_than_its_source() -> None:
    """Same-basis comparison keeps the real invariant against growth."""

    small = HumanMessage(id="small-source", content="tiny")
    huge = AIMessage(id="huge-result", content="grown " * 500)

    with pytest.raises(ValidationError):
        compaction_checkpoint_receipt(
            _stale_wire_snapshot(projected_tokens=10),
            source_checkpoint_id="checkpoint-source",
            source_values={"messages": [small], "summary_text": None},
            checkpoint_values={"messages": [huge], "summary_text": "s"},
            result_generation=RESULT_GENERATION,
        )


def test_receipt_counts_declared_visual_cost_when_an_image_is_archived() -> None:
    """A bounded image source must not look smaller than its text summary."""

    source = _stale_wire_snapshot(projected_tokens=10).model_copy(
        update={
            "estimator": ContextCheckpointEstimator(
                error_allowance_ratio=0.2,
                provider_fixed_overhead_tokens=256,
                provider_per_message_overhead_tokens=32,
                provider_per_tool_overhead_tokens=96,
                fixed_message_count=1,
                tool_count=0,
                visual_max_tokens_per_image=2_048,
            ),
        },
    )
    source_values = {
        "messages": [
            HumanMessage(
                id="image-source",
                content=[
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,aGVsbG8=",
                        },
                    },
                ],
            ),
            AIMessage(id="image-response", content="ok"),
        ],
        "summary_text": None,
    }

    receipt = compaction_checkpoint_receipt(
        source,
        source_checkpoint_id="checkpoint-source",
        source_values=source_values,
        checkpoint_values={
            "messages": [],
            "summary_text": "x" * 500,
        },
        result_generation=RESULT_GENERATION,
    )

    remeasured = remeasure_replacement_checkpoint(
        source,
        generation=RESULT_GENERATION,
        checkpoint_values=source_values,
    )
    visual = next(contribution for contribution in remeasured.measurement.contributions if contribution.lane is ContextLane.VISUAL_MEDIA)
    assert visual.token_estimate.projected_tokens == 2_048
    assert visual.token_estimate.safety_upper_bound_tokens == 2_048
    assert receipt.result_tokens < receipt.source_tokens


def test_receipt_rejects_visual_replacement_without_a_frozen_cost_bound() -> None:
    """An unmeasured image cannot participate in the size invariant as zero."""

    with pytest.raises(VisualTokenCostContractError) as caught:
        compaction_checkpoint_receipt(
            _stale_wire_snapshot(projected_tokens=10),
            source_checkpoint_id="checkpoint-source",
            source_values={
                "messages": [
                    HumanMessage(
                        id="image-source",
                        content=[
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,aGVsbG8=",
                                },
                            },
                        ],
                    ),
                    AIMessage(id="image-response", content="ok"),
                ],
                "summary_text": None,
            },
            checkpoint_values={
                "messages": [],
                "summary_text": "x" * 500,
            },
            result_generation=RESULT_GENERATION,
        )

    assert caught.value.unmeasured_items == 1


class _CapturingObserver:
    """Records the receipt inputs, then fails receipt validation."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def prepare_compaction_checkpoint_receipt(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        raise ValidationError.from_exception_data(
            "ContextCompactionCheckpointReceipt",
            [],
        )


class _ActualReceiptObserver:
    """Exercise the real receipt seam behind the middleware observer port."""

    def prepare_compaction_checkpoint_receipt(
        self,
        **kwargs: Any,
    ) -> Any:
        raw_source = kwargs["source_snapshot"]
        assert isinstance(raw_source, dict)
        return compaction_checkpoint_receipt(
            ContextCheckpointProjectionSnapshot.from_safe_mapping(raw_source),
            source_checkpoint_id=kwargs["source_checkpoint_id"],
            source_values=kwargs["source_values"],
            checkpoint_values=kwargs["result_values"],
            result_generation=RESULT_GENERATION,
        )


def _turn(prefix: str) -> list[Any]:
    return [
        HumanMessage(id=f"{prefix}-human", content=f"{prefix} user"),
        AIMessage(id=f"{prefix}-assistant", content=f"{prefix} answer"),
    ]


def _middleware(
    model: _RecordingModel,
    observer: object | None,
) -> DeerFlowSummarizationMiddleware:
    return DeerFlowSummarizationMiddleware(
        model=model,
        trigger=("messages", 1),
        keep=("messages", 1),
        trim_tokens_to_summarize=20_000,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
        context_compaction_observer=observer,
    )


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        context={"thread_id": "thread-1"},
        execution_info=SimpleNamespace(checkpoint_id="ckpt-1"),
    )


def _snapshot_state(messages: list[Any]) -> dict[str, Any]:
    return {
        "messages": messages,
        CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY: (_stale_wire_snapshot(projected_tokens=10).to_safe_mapping()),
    }


def test_compaction_warning_reader_accepts_only_the_closed_v1_contract() -> None:
    warning = {
        "version": 1,
        "disposition": "skip_this_turn",
        "reason": "checkpoint_unmeasurable",
    }

    parsed = read_context_compaction_warning(warning)

    assert parsed == warning
    assert parsed["reason"] is ContextCompactionFailureReason.CHECKPOINT_UNMEASURABLE
    assert read_context_compaction_warning({**warning, "version": 2}) is None
    assert read_context_compaction_warning({**warning, "reason": "other"}) is None
    assert read_context_compaction_warning({**warning, "extra": True}) is None


def test_compaction_warning_is_checkpoint_persistent_and_private() -> None:
    warning = {
        "version": 1,
        "disposition": "skip_this_turn",
        "reason": "checkpoint_unmeasurable",
    }
    builder = StateGraph(ContextCompactionMiddlewareState)
    builder.add_node(
        "warn",
        lambda _state: {CONTEXT_COMPACTION_WARNING_STATE_KEY: warning},
    )
    builder.add_edge(START, "warn")
    builder.add_edge("warn", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "compaction-warning-thread"}}

    graph.invoke({"messages": [HumanMessage(content="hello")]}, config)
    checkpoint = graph.get_state(config)

    assert read_context_compaction_warning(checkpoint.values[CONTEXT_COMPACTION_WARNING_STATE_KEY]) == warning
    assert CONTEXT_COMPACTION_WARNING_STATE_KEY not in serialize_channel_values(dict(checkpoint.values))


def test_auto_compaction_receipt_failure_degrades_to_skip_and_forwards_source() -> None:
    model = _model()
    observer = _CapturingObserver()
    middleware = _middleware(model, observer)
    messages = [*_turn("first"), *_turn("second")]
    state = _snapshot_state(messages)

    update = middleware.before_model(state, _runtime())  # type: ignore[arg-type]

    assert update == {
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "receipt_invalid",
        }
    }
    assert model.call_count > 0
    assert observer.kwargs is not None
    source_values = observer.kwargs.get("source_values")
    assert isinstance(source_values, dict)
    assert list(source_values["messages"]) == messages
    assert source_values["summary_text"] is None


def test_auto_compaction_rejects_undeclared_visual_cost_before_snip_call() -> None:
    """Automatic compaction does not spend a model call on an unsafe receipt."""

    model = _model()
    middleware = _middleware(model, _ActualReceiptObserver())
    state = _snapshot_state(
        [
            HumanMessage(
                id="first-human",
                content=[
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,aGVsbG8=",
                        },
                    },
                ],
            ),
            AIMessage(id="first-assistant", content="first answer"),
            *_turn("second"),
        ],
    )

    update = middleware.before_model(state, _runtime())  # type: ignore[arg-type]

    assert update == {
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "checkpoint_unmeasurable",
        }
    }
    assert model.call_count == 0


@pytest.mark.asyncio
async def test_async_auto_compaction_rejects_undeclared_visual_cost_before_snip_call() -> None:
    model = _model()
    middleware = _middleware(model, _ActualReceiptObserver())
    state = _snapshot_state(
        [
            HumanMessage(
                id="first-human",
                content=[
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,aGVsbG8=",
                        },
                    },
                ],
            ),
            AIMessage(id="first-assistant", content="first answer"),
            *_turn("second"),
        ],
    )

    update = await middleware.abefore_model(  # type: ignore[arg-type]
        state,
        _runtime(),
    )

    assert update == {
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "checkpoint_unmeasurable",
        }
    }
    assert model.call_count == 0


def test_bootstrap_receipt_estimator_keeps_the_frozen_visual_bound() -> None:
    """Pre-Provider compaction inherits vision cost from the frozen profile."""

    model = _RecordingModel(
        responses=[_dual("Compact continuity.", "- [durable] Compact fact.")],
        custom_get_token_ids=lambda text: list(range(len(text))),
        profile={"max_input_tokens": 100_000},
    )
    profile = build_provider_request_profile(
        model=model,
        model_name="lead",
        provider_adapter="openai",
        system_prompt="system",
        tools=(),
        supports_vision=True,
    )
    observer = _CapturingObserver()
    middleware = DeerFlowSummarizationMiddleware(
        model=model,
        context_model=model,
        trigger=("tokens", 20_000),
        keep=("tokens", 100),
        trim_tokens_to_summarize=20_000,
        summary_prompt=SNIP_ARCHIVE_PROMPT,
        context_compaction_observer=observer,
    )
    state = {
        "messages": [
            HumanMessage(id="first-human", content="x" * 60_000),
            AIMessage(id="first-assistant", content="first answer"),
            HumanMessage(id="second-human", content="second question"),
            AIMessage(id="second-assistant", content="second answer"),
        ],
        PROVIDER_REQUEST_PROFILE_STATE_KEY: profile.snapshot(),
    }

    update = middleware.before_model(state, _runtime())  # type: ignore[arg-type]

    assert update == {
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "receipt_invalid",
        }
    }
    assert observer.kwargs is not None
    estimator = observer.kwargs["estimator"]
    assert isinstance(estimator, ContextCheckpointEstimator)
    assert estimator.visual_max_tokens_per_image == 2_048


@pytest.mark.asyncio
async def test_async_receipt_failure_returns_the_same_typed_skip() -> None:
    model = _model()
    observer = _CapturingObserver()
    middleware = _middleware(model, observer)
    state = _snapshot_state([*_turn("first"), *_turn("second")])

    update = await middleware.abefore_model(  # type: ignore[arg-type]
        state,
        _runtime(),
    )

    assert update == {
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "receipt_invalid",
        }
    }
    assert model.call_count > 0


def test_missing_estimator_skips_before_any_snip_model_call() -> None:
    model = _model()
    middleware = _middleware(model, _CapturingObserver())
    state = {"messages": [*_turn("first"), *_turn("second")]}

    update = middleware.before_model(state, _runtime())  # type: ignore[arg-type]

    assert update == {
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "checkpoint_unmeasurable",
        }
    }
    assert model.call_count == 0


def test_non_triggered_turn_is_none_and_clears_a_prior_skip_warning() -> None:
    model = _model()
    middleware = _middleware(model, None)
    messages = _turn("only")

    assert (
        middleware.before_model(  # type: ignore[arg-type]
            {"messages": messages},
            _runtime(),
        )
        is None
    )
    assert middleware.before_model(  # type: ignore[arg-type]
        {
            "messages": messages,
            "context_compaction_warning": {
                "version": 1,
                "disposition": "skip_this_turn",
                "reason": "checkpoint_unmeasurable",
            },
        },
        _runtime(),
    ) == {"context_compaction_warning": None}


def test_successful_compaction_clears_a_prior_skip_warning() -> None:
    model = _model()
    middleware = _middleware(model, None)
    state = {
        "messages": [*_turn("first"), *_turn("second")],
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "checkpoint_unmeasurable",
        },
    }

    update = middleware.before_model(state, _runtime())  # type: ignore[arg-type]

    assert update is not None
    assert update["context_compaction_warning"] is None


@pytest.mark.asyncio
async def test_async_successful_compaction_clears_a_prior_skip_warning() -> None:
    model = _model()
    middleware = _middleware(model, None)
    state = {
        "messages": [*_turn("first"), *_turn("second")],
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "checkpoint_unmeasurable",
        },
    }

    update = await middleware.abefore_model(  # type: ignore[arg-type]
        state,
        _runtime(),
    )

    assert update is not None
    assert update["context_compaction_warning"] is None


def test_missing_observer_api_skips_before_any_snip_model_call() -> None:
    model = _model()
    middleware = _middleware(model, object())
    state = _snapshot_state([*_turn("first"), *_turn("second")])

    update = middleware.before_model(state, _runtime())  # type: ignore[arg-type]

    assert update == {
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "observer_unsupported",
        }
    }
    assert model.call_count == 0


@pytest.mark.asyncio
async def test_async_missing_observer_api_returns_the_same_typed_skip() -> None:
    model = _model()
    middleware = _middleware(model, object())
    state = _snapshot_state([*_turn("first"), *_turn("second")])

    update = await middleware.abefore_model(  # type: ignore[arg-type]
        state,
        _runtime(),
    )

    assert update == {
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "observer_unsupported",
        }
    }
    assert model.call_count == 0


@pytest.mark.asyncio
async def test_async_missing_estimator_returns_the_same_typed_skip() -> None:
    model = _model()
    middleware = _middleware(model, _CapturingObserver())
    state = {"messages": [*_turn("first"), *_turn("second")]}

    update = await middleware.abefore_model(  # type: ignore[arg-type]
        state,
        _runtime(),
    )

    assert update == {
        "context_compaction_warning": {
            "version": 1,
            "disposition": "skip_this_turn",
            "reason": "checkpoint_unmeasurable",
        }
    }
    assert model.call_count == 0


def test_missing_estimator_keeps_typed_error_on_force_paths() -> None:
    model = _model()
    middleware = _middleware(model, _CapturingObserver())
    state = {"messages": [*_turn("first"), *_turn("second")]}

    with pytest.raises(SnipCompactionFailed):
        middleware.compact_state(state, _runtime(), force=True)  # type: ignore[arg-type]
    assert model.call_count == 0


def test_invalid_checkpoint_keeps_stable_typed_reason_on_force_paths() -> None:
    model = _model()
    middleware = _middleware(model, _CapturingObserver())
    state = {
        "messages": [*_turn("first"), *_turn("second")],
        CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY: {"version": 999},
    }

    with pytest.raises(SnipCompactionFailed) as caught:
        middleware.compact_state(state, _runtime(), force=True)  # type: ignore[arg-type]

    assert caught.value.reason == "checkpoint_unmeasurable"
    assert str(caught.value) == "checkpoint_unmeasurable"
    assert model.call_count == 0
