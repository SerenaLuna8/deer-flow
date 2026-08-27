from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from support.private_thread_seed import TEST_MODEL_REF, seed_private_thread_database
from support.system_model_seed import (
    seed_system_model_config,
    system_model_payload_checksum,
)

from app.gateway.private_work_schemas import PrivateRunCreateRequest
from app.gateway.routers.private_work import _run_response
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkRunExecutionProfileUnsupported
from app.private_work.execution_profile import (
    RUN_EXECUTION_PROFILE_KWARG,
    EffectiveRunExecutionProfile,
    RequestedRunExecutionProfile,
    RunExecutionProfileUnsupported,
    RunModelSelectionLocked,
    parse_persisted_run_execution_profile,
    persisted_run_execution_profile,
    resolve_admitted_run_execution_profile,
    selected_run_model_ref,
)
from app.private_work.http_runtime import start_private_run
from app.private_work.inbound_dedupe import PrivateRunInboundDelivery
from app.private_work.run_admission import (
    PersistedRunSnapshot,
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
    PrivateRunInboundAuthority,
)
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRecord
from app.private_work.sandbox_files import (
    RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG,
    required_current_upload_snapshot_from_run_kwargs,
)
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.private_work.workload_profile import (
    RUN_WORKLOAD_PROFILE_KWARG,
    EffectiveRunWorkloadProfile,
    RequestedRunWorkloadProfile,
    persisted_run_workload_profile,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.execution import (
    PermanentExecutionError,
    PrivateRunExecution,
    RunAgentPrivateExecutor,
)
from app.reliability.run_execution.vision_dispatch import (
    PrivateRunVisionDispatchAuthority,
)
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.catalog_state_repository import CatalogStateRepository
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
)
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    MaterializedAgentRuntimePolicy,
)
from app.system_settings import SystemModelCatalogService
from app.worker.service import JobLeaseAuthority
from deerflow.agents.memory.snip import SnipArchiveContext
from deerflow.config.agents_config import AgentModelSettings
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.models.factory import create_chat_model
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.runs.execution_contracts import (
    RunAgentOutcome,
    RunAgentUsageSnapshot,
)

PRIMARY_MODEL_REF = "00000000-0000-4000-8000-000000000301"
OTHER_MODEL_REF = "00000000-0000-4000-8000-000000000302"
SAMPLING_MODEL_REF = "00000000-0000-4000-8000-000000000303"


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


def test_private_run_execution_profile_is_strict_and_separate_from_generic_context() -> None:
    request = PrivateRunCreateRequest.model_validate(
        {
            "execution_profile": {
                "model_name": PRIMARY_MODEL_REF,
                "thinking_enabled": True,
                "reasoning_effort": "high",
            },
            "config": {
                "context": {
                    "model_name": "forged-config-model",
                    "thinking_enabled": False,
                    "reasoning_effort": "minimal",
                    "safe": "value",
                }
            },
            "context": {
                "model_name": "forged-context-model",
                "thinking_enabled": False,
                "reasoning_effort": "minimal",
            },
        }
    )

    assert request.execution_profile.model_dump() == {
        "model_name": PRIMARY_MODEL_REF,
        "thinking_enabled": True,
        "reasoning_effort": "high",
    }
    assert request.config == {"context": {"safe": "value"}}
    assert request.context == {}

    with pytest.raises(ValidationError):
        PrivateRunCreateRequest.model_validate(
            {
                "execution_profile": {
                    "model_name": PRIMARY_MODEL_REF,
                    "unknown": True,
                }
            }
        )


def test_private_run_server_context_owns_channel_identity() -> None:
    caller_kwargs = {
        "config": {
            "context": {
                "channel_user_id": "forged-browser-identity",
                "safe": "value",
            },
        },
    }
    ordinary = PrivateRunAdmissionService._server_kwargs(
        caller_kwargs,
        None,
    )
    continuation = PrivateRunAdmissionService._server_kwargs(
        caller_kwargs,
        PrivateRunAdmissionServerContext(
            channel_user_id="frozen-channel-user",
        ),
    )
    inbound = PrivateRunAdmissionService._server_kwargs(
        caller_kwargs,
        PrivateRunAdmissionServerContext(
            inbound_authority=PrivateRunInboundAuthority(
                connection_id=str(uuid.uuid4()),
                provider="feishu",
                external_account_id="verified-inbound-user",
                workspace_id=None,
                external_conversation_id="chat-1",
                external_topic_id=None,
            ),
            inbound_delivery=PrivateRunInboundDelivery("delivery-1"),
        ),
    )

    assert ordinary["config"]["context"] == {
        "channel_user_id": None,
        "safe": "value",
    }
    assert continuation["config"]["context"]["channel_user_id"] == ("frozen-channel-user")
    assert inbound["config"]["context"]["channel_user_id"] == ("verified-inbound-user")


