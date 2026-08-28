"""Auditable Lead provider-request estimates and a read-only final guard.

Automatic compaction owns state mutation.  This module only measures the
retained state against one immutable profile and verifies the final shaped
``ModelRequest`` before it reaches the provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NotRequired, Protocol, TypedDict, override, runtime_checkable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
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
from deerflow.agents.middlewares.manifest import MiddlewareHook, middleware_hooks
from deerflow.agents.middlewares.provider_request_cost_adapter import (
    PROVIDER_NON_ASCII_SAFETY_SUPPLEMENT_TOKENS_PER_BYTE,
    ProviderModelRequestCostAdapter,
    provider_visible_message_payload,
    provider_visible_messages_payload,
)
from deerflow.agents.provider_request_contract import (
    CONTEXT_COMPACTION_RECEIPT_STATE_KEY,
    CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
    provider_response_digest,
)
from deerflow.error_codes import (
    ContextProviderCallAmbiguousError,
    PublicRunError,
    PublicRunErrorCode,
)
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

PROVIDER_REQUEST_ESTIMATOR_REVISION = "provider-wire-engineering-v6"
PROVIDER_REQUEST_ERROR_CONTRACT = "versioned_engineering_allowance_for_app_owned_serialized_material_plus_declared_provider_overhead"
_SERIALIZATION_FRAMING_UTF8_BYTES = 1_024
_ESTIMATE_UTF8_BYTES_PER_TOKEN = 4

# These are platform declarations, not estimates learned from observed usage.
# UTF-8 bytes cover all app-owned material; these allowances cover provider
# framing that is not present in the app serialization. Unknown adapters fail
# closed instead of inheriting a zero-overhead assumption.
_PROVIDER_OVERHEAD: dict[str, tuple[int, int, int]] = {
    "anthropic": (256, 32, 96),
    "deepseek": (256, 32, 96),
    "openai": (256, 32, 96),
    "openai_responses": (256, 32, 96),
    "patched_deepseek": (256, 32, 96),
    "patched_openai": (256, 32, 96),
    "patched_openai_responses": (256, 32, 96),
    "vllm": (256, 32, 96),
}
_PROVIDER_ERROR_ALLOWANCE_RATIO: dict[str, float] = {
    "anthropic": 0.25,
    "deepseek": 0.20,
    "openai": 0.20,
    "openai_responses": 0.20,
    "patched_deepseek": 0.20,
    "patched_openai": 0.20,
    "patched_openai_responses": 0.20,
    "vllm": 0.25,
}
# Declared per-image Token upper bounds for adapters whose providers cap or
# downscale oversized images server-side (Anthropic ~1,590 Tokens at its
# 1568px cap; OpenAI high-detail tiling stays under 2,048 after its 2048/768
# resize). These are platform declarations in the same spirit as the byte
# allowances above. Adapters without a documented per-image cap (for example
# ``vllm``, whose cost depends on the served model) stay undeclared, and Lead
# vision material for them remains fail-closed.
_PROVIDER_VISUAL_MAX_TOKENS_PER_IMAGE: dict[str, int] = {
    "anthropic": 1_600,
    "openai": 2_048,
    "openai_responses": 2_048,
    "patched_openai": 2_048,
    "patched_openai_responses": 2_048,
}
# Mirrors ViewImageMiddleware._MAX_CURRENT_UPLOAD_IMAGES; a focused test keeps
# the two values synchronized without importing the middleware (and its PIL
# dependency chain) here.
_MAX_CURRENT_UPLOAD_IMAGE_ALLOWANCE = 4
# Declared byte allowances for the ephemeral image-context message text that
# ViewImageMiddleware injects around visual blocks (one header plus one label
# line per image). Visual bytes themselves are excluded from byte accounting
# and carried as declared per-image Tokens instead.
_VISION_CONTEXT_HEADER_UTF8_BYTES = 1_024
_VISION_CONTEXT_PER_IMAGE_UTF8_BYTES = 512
_VISUAL_BLOCK_TYPES = frozenset({"image", "image_url", "input_image"})
_PROVIDER_CLASS_TO_ADAPTER = {
    "langchain_anthropic:ChatAnthropic": "anthropic",
    "langchain_deepseek:ChatDeepSeek": "deepseek",
    "langchain_openai:ChatOpenAI": "openai",
    "deerflow.models.patched_deepseek:PatchedChatDeepSeek": "patched_deepseek",
    "deerflow.models.patched_openai:PatchedChatOpenAI": "patched_openai",
    "deerflow.models.vllm_provider:VllmChatModel": "vllm",
}


class ProviderRequestUsageUnsupported(PublicRunError):
    """Raised when the declared safety contract cannot cover a request."""

    def __init__(self, detail: str | None = None) -> None:
        self.internal_detail = detail
        super().__init__(PublicRunErrorCode.PROVIDER_REQUEST_USAGE_UNSUPPORTED)


class ProviderRequestProfileDrift(PublicRunError):
    """Raised when final app-owned material exceeds the frozen profile."""

    def __init__(self, detail: str | None = None) -> None:
        self.internal_detail = detail
        super().__init__(PublicRunErrorCode.PROVIDER_REQUEST_PROFILE_DRIFT)


class ContextCapacityExceeded(PublicRunError):
    """Raised before Provider invocation when final Context exceeds capacity."""

    def __init__(self, detail: str | None = None) -> None:
        self.internal_detail = detail
        super().__init__(PublicRunErrorCode.CONTEXT_CAPACITY_EXCEEDED)


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


class ProviderRequestComponentSnapshot(TypedDict):
    estimated_tokens: int
    error_allowance_tokens: int
    safety_bound_tokens: int


class ProviderToolSchemaFact(TypedDict):
    """Secret-free identity and size of one provider-facing tool schema."""

    name: str
    schema_utf8_bytes: int
    schema_sha256: str


class ProviderRequestProfileSnapshot(TypedDict):
    version: int
    estimator_revision: str
    error_contract: str
    model_name: str
    provider_adapter: str | None
    profile_fingerprint: str
    authority_identity: str | None
    capture_provider_input_tokens: bool
    closure_identity: str | None
    mcp_closure_present: bool
    runtime_policy_identity: str | None
    workload_profile: str | None
    supported: bool
    unsupported_reason: str | None
    supports_vision: bool
    visual_max_tokens_per_image: NotRequired[int | None]
    max_input_tokens: int | None
    static_system_utf8_bytes: int
    full_tool_schema_utf8_bytes: int
    full_tool_count: int
    full_tool_schema_facts: tuple[ProviderToolSchemaFact, ...]
    bounded_overlay_utf8_bytes: int
    bounded_overlay_message_count: int
    provider_fixed_overhead_tokens: int
    provider_per_message_overhead_tokens: int
    provider_per_tool_overhead_tokens: int
    error_allowance_ratio: float


class ProviderRequestMeasurementSnapshot(TypedDict):
    version: int
    estimator_revision: str
    error_contract: str
    model_name: str
    profile_fingerprint: str
    request_fingerprint: str
    estimated_tokens: int
    error_allowance_tokens: int
    safety_bound_tokens: int
    allowed_safety_bound_tokens: int
    message_count: int
    full_tool_count: int
    components: dict[str, ProviderRequestComponentSnapshot]
    provider_input_tokens: int | None
    provider_input_tokens_exceeded_bound: NotRequired[bool]
    authority_identity: str | None
    run_id: NotRequired[str]


@dataclass(frozen=True, slots=True)
class ProviderRequestComponent:
    estimated_tokens: int
    error_allowance_tokens: int
    safety_bound_tokens: int

    def snapshot(self) -> ProviderRequestComponentSnapshot:
        return {
            "estimated_tokens": self.estimated_tokens,
            "error_allowance_tokens": self.error_allowance_tokens,
            "safety_bound_tokens": self.safety_bound_tokens,
        }


@dataclass(frozen=True, slots=True)
class ProviderRequestContextMeasurement:
    estimated_tokens: int
    error_allowance_tokens: int
    safety_bound_tokens: int
    material_utf8_bytes: int
    message_count: int
    full_tool_count: int
    components: dict[str, ProviderRequestComponent]


@dataclass(frozen=True, slots=True)
class ProviderRequestMaterialMeasurement:
    estimated_tokens: int
    error_allowance_tokens: int
    safety_bound_tokens: int
    material_utf8_bytes: int
    message_count: int
    tool_count: int
    request_fingerprint: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _message_payload(
    message: BaseMessage,
    *,
    provider_adapter: str,
) -> dict[str, object]:
    return provider_visible_message_payload(
        message,
        provider_adapter=provider_adapter,
    )


def _material_bytes(
    value: object,
    *,
    provider_adapter: str,
) -> bytes:
    if isinstance(value, BaseMessage):
        return _canonical_json(
            _message_payload(
                value,
                provider_adapter=provider_adapter,
            )
        )
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return _canonical_json(value)


def _tool_payload(tool: BaseTool | dict[str, Any]) -> dict[str, Any]:
    converted = convert_to_openai_tool(tool)
    if not isinstance(converted, dict):
        raise TypeError("provider tool conversion returned a non-mapping")
    return converted


def _tool_name(tool: BaseTool | dict[str, Any]) -> str:
    if isinstance(tool, BaseTool):
        return tool.name
    function = tool.get("function")
    if isinstance(function, Mapping) and isinstance(function.get("name"), str):
        return function["name"]
    name = tool.get("name")
    if isinstance(name, str):
        return name
    raise ValueError("provider tool has no stable name")


def provider_tool_schema_fact(
    tool: BaseTool | dict[str, Any],
) -> ProviderToolSchemaFact:
    """Project one exact provider schema without retaining its plaintext."""

    payload = _canonical_json(_tool_payload(tool))
    return {
        "name": _tool_name(tool),
        "schema_utf8_bytes": len(payload),
        "schema_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _canonicalize_tool_schema_facts(
    facts: Sequence[Mapping[str, object]],
) -> tuple[ProviderToolSchemaFact, ...]:
    by_name: dict[str, ProviderToolSchemaFact] = {}
    for raw in facts:
        name = raw.get("name")
        schema_utf8_bytes = raw.get("schema_utf8_bytes")
        schema_sha256 = raw.get("schema_sha256")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(schema_utf8_bytes, int)
            or isinstance(schema_utf8_bytes, bool)
            or schema_utf8_bytes <= 0
            or not isinstance(schema_sha256, str)
            or len(schema_sha256) != 64
            or any(character not in "0123456789abcdef" for character in schema_sha256)
        ):
            raise ValueError("provider tool schema fact is invalid")
        by_name[name] = {
            "name": name,
            "schema_utf8_bytes": schema_utf8_bytes,
            "schema_sha256": schema_sha256,
        }
    return tuple(by_name.values())


def _tool_schema_list_utf8_bytes(
    facts: Sequence[ProviderToolSchemaFact],
) -> int:
    # Canonical JSON list framing is two brackets plus one comma between each
    # already-canonical object payload.
    return 2 + sum(item["schema_utf8_bytes"] for item in facts) + max(0, len(facts) - 1)


def canonicalize_full_tools(
    tools: Sequence[BaseTool | dict[str, Any]],
) -> tuple[BaseTool | dict[str, Any], ...]:
    """Apply LangChain's name-deduped effective tool-set semantics."""

    by_name: dict[str, BaseTool | dict[str, Any]] = {}
    for tool in tools:
        by_name[_tool_name(tool)] = tool
    return tuple(by_name.values())


