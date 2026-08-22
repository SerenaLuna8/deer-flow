"""Acceptance contract for the ``task`` tool's lifecycle Adapter.

These tests deliberately replace the process lifecycle with a small owner-loop
fake.  They pin the Adapter boundary without reaching through the lifecycle to
the old executor registry/future APIs.
"""

from __future__ import annotations

import asyncio
import importlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from deerflow.config.agents_config import AgentModelSettings
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.skills.types import Skill, SkillCategory
from deerflow.subagents.binding import (
    AgentGraphExecutionInputs,
    ConfiguredLeadParentExecutionProfile,
    ParentExecutionBindingFactory,
    PrivateRunParentExecutionProfile,
    SdkParentExecutionProfile,
)
from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.delegated_context import DelegatedRuntimeContextProjection
from deerflow.subagents.lifecycle import (
    SubagentCancellationCode,
    SubagentCancelled,
    SubagentCompleted,
    SubagentFailed,
    SubagentFailureCode,
    SubagentQuiescencePolicy,
    SubagentTaskSnapshot,
    SubagentTaskStatus,
    SubagentTimedOut,
    SubagentTimeoutPhase,
    SubagentTokenUsage,
    SubagentUsageCompleteness,
    SubagentUsageSettlement,
)
from deerflow.subagents.runtime_catalog import (
    build_runtime_agent_catalog,
    build_runtime_agent_profile,
)

task_module = importlib.import_module("deerflow.tools.builtins.task_tool")


def _binding_factory(*, model_name: str = "parent-model") -> ParentExecutionBindingFactory:
    app_config = SimpleNamespace()
    return ParentExecutionBindingFactory(
        ConfiguredLeadParentExecutionProfile(
            graph=AgentGraphExecutionInputs(
                model=object(),
                tools=(),
                middleware=(),
                system_prompt=None,
                state_schema=dict,
            ),
            app_config=app_config,
            asset_context=None,
            agent_config=None,
            model_name=model_name,
            thinking_enabled=False,
            reasoning_effort=None,
            plan_mode=False,
            subagent_enabled=True,
            agent_name="lead",
            available_skills=None,
        )
    )


def _runtime(
    factory: object | None,
    *,
    callbacks: list[object] | None = None,
    context_updates: dict[str, object] | None = None,
) -> SimpleNamespace:
    context: dict[str, object] = {}
    if factory is not None:
        context[RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY] = factory
    context.update(context_updates or {})
    return SimpleNamespace(
        state={},
        context=context,
        config={
            "metadata": {"model_name": "caller-metadata-model"},
            "callbacks": callbacks or [],
            "configurable": {},
        },
        store=None,
    )


