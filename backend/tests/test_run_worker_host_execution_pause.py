from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from langgraph.types import Command

import deerflow.runtime.runs.worker as run_worker
from deerflow.agents.middlewares.host_execution_batch_barrier_middleware import (
    HostExecutionApprovalPauseMiddleware,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.runtime.runs.manager import RunManager
from deerflow.runtime.runs.schemas import RunStatus
from deerflow.runtime.runs.worker import RunContext, run_agent


def _bridge() -> SimpleNamespace:
    return SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )


def _config(
    *,
    use: str = "deerflow.sandbox.local:LocalSandboxProvider",
    mode: str = "approval_required",
) -> AppConfig:
    return AppConfig(
        sandbox=SandboxConfig(
            use=use,
            host_execution_approval={
                "mode": mode,
                "execution_domain_id": ("test-worker" if mode == "approval_required" else None),
            },
        ),
    )


def _approval_message(*, source_run_id: str, suffix: str) -> ToolMessage:
    return ToolMessage(
        content="Host command execution requires approval.",
        tool_call_id=f"tool-{suffix}",
        name="bash",
        artifact={
            "host_execution_approval": {
                "schema_version": 1,
                "kind": "local_shell",
                "approval_id": f"approval-{suffix}",
                "source_run_id": source_run_id,
                "source_tool_call_id": f"tool-{suffix}",
            },
        },
    )