def collect_middleware_tools(
    middlewares: Sequence[AgentMiddleware],
) -> tuple[BaseTool | dict[str, Any], ...]:
    """Collect tools LangChain prepends from middleware state schemas."""

    collected: list[BaseTool | dict[str, Any]] = []
    for middleware in middlewares:
        tools = getattr(middleware, "tools", None)
        if isinstance(tools, (list, tuple)):
            collected.extend(tool for tool in tools if isinstance(tool, (BaseTool, dict)))
    return tuple(collected)


def collect_middleware_system_prompts(
    middlewares: Sequence[AgentMiddleware],
) -> tuple[str, ...]:
    """Collect static system material appended by LangChain middleware."""

    prompts: list[str] = []
    for middleware in middlewares:
        prompt = getattr(middleware, "system_prompt", None)
        if isinstance(prompt, str) and prompt:
            prompts.append(prompt)
    return tuple(prompts)


def collect_custom_middleware_request_contract(
    middlewares: Sequence[AgentMiddleware],
) -> tuple[tuple[object, ...], int, str | None]:
    """Collect explicit bounds for custom hooks that can shape a model request."""

    material: list[object] = []
    message_count = 0
    request_shaping_hooks = {
        MiddlewareHook.BEFORE_MODEL,
        MiddlewareHook.WRAP_MODEL_CALL,
    }
    for middleware in middlewares:
        if request_shaping_hooks.isdisjoint(middleware_hooks(middleware)):
            continue
        bounded_material = getattr(
            middleware,
            "provider_request_bounded_overlay_material",
            None,
        )
        bounded_message_count = getattr(
            middleware,
            "provider_request_bounded_overlay_message_count",
            None,
        )
        if type(bounded_material) is not tuple or type(bounded_message_count) is not int or bounded_message_count < 0:
            return (
                (),
                0,
                "provider_request_usage_unsupported: custom request shaper has no bounded contract",
            )
        material.extend(bounded_material)
        message_count += bounded_message_count
    return tuple(material), message_count, None


