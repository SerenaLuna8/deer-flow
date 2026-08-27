from __future__ import annotations

from copy import deepcopy

import pytest
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.runtime import Runtime
from langgraph.types import Command

from deerflow.agents.middlewares.provider_request_usage import (
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    PROVIDER_REQUEST_PROFILE_STATE_KEY,
    FinalProviderRequestGuard,
    ProviderRequestCapacityExceeded,
    ProviderRequestProfileDrift,
    ProviderRequestUsageUnsupported,
    build_provider_request_profile,
    build_provider_request_profile_snapshot_from_facts,
    collect_middleware_system_prompts,
    collect_middleware_tools,
    measure_profile_context,
    provider_request_runtime_policy_compatibility_identity,
    provider_request_runtime_policy_identity,
    provider_tool_schema_fact,
)
from deerflow.runtime.context_keys import RuntimeContextKeys


class _ToolBindingFakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _model(*, max_input_tokens: int = 100_000) -> _ToolBindingFakeModel:
    return _ToolBindingFakeModel(
        messages=iter([AIMessage(content="unused")]),
        profile={"max_input_tokens": max_input_tokens},
    )


@tool
def default_tool(query: str) -> str:
    """Search the default catalog."""

    return query


@tool
def deferred_tool(query: str, limit: int = 10) -> str:
    """Search a deferred catalog with a deliberately non-trivial schema."""

    return query[:limit]


def _profile(
    *,
    max_input_tokens: int = 100_000,
    supports_vision: bool = False,
    capture_provider_input_tokens: bool = True,
):
    return build_provider_request_profile(
        model=_model(max_input_tokens=max_input_tokens),
        model_name="lead",
        provider_adapter="openai",
        system_prompt="canonical system prompt " * 20,
        tools=(default_tool, deferred_tool),
        bounded_overlay_material=("bounded dynamic reminder",),
        supports_vision=supports_vision,
        capture_provider_input_tokens=capture_provider_input_tokens,
    )


def _request(
    profile,
    *,
    tools=None,
    messages=None,
    token_usage_tracking_enabled: bool | None = None,
) -> ModelRequest:
    state = {
        PROVIDER_REQUEST_PROFILE_STATE_KEY: profile.snapshot(),
        "summary_text": "durable summary",
        "delegations": [],
        "skill_context": [],
        "messages": messages or [HumanMessage(id="human-1", content="hello")],
    }
    runtime_context: dict[str, object] = {"run_id": "run-1"}
    if token_usage_tracking_enabled is not None:
        runtime_context[RuntimeContextKeys.TOKEN_USAGE_TRACKING_ENABLED] = token_usage_tracking_enabled
    return ModelRequest(
        model=_model(max_input_tokens=profile.max_input_tokens or 100_000),
        messages=list(state["messages"]),
        system_prompt=profile.system_prompt,
        tools=list(tools or profile.tools),
        state=state,
        runtime=Runtime(context=runtime_context),
    )


class _PolicyConfig:
    def __init__(
        self,
        *,
        trigger: tuple[str, int | float],
        tool_search_enabled: bool = True,
        max_recursion_limit: int = 1_000,
    ) -> None:
        self._value = {
            "max_recursion_limit": max_recursion_limit,
            "summarization": {
                "enabled": True,
                "trigger": trigger,
                "keep": ("messages", 20),
            },
            "tool_search": {"enabled": tool_search_enabled},
        }

    def model_dump(self, *, mode: str):
        assert mode == "json"
        return deepcopy(self._value)


def test_runtime_policy_compatibility_ignores_summarization_trigger() -> None:
    original = _PolicyConfig(trigger=("tokens", 100_000))
    changed_trigger = _PolicyConfig(trigger=("fraction", 0.8))
    changed_request_policy = _PolicyConfig(
        trigger=("tokens", 100_000),
        tool_search_enabled=False,
    )

    assert provider_request_runtime_policy_identity(original) != provider_request_runtime_policy_identity(changed_trigger)
    assert provider_request_runtime_policy_compatibility_identity(original) == provider_request_runtime_policy_compatibility_identity(changed_trigger)
    assert provider_request_runtime_policy_compatibility_identity(original) != provider_request_runtime_policy_compatibility_identity(changed_request_policy)


def test_runtime_policy_compatibility_ignores_execution_step_limit() -> None:
    original = _PolicyConfig(
        trigger=("tokens", 100_000),
        max_recursion_limit=1_000,
    )
    changed_step_limit = _PolicyConfig(
        trigger=("tokens", 100_000),
        max_recursion_limit=200,
    )

    assert provider_request_runtime_policy_identity(original) != provider_request_runtime_policy_identity(changed_step_limit)
    assert provider_request_runtime_policy_compatibility_identity(original) == provider_request_runtime_policy_compatibility_identity(changed_step_limit)