def test_default_agent_selection_and_effective_profile_are_fail_closed() -> None:
    requested = RequestedRunExecutionProfile(
        model_name=PRIMARY_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="high",
    )
    assert selected_run_model_ref("default", requested) == PRIMARY_MODEL_REF

    effective = resolve_admitted_run_execution_profile(
        requested=requested,
        model_ref=PRIMARY_MODEL_REF,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_vision=True,
        agent_thinking_enabled=None,
        agent_reasoning_effort=None,
    )
    assert effective == EffectiveRunExecutionProfile(
        model_name=PRIMARY_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="high",
        supports_vision=True,
    )

    with pytest.raises(RunModelSelectionLocked):
        selected_run_model_ref(OTHER_MODEL_REF, requested)

    with pytest.raises(RunExecutionProfileUnsupported):
        resolve_admitted_run_execution_profile(
            requested=requested,
            model_ref=OTHER_MODEL_REF,
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=False,
            agent_thinking_enabled=None,
            agent_reasoning_effort=None,
        )


def test_disabled_thinking_freezes_none_effort_for_reasoning_models() -> None:
    effective = resolve_admitted_run_execution_profile(
        requested=RequestedRunExecutionProfile(
            thinking_enabled=False,
            reasoning_effort="none",
        ),
        model_ref=PRIMARY_MODEL_REF,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_vision=True,
        agent_thinking_enabled=None,
        agent_reasoning_effort=None,
    )

    assert effective.thinking_enabled is False
    assert effective.reasoning_effort == "none"

    with pytest.raises(RunExecutionProfileUnsupported):
        resolve_admitted_run_execution_profile(
            requested=RequestedRunExecutionProfile(
                thinking_enabled=False,
                reasoning_effort="minimal",
            ),
            model_ref=PRIMARY_MODEL_REF,
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_vision=True,
            agent_thinking_enabled=None,
            agent_reasoning_effort=None,
        )


def test_flash_and_image_profile_reach_openai_responses_payload() -> None:
    model = ModelConfig(
        name=PRIMARY_MODEL_REF,
        display_name="GPT 5.6 Luna",
        description="",
        use="langchain_openai:ChatOpenAI",
        model="gpt-5.6-luna",
        max_input_tokens=64_000,
        api_key=SecretStr("unit-test-key"),
        base_url="https://opencode.ai/zen/v1",
        use_responses_api=True,
        output_version="responses/v1",
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_vision=True,
    )
    chat_model = create_chat_model(
        name=model.name,
        thinking_enabled=False,
        reasoning_effort="none",
        app_config=AppConfig(
            models=[model],
            sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        ),
        attach_tracing=False,
    )

    payload = chat_model._get_request_payload(  # pyright: ignore[reportPrivateUsage]
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,iVBORw0KGgo=",
                        },
                    },
                ]
            )
        ],
    )

    assert payload["model"] == "gpt-5.6-luna"
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["input"][0]["content"] == [
        {"type": "input_text", "text": "describe"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,iVBORw0KGgo=",
        },
    ]


def _run_record(
    *,
    requested: RequestedRunExecutionProfile,
    effective: EffectiveRunExecutionProfile,
) -> PrivateRunRecord:
    now = datetime.now(UTC)
    return PrivateRunRecord(
        run_id=str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        assistant_id=str(uuid.uuid4()),
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={
            "input": {"messages": []},
            RUN_EXECUTION_PROFILE_KWARG: persisted_run_execution_profile(
                requested,
                effective,
            ),
            RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
                RequestedRunWorkloadProfile(),
                EffectiveRunWorkloadProfile(name="interactive"),
            ),
        },
        origin_trace_id="a" * 32,
        error=None,
        model_name=effective.model_name,
        created_at=now,
        updated_at=now,
    )


