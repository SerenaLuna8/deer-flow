from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.runtime import Runtime

import deerflow.agents.lead_agent.agent as lead_agent_module
from app.private_work.context import PrivateWorkContext
from app.private_work.run_admission import PersistedRunSnapshot
from app.private_work.run_repository import PrivateRunRecord
from app.private_work.run_skill_tree_materializer import (
    RuntimeOwnedMaterializedRunSkillTree,
)
from app.private_work.sandbox_files import (
    PrivateFileRunScope,
    PrivateRunFileAuthority,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.run_execution.contracts import PrivateRunExecution
from app.reliability.run_execution.errors import (
    PermanentExecutionError,
    TransientExecutionError,
)
from app.reliability.run_execution.executor import RunAgentPrivateExecutor
from app.shared_assets.skill_builder_agent_runtime import SkillBuilderAgentFactory
from app.shared_assets.skill_design_activity import SkillDesignActivityLimitExceeded
from app.worker.service import JobLeaseAuthority
from deerflow.agents.middlewares.provider_request_usage import (
    FinalProviderRequestGuard,
    build_provider_request_profile,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.file_authority import AuthorityManifest
from deerflow.models.provider_outcome import ProviderFailedResponseError
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.runtime import run_agent
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.runs.execution_contracts import (
    RunAgentOutcome,
    RunAgentUsageSnapshot,
)
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.sandbox.sandbox_provider import (
    PrivateSandboxLease,
    RunReadonlyMountSource,
    RunScopedReadOnlyMount,
)
from deerflow.subagents.binding import (
    AgentGraphExecutionInputs,
    ParentExecutionBindingFactory,
    PrivateRunParentExecutionProfile,
)
from deerflow.tools.builtins.task_tool import task_tool

_MODEL_REF = "00000000-0000-4000-8000-000000000451"
_FULL_BUILDER_TOOL_GROUPS = ("bash", "task")


def _runner_success() -> RunAgentOutcome:
    return RunAgentOutcome.succeeded(
        RunAgentUsageSnapshot(
            total_input_tokens=0,
            total_output_tokens=0,
            total_tokens=0,
            llm_call_count=0,
            lead_agent_tokens=0,
            subagent_tokens=0,
            middleware_tokens=0,
            token_usage_by_model={},
        )
    )


class _Runtime:
    def __init__(self, skill_root: Path, events: list[str] | None = None) -> None:
        self.model_ref = _MODEL_REF
        self.skill_root = skill_root
        self.model_settings = SimpleNamespace(
            thinking_enabled=False,
            reasoning_effort=None,
        )
        self.skills = ()
        self.mcp_definitions = ()
        self.mcp_tools = ()
        self.tool_groups = ()
        self.agent_catalog = None
        self._events = events
        self._closed = False
        self._skill_tree = RuntimeOwnedMaterializedRunSkillTree(
            owner_root=skill_root.parent / "skill-tree-owner",
            source=RunReadonlyMountSource(
                owner_id=uuid.uuid4(),
                worker_root=skill_root,
            ),
            manifests=(),
            skills=(),
        )

    def borrow_materialized_skill_tree(
        self,
    ) -> RuntimeOwnedMaterializedRunSkillTree:
        return self._skill_tree

    async def aclose(self, _mount_outcome=None) -> None:
        if self._closed:
            return
        self._closed = True
        if self._events is not None:
            self._events.append("runtime-close")


class _Assets:
    def __init__(self, runtime: _Runtime) -> None:
        self._runtime = runtime

    async def materialize(self, *_args, **_kwargs) -> _Runtime:
        return self._runtime


class _Checkpointer:
    def for_context(self, _context, *, thread_kind: str):
        assert thread_kind == "skill_builder"
        return SimpleNamespace(
            set_authorization_boundary=lambda _boundary: None,
            aget_tuple=AsyncMock(return_value=None),
        )


def _execution_bundle(
    tmp_path: Path,
    *,
    runner,
    events: list[str] | None = None,
    activity_emitter_factory=None,
) -> tuple[
    RunAgentPrivateExecutor,
    PrivateRunExecution,
    JobLeaseAuthority,
    _Runtime,
]:
    model = ModelConfig(
        name=_MODEL_REF,
        display_name="Skill Builder provider test",
        description="",
        use="support.fake_models:FakeVisionBridgeChatModel",
        model="skill-builder-provider-test",
        max_input_tokens=64_000,
    )
    app_config = AppConfig(
        models=[model],
        sandbox={
            "use": "deerflow.sandbox.local:LocalSandboxProvider",
            "host_execution_approval": {
                "mode": "approval_required",
                "execution_domain_id": "skill-builder-provider-test",
            },
        },
    )
    project = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="skill-builder-provider-test",
    )
    context = PrivateWorkContext.from_project(project)
    now = datetime.now(UTC)
    run = PrivateRunRecord(
        run_id=str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        assistant_id=str(uuid.uuid4()),
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={"input": {"messages": []}},
        origin_trace_id="b" * 32,
        error=None,
        model_name=model.name,
        created_at=now,
        updated_at=now,
    )
    skill_root = tmp_path / "exact-skills"
    skill_root.mkdir()
    runtime = _Runtime(skill_root, events)
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    if activity_emitter_factory is None:

        async def activity_emitter_factory(*_args, **_kwargs):
            return SimpleNamespace(append=AsyncMock())

    executor = RunAgentPrivateExecutor(
        lambda: None,
        app_config=app_config,
        bridge=bridge,
        project_checkpointer=_Checkpointer(),
        store=SimpleNamespace(),
        event_store=None,
        asset_runtime=_Assets(runtime),
        agent_factory=object(),
        runner=runner,
        skill_builder_activity_emitter_factory=activity_emitter_factory,
    )
    execution = PrivateRunExecution(
        context=context,
        run=run,
        snapshot=PersistedRunSnapshot(
            assets=(),
            mcp_secrets=(),
            catalog_generation=1,
        ),
        checkpoint_namespace="",
        graph_input={"messages": []},
        command=None,
        config={},
        interrupt_before=None,
        interrupt_after=None,
        stream_mode=["values"],
        stream_subgraphs=False,
        runtime_kind="skill_builder",
    )
    claim = JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="lease",
        job_type="private_run",
        scope=JobScope(context.project_id, str(context.user_id)),
        run_id=run.run_id,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        origin_trace_id=run.origin_trace_id,
    )
    authority = JobLeaseAuthority(lambda: None, claim, lease_seconds=30)
    return executor, execution, authority, runtime