def _tool_message(command: Command) -> ToolMessage:
    assert isinstance(command, Command)
    messages = command.update["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    message = messages[0]
    assert isinstance(message, ToolMessage)
    return message


def _skill(name: str) -> Skill:
    skill_dir = Path("/runtime-skills") / name
    return Skill(
        name=name,
        description=name,
        license=None,
        skill_dir=skill_dir,
        skill_file=skill_dir / "SKILL.md",
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        enabled=True,
        runtime_read_only=True,
    )


@pytest.mark.asyncio
async def test_task_adapter_passes_one_delegated_context_projection_to_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    now = datetime.now(UTC)

    class Lifecycle:
        async def run(self, call, binding, *, observers=()):  # type: ignore[no-untyped-def]
            runner = await binding.runner_factory()
            assert runner.trace_id == "runner-trace"
            return SubagentCompleted(
                execution_id=uuid.uuid4(),
                task_id=call.task_id,
                trace_id="runner-trace",
                queued_at=now,
                started_at=now,
                completed_at=now,
                ai_messages=(),
                usage=None,
                usage_completeness=SubagentUsageCompleteness.FINAL_OBSERVED,
                quiescent=True,
                result="done",
                stop_reason=None,
            )

    def build_runner(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return SimpleNamespace(trace_id="runner-trace")

    monkeypatch.setattr(task_module, "subagent_task_lifecycle", Lifecycle())
    monkeypatch.setattr(task_module, "get_stream_writer", lambda: lambda _event: None)
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
            description="projection probe",
            model="inherit",
            timeout_seconds=2,
        ),
    )
    monkeypatch.setattr(
        task_module,
        "_assemble_subagent_tools",
        lambda **_kwargs: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(task_module, "_new_subagent_graph_runner", build_runner)

    command = await task_module.task_tool.coroutine(
        runtime=_runtime(
            _binding_factory(),
            context_updates={
                RuntimeContextKeys.THREAD_ID: "thread-1",
                RuntimeContextKeys.RUN_ID: "run-1",
                RuntimeContextKeys.USER_ID: "user-1",
                RuntimeContextKeys.CHANNEL_USER_ID: None,
            },
        ),
        description="projection probe",
        prompt="capture one projection",
        subagent_type="general-purpose",
        tool_call_id="call-projection",
    )

    assert _tool_message(command).additional_kwargs["subagent_status"] == "completed"
    projection = captured["delegated_context"]
    assert type(projection) is DelegatedRuntimeContextProjection
    assert projection.build()[RuntimeContextKeys.IS_SUBAGENT] is True
    assert RuntimeContextKeys.CHANNEL_USER_ID in projection.build()
    assert projection.build()[RuntimeContextKeys.CHANNEL_USER_ID] is None
    assert {
        "app_config",
        "thread_id",
        "user_id",
        "user_role",
        "oauth_provider",
        "oauth_id",
        "run_id",
        "guardrail_attribution",
        "private_scope",
        "file_authority",
        "authorization_boundary",
        "authorization_checker",
        "run_read_only_mounts",
        "channel_user_id",
        "channel_identity_present",
        "deerflow_trace_id",
        "runtime_skills",
        "agent_prompt_bundle",
        "skill_scoped_secrets",
        "skill_secret_provider",
        "host_execution_approval_port",
        "host_execution_agent_path",
    }.isdisjoint(captured)


@pytest.mark.asyncio
@pytest.mark.parametrize("forged_factory", [None, object()])
async def test_missing_or_forged_graph_binding_fails_before_start(
    monkeypatch: pytest.MonkeyPatch,
    forged_factory: object | None,
) -> None:
    events: list[dict[str, object]] = []
    lifecycle_run = MagicMock(side_effect=AssertionError("must not admit"))
    monkeypatch.setattr(task_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(
        task_module,
        "subagent_task_lifecycle",
        SimpleNamespace(run=lifecycle_run),
        raising=False,
    )

    command = await task_module.task_tool.coroutine(
        runtime=_runtime(forged_factory),
        description="reject binding",
        prompt="do not run",
        subagent_type="general-purpose",
        tool_call_id="reused-tool-call",
    )

    message = _tool_message(command)
    assert message.additional_kwargs["subagent_status"] == "failed"
    assert message.additional_kwargs["subagent_error"] == "SUBAGENT_EXECUTION_FAILED"
    assert events == []
    lifecycle_run.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_agent_is_preflight_rejected_without_materialization_or_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, object]] = []
    lifecycle_run = MagicMock(side_effect=AssertionError("must not admit"))
    materialize = MagicMock(side_effect=AssertionError("must not materialize"))
    monkeypatch.setattr(task_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_module, "get_available_subagent_names", lambda **_kwargs: ["general-purpose"])
    monkeypatch.setattr(task_module, "get_subagent_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task_module, "_assemble_subagent_tools", materialize)
    monkeypatch.setattr(
        task_module,
        "subagent_task_lifecycle",
        SimpleNamespace(run=lifecycle_run),
        raising=False,
    )

    command = await task_module.task_tool.coroutine(
        runtime=_runtime(_binding_factory()),
        description="reject unknown",
        prompt="do not run",
        subagent_type="invented-agent",
        tool_call_id="call-preflight",
    )

    message = _tool_message(command)
    assert message.additional_kwargs["subagent_status"] == "failed"
    assert "Unknown subagent type 'invented-agent'" in str(message.content)
    assert events == []
    lifecycle_run.assert_not_called()
    materialize.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_bash_is_preflight_rejected_before_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, object]] = []
    lifecycle_run = MagicMock(side_effect=AssertionError("must not admit"))
    materialize = MagicMock(side_effect=AssertionError("must not materialize"))
    monkeypatch.setattr(task_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(
        task_module,
        "get_available_subagent_names",
        lambda **_kwargs: ["general-purpose", "bash"],
    )
    monkeypatch.setattr(
        task_module,
        "get_subagent_config",
        lambda *_args, **_kwargs: SubagentConfig(
            name="bash",
            description="bash",
            model="inherit",
        ),
    )
    monkeypatch.setattr(task_module, "is_host_bash_available", lambda _config: False)
    monkeypatch.setattr(task_module, "_assemble_subagent_tools", materialize)
    monkeypatch.setattr(
        task_module,
        "subagent_task_lifecycle",
        SimpleNamespace(run=lifecycle_run),
    )

    command = await task_module.task_tool.coroutine(
        runtime=_runtime(_binding_factory()),
        description="reject bash",
        prompt="do not run",
        subagent_type="bash",
        tool_call_id="call-bash-preflight",
    )

    assert _tool_message(command).additional_kwargs["subagent_status"] == "failed"
    assert events == []
    lifecycle_run.assert_not_called()
    materialize.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "context_updates", "expected_policy"),
    [
        (
            ParentExecutionBindingFactory(
                SdkParentExecutionProfile(
                    graph=AgentGraphExecutionInputs(
                        model=object(),
                        tools=(),
                        middleware=(),
                        system_prompt=None,
                        state_schema=dict,
                    ),
                    features=None,
                    full_middleware_takeover=False,
                    plan_mode=False,
                    checkpoint_channel_mode="full",
                    checkpoint_snapshot_frequency=None,
                )
            ),
            {RuntimeContextKeys.PRIVATE_SCOPE: object()},
            SubagentQuiescencePolicy.BOUNDED_WITH_REAPER,
        ),
        (
            ParentExecutionBindingFactory(
                PrivateRunParentExecutionProfile(
                    graph=AgentGraphExecutionInputs(
                        model=object(),
                        tools=(),
                        middleware=(),
                        system_prompt=None,
                        state_schema=dict,
                    ),
                    app_config=SimpleNamespace(),
                    asset_context=None,
                    private_runtime=object(),
                    model_name="private-model",
                    thinking_enabled=False,
                    reasoning_effort=None,
                    runtime_skills=(),
                    runtime_agent_catalog=None,
                    tool_groups=(),
                )
            ),
            {},
            SubagentQuiescencePolicy.REQUIRED_BEFORE_RETURN,
        ),
    ],
)
async def test_quiescence_policy_comes_only_from_explicit_graph_profile(
    monkeypatch: pytest.MonkeyPatch,
    factory: ParentExecutionBindingFactory,
    context_updates: dict[str, object],
    expected_policy: SubagentQuiescencePolicy,
) -> None:
    execution_id = uuid.uuid4()
    now = datetime.now(UTC)

    class Lifecycle:
        async def run(self, call, binding, *, observers=()):  # type: ignore[no-untyped-def]
            assert binding.quiescence_policy is expected_policy
            outcome = SubagentCompleted(
                execution_id=execution_id,
                task_id=call.task_id,
                trace_id="trace",
                queued_at=now,
                started_at=now,
                completed_at=now,
                ai_messages=(),
                usage=None,
                usage_completeness=SubagentUsageCompleteness.FINAL_OBSERVED,
                quiescent=True,
                result="done",
                stop_reason=None,
            )
            for observer in observers:
                await observer(outcome)
            return outcome

    monkeypatch.setattr(task_module, "subagent_task_lifecycle", Lifecycle())
    monkeypatch.setattr(task_module, "get_stream_writer", lambda: lambda _event: None)
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
            description="probe",
            model="inherit",
            timeout_seconds=2,
        ),
    )

    command = await task_module.task_tool.coroutine(
        runtime=_runtime(factory, context_updates=context_updates),
        description="profile policy",
        prompt="finish",
        subagent_type="general-purpose",
        tool_call_id="call-profile",
    )

    assert _tool_message(command).additional_kwargs["subagent_status"] == "completed"


