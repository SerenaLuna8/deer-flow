"""Production assembly contracts for lead-only Memory tools."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import deerflow.agents.lead_agent.agent as lead_agent_module
import deerflow.tools.tools as tools_module
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig

task_tool_module = importlib.import_module("deerflow.tools.builtins.task_tool")

MODEL_NAME = "memory-tool-registration-model"


def _app_config() -> AppConfig:
    return AppConfig(
        models=[
            ModelConfig(
                name=MODEL_NAME,
                display_name="Memory tool registration",
                description="",
                use="langchain_openai:ChatOpenAI",
                model=MODEL_NAME,
                api_key=SecretStr("unit-test-key"),
                base_url="https://example.invalid/v1",
                supports_thinking=False,
                supports_vision=False,
            )
        ],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        database={"url": "postgresql://localhost/memory-tool-registration"},
        skills={"deferred_discovery": False},
        tool_search={"enabled": False},
        tools=[],
    )


def _private_runtime(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        model_ref=MODEL_NAME,
        model_settings=None,
        tool_groups=(),
        skills=(),
        safe_manifest=SimpleNamespace(skills=()),
        mcp_tools=(),
        skill_root=tmp_path,
        prompt_bundle=None,
        agent_catalog=None,
        soul="",
    )


class _SearchAuthority:
    async def search_episodes(self, **_kwargs):
        return ()


class _ProposeAuthority:
    async def propose_entry(self, **_kwargs):
        return None


class _FullAuthority(_SearchAuthority, _ProposeAuthority):
    pass


def _assemble_lead_tool_names(monkeypatch, tmp_path, authority: object | None) -> set[str]:
    captured: dict[str, object] = {}

    def capture_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(lead_agent_module, "frozen_checkpoint_channel_mode", lambda: None)
    monkeypatch.setattr(lead_agent_module, "freeze_checkpoint_channel_mode", lambda value: value)
    monkeypatch.setattr(lead_agent_module, "freeze_checkpoint_snapshot_frequency", lambda value: value)
    monkeypatch.setattr(lead_agent_module, "inject_checkpoint_mode", lambda *_args: None)
    monkeypatch.setattr(lead_agent_module, "build_tracing_callbacks", lambda: [])
    monkeypatch.setattr(lead_agent_module, "build_middlewares", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(lead_agent_module, "normalize_middleware_state_schemas", lambda value, *_args: value)
    monkeypatch.setattr(lead_agent_module, "apply_prompt_template", lambda **_kwargs: "prompt")
    monkeypatch.setattr(lead_agent_module, "get_thread_state_schema", lambda *_args: dict)
    monkeypatch.setattr(lead_agent_module, "create_chat_model", lambda **_kwargs: object())
    monkeypatch.setattr(lead_agent_module, "create_agent", capture_agent)

    context = {} if authority is None else {"__memory_authority": authority}
    lead_agent_module._make_lead_agent(
        {"configurable": {}, "context": context},
        app_config=_app_config(),
        private_runtime=_private_runtime(tmp_path),
    )
    return {tool.name for tool in captured["tools"]}


@pytest.mark.parametrize(
    ("authority", "expected"),
    [
        (None, set()),
        (_SearchAuthority(), {"recall_memory"}),
        (_ProposeAuthority(), {"remember"}),
        (_FullAuthority(), {"recall_memory", "remember"}),
    ],
)
def test_production_lead_assembly_registers_only_authorized_memory_tools(
    monkeypatch,
    tmp_path,
    authority: object | None,
    expected: set[str],
) -> None:
    names = _assemble_lead_tool_names(monkeypatch, tmp_path, authority)

    assert names & {"recall_memory", "remember"} == expected


@pytest.mark.asyncio
async def test_production_subagent_assembly_never_inherits_parent_memory_tools() -> None:
    parent_context = {
        "private_scope": object(),
        "__memory_authority": _FullAuthority(),
    }

    tools = await task_tool_module._assemble_subagent_tools(
        parent_context=parent_context,
        runtime_agent_profile=None,
        effective_model=MODEL_NAME,
        effective_tool_groups=None,
        app_config=_app_config(),
    )

    names = {tool.name for tool in tools}
    assert {"present_files", "ask_clarification", "list_uploaded_files"} <= names
    assert names.isdisjoint({"task", "recall_memory", "remember"})


def test_subagent_production_tool_registry_has_no_memory_tools() -> None:
    registered_names = {tool.name for tool in (*tools_module.BUILTIN_TOOLS, *tools_module.SUBAGENT_TOOLS)}
    assert registered_names.isdisjoint({"recall_memory", "remember"})