@pytest.mark.asyncio
async def test_skill_builder_activity_limit_is_not_retried(tmp_path: Path) -> None:
    async def activity_emitter_factory(*_args, **_kwargs):
        raise SkillDesignActivityLimitExceeded

    runner = AsyncMock(side_effect=AssertionError("runner must not start"))

    executor, execution, authority, _runtime = _execution_bundle(
        tmp_path,
        runner=runner,
        activity_emitter_factory=activity_emitter_factory,
    )

    with pytest.raises(PermanentExecutionError):
        await executor.execute(execution, authority)
    runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "retry_safe"),
    ((429, True), (500, False)),
)
async def test_skill_builder_provider_status_answer_stays_known_without_chat_observer(
    tmp_path: Path,
    status_code: int,
    retry_safe: bool,
) -> None:
    observed: dict[str, object] = {}

    async def runner(
        _bridge,
        _run_manager,
        _record,
        *,
        ctx,
        **_kwargs,
    ) -> RunAgentOutcome:
        observed["observer"] = ctx.context_evidence_observer
        model = GenericFakeChatModel(
            messages=iter([AIMessage(content="unused")]),
            profile={"max_input_tokens": 100_000},
        )
        profile = build_provider_request_profile(
            model=model,
            model_name="skill-builder-provider-test",
            provider_adapter="openai",
            system_prompt="system",
            tools=(),
            supports_vision=False,
        )
        request = ModelRequest(
            model=model,
            messages=[HumanMessage(content="build a skill")],
            system_prompt=profile.system_prompt,
            tools=[],
            state={
                "provider_request_profile": profile.snapshot(),
                "messages": [HumanMessage(content="build a skill")],
            },
            runtime=Runtime(context={"run_id": "skill-builder-run"}),
        )
        provider_request = httpx.Request(
            "POST",
            "https://provider.invalid/v1/chat/completions",
        )
        provider_error = (
            openai.RateLimitError(
                "rate limited",
                response=httpx.Response(status_code, request=provider_request),
                body=None,
            )
            if status_code == 429
            else openai.APIStatusError(
                "provider failed",
                response=httpx.Response(status_code, request=provider_request),
                body=None,
            )
        )

        async def provider_call(_request: ModelRequest) -> Any:
            raise provider_error

        with pytest.raises(ProviderFailedResponseError) as caught:
            await FinalProviderRequestGuard(
                profile,
                evidence_observer=ctx.context_evidence_observer,
            ).awrap_model_call(request, provider_call)
        observed["failure_code"] = caught.value.failure_code
        observed["retry_safe"] = caught.value.retry_safe
        return _runner_success()

    executor, execution, authority, _runtime = _execution_bundle(
        tmp_path,
        runner=runner,
    )

    result = await executor.execute(execution, authority)

    assert result.status == "succeeded"
    assert observed == {
        "observer": None,
        "failure_code": f"PROVIDER_HTTP_{status_code}",
        "retry_safe": retry_safe,
    }


