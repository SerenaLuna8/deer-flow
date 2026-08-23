from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.subagents.binding import (
    AgentGraphExecutionInputs,
    ParentExecutionBindingFactory,
    PrivateRunParentExecutionProfile,
)
from deerflow.subagents.config import SubagentConfig

task_module = importlib.import_module("deerflow.tools.builtins.task_tool")


def _private_profile() -> PrivateRunParentExecutionProfile:
    return PrivateRunParentExecutionProfile(
        graph=AgentGraphExecutionInputs(
            model=object(),
            tools=(),
            middleware=(),
            system_prompt=None,
            state_schema=dict,
        ),
        app_config=SimpleNamespace(),
        asset_context=None,
        private_runtime=SimpleNamespace(mcp_tools=()),
        model_name="private-model",
        thinking_enabled=False,
        reasoning_effort=None,
        runtime_skills=(),
        runtime_agent_catalog=None,
        tool_groups=(),
    )


@pytest.mark.asyncio
async def test_private_subagent_tool_assembly_filters_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _private_profile()
    parent_binding = SimpleNamespace(profile=profile)
    bash = SimpleNamespace(name="bash")
    read_file = SimpleNamespace(name="read_file")

    tools_module = importlib.import_module("deerflow.tools")
    monkeypatch.setattr(
        tools_module,
        "get_available_tools",
        lambda **_kwargs: [bash, read_file],
    )

    tools = await task_module._assemble_subagent_tools(
        parent_binding=parent_binding,
        parent_context={},
        runtime_agent_profile=SimpleNamespace(mcp_tools=()),
        effective_model="private-model",
        effective_tool_groups=(),
        app_config=profile.app_config,
    )

    assert [tool.name for tool in tools] == ["read_file"]


@pytest.mark.asyncio
async def test_private_bash_subagent_fails_closed_before_lifecycle_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = ParentExecutionBindingFactory(_private_profile())
    runtime = SimpleNamespace(
        state={},
        context={
            RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: factory,
            RuntimeContextKeys.FILE_AUTHORITY: SimpleNamespace(
                delegated_output_scope=lambda _task_id: None,
            ),
        },
        config={"metadata": {}, "callbacks": [], "configurable": {}},
        store=None,
    )
    monkeypatch.setattr(
        task_module,
        "get_available_subagent_names",
        lambda **_kwargs: ["bash"],
    )
    monkeypatch.setattr(
        task_module,
        "get_subagent_config",
        lambda *_args, **_kwargs: SubagentConfig(
            name="bash",
            description="shell delegate",
            model="inherit",
            timeout_seconds=2,
        ),
    )

    class LifecycleMustNotRun:
        async def run(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("private bash must fail before lifecycle admission")

    monkeypatch.setattr(
        task_module,
        "subagent_task_lifecycle",
        LifecycleMustNotRun(),
    )

    command = await task_module.task_tool.coroutine(
        runtime=runtime,
        description="run shell",
        prompt="execute a private shell command",
        subagent_type="bash",
        tool_call_id="task-bash-1",
    )

    message = command.update["messages"][0]
    assert isinstance(message, ToolMessage)
    assert message.additional_kwargs["subagent_status"] == "failed"
    assert "per-Task filesystem namespace" in message.content


@pytest.mark.asyncio
async def test_private_task_without_file_authority_fails_before_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = ParentExecutionBindingFactory(_private_profile())
    runtime = SimpleNamespace(
        state={},
        context={
            RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: factory,
        },
        config={"metadata": {}, "callbacks": [], "configurable": {}},
        store=None,
    )
    monkeypatch.setattr(
        task_module,
        "get_available_subagent_names",
        lambda **_kwargs: ["general-purpose"],
    )
    monkeypatch.setattr(
        task_module,
        "get_subagent_config",
        lambda *_args, **_kwargs: SubagentConfig(
            name="general-purpose",
            description="isolated delegate",
            model="inherit",
            timeout_seconds=2,
        ),
    )

    class LifecycleMustNotRun:
        async def run(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError(
                "private task without file isolation must fail before admission",
            )

    monkeypatch.setattr(
        task_module,
        "subagent_task_lifecycle",
        LifecycleMustNotRun(),
    )

    command = await task_module.task_tool.coroutine(
        runtime=runtime,
        description="isolated task",
        prompt="perform delegated work",
        subagent_type="general-purpose",
        tool_call_id="task-no-authority-1",
    )

    message = command.update["messages"][0]
    assert isinstance(message, ToolMessage)
    assert message.additional_kwargs["subagent_status"] == "failed"