@pytest.mark.asyncio
async def test_private_runtime_agent_keeps_its_own_builtin_tool_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead_tool = StructuredTool.from_function(
        lambda: "lead",
        name="lead_only",
        description="Lead-only tool",
    )
    delegate_tool = StructuredTool.from_function(
        lambda: "delegate",
        name="delegate_only",
        description="Delegate-only tool",
    )
    profile = PrivateRunParentExecutionProfile(
        graph=AgentGraphExecutionInputs(
            model=object(),
            tools=(lead_tool,),
            middleware=(),
            system_prompt=None,
            state_schema=dict,
        ),
        app_config=SimpleNamespace(),
        asset_context=None,
        private_runtime=object(),
        model_name="lead-model",
        thinking_enabled=False,
        reasoning_effort=None,
        runtime_skills=(),
        runtime_agent_catalog=None,
        tool_groups=("lead",),
    )
    parent_binding = SimpleNamespace(profile=profile)
    runtime_agent_profile = SimpleNamespace(mcp_tools=())

    tools_module = importlib.import_module("deerflow.tools")
    observed_groups: list[tuple[str, ...] | None] = []

    def available_tools(**kwargs):  # type: ignore[no-untyped-def]
        observed_groups.append(kwargs.get("groups"))
        return [delegate_tool]

    monkeypatch.setattr(tools_module, "get_available_tools", available_tools)

    tools = await task_module._assemble_subagent_tools(
        parent_binding=parent_binding,
        parent_context={},
        runtime_agent_profile=runtime_agent_profile,
        effective_model="delegate-model",
        effective_tool_groups=("delegate",),
        app_config=profile.app_config,
    )

    assert observed_groups == [("delegate",)]
    assert [tool.name for tool in tools] == ["delegate_only"]


