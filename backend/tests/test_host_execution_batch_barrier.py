"""Approval-mode barrier for model-emitted parallel tool batches."""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from deerflow.agents.middlewares.assembly import (
    build_lead_runtime_middlewares,
    build_subagent_runtime_middlewares,
)
from deerflow.agents.middlewares.host_execution_batch_barrier_middleware import (
    HostExecutionBatchBarrierMiddleware,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig


def _config(
    *,
    use: str = "deerflow.sandbox.local:LocalSandboxProvider",
    mode: str = "approval_required",
    allow_host_bash: bool = False,
) -> AppConfig:
    return AppConfig(
        sandbox=SandboxConfig(
            use=use,
            allow_host_bash=allow_host_bash,
            host_execution_approval={
                "mode": mode,
                "execution_domain_id": ("test-worker" if mode == "approval_required" else None),
            },
        ),
    )


def _message(names: list[str]) -> AIMessage:
    calls = [
        {
            "name": name,
            "args": {"value": name},
            "id": f"call-{index}",
            "type": "tool_call",
        }
        for index, name in enumerate(names)
    ]
    raw_calls = [
        {
            "id": call["id"],
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": "{}",
            },
        }
        for call in calls
    ]
    return AIMessage(
        content="",
        tool_calls=calls,
        additional_kwargs={"tool_calls": raw_calls},
    )


@pytest.mark.parametrize(
    ("names", "kept"),
    [
        (["write_file", "bash"], "write_file"),
        (["bash", "write_file"], "bash"),
        (["read_file", "task", "write_file"], "read_file"),
        (["task", "task"], "task"),
    ],
)
def test_local_approval_batch_keeps_only_original_first_call(
    names: list[str],
    kept: str,
) -> None:
    middleware = HostExecutionBatchBarrierMiddleware()
    message = _message(names)

    result = middleware.after_model(
        {"messages": [message]},
        SimpleNamespace(),
    )

    assert result is not None
    updated = result["messages"][0]
    assert [call["name"] for call in updated.tool_calls] == [kept]
    assert [call["function"]["name"] for call in updated.additional_kwargs["tool_calls"]] == [kept]


def test_batch_without_bash_or_task_is_unchanged() -> None:
    middleware = HostExecutionBatchBarrierMiddleware()

    assert (
        middleware.after_model(
            {"messages": [_message(["read_file", "write_file"])]},
            SimpleNamespace(),
        )
        is None
    )


def test_runtime_assembly_adds_barrier_only_for_local_approval_mode() -> None:
    def names(config: AppConfig, *, subagent: bool) -> list[str]:
        middlewares = build_subagent_runtime_middlewares(app_config=config) if subagent else build_lead_runtime_middlewares(app_config=config)
        return [type(middleware).__name__ for middleware in middlewares]

    for subagent in (False, True):
        assert "HostExecutionBatchBarrierMiddleware" in names(
            _config(),
            subagent=subagent,
        )
        assert "HostExecutionBatchBarrierMiddleware" not in names(
            _config(mode="disabled"),
            subagent=subagent,
        )
        assert "HostExecutionBatchBarrierMiddleware" not in names(
            _config(mode="disabled", allow_host_bash=True),
            subagent=subagent,
        )
        assert "HostExecutionBatchBarrierMiddleware" not in names(
            _config(
                use="deerflow.community.aio_sandbox:AioSandboxProvider",
            ),
            subagent=subagent,
        )


class _ToolBindingFakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):  # type: ignore[no-untyped-def]
        del tools, kwargs
        return self


def _fake_model(responses: Iterable[AIMessage]) -> GenericFakeChatModel:
    return _ToolBindingFakeModel(messages=iter(responses))


@pytest.mark.parametrize(
    ("batch", "expected_started"),
    [
        (["write_file", "bash"], ["write_file"]),
        (["bash", "write_file"], ["bash"]),
        (["read_file", "task"], ["read_file"]),
    ],
)
def test_sibling_tool_handlers_never_start_before_replanning(
    batch: list[str],
    expected_started: list[str],
) -> None:
    started: list[str] = []

    @tool("write_file")
    def write_file(value: str) -> str:
        """Record a write."""
        started.append("write_file")
        return value

    @tool("bash")
    def bash(value: str) -> str:
        """Record a command."""
        started.append("bash")
        return value

    @tool("read_file")
    def read_file(value: str) -> str:
        """Record a read."""
        started.append("read_file")
        return value

    @tool("task")
    def task(value: str) -> str:
        """Record a delegation."""
        started.append("task")
        return value

    agent = create_agent(
        model=_fake_model([_message(batch), AIMessage(content="done")]),
        tools=[write_file, bash, read_file, task],
        middleware=[HostExecutionBatchBarrierMiddleware()],
    )

    result = agent.invoke({"messages": [HumanMessage(content="go")]})

    assert result["messages"][-1].content == "done"
    assert started == expected_started