def contains_visual_material(messages: Sequence[BaseMessage]) -> bool:
    """Return whether Provider-visible messages contain a visual block."""

    for message in messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type", "")).lower()
            if block_type in _VISUAL_BLOCK_TYPES:
                return True
    return False


def _is_visual_payload_block(block: object) -> bool:
    return isinstance(block, Mapping) and str(block.get("type", "")).lower() in _VISUAL_BLOCK_TYPES


def _project_visual_blocks(
    payloads: Sequence[Mapping[str, object]],
) -> tuple[list[Mapping[str, object]], int]:
    """Split visual blocks out of provider payloads for byte accounting.

    Visual bytes (base64 data URLs) must never be estimated as text; callers
    account the returned count against the declared per-image Token bound.
    """

    projected: list[Mapping[str, object]] = []
    visual_count = 0
    for payload in payloads:
        content = payload.get("content")
        if not isinstance(content, list):
            projected.append(payload)
            continue
        retained = [block for block in content if not _is_visual_payload_block(block)]
        removed = len(content) - len(retained)
        if removed == 0:
            projected.append(payload)
            continue
        visual_count += removed
        replacement = dict(payload)
        replacement["content"] = retained
        projected.append(replacement)
    return projected, visual_count


def _model_max_input_tokens(model: object) -> int | None:
    profile = getattr(model, "profile", None)
    value = profile.get("max_input_tokens") if isinstance(profile, Mapping) else getattr(profile, "max_input_tokens", None)
    return value if isinstance(value, int) and value > 0 else None


