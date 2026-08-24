"""Auditable Lead provider-request estimates and a read-only final guard.

Automatic compaction owns state mutation.  This module only measures the
retained state against one immutable profile and verifies the final shaped
``ModelRequest`` before it reaches the provider.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NotRequired, TypedDict, override

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
    message_to_dict,
)
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.runtime import Runtime
from langgraph.types import Command

from deerflow.agents.middlewares.durable_context_middleware import (
    render_durable_context_messages,
)
from deerflow.agents.middlewares.manifest import MiddlewareHook, middleware_hooks
from deerflow.agents.provider_request_contract import (
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
)
from deerflow.error_codes import PublicRunError, PublicRunErrorCode
from deerflow.runtime.context_keys import RuntimeContextKeys

PROVIDER_REQUEST_ESTIMATOR_REVISION = "provider-request-engineering-v1"
PROVIDER_REQUEST_ERROR_CONTRACT = "versioned_engineering_allowance_for_app_owned_serialized_material_plus_declared_provider_overhead"
_SERIALIZATION_FRAMING_UTF8_BYTES = 1_024

# These are platform declarations, not estimates learned from observed usage.
# UTF-8 bytes cover all app-owned material; these allowances cover provider
# framing that is not present in the app serialization. Unknown adapters fail
# closed instead of inheriting a zero-overhead assumption.
_PROVIDER_OVERHEAD: dict[str, tuple[int, int, int]] = {
    "anthropic": (256, 32, 96),
    "deepseek": (256, 32, 96),
    "openai": (256, 32, 96),
    "patched_deepseek": (256, 32, 96),
    "patched_openai": (256, 32, 96),
    "vllm": (256, 32, 96),
}
_PROVIDER_ERROR_ALLOWANCE_RATIO: dict[str, float] = {
    "anthropic": 0.25,
    "deepseek": 0.20,
    "openai": 0.20,
    "patched_deepseek": 0.20,
    "patched_openai": 0.20,
    "vllm": 0.25,
}
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


class ProviderRequestCapacityExceeded(PublicRunError):
    """Raised before provider invocation when the safety value exceeds capacity."""

    def __init__(self, detail: str | None = None) -> None:
        self.internal_detail = detail
        super().__init__(PublicRunErrorCode.PROVIDER_REQUEST_CAPACITY_EXCEEDED)


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


def _message_payload(message: BaseMessage) -> dict[str, object]:
    return message_to_dict(message)


def _material_bytes(value: object) -> bytes:
    if isinstance(value, BaseMessage):
        return _canonical_json(_message_payload(value))
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


def _contains_visual_material(messages: Sequence[BaseMessage]) -> bool:
    for message in messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type", "")).lower()
            if block_type in {"image", "image_url", "input_image"}:
                return True
    return False


def _model_max_input_tokens(model: object) -> int | None:
    profile = getattr(model, "profile", None)
    value = profile.get("max_input_tokens") if isinstance(profile, Mapping) else getattr(profile, "max_input_tokens", None)
    return value if isinstance(value, int) and value > 0 else None


def _estimate_from_bytes(value: int) -> int:
    return math.ceil(value / 4)


def _component(
    *,
    material_bytes: int,
    error_allowance_ratio: float,
    overhead_tokens: int = 0,
) -> ProviderRequestComponent:
    estimate = _estimate_from_bytes(material_bytes)
    # This is a deliberately versioned engineering allowance, not a proof
    # about every provider tokenizer. Raw bytes remain separately auditable and
    # are used for profile-drift checks; they are not treated as token usage.
    allowance = math.ceil(estimate * error_allowance_ratio) + overhead_tokens
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
        if _contains_visual_material(messages):
            raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: vision overhead is undeclared")
        tools = canonicalize_full_tools(tuple(request.tools or ()))
        actual_facts = tuple(provider_tool_schema_fact(tool) for tool in tools)
        frozen_facts = {fact["name"]: fact for fact in self.full_tool_schema_facts}
        if any(frozen_facts.get(fact["name"]) != fact for fact in actual_facts):
            raise ProviderRequestProfileDrift("provider_request_profile_drift: final tool schema is outside frozen profile")
        message_payloads = [_message_payload(message) for message in messages]
        tool_payloads = [_tool_payload(tool) for tool in tools]
        material = _canonical_json({"messages": message_payloads, "tools": tool_payloads})
        overhead = self.provider_fixed_overhead_tokens + len(messages) * self.provider_per_message_overhead_tokens + len(tools) * self.provider_per_tool_overhead_tokens
        estimate = _estimate_from_bytes(len(material))
        allowance = math.ceil(estimate * self.error_allowance_ratio) + overhead
        return ProviderRequestMaterialMeasurement(
            estimated_tokens=estimate,
            error_allowance_tokens=allowance,
            safety_bound_tokens=estimate + allowance,
            material_utf8_bytes=len(material),
            message_count=len(messages),
            tool_count=len(tools),
            request_fingerprint=hashlib.sha256(material).hexdigest(),
        )


def resolve_provider_adapter(
    provider_adapter: str | None,
    provider_class_path: str | None = None,
) -> str | None:
    if provider_adapter in _PROVIDER_OVERHEAD:
        return provider_adapter
    return _PROVIDER_CLASS_TO_ADAPTER.get(provider_class_path or "")


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
    """Fingerprint request policy while allowing Gauge trigger changes."""

    material = _provider_request_runtime_policy_material(app_config)
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

    resolved_adapter = resolve_provider_adapter(provider_adapter, provider_class_path)
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
    try:
        effective_facts = _canonicalize_tool_schema_facts(tool_schema_facts)
        effective_middleware_prompts = tuple(prompt for prompt in middleware_system_prompts if isinstance(prompt, str) and prompt)
        system_bytes = sum(len(_material_bytes(SystemMessage(content=prompt))) for prompt in (system_prompt, *effective_middleware_prompts))
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
        overlay_bytes = bounded_overlay_utf8_bytes + sum(len(_material_bytes(item)) for item in bounded_overlay_material)
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
    if _contains_visual_material(messages):
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: vision overhead is undeclared")
    viewed_images = current.get("viewed_images")
    if snapshot.get("supports_vision") is True and isinstance(viewed_images, Mapping) and viewed_images:
        raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: vision overhead is undeclared")

    durable_messages = _state_ephemeral_material(current)
    todo_messages = _todo_ephemeral_material(current)
    slash_messages = _slash_request_ephemeral_material(messages)
    compressible_bytes = len(_canonical_json([_message_payload(message) for message in messages]))
    fixed_bytes = int(snapshot["static_system_utf8_bytes"]) + int(snapshot["full_tool_schema_utf8_bytes"]) + _SERIALIZATION_FRAMING_UTF8_BYTES
    ephemeral_bytes = (
        int(snapshot["bounded_overlay_utf8_bytes"])
        + sum(len(_material_bytes(message)) for message in durable_messages)
        + sum(len(_material_bytes(message)) for message in todo_messages)
        + sum(len(_material_bytes(message)) for message in slash_messages)
    )
    compressible = _component(
        material_bytes=compressible_bytes,
        error_allowance_ratio=float(allowance_ratio),
        overhead_tokens=(len(messages) * int(snapshot["provider_per_message_overhead_tokens"])),
    )
    fixed = _component(
        material_bytes=fixed_bytes,
        error_allowance_ratio=float(allowance_ratio),
        overhead_tokens=(int(snapshot["provider_fixed_overhead_tokens"]) + int(snapshot["full_tool_count"]) * int(snapshot["provider_per_tool_overhead_tokens"]) + int(snapshot["provider_per_message_overhead_tokens"])),
    )
    ephemeral = _component(
        material_bytes=ephemeral_bytes,
        error_allowance_ratio=float(allowance_ratio),
        overhead_tokens=((len(durable_messages) + len(todo_messages) + len(slash_messages) + int(snapshot["bounded_overlay_message_count"])) * int(snapshot["provider_per_message_overhead_tokens"])),
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
        message_count=(len(messages) + len(durable_messages) + len(todo_messages) + len(slash_messages) + int(snapshot["bounded_overlay_message_count"]) + 1),
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
) -> ExtendedModelResponse:
    response = _model_response(result)
    update = {PROVIDER_REQUEST_MEASUREMENT_STATE_KEY: measurement}
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

    def __init__(self, profile: ProviderRequestProfile) -> None:
        self.profile = profile

    @override
    def before_agent(self, state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
        return {PROVIDER_REQUEST_PROFILE_STATE_KEY: self.profile.snapshot()}

    @override
    async def abefore_agent(self, state: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
        return self.before_agent(state, runtime)

    def _measure_and_validate(
        self,
        request: ModelRequest,
    ) -> tuple[ProviderRequestMaterialMeasurement, ProviderRequestContextMeasurement]:
        stored = (request.state or {}).get(PROVIDER_REQUEST_PROFILE_STATE_KEY)
        if isinstance(stored, Mapping) and stored.get("profile_fingerprint") != self.profile.profile_fingerprint:
            raise ProviderRequestProfileDrift("provider_request_profile_drift: checkpoint profile changed")
        runtime_run_id = _runtime_run_id(request.runtime)
        if self.profile.authority_identity is not None and runtime_run_id != self.profile.authority_identity:
            raise ProviderRequestProfileDrift("provider_request_profile_drift: Run authority changed")
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
        if capacity is not None and actual.safety_bound_tokens > capacity:
            raise ProviderRequestCapacityExceeded("provider_request_capacity_exceeded: final request safety value exceeds model capacity")
        return actual, allowed

    @staticmethod
    def _validated_provider_input_tokens(
        response: ModelResponse,
        actual: ProviderRequestMaterialMeasurement,
    ) -> int | None:
        provider_input_tokens = _provider_input_tokens(response)
        if provider_input_tokens is not None and provider_input_tokens > actual.safety_bound_tokens:
            raise ProviderRequestUsageUnsupported("provider_request_usage_unsupported: provider input tokens exceeded current engineering safety bound")
        return provider_input_tokens

    def _snapshot(
        self,
        request: ModelRequest,
        actual: ProviderRequestMaterialMeasurement,
        allowed: ProviderRequestContextMeasurement,
        provider_input_tokens: int | None,
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
        actual, allowed = self._measure_and_validate(request)
        result = handler(request)
        response = _model_response(result)
        provider_input_tokens = self._validated_provider_input_tokens(response, actual)
        return _attach_measurement(
            result,
            self._snapshot(request, actual, allowed, provider_input_tokens),
        )

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelCallResult]],
    ) -> ModelCallResult:
        actual, allowed = self._measure_and_validate(request)
        result = await handler(request)
        response = _model_response(result)
        provider_input_tokens = self._validated_provider_input_tokens(response, actual)
        return _attach_measurement(
            result,
            self._snapshot(request, actual, allowed, provider_input_tokens),
        )


__all__ = [
    "PROVIDER_REQUEST_ERROR_CONTRACT",
    "PROVIDER_REQUEST_ESTIMATOR_REVISION",
    "PROVIDER_REQUEST_MEASUREMENT_STATE_KEY",
    "PROVIDER_REQUEST_PROFILE_STATE_KEY",
    "FinalProviderRequestGuard",
    "ProviderRequestCapacityExceeded",
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
    "measure_profile_context",
    "measure_profile_snapshot_context",
    "provider_request_closure_identity",
    "provider_tool_schema_fact",
    "provider_request_runtime_policy_identity",
    "provider_request_runtime_policy_compatibility_identity",
    "resolve_provider_adapter",
]
