"""Auditable Lead provider-request measurement and a read-only final guard.

Automatic compaction owns state mutation. This compatibility facade re-exports
frozen profile contracts while retaining graph-state measurement and final
Provider dispatch observation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol, override, runtime_checkable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

from deerflow.agents.middlewares.dangling_tool_call_middleware import (
    project_dangling_tool_call_messages,
)
from deerflow.agents.middlewares.durable_context_middleware import (
    render_durable_context_messages,
)
from deerflow.agents.middlewares.input_sanitization_middleware import (
    project_input_sanitization_messages,
)
from deerflow.agents.middlewares.provider_request_cost_adapter import (
    PROVIDER_NON_ASCII_SAFETY_SUPPLEMENT_TOKENS_PER_BYTE,
    ProviderModelRequestCostAdapter,
    provider_visible_messages_payload,
)
from deerflow.agents.middlewares.provider_request_profile import (
    _MAX_CURRENT_UPLOAD_IMAGE_ALLOWANCE,
    _PROVIDER_VISUAL_MAX_TOKENS_PER_IMAGE,  # noqa: F401
    _SERIALIZATION_FRAMING_UTF8_BYTES,
    _VISION_CONTEXT_HEADER_UTF8_BYTES,
    _VISION_CONTEXT_PER_IMAGE_UTF8_BYTES,
    PROVIDER_REQUEST_ERROR_CONTRACT,
    PROVIDER_REQUEST_ESTIMATOR_REVISION,
    ContextCapacityExceeded,
    ProviderRequestComponent,
    ProviderRequestComponentSnapshot,  # noqa: F401
    ProviderRequestContextMeasurement,
    ProviderRequestMaterialMeasurement,
    ProviderRequestMeasurementSnapshot,
    ProviderRequestProfile,
    ProviderRequestProfileDrift,
    ProviderRequestProfileSnapshot,
    ProviderRequestUsageUnsupported,
    ProviderToolSchemaFact,
    _canonical_json,
    _canonicalize_tool_schema_facts,
    _component,
    _material_bytes,
    _non_ascii_utf8_bytes,
    _project_visual_blocks,
    _tool_schema_list_utf8_bytes,
    build_provider_request_profile,
    build_provider_request_profile_snapshot_from_facts,
    canonicalize_full_tools,
    collect_custom_middleware_request_contract,
    collect_middleware_system_prompts,
    collect_middleware_tools,
    contains_visual_material,
    declared_visual_max_tokens_per_image,
    provider_request_closure_identity,
    provider_request_runtime_policy_compatibility_identity,
    provider_request_runtime_policy_identity,
    provider_tool_schema_fact,
    resolve_provider_adapter,
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


def _state_ephemeral_material(state: Mapping[str, object]) -> tuple[BaseMessage, ...]:
    ledger = state.get("delegations")
    skills = state.get("skill_context")
    return render_durable_context_messages(
        state.get("summary_text") if isinstance(state.get("summary_text"), str) else None,
        list(ledger) if isinstance(ledger, list) else [],
        list(skills) if isinstance(skills, list) else [],
    )


def _todo_ephemeral_material(state: Mapping[str, object]) -> tuple[BaseMessage, ...]:
    todos = state.get("todos")
    if not isinstance(todos, list) or not todos:
        return ()
    # Lazy import avoids a module cycle: TodoMiddleware's state schema imports
    # ThreadState, while this module is also used by ThreadState serialization.
    from deerflow.agents.middlewares.todo_middleware import (
        render_todo_request_reserve,
    )

    return (
        HumanMessage(
            content=render_todo_request_reserve(todos),
            name="todo_request_reserve",
        ),
    )


def _slash_request_ephemeral_material(
    messages: Sequence[BaseMessage],
) -> tuple[BaseMessage, ...]:
    last_human = next(
        (message for message in reversed(messages) if isinstance(message, HumanMessage)),
        None,
    )
    if last_human is None or not isinstance(last_human.content, str):
        return ()
    if not last_human.content.lstrip().startswith("/"):
        return ()
    # Slash activation duplicates the remaining user request inside its hidden
    # wrapper. The largest installed Skill wrapper itself is frozen into the
    # profile's bounded overlay byte count at assembly.
    return (HumanMessage(content=last_human.content, name="slash_request_reserve"),)


def measure_profile_snapshot_context(
    snapshot: Mapping[str, object],
    state: Mapping[str, object] | None,
) -> ProviderRequestContextMeasurement:
    """Measure one checkpoint from the persisted immutable profile snapshot."""

    if snapshot.get("version") != 1 or snapshot.get("estimator_revision") != PROVIDER_REQUEST_ESTIMATOR_REVISION or snapshot.get("error_contract") != PROVIDER_REQUEST_ERROR_CONTRACT:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: profile revision is unavailable")
    if snapshot.get("supported") is not True:
        reason = snapshot.get("unsupported_reason")
        raise ProviderRequestUsageUnsupported(reason if isinstance(reason, str) else "provider request profile is unsupported")
    provider_adapter = snapshot.get("provider_adapter")
    if not isinstance(provider_adapter, str) or not provider_adapter:
        raise ProviderRequestUsageUnsupported(
            "provider_request_usage_unsupported: provider adapter is unavailable",
        )
    integer_fields = (
        "static_system_utf8_bytes",
        "full_tool_schema_utf8_bytes",
        "full_tool_count",
        "bounded_overlay_utf8_bytes",
        "bounded_overlay_message_count",
        "provider_fixed_overhead_tokens",
        "provider_per_message_overhead_tokens",
        "provider_per_tool_overhead_tokens",
    )
    if any(not isinstance(snapshot.get(field), int) or snapshot[field] < 0 for field in integer_fields):
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: profile components are invalid")
    raw_tool_facts = snapshot.get("full_tool_schema_facts")
    try:
        tool_facts = _canonicalize_tool_schema_facts(raw_tool_facts if isinstance(raw_tool_facts, (list, tuple)) else ())
    except ValueError:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: profile tool facts are invalid") from None
    if len(tool_facts) != snapshot["full_tool_count"] or _tool_schema_list_utf8_bytes(tool_facts) != snapshot["full_tool_schema_utf8_bytes"]:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: profile tool facts do not match components")
    allowance_ratio = snapshot.get("error_allowance_ratio")
    if not isinstance(allowance_ratio, (int, float)) or not 0 <= allowance_ratio <= 1:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: profile allowance is invalid")
    current = state or {}
    raw_messages = current.get("messages")
    messages = list(raw_messages) if isinstance(raw_messages, list) else []
    if any(not isinstance(message, BaseMessage) for message in messages):
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: checkpoint messages are invalid")
    raw_visual_declared = snapshot.get("visual_max_tokens_per_image")
    visual_declared = raw_visual_declared if isinstance(raw_visual_declared, int) and not isinstance(raw_visual_declared, bool) and raw_visual_declared > 0 else None
    if visual_declared is None and contains_visual_material(messages):
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: vision overhead is undeclared")
    viewed_images = current.get("viewed_images")
    viewed_image_count = len(viewed_images) if isinstance(viewed_images, Mapping) else 0
    if snapshot.get("supports_vision") is True and viewed_image_count and visual_declared is None:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: vision overhead is undeclared")

    durable_messages = _state_ephemeral_material(current)
    todo_messages = _todo_ephemeral_material(current)
    slash_messages = _slash_request_ephemeral_material(messages)
    # InputSanitization is a temporary outer request transform, so checkpoint
    # state retains the original HumanMessages. Project that exact transform
    # for profile capacity/drift accounting while leaving Slash Skill and other
    # state-derived overlays anchored to the original state above.
    provider_messages = project_dangling_tool_call_messages(
        project_input_sanitization_messages(messages),
    )
    provider_message_payloads = list(
        provider_visible_messages_payload(
            provider_messages,
            provider_adapter=provider_adapter,
        )
    )
    state_visual_count = 0
    if visual_declared is not None:
        provider_message_payloads, state_visual_count = _project_visual_blocks(
            provider_message_payloads,
        )
    # A vision-declared profile reserves the ephemeral image-context message
    # that ViewImageMiddleware injects: every retained viewed_images entry plus
    # the bounded current-upload allowance, each at the declared per-image cost,
    # plus the bounded label text around the visual blocks.
    vision_active = snapshot.get("supports_vision") is True and visual_declared is not None
    vision_image_allowance = (viewed_image_count + _MAX_CURRENT_UPLOAD_IMAGE_ALLOWANCE) if vision_active else 0
    vision_overhead_tokens = (vision_image_allowance + state_visual_count) * (visual_declared or 0)
    vision_context_bytes = (_VISION_CONTEXT_HEADER_UTF8_BYTES + vision_image_allowance * _VISION_CONTEXT_PER_IMAGE_UTF8_BYTES) if vision_active else 0
    vision_message_count = 1 if vision_active else 0
    non_ascii_supplement_per_byte = PROVIDER_NON_ASCII_SAFETY_SUPPLEMENT_TOKENS_PER_BYTE.get(provider_adapter, 0.0)
    compressible_material = _canonical_json(provider_message_payloads)
    compressible_bytes = len(compressible_material)
    fixed_bytes = int(snapshot["static_system_utf8_bytes"]) + int(snapshot["full_tool_schema_utf8_bytes"]) + _SERIALIZATION_FRAMING_UTF8_BYTES
    rendered_materials = [
        _material_bytes(
            message,
            provider_adapter=provider_adapter,
        )
        for message in (*durable_messages, *todo_messages, *slash_messages)
    ]
    ephemeral_bytes = int(snapshot["bounded_overlay_utf8_bytes"]) + vision_context_bytes + sum(len(material) for material in rendered_materials)
    # Declared overlay and vision label allowances have no retained text, so
    # they count fully toward the non-ASCII supplement (conservative).
    ephemeral_non_ascii_bytes = int(snapshot["bounded_overlay_utf8_bytes"]) + vision_context_bytes + sum(_non_ascii_utf8_bytes(material) for material in rendered_materials)
    compressible = _component(
        material_bytes=compressible_bytes,
        error_allowance_ratio=float(allowance_ratio),
        overhead_tokens=(len(provider_message_payloads) * int(snapshot["provider_per_message_overhead_tokens"])),
        non_ascii_bytes=_non_ascii_utf8_bytes(compressible_material),
        non_ascii_supplement_per_byte=non_ascii_supplement_per_byte,
    )
    fixed = _component(
        material_bytes=fixed_bytes,
        error_allowance_ratio=float(allowance_ratio),
        overhead_tokens=(int(snapshot["provider_fixed_overhead_tokens"]) + int(snapshot["full_tool_count"]) * int(snapshot["provider_per_tool_overhead_tokens"]) + int(snapshot["provider_per_message_overhead_tokens"])),
    )
    ephemeral = _component(
        material_bytes=ephemeral_bytes,
        error_allowance_ratio=float(allowance_ratio),
        overhead_tokens=(
            ((len(durable_messages) + len(todo_messages) + len(slash_messages) + vision_message_count + int(snapshot["bounded_overlay_message_count"])) * int(snapshot["provider_per_message_overhead_tokens"])) + vision_overhead_tokens
        ),
        non_ascii_bytes=ephemeral_non_ascii_bytes,
        non_ascii_supplement_per_byte=non_ascii_supplement_per_byte,
    )
    components = {
        "compressible": compressible,
        "fixed": fixed,
        "ephemeral": ephemeral,
    }
    return ProviderRequestContextMeasurement(
        estimated_tokens=sum(item.estimated_tokens for item in components.values()),
        error_allowance_tokens=sum(item.error_allowance_tokens for item in components.values()),
        safety_bound_tokens=sum(item.safety_bound_tokens for item in components.values()),
        material_utf8_bytes=compressible_bytes + fixed_bytes + ephemeral_bytes,
        message_count=(len(provider_message_payloads) + len(durable_messages) + len(todo_messages) + len(slash_messages) + vision_message_count + int(snapshot["bounded_overlay_message_count"]) + 1),
        full_tool_count=int(snapshot["full_tool_count"]),
        components=components,
    )


def measure_profile_context(
    profile: ProviderRequestProfile,
    state: Mapping[str, object] | None,
) -> ProviderRequestContextMeasurement:
    """Measure the auto-trigger input from one in-memory frozen profile."""

    return measure_profile_snapshot_context(profile.snapshot(), state)


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


__all__ = [
    "PROVIDER_REQUEST_ERROR_CONTRACT",
    "PROVIDER_REQUEST_ESTIMATOR_REVISION",
    "PROVIDER_REQUEST_MEASUREMENT_STATE_KEY",
    "PROVIDER_REQUEST_PROFILE_STATE_KEY",
    "ContextCapacityExceeded",
    "FinalProviderRequestGuard",
    "ProviderDispatchOutcomeAmbiguous",
    "ProviderNoResponseProvenError",
    "ProviderRequestEvidenceObserver",
    "ProviderRequestComponent",
    "ProviderRequestContextMeasurement",
    "ProviderRequestMaterialMeasurement",
    "ProviderRequestMeasurementSnapshot",
    "ProviderRequestProfile",
    "ProviderRequestProfileDrift",
    "ProviderRequestProfileSnapshot",
    "ProviderToolSchemaFact",
    "ProviderRequestUsageUnsupported",
    "build_provider_request_profile",
    "build_provider_request_profile_snapshot_from_facts",
    "canonicalize_full_tools",
    "collect_custom_middleware_request_contract",
    "collect_middleware_tools",
    "collect_middleware_system_prompts",
    "contains_visual_material",
    "declared_visual_max_tokens_per_image",
    "measure_profile_context",
    "measure_profile_snapshot_context",
    "provider_request_closure_identity",
    "provider_tool_schema_fact",
    "provider_request_runtime_policy_identity",
    "provider_request_runtime_policy_compatibility_identity",
    "resolve_provider_adapter",
]