def _estimate_from_bytes(value: int) -> int:
    return math.ceil(value / _ESTIMATE_UTF8_BYTES_PER_TOKEN)


def _non_ascii_utf8_bytes(material: bytes) -> int:
    return sum(1 for byte in material if byte >= 0x80)


def _component(
    *,
    material_bytes: int,
    error_allowance_ratio: float,
    overhead_tokens: int = 0,
    non_ascii_bytes: int = 0,
    non_ascii_supplement_per_byte: float = 0.0,
) -> ProviderRequestComponent:
    estimate = _estimate_from_bytes(material_bytes)
    # This is a deliberately versioned engineering allowance, not a proof
    # about every provider tokenizer. Raw bytes remain separately auditable and
    # are used for profile-drift checks; they are not treated as token usage.
    # Non-ASCII material carries a declared per-byte supplement because bytes/4
    # is not an upper bound for CJK-inefficient tokenizers.
    allowance = math.ceil(estimate * error_allowance_ratio) + math.ceil(non_ascii_bytes * non_ascii_supplement_per_byte) + overhead_tokens
    return ProviderRequestComponent(
        estimated_tokens=estimate,
        error_allowance_tokens=allowance,
        safety_bound_tokens=estimate + allowance,
    )


@dataclass(frozen=True, slots=True)
class ProviderRequestProfile:
    model_name: str
    provider_adapter: str | None
    system_prompt: str
    middleware_system_prompts: tuple[str, ...]
    tools: tuple[BaseTool | dict[str, Any], ...]
    bounded_overlay_material: tuple[object, ...]
    bounded_overlay_message_count: int
    supports_vision: bool
    max_input_tokens: int | None
    supported: bool
    unsupported_reason: str | None
    provider_fixed_overhead_tokens: int
    provider_per_message_overhead_tokens: int
    provider_per_tool_overhead_tokens: int
    static_system_utf8_bytes: int
    full_tool_schema_utf8_bytes: int
    full_tool_schema_facts: tuple[ProviderToolSchemaFact, ...]
    bounded_overlay_utf8_bytes: int
    profile_fingerprint: str
    authority_identity: str | None
    capture_provider_input_tokens: bool
    closure_identity: str | None
    mcp_closure_present: bool
    runtime_policy_identity: str | None
    workload_profile: str | None
    error_allowance_ratio: float
    visual_max_tokens_per_image: int | None = None

    @property
    def full_tool_count(self) -> int:
        return len(self.tools)

    def _require_supported(self) -> None:
        if not self.supported:
            raise ProviderRequestUsageUnsupported(self.unsupported_reason or "provider request profile is unsupported")

    def snapshot(self) -> ProviderRequestProfileSnapshot:
        return {
            "version": 1,
            "estimator_revision": PROVIDER_REQUEST_ESTIMATOR_REVISION,
            "error_contract": PROVIDER_REQUEST_ERROR_CONTRACT,
            "model_name": self.model_name,
            "provider_adapter": self.provider_adapter,
            "profile_fingerprint": self.profile_fingerprint,
            "authority_identity": self.authority_identity,
            "capture_provider_input_tokens": self.capture_provider_input_tokens,
            "closure_identity": self.closure_identity,
            "mcp_closure_present": self.mcp_closure_present,
            "runtime_policy_identity": self.runtime_policy_identity,
            "workload_profile": self.workload_profile,
            "supported": self.supported,
            "unsupported_reason": self.unsupported_reason,
            "supports_vision": self.supports_vision,
            "visual_max_tokens_per_image": self.visual_max_tokens_per_image,
            "max_input_tokens": self.max_input_tokens,
            "static_system_utf8_bytes": self.static_system_utf8_bytes,
            "full_tool_schema_utf8_bytes": self.full_tool_schema_utf8_bytes,
            "full_tool_count": self.full_tool_count,
            "full_tool_schema_facts": self.full_tool_schema_facts,
            "bounded_overlay_utf8_bytes": self.bounded_overlay_utf8_bytes,
            "bounded_overlay_message_count": self.bounded_overlay_message_count,
            "provider_fixed_overhead_tokens": self.provider_fixed_overhead_tokens,
            "provider_per_message_overhead_tokens": self.provider_per_message_overhead_tokens,
            "provider_per_tool_overhead_tokens": self.provider_per_tool_overhead_tokens,
            "error_allowance_ratio": self.error_allowance_ratio,
        }

    def measure_request(self, request: ModelRequest) -> ProviderRequestMaterialMeasurement:
        self._require_supported()
        messages = list(request.messages)
        if request.system_message is not None:
            messages = [request.system_message, *messages]
        if self.visual_max_tokens_per_image is None and contains_visual_material(messages):
            raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: vision overhead is undeclared")
        tools = canonicalize_full_tools(tuple(request.tools or ()))
        actual_facts = tuple(provider_tool_schema_fact(tool) for tool in tools)
        frozen_facts = {fact["name"]: fact for fact in self.full_tool_schema_facts}
        if any(frozen_facts.get(fact["name"]) != fact for fact in actual_facts):
            raise ProviderRequestProfileDrift("provider_request_profile_drift: final tool schema is outside frozen profile")
        if self.provider_adapter is None:
            raise ProviderRequestUsageUnsupported(
                "provider_request_usage_unsupported: provider adapter is unavailable",
            )
        message_payloads = list(
            provider_visible_messages_payload(
                messages,
                provider_adapter=self.provider_adapter,
            )
        )
        visual_count = 0
        if self.visual_max_tokens_per_image is not None:
            message_payloads, visual_count = _project_visual_blocks(message_payloads)
        tool_payloads = [_tool_payload(tool) for tool in tools]
        material = _canonical_json({"messages": message_payloads, "tools": tool_payloads})
        # The non-ASCII supplement covers conversation material only: the
        # frozen profile cannot reconstruct system/tool text composition from
        # its snapshot, so both sides of the drift comparison exclude it.
        conversation_payloads = message_payloads[1:] if request.system_message is not None else message_payloads
        conversation_non_ascii = _non_ascii_utf8_bytes(_canonical_json(conversation_payloads))
        non_ascii_supplement = math.ceil(conversation_non_ascii * PROVIDER_NON_ASCII_SAFETY_SUPPLEMENT_TOKENS_PER_BYTE.get(self.provider_adapter, 0.0))
        overhead = self.provider_fixed_overhead_tokens + len(messages) * self.provider_per_message_overhead_tokens + len(tools) * self.provider_per_tool_overhead_tokens + visual_count * (self.visual_max_tokens_per_image or 0)
        estimate = _estimate_from_bytes(len(material))
        allowance = math.ceil(estimate * self.error_allowance_ratio) + non_ascii_supplement + overhead
        return ProviderRequestMaterialMeasurement(
            estimated_tokens=estimate,
            error_allowance_tokens=allowance,
            safety_bound_tokens=estimate + allowance,
            material_utf8_bytes=len(material),
            message_count=len(message_payloads),
            tool_count=len(tools),
            request_fingerprint=hashlib.sha256(material).hexdigest(),
        )


