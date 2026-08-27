from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.runtime import Runtime
from pydantic import SecretStr

import deerflow.agents.lead_agent.agent as lead_agent_module
from deerflow.agents.lead_agent.agent import TrustedLeadAgentExtension
from deerflow.agents.lead_agent.prompt import LeadPromptText
from deerflow.agents.middlewares.manifest import (
    MiddlewarePhase,
    assign_middleware_layer,
)
from deerflow.agents.middlewares.provider_request_cost_adapter import (
    SystemPromptLaneSpan,
    SystemPromptProvenance,
)
from deerflow.agents.middlewares.provider_request_usage import (
    FinalProviderRequestGuard,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.runtime.context_evidence import ContextLane
from deerflow.subagents.binding import (
    ParentExecutionBindingFactory,
    PrivateRunParentExecutionProfile,
)
from deerflow.tools.builtins import task_tool as canonical_task_tool
from deerflow.tools.mcp_metadata import tag_mcp_tool

MODEL_NAME = "77777777-7777-4777-8777-777777777778"
FULL_TOOL_GROUPS = ("web", "file:read", "file:write", "bash", "task")
BUILDER_SYSTEM_PROMPT = """You are the internal Skill Builder.
Candidate Skill files remain governed by the draft tools.
Finish with finalize_skill_candidate or request_skill_clarification.
"""


class _BuilderTerminalMiddleware(AgentMiddleware):
    pass


class _BuilderOutputLimitMiddleware(AgentMiddleware):
    pass


class _AfterModelOnlyMiddleware(AgentMiddleware):
    def after_model(self, state, runtime):  # type: ignore[no-untyped-def]
        return None


class _UnboundedRequestShaperMiddleware(AgentMiddleware):
    def wrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        return handler(request)


class _BoundedRequestShaperMiddleware(_UnboundedRequestShaperMiddleware):
    provider_request_bounded_overlay_material = (
        HumanMessage(
            content="bounded trusted reminder",
            name="trusted_reminder",
            additional_kwargs={"hide_from_ui": True},
        ),
    )
    provider_request_bounded_overlay_message_count = 1


def _tool(name: str) -> StructuredTool:
    def invoke() -> str:
        return name

    return StructuredTool.from_function(
        func=invoke,
        name=name,
        description=f"{name} test tool",
    )


def _app_config() -> AppConfig:
    return AppConfig(
        models=[
            ModelConfig(
                name=MODEL_NAME,
                display_name="Trusted extension test",
                description="",
                use="langchain_openai:ChatOpenAI",
                model="test-model",
                max_input_tokens=64_000,
                api_key=SecretStr("unit-test-key"),
                supports_thinking=False,
                supports_reasoning_effort=False,
            )
        ],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        summarization={"enabled": False},
        tool_search={"enabled": False},
        guardrails={"enabled": False},
    )


def _private_runtime(tmp_path: Path) -> object:
    return SimpleNamespace(
        model_ref=MODEL_NAME,
        model_settings=None,
        tool_groups=FULL_TOOL_GROUPS,
        skills=(),
        safe_manifest=SimpleNamespace(skills=()),
        mcp_tools=(),
        skill_root=tmp_path,
        prompt_bundle=None,
        soul="builder prompt",
        agent_catalog=None,
        capability_notice="",
        provider_request_closure_identity="closure-test",
        provider_request_mcp_closure_present=False,
    )


def _install_factory_spies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured: dict[str, object],
) -> None:
    baseline = _tool("baseline_tool")
    ordinary_clarification = _tool("ask_clarification")

    def available_tools(**kwargs):  # type: ignore[no-untyped-def]
        captured["available_tool_kwargs"] = kwargs
        return [baseline, ordinary_clarification]

    def build_middlewares(*_args, **kwargs):  # type: ignore[no-untyped-def]
        captured["middleware_kwargs"] = kwargs
        return [
            assign_middleware_layer(
                middleware,
                layer_id=f"custom_{index}",
                phase=MiddlewarePhase.CUSTOM,
                slot=index,
                why="Test custom middleware layer.",
            )
            for index, middleware in enumerate(
                kwargs.get("custom_middlewares") or (),
                start=1,
            )
        ]

    def create_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured["agent_kwargs"] = kwargs
        return "canonical-graph"

    monkeypatch.setattr(lead_agent_module, "frozen_checkpoint_channel_mode", lambda: None)
    monkeypatch.setattr(lead_agent_module, "freeze_checkpoint_channel_mode", lambda value: value)
    monkeypatch.setattr(lead_agent_module, "freeze_checkpoint_snapshot_frequency", lambda value: value)
    monkeypatch.setattr(lead_agent_module, "inject_checkpoint_mode", lambda *_args: None)
    monkeypatch.setattr(lead_agent_module, "build_tracing_callbacks", lambda: [])
    monkeypatch.setattr(lead_agent_module, "build_middlewares", build_middlewares)
    monkeypatch.setattr(lead_agent_module, "normalize_middleware_state_schemas", lambda value, *_args: value)

    def apply_prompt_template(**kwargs):  # type: ignore[no-untyped-def]
        captured.setdefault("prompt_calls", []).append(kwargs)
        return "canonical-prompt"

    monkeypatch.setattr(
        lead_agent_module,
        "apply_prompt_template",
        apply_prompt_template,
    )
    monkeypatch.setattr(lead_agent_module, "get_thread_state_schema", lambda *_args: dict)
    monkeypatch.setattr(
        lead_agent_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: object(),
    )
    monkeypatch.setattr(lead_agent_module, "create_agent", create_agent)
    monkeypatch.setattr("deerflow.tools.get_available_tools", available_tools)