def test_profile_trigger_counts_first_call_system_all_tools_and_durable_context() -> None:
    profile = _profile()
    messages = [HumanMessage(id="human-1", content="x")]

    measured = measure_profile_context(
        profile,
        {
            "messages": messages,
            "summary_text": "retained durable summary",
            "delegations": [],
            "skill_context": [],
        },
    )

    assert measured.components["compressible"].safety_bound_tokens > 0
    assert measured.components["fixed"].safety_bound_tokens > measured.components["compressible"].safety_bound_tokens
    assert measured.components["ephemeral"].safety_bound_tokens > 0
    assert measured.full_tool_count == 2
    assert measured.safety_bound_tokens > measured.estimated_tokens


def test_profile_counts_middleware_prompt_and_tool_as_fixed_material() -> None:
    todo = TodoListMiddleware()
    without_middleware = _profile()
    with_middleware = build_provider_request_profile(
        model=_model(),
        model_name="lead",
        provider_adapter="openai",
        system_prompt="base",
        tools=(*collect_middleware_tools((todo,)), default_tool, deferred_tool),
        middleware_system_prompts=collect_middleware_system_prompts((todo,)),
    )

    assert with_middleware.full_tool_count == 3
    assert with_middleware.static_system_utf8_bytes > without_middleware.static_system_utf8_bytes


def test_todo_state_adds_exact_ephemeral_request_reserve() -> None:
    profile = _profile()
    baseline = measure_profile_context(profile, {"messages": []})
    with_todo = measure_profile_context(
        profile,
        {
            "messages": [],
            "todos": [{"content": "finish the audit", "status": "pending"}],
        },
    )

    assert with_todo.components["ephemeral"].safety_bound_tokens > baseline.components["ephemeral"].safety_bound_tokens


def test_slash_request_duplicate_adds_ephemeral_reserve() -> None:
    profile = _profile()
    ordinary = measure_profile_context(
        profile,
        {"messages": [HumanMessage(content="skill task")]},
    )
    activated = measure_profile_context(
        profile,
        {"messages": [HumanMessage(content="/audit skill task")]},
    )

    assert activated.components["ephemeral"].safety_bound_tokens > ordinary.components["ephemeral"].safety_bound_tokens


def test_full_catalog_profile_covers_initial_filter_and_later_promotion() -> None:
    profile = _profile()
    initial = profile.measure_request(_request(profile, tools=[default_tool]))
    promoted = profile.measure_request(_request(profile, tools=[default_tool, deferred_tool]))
    allowed = measure_profile_context(profile, _request(profile).state)

    assert initial.safety_bound_tokens < promoted.safety_bound_tokens
    assert promoted.safety_bound_tokens <= allowed.safety_bound_tokens


def test_final_guard_rejects_same_byte_request_with_more_message_overhead() -> None:
    profile = _profile()
    actual_messages = [HumanMessage(content="x" * 20) for _ in range(20)]
    request = _request(
        profile,
        messages=[HumanMessage(content="y" * 980)],
    ).override(messages=actual_messages)
    actual = profile.measure_request(request)
    allowed = measure_profile_context(profile, request.state)
    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(result=[AIMessage(content="must not run")])

    assert actual.material_utf8_bytes == allowed.material_utf8_bytes
    assert actual.safety_bound_tokens > allowed.safety_bound_tokens
    with pytest.raises(ProviderRequestProfileDrift):
        FinalProviderRequestGuard(profile).wrap_model_call(request, handler)
    assert called is False


def test_final_guard_rejects_message_count_outside_frozen_contract_when_safety_fits() -> None:
    profile = _profile()
    actual_messages = [HumanMessage(content="x" * 20) for _ in range(10)]
    request = _request(
        profile,
        messages=[HumanMessage(content="y" * 700)],
    ).override(messages=actual_messages)
    actual = profile.measure_request(request)
    allowed = measure_profile_context(profile, request.state)
    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(result=[AIMessage(content="must not run")])

    assert actual.material_utf8_bytes < allowed.material_utf8_bytes
    assert actual.safety_bound_tokens < allowed.safety_bound_tokens
    assert actual.message_count > allowed.message_count
    with pytest.raises(ProviderRequestProfileDrift):
        FinalProviderRequestGuard(profile).wrap_model_call(request, handler)
    assert called is False