def resolve_provider_adapter(
    provider_adapter: str | None,
    provider_class_path: str | None = None,
    *,
    model: object | None = None,
) -> str | None:
    resolved = provider_adapter if provider_adapter in _PROVIDER_OVERHEAD else _PROVIDER_CLASS_TO_ADAPTER.get(provider_class_path or "")
    if getattr(model, "use_responses_api", None) is True and resolved in {"openai", "patched_openai"}:
        return f"{resolved}_responses"
    return resolved


def declared_visual_max_tokens_per_image(
    provider_adapter: str | None,
    provider_class_path: str | None = None,
    *,
    model: object | None = None,
) -> int | None:
    """Return the declared per-image Token upper bound for one adapter.

    ``None`` means the adapter has no declared visual cost; Lead vision
    injection and the ``view_image`` tool must stay disarmed for it because
    the final provider guard fails closed on unmeasured visual material.
    """

    resolved = resolve_provider_adapter(
        provider_adapter,
        provider_class_path,
        model=model,
    )
    return _PROVIDER_VISUAL_MAX_TOKENS_PER_IMAGE.get(resolved or "")


def provider_request_closure_identity(
    *,
    agent_facts: Sequence[tuple[str, str]],
    catalog_generation: int,
) -> str:
    """Hash the Lead identity under one resolver-visible catalog generation."""

    material = _canonical_json(
        {
            "agents": tuple(agent_facts),
            "catalog_generation": catalog_generation,
        }
    )
    return hashlib.sha256(material).hexdigest()