def _provider_guard(captured: dict[str, object]) -> FinalProviderRequestGuard:
    middleware = captured["agent_kwargs"]["middleware"]  # type: ignore[index]
    return next(item for item in middleware if isinstance(item, FinalProviderRequestGuard))


def test_trusted_extension_adds_and_excludes_tools_and_uses_canonical_middleware_slots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _install_factory_spies(monkeypatch, captured=captured)
    extra_tool = _tool("builder_terminal_tool")
    terminal = _BuilderTerminalMiddleware()
    output_limit = _BuilderOutputLimitMiddleware()
    extension = TrustedLeadAgentExtension(
        extra_tools=(extra_tool,),
        excluded_tool_names=frozenset({"ask_clarification"}),
        custom_middlewares=(terminal,),
        output_limit_recovery_override=output_limit,
        system_prompt_override=BUILDER_SYSTEM_PROMPT,
    )

    graph = lead_agent_module._make_lead_agent(
        {"configurable": {"thinking_enabled": False}},
        app_config=_app_config(),
        private_runtime=_private_runtime(tmp_path),
        trusted_extension=extension,
    )

    assert graph == "canonical-graph"
    assert captured["available_tool_kwargs"]["groups"] == list(FULL_TOOL_GROUPS)  # type: ignore[index]
    assert [tool.name for tool in captured["agent_kwargs"]["tools"]] == [  # type: ignore[index]
        "baseline_tool",
        "builder_terminal_tool",
    ]
    middleware_kwargs = captured["middleware_kwargs"]
    assert middleware_kwargs["custom_middlewares"] == [terminal]  # type: ignore[index]
    assert middleware_kwargs["output_limit_recovery_override"] is output_limit  # type: ignore[index]
    assert captured["agent_kwargs"]["system_prompt"] == BUILDER_SYSTEM_PROMPT  # type: ignore[index]
    assert captured.get("prompt_calls") is None


def test_private_runtime_without_skills_does_not_require_a_materialized_skill_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _install_factory_spies(monkeypatch, captured=captured)
    private_runtime = _private_runtime(tmp_path)
    del private_runtime.skill_root

    graph = lead_agent_module._make_lead_agent(
        {"configurable": {"thinking_enabled": False}},
        app_config=_app_config(),
        private_runtime=private_runtime,
    )

    assert graph == "canonical-graph"
    middleware_kwargs = captured["middleware_kwargs"]
    assert middleware_kwargs["runtime_skills"] == ()  # type: ignore[index]
    assert middleware_kwargs["runtime_skills_root"] is None  # type: ignore[index]


@pytest.mark.parametrize(
    "middleware",
    (_AfterModelOnlyMiddleware(), _BoundedRequestShaperMiddleware()),
)
def test_trusted_extension_profile_allows_non_shaping_or_bounded_middleware(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    middleware: AgentMiddleware,
) -> None:
    captured: dict[str, object] = {}
    _install_factory_spies(monkeypatch, captured=captured)

    lead_agent_module._make_lead_agent(
        {"configurable": {"thinking_enabled": False}},
        app_config=_app_config(),
        private_runtime=_private_runtime(tmp_path),
        trusted_extension=TrustedLeadAgentExtension(
            custom_middlewares=(middleware,),
        ),
    )

    assert _provider_guard(captured).profile.supported is True
    assert _provider_guard(captured).profile.closure_identity == "closure-test"
    assert _provider_guard(captured).profile.runtime_policy_identity is not None
    assert _provider_guard(captured).profile.workload_profile == "interactive"


def test_trusted_extension_profile_fails_closed_for_unbounded_request_shaper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _install_factory_spies(monkeypatch, captured=captured)

    lead_agent_module._make_lead_agent(
        {"configurable": {"thinking_enabled": False}},
        app_config=_app_config(),
        private_runtime=_private_runtime(tmp_path),
        trusted_extension=TrustedLeadAgentExtension(
            custom_middlewares=(_UnboundedRequestShaperMiddleware(),),
        ),
    )

    profile = _provider_guard(captured).profile
    assert profile.supported is False
    assert profile.unsupported_reason == ("provider_request_usage_unsupported: custom request shaper has no bounded contract")


