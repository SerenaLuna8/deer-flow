"""SDK-specific Sub-Agent graph runner inputs remain config-free."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.runtime import Runtime
from pydantic import SecretStr

from deerflow.agents.factory import create_deerflow_agent
from deerflow.agents.features import RuntimeFeatures
from deerflow.agents.lead_agent.prompt import AgentPromptBundle
from deerflow.agents.middlewares.input_sanitization_middleware import (
    InputSanitizationMiddleware,
)
from deerflow.agents.middlewares.provider_request_cost_adapter import (
    provider_visible_message_payload,
)
from deerflow.agents.middlewares.provider_request_usage import (
    FinalProviderRequestGuard,
)
from deerflow.agents.middlewares.tool_call_control import (
    TOOL_CALL_CONTROL_STATE_KEY,
    FixedToolCallControlScope,
    GraphToolCallControlTopology,
    ToolCallControlLoopFinalizationFailed,
    ToolCallControlStateInvalid,
    default_graph_tool_call_control_profile,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.runtime.context_carrier import RuntimeContextCarrier
from deerflow.runtime.context_evidence import ContextLane
from deerflow.subagents.binding import (
    ParentExecutionBindingFactory,
    SdkFeatureSnapshot,
    SdkParentExecutionProfile,
)
from deerflow.subagents.change_signal import SubagentChangeSignal
from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.delegated_context import DelegatedRuntimeContextProjection
from deerflow.subagents.executor import (
    _SubagentGraphResult,
    _SubagentGraphRunner,
    _SubagentGraphStatus,
)
from deerflow.tools.builtins.present_file_tool import present_file_tool
from deerflow.tools.mcp_metadata import tag_mcp_routing, tag_mcp_tool


def _config() -> SubagentConfig:
    return SubagentConfig(
        name="general-purpose",
        description="delegated work",
        model="inherit",
    )


def _delegated_context(
    *,
    app_config: object | None = None,
    run_id: str | None = None,
    token_usage_tracking_enabled: bool | None = None,
    agent_prompt_bundle: object | None = None,
) -> DelegatedRuntimeContextProjection:
    return DelegatedRuntimeContextProjection(
        _carrier=RuntimeContextCarrier(
            app_config=app_config,
            is_subagent=True,
            run_id=run_id,
            token_usage_tracking_enabled=token_usage_tracking_enabled,
        ),
        channel_identity_mode="absent",
        agent_prompt_bundle=agent_prompt_bundle,
        runtime_skills=(),
    )


def _configured_app_config(*, summarization_enabled: bool = False) -> AppConfig:
    return AppConfig(
        models=[
            ModelConfig(
                name="provider-model",
                display_name="Provider model",
                description="",
                use="langchain_openai:ChatOpenAI",
                model="provider-model",
                max_input_tokens=32_000,
                api_key=SecretStr("unit-test-key"),
                base_url="https://example.invalid/v1",
                supports_thinking=False,
                supports_vision=False,
            )
        ],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        database={"url": "postgresql://localhost/subagent-context-test"},
        summarization={
            "enabled": summarization_enabled,
            "model_name": "provider-model",
        },
    )


@pytest.mark.parametrize("disallowed_tools", [None, [], ["task"]])
def test_subagent_runner_never_exposes_lead_only_present_files(
    disallowed_tools: list[str] | None,
) -> None:
    @tool("safe_lookup")
    def safe_lookup(query: str) -> str:
        """Return a safe lookup result."""

        return query

    runner = _SubagentGraphRunner(
        config=SubagentConfig(
            name="runtime-agent",
            description="delegated work",
            model="inherit",
            disallowed_tools=disallowed_tools,
        ),
        tools=[safe_lookup, present_file_tool],
        delegated_context=_delegated_context(),
        model_override=MagicMock(name="sdk-model"),
        tool_search_enabled=False,
    )

    assert [candidate.name for candidate in runner.tools] == ["safe_lookup"]


@pytest.mark.asyncio
async def test_every_subagent_receives_platform_file_handoff_instruction() -> None:
    configurable_instruction = "After writing a deliverable, call `present_files`; if it is missing, tell the user that no download link can be created."
    skill_instruction = "Skill requirement: call `present_files` after creating the report."
    runner = _SubagentGraphRunner(
        config=SubagentConfig(
            name="runtime-agent",
            description="delegated work",
            system_prompt=configurable_instruction,
            model="inherit",
        ),
        tools=[],
        delegated_context=_delegated_context(),
        model_override=MagicMock(name="sdk-model"),
        tool_search_enabled=False,
    )
    runner._load_skill_messages = AsyncMock(  # type: ignore[method-assign]
        return_value=[SystemMessage(content=skill_instruction)],
    )

    state, _, _ = await runner._build_initial_state("create the report")

    system_prompt = str(state["messages"][0].content)
    normalized_system_prompt = " ".join(system_prompt.split())
    handoff_instruction = "If a Skill or delegated instruction asks you to call `present_files`, treat that step as Lead-owned."
    assert configurable_instruction in system_prompt
    assert skill_instruction in system_prompt
    assert handoff_instruction in system_prompt
    assert system_prompt.index(configurable_instruction) < system_prompt.index(skill_instruction)
    assert system_prompt.index(skill_instruction) < system_prompt.index(handoff_instruction)
    assert "do not report `present_files` as unavailable, invalid, or missing" in normalized_system_prompt
    assert "report the completed result and generated file paths only" in normalized_system_prompt


@pytest.mark.asyncio
async def test_configured_subagent_final_guard_uses_positive_prompt_and_mcp_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tool("builtin_lookup")
    def builtin_lookup(query: str) -> str:
        """Look up one built-in record."""

        return query

    @tool("remote_lookup")
    def remote_lookup(query: str) -> str:
        """Look up one remote record."""

        return query

    tag_mcp_tool(remote_lookup)
    tag_mcp_routing(
        remote_lookup,
        {
            "mode": "prefer",
            "keywords": ["remote"],
            "priority": 1,
        },
    )
    app_config = _configured_app_config()
    runner = _SubagentGraphRunner(
        config=SubagentConfig(
            name="runtime-agent",
            description="delegated work",
            system_prompt="configured Agent instructions",
            model="inherit",
        ),
        tools=[builtin_lookup, remote_lookup],
        delegated_context=_delegated_context(
            app_config=app_config,
            run_id="parent-run",
            token_usage_tracking_enabled=True,
            agent_prompt_bundle=AgentPromptBundle(
                payload_schema_version=2,
                agents_instructions="inherited Agent bundle",
                soul="",
                identity="",
                user_context="",
            ),
        ),
        parent_model="provider-model",
        tool_search_enabled=True,
    )
    runner._load_skill_messages = AsyncMock(  # type: ignore[method-assign]
        return_value=[SystemMessage(content="<skill>frozen Skill body</skill>")],
    )
    state, final_tools, deferred_setup = await runner._build_initial_state(
        "inspect the context",
    )
    captured: dict[str, object] = {}

    def capture_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "deerflow.subagents.executor.create_agent",
        capture_agent,
    )
    runner._create_agent(
        final_tools,
        deferred_setup=deferred_setup,
        execution_id=uuid.uuid4(),
    )
    guard = next(
        middleware
        for middleware in captured["middleware"]  # type: ignore[union-attr]
        if isinstance(middleware, FinalProviderRequestGuard)
    )
    request = ModelRequest(
        model=captured["model"],
        messages=state["messages"],
        system_prompt=None,
        tools=final_tools,
        state=state,
        runtime=Runtime(context={"run_id": "parent-run"}),
    )

    measurement = guard.cost_adapter.measure_final_request(request)

    by_lane = {item.lane: item for item in measurement.contributions}
    assert {
        ContextLane.SYSTEM_PROMPT,
        ContextLane.AGENT_INSTRUCTIONS,
        ContextLane.TOOL_DEFINITIONS,
        ContextLane.SKILLS,
        ContextLane.MCP_DYNAMIC_TOOLS,
        ContextLane.CONVERSATION,
        ContextLane.PROVIDER_OVERHEAD,
    }.issubset(by_lane)
    system_prompt = str(state["messages"][0].content)
    assert "configured Agent instructions" in system_prompt
    assert "inherited Agent bundle" in system_prompt
    assert "frozen Skill body" in system_prompt
    assert "<available-deferred-tools>" in system_prompt
    assert "<mcp_routing_hints>" in system_prompt

    def serialized_bytes(value: object) -> int:
        return len(
            __import__("json")
            .dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            .encode("utf-8")
        )

    expected_bytes = sum(serialized_bytes(provider_visible_message_payload(message)) for message in state["messages"])
    expected_bytes += sum(serialized_bytes(convert_to_openai_tool(candidate)) for candidate in final_tools)
    assert sum(contribution.model_visible_bytes for contribution in measurement.contributions if contribution.lane is not ContextLane.PROVIDER_OVERHEAD) == expected_bytes


@pytest.mark.asyncio
async def test_configured_subagent_profile_covers_input_sanitization_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_config = _configured_app_config()
    runner = _SubagentGraphRunner(
        config=SubagentConfig(
            name="runtime-agent",
            description="delegated work",
            model="inherit",
        ),
        tools=[],
        delegated_context=_delegated_context(
            app_config=app_config,
            run_id="parent-run",
            token_usage_tracking_enabled=True,
        ),
        parent_model="provider-model",
        tool_search_enabled=False,
    )
    state, final_tools, deferred_setup = await runner._build_initial_state(
        "x" * 1200,
    )
    captured: dict[str, object] = {}

    def capture_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "deerflow.subagents.executor.create_agent",
        capture_agent,
    )
    runner._create_agent(
        final_tools,
        deferred_setup=deferred_setup,
    )
    middlewares = captured["middleware"]
    sanitizer = next(
        middleware
        for middleware in middlewares  # type: ignore[union-attr]
        if isinstance(middleware, InputSanitizationMiddleware)
    )
    guard = next(
        middleware
        for middleware in middlewares  # type: ignore[union-attr]
        if isinstance(middleware, FinalProviderRequestGuard)
    )
    request = ModelRequest(
        model=captured["model"],
        messages=state["messages"],
        system_prompt=None,
        tools=final_tools,
        state=state,
        runtime=Runtime(context={"run_id": "parent-run"}),
    )
    sanitized = sanitizer._try_process(request)
    call_count = 0

    def handler(_request: ModelRequest) -> AIMessage:
        nonlocal call_count
        call_count += 1
        return AIMessage(content="ok")

    guard.wrap_model_call(sanitized, handler)

    continued_state = {
        **state,
        "messages": [
            *state["messages"],
            AIMessage(content="continue"),
            HumanMessage(content="y" * 5000),
        ],
    }
    continued = ModelRequest(
        model=captured["model"],
        messages=continued_state["messages"],
        system_prompt=None,
        tools=final_tools,
        state=continued_state,
        runtime=Runtime(context={"run_id": "parent-run"}),
    )
    guard.wrap_model_call(sanitizer._try_process(continued), handler)

    assert call_count == 2


@pytest.mark.asyncio
async def test_frozen_disabled_token_tracking_does_not_install_or_aggregate_subagent_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.subagents import executor as executor_module

    collector_factory = MagicMock(
        side_effect=AssertionError(
            "disabled token tracking must not install the Sub-Agent collector",
        )
    )
    monkeypatch.setattr(
        executor_module,
        "SubagentTokenCollector",
        collector_factory,
    )
    monkeypatch.setattr(executor_module, "build_tracing_callbacks", lambda: [])
    monkeypatch.setattr(
        executor_module,
        "inject_langfuse_metadata",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        executor_module,
        "is_trace_correlation_enabled",
        lambda _config: False,
    )

    # The explicit frozen Run flag wins even if the retained AppConfig object
    # would otherwise enable tracking.
    app_config = SimpleNamespace(
        token_usage=SimpleNamespace(enabled=True),
    )
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(
            app_config=app_config,
            run_id="parent-run",
            token_usage_tracking_enabled=False,
        ),
        parent_model="provider-model",
        model_override=MagicMock(name="sdk-model"),
        middleware_override=(),
        tool_search_enabled=False,
    )
    captured_callbacks: list[object] = []

    class Agent:
        async def astream(
            self,
            _state,
            *,
            config,
            context,
            stream_mode,
        ):
            del context
            assert stream_mode == "values"
            captured_callbacks.extend(config.get("callbacks") or [])
            yield {"messages": [AIMessage(content="delegated result")]}

    async def build_initial_state(_task: str):
        return (
            {},
            [],
            SimpleNamespace(deferred_names=frozenset()),
        )

    runner._build_initial_state = build_initial_state  # type: ignore[method-assign]
    runner._create_agent = MagicMock(return_value=Agent())  # type: ignore[method-assign]
    result = _SubagentGraphResult(
        execution_id=uuid.uuid4(),
        trace_id=runner.trace_id,
        status=_SubagentGraphStatus.PENDING,
    )

    outcome = await runner._aexecute("inspect delegated state", result)

    assert outcome.status is _SubagentGraphStatus.COMPLETED
    assert outcome.token_usage_records == []
    assert captured_callbacks == []
    collector_factory.assert_not_called()


@pytest.mark.asyncio
async def test_frozen_enabled_token_tracking_preserves_subagent_collector_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.subagents import executor as executor_module

    usage_records = [
        {
            "source_run_id": "subagent-call-1",
            "caller": "subagent:general-purpose",
            "model_name": "provider-model",
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
        }
    ]
    collector = MagicMock()
    collector.snapshot_records.return_value = usage_records
    collector_factory = MagicMock(return_value=collector)
    monkeypatch.setattr(
        executor_module,
        "SubagentTokenCollector",
        collector_factory,
    )
    monkeypatch.setattr(executor_module, "build_tracing_callbacks", lambda: [])
    monkeypatch.setattr(
        executor_module,
        "inject_langfuse_metadata",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        executor_module,
        "is_trace_correlation_enabled",
        lambda _config: False,
    )

    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(
            app_config=SimpleNamespace(
                token_usage=SimpleNamespace(enabled=False),
            ),
            run_id="parent-run",
            token_usage_tracking_enabled=True,
        ),
        parent_model="provider-model",
        model_override=MagicMock(name="sdk-model"),
        middleware_override=(),
        tool_search_enabled=False,
    )
    captured_callbacks: list[object] = []

    class Agent:
        async def astream(
            self,
            _state,
            *,
            config,
            context,
            stream_mode,
        ):
            del context
            assert stream_mode == "values"
            captured_callbacks.extend(config.get("callbacks") or [])
            yield {"messages": [AIMessage(content="delegated result")]}

    async def build_initial_state(_task: str):
        return (
            {},
            [],
            SimpleNamespace(deferred_names=frozenset()),
        )

    runner._build_initial_state = build_initial_state  # type: ignore[method-assign]
    runner._create_agent = MagicMock(return_value=Agent())  # type: ignore[method-assign]
    result = _SubagentGraphResult(
        execution_id=uuid.uuid4(),
        trace_id=runner.trace_id,
        status=_SubagentGraphStatus.PENDING,
    )

    outcome = await runner._aexecute("inspect delegated state", result)

    assert outcome.status is _SubagentGraphStatus.COMPLETED
    assert outcome.token_usage_records == usage_records
    assert captured_callbacks == [collector]
    collector_factory.assert_called_once_with(
        caller="subagent:general-purpose",
    )


def test_sdk_full_takeover_uses_exact_model_and_middleware_without_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.subagents import executor as executor_module

    model = object()
    middleware = object()
    captured: dict[str, object] = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(executor_module, "create_agent", fake_create_agent)
    monkeypatch.setattr(
        executor_module,
        "get_app_config",
        lambda: (_ for _ in ()).throw(AssertionError("global config was read")),
    )
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(),
        model_override=model,
        middleware_override=(middleware,),
        tool_search_enabled=False,
    )

    runner._create_agent([])

    assert captured["model"] is model
    assert captured["middleware"] == [middleware]
    assert runner.app_config is None


def test_sdk_full_takeover_does_not_inject_graph_tool_call_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.subagents import executor as executor_module

    model = object()
    caller_middleware = object()
    captured: dict[str, object] = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(executor_module, "create_agent", fake_create_agent)
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(),
        model_override=model,
        middleware_override=(caller_middleware,),
        tool_search_enabled=False,
    )

    runner._create_agent([], execution_id=uuid.uuid4())

    assert captured["middleware"] == [caller_middleware]


def test_sdk_feature_profile_preserves_explicit_extra_middleware_once_for_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.agents import factory as factory_module
    from deerflow.subagents import executor as executor_module

    class DelegatedMarkerMiddleware(AgentMiddleware):
        pass

    model = MagicMock(name="sdk-model")
    marker = DelegatedMarkerMiddleware()
    parent_graph_inputs: dict[str, object] = {}

    def capture_parent_graph(**kwargs):
        parent_graph_inputs.update(kwargs)
        return object()

    monkeypatch.setattr(factory_module, "create_agent", capture_parent_graph)
    create_deerflow_agent(
        model,
        features=RuntimeFeatures(subagent=True, sandbox=False),
        extra_middleware=[marker],
    )

    bound_task = next(
        tool
        for tool in parent_graph_inputs["tools"]  # type: ignore[union-attr]
        if tool.name == "task"
    )
    binding_factory = next(cell.cell_contents for cell in bound_task.coroutine.__closure__ or () if type(cell.cell_contents) is ParentExecutionBindingFactory)
    profile = binding_factory.profile
    assert type(profile) is SdkParentExecutionProfile
    assert profile.features is not None
    assert binding_factory.tool_call_control_topology is not None

    delegated_graph_inputs: dict[str, object] = {}

    def capture_delegated_graph(**kwargs):
        delegated_graph_inputs.update(kwargs)
        return object()

    monkeypatch.setattr(executor_module, "create_agent", capture_delegated_graph)
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(),
        model_override=profile.graph.model,
        sdk_feature_snapshot=profile.features,
        tool_search_enabled=False,
        tool_call_control_topology=binding_factory.tool_call_control_topology,
    )

    runner._create_agent([], execution_id=uuid.uuid4())

    assert (
        sum(
            middleware is marker
            for middleware in delegated_graph_inputs["middleware"]  # type: ignore[union-attr]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_sdk_feature_profile_builds_state_without_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.subagents import executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "get_app_config",
        lambda: (_ for _ in ()).throw(AssertionError("global config was read")),
    )
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(),
        model_override=MagicMock(name="sdk-model"),
        sdk_feature_snapshot=SdkFeatureSnapshot(
            sandbox=False,
            memory=False,
            summarization=False,
            subagent=True,
            vision=False,
            auto_title=True,
            guardrail=False,
            loop_detection=False,
            token_budget=False,
        ),
        tool_search_enabled=False,
    )

    state, tools, deferred = await runner._build_initial_state("inspect")

    assert state["messages"][-1].content == "inspect"
    assert tools == []
    assert deferred.deferred_names == frozenset()


def test_configured_subagent_control_uses_lifecycle_internal_execution_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.agents.middlewares import assembly as assembly_module
    from deerflow.subagents import executor as executor_module

    captured: dict[str, object] = {}

    class _ModelRuntime:
        def __init__(self, *, app_config: object) -> None:
            del app_config

        def build_chat_model(self, **kwargs: object) -> object:
            del kwargs
            model = MagicMock()
            model.profile = {"max_input_tokens": 32_000}
            return model

    def fake_build_subagent_runtime_middlewares(**kwargs: object) -> list[object]:
        captured.update(kwargs)
        return [kwargs["tool_call_control"]]

    monkeypatch.setattr(executor_module, "ModelRuntime", _ModelRuntime)
    monkeypatch.setattr(
        assembly_module,
        "build_subagent_runtime_middlewares",
        fake_build_subagent_runtime_middlewares,
    )
    monkeypatch.setattr(
        assembly_module,
        "append_final_provider_request_guard",
        lambda middlewares, guard: [*middlewares, guard],
    )
    monkeypatch.setattr(
        executor_module,
        "create_agent",
        lambda **_kwargs: object(),
    )
    execution_id = uuid.uuid4()
    control_profile = default_graph_tool_call_control_profile("research")
    topology = GraphToolCallControlTopology(
        profile=control_profile,
        lead_scope=FixedToolCallControlScope("parent-run-id"),
    )
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(
            app_config=_configured_app_config(),
            run_id="parent-run-id",
        ),
        parent_model="provider-model",
        tool_call_control_topology=topology,
    )

    runner._create_agent([], execution_id=execution_id)

    control = captured["tool_call_control"]
    initialized = control.before_agent({}, Runtime(context={}))
    assert initialized is not None
    assert initialized[TOOL_CALL_CONTROL_STATE_KEY]["scope_id"] == str(execution_id)


def test_configured_subagent_graph_installs_execution_observer_on_final_provider_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.subagents import executor as executor_module

    captured: dict[str, object] = {}
    app_config = _configured_app_config()
    model = MagicMock(name="subagent-model")
    model.profile = {"max_input_tokens": 32_000}
    evidence_observer = MagicMock(name="execution-evidence-observer")

    class _ModelRuntime:
        def __init__(self, *, app_config: object) -> None:
            assert app_config is app_config_instance

        def build_chat_model(self, **kwargs: object) -> object:
            assert kwargs["model_name"] == "provider-model"
            return model

    app_config_instance = app_config
    monkeypatch.setattr(executor_module, "ModelRuntime", _ModelRuntime)

    def capture_agent(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(executor_module, "create_agent", capture_agent)
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(
            app_config=app_config,
            run_id="parent-run-id",
        ),
        parent_model="provider-model",
    )

    runner._create_agent(
        [],
        execution_id=uuid.uuid4(),
        context_evidence_observer=evidence_observer,
    )

    middlewares = captured["middleware"]
    assert isinstance(middlewares, list)
    guards = [middleware for middleware in middlewares if isinstance(middleware, FinalProviderRequestGuard)]
    assert len(guards) == 1
    assert middlewares[-1] is guards[0]
    assert guards[0].evidence_observer is evidence_observer
    assert guards[0].profile.model_name == "provider-model"
    assert guards[0].profile.max_input_tokens == 32_000
    assert guards[0].profile.authority_identity == "parent-run-id"
    assert captured["checkpointer"] is False


def test_subagent_stop_receipts_use_their_own_scope_and_priority() -> None:
    internal_execution_id = uuid.uuid4()
    consumed: list[tuple[str, str | None]] = []

    class _Receipt:
        def __init__(self, owner: str, reason: str | None) -> None:
            self.owner = owner
            self.reason = reason

        def consume_stop_reason(self, scope_id: str | None) -> str | None:
            consumed.append((self.owner, scope_id))
            return self.reason

    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(run_id="parent-run-id"),
        model_override=object(),
        middleware_override=(),
        tool_search_enabled=False,
    )
    runner._tool_call_control_middleware = _Receipt(  # type: ignore[assignment]
        "tool-control",
        "tool_budget_capped",
    )
    runner._additional_stop_reason_middlewares = [
        _Receipt("token-budget", "token_capped"),
        _Receipt("later-loop", "loop_capped"),
        _Receipt("provider-output", "output_truncated"),
    ]

    assert runner._consume_stop_reason(internal_execution_id) == "output_truncated"
    assert consumed == [
        ("tool-control", str(internal_execution_id)),
        ("token-budget", "parent-run-id"),
        ("later-loop", "parent-run-id"),
        ("provider-output", "parent-run-id"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_status", "expected_result", "expected_error"),
    [
        (
            "usable partial result",
            _SubagentGraphStatus.COMPLETED,
            "usable partial result",
            None,
        ),
        ("", _SubagentGraphStatus.FAILED, None, "MODEL_OUTPUT_LIMIT"),
    ],
)
async def test_provider_output_truncation_requires_usable_partial_text(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected_status: _SubagentGraphStatus,
    expected_result: str | None,
    expected_error: str | None,
) -> None:
    from deerflow.subagents import executor as executor_module

    monkeypatch.setattr(executor_module, "build_tracing_callbacks", lambda: [])
    monkeypatch.setattr(
        executor_module,
        "inject_langfuse_metadata",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        executor_module,
        "is_trace_correlation_enabled",
        lambda _config: False,
    )

    class _OutputLimitReceipt:
        def consume_stop_reason(self, _scope_id: str | None) -> str:
            return "output_truncated"

    class _Agent:
        async def astream(self, *_args: object, **_kwargs: object):
            yield {
                "messages": [
                    AIMessage(
                        content=content,
                        response_metadata={"finish_reason": "length"},
                    )
                ]
            }

    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(
            app_config=SimpleNamespace(
                token_usage=SimpleNamespace(enabled=False),
            ),
            run_id="parent-run-id",
            token_usage_tracking_enabled=False,
        ),
        parent_model="provider-model",
        model_override=object(),
        middleware_override=(),
        tool_search_enabled=False,
    )

    async def build_initial_state(_task: str):
        return {}, [], SimpleNamespace(deferred_names=frozenset())

    runner._build_initial_state = build_initial_state  # type: ignore[method-assign]
    runner._create_agent = MagicMock(return_value=_Agent())  # type: ignore[method-assign]
    runner._additional_stop_reason_middlewares = [_OutputLimitReceipt()]
    result = _SubagentGraphResult(
        execution_id=uuid.uuid4(),
        trace_id=runner.trace_id,
        status=_SubagentGraphStatus.PENDING,
    )

    outcome = await runner._aexecute("inspect delegated state", result)

    assert outcome.status is expected_status
    assert outcome.result == expected_result
    assert outcome.error == expected_error
    assert outcome.stop_reason == "output_truncated"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
async def test_subagent_runner_settles_context_evidence_after_every_graph_terminal(
    terminal_status: str,
) -> None:
    execution_id = uuid.uuid4()
    calls: list[tuple[str, object]] = []

    class _Observer:
        async def record_settled(self) -> None:
            calls.append(("settled", asyncio.get_running_loop()))

    async def create_observer(
        current_execution_id: uuid.UUID,
        model_name: str,
    ) -> _Observer:
        calls.append(("created", current_execution_id))
        assert model_name == "provider-model"
        return _Observer()

    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(run_id="parent-run-id"),
        parent_model="provider-model",
        model_override=object(),
        middleware_override=(),
        tool_search_enabled=False,
        context_evidence_observer_factory=create_observer,
    )
    holder = runner._create_lifecycle_result_holder(
        execution_id=execution_id,
        changes=SubagentChangeSignal(),
    )

    async def execute(_prompt: str, result):  # type: ignore[no-untyped-def]
        if terminal_status == "completed":
            result.try_set_terminal(
                _SubagentGraphStatus.COMPLETED,
                result="done",
            )
        else:
            result.try_set_terminal(
                _SubagentGraphStatus.FAILED,
                error="SUBAGENT_EXECUTION_FAILED",
            )
        return result

    runner._aexecute = execute  # type: ignore[method-assign]

    await runner._run_lifecycle_graph("inspect", holder)

    assert calls == [
        ("created", execution_id),
        ("settled", asyncio.get_running_loop()),
    ]


@pytest.mark.asyncio
async def test_subagent_runner_settles_context_evidence_before_cancellation_unwinds() -> None:
    execution_id = uuid.uuid4()
    graph_started = asyncio.Event()
    settlement_started = asyncio.Event()
    release_settlement = asyncio.Event()
    settled = asyncio.Event()

    class _Observer:
        async def record_settled(self) -> None:
            settlement_started.set()
            await release_settlement.wait()
            settled.set()

    async def create_observer(
        current_execution_id: uuid.UUID,
        model_name: str,
    ) -> _Observer:
        assert current_execution_id == execution_id
        assert model_name == "provider-model"
        return _Observer()

    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(run_id="parent-run-id"),
        parent_model="provider-model",
        model_override=object(),
        middleware_override=(),
        tool_search_enabled=False,
        context_evidence_observer_factory=create_observer,
    )
    holder = runner._create_lifecycle_result_holder(
        execution_id=execution_id,
        changes=SubagentChangeSignal(),
    )

    async def execute(_prompt: str, _result):  # type: ignore[no-untyped-def]
        graph_started.set()
        await asyncio.Event().wait()

    runner._aexecute = execute  # type: ignore[method-assign]
    task = asyncio.create_task(
        runner._run_lifecycle_graph("inspect", holder),
    )
    await graph_started.wait()
    task.cancel()
    await settlement_started.wait()
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    release_settlement.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert settled.is_set()


@pytest.mark.asyncio
async def test_context_evidence_factory_never_resolves_a_model_from_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.subagents import executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "get_app_config",
        lambda: (_ for _ in ()).throw(
            AssertionError("current platform config must not be read"),
        ),
    )

    async def create_observer(
        _execution_id: uuid.UUID,
        _model_name: str,
    ) -> object:
        raise AssertionError("observer must not be created without frozen model")

    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(run_id="parent-run-id"),
        model_override=object(),
        middleware_override=(),
        tool_search_enabled=False,
        context_evidence_observer_factory=create_observer,
    )
    holder = runner._create_lifecycle_result_holder(
        execution_id=uuid.uuid4(),
        changes=SubagentChangeSignal(),
    )

    with pytest.raises(
        RuntimeError,
        match="frozen Sub-Agent model is unavailable",
    ):
        await runner._run_lifecycle_graph("inspect", holder)


@pytest.mark.asyncio
async def test_subagent_runner_preserves_tool_control_state_invalid_code(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from deerflow.subagents import executor as executor_module

    sensitive_detail = "sensitive cross-run state"

    class _FailingGraph:
        async def astream(self, *_args: object, **_kwargs: object):
            if False:
                yield None
            raise ToolCallControlStateInvalid(sensitive_detail)

    monkeypatch.setattr(
        executor_module,
        "create_agent",
        lambda **_kwargs: _FailingGraph(),
    )
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(),
        model_override=object(),
        middleware_override=(),
        tool_search_enabled=False,
    )
    holder = runner._create_lifecycle_result_holder(
        execution_id=uuid.uuid4(),
        changes=SubagentChangeSignal(),
    )

    await runner._run_lifecycle_graph("inspect", holder)

    snapshot = holder._snapshot_for_lifecycle()
    assert snapshot.status == "failed"
    assert snapshot.error == "TOOL_CALL_CONTROL_STATE_INVALID"
    assert sensitive_detail not in (snapshot.error or "")
    assert sensitive_detail not in caplog.text


@pytest.mark.asyncio
async def test_subagent_runner_preserves_loop_finalization_failed_code(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from deerflow.subagents import executor as executor_module

    sensitive_detail = "sensitive repeated proposal"

    class _FailingGraph:
        async def astream(self, *_args: object, **_kwargs: object):
            if False:
                yield None
            raise ToolCallControlLoopFinalizationFailed(sensitive_detail)

    monkeypatch.setattr(
        executor_module,
        "create_agent",
        lambda **_kwargs: _FailingGraph(),
    )
    runner = _SubagentGraphRunner(
        config=_config(),
        tools=[],
        delegated_context=_delegated_context(),
        model_override=object(),
        middleware_override=(),
        tool_search_enabled=False,
    )
    holder = runner._create_lifecycle_result_holder(
        execution_id=uuid.uuid4(),
        changes=SubagentChangeSignal(),
    )

    await runner._run_lifecycle_graph("inspect", holder)

    snapshot = holder._snapshot_for_lifecycle()
    assert snapshot.status == "failed"
    assert snapshot.error == "LOOP_FINALIZATION_FAILED"
    assert sensitive_detail not in (snapshot.error or "")
    assert sensitive_detail not in caplog.text