def _provider_request_runtime_policy_material(
    app_config: object,
) -> dict[str, object]:
    if not hasattr(app_config, "model_dump"):
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: runtime policy identity is unavailable")
    value = app_config.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: runtime policy identity is unavailable")
    sections = (
        "input_polish",
        "loop_detection",
        "max_recursion_limit",
        "memory",
        "read_before_write",
        "safety_finish_reason",
        "subagents",
        "suggestions",
        "summarization",
        "title",
        "token_budget",
        "token_usage",
        "tool_output",
        "tool_search",
        "vision_bridge",
    )
    return {name: value.get(name) for name in sections}


def provider_request_runtime_policy_identity(app_config: object) -> str:
    """Fingerprint the secret-free config sections that can shape a request."""

    return hashlib.sha256(_canonical_json(_provider_request_runtime_policy_material(app_config))).hexdigest()


def provider_request_runtime_policy_compatibility_identity(
    app_config: object,
) -> str:
    """Fingerprint request policy while allowing Gauge-safe changes."""

    material = _provider_request_runtime_policy_material(app_config)
    # This limits Graph execution depth; it does not reshape the retained
    # provider request measured by an idle Context Gauge.
    material.pop("max_recursion_limit", None)
    summarization = material.get("summarization")
    if isinstance(summarization, Mapping):
        compatible_summarization = dict(summarization)
        compatible_summarization.pop("trigger", None)
        material["summarization"] = compatible_summarization
    return hashlib.sha256(_canonical_json(material)).hexdigest()