def test_runtime_config_cannot_construct_a_trusted_extension(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _install_factory_spies(monkeypatch, captured=captured)
    injected = TrustedLeadAgentExtension(
        extra_tools=(_tool("request_injected_tool"),),
        excluded_tool_names=frozenset({"baseline_tool"}),
    )

    graph = lead_agent_module._make_lead_agent(
        {
            "configurable": {"thinking_enabled": False},
            "context": {
                "trusted_extension": injected,
                "system_prompt_override": "forged Builder prompt",
            },
        },
        app_config=_app_config(),
        private_runtime=_private_runtime(tmp_path),
    )

    assert graph == "canonical-graph"
    assert [tool.name for tool in captured["agent_kwargs"]["tools"]] == [  # type: ignore[index]
        "baseline_tool",
        "ask_clarification",
    ]
    assert captured["agent_kwargs"]["system_prompt"] == "canonical-prompt"  # type: ignore[index]
    assert len(captured["prompt_calls"]) == 1  # type: ignore[arg-type]


def test_private_runtime_capability_notice_reaches_canonical_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _install_factory_spies(monkeypatch, captured=captured)
    private_runtime = _private_runtime(tmp_path)
    private_runtime.capability_notice = "<runtime_capability_status>safe</runtime_capability_status>"

    graph = lead_agent_module._make_lead_agent(
        {"configurable": {"thinking_enabled": False}},
        app_config=_app_config(),
        private_runtime=private_runtime,
    )

    assert graph == "canonical-graph"
    assert captured["prompt_calls"][0]["runtime_capability_notice"] == private_runtime.capability_notice  # type: ignore[index]


def test_private_lead_guard_receives_render_and_frozen_mcp_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _install_factory_spies(monkeypatch, captured=captured)
    prompt_text = "canonical-prompt"
    provenance = SystemPromptProvenance(
        system_prompt=prompt_text,
        spans=(
            SystemPromptLaneSpan(
                source_name="agent_definition",
                lane=ContextLane.AGENT_INSTRUCTIONS,
                start=0,
                end=len(prompt_text),
            ),
        ),
    )
    monkeypatch.setattr(
        lead_agent_module,
        "apply_prompt_template",
        lambda **_kwargs: LeadPromptText(
            prompt_text,
            context_provenance=provenance,
        ),
    )
    runtime = _private_runtime(tmp_path)
    mcp_tool = tag_mcp_tool(_tool("frozen_mcp_tool"))
    runtime.mcp_tools = (mcp_tool,)

    lead_agent_module._make_lead_agent(
        {"configurable": {"thinking_enabled": False}},
        app_config=_app_config(),
        private_runtime=runtime,
    )

    guard = _provider_guard(captured)
    measurement = guard.cost_adapter.measure_final_request(
        ModelRequest(
            model="test-model",
            messages=[HumanMessage(content="hello")],
            system_prompt=prompt_text,
            tools=captured["agent_kwargs"]["tools"],  # type: ignore[index]
            state={"messages": [HumanMessage(content="hello")]},
            runtime=Runtime(context={}),
        )
    )
    lanes = {item.lane for item in measurement.contributions}
    assert ContextLane.AGENT_INSTRUCTIONS in lanes
    assert ContextLane.MCP_DYNAMIC_TOOLS in lanes
    assert ContextLane.TOOL_DEFINITIONS in lanes


def test_private_runtime_builds_explicit_parent_execution_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _install_factory_spies(monkeypatch, captured=captured)
    private_runtime = _private_runtime(tmp_path)
    monkeypatch.setattr(
        "deerflow.tools.get_available_tools",
        lambda **_kwargs: [canonical_task_tool],
    )

    class _ContextEvidenceObserverFactory:
        async def record_request_prepared(self, _measurement):  # type: ignore[no-untyped-def]
            raise AssertionError("model execution is not part of this test")

        def create_subagent_observer(self, *_args: object) -> object:
            return object()

    context_evidence_observer = _ContextEvidenceObserverFactory()
    graph = lead_agent_module._make_lead_agent(
        # No private_scope marker: the explicit private_runtime argument, not
        # caller metadata, owns profile selection.
        {"configurable": {"thinking_enabled": False}},
        app_config=_app_config(),
        private_runtime=private_runtime,
        context_evidence_observer=context_evidence_observer,
    )

    assert graph == "canonical-graph"
    bound_task = captured["agent_kwargs"]["tools"][0]  # type: ignore[index]
    assert bound_task.name == "task"
    assert bound_task is not canonical_task_tool
    binding_factory = next(cell.cell_contents for cell in bound_task.coroutine.__closure__ or () if type(cell.cell_contents) is ParentExecutionBindingFactory)
    profile = binding_factory.profile
    assert type(profile) is PrivateRunParentExecutionProfile
    assert profile.kind == "private_run"
    assert profile.private_runtime is private_runtime
    assert profile.tool_groups == FULL_TOOL_GROUPS
    assert binding_factory.context_evidence_observer_factory is context_evidence_observer
