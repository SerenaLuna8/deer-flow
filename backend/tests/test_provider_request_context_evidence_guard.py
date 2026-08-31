from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
)
from langchain_core.tools import tool
from langgraph.runtime import Runtime
from openai import APIConnectionError

from deerflow.agents.middlewares.durable_context_middleware import (
    render_durable_context_messages,
)
from deerflow.agents.middlewares.provider_request_cost_adapter import (
    ProviderModelRequestCostAdapter,
    ProviderRequestFragment,
    ProviderRequestFragmentKind,
    provider_visible_message_payload,
    provider_visible_messages_payload,
)
from deerflow.agents.middlewares.provider_request_usage import (
    PROVIDER_REQUEST_MEASUREMENT_STATE_KEY,
    ContextCapacityExceeded,
    FinalProviderRequestGuard,
    ProviderDispatchOutcomeAmbiguous,
    ProviderNoResponseProvenError,
    ProviderRequestEvidenceObserver,
    ProviderRequestUsageUnsupported,
    build_provider_request_profile,
)
from deerflow.agents.provider_request_contract import (
    CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
)
from deerflow.runtime.context_evidence import (
    CompactionProjection,
    ContextCheckpointEstimator,
    ContextCheckpointProjectionSnapshot,
    ContextContribution,
    ContextLane,
    ContextModelProjection,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProviderAmbiguityReason,
    ProviderCallIdentity,
    ProviderRetrySafety,
    TokenEstimate,
    TokenEstimateKind,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.tools.mcp_metadata import tag_mcp_tool


@tool
def builtin_lookup(query: str) -> str:
    """Look up one built-in record."""

    return query


@tool
def frozen_mcp_lookup(query: str, limit: int = 10) -> str:
    """Look up one record through an admitted MCP tool."""

    return f"{query}:{limit}"


tag_mcp_tool(frozen_mcp_lookup)


class _ToolBindingFakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _model(*, max_input_tokens: int = 100_000) -> _ToolBindingFakeModel:
    return _ToolBindingFakeModel(
        messages=iter([AIMessage(content="unused")]),
        profile={"max_input_tokens": max_input_tokens},
    )


@tool
def lookup(query: str) -> str:
    """Look up one record."""

    return query


def _profile(
    *,
    max_input_tokens: int = 100_000,
    capture_provider_input_tokens: bool = True,
    provider_adapter: str = "openai",
):
    return build_provider_request_profile(
        model=_model(max_input_tokens=max_input_tokens),
        model_name="lead",
        provider_adapter=provider_adapter,
        system_prompt="platform rules and agent instructions",
        tools=(lookup,),
        supports_vision=True,
        capture_provider_input_tokens=capture_provider_input_tokens,
    )


def _request(
    profile,
    *,
    messages: list | None = None,
    token_usage_tracking_enabled: bool = True,
) -> ModelRequest:
    return ModelRequest(
        model=_model(max_input_tokens=profile.max_input_tokens or 100_000),
        messages=messages or [HumanMessage(id="human-1", content="hello")],
        system_prompt=profile.system_prompt,
        tools=list(profile.tools),
        state={
            "provider_request_profile": profile.snapshot(),
            "summary_text": "earlier context",
            "messages": messages or [HumanMessage(id="human-1", content="hello")],
        },
        runtime=Runtime(
            context={
                "run_id": "run-1",
                RuntimeContextKeys.TOKEN_USAGE_TRACKING_ENABLED: token_usage_tracking_enabled,
            }
        ),
    )


def test_model_request_cost_adapter_projects_positive_closed_lanes() -> None:
    profile = _profile()
    _, summary_message = render_durable_context_messages(
        "earlier context",
        [],
        [],
    )
    request = _request(
        profile,
        messages=[summary_message, HumanMessage(id="human-1", content="new question")],
    )

    measurement = ProviderModelRequestCostAdapter.from_profile(profile).measure_final_request(request)

    by_lane = {item.lane: item for item in measurement.contributions}
    assert tuple(by_lane) == (
        ContextLane.SYSTEM_PROMPT,
        ContextLane.TOOL_DEFINITIONS,
        ContextLane.SUMMARIZED_CONVERSATION,
        ContextLane.CONVERSATION,
        ContextLane.PROVIDER_OVERHEAD,
    )
    assert all(item.token_estimate.kind is not TokenEstimateKind.UNMEASURED for item in by_lane.values())
    assert measurement.projected_tokens > 0
    assert measurement.require_safety_upper_bound() >= measurement.projected_tokens
    rendered = repr(measurement.to_safe_mapping())
    assert "earlier context" not in rendered
    assert "new question" not in rendered
    assert "platform rules" not in rendered


def test_model_request_measurement_excludes_local_message_metadata() -> None:
    profile = _profile()
    plain = HumanMessage(
        id="human-plain",
        content="--- BEGIN USER INPUT ---\nquestion\n--- END USER INPUT ---",
    )
    locally_annotated = HumanMessage(
        id="human-local",
        content=plain.content,
        additional_kwargs={
            "original_user_content": "question",
            "token_usage_attribution": {
                "version": 1,
                "kind": "final_answer",
            },
        },
    )
    plain_request = _request(profile, messages=[plain])
    annotated_request = _request(profile, messages=[locally_annotated])
    adapter = ProviderModelRequestCostAdapter.from_profile(profile)

    plain_measurement = adapter.measure_final_request(plain_request)
    annotated_measurement = adapter.measure_final_request(annotated_request)
    plain_material = profile.measure_request(plain_request)
    annotated_material = profile.measure_request(annotated_request)

    assert annotated_measurement.request_fingerprint == (plain_measurement.request_fingerprint)
    assert [(item.lane, item.model_visible_bytes, item.token_estimate) for item in annotated_measurement.contributions] == [(item.lane, item.model_visible_bytes, item.token_estimate) for item in plain_measurement.contributions]
    assert annotated_material.request_fingerprint == plain_material.request_fingerprint
    assert annotated_material.material_utf8_bytes == plain_material.material_utf8_bytes


def test_model_request_measurement_preserves_tool_result_status() -> None:
    profile = _profile(provider_adapter="anthropic")
    success = ToolMessage(
        content="result",
        tool_call_id="call-1",
        status="success",
    )
    error = ToolMessage(
        content="result",
        tool_call_id="call-1",
        status="error",
    )
    adapter = ProviderModelRequestCostAdapter.from_profile(profile)

    success_measurement = adapter.measure_final_request(
        _request(profile, messages=[success]),
    )
    error_measurement = adapter.measure_final_request(
        _request(profile, messages=[error]),
    )

    success_payload = provider_visible_message_payload(
        success,
        provider_adapter="anthropic",
    )
    error_payload = provider_visible_message_payload(
        error,
        provider_adapter="anthropic",
    )
    assert success_payload["content"][0]["is_error"] is False  # type: ignore[index]
    assert error_payload["content"][0]["is_error"] is True  # type: ignore[index]
    assert success_measurement.request_fingerprint != (error_measurement.request_fingerprint)


@pytest.mark.parametrize(
    "provider_adapter",
    [
        "anthropic",
        "deepseek",
        "openai",
        "openai_responses",
        "vllm",
    ],
)
def test_profile_and_cost_adapter_share_provider_wire_fingerprint(
    provider_adapter: str,
) -> None:
    profile = _profile(provider_adapter=provider_adapter)
    request = _request(
        profile,
        messages=[HumanMessage(content="provider-visible question")],
    )

    material = profile.measure_request(request)
    measurement = ProviderModelRequestCostAdapter.from_profile(
        profile,
    ).measure_final_request(request)

    assert measurement.request_fingerprint == material.request_fingerprint


def test_provider_wire_projection_normalizes_adapter_specific_replay_fields() -> None:
    reasoning = AIMessage(
        content="answer",
        additional_kwargs={
            "reasoning": "preferred reasoning",
            "reasoning_content": "fallback reasoning",
            "local_only": "must not be sent",
        },
    )
    deepseek = provider_visible_message_payload(
        reasoning,
        provider_adapter="deepseek",
    )
    vllm = provider_visible_message_payload(
        reasoning,
        provider_adapter="vllm",
    )
    assert deepseek["reasoning_content"] == "fallback reasoning"
    assert "reasoning" not in deepseek
    assert vllm["reasoning"] == "preferred reasoning"
    assert "reasoning_content" not in vllm
    assert "local_only" not in deepseek
    assert "local_only" not in vllm

    audio = AIMessage(
        content=[],
        additional_kwargs={
            "audio": {
                "id": "audio-1",
                "data": "x" * 20_000,
                "transcript": "y" * 20_000,
            },
        },
    )
    audio_payload = provider_visible_message_payload(
        audio,
        provider_adapter="openai",
    )
    assert audio_payload["audio"] == {"id": "audio-1"}

    anthropic_v1 = AIMessage(
        content=[
            {
                "type": "reasoning",
                "reasoning": "think",
                "signature": "signature",
            },
        ],
        response_metadata={
            "model_provider": "anthropic",
            "output_version": "v1",
        },
    )
    without_replay_metadata = anthropic_v1.model_copy(
        update={"response_metadata": {}},
    )
    assert provider_visible_message_payload(
        anthropic_v1,
        provider_adapter="anthropic",
    ) != provider_visible_message_payload(
        without_replay_metadata,
        provider_adapter="anthropic",
    )


def test_openai_responses_profile_uses_responses_wire_projection() -> None:
    profile = build_provider_request_profile(
        model=SimpleNamespace(
            profile={"max_input_tokens": 100_000},
            use_responses_api=True,
        ),
        model_name="responses-model",
        provider_adapter="openai",
        system_prompt="system",
        tools=(lookup,),
    )
    message = ToolMessage(
        content="computer output",
        tool_call_id="call-1",
        additional_kwargs={
            "type": "computer_call_output",
            "acknowledged_safety_checks": [
                {
                    "id": "check-1",
                    "code": "accepted",
                    "message": "approved",
                },
            ],
        },
    )
    request = _request(profile, messages=[message])

    payload = provider_visible_messages_payload(
        (message,),
        provider_adapter="openai_responses",
    )[0]
    material = profile.measure_request(request)
    measurement = ProviderModelRequestCostAdapter.from_profile(
        profile,
    ).measure_final_request(request)

    assert profile.provider_adapter == "openai_responses"
    assert payload["type"] == "computer_call_output"
    assert payload["acknowledged_safety_checks"] == [
        {
            "id": "check-1",
            "code": "accepted",
            "message": "approved",
        },
    ]
    assert measurement.request_fingerprint == material.request_fingerprint


def test_durable_context_message_splits_summary_skills_and_conversation_without_changing_wire_payload() -> None:
    profile = _profile()
    delegation = {
        "id": "task-1",
        "description": "inspect the implementation",
        "subagent_type": "general-purpose",
        "status": "completed",
        "result_brief": "done",
    }
    skill = {
        "name": "context-audit",
        "path": "/skills/context-audit/SKILL.md",
        "description": "Audit context attribution.",
        "loaded_at": 1,
    }
    authority_message, data_message = render_durable_context_messages(
        "earlier summary",
        [delegation],
        [skill],
    )
    messages = [authority_message, data_message, HumanMessage(id="human-1", content="new question")]
    request = _request(profile, messages=messages)

    measurement = ProviderModelRequestCostAdapter.from_profile(profile).measure_final_request(request)

    by_lane = {item.lane: item for item in measurement.contributions}
    assert by_lane[ContextLane.SUMMARIZED_CONVERSATION].model_visible_bytes == len(b"earlier summary")
    assert by_lane[ContextLane.SKILLS].model_visible_bytes > 0
    assert by_lane[ContextLane.CONVERSATION].model_visible_bytes > by_lane[ContextLane.SUMMARIZED_CONVERSATION].model_visible_bytes

    expected_request_material_bytes = sum(
        len(
            json.dumps(
                provider_visible_message_payload(message),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        for message in [SystemMessage(content=profile.system_prompt), *messages]
    )
    from langchain_core.utils.function_calling import convert_to_openai_tool

    expected_request_material_bytes += len(
        json.dumps(
            convert_to_openai_tool(lookup),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert sum(contribution.model_visible_bytes for contribution in measurement.contributions if contribution.lane is not ContextLane.PROVIDER_OVERHEAD) == expected_request_material_bytes
    serialized = repr(message_to_dict(data_message))
    assert "provenance" not in serialized
    assert "ContextLane" not in serialized


def test_unproven_durable_context_never_attributes_the_whole_message_to_summary() -> None:
    profile = _profile()
    mixed_message = HumanMessage(
        id="mixed-context",
        content=("<durable_context_data>\n## Conversation summary so far\nsummary\n\n## Active skills\n- skill details\n</durable_context_data>"),
        additional_kwargs={"hide_from_ui": True, "durable_context_data": True},
    )

    measurement = ProviderModelRequestCostAdapter.from_profile(profile).measure_final_request(_request(profile, messages=[mixed_message]))

    lanes = {item.lane for item in measurement.contributions}
    assert ContextLane.SUMMARIZED_CONVERSATION not in lanes
    assert ContextLane.CONVERSATION in lanes


def test_registered_lane_resolver_classifies_known_tool_sources_without_guessing() -> None:
    profile = _profile()

    class _Resolver:
        def resolve_lane(
            self,
            request: ModelRequest,
            fragment: ProviderRequestFragment,
            /,
        ) -> ContextLane | None:
            del request
            if fragment.kind is ProviderRequestFragmentKind.TOOL_DEFINITION and fragment.source_name == "lookup":
                return ContextLane.MCP_DYNAMIC_TOOLS
            return None

    measurement = ProviderModelRequestCostAdapter.from_profile(
        profile,
        lane_resolvers=(_Resolver(),),
    ).measure_final_request(_request(profile))

    assert ContextLane.MCP_DYNAMIC_TOOLS in {item.lane for item in measurement.contributions}
    assert ContextLane.TOOL_DEFINITIONS not in {item.lane for item in measurement.contributions}


def test_rendered_prompt_provenance_splits_known_system_sources_and_reconciles_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.agents.lead_agent import prompt as prompt_module

    monkeypatch.setattr(
        prompt_module,
        "_build_subagent_section",
        lambda *_args, **_kwargs: "<subagent_system>frozen delegates</subagent_system>",
    )
    monkeypatch.setattr(
        prompt_module,
        "get_skills_prompt_section",
        lambda *_args, **_kwargs: "<skill_system>frozen skill index</skill_system>",
    )
    monkeypatch.setattr(prompt_module, "_build_acp_section", lambda **_kwargs: "")
    monkeypatch.setattr(prompt_module, "_build_custom_mounts_section", lambda **_kwargs: "")
    render = prompt_module.render_prompt_template(
        subagent_enabled=True,
        exact_agent_prompt=prompt_module.AgentPromptBundle(
            payload_schema_version=2,
            agents_instructions="项目指令",
            soul="project soul",
            identity="",
            user_context="",
        ),
        mcp_routing_hints_section="<mcp_routing_hints>frozen route</mcp_routing_hints>",
        runtime_capability_notice="<runtime_capability_status>one unavailable</runtime_capability_status>",
    )
    profile = build_provider_request_profile(
        model=_model(),
        model_name="lead",
        provider_adapter="openai",
        system_prompt=render.system_prompt,
        tools=(builtin_lookup, frozen_mcp_lookup),
    )
    final_system_prompt = f"middleware prefix\n\n{render.system_prompt}\n\ndynamic reminder"
    request = ModelRequest(
        model=_model(),
        messages=[HumanMessage(id="human-1", content="hello")],
        system_prompt=final_system_prompt,
        tools=[builtin_lookup, frozen_mcp_lookup],
        state={"messages": [HumanMessage(id="human-1", content="hello")]},
        runtime=Runtime(context={"run_id": "run-1"}),
    )

    measurement = ProviderModelRequestCostAdapter.from_profile(
        profile,
        system_prompt_provenance=render.provenance,
        mcp_dynamic_tools=(frozen_mcp_lookup,),
    ).measure_final_request(request)

    by_lane = {item.lane: item for item in measurement.contributions}
    assert {
        ContextLane.SYSTEM_PROMPT,
        ContextLane.AGENT_INSTRUCTIONS,
        ContextLane.TOOL_DEFINITIONS,
        ContextLane.SKILLS,
        ContextLane.MCP_DYNAMIC_TOOLS,
        ContextLane.SUBAGENT_DEFINITIONS,
        ContextLane.CONVERSATION,
        ContextLane.PROVIDER_OVERHEAD,
    }.issubset(by_lane)

    import json

    def serialized_bytes(value: object) -> int:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    from langchain_core.utils.function_calling import convert_to_openai_tool

    expected_request_material_bytes = sum(
        (
            serialized_bytes(
                provider_visible_message_payload(
                    SystemMessage(content=final_system_prompt),
                ),
            ),
            serialized_bytes(
                provider_visible_message_payload(
                    HumanMessage(id="human-1", content="hello"),
                ),
            ),
            serialized_bytes(convert_to_openai_tool(builtin_lookup)),
            serialized_bytes(convert_to_openai_tool(frozen_mcp_lookup)),
        )
    )
    assert sum(contribution.model_visible_bytes for contribution in measurement.contributions if contribution.lane is not ContextLane.PROVIDER_OVERHEAD) == expected_request_material_bytes
    assert "项目指令" not in repr(render)
    assert "frozen skill index" not in repr(render.provenance)


def test_system_prompt_provenance_mismatch_falls_back_without_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.agents.lead_agent import prompt as prompt_module

    monkeypatch.setattr(
        prompt_module,
        "get_skills_prompt_section",
        lambda *_args, **_kwargs: "<skill_system>frozen skill index</skill_system>",
    )
    monkeypatch.setattr(prompt_module, "_build_acp_section", lambda **_kwargs: "")
    monkeypatch.setattr(prompt_module, "_build_custom_mounts_section", lambda **_kwargs: "")
    render = prompt_module.render_prompt_template()
    drifted_prompt = render.system_prompt.replace("<role>", "<changed-role>", 1)
    profile = build_provider_request_profile(
        model=_model(),
        model_name="lead",
        provider_adapter="openai",
        system_prompt=drifted_prompt,
        tools=(),
    )
    request = ModelRequest(
        model=_model(),
        messages=[HumanMessage(content="hello")],
        system_prompt=drifted_prompt,
        tools=[],
        state={"messages": [HumanMessage(content="hello")]},
        runtime=Runtime(context={"run_id": "run-1"}),
    )

    measurement = ProviderModelRequestCostAdapter.from_profile(
        profile,
        system_prompt_provenance=render.provenance,
    ).measure_final_request(request)

    lanes = {item.lane for item in measurement.contributions}
    assert ContextLane.SYSTEM_PROMPT in lanes
    assert ContextLane.SKILLS not in lanes


def test_mcp_lane_uses_exact_frozen_schema_provenance_not_tool_name() -> None:
    profile = build_provider_request_profile(
        model=_model(),
        model_name="lead",
        provider_adapter="openai",
        system_prompt="system",
        tools=(frozen_mcp_lookup,),
    )
    request = ModelRequest(
        model=_model(),
        messages=[HumanMessage(content="hello")],
        system_prompt="system",
        tools=[frozen_mcp_lookup],
        state={"messages": [HumanMessage(content="hello")]},
        runtime=Runtime(context={"run_id": "run-1"}),
    )

    without_provenance = ProviderModelRequestCostAdapter.from_profile(profile).measure_final_request(request)
    with_provenance = ProviderModelRequestCostAdapter.from_profile(
        profile,
        mcp_dynamic_tools=(frozen_mcp_lookup,),
    ).measure_final_request(request)

    assert ContextLane.MCP_DYNAMIC_TOOLS not in {item.lane for item in without_provenance.contributions}
    assert ContextLane.TOOL_DEFINITIONS in {item.lane for item in without_provenance.contributions}
    assert ContextLane.MCP_DYNAMIC_TOOLS in {item.lane for item in with_provenance.contributions}
    assert ContextLane.TOOL_DEFINITIONS not in {item.lane for item in with_provenance.contributions}


def test_state_system_message_is_not_misattributed_to_conversation() -> None:
    """Sub-Agent assembly carries its frozen prompt as a state message."""

    profile = _profile()
    measurement = ProviderModelRequestCostAdapter.from_profile(profile).measure_final_request(
        _request(
            profile,
            messages=[
                SystemMessage(id="subagent-system", content="frozen sub-agent instructions"),
                HumanMessage(id="human-1", content="new question"),
            ],
        )
    )

    by_lane = {item.lane: item for item in measurement.contributions}
    assert ContextLane.SYSTEM_PROMPT in by_lane
    assert ContextLane.CONVERSATION in by_lane
    assert by_lane[ContextLane.SYSTEM_PROMPT].model_visible_bytes > by_lane[ContextLane.CONVERSATION].model_visible_bytes


def test_visual_request_forms_partial_measurement_but_final_guard_fails_closed() -> None:
    # ``deepseek`` declares no per-image token cost, so visual material keeps
    # the fail-closed dispatch contract with a visible partial lower bound.
    profile = _profile(provider_adapter="deepseek")
    request = _request(
        profile,
        messages=[
            HumanMessage(
                id="human-image",
                content=[
                    {"type": "text", "text": "inspect this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AA=="},
                    },
                ],
            )
        ],
    )
    adapter = ProviderModelRequestCostAdapter.from_profile(profile)

    measurement = adapter.measure_final_request(request)

    visual = next(item for item in measurement.contributions if item.lane is ContextLane.VISUAL_MEDIA)
    assert visual.token_estimate.kind is TokenEstimateKind.UNMEASURED
    assert measurement.lower_bound_tokens > 0
    assert measurement.safety_upper_bound_tokens is None
    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(result=[AIMessage(content="must not run")])

    with pytest.raises(ProviderRequestUsageUnsupported) as caught:
        FinalProviderRequestGuard(profile, cost_adapter=adapter).wrap_model_call(
            request,
            handler,
        )

    assert called is False
    assert caught.value.internal_detail == "VISUAL_TOKEN_UPPER_BOUND_UNAVAILABLE:1"


def test_unknown_fixed_closure_measurement_keeps_final_guard_closed() -> None:
    profile = _profile()
    request = _request(profile)
    measurement = FinalRequestMeasurement(
        request_fingerprint="e" * 64,
        adapter_revision="checkpoint-bootstrap-v1",
        contributions=(
            ContextContribution(
                contribution_id="f" * 64,
                source_identity_digest="a" * 64,
                lane=ContextLane.SYSTEM_PROMPT,
                model_visible_bytes=0,
                token_estimate=TokenEstimate.unmeasured(item_count=1),
            ),
        ),
    )

    class PartialAdapter:
        def measure_final_request(self, _request: ModelRequest, /):
            return measurement

    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(result=[AIMessage(content="must not run")])

    with pytest.raises(ProviderRequestUsageUnsupported) as caught:
        FinalProviderRequestGuard(
            profile,
            cost_adapter=PartialAdapter(),
        ).wrap_model_call(request, handler)

    assert called is False
    assert caught.value.internal_detail == "CONTEXT_TOKEN_UPPER_BOUND_UNAVAILABLE:1"


class _RecordingObserver(ProviderRequestEvidenceObserver):
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events
        self.measurement: FinalRequestMeasurement | None = None

    async def record_request_prepared(
        self,
        measurement: FinalRequestMeasurement,
        /,
    ) -> ProviderCallIdentity:
        self.events.append(("prepared", measurement))
        self.measurement = measurement
        return ProviderCallIdentity.derive(
            subject=ContextSubject.lead_thread(thread_id="thread-1"),
            generation=ContextWindowGeneration(generation_id=UUID("44444444-4444-4444-8444-444444444444")),
            source_checkpoint_id="checkpoint-1",
            graph_step="lead:model",
            model_call_ordinal=1,
            request_fingerprint=measurement.request_fingerprint,
        )

    async def record_request_dispatched(self, provider_call: ProviderCallIdentity, /) -> None:
        self.events.append(("dispatched", provider_call.provider_call_id))

    async def record_provider_observed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        input_tokens: int,
    ) -> None:
        self.events.append(("observed", input_tokens))

    async def record_provider_usage_unreported(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None:
        self.events.append(("usage_unreported", provider_call.provider_call_id))

    async def record_provider_failed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        failure_code: str,
        retry_safety: ProviderRetrySafety,
    ) -> None:
        self.events.append(("failed", (failure_code, retry_safety)))

    async def record_provider_ambiguous(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        reason: ProviderAmbiguityReason,
    ) -> None:
        self.events.append(("ambiguous", reason))

    def checkpoint_projection_snapshot(
        self,
        *,
        estimator: ContextCheckpointEstimator,
        provider_call: ProviderCallIdentity,
        origin_run_id: str,
        provider_response_message_start: int,
        provider_response_message_count: int,
        provider_response_digest: str,
    ) -> ContextCheckpointProjectionSnapshot:
        if self.measurement is None:
            raise RuntimeError("missing test measurement")
        assert origin_run_id == "run-1"
        return ContextCheckpointProjectionSnapshot(
            generation=ContextWindowGeneration(
                generation_id=UUID("44444444-4444-4444-8444-444444444444"),
            ),
            model=ContextModelProjection(
                identity_digest="d" * 64,
                context_window_tokens=100_000,
            ),
            measurement=self.measurement,
            compaction=CompactionProjection(enabled=False, reached=False),
            estimator=estimator,
            provider_call_id=provider_call.provider_call_id,
            provider_subject=provider_call.subject,
            origin_run_id=origin_run_id,
            provider_response_message_start=provider_response_message_start,
            provider_response_message_count=provider_response_message_count,
            provider_response_digest=provider_response_digest,
        )


def _openai_connection_error(cause: Exception) -> APIConnectionError:
    request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/chat/completions",
    )
    try:
        raise cause
    except Exception as error:
        try:
            raise APIConnectionError(request=request) from error
        except APIConnectionError as wrapped:
            return wrapped


@pytest.mark.asyncio
async def test_async_guard_durably_orders_evidence_and_observes_raw_usage_when_tracking_is_off() -> None:
    profile = _profile(capture_provider_input_tokens=False)
    events: list[tuple[str, Any]] = []
    observer = _RecordingObserver(events)

    async def handler(_request: ModelRequest) -> ModelCallResult:
        events.append(("handler", None))
        return ModelResponse(
            result=[
                AIMessage(
                    content="answer",
                    usage_metadata={
                        "input_tokens": 123,
                        "output_tokens": 4,
                        "total_tokens": 127,
                    },
                )
            ]
        )

    result = await FinalProviderRequestGuard(
        profile,
        evidence_observer=observer,
    ).awrap_model_call(
        _request(profile, token_usage_tracking_enabled=False),
        handler,
    )

    assert [name for name, _value in events] == [
        "prepared",
        "dispatched",
        "handler",
        "observed",
    ]
    assert events[-1] == ("observed", 123)
    assert result.command is not None
    assert result.command.update[PROVIDER_REQUEST_MEASUREMENT_STATE_KEY]["provider_input_tokens"] is None
    projection_snapshot = result.command.update[CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY]
    assert projection_snapshot["measurement"]["request_fingerprint"] == (events[0][1].request_fingerprint)
    assert projection_snapshot["estimator"]["tool_count"] == 1
    assert projection_snapshot["estimator"]["visual_max_tokens_per_image"] == 2_048
    assert projection_snapshot["provider_call_id"] == (
        ProviderCallIdentity.derive(
            subject=ContextSubject.lead_thread(thread_id="thread-1"),
            generation=ContextWindowGeneration(
                generation_id=UUID("44444444-4444-4444-8444-444444444444"),
            ),
            source_checkpoint_id="checkpoint-1",
            graph_step="lead:model",
            model_call_ordinal=1,
            request_fingerprint=events[0][1].request_fingerprint,
        ).provider_call_id
    )
    assert projection_snapshot["provider_subject"] == {
        "kind": "lead_thread",
        "thread_id": "thread-1",
    }
    assert projection_snapshot["origin_run_id"] == "run-1"
    assert projection_snapshot["provider_response_message_start"] == 1
    assert projection_snapshot["provider_response_message_count"] == 1
    assert len(projection_snapshot["provider_response_digest"]) == 64
    assert "answer" not in repr(projection_snapshot)


@pytest.mark.asyncio
async def test_response_proof_failure_is_terminal_after_provider_observation() -> None:
    class _BrokenSnapshotObserver(_RecordingObserver):
        def checkpoint_projection_snapshot(self, **_kwargs: object) -> object:
            raise ValueError("response cannot be fingerprinted")

    profile = _profile()
    events: list[tuple[str, Any]] = []
    observer = _BrokenSnapshotObserver(events)

    async def handler(_request: ModelRequest) -> ModelCallResult:
        events.append(("handler", None))
        return ModelResponse(result=[AIMessage(content="answer")])

    with pytest.raises(ProviderDispatchOutcomeAmbiguous) as caught:
        await FinalProviderRequestGuard(
            profile,
            evidence_observer=observer,
        ).awrap_model_call(_request(profile), handler)

    assert [name for name, _value in events] == [
        "prepared",
        "dispatched",
        "handler",
        "usage_unreported",
    ]
    assert isinstance(caught.value.__cause__, ValueError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cause_type", "failure_code"),
    [
        (httpx.ConnectError, "PROVIDER_CONNECT_FAILED"),
        (httpx.ConnectTimeout, "PROVIDER_CONNECT_TIMEOUT"),
        (httpx.PoolTimeout, "PROVIDER_POOL_TIMEOUT"),
    ],
)
async def test_async_guard_records_openai_family_connect_stage_as_proven_no_response(
    cause_type: type[httpx.RequestError],
    failure_code: str,
) -> None:
    profile = _profile(provider_adapter="deepseek")
    events: list[tuple[str, Any]] = []
    observer = _RecordingObserver(events)
    request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/chat/completions",
    )
    provider_error = _openai_connection_error(
        cause_type("connect stage failed", request=request),
    )

    async def handler(_request: ModelRequest) -> ModelCallResult:
        events.append(("handler", None))
        raise provider_error

    with pytest.raises(ProviderNoResponseProvenError) as caught:
        await FinalProviderRequestGuard(
            profile,
            evidence_observer=observer,
        ).awrap_model_call(_request(profile), handler)

    assert caught.value.failure_code == failure_code
    assert caught.value.__cause__ is provider_error
    assert [name for name, _value in events] == [
        "prepared",
        "dispatched",
        "handler",
        "failed",
    ]
    assert events[-1] == (
        "failed",
        (failure_code, ProviderRetrySafety.NO_RESPONSE_PROVEN),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause_type",
    [
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
    ],
)
async def test_async_guard_keeps_post_connect_transport_failure_ambiguous(
    cause_type: type[httpx.RequestError],
) -> None:
    profile = _profile(provider_adapter="deepseek")
    events: list[tuple[str, Any]] = []
    observer = _RecordingObserver(events)
    request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/chat/completions",
    )
    provider_error = _openai_connection_error(
        cause_type("provider outcome unknown", request=request),
    )

    async def handler(_request: ModelRequest) -> ModelCallResult:
        events.append(("handler", None))
        raise provider_error

    with pytest.raises(ProviderDispatchOutcomeAmbiguous) as caught:
        await FinalProviderRequestGuard(
            profile,
            evidence_observer=observer,
        ).awrap_model_call(_request(profile), handler)

    assert caught.value.__cause__ is provider_error
    assert [name for name, _value in events] == [
        "prepared",
        "dispatched",
        "handler",
        "ambiguous",
    ]
    assert events[-1] == (
        "ambiguous",
        ProviderAmbiguityReason.DISPATCH_OUTCOME_UNKNOWN,
    )