def build_provider_request_profile(
    *,
    model: object,
    model_name: str,
    provider_adapter: str | None,
    system_prompt: str,
    tools: Sequence[BaseTool | dict[str, Any]],
    middleware_system_prompts: Sequence[str] = (),
    bounded_overlay_material: Sequence[object] = (),
    bounded_overlay_utf8_bytes: int = 0,
    bounded_overlay_message_count: int = 1,
    supports_vision: bool = False,
    provider_class_path: str | None = None,
    unsupported_reason: str | None = None,
    authority_identity: str | None = None,
    capture_provider_input_tokens: bool = True,
    closure_identity: str | None = None,
    mcp_closure_present: bool = False,
    runtime_policy_identity: str | None = None,
    workload_profile: str | None = None,
) -> ProviderRequestProfile:
    """Build one immutable profile after canonical Lead tools are assembled."""

    reason = unsupported_reason
    try:
        effective_tools = canonicalize_full_tools(tuple(tools))
        tool_facts = tuple(provider_tool_schema_fact(tool) for tool in effective_tools)
    except (TypeError, ValueError, json.JSONDecodeError):
        effective_tools = ()
        tool_facts = ()
        reason = "provider_request_usage_unsupported: tool schema is not serializable"
    snapshot = build_provider_request_profile_snapshot_from_facts(
        model=model,
        model_name=model_name,
        provider_adapter=provider_adapter,
        provider_class_path=provider_class_path,
        system_prompt=system_prompt,
        tool_schema_facts=tool_facts,
        middleware_system_prompts=middleware_system_prompts,
        bounded_overlay_material=bounded_overlay_material,
        bounded_overlay_utf8_bytes=bounded_overlay_utf8_bytes,
        bounded_overlay_message_count=bounded_overlay_message_count,
        supports_vision=supports_vision,
        unsupported_reason=reason,
        authority_identity=authority_identity,
        capture_provider_input_tokens=capture_provider_input_tokens,
        closure_identity=closure_identity,
        mcp_closure_present=mcp_closure_present,
        runtime_policy_identity=runtime_policy_identity,
        workload_profile=workload_profile,
    )
    effective_middleware_prompts = tuple(prompt for prompt in middleware_system_prompts if isinstance(prompt, str) and prompt)
    return ProviderRequestProfile(
        model_name=model_name,
        provider_adapter=snapshot["provider_adapter"],
        system_prompt=system_prompt,
        middleware_system_prompts=effective_middleware_prompts,
        tools=effective_tools,
        bounded_overlay_material=tuple(bounded_overlay_material),
        bounded_overlay_message_count=max(0, bounded_overlay_message_count),
        supports_vision=supports_vision,
        max_input_tokens=snapshot["max_input_tokens"],
        supported=snapshot["supported"],
        unsupported_reason=snapshot["unsupported_reason"],
        provider_fixed_overhead_tokens=snapshot["provider_fixed_overhead_tokens"],
        provider_per_message_overhead_tokens=snapshot["provider_per_message_overhead_tokens"],
        provider_per_tool_overhead_tokens=snapshot["provider_per_tool_overhead_tokens"],
        static_system_utf8_bytes=snapshot["static_system_utf8_bytes"],
        full_tool_schema_utf8_bytes=snapshot["full_tool_schema_utf8_bytes"],
        full_tool_schema_facts=snapshot["full_tool_schema_facts"],
        bounded_overlay_utf8_bytes=snapshot["bounded_overlay_utf8_bytes"],
        profile_fingerprint=snapshot["profile_fingerprint"],
        authority_identity=authority_identity,
        capture_provider_input_tokens=capture_provider_input_tokens,
        closure_identity=closure_identity,
        mcp_closure_present=mcp_closure_present,
        runtime_policy_identity=runtime_policy_identity,
        workload_profile=workload_profile,
        error_allowance_ratio=snapshot["error_allowance_ratio"],
        visual_max_tokens_per_image=snapshot.get("visual_max_tokens_per_image"),
    )