def _named_tool(name: str) -> StructuredTool:
    def invoke() -> str:
        return name

    return StructuredTool.from_function(
        func=invoke,
        name=name,
        description=f"{name} provider policy test tool",
    )


def _tool_policy_config(*, aio: bool) -> AppConfig:
    model = ModelConfig(
        name=_MODEL_REF,
        display_name="Skill Builder tool policy test",
        description="",
        use="support.fake_models:FakeVisionBridgeChatModel",
        model="skill-builder-tool-policy-test",
        max_input_tokens=64_000,
    )
    sandbox: dict[str, object] = {
        "use": ("deerflow.sandbox.aio:AioSandboxProvider" if aio else "deerflow.sandbox.local:LocalSandboxProvider"),
    }
    if not aio:
        sandbox["host_execution_approval"] = {
            "mode": "approval_required",
            "execution_domain_id": "skill-builder-provider-test",
        }
    return AppConfig(
        models=[model],
        sandbox=sandbox,
        summarization={"enabled": False},
        tool_search={"enabled": False},
        skills={"deferred_discovery": False},
        guardrails={"enabled": False},
    )


def _capture_canonical_tool_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    app_config: AppConfig,
) -> list[str]:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        lead_agent_module,
        "frozen_checkpoint_channel_mode",
        lambda: None,
    )
    monkeypatch.setattr(
        lead_agent_module,
        "freeze_checkpoint_channel_mode",
        lambda value: value,
    )
    monkeypatch.setattr(
        lead_agent_module,
        "freeze_checkpoint_snapshot_frequency",
        lambda value: value,
    )
    monkeypatch.setattr(
        lead_agent_module,
        "inject_checkpoint_mode",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        lead_agent_module,
        "build_tracing_callbacks",
        lambda: [],
    )
    monkeypatch.setattr(
        lead_agent_module,
        "build_middlewares",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        lead_agent_module,
        "normalize_middleware_state_schemas",
        lambda value, *_args: value,
    )
    monkeypatch.setattr(
        lead_agent_module,
        "apply_prompt_template",
        lambda **_kwargs: "prompt",
    )
    monkeypatch.setattr(
        lead_agent_module,
        "get_thread_state_schema",
        lambda *_args: dict,
    )
    monkeypatch.setattr(
        lead_agent_module.ModelRuntime,
        "build_chat_model",
        lambda _self, **_kwargs: object(),
    )

    def create_agent(**kwargs):
        captured["tools"] = kwargs["tools"]
        return object()

    monkeypatch.setattr(lead_agent_module, "create_agent", create_agent)
    monkeypatch.setattr(
        "deerflow.tools.get_available_tools",
        lambda **_kwargs: [_named_tool("bash"), _named_tool("task")],
    )

    lead_agent_module._make_lead_agent(
        {
            "configurable": {"thinking_enabled": False},
            "context": {"non_interactive": True},
        },
        app_config=app_config,
        private_runtime=SimpleNamespace(
            model_ref=_MODEL_REF,
            model_settings=None,
            tool_groups=_FULL_BUILDER_TOOL_GROUPS,
            skills=(),
            safe_manifest=SimpleNamespace(skills=()),
            mcp_tools=(),
            skill_root=tmp_path,
            prompt_bundle=None,
            soul="Skill Builder provider policy",
            agent_catalog=None,
        ),
    )
    return [tool.name for tool in captured["tools"]]