def test_final_guard_rejects_same_size_tool_schema_outside_frozen_facts() -> None:
    profile = _profile()
    altered_tool = deepcopy(convert_to_openai_tool(default_tool))
    original_description = altered_tool["function"]["description"]
    altered_tool["function"]["description"] = "x" * len(original_description)
    original_fact = provider_tool_schema_fact(default_tool)
    altered_fact = provider_tool_schema_fact(altered_tool)
    request = _request(profile, tools=[altered_tool, deferred_tool])
    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(result=[AIMessage(content="must not run")])

    assert altered_fact["name"] == original_fact["name"]
    assert altered_fact["schema_utf8_bytes"] == original_fact["schema_utf8_bytes"]
    assert altered_fact["schema_sha256"] != original_fact["schema_sha256"]
    with pytest.raises(ProviderRequestProfileDrift):
        FinalProviderRequestGuard(profile).wrap_model_call(request, handler)
    assert called is False


def test_final_guard_fails_before_provider_when_actual_request_exceeds_capacity() -> None:
    profile = _profile(max_input_tokens=256)
    guard = FinalProviderRequestGuard(profile)
    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(result=[AIMessage(content="must not run")])

    with pytest.raises(ProviderRequestCapacityExceeded):
        guard.wrap_model_call(_request(profile), handler)

    assert called is False


def test_capacity_guard_does_not_treat_engineering_allowance_as_proven_overflow() -> None:
    profile = _profile(max_input_tokens=100_000)
    # UTF-8 material is large enough that a raw-byte or 2x-estimate hard stop
    # would reject it, while the versioned estimate remains below capacity.
    request = _request(
        profile,
        messages=[HumanMessage(content="中" * 80_000)],
    )
    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(result=[AIMessage(content="accepted")])

    result = FinalProviderRequestGuard(profile).wrap_model_call(request, handler)

    assert called is True
    assert isinstance(result, ExtendedModelResponse)


def test_capacity_guard_blocks_when_allowance_crosses_window() -> None:
    profile = _profile(max_input_tokens=100_000)
    request = _request(
        profile,
        messages=[HumanMessage(content="中" * 115_000)],
    )
    measured = profile.measure_request(request)
    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(result=[AIMessage(content="must not run")])

    assert measured.estimated_tokens < 100_000
    assert measured.safety_bound_tokens > 100_000
    with pytest.raises(ProviderRequestCapacityExceeded):
        FinalProviderRequestGuard(profile).wrap_model_call(request, handler)
    assert called is False


def test_final_guard_merges_scalar_measurement_without_replacing_outer_command() -> None:
    profile = _profile(max_input_tokens=200_000)
    guard = FinalProviderRequestGuard(profile)
    provider_message = AIMessage(
        id="provider-ai",
        content="answer",
        usage_metadata={"input_tokens": 123, "output_tokens": 4, "total_tokens": 127},
    )
    existing = ExtendedModelResponse(
        ModelResponse(result=[provider_message]),
        Command(update={"outer_fact": {"preserved": True}}, goto="tools"),
    )

    result = guard.wrap_model_call(_request(profile), lambda _request: existing)

    assert isinstance(result, ExtendedModelResponse)
    assert result.model_response.result == [provider_message]
    assert result.command is not None
    assert result.command.goto == "tools"
    assert result.command.update["outer_fact"] == {"preserved": True}
    measurement = result.command.update[PROVIDER_REQUEST_MEASUREMENT_STATE_KEY]
    assert measurement["provider_input_tokens"] == 123
    assert "messages" not in result.command.update


def test_final_guard_fails_closed_when_provider_input_exceeds_current_engineering_bound() -> None:
    profile = _profile(max_input_tokens=200_000)
    request = _request(profile)
    actual = profile.measure_request(request)
    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(
            result=[
                AIMessage(
                    content="answer",
                    usage_metadata={
                        "input_tokens": actual.safety_bound_tokens + 1,
                        "output_tokens": 1,
                        "total_tokens": actual.safety_bound_tokens + 2,
                    },
                )
            ]
        )

    with pytest.raises(ProviderRequestUsageUnsupported) as caught:
        FinalProviderRequestGuard(profile).wrap_model_call(request, handler)

    assert called is True
    assert "engineering safety bound" in (caught.value.internal_detail or "")