@pytest.mark.asyncio
async def test_private_runtime_agent_keeps_its_own_skill_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead_skill = _skill("lead-skill")
    delegate_skill = _skill("delegate-skill")
    runtime_agent = build_runtime_agent_profile(
        key="project/delegate",
        description="Delegate",
        model_name="delegate-model",
        model_settings=AgentModelSettings(),
        tool_groups=("delegate",),
        prompt_bundle=object(),
        runtime_skills=(delegate_skill,),
        mcp_tools=(),
    )
    profile = PrivateRunParentExecutionProfile(
        graph=AgentGraphExecutionInputs(
            model=object(),
            tools=(),
            middleware=(),
            system_prompt=None,
            state_schema=dict,
        ),
        app_config=SimpleNamespace(),
        asset_context=None,
        private_runtime=object(),
        model_name="lead-model",
        thinking_enabled=False,
        reasoning_effort=None,
        runtime_skills=(lead_skill,),
        runtime_agent_catalog=build_runtime_agent_catalog((runtime_agent,)),
        tool_groups=("lead",),
    )
    factory = ParentExecutionBindingFactory(profile)
    captured: dict[str, object] = {}
    now = datetime.now(UTC)

    class Lifecycle:
        async def run(self, call, binding, *, observers=()):  # type: ignore[no-untyped-def]
            await binding.runner_factory()
            return SubagentCompleted(
                execution_id=uuid.uuid4(),
                task_id=call.task_id,
                trace_id="trace",
                queued_at=now,
                started_at=now,
                completed_at=now,
                ai_messages=(),
                usage=None,
                usage_completeness=SubagentUsageCompleteness.FINAL_OBSERVED,
                quiescent=True,
                result="done",
                stop_reason=None,
            )

    def build_runner(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return SimpleNamespace(trace_id="trace")

    monkeypatch.setattr(task_module, "subagent_task_lifecycle", Lifecycle())
    monkeypatch.setattr(task_module, "get_stream_writer", lambda: lambda _event: None)
    monkeypatch.setattr(task_module, "get_available_subagent_names", lambda **_kwargs: [])
    monkeypatch.setattr(task_module, "_assemble_subagent_tools", lambda **_kwargs: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(task_module, "_new_subagent_graph_runner", build_runner)

    command = await task_module.task_tool.coroutine(
        runtime=_runtime(factory),
        description="profile skills",
        prompt="use delegate skill",
        subagent_type="project/delegate",
        tool_call_id="call-profile-skills",
    )

    assert _tool_message(command).additional_kwargs["subagent_status"] == "completed"
    projection = captured["delegated_context"]
    assert type(projection) is DelegatedRuntimeContextProjection
    assert projection.runtime_skills == (delegate_skill,)
    assert projection.agent_prompt_bundle is runtime_agent.prompt_bundle


@pytest.mark.asyncio
async def test_lifecycle_drives_exact_events_lazy_materialization_settlement_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, object]] = []
    execution_id = uuid.uuid4()
    now = datetime.now(UTC)
    admitted = False
    materialized: list[str] = []
    owner_loop = asyncio.get_running_loop()

    class Journal:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, object]]] = []

        def record_external_llm_usage_records(self, records):  # type: ignore[no-untyped-def]
            assert asyncio.get_running_loop() is owner_loop
            self.calls.append(records)

    journal = Journal()

    class Executor:
        trace_id = "runner-trace"

        async def _run_lifecycle_graph(self, prompt, holder):  # type: ignore[no-untyped-def]
            assert admitted is True
            assert prompt == "finish quickly"
            materialized.append("ran")
            return holder

    async def no_tools(**_kwargs):  # type: ignore[no-untyped-def]
        assert admitted is True
        materialized.append("tools")
        return []

    def build_executor(**_kwargs):  # type: ignore[no-untyped-def]
        assert admitted is True
        assert "runtime_skills" not in _kwargs
        materialized.append("executor")
        return Executor()

    class Lifecycle:
        async def run(self, call, binding, *, observers=()):  # type: ignore[no-untyped-def]
            nonlocal admitted
            assert call.task_id == "reused-tool-call"
            assert call.queue_timeout_seconds == 62
            assert call.execution_timeout_seconds == 2
            assert call.quiescence_timeout_seconds == 60
            assert materialized == []
            admitted = True
            runner = await binding.runner_factory()
            # The lifecycle has crossed the scheduler gate and started the
            # execution clock before invoking this async materializer.
            assert materialized == ["tools", "executor"]
            await runner._run_lifecycle_graph(call.prompt, object())
            assert materialized == ["tools", "executor", "ran"]

            pending = SubagentTaskSnapshot(
                execution_id=execution_id,
                task_id=call.task_id,
                status=SubagentTaskStatus.PENDING,
                trace_id="runner-trace",
                queued_at=now,
                started_at=None,
                ai_messages=(),
                usage=None,
                usage_completeness=SubagentUsageCompleteness.LATEST_OBSERVED,
            )
            running = SubagentTaskSnapshot(
                execution_id=execution_id,
                task_id=call.task_id,
                status=SubagentTaskStatus.RUNNING,
                trace_id="runner-trace",
                queued_at=now,
                started_at=now,
                ai_messages=(
                    {"role": "assistant", "content": "step one"},
                    {"role": "assistant", "content": "step two"},
                ),
                usage=SubagentTokenUsage(2, 3, 5),
                usage_completeness=SubagentUsageCompleteness.LATEST_OBSERVED,
            )
            outcome = SubagentCompleted(
                execution_id=execution_id,
                task_id=call.task_id,
                trace_id="runner-trace",
                queued_at=now,
                started_at=now,
                completed_at=now,
                ai_messages=running.ai_messages,
                usage=SubagentTokenUsage(2, 3, 5),
                usage_completeness=SubagentUsageCompleteness.FINAL_OBSERVED,
                quiescent=True,
                result="probe complete",
                stop_reason=None,
            )
            for observer in observers:
                await observer(pending)
                await observer(running)
            assert binding.settle_usage is not None
            await binding.settle_usage(
                SubagentUsageSettlement(
                    receipt_id=execution_id,
                    task_id=call.task_id,
                    records=(
                        {
                            "source_run_id": "sub-run-1",
                            "caller": "subagent:general-purpose",
                            "model_name": "parent-model",
                            "input_tokens": 2,
                            "output_tokens": 3,
                            "total_tokens": 5,
                        },
                    ),
                )
            )
            for observer in observers:
                await observer(outcome)
            return outcome

    monkeypatch.setattr(task_module, "subagent_task_lifecycle", Lifecycle(), raising=False)
    monkeypatch.setattr(task_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(task_module, "get_available_subagent_names", lambda **_kwargs: ["general-purpose"])
    monkeypatch.setattr(
        task_module,
        "get_subagent_config",
        lambda *_args, **_kwargs: SubagentConfig(
            name="general-purpose",
            description="probe",
            model="inherit",
            timeout_seconds=2,
        ),
    )
    monkeypatch.setattr(task_module, "_assemble_subagent_tools", no_tools)
    monkeypatch.setattr(
        task_module,
        "_new_subagent_graph_runner",
        build_executor,
        raising=False,
    )

    command = await task_module.task_tool.coroutine(
        runtime=_runtime(
            _binding_factory(),
            callbacks=[journal],
            context_updates={RuntimeContextKeys.RUNTIME_SKILLS: (object(),)},
        ),
        description="event probe",
        prompt="finish quickly",
        subagent_type="general-purpose",
        tool_call_id="reused-tool-call",
    )

    message = _tool_message(command)
    assert [event["type"] for event in events] == [
        "task_started",
        "task_running",
        "task_running",
        "task_completed",
    ]
    assert [event["message_index"] for event in events[1:3]] == [1, 2]
    assert message.additional_kwargs["subagent_status"] == "completed"
    assert message.additional_kwargs["subagent_token_usage"] == {
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
    }
    assert message.additional_kwargs["subagent_usage_receipt_id"] == str(execution_id)
    assert message.additional_kwargs["subagent_usage_receipt_id"] != message.tool_call_id
    assert message.additional_kwargs["subagent_usage_completeness"] == "final_observed"
    assert events[-1]["usage_completeness"] == "final_observed"
    assert journal.calls == [
        [
            {
                "source_run_id": "sub-run-1",
                "caller": "subagent:general-purpose",
                "model_name": "parent-model",
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            }
        ]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "phase",
        "expected_status",
        "expected_content",
        "expected_terminal_event",
    ),
    [
        (
            SubagentTimeoutPhase.QUEUE,
            "polling_timed_out",
            "Task polling timed out after 2 minutes. This may indicate the background task is stuck. Status: pending",
            {
                "type": "task_timed_out",
                "task_id": "call-timeout",
                "usage": None,
                "usage_completeness": "final_observed",
                "model_name": "parent-model",
            },
        ),
        (
            SubagentTimeoutPhase.EXECUTION,
            "timed_out",
            "Task timed out. Error: Execution timed out after 120 seconds",
            {
                "type": "task_timed_out",
                "task_id": "call-timeout",
                "error": "Execution timed out after 120 seconds",
                "usage": None,
                "usage_completeness": "final_observed",
                "model_name": "parent-model",
            },
        ),
    ],
)
async def test_timeout_phase_preserves_existing_wire_and_presentation(
    phase: SubagentTimeoutPhase,
    expected_status: str,
    expected_content: str,
    expected_terminal_event: dict[str, object],
) -> None:
    execution_id = uuid.uuid4()
    now = datetime.now(UTC)
    outcome = SubagentTimedOut(
        execution_id=execution_id,
        task_id="call-timeout",
        trace_id="trace",
        queued_at=now,
        started_at=(None if phase is SubagentTimeoutPhase.QUEUE else now),
        completed_at=now,
        ai_messages=(),
        usage=None,
        usage_completeness=SubagentUsageCompleteness.FINAL_OBSERVED,
        quiescent=True,
        timeout_phase=phase,
    )
    events: list[dict[str, object]] = []
    adapter = task_module._TaskLifecycleEventAdapter(
        writer=events.append,
        task_id="call-timeout",
        description="timeout probe",
        model_name="parent-model",
        execution_timeout_seconds=120,
    )

    await adapter(outcome)
    command = task_module._outcome_command(
        outcome,
        tool_call_id="call-timeout",
        model_name="parent-model",
        execution_timeout_seconds=120,
    )

    assert events == [
        {
            "type": "task_started",
            "task_id": "call-timeout",
            "description": "timeout probe",
            "model_name": "parent-model",
        },
        expected_terminal_event,
    ]
    message = _tool_message(command)
    assert message.content == expected_content
    assert message.additional_kwargs["subagent_status"] == expected_status
    assert message.additional_kwargs["subagent_usage_receipt_id"] == str(execution_id)


