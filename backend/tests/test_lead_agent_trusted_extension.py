from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import StructuredTool
from pydantic import SecretStr

import deerflow.agents.lead_agent.agent as lead_agent_module
from deerflow.agents.lead_agent.agent import TrustedLeadAgentExtension
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.subagents.binding import (
    ParentExecutionBindingFactory,
    PrivateRunParentExecutionProfile,
)
from deerflow.tools.builtins import task_tool as canonical_task_tool

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
        return list(kwargs.get("custom_middlewares") or ())

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
    graph = lead_agent_module._make_lead_agent(
        # No private_scope marker: the explicit private_runtime argument, not
        # caller metadata, owns profile selection.
        {"configurable": {"thinking_enabled": False}},
        app_config=_app_config(),
        private_runtime=private_runtime,
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
