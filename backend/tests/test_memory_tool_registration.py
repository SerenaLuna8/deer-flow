"""Production assembly contracts for lead-only Memory tools."""

from __future__ import annotations

import importlib
import uuid
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import deerflow.agents.lead_agent.agent as lead_agent_module
import deerflow.tools.tools as tools_module
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.tool_config import ToolConfig, ToolGroupConfig

task_tool_module = importlib.import_module("deerflow.tools.builtins.task_tool")

MODEL_NAME = "memory-tool-registration-model"


def _app_config(
    *,
    lead_supports_vision: bool = False,
    bridge: bool = False,
) -> AppConfig:
    models = [
        ModelConfig(
            name=MODEL_NAME,
            display_name="Memory tool registration",
            description="",
            use="langchain_openai:ChatOpenAI",
            model=MODEL_NAME,
            api_key=SecretStr("unit-test-key"),
            base_url="https://example.invalid/v1",
            supports_thinking=False,
            supports_vision=lead_supports_vision,
        )
    ]
    vision_bridge: dict[str, object] = {}
    if bridge:
        vision_model = ModelConfig(
            name="vision-small-v1",
            display_name="Vision fake",
            description="",
            use="deerflow.vision.fake_chat_model:FakeVisionBridgeChatModel",
            model="fake-vision",
            supports_vision=True,
        )
        vision_model._system_model_config_version_id = uuid.uuid4()
        vision_model._system_provider_adapter = "vision_bridge_fake"
        models.append(vision_model)
        vision_bridge = {"model_name": "vision-small-v1"}
    return AppConfig(
        models=models,
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        database={"url": "postgresql://localhost/memory-tool-registration"},
        skills={"deferred_discovery": False},
        tool_search={"enabled": False},
        tools=[],
        vision_bridge=vision_bridge,
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


def _assemble_lead_tool_names(
    monkeypatch,
    tmp_path,
    authority: object | None,
    *,
    app_config: AppConfig | None = None,
) -> set[str]:
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
        app_config=app_config or _app_config(),
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


def test_private_text_lead_registers_bridge_but_native_vision_does_not(
    monkeypatch,
    tmp_path,
) -> None:
    text_names = _assemble_lead_tool_names(
        monkeypatch,
        tmp_path,
        None,
        app_config=_app_config(bridge=True),
    )
    native_names = _assemble_lead_tool_names(
        monkeypatch,
        tmp_path,
        None,
        app_config=_app_config(
            lead_supports_vision=True,
            bridge=True,
        ),
    )

    assert "inspect_image" in text_names
    assert "view_image" not in text_names
    assert "inspect_image" not in native_names
    assert "view_image" in native_names


@pytest.mark.asyncio
async def test_subagent_never_inherits_parent_bridge_tool() -> None:
    tools = await task_tool_module._assemble_subagent_tools(
        parent_context={"private_scope": object()},
        runtime_agent_profile=None,
        effective_model=MODEL_NAME,
        effective_tool_groups=None,
        app_config=_app_config(bridge=True),
    )

    assert "inspect_image" not in {tool.name for tool in tools}


def test_subagent_production_tool_registry_has_no_memory_tools() -> None:
    registered_names = {tool.name for tool in (*tools_module.BUILTIN_TOOLS, *tools_module.SUBAGENT_TOOLS)}
    assert registered_names.isdisjoint({"recall_memory", "remember"})


@pytest.mark.parametrize("kind", ["tool", "group"])
def test_config_cannot_claim_reserved_inspect_image_name(kind: str) -> None:
    config = _app_config()
    if kind == "tool":
        config.tools = [
            ToolConfig(
                name="inspect_image",
                group="file:read",
                use="deerflow.sandbox.tools:read_file_tool",
            )
        ]
    else:
        config.tool_groups = [ToolGroupConfig(name="inspect_image")]

    with pytest.raises(ValueError, match="Reserved platform tool"):
        tools_module.get_available_tools(
            app_config=config,
            include_mcp=False,
        )