@pytest.mark.asyncio
async def test_nonquiescent_execution_timeout_preserves_coordination_timeout_wire() -> None:
    execution_id = uuid.uuid4()
    now = datetime.now(UTC)
    outcome = SubagentTimedOut(
        execution_id=execution_id,
        task_id="call-coordination-timeout",
        trace_id="trace",
        queued_at=now,
        started_at=now,
        completed_at=now,
        ai_messages=(),
        usage=SubagentTokenUsage(2, 3, 5),
        usage_completeness=SubagentUsageCompleteness.LATEST_OBSERVED,
        quiescent=False,
        timeout_phase=SubagentTimeoutPhase.EXECUTION,
    )
    events: list[dict[str, object]] = []
    adapter = task_module._TaskLifecycleEventAdapter(
        writer=events.append,
        task_id=outcome.task_id,
        description="coordination timeout",
        model_name="parent-model",
        execution_timeout_seconds=120,
    )

    await adapter(outcome)
    command = task_module._outcome_command(
        outcome,
        tool_call_id=outcome.task_id,
        model_name="parent-model",
        execution_timeout_seconds=120,
    )

    assert events[-1] == {
        "type": "task_timed_out",
        "task_id": outcome.task_id,
        "usage": {
            "input_tokens": 2,
            "output_tokens": 3,
            "total_tokens": 5,
        },
        "usage_completeness": "latest_observed",
        "model_name": "parent-model",
    }
    message = _tool_message(command)
    assert message.content == "Task polling timed out after 2 minutes. This may indicate the background task is stuck. Status: running"
    assert message.additional_kwargs["subagent_status"] == "polling_timed_out"
    assert message.additional_kwargs["subagent_usage_completeness"] == "latest_observed"