def test_run_idempotency_includes_the_requested_execution_profile() -> None:
    requested = RequestedRunExecutionProfile(
        model_name=PRIMARY_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="high",
    )
    effective = EffectiveRunExecutionProfile(
        model_name=PRIMARY_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="high",
        supports_vision=True,
    )
    record = _run_record(requested=requested, effective=effective)
    base = PrivateRunCreate(
        run_id=record.run_id,
        kwargs={"input": {"messages": []}},
        execution_profile=requested,
    )

    assert PrivateRunAdmissionService._is_same_request(
        record,
        thread_id=record.thread_id,
        request=base,
    )
    assert not PrivateRunAdmissionService._is_same_request(
        record,
        thread_id=record.thread_id,
        request=PrivateRunCreate(
            run_id=record.run_id,
            kwargs={"input": {"messages": []}},
            execution_profile=RequestedRunExecutionProfile(
                model_name=PRIMARY_MODEL_REF,
                thinking_enabled=True,
                reasoning_effort="low",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_private_run_launcher_passes_only_the_typed_profile_to_admission() -> None:
    requested = RequestedRunExecutionProfile(
        model_name=PRIMARY_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="high",
    )
    effective = EffectiveRunExecutionProfile(
        model_name=PRIMARY_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="high",
        supports_vision=True,
    )
    record = _run_record(requested=requested, effective=effective)
    captured: dict[str, object] = {}

    class Admission:
        async def admit(
            self,
            context,
            thread_id,
            request,
            *,
            server_context,
        ):
            captured.update(
                context=context,
                thread_id=thread_id,
                request=request,
                server_context=server_context,
            )
            return SimpleNamespace(
                run=record,
                thread_id=thread_id,
                opaque_runtime_scope=object(),
                inbound_delivery_replay=False,
            )

    body = PrivateRunCreateRequest.model_validate(
        {
            "input": {"messages": [{"role": "user", "content": "hello"}]},
            "execution_profile": requested.as_dict(),
            "context": {
                "model_name": "forged",
                "thinking_enabled": False,
                "reasoning_effort": "minimal",
            },
        }
    )
    context = SimpleNamespace(
        request_id="profile-launch",
        resource_scope=object(),
    )

    launched = await start_private_run(
        body,
        record.thread_id,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        context,
        admission_service=Admission(),
    )

    admitted_request = captured["request"]
    assert admitted_request.execution_profile == requested
    assert admitted_request.kwargs["config"]["context"] == {
        "thread_id": record.thread_id,
    }
    assert launched.model_name == effective.model_name


def test_worker_consumes_only_the_persisted_effective_profile() -> None:
    requested = RequestedRunExecutionProfile(
        model_name=PRIMARY_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="high",
    )
    effective = EffectiveRunExecutionProfile(
        model_name=PRIMARY_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="high",
        supports_vision=True,
    )
    execution = SimpleNamespace(
        config={
            "configurable": {
                "thinking_enabled": False,
                "reasoning_effort": "minimal",
            },
            "context": {
                "thinking_enabled": False,
                "reasoning_effort": "minimal",
            },
        },
        run=SimpleNamespace(
            kwargs={
                RUN_EXECUTION_PROFILE_KWARG: persisted_run_execution_profile(
                    requested,
                    effective,
                )
            }
        ),
    )

    config = RunAgentPrivateExecutor._runner_config(execution, object())

    assert config["configurable"]["thinking_enabled"] is True
    assert config["configurable"]["reasoning_effort"] == "high"
    assert config["context"]["thinking_enabled"] is True
    assert config["context"]["reasoning_effort"] == "high"

    execution.run.kwargs[RUN_EXECUTION_PROFILE_KWARG] = {"requested": {}, "effective": {}}
    with pytest.raises(PermanentExecutionError, match="RUN_EXECUTION_PROFILE_STALE"):
        RunAgentPrivateExecutor._runner_config(execution, object())


@pytest.mark.asyncio
async def test_worker_treats_agent_sampling_incompatibility_as_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = RequestedRunExecutionProfile(model_name=SAMPLING_MODEL_REF)
    effective = EffectiveRunExecutionProfile(
        model_name=SAMPLING_MODEL_REF,
        thinking_enabled=False,
        reasoning_effort="none",
        supports_vision=False,
    )
    run = _run_record(requested=requested, effective=effective)
    project = ProjectContext(
        user_id=uuid.UUID(run.owner_user_id),
        project_id=run.project_id,
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="sampling-worker",
    )
    context = PrivateWorkContext.from_project(project)
    model = ModelConfig(
        name=SAMPLING_MODEL_REF,
        display_name="Sampling test",
        description="",
        use="support.fake_models:FakeVisionBridgeChatModel",
        model="sampling-test",
        max_input_tokens=64_000,
        supports_thinking=True,
        supports_reasoning_effort=True,
    )

    class Models:
        async def materialize_snapshot(self, **_kwargs):
            return model

    class Checkpointer:
        def for_context(self, _context):
            return SimpleNamespace()

    app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )

    class Assets:
        async def materialize(self, *_args, **_kwargs):
            create_chat_model(
                model.name,
                app_config=app_config,
                attach_tracing=False,
                model_overrides={"max_tokens": 64},
            )

    executor = RunAgentPrivateExecutor(
        lambda: None,
        app_config=app_config,
        bridge=SimpleNamespace(),
        project_checkpointer=Checkpointer(),
        store=SimpleNamespace(),
        event_store=SimpleNamespace(),
        asset_runtime=Assets(),
        model_materializer=Models(),
        agent_factory=object(),
        runner=object(),
    )
    archive_context = SnipArchiveContext(
        enabled=False,
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        namespace="default",
        preference_version=1,
        summary_model=None,
    )

    async def memory_archive_context(*_args, **_kwargs):
        return archive_context

    monkeypatch.setattr(
        executor,
        "_memory_archive_context",
        memory_archive_context,
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
    authority = JobLeaseAuthority(
        lambda: None,
        claim,
        lease_seconds=30,
    )

    with pytest.raises(
        PermanentExecutionError,
        match="RUN_EXECUTION_PROFILE_UNSUPPORTED",
    ):
        await executor.execute(execution, authority)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_kind",
    ["chat", "skill_builder"],
    ids=["chat", "skill-builder"],
)
@pytest.mark.parametrize(
    ("provider_adapter", "provider_settings"),
    [
        (
            "openai",
            {
                "base_url": "https://responses.example.test/v1",
                "use_responses_api": True,
            },
        ),
        ("anthropic", {}),
        ("vllm", {}),
    ],
    ids=["openai-responses", "anthropic", "ordinary-vision-adapter"],
)
async def test_worker_injects_durable_authority_for_any_selected_visual_adapter(
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
    provider_adapter: str,
    provider_settings: dict[str, object],
) -> None:
    vision_model_ref = OTHER_MODEL_REF
    lead_model = ModelConfig(
        name=PRIMARY_MODEL_REF,
        display_name="Text lead",
        description="",
        use="support.fake_models:FakeVisionBridgeChatModel",
        model="text-lead",
        max_input_tokens=64_000,
        supports_vision=False,
    )
    lead_model._system_model_config_id = uuid.uuid4()
    lead_model._system_provider_adapter = "openai"
    vision_model = ModelConfig(
        name=vision_model_ref,
        display_name="Selected visual model",
        description="",
        use="support.fake_models:FakeVisionBridgeChatModel",
        model="selected-visual-model",
        max_input_tokens=64_000,
        supports_vision=True,
        **provider_settings,
    )
    vision_model._system_model_config_id = uuid.uuid4()
    vision_model._system_provider_adapter = provider_adapter
    app_config = AppConfig(
        models=[lead_model, vision_model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )
    runtime_policy = AgentRuntimePolicyValue(
        title={"enabled": False},
        vision_bridge={"model_name": vision_model_ref},
    )
    materialized_purposes: list[str] = []

    class RuntimePolicy:
        async def materialize_run_snapshot_envelope(self, **_kwargs):
            return MaterializedAgentRuntimePolicy(
                schema_version=6,
                value=runtime_policy,
            )

    class Models:
        async def materialize_snapshot(self, *, purpose: str, **_kwargs):
            materialized_purposes.append(purpose)
            if purpose == "lead":
                return lead_model
            assert purpose == "vision"
            return vision_model

    class Runtime:
        model_ref = lead_model.name
        skill_root = None

        def borrow_materialized_skill_tree(self):
            return None

        async def aclose(self) -> None:
            return None

    class Assets:
        async def materialize(self, *_args, **_kwargs):
            return Runtime()

    class Checkpointer:
        def for_context(self, _context, *, thread_kind: str):
            assert thread_kind == runtime_kind
            return SimpleNamespace(
                set_authorization_boundary=lambda _boundary: None,
            )

    observed: dict[str, object] = {}

    async def runner(_bridge, _run_manager, record, *, ctx, **_kwargs):
        observed["authority"] = ctx.vision_dispatch_authority
        observed["record_status"] = str(record.status)
        observed["tool_control_policy"] = ctx.tool_call_control_policy
        observed["max_concurrent_subagents"] = ctx.max_concurrent_subagents
        observed["max_total_subagents"] = ctx.max_total_subagents
        return _runner_success()

    async def activity_emitter_factory(*_args):
        return SimpleNamespace()

    executor = RunAgentPrivateExecutor(
        lambda: None,
        app_config=app_config,
        bridge=SimpleNamespace(),
        project_checkpointer=Checkpointer(),
        store=SimpleNamespace(),
        event_store=SimpleNamespace(),
        asset_runtime=Assets(),
        model_materializer=Models(),
        runtime_policy_materializer=RuntimePolicy(),
        agent_factory=object(),
        runner=runner,
        skill_builder_activity_emitter_factory=activity_emitter_factory,
    )
    requested = RequestedRunExecutionProfile(model_name=lead_model.name)
    effective = EffectiveRunExecutionProfile(
        model_name=lead_model.name,
        thinking_enabled=False,
        reasoning_effort="none",
        supports_vision=False,
    )
    run = _run_record(requested=requested, effective=effective)
    context = PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.UUID(run.owner_user_id),
            project_id=run.project_id,
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="worker-vision-authority",
        )
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
        runtime_kind=runtime_kind,
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
    archive_context = SnipArchiveContext(
        enabled=False,
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        namespace="default",
        preference_version=1,
        summary_model=None,
    )

    async def memory_archive_context(*_args, **_kwargs):
        return archive_context

    monkeypatch.setattr(
        executor,
        "_memory_archive_context",
        memory_archive_context,
    )

    result = await executor.execute(execution, authority)

    assert result.status == "succeeded"
    assert observed["record_status"] == "pending"
    assert materialized_purposes == ["lead", "vision"]
    assert isinstance(
        observed["authority"],
        PrivateRunVisionDispatchAuthority,
    )
    assert observed["tool_control_policy"].workload_profile == "interactive"
    assert observed["tool_control_policy"].accounting_mode == "lead_run_subagent_task"
    assert observed["tool_control_policy"].lead.internal_tool_call_limit == 200
    assert observed["tool_control_policy"].subagent.internal_tool_call_limit == 50
    assert observed["max_concurrent_subagents"] == 3
    assert observed["max_total_subagents"] == 6


def test_run_response_echoes_the_effective_execution_profile() -> None:
    requested = RequestedRunExecutionProfile(model_name=PRIMARY_MODEL_REF)
    effective = EffectiveRunExecutionProfile(
        model_name=PRIMARY_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="medium",
        supports_vision=True,
    )

    response = _run_response(_run_record(requested=requested, effective=effective))

    assert response.execution_profile is not None
    assert response.execution_profile.model_dump() == {
        "model_name": PRIMARY_MODEL_REF,
        "thinking_enabled": True,
        "reasoning_effort": "medium",
        "supports_vision": True,
    }


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_run_snapshot_freezes_selected_model_and_effective_profile(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    default_model_id = uuid.uuid4()
    default_version_id = default_model_id
    selected_model_id = uuid.UUID(TEST_MODEL_REF)
    selected_version_id = selected_model_id
    selected_name = str(selected_model_id)
    try:
        async with seed.factory() as session, session.begin():
            agent_definition = (
                await session.execute(
                    text(
                        """SELECT definition_id,payload_checksum
                        FROM agents
                        WHERE id=:agent_id"""
                    ),
                    {"agent_id": seed.project_agent_id},
                )
            ).one()
            session.add(
                ThreadMetaRow(
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=str(seed.owner_a.user_id),
                    display_name="Execution profile",
                    status="idle",
                    metadata_json={},
                    project_id=seed.owner_a.project_id,
                    agent_asset_id=seed.project_agent_id,
                    agent_scope="project",
                )
            )
            for model_id, version_id, name, supports in (
                (
                    default_model_id,
                    default_version_id,
                    str(default_model_id),
                    False,
                ),
                (
                    selected_model_id,
                    selected_version_id,
                    selected_name,
                    True,
                ),
            ):
                assert version_id == model_id
                await seed_system_model_config(
                    session,
                    model_id=model_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    display_name=name,
                    provider_model=name,
                    supports_thinking=supports,
                    supports_reasoning_effort=supports,
                    supports_vision=supports,
                )
            await session.execute(
                text(
                    """UPDATE system_model_catalog_state
                    SET default_model_config_id=:model,revision=revision+1
                    WHERE id=1"""
                ),
                {"model": default_model_id},
            )
            generation = await CatalogStateRepository(session).read_generation()

        resolved_payload = AgentPayload(
            description="",
            soul="thread agent",
            model_ref=TEST_MODEL_REF,
            model_settings=AgentModelSettings(),
            tool_groups=(),
            skill_refs=(),
            mcp_version_ids=(),
            payload_schema_version=4,
        )
        resolved = ResolvedAgentSnapshot(
            kind=AssetKind.AGENT,
            scope=AssetScope.PROJECT,
            asset_id=seed.project_agent_id,
            version_id=agent_definition.definition_id,
            checksum=agent_payload_checksum(resolved_payload),
            catalog_generation=generation,
            dependency_version_ids=(),
            skill_version_ids=(),
            payload=resolved_payload,
        )
        repository = RunSnapshotRepository(
            seed.factory,
            model_catalog=SystemModelCatalogService(seed.factory),
        )
        async with seed.factory() as session, session.begin():
            created = await repository.create_run_with_snapshot_in_session(
                session,
                seed.owner_a,
                thread_id,
                PrivateRunCreate(
                    run_id=run_id,
                    execution_profile=RequestedRunExecutionProfile(
                        model_name=selected_name,
                        thinking_enabled=True,
                        reasoning_effort="high",
                    ),
                ),
                resolved,
            )

        assert created.model_name == selected_name
        assert created.kwargs[RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG] == []
        assert required_current_upload_snapshot_from_run_kwargs(created.kwargs) == ()
        _, effective = parse_persisted_run_execution_profile(created.kwargs[RUN_EXECUTION_PROFILE_KWARG])
        assert effective == EffectiveRunExecutionProfile(
            model_name=selected_name,
            thinking_enabled=True,
            reasoning_effort="high",
            supports_vision=True,
        )
        async with seed.factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT model_config_id,payload_checksum
                        FROM run_model_config_snapshots
                        WHERE run_id=:run_id AND purpose='lead'"""
                    ),
                    {"run_id": run_id},
                )
            ).one()
        assert row.model_config_id == selected_model_id
        assert row.payload_checksum == system_model_payload_checksum(
            model_id=selected_model_id,
            provider_adapter="vision_bridge_fake",
            provider_model=selected_name,
            settings=None,
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_vision=True,
        )

        incompatible_version_id = uuid.uuid4()
        incompatible_payload = AgentPayload(
            description=resolved.payload.description,
            soul=resolved.payload.soul,
            model_ref=resolved.payload.model_ref,
            model_settings=AgentModelSettings(max_tokens=64),
            tool_groups=resolved.payload.tool_groups,
            skill_refs=resolved.payload.skill_refs,
            mcp_version_ids=resolved.payload.mcp_version_ids,
            agents_instructions=resolved.payload.agents_instructions,
            identity=resolved.payload.identity,
            user_context=resolved.payload.user_context,
            payload_schema_version=4,
        )
        incompatible_checksum = agent_payload_checksum(incompatible_payload)
        async with seed.factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('deerflow.agent_definition_mutation_id',:agent_id,true)"),
                {"agent_id": str(seed.project_agent_id)},
            )
            await session.execute(
                text(
                    """UPDATE agents
                       SET definition_id=:version_id,
                           model_settings=CAST(:model_settings AS jsonb),
                           payload_checksum=:checksum,
                           revision=revision+1
                       WHERE id=:agent_id""",
                ),
                {
                    "version_id": incompatible_version_id,
                    "model_settings": '{"max_tokens":64}',
                    "checksum": incompatible_checksum,
                    "agent_id": seed.project_agent_id,
                },
            )
        incompatible_agent = ResolvedAgentSnapshot(
            kind=resolved.kind,
            scope=resolved.scope,
            asset_id=resolved.asset_id,
            version_id=incompatible_version_id,
            checksum=incompatible_checksum,
            catalog_generation=resolved.catalog_generation,
            dependency_version_ids=resolved.dependency_version_ids,
            skill_version_ids=resolved.skill_version_ids,
            payload=incompatible_payload,
        )
        with pytest.raises(
            PrivateWorkRunExecutionProfileUnsupported,
            match="selected model does not support",
        ) as incompatible:
            await repository.create_run_with_snapshot(
                seed.owner_a,
                thread_id,
                PrivateRunCreate(
                    run_id=str(uuid.uuid4()),
                    execution_profile=RequestedRunExecutionProfile(
                        model_name=selected_name,
                    ),
                ),
                incompatible_agent,
            )
        assert incompatible.value.code == "RUN_EXECUTION_PROFILE_UNSUPPORTED"
    finally:
        await seed.engine.dispose()
