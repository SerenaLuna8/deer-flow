"""Final Provider-request dispatch guard and durable Evidence observation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any, Protocol, override, runtime_checkable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

from deerflow.agents.middlewares.provider_request_cost_adapter import ProviderModelRequestCostAdapter
from deerflow.agents.middlewares.provider_request_measurement import measure_profile_context
from deerflow.agents.middlewares.provider_request_profile import (
    PROVIDER_REQUEST_ERROR_CONTRACT,
    PROVIDER_REQUEST_ESTIMATOR_REVISION,
    ContextCapacityExceeded,
    ProviderRequestContextMeasurement,
    ProviderRequestMaterialMeasurement,
    ProviderRequestMeasurementSnapshot,
    ProviderRequestProfile,
    ProviderRequestProfileDrift,
    ProviderRequestUsageUnsupported,
)
from deerflow.agents.provider_request_contract import (
    CONTEXT_COMPACTION_RECEIPT_STATE_KEY,
    CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
    provider_response_digest,
)
from deerflow.error_codes import ContextProviderCallAmbiguousError
from deerflow.models.provider_outcome import (
    ProviderFailedResponseError,
    ProviderFailedResponseProof,
    ProviderNoResponseProvenError,
    classify_provider_failed_response,
    classify_provider_no_response,
)
from deerflow.runtime.context_evidence import (
    ContextCheckpointEstimator,
    ContextCheckpointProjectionSnapshot,
    ContextTokenCostContractError,
    FinalRequestMeasurement,
    FinalShapedRequestCostAdapter,
    ProviderAmbiguityReason,
    ProviderCallIdentity,
    ProviderRetrySafety,
    VisualTokenCostContractError,
)
from deerflow.runtime.context_keys import RuntimeContextKeys

logger = logging.getLogger(__name__)


class ProviderDispatchOutcomeAmbiguous(ContextProviderCallAmbiguousError):
    """A dispatched Provider call has no outcome that is safe to retry."""

    def __init__(self) -> None:
        super().__init__("Provider dispatch outcome is ambiguous")


@runtime_checkable
class ProviderRequestEvidenceObserver(Protocol):
    """Durable, execution-bound sink for one final Provider-call lifecycle.

    Implementations bind Subject, Context Window Generation, source Checkpoint,
    graph step, ordinal, Run/lease authority, and storage.  The Guard supplies
    only content-free measurement/outcome facts and awaits every transition.
    """

    async def record_request_prepared(
        self,
        measurement: FinalRequestMeasurement,
        /,
    ) -> ProviderCallIdentity: ...

    async def record_request_dispatched(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None: ...
    async def record_provider_observed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        input_tokens: int,
    ) -> None: ...

    async def record_provider_usage_unreported(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None: ...

    async def record_provider_failed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        failure_code: str,
        retry_safety: ProviderRetrySafety,
    ) -> None: ...

    async def record_provider_ambiguous(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        reason: ProviderAmbiguityReason,
    ) -> None: ...


async def _join_durable_observer_transition(
    transition: Awaitable[None],
) -> asyncio.CancelledError | None:
    """Join one Evidence transition and defer repeated caller cancellation."""

    task = asyncio.create_task(transition)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.cancelled():
                break
            cancellation = cancellation or error
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        except Exception:
            break
    task.result()
    return cancellation


async def _record_ambiguity_despite_cancellation(
    observer: ProviderRequestEvidenceObserver,
    provider_call: ProviderCallIdentity,
) -> None:
    """Join the durable ambiguous terminal before propagating cancellation."""

    await _join_durable_observer_transition(
        observer.record_provider_ambiguous(
            provider_call,
            reason=ProviderAmbiguityReason.DISPATCH_OUTCOME_UNKNOWN,
        )
    )


async def _record_proven_no_response_failure(
    observer: ProviderRequestEvidenceObserver,
    provider_call: ProviderCallIdentity,
    *,
    failure_code: str,
) -> None:
    """Persist retry authority or fail closed as an ambiguous dispatch."""

    try:
        cancellation = await _join_durable_observer_transition(
            observer.record_provider_failed(
                provider_call,
                failure_code=failure_code,
                retry_safety=ProviderRetrySafety.NO_RESPONSE_PROVEN,
            )
        )
    except (Exception, asyncio.CancelledError) as persistence_error:
        raise ProviderDispatchOutcomeAmbiguous() from persistence_error
    if cancellation is not None:
        raise cancellation


async def _record_provider_failed_response(
    observer: ProviderRequestEvidenceObserver,
    provider_call: ProviderCallIdentity,
    *,
    proof: ProviderFailedResponseProof,
) -> None:
    """Persist a definite failure answer or fail closed as ambiguous."""

    try:
        cancellation = await _join_durable_observer_transition(
            observer.record_provider_failed(
                provider_call,
                failure_code=proof.failure_code,
                retry_safety=(ProviderRetrySafety.FAILED_RESPONSE_RETRY_SAFE if proof.retry_safe else ProviderRetrySafety.UNSAFE),
            )
        )
    except (Exception, asyncio.CancelledError) as persistence_error:
        raise ProviderDispatchOutcomeAmbiguous() from persistence_error
    if cancellation is not None:
        raise cancellation


def _model_response(result: ModelCallResult) -> ModelResponse:
    if isinstance(result, ExtendedModelResponse):
        return result.model_response
    if isinstance(result, AIMessage):
        return ModelResponse(result=[result])
    return result


def _provider_input_tokens(response: ModelResponse) -> int | None:
    for message in reversed(response.result):
        if not isinstance(message, AIMessage):
            continue
        usage = message.usage_metadata
        if isinstance(usage, Mapping):
            value = usage.get("input_tokens")
            if isinstance(value, int) and value >= 0:
                return value
    return None


def _runtime_run_id(runtime: Runtime | None) -> str | None:
    context = getattr(runtime, "context", None)
    value = context.get("run_id") if isinstance(context, Mapping) else None
    return value if isinstance(value, str) and value else None


def _runtime_token_usage_tracking_enabled(runtime: Runtime | None) -> bool:
    context = getattr(runtime, "context", None)
    if not isinstance(context, Mapping):
        return True
    return context.get(RuntimeContextKeys.TOKEN_USAGE_TRACKING_ENABLED) is not False


def _attach_measurement(
    result: ModelCallResult,
    measurement: ProviderRequestMeasurementSnapshot,
    *,
    context_projection_snapshot: ContextCheckpointProjectionSnapshot | None = None,
) -> ExtendedModelResponse:
    response = _model_response(result)
    update = {PROVIDER_REQUEST_MEASUREMENT_STATE_KEY: measurement}
    if context_projection_snapshot is not None:
        update[CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY] = context_projection_snapshot.to_safe_mapping()
        update[CONTEXT_COMPACTION_RECEIPT_STATE_KEY] = None
    if not isinstance(result, ExtendedModelResponse) or result.command is None:
        return ExtendedModelResponse(response, Command(update=update))
    existing = result.command
    if existing.update is not None and not isinstance(existing.update, Mapping):
        raise RuntimeError("Cannot merge provider-request measurement command")
    return ExtendedModelResponse(
        response,
        replace(existing, update={**dict(existing.update or {}), **update}),
    )


class FinalProviderRequestGuard(AgentMiddleware):
    """Innermost read-only meter/guard for the final shaped Lead request."""

    def __init__(
        self,
        profile: ProviderRequestProfile,
        *,
        cost_adapter: FinalShapedRequestCostAdapter[ModelRequest] | None = None,
        evidence_observer: ProviderRequestEvidenceObserver | None = None,
    ) -> None:
        self.profile = profile
        self.cost_adapter = cost_adapter or ProviderModelRequestCostAdapter.from_profile(profile)
        self.evidence_observer = evidence_observer

    @override
    def before_agent(self, state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
        return {PROVIDER_REQUEST_PROFILE_STATE_KEY: self.profile.snapshot()}

    @override
    async def abefore_agent(self, state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
        return self.before_agent(state, runtime)

    def _measure_evidence(
        self,
        request: ModelRequest,
    ) -> FinalRequestMeasurement:
        """Measure the real shaped request after validating Run authority."""

        self.profile._require_supported()
        stored = (request.state or {}).get(PROVIDER_REQUEST_PROFILE_STATE_KEY)
        if isinstance(stored, Mapping) and stored.get("profile_fingerprint") != self.profile.profile_fingerprint:
            raise ProviderRequestProfileDrift("provider_request_profile_drift: checkpoint profile changed")
        runtime_run_id = _runtime_run_id(request.runtime)
        if self.profile.authority_identity is not None and runtime_run_id != self.profile.authority_identity:
            raise ProviderRequestProfileDrift("provider_request_profile_drift: Run authority changed")
        return self.cost_adapter.measure_final_request(request)

    def _validate_final_request(
        self,
        request: ModelRequest,
        evidence_measurement: FinalRequestMeasurement,
    ) -> tuple[
        ProviderRequestMaterialMeasurement,
        ProviderRequestContextMeasurement,
    ]:
        try:
            evidence_safety_upper_bound = evidence_measurement.require_safety_upper_bound()
        except (
            ContextTokenCostContractError,
            VisualTokenCostContractError,
        ) as error:
            raise ProviderRequestUsageUnsupported(f"{error.code}:{error.unmeasured_items}") from None
        allowed = measure_profile_context(self.profile, request.state or {})
        actual = self.profile.measure_request(request)
        if actual.material_utf8_bytes > allowed.material_utf8_bytes:
            raise ProviderRequestProfileDrift("provider_request_profile_drift: final request exceeded frozen profile")
        if actual.safety_bound_tokens > allowed.safety_bound_tokens:
            raise ProviderRequestProfileDrift("provider_request_profile_drift: final request safety bound exceeded frozen profile")
        if actual.message_count > allowed.message_count:
            raise ProviderRequestProfileDrift("provider_request_profile_drift: final request message count exceeded frozen profile")
        # ``measure_request`` has already verified every exact schema fact
        # against the frozen full-tool closure. Keep the count comparison here
        # as the final aggregate contract shared with the Gauge measurement.
        if actual.tool_count > allowed.full_tool_count:
            raise ProviderRequestProfileDrift("provider_request_profile_drift: final request tool facts exceeded frozen profile")
        capacity = self.profile.max_input_tokens
        if capacity is not None and evidence_safety_upper_bound > capacity:
            raise ContextCapacityExceeded("context_capacity_exceeded: final request safety value exceeds model capacity")
        return actual, allowed

    def _measure_and_validate(
        self,
        request: ModelRequest,
    ) -> tuple[
        ProviderRequestMaterialMeasurement,
        ProviderRequestContextMeasurement,
        FinalRequestMeasurement,
    ]:
        evidence_measurement = self._measure_evidence(request)
        actual, allowed = self._validate_final_request(
            request,
            evidence_measurement,
        )
        return actual, allowed, evidence_measurement

    @staticmethod
    def _observed_provider_input_tokens(
        response: ModelResponse,
        actual: ProviderRequestMaterialMeasurement,
    ) -> tuple[int | None, bool]:
        """Record, never execute: a completed provider response is authoritative.

        An observation above the versioned engineering bound is estimator-drift
        evidence (for example a CJK-inefficient tokenizer), not a reason to
        discard an already completed, already billed provider response. The
        bound keeps protecting dispatch through capacity admission only.
        """

        provider_input_tokens = _provider_input_tokens(response)
        if provider_input_tokens is None or provider_input_tokens <= actual.safety_bound_tokens:
            return provider_input_tokens, False
        logger.warning(
            "Provider reported %d input tokens above the versioned engineering safety bound %d; recording estimator drift",
            provider_input_tokens,
            actual.safety_bound_tokens,
        )
        return provider_input_tokens, True

    def _snapshot(
        self,
        request: ModelRequest,
        actual: ProviderRequestMaterialMeasurement,
        allowed: ProviderRequestContextMeasurement,
        provider_input_tokens: int | None,
        *,
        provider_input_bound_exceeded: bool = False,
    ) -> ProviderRequestMeasurementSnapshot:
        result: ProviderRequestMeasurementSnapshot = {
            "version": 1,
            "estimator_revision": PROVIDER_REQUEST_ESTIMATOR_REVISION,
            "error_contract": PROVIDER_REQUEST_ERROR_CONTRACT,
            "model_name": self.profile.model_name,
            "profile_fingerprint": self.profile.profile_fingerprint,
            "request_fingerprint": actual.request_fingerprint,
            "estimated_tokens": actual.estimated_tokens,
            "error_allowance_tokens": actual.error_allowance_tokens,
            "safety_bound_tokens": actual.safety_bound_tokens,
            "allowed_safety_bound_tokens": allowed.safety_bound_tokens,
            "message_count": actual.message_count,
            "full_tool_count": allowed.full_tool_count,
            "components": {name: component.snapshot() for name, component in allowed.components.items()},
            "provider_input_tokens": (provider_input_tokens if self.profile.capture_provider_input_tokens and _runtime_token_usage_tracking_enabled(request.runtime) else None),
            "authority_identity": self.profile.authority_identity,
        }
        if provider_input_bound_exceeded:
            result["provider_input_tokens_exceeded_bound"] = True
        run_id = _runtime_run_id(request.runtime)
        if run_id is not None:
            result["run_id"] = run_id
        return result

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelCallResult],
    ) -> ModelCallResult:
        if self.evidence_observer is not None:
            raise RuntimeError("ProviderRequestEvidenceObserver requires awrap_model_call")
        actual, allowed, _evidence_measurement = self._measure_and_validate(request)
        result = handler(request)
        response = _model_response(result)
        provider_input_tokens, bound_exceeded = self._observed_provider_input_tokens(
            response,
            actual,
        )
        return _attach_measurement(
            result,
            self._snapshot(
                request,
                actual,
                allowed,
                provider_input_tokens,
                provider_input_bound_exceeded=bound_exceeded,
            ),
        )

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelCallResult]],
    ) -> ModelCallResult:
        evidence_measurement = self._measure_evidence(request)
        observer = self.evidence_observer
        provider_call: ProviderCallIdentity | None = None
        if observer is not None:
            provider_call = await observer.record_request_prepared(evidence_measurement)
            if provider_call.request_fingerprint != evidence_measurement.request_fingerprint:
                raise RuntimeError("ProviderRequestEvidenceObserver returned a mismatched request identity")
        actual, allowed = self._validate_final_request(
            request,
            evidence_measurement,
        )
        if observer is not None and provider_call is not None:
            await observer.record_request_dispatched(provider_call)
        try:
            result = await handler(request)
        except asyncio.CancelledError as cancellation:
            if observer is not None and provider_call is not None:
                try:
                    await _record_ambiguity_despite_cancellation(
                        observer,
                        provider_call,
                    )
                except (Exception, asyncio.CancelledError) as persistence_error:
                    raise ProviderDispatchOutcomeAmbiguous() from persistence_error
            raise cancellation
        except ProviderNoResponseProvenError as error:
            if observer is not None and provider_call is not None:
                await _record_proven_no_response_failure(
                    observer,
                    provider_call,
                    failure_code=error.failure_code,
                )
            raise
        except Exception as error:
            no_response_proof = classify_provider_no_response(
                provider_adapter=self.profile.provider_adapter,
                error=error,
            )
            if no_response_proof is not None:
                proven_error = ProviderNoResponseProvenError(
                    failure_code=no_response_proof.failure_code,
                )
                if observer is not None and provider_call is not None:
                    await _record_proven_no_response_failure(
                        observer,
                        provider_call,
                        failure_code=proven_error.failure_code,
                    )
                raise proven_error from error
            failed_response_proof = classify_provider_failed_response(
                provider_adapter=self.profile.provider_adapter,
                error=error,
            )
            if failed_response_proof is not None:
                # A definite Provider failure answer is a known outcome, not
                # an ambiguous dispatch. Record it and propagate both the
                # original SDK error and adapter-owned retry safety so the
                # outer error-handling policy cannot infer safety from status
                # alone.
                if observer is not None and provider_call is not None:
                    await _record_provider_failed_response(
                        observer,
                        provider_call,
                        proof=failed_response_proof,
                    )
                raise ProviderFailedResponseError(
                    proof=failed_response_proof,
                    provider_error=error,
                ) from error
            if observer is not None and provider_call is not None:
                try:
                    await _record_ambiguity_despite_cancellation(
                        observer,
                        provider_call,
                    )
                except (Exception, asyncio.CancelledError) as persistence_error:
                    raise ProviderDispatchOutcomeAmbiguous() from persistence_error
                raise ProviderDispatchOutcomeAmbiguous() from error
            raise
        response = _model_response(result)
        raw_provider_input_tokens = _provider_input_tokens(response)
        deferred_cancellation: asyncio.CancelledError | None = None
        if observer is not None and provider_call is not None:
            try:
                if raw_provider_input_tokens is None:
                    deferred_cancellation = await _join_durable_observer_transition(observer.record_provider_usage_unreported(provider_call))
                else:
                    deferred_cancellation = await _join_durable_observer_transition(
                        observer.record_provider_observed(
                            provider_call,
                            input_tokens=raw_provider_input_tokens,
                        )
                    )
            except (Exception, asyncio.CancelledError) as error:
                try:
                    await _join_durable_observer_transition(
                        observer.record_provider_ambiguous(
                            provider_call,
                            reason=(ProviderAmbiguityReason.OBSERVATION_PERSISTENCE_UNKNOWN),
                        )
                    )
                except (Exception, asyncio.CancelledError):
                    pass
                raise ProviderDispatchOutcomeAmbiguous() from error
        if deferred_cancellation is not None:
            raise deferred_cancellation
        provider_input_tokens, bound_exceeded = self._observed_provider_input_tokens(
            response,
            actual,
        )
        context_projection_snapshot = None
        snapshot_factory = getattr(
            observer,
            "checkpoint_projection_snapshot",
            None,
        )
        if callable(snapshot_factory):
            try:
                state_messages = (request.state or {}).get("messages")
                state_message_count = len(state_messages) if isinstance(state_messages, list) else 0
                origin_run_id = _runtime_run_id(request.runtime)
                if provider_call is None or origin_run_id is None:
                    raise RuntimeError(
                        "Context checkpoint snapshot requires Provider call authority",
                    )
                response_messages = tuple(response.result)
                summary_present = bool(
                    isinstance(
                        (request.state or {}).get("summary_text"),
                        str,
                    )
                    and (request.state or {}).get("summary_text")
                )
                fixed_message_count = max(
                    actual.message_count - state_message_count - (1 if summary_present else 0),
                    0,
                )
                candidate = snapshot_factory(
                    estimator=ContextCheckpointEstimator(
                        error_allowance_ratio=self.profile.error_allowance_ratio,
                        provider_fixed_overhead_tokens=(self.profile.provider_fixed_overhead_tokens),
                        provider_per_message_overhead_tokens=(self.profile.provider_per_message_overhead_tokens),
                        provider_per_tool_overhead_tokens=(self.profile.provider_per_tool_overhead_tokens),
                        visual_max_tokens_per_image=(self.profile.visual_max_tokens_per_image),
                        fixed_message_count=fixed_message_count,
                        tool_count=actual.tool_count,
                    ),
                    provider_call=provider_call,
                    origin_run_id=origin_run_id,
                    provider_response_message_start=state_message_count,
                    provider_response_message_count=len(response_messages),
                    provider_response_digest=provider_response_digest(
                        response_messages,
                    ),
                )
                if not isinstance(
                    candidate,
                    ContextCheckpointProjectionSnapshot,
                ):
                    raise RuntimeError(
                        "Context Evidence observer returned an invalid checkpoint snapshot",
                    )
            except Exception as error:
                raise ProviderDispatchOutcomeAmbiguous() from error
            context_projection_snapshot = candidate
        try:
            return _attach_measurement(
                result,
                self._snapshot(
                    request,
                    actual,
                    allowed,
                    provider_input_tokens,
                    provider_input_bound_exceeded=bound_exceeded,
                ),
                context_projection_snapshot=context_projection_snapshot,
            )
        except Exception as error:
            if observer is not None and provider_call is not None:
                raise ProviderDispatchOutcomeAmbiguous() from error
            raise