@pytest.mark.anyio
async def test_initial_turn_approval_drains_graph_then_skips_goal_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-host-approval-initial")
    evaluator = AsyncMock(side_effect=AssertionError("goal evaluator must not run after approval"))
    monkeypatch.setattr(run_worker, "_prepare_goal_continuation_input", evaluator)
    stream_drained_after_approval = 0
    stream_invocations = 0

    class Agent:
        async def astream(
            self,
            graph_input: dict[str, Any],
            *,
            config: dict[str, Any] | None = None,
            stream_mode: list[str] | str | None = None,
            subgraphs: bool = False,
        ):
            nonlocal stream_drained_after_approval, stream_invocations
            del graph_input, config, stream_mode, subgraphs
            stream_invocations += 1
            # A prior Run's approval is replayed in values-mode history and must
            # not suspend this Run.
            yield {
                "messages": [
                    _approval_message(
                        source_run_id="prior-run",
                        suffix="prior",
                    ),
                ],
            }
            yield {
                "messages": [
                    _approval_message(
                        source_run_id=record.run_id,
                        suffix="current",
                    ),
                ],
            }
            stream_drained_after_approval += 1
            yield {"messages": []}

    await run_agent(
        _bridge(),
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    assert record.status is RunStatus.success
    assert stream_invocations == 1
    assert stream_drained_after_approval == 1
    assert record.suspended_approval_id == "approval-current"
    evaluator.assert_not_awaited()


@pytest.mark.anyio
async def test_source_output_delivery_is_deferred_at_exact_approval_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: an unpresented output must not turn a valid pause into error."""

    run_manager = RunManager()
    record = await run_manager.create("thread-host-approval-output-deferred")
    monkeypatch.setattr(
        run_worker,
        "_prepare_goal_continuation_input",
        AsyncMock(
            side_effect=AssertionError(
                "goal evaluator must not run after approval",
            ),
        ),
    )

    class Agent:
        async def astream(self, *args: Any, **kwargs: Any):
            del args, kwargs
            yield {
                "messages": [
                    _approval_message(
                        source_run_id=record.run_id,
                        suffix="deferred-output",
                    ),
                ],
            }

    class FileAuthority:
        async def restore(self) -> object:
            return object()

        async def finalize(self) -> object:
            return SimpleNamespace(
                workspace_changes={
                    "created": ["outputs/bubble_sort.py"],
                    "modified": [],
                    "deleted": [],
                },
                artifacts=(),
            )

        async def output_delivery_status(self) -> str:
            return "not_required"

        async def mark_failed(self) -> None:
            pass

        async def release(self) -> None:
            pass

    suspension_port = SimpleNamespace(
        seal_suspended_approval_marker=AsyncMock(),
    )
    await run_agent(
        _bridge(),
        run_manager,
        record,
        ctx=RunContext(
            app_config=_config(),
            checkpointer=None,
            file_authority=FileAuthority(),
            host_execution_approval_port=suspension_port,
        ),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={},
        config={},
    )

    assert record.status is RunStatus.success
    assert record.error is None
    assert record.suspended_approval_id == "approval-deferred-output"
    suspension_port.seal_suspended_approval_marker.assert_awaited_once_with(
        "approval-deferred-output",
    )


@pytest.mark.anyio
async def test_goal_continuation_approval_drains_graph_then_skips_next_hidden_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-host-approval-goal-continuation")
    evaluator = AsyncMock(
        side_effect=[
            {"messages": [{"role": "user", "content": "hidden continuation"}]},
            AssertionError("goal evaluator must not rerun after approval"),
        ],
    )
    monkeypatch.setattr(run_worker, "_prepare_goal_continuation_input", evaluator)
    stream_inputs: list[dict[str, Any]] = []
    stream_drained_after_approval = 0

    class Agent:
        async def astream(
            self,
            graph_input: dict[str, Any],
            *,
            config: dict[str, Any] | None = None,
            stream_mode: list[str] | str | None = None,
            subgraphs: bool = False,
        ):
            nonlocal stream_drained_after_approval
            del config, stream_mode, subgraphs
            stream_inputs.append(graph_input)
            if len(stream_inputs) == 1:
                yield {"messages": []}
                return
            if len(stream_inputs) == 2:
                yield {
                    "messages": [
                        _approval_message(
                            source_run_id=record.run_id,
                            suffix="continuation",
                        ),
                    ],
                }
                stream_drained_after_approval += 1
                yield {"messages": []}
                return
            raise AssertionError("agent must not receive another hidden turn")

    await run_agent(
        _bridge(),
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={"messages": []},
        config={},
    )

    assert record.status is RunStatus.success
    assert len(stream_inputs) == 2
    assert stream_drained_after_approval == 1
    assert record.suspended_approval_id == "approval-continuation"
    assert evaluator.await_count == 1


@pytest.mark.parametrize(
    ("app_config", "expected_durability"),
    [
        (_config(), "sync"),
        (_config(mode="disabled"), None),
        (
            _config(
                use="deerflow.community.aio_sandbox:AioSandboxProvider",
            ),
            None,
        ),
    ],
)
@pytest.mark.parametrize(
    ("stream_modes", "stream_subgraphs"),
    [(["values"], False), (["values", "custom"], False)],
)
@pytest.mark.anyio
async def test_sync_durability_is_scoped_to_local_approval_runs(
    monkeypatch: pytest.MonkeyPatch,
    app_config: AppConfig,
    expected_durability: str | None,
    stream_modes: list[str],
    stream_subgraphs: bool,
) -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-host-approval-durability")
    monkeypatch.setattr(
        run_worker,
        "_prepare_goal_continuation_input",
        AsyncMock(return_value=None),
    )
    stream_kwargs: list[dict[str, Any]] = []

    class Agent:
        async def astream(self, *args: Any, **kwargs: Any):
            del args
            stream_kwargs.append(kwargs)
            if False:
                yield None

    await run_agent(
        _bridge(),
        run_manager,
        record,
        ctx=RunContext(
            app_config=app_config,
            checkpointer=None,
            host_execution_approval_port=object(),
        ),
        agent_factory=lambda **_kwargs: Agent(),
        graph_input={"messages": []},
        config={},
        stream_modes=stream_modes,
        stream_subgraphs=stream_subgraphs,
    )

    assert record.status is RunStatus.success
    assert len(stream_kwargs) == 1
    if expected_durability is None:
        assert "durability" not in stream_kwargs[0]
    else:
        assert stream_kwargs[0]["durability"] == expected_durability


class _ToolBindingFakeModel(GenericFakeChatModel):
    generate_calls: ClassVar[int] = 0

    def bind_tools(
        self,
        tools: Sequence[Any],
        **kwargs: Any,
    ) -> GenericFakeChatModel:
        del tools, kwargs
        return self

    def _generate(self, *args: Any, **kwargs: Any):
        type(self).generate_calls += 1
        return super()._generate(*args, **kwargs)


@pytest.mark.anyio
async def test_custom_only_stream_uses_checkpoint_artifact_to_skip_goal_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-host-approval-custom-only")
    evaluator = AsyncMock(side_effect=AssertionError("goal evaluator must not run after approval"))
    monkeypatch.setattr(run_worker, "_prepare_goal_continuation_input", evaluator)

    @tool("bash")
    async def bash(value: str) -> Command:
        """Stage one host command approval."""
        del value
        return Command(
            update={
                "messages": [
                    _approval_message(
                        source_run_id=record.run_id,
                        suffix="custom",
                    )
                ]
            },
            goto=END,
        )

    _ToolBindingFakeModel.generate_calls = 0
    model = _ToolBindingFakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "bash",
                            "args": {"value": "bash"},
                            "id": "tool-custom",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="must not be generated"),
            ]
        )
    )
    checkpointer = InMemorySaver()
    agent = create_agent(
        model=model,
        tools=[bash],
        middleware=[HostExecutionApprovalPauseMiddleware()],
        context_schema=dict,
        checkpointer=checkpointer,
    )
    terminal_order: list[str] = []
    suspension_port = SimpleNamespace(
        seal_suspended_approval_marker=AsyncMock(
            side_effect=lambda _approval_id: terminal_order.append("marker"),
        ),
    )
    bridge = _bridge()
    bridge.publish_end = AsyncMock(
        side_effect=lambda _run_id: terminal_order.append("terminal"),
    )

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(
            app_config=_config(),
            checkpointer=checkpointer,
            host_execution_approval_port=suspension_port,
        ),
        agent_factory=lambda **_kwargs: agent,
        graph_input={"messages": [HumanMessage(content="go")]},
        config={},
        stream_modes=["custom"],
    )

    assert record.status is RunStatus.success
    assert _ToolBindingFakeModel.generate_calls == 1
    assert record.suspended_approval_id == "approval-custom"
    assert terminal_order == ["marker", "terminal"]
    suspension_port.seal_suspended_approval_marker.assert_awaited_once_with(
        "approval-custom",
    )
    evaluator.assert_not_awaited()