def build_provider_request_profile_snapshot_from_facts(
    *,
    model: object,
    model_name: str,
    provider_adapter: str | None,
    system_prompt: str,
    tool_schema_facts: Sequence[Mapping[str, object]],
    middleware_system_prompts: Sequence[str] = (),
    bounded_overlay_material: Sequence[object] = (),
    bounded_overlay_utf8_bytes: int = 0,
    bounded_overlay_message_count: int = 1,
    supports_vision: bool = False,
    provider_class_path: str | None = None,
    unsupported_reason: str | None = None,
    authority_identity: str | None = None,
    capture_provider_input_tokens: bool = True,
    closure_identity: str | None = None,
    mcp_closure_present: bool = False,
    runtime_policy_identity: str | None = None,
    workload_profile: str | None = None,
) -> ProviderRequestProfileSnapshot:
    """Build the same immutable profile from secret-free schema facts."""

    resolved_adapter = resolve_provider_adapter(
        provider_adapter,
        provider_class_path,
        model=model,
    )
    reason = unsupported_reason
    if resolved_adapter is None and reason is None:
        reason = "provider_request_usage_unsupported: provider overhead is undeclared"
    if type(capture_provider_input_tokens) is not bool:
        reason = "provider_request_usage_unsupported: token usage capture contract is invalid"
    if closure_identity is not None and (not isinstance(closure_identity, str) or not closure_identity):
        reason = "provider_request_usage_unsupported: closure identity is invalid"
    if type(mcp_closure_present) is not bool:
        reason = "provider_request_usage_unsupported: MCP closure contract is invalid"
    if runtime_policy_identity is not None and (not isinstance(runtime_policy_identity, str) or not runtime_policy_identity):
        reason = "provider_request_usage_unsupported: runtime policy identity is invalid"
    if workload_profile is not None and workload_profile not in {"interactive", "research"}:
        reason = "provider_request_usage_unsupported: workload profile is invalid"
    overhead = _PROVIDER_OVERHEAD.get(resolved_adapter or "", (0, 0, 0))
    allowance_ratio = _PROVIDER_ERROR_ALLOWANCE_RATIO.get(resolved_adapter or "", 0.0)
    visual_max_tokens = _PROVIDER_VISUAL_MAX_TOKENS_PER_IMAGE.get(resolved_adapter or "") if supports_vision else None
    try:
        effective_facts = _canonicalize_tool_schema_facts(tool_schema_facts)
        effective_middleware_prompts = tuple(prompt for prompt in middleware_system_prompts if isinstance(prompt, str) and prompt)
        if resolved_adapter is None:
            raise ValueError("provider adapter is unavailable")
        system_bytes = sum(
            len(
                _material_bytes(
                    SystemMessage(content=prompt),
                    provider_adapter=resolved_adapter,
                )
            )
            for prompt in (system_prompt, *effective_middleware_prompts)
        )
        tool_bytes = _tool_schema_list_utf8_bytes(effective_facts)
        if (
            not isinstance(bounded_overlay_utf8_bytes, int)
            or isinstance(bounded_overlay_utf8_bytes, bool)
            or bounded_overlay_utf8_bytes < 0
            or not isinstance(bounded_overlay_message_count, int)
            or isinstance(bounded_overlay_message_count, bool)
            or bounded_overlay_message_count < 0
        ):
            raise ValueError("bounded overlay contract is invalid")
        overlay_bytes = bounded_overlay_utf8_bytes + sum(
            len(
                _material_bytes(
                    item,
                    provider_adapter=resolved_adapter,
                )
            )
            for item in bounded_overlay_material
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        effective_facts = ()
        effective_middleware_prompts = ()
        system_bytes = len(system_prompt.encode("utf-8"))
        tool_bytes = 2
        overlay_bytes = 0
        reason = "provider_request_usage_unsupported: profile material is invalid"
    fingerprint_material = _canonical_json(
        {
            "revision": PROVIDER_REQUEST_ESTIMATOR_REVISION,
            "model_name": model_name,
            "provider_adapter": resolved_adapter,
            "system_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
            "middleware_system_sha256": [hashlib.sha256(prompt.encode("utf-8")).hexdigest() for prompt in effective_middleware_prompts],
            "system_bytes": system_bytes,
            "tool_facts": effective_facts,
            "tool_bytes": tool_bytes,
            "overlay_bytes": overlay_bytes,
            "overlay_messages": bounded_overlay_message_count,
            "overhead": overhead,
            "supports_vision": supports_vision,
            "visual_max_tokens_per_image": visual_max_tokens,
            "authority_identity": authority_identity,
            "capture_provider_input_tokens": capture_provider_input_tokens,
            "closure_identity": closure_identity,
            "mcp_closure_present": mcp_closure_present,
            "runtime_policy_identity": runtime_policy_identity,
            "workload_profile": workload_profile,
            "allowance_ratio": allowance_ratio,
        }
    )
    return {
        "version": 1,
        "estimator_revision": PROVIDER_REQUEST_ESTIMATOR_REVISION,
        "error_contract": PROVIDER_REQUEST_ERROR_CONTRACT,
        "model_name": model_name,
        "provider_adapter": resolved_adapter,
        "profile_fingerprint": hashlib.sha256(fingerprint_material).hexdigest(),
        "authority_identity": authority_identity,
        "capture_provider_input_tokens": capture_provider_input_tokens,
        "closure_identity": closure_identity,
        "mcp_closure_present": mcp_closure_present,
        "runtime_policy_identity": runtime_policy_identity,
        "workload_profile": workload_profile,
        "supported": reason is None,
        "unsupported_reason": reason,
        "supports_vision": supports_vision,
        "visual_max_tokens_per_image": visual_max_tokens,
        "max_input_tokens": _model_max_input_tokens(model),
        "static_system_utf8_bytes": system_bytes,
        "full_tool_schema_utf8_bytes": tool_bytes,
        "full_tool_count": len(effective_facts),
        "full_tool_schema_facts": effective_facts,
        "bounded_overlay_utf8_bytes": overlay_bytes,
        "bounded_overlay_message_count": max(0, bounded_overlay_message_count),
        "provider_fixed_overhead_tokens": overhead[0],
        "provider_per_message_overhead_tokens": overhead[1],
        "provider_per_tool_overhead_tokens": overhead[2],
        "error_allowance_ratio": allowance_ratio,
    }


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
