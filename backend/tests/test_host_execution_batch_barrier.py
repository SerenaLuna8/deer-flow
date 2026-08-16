"""Approval-mode barrier for model-emitted parallel tool batches."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from types import SimpleNamespace
from typing import ClassVar

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from langgraph.types import Command

from deerflow.agents.middlewares.assembly import (
    build_lead_runtime_middlewares,
    build_subagent_runtime_middlewares,
)
from deerflow.agents.middlewares.host_execution_batch_barrier_middleware import (
    HostExecutionApprovalPauseMiddleware,
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
        assert "HostExecutionApprovalPauseMiddleware" in names(
            _config(),
            subagent=subagent,
        )
        assert "HostExecutionBatchBarrierMiddleware" not in names(
            _config(mode="disabled"),
            subagent=subagent,
        )
        assert "HostExecutionApprovalPauseMiddleware" not in names(
            _config(mode="disabled"),
            subagent=subagent,
        )
        assert "HostExecutionBatchBarrierMiddleware" not in names(
            _config(mode="disabled", allow_host_bash=True),
            subagent=subagent,
        )
        assert "HostExecutionApprovalPauseMiddleware" not in names(
            _config(mode="disabled", allow_host_bash=True),
            subagent=subagent,
        )
        assert "HostExecutionBatchBarrierMiddleware" not in names(
            _config(
                use="deerflow.community.aio_sandbox:AioSandboxProvider",
            ),
            subagent=subagent,
        )
        assert "HostExecutionApprovalPauseMiddleware" not in names(
            _config(
                use="deerflow.community.aio_sandbox:AioSandboxProvider",
            ),
            subagent=subagent,
        )


class _ToolBindingFakeModel(GenericFakeChatModel):
    generate_calls: ClassVar[int] = 0

    def bind_tools(self, tools, **kwargs):  # type: ignore[no-untyped-def]
        del tools, kwargs
        return self

    def _generate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        type(self).generate_calls += 1
        return super()._generate(*args, **kwargs)


def _fake_model(responses: Iterable[AIMessage]) -> GenericFakeChatModel:
    _ToolBindingFakeModel.generate_calls = 0
    return _ToolBindingFakeModel(messages=iter(responses))


def _approval_message(*, source_run_id: str) -> ToolMessage:
    return ToolMessage(
        content="Host command execution requires approval.",
        tool_call_id="call-bash",
        name="bash",
        artifact={
            "host_execution_approval": {
                "schema_version": 1,
                "kind": "local_shell",
                "approval_id": "approval-1",
                "source_run_id": source_run_id,
                "source_tool_call_id": "call-bash",
            },
        },
    )


@pytest.mark.parametrize(
    ("source_run_id", "expected"),
    [("run-current", {"jump_to": "end"}), ("run-prior", None)],
)
def test_approval_pause_matches_only_the_current_run(
    source_run_id: str,
    expected: dict[str, str] | None,
) -> None:
    middleware = HostExecutionApprovalPauseMiddleware()

    assert (
        middleware.before_model(
            {"messages": [_approval_message(source_run_id=source_run_id)]},
            SimpleNamespace(context={"run_id": "run-current"}),
        )
        == expected
    )


@pytest.mark.anyio
async def test_approval_pause_checkpoints_tool_result_before_model_can_resume() -> None:
    run_id = "run-current"

    def contains_approval(value: object) -> bool:
        if isinstance(value, ToolMessage):
            return isinstance(value.artifact, Mapping) and isinstance(
                value.artifact.get("host_execution_approval"),
                Mapping,
            )
        if isinstance(value, Mapping):
            return any(contains_approval(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(contains_approval(item) for item in value)
        return False

    class DelayedApprovalSaver(InMemorySaver):
        def __init__(self) -> None:
            super().__init__()
            self.approval_write_started = asyncio.Event()
            self.release_approval_write = asyncio.Event()
            self.approval_write_persisted = False

        async def aput(
            self,
            config,
            checkpoint,
            metadata,
            new_versions,
        ):
            has_approval = contains_approval(checkpoint)
            if has_approval:
                self.approval_write_started.set()
                await self.release_approval_write.wait()
            result = await super().aput(
                config,
                checkpoint,
                metadata,
                new_versions,
            )
            if has_approval:
                self.approval_write_persisted = True
            return result

        async def aput_writes(
            self,
            config,
            writes,
            task_id,
            task_path="",
        ) -> None:
            has_approval = contains_approval(writes)
            if has_approval:
                self.approval_write_started.set()
                await self.release_approval_write.wait()
            await super().aput_writes(
                config,
                writes,
                task_id,
                task_path,
            )
            if has_approval:
                self.approval_write_persisted = True

    checkpointer = DelayedApprovalSaver()

    class CheckpointAssertingPauseMiddleware(HostExecutionApprovalPauseMiddleware):
        async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
            result = await super().abefore_model(state, runtime)
            if result is not None:
                assert checkpointer.approval_write_persisted
            return result

    @tool("bash")
    async def bash(value: str) -> Command:
        """Stage one host command approval."""
        del value
        return Command(
            update={"messages": [_approval_message(source_run_id=run_id)]},
            goto=END,
        )

    model = _fake_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "bash",
                        "args": {"value": "bash"},
                        "id": "call-bash",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="must not be generated"),
        ]
    )
    agent = create_agent(
        model=model,
        tools=[bash],
        middleware=[CheckpointAssertingPauseMiddleware()],
        context_schema=dict,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": "thread-approval-pause"}}

    async def consume_stream() -> None:
        async for _chunk in agent.astream(
            {"messages": [HumanMessage(content="go")]},
            config=config,
            context={"run_id": run_id},
            stream_mode="values",
            durability="sync",
        ):
            pass

    stream_task = asyncio.create_task(consume_stream())
    try:
        await asyncio.wait_for(
            checkpointer.approval_write_started.wait(),
            timeout=1,
        )
        assert _ToolBindingFakeModel.generate_calls == 1
        assert not stream_task.done()
    finally:
        checkpointer.release_approval_write.set()
    await stream_task

    state = await agent.aget_state(config)
    approval_messages = [message for message in state.values["messages"] if isinstance(message, ToolMessage) and isinstance(message.artifact, dict) and "host_execution_approval" in message.artifact]
    assert len(approval_messages) == 1
    assert _ToolBindingFakeModel.generate_calls == 1
    assert all(not isinstance(message, AIMessage) or message.content != "must not be generated" for message in state.values["messages"])


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
