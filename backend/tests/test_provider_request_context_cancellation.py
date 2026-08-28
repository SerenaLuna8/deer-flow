from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.provider_request_usage import (
    FinalProviderRequestGuard,
    ProviderRequestEvidenceObserver,
    ProviderRequestUsageUnsupported,
    build_provider_request_profile,
)
from deerflow.runtime.context_evidence import (
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProviderAmbiguityReason,
    ProviderCallIdentity,
    ProviderRetrySafety,
)


def _model() -> GenericFakeChatModel:
    return GenericFakeChatModel(
        messages=iter([AIMessage(content="unused")]),
        profile={"max_input_tokens": 100_000},
    )


class _Observer(ProviderRequestEvidenceObserver):
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def record_request_prepared(
        self,
        measurement: FinalRequestMeasurement,
        /,
    ) -> ProviderCallIdentity:
        self.events.append(("prepared", None))
        return ProviderCallIdentity.derive(
            subject=ContextSubject.lead_thread(thread_id="thread-1"),
            generation=ContextWindowGeneration(generation_id=UUID("44444444-4444-4444-8444-444444444444")),
            source_checkpoint_id="checkpoint-1",
            graph_step="lead:model",
            model_call_ordinal=1,
            request_fingerprint=measurement.request_fingerprint,
        )

    async def record_request_dispatched(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None:
        del provider_call
        self.events.append(("dispatched", None))

    async def record_provider_observed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        input_tokens: int,
    ) -> None:
        del provider_call, input_tokens

    async def record_provider_usage_unreported(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None:
        del provider_call

    async def record_provider_failed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        failure_code: str,
        retry_safety: ProviderRetrySafety,
    ) -> None:
        del provider_call, failure_code, retry_safety

    async def record_provider_ambiguous(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        reason: ProviderAmbiguityReason,
    ) -> None:
        del provider_call
        self.events.append(("ambiguous", reason))


class _BlockingOutcomeObserver(_Observer):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def record_provider_usage_unreported(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None:
        del provider_call
        self.started.set()
        await self.release.wait()
        self.events.append(("usage_unreported", None))


@pytest.mark.asyncio
async def test_cancelled_provider_dispatch_is_durably_marked_ambiguous() -> None:
    model = _model()
    profile = build_provider_request_profile(
        model=model,
        model_name="lead",
        provider_adapter="openai",
        system_prompt="system",
        tools=(),
        supports_vision=False,
    )
    request = ModelRequest(
        model=model,
        messages=[HumanMessage(content="hello")],
        system_prompt="system",
        tools=[],
        state={"messages": [HumanMessage(content="hello")]},
        runtime=Runtime(context={"run_id": "run-1"}),
    )
    observer = _Observer()

    async def cancelled_handler(_request: ModelRequest) -> ModelCallResult:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await asyncio.sleep(0)
        raise AssertionError("cancel must interrupt the Provider handler")

    with pytest.raises(asyncio.CancelledError):
        await FinalProviderRequestGuard(
            profile,
            evidence_observer=observer,
        ).awrap_model_call(request, cancelled_handler)

    assert observer.events == [
        ("prepared", None),
        ("dispatched", None),
        ("ambiguous", ProviderAmbiguityReason.DISPATCH_OUTCOME_UNKNOWN),
    ]


@pytest.mark.asyncio
async def test_unmeasured_visual_is_prepared_for_partial_projection_before_guard_failure() -> None:
    model = _model()
    # ``deepseek`` declares no per-image token cost, so its visual material
    # keeps the fail-closed dispatch contract exercised here.
    profile = build_provider_request_profile(
        model=model,
        model_name="lead",
        provider_adapter="deepseek",
        system_prompt="system",
        tools=(),
        supports_vision=True,
    )
    image_message = HumanMessage(
        content=[
            {"type": "text", "text": "inspect"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AA=="},
            },
        ]
    )
    request = ModelRequest(
        model=model,
        messages=[image_message],
        system_prompt="system",
        tools=[],
        state={"messages": [image_message]},
        runtime=Runtime(context={"run_id": "run-1"}),
    )
    observer = _Observer()
    called = False

    async def handler(_request: ModelRequest) -> ModelCallResult:
        nonlocal called
        called = True
        raise AssertionError("unmeasured visual must not reach Provider")

    with pytest.raises(ProviderRequestUsageUnsupported):
        await FinalProviderRequestGuard(
            profile,
            evidence_observer=observer,
        ).awrap_model_call(request, handler)

    assert called is False
    assert observer.events == [("prepared", None)]


@pytest.mark.asyncio
async def test_cancellation_during_outcome_ack_joins_durable_evidence() -> None:
    model = _model()
    profile = build_provider_request_profile(
        model=model,
        model_name="lead",
        provider_adapter="openai",
        system_prompt="system",
        tools=(),
        supports_vision=False,
    )
    request = ModelRequest(
        model=model,
        messages=[HumanMessage(content="hello")],
        system_prompt="system",
        tools=[],
        state={"messages": [HumanMessage(content="hello")]},
        runtime=Runtime(context={"run_id": "run-1"}),
    )
    observer = _BlockingOutcomeObserver()

    async def handler(_request: ModelRequest) -> ModelCallResult:
        return ModelResponse(result=[AIMessage(content="answer")])

    operation = asyncio.create_task(
        FinalProviderRequestGuard(
            profile,
            evidence_observer=observer,
        ).awrap_model_call(request, handler)
    )
    await observer.started.wait()
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    observer.release.set()

    with pytest.raises(asyncio.CancelledError):
        await operation

    assert observer.events == [
        ("prepared", None),
        ("dispatched", None),
        ("usage_unreported", None),
    ]