@pytest.mark.asyncio
async def test_failure_detail_and_cancellation_code_keep_adapter_owned_wire() -> None:
    now = datetime.now(UTC)
    common = {
        "task_id": "call-terminal",
        "trace_id": "trace",
        "queued_at": now,
        "started_at": now,
        "completed_at": now,
        "ai_messages": (),
        "usage": None,
        "usage_completeness": SubagentUsageCompleteness.FINAL_OBSERVED,
        "quiescent": True,
    }
    failed = SubagentFailed(
        **common,
        execution_id=uuid.uuid4(),
        failure_code=SubagentFailureCode.TURN_BUDGET_EXHAUSTED,
        detail="Reached max_turns=7",
        stop_reason="turn_capped",
    )
    cancelled = SubagentCancelled(
        **common,
        execution_id=uuid.uuid4(),
        cancellation_code=SubagentCancellationCode.LIFECYCLE_SHUTDOWN,
    )

    failed_events: list[dict[str, object]] = []
    failed_adapter = task_module._TaskLifecycleEventAdapter(
        writer=failed_events.append,
        task_id="call-terminal",
        description="failure probe",
        model_name="parent-model",
        execution_timeout_seconds=120,
    )
    await failed_adapter(failed)
    failed_message = _tool_message(
        task_module._outcome_command(
            failed,
            tool_call_id="call-terminal",
            model_name="parent-model",
            execution_timeout_seconds=120,
        )
    )

    assert failed_events[-1]["error"] == "Reached max_turns=7"
    assert failed_message.content == ("Task failed (capped: turn budget). Error: Reached max_turns=7")

    cancelled_events: list[dict[str, object]] = []
    cancelled_adapter = task_module._TaskLifecycleEventAdapter(
        writer=cancelled_events.append,
        task_id="call-terminal",
        description="cancel probe",
        model_name="parent-model",
        execution_timeout_seconds=120,
    )
    await cancelled_adapter(cancelled)
    cancelled_message = _tool_message(
        task_module._outcome_command(
            cancelled,
            tool_call_id="call-terminal",
            model_name="parent-model",
            execution_timeout_seconds=120,
        )
    )

    assert cancelled_events[-1]["error"] == "Cancelled by user"
    assert cancelled_message.content == ("Task cancelled by user. Error: Cancelled by user")