@pytest.mark.asyncio
async def test_skill_builder_executor_installs_private_file_authority_with_exact_skill_mount(
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    async def runner(
        _bridge,
        _run_manager,
        _record,
        *,
        ctx,
        agent_factory,
        config,
        **_kwargs,
    ) -> RunAgentOutcome:
        observed["file_authority"] = ctx.file_authority
        observed["agent_factory"] = agent_factory
        observed["host_execution_approval_port"] = ctx.host_execution_approval_port
        observed["config"] = config
        return _runner_success()

    executor, execution, authority, _runtime = _execution_bundle(
        tmp_path,
        runner=runner,
    )
    execution = replace(
        execution,
        config={"context": {"non_interactive": False}},
    )

    result = await executor.execute(execution, authority)

    assert result.status == "succeeded"
    file_authority = observed["file_authority"]
    assert file_authority is not None
    assert isinstance(observed["agent_factory"], SkillBuilderAgentFactory)
    assert observed["host_execution_approval_port"] is None
    assert observed["config"]["context"]["non_interactive"] is True
    assert file_authority._mounts == ()  # pyright: ignore[reportPrivateUsage]
    assert (  # pyright: ignore[reportPrivateUsage]
        file_authority._run_skill_tree is _runtime.borrow_materialized_skill_tree()
    )
    assert (  # pyright: ignore[reportPrivateUsage]
        file_authority._skill_container_path == "/mnt/skills"
    )


def test_private_file_authority_authorizes_only_active_exact_read_only_mount(
    tmp_path: Path,
) -> None:
    _executor, execution, _job_authority, _runtime = _execution_bundle(
        tmp_path,
        runner=AsyncMock(),
    )
    mount = RunScopedReadOnlyMount(
        run_id=execution.run.run_id,
        container_path="/mnt/skills",
        host_path=str(tmp_path / "exact-skills"),
    )
    authority = PrivateRunFileAuthority(
        PrivateFileRunScope(
            execution.context,
            thread_id=execution.run.thread_id,
            run_id=execution.run.run_id,
        ),
        MagicMock(),
        MagicMock(),
        mounts=(mount,),
    )

    assert not authority.authorizes_run_read_only_mount_path(
        run_id=execution.run.run_id,
        path="/mnt/skills/public/skill-creator/SKILL.md",
    )

    authority._lease = PrivateSandboxLease(  # pyright: ignore[reportPrivateUsage]
        sandbox_id="private-1",
        run_id=execution.run.run_id,
        relative_root="projects/project/users/owner/threads/thread",
    )
    authority._sandbox = object()  # pyright: ignore[reportPrivateUsage]
    authority._manifest = AuthorityManifest(  # pyright: ignore[reportPrivateUsage]
        entries=(),
        run_id=execution.run.run_id,
    )

    assert authority.authorizes_run_read_only_mount_path(
        run_id=execution.run.run_id,
        path="/mnt/skills/public/skill-creator/SKILL.md",
    )
    assert not authority.authorizes_run_read_only_mount_path(
        run_id="other-run",
        path="/mnt/skills/public/skill-creator/SKILL.md",
    )
    assert not authority.authorizes_run_read_only_mount_path(
        run_id=execution.run.run_id,
        path="/mnt/skills-extra/public/skill-creator/SKILL.md",
    )


def test_skill_builder_local_approval_hides_direct_bash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tool_names = _capture_canonical_tool_names(
        monkeypatch,
        tmp_path,
        app_config=_tool_policy_config(aio=False),
    )

    assert "bash" not in tool_names
    assert "task" in tool_names


@pytest.mark.asyncio
async def test_skill_builder_private_run_blocks_delegated_bash() -> None:
    app_config = _tool_policy_config(aio=False)

    @asynccontextmanager
    async def delegated_output_scope(_task_id: str):
        yield SimpleNamespace(
            output_root=("/mnt/user-data/workspace/.deerflow/subagents/44444444444444444444444444444444/outputs"),
            promotions=(),
        )

    binding_factory = ParentExecutionBindingFactory(
        PrivateRunParentExecutionProfile(
            graph=AgentGraphExecutionInputs(
                model=object(),
                tools=(),
                middleware=(),
                system_prompt=None,
                state_schema=dict,
            ),
            app_config=app_config,
            asset_context=None,
            private_runtime=object(),
            model_name=_MODEL_REF,
            thinking_enabled=False,
            reasoning_effort=None,
            runtime_skills=(),
            runtime_agent_catalog=None,
            tool_groups=(),
        )
    )
    result = await task_tool.coroutine(
        runtime=SimpleNamespace(
            context={
                RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: binding_factory,
                RuntimeContextKeys.FILE_AUTHORITY: SimpleNamespace(
                    delegated_output_scope=delegated_output_scope,
                ),
                RuntimeContextKeys.NON_INTERACTIVE: True,
            },
            state={},
            config={},
            store=None,
        ),
        description="test delegated bash",
        prompt="Run a harmless command.",
        subagent_type="bash",
        tool_call_id="builder-task-bash",
    )

    message = result.update["messages"][0]
    assert "per-Task filesystem namespace" in message.content


def test_skill_builder_aio_keeps_isolated_bash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tool_names = _capture_canonical_tool_names(
        monkeypatch,
        tmp_path,
        app_config=_tool_policy_config(aio=True),
    )

    assert "bash" in tool_names
    assert "task" in tool_names


@pytest.mark.asyncio
async def test_skill_builder_provider_acquisition_failure_precedes_agent_model_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    model_build = MagicMock(
        side_effect=AssertionError("model must not build before provider acquisition"),
    )

    class FailingAuthority:
        sandbox_id = None

        def __init__(self, *_args, **_kwargs) -> None:
            self._released = False

        async def restore(self):
            events.append("provider-acquire")
            raise SandboxRuntimeError("provider acquisition failed")

        async def mark_failed(self) -> None:
            events.append("mark-failed")

        async def release(self) -> None:
            if self._released:
                return
            self._released = True
            events.append("authority-release")

    async def worker_runner(*args, ctx, **kwargs) -> RunAgentOutcome:
        _bridge, run_manager, record = args
        return await run_agent(
            SimpleNamespace(
                publish=AsyncMock(),
                publish_end=AsyncMock(),
            ),
            run_manager,
            record,
            ctx=replace(ctx, event_store=None),
            **kwargs,
        )

    monkeypatch.setattr(
        "app.reliability.run_execution.preparation.PrivateRunFileAuthority",
        FailingAuthority,
    )
    monkeypatch.setattr(
        SkillBuilderAgentFactory,
        "private_runtime_factory",
        model_build,
    )
    executor, execution, authority, _runtime = _execution_bundle(
        tmp_path,
        runner=worker_runner,
        events=events,
    )

    result = await executor.execute(execution, authority)

    assert result.status == "failed"
    assert events == [
        "provider-acquire",
        "mark-failed",
        "authority-release",
        "runtime-close",
    ]
    model_build.assert_not_called()


@pytest.mark.asyncio
async def test_executor_releases_private_authority_before_removing_pinned_skills(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class RecordingAuthority:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def release(self) -> None:
            events.append("authority-release")

    async def failing_runner(*_args, **_kwargs) -> None:
        raise RuntimeError("runner failed")

    monkeypatch.setattr(
        "app.reliability.run_execution.preparation.PrivateRunFileAuthority",
        RecordingAuthority,
    )
    executor, execution, authority, _runtime = _execution_bundle(
        tmp_path,
        runner=failing_runner,
        events=events,
    )

    with pytest.raises(TransientExecutionError):
        await executor.execute(execution, authority)

    assert events == ["authority-release", "runtime-close"]