@pytest.mark.asyncio
async def test_async_guard_keeps_unproven_api_connection_failure_ambiguous() -> None:
    profile = _profile(provider_adapter="deepseek")
    events: list[tuple[str, Any]] = []
    observer = _RecordingObserver(events)
    provider_error = APIConnectionError(
        request=httpx.Request(
            "POST",
            "https://provider.invalid/v1/chat/completions",
        ),
    )

    async def handler(_request: ModelRequest) -> ModelCallResult:
        events.append(("handler", None))
        raise provider_error

    with pytest.raises(ProviderDispatchOutcomeAmbiguous) as caught:
        await FinalProviderRequestGuard(
            profile,
            evidence_observer=observer,
        ).awrap_model_call(_request(profile), handler)

    assert caught.value.__cause__ is provider_error
    assert [name for name, _value in events] == [
        "prepared",
        "dispatched",
        "handler",
        "ambiguous",
    ]
    assert events[-1] == (
        "ambiguous",
        ProviderAmbiguityReason.DISPATCH_OUTCOME_UNKNOWN,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "terminal_event", "terminal_value", "raised_type"),
    [
        (
            ProviderNoResponseProvenError(failure_code="PROVIDER_TIMEOUT"),
            "failed",
            ("PROVIDER_TIMEOUT", ProviderRetrySafety.NO_RESPONSE_PROVEN),
            ProviderNoResponseProvenError,
        ),
        (
            RuntimeError("socket outcome unknown"),
            "ambiguous",
            ProviderAmbiguityReason.DISPATCH_OUTCOME_UNKNOWN,
            ProviderDispatchOutcomeAmbiguous,
        ),
    ],
)
async def test_async_guard_classifies_only_explicit_no_response_as_failed(
    error: Exception,
    terminal_event: str,
    terminal_value: Any,
    raised_type: type[Exception],
) -> None:
    profile = _profile()
    events: list[tuple[str, Any]] = []
    observer = _RecordingObserver(events)

    async def handler(_request: ModelRequest) -> ModelCallResult:
        events.append(("handler", None))
        raise error

    with pytest.raises(raised_type) as caught:
        await FinalProviderRequestGuard(
            profile,
            evidence_observer=observer,
        ).awrap_model_call(_request(profile), handler)

    assert [name for name, _value in events] == [
        "prepared",
        "dispatched",
        "handler",
        terminal_event,
    ]
    assert events[-1] == (terminal_event, terminal_value)
    if raised_type is ProviderDispatchOutcomeAmbiguous:
        assert str(caught.value) == "Provider dispatch outcome is ambiguous"
        assert caught.value.__cause__ is error
        assert "socket outcome unknown" not in str(caught.value)


def test_sync_guard_rejects_async_observer_before_provider_dispatch() -> None:
    profile = _profile()
    observer = _RecordingObserver([])
    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(result=[AIMessage(content="must not run")])

    with pytest.raises(RuntimeError, match="requires awrap_model_call"):
        FinalProviderRequestGuard(
            profile,
            evidence_observer=observer,
        ).wrap_model_call(_request(profile), handler)

    assert called is False


def test_final_guard_uses_core_safety_upper_bound_for_capacity() -> None:
    profile = _profile(max_input_tokens=10)
    called = False

    def handler(_request: ModelRequest) -> ModelResponse:
        nonlocal called
        called = True
        return ModelResponse(result=[AIMessage(content="must not run")])

    with pytest.raises(ContextCapacityExceeded):
        FinalProviderRequestGuard(profile).wrap_model_call(_request(profile), handler)

    assert called is False