def test_final_guard_disabled_token_tracking_keeps_safety_measurement_without_persisting_exact_provider_usage() -> None:
    profile = _profile(max_input_tokens=200_000)
    provider_message = AIMessage(
        id="provider-ai-disabled-tracking",
        content="answer",
        usage_metadata={
            "input_tokens": 123,
            "output_tokens": 5,
            "total_tokens": 128,
        },
    )

    result = FinalProviderRequestGuard(profile).wrap_model_call(
        _request(
            profile,
            token_usage_tracking_enabled=False,
        ),
        lambda _request: ModelResponse(result=[provider_message]),
    )

    assert isinstance(result, ExtendedModelResponse)
    assert result.command is not None
    measurement = result.command.update[PROVIDER_REQUEST_MEASUREMENT_STATE_KEY]
    assert measurement["provider_input_tokens"] is None
    assert measurement["estimated_tokens"] > 0
    assert measurement["safety_bound_tokens"] >= measurement["estimated_tokens"]
    assert provider_message.usage_metadata == {
        "input_tokens": 123,
        "output_tokens": 5,
        "total_tokens": 128,
    }


def test_final_guard_tracking_off_still_rejects_provider_input_above_bound() -> None:
    profile = _profile(
        max_input_tokens=200_000,
        capture_provider_input_tokens=False,
    )
    request = _request(
        profile,
        token_usage_tracking_enabled=False,
    )
    actual = profile.measure_request(request)

    with pytest.raises(ProviderRequestUsageUnsupported) as caught:
        FinalProviderRequestGuard(profile).wrap_model_call(
            request,
            lambda _request: ModelResponse(
                result=[
                    AIMessage(
                        content="answer",
                        usage_metadata={
                            "input_tokens": actual.safety_bound_tokens + 1,
                            "output_tokens": 1,
                            "total_tokens": actual.safety_bound_tokens + 2,
                        },
                    )
                ]
            ),
        )

    assert profile.snapshot()["capture_provider_input_tokens"] is False
    assert "engineering safety bound" in (caught.value.internal_detail or "")


def test_final_guard_does_not_persist_exact_provider_usage_when_tracking_is_disabled() -> None:
    profile = _profile(
        max_input_tokens=200_000,
        capture_provider_input_tokens=False,
    )
    request = _request(profile)
    request = request.override(
        runtime=Runtime(
            context={
                "run_id": "run-1",
                RuntimeContextKeys.TOKEN_USAGE_TRACKING_ENABLED: False,
            }
        )
    )
    provider_message = AIMessage(
        content="answer",
        usage_metadata={"input_tokens": 123, "output_tokens": 4, "total_tokens": 127},
    )

    result = FinalProviderRequestGuard(profile).wrap_model_call(
        request,
        lambda _request: ModelResponse(result=[provider_message]),
    )

    assert isinstance(result, ExtendedModelResponse)
    assert profile.snapshot()["capture_provider_input_tokens"] is False
    assert result.command is not None
    measurement = result.command.update[PROVIDER_REQUEST_MEASUREMENT_STATE_KEY]
    assert measurement["provider_input_tokens"] is None


def test_final_guard_rejects_undeclared_visual_material() -> None:
    profile = _profile(supports_vision=True)
    request = _request(
        profile,
        messages=[
            HumanMessage(
                content=[
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                ]
            )
        ],
    )

    with pytest.raises(ProviderRequestUsageUnsupported) as caught:
        FinalProviderRequestGuard(profile).wrap_model_call(
            request,
            lambda _request: ModelResponse(result=[AIMessage(content="no")]),
        )
    assert "vision" in (caught.value.internal_detail or "")


def test_profile_snapshot_does_not_persist_prompt_or_tool_schema_plaintext() -> None:
    profile = _profile()
    snapshot = profile.snapshot()
    rendered = repr(snapshot)

    assert "canonical system prompt" not in rendered
    assert "deferred catalog" not in rendered
    assert snapshot["profile_fingerprint"] == profile.profile_fingerprint


def test_fact_only_profile_matches_the_same_provider_tool_material() -> None:
    profile = _profile()
    facts = tuple(provider_tool_schema_fact(tool) for tool in profile.tools)

    projected = build_provider_request_profile_snapshot_from_facts(
        model=_model(),
        model_name="lead",
        provider_adapter="openai",
        system_prompt=profile.system_prompt,
        tool_schema_facts=facts,
        bounded_overlay_material=("bounded dynamic reminder",),
    )

    assert projected["static_system_utf8_bytes"] == profile.static_system_utf8_bytes
    assert projected["full_tool_schema_utf8_bytes"] == profile.full_tool_schema_utf8_bytes
    assert projected["full_tool_count"] == profile.full_tool_count
    assert all(set(fact) == {"name", "schema_utf8_bytes", "schema_sha256"} for fact in facts)
    assert "Search a deferred catalog" not in repr(projected)
