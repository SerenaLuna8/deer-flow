from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text
from support.m4_private_threads import seed_m4_thread_database

from app.audit.models import resolve_system_audit_context
from app.audit.service import AuditService
from app.automations.dispatcher import AutomationDispatcher
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkAssetStale
from app.private_work.run_admission import (
    PersistedRunSnapshot,
    PrivateRunAdmissionService,
)
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRecord
from app.private_work.snapshot_repository import (
    RunAssetSnapshot,
    RunSnapshotRepository,
    agent_model_snapshot_purpose,
)
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.execution import (
    PermanentExecutionError,
    PrivateRunExecution,
    RunAgentPrivateExecutor,
    TransientExecutionError,
)
from app.reliability.jobs import JobClaim, JobScope
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
)
from app.system_runtime_settings.bootstrap import (
    bootstrap_system_runtime_policies,
)
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    LockedAgentRuntimePolicy,
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_settings import (
    SystemModelCatalogService,
    SystemModelMaterializer,
)
from app.system_settings.errors import SystemModelNotFound
from app.system_settings.models import CreateSystemModel, UpdateSystemModel
from deerflow.config.app_config import (
    AppConfig,
    peek_current_app_config,
    pop_current_app_config,
    push_current_app_config,
)
from deerflow.config.model_config import ModelConfig
from deerflow.runtime import RunStatus


def _private_context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id=str(uuid.uuid4()),
        )
    )


def _resolved_agent(*, model_ref: str = "default") -> ResolvedAgentSnapshot:
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    return ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.SYSTEM,
        asset_id=asset_id,
        version_id=version_id,
        checksum="a" * 64,
        catalog_generation=7,
        dependency_version_ids=(),
        payload=AgentPayload(
            description="test",
            soul="test",
            model_ref=model_ref,
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
    )


@pytest.mark.anyio
async def test_run_snapshot_catalog_writes_exact_model_name_in_the_caller_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _private_context()
    session = MagicMock(spec=AsyncSession)
    session.in_transaction.return_value = True
    session.flush = AsyncMock()
    now = datetime.now(UTC)
    catalog_calls: list[dict[str, object]] = []

    class ModelCatalog:
        async def admit_model_snapshot(self, passed_session, **kwargs):
            catalog_calls.append(
                {
                    "session": passed_session,
                    **kwargs,
                }
            )
            return SimpleNamespace(logical_name="db-exact-model")

    base_policy = default_policy_value(RuntimePolicySection.AGENT_RUNTIME)
    assert isinstance(base_policy, AgentRuntimePolicyValue)
    runtime_value = base_policy.model_copy(
        update={
            "max_recursion_limit": 77,
            "title": base_policy.title.model_copy(
                update={"model_name": "title-model"},
            ),
            "summarization": base_policy.summarization.model_copy(
                update={"model_name": "summary-model"},
            ),
            "memory": base_policy.memory.model_copy(
                update={"model_name": "memory-model"},
            ),
        }
    )
    locked_policy = LockedAgentRuntimePolicy(
        policy_version_id=uuid.uuid4(),
        schema_version=1,
        payload_checksum="b" * 64,
        value=runtime_value,
    )
    runtime_snapshot_calls: list[dict[str, object]] = []

    class RuntimePolicy:
        async def lock_agent_runtime_for_admission(self, passed_session):
            assert passed_session is session
            return locked_policy

        async def admit_run_snapshot(self, passed_session, **kwargs):
            runtime_snapshot_calls.append(
                {"session": passed_session, **kwargs},
            )

    class FakeRunRepository:
        record: PrivateRunRecord | None = None

        def __init__(self, passed_session) -> None:
            assert passed_session is session

        async def create(self, *, scope, thread_id, request):
            assert scope == context.resource_scope
            assert request.model_name is None
            self.__class__.record = PrivateRunRecord(
                run_id=request.run_id,
                thread_id=thread_id,
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                assistant_id=request.assistant_id,
                status=request.status,
                multitask_strategy=request.multitask_strategy,
                metadata=request.metadata,
                kwargs=request.kwargs,
                origin_trace_id=request.origin_trace_id,
                error=None,
                model_name=request.model_name,
                created_at=now,
                updated_at=now,
            )
            return self.__class__.record

        async def update_model_name(self, *, scope, run_id, model_name):
            assert scope == context.resource_scope
            assert self.__class__.record is not None
            assert run_id == self.__class__.record.run_id
            self.__class__.record = replace(
                self.__class__.record,
                model_name=model_name,
            )
            return True

        async def get(self, *, scope, run_id, lock=False):
            assert scope == context.resource_scope
            assert lock is True
            assert self.__class__.record is not None
            assert run_id == self.__class__.record.run_id
            return self.__class__.record

    monkeypatch.setattr(
        "app.private_work.snapshot_repository.PrivateRunRepository",
        FakeRunRepository,
    )
    repository = RunSnapshotRepository(
        object(),
        model_ref_resolver=SimpleNamespace(
            resolve=lambda _model_ref: (_ for _ in ()).throw(
                AssertionError("YAML model resolver must not run"),
            ),
        ),
        model_catalog=ModelCatalog(),
        runtime_policy=RuntimePolicy(),
    )
    repository.validate_agent_closure_in_session = AsyncMock(
        return_value=([], [], {}, {}),
    )
    request = PrivateRunCreate(
        run_id=str(uuid.uuid4()),
        origin_trace_id=context.request_id,
        kwargs={"config": {"recursion_limit": 10_000}},
    )
    resolved = _resolved_agent()

    admitted = await repository.create_run_with_snapshot_in_session(
        session,
        context,
        "thread-1",
        request,
        resolved,
    )

    assert admitted.model_name == "db-exact-model"
    assert admitted.kwargs["config"]["recursion_limit"] == 77
    assert [call["purpose"] for call in catalog_calls] == [
        "lead",
        "title",
        "summarization",
        "memory",
    ]
    assert [call["model_ref"] for call in catalog_calls] == [
        "default",
        "title-model",
        "summary-model",
        "memory-model",
    ]
    assert runtime_snapshot_calls == [
        {
            "session": session,
            "project_id": context.project_id,
            "owner_user_id": str(context.user_id),
            "thread_id": "thread-1",
            "run_id": request.run_id,
            "locked_policy": locked_policy,
        }
    ]
    assert session.add_all.call_count == 3
    session.flush.assert_awaited_once()


def test_private_and_automation_admission_both_receive_the_database_model_catalog() -> None:
    catalog = SimpleNamespace()

    private_admission = PrivateRunAdmissionService(
        object(),
        model_catalog=catalog,
    )
    automation_admission = AutomationDispatcher(
        object(),
        model_catalog=catalog,
    )

    assert private_admission._snapshots._model_catalog is catalog
    assert automation_admission._snapshots._model_catalog is catalog


def _worker_execution(
    context: PrivateWorkContext,
    *,
    model_name: str = "db-exact-model",
) -> tuple[PrivateRunExecution, object]:
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    run = PrivateRunRecord(
        run_id=str(uuid.uuid4()),
        thread_id="private-thread",
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        assistant_id=None,
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={},
        origin_trace_id=context.request_id,
        error=None,
        model_name=model_name,
        created_at=now,
        updated_at=now,
        job_id=job_id,
    )
    execution = PrivateRunExecution(
        context=context,
        run=run,
        snapshot=PersistedRunSnapshot(
            assets=(),
            mcp_grants=(),
            catalog_generation=1,
        ),
        checkpoint_namespace=run.run_id,
        graph_input={},
        command=None,
        config={},
        interrupt_before=None,
        interrupt_after=None,
        stream_mode=[],
        stream_subgraphs=False,
    )
    claim = JobClaim(
        job_id=job_id,
        attempt_id=uuid.uuid4(),
        lease_token="worker-lease",
        job_type="private_run",
        scope=JobScope(
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
        ),
        run_id=run.run_id,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        origin_trace_id=run.origin_trace_id,
    )

    class Authority:
        cancel_requested = False

        def __init__(self) -> None:
            self.claim = claim

        def bind_cancel_callback(self, callback) -> None:
            self.cancel_callback = callback

    return execution, Authority()


def _model(name: str) -> ModelConfig:
    return ModelConfig(
        name=name,
        display_name=name,
        description="test",
        use="langchain_openai.ChatOpenAI",
        model=f"provider-{name}",
    )


def _base_config() -> AppConfig:
    return AppConfig(
        sandbox={
            "use": "deerflow.sandbox.local:LocalSandboxProvider",
        },
    )


def test_runtime_policy_overlay_changes_only_database_owned_leaves() -> None:
    base_config = AppConfig(
        sandbox={
            "use": "deerflow.sandbox.local:LocalSandboxProvider",
        },
        title={
            "enabled": True,
            "max_words": 6,
            "max_chars": 60,
            "model_name": None,
            "prompt_template": "operator-owned title prompt",
        },
        tool_output={
            "enabled": True,
            "externalize_min_chars": 12_000,
            "preview_head_chars": 2_000,
            "preview_tail_chars": 1_000,
            "fallback_max_chars": 30_000,
            "fallback_head_chars": 8_000,
            "fallback_tail_chars": 3_000,
            "storage_subdir": ".operator-tool-results",
            "exempt_tools": ["read_file"],
            "tool_overrides": {},
        },
    )

    class RuntimePolicy:
        def model_dump(self, *, mode: str):
            assert mode == "python"
            return {
                "max_recursion_limit": 77,
                "title": {
                    "enabled": False,
                    "max_words": 8,
                    "max_chars": 80,
                    "model_name": None,
                },
                "tool_output": {
                    "enabled": False,
                    "externalize_min_chars": 9_000,
                    "preview_head_chars": 900,
                    "preview_tail_chars": 450,
                    "fallback_max_chars": 12_000,
                    "fallback_head_chars": 4_000,
                    "fallback_tail_chars": 2_000,
                    "exempt_tools": ["read_file", "grep"],
                    "tool_overrides": {"bash": 5_000},
                },
            }

    runtime_config = base_config.with_runtime_policy(RuntimePolicy())

    assert runtime_config is not base_config
    assert runtime_config.max_recursion_limit == 77
    assert runtime_config.title.enabled is False
    assert runtime_config.title.max_words == 8
    assert runtime_config.title.prompt_template == "operator-owned title prompt"
    assert runtime_config.tool_output.enabled is False
    assert runtime_config.tool_output.storage_subdir == ".operator-tool-results"
    assert runtime_config.tool_output.tool_overrides == {"bash": 5_000}
    assert base_config.max_recursion_limit == 1_000
    assert base_config.title.enabled is True
    assert base_config.tool_output.enabled is True

    with pytest.raises(
        ValueError,
        match="deployment-owned runtime policy field",
    ):
        base_config.with_runtime_policy(
            {"title": {"prompt_template": "database must not own prompts"}},
        )

    with pytest.raises(
        ValueError,
        match="deployment-owned runtime policy field",
    ):
        base_config.with_runtime_policy(
            {"tool_output": {"storage_subdir": ".database-owned"}},
        )


@pytest.mark.anyio
async def test_worker_uses_exact_materialized_runtime_config_and_restores_contextvar() -> None:
    context = _private_context()
    execution, authority = _worker_execution(context)
    base_config = _base_config()
    outer_config = base_config.with_runtime_models((_model("outer-model"),))
    runtime_model = _model("db-exact-model")
    title_model = _model("title-exact-model")
    summary_model = _model("summary-exact-model")
    memory_model = _model("memory-exact-model")
    captured: dict[str, object] = {}

    class Materializer:
        async def materialize_snapshot(self, **kwargs):
            captured.setdefault("materializer", []).append(kwargs)
            return {
                "lead": runtime_model,
                "title": title_model,
                "summarization": summary_model,
                "memory": memory_model,
            }[kwargs["purpose"]]

    class RuntimePolicy:
        def model_dump(self, *, mode: str):
            assert mode == "python"
            return {
                "max_recursion_limit": 73,
                "token_budget": {
                    "enabled": True,
                    "max_tokens": 12_000,
                    "max_input_tokens": 8_000,
                    "max_output_tokens": 4_000,
                    "warn_threshold": 0.7,
                    "hard_stop_threshold": 0.9,
                },
                "title": {"model_name": "title-exact-model"},
                "summarization": {"model_name": "summary-exact-model"},
                "memory": {"model_name": "memory-exact-model"},
            }

    class RuntimePolicyMaterializer:
        async def materialize_run_snapshot(self, **kwargs):
            captured["runtime_policy_materializer"] = kwargs
            return RuntimePolicy()

    class PrivateRuntime:
        model_ref = "default"
        skill_root = None

        async def aclose(self) -> None:
            captured["closed"] = True

    class AssetRuntime:
        async def materialize(self, passed_context, admitted, *, authorization_boundary):
            assert passed_context is context
            assert admitted.run.run_id == execution.run.run_id
            assert authorization_boundary.execution_job_id == authority.claim.job_id
            captured["asset_context_config"] = peek_current_app_config()
            return PrivateRuntime()

    class ProjectCheckpointer:
        def for_context(self, passed_context):
            assert passed_context is context
            return SimpleNamespace(
                set_authorization_boundary=lambda _boundary: None,
            )

    async def runner(_bridge, run_manager, record, *, ctx, **_kwargs):
        captured["runner_context_config"] = peek_current_app_config()
        captured["run_context_config"] = ctx.app_config
        await run_manager.set_status(record.run_id, RunStatus.success)

    executor = RunAgentPrivateExecutor(
        object(),
        app_config=base_config,
        bridge=object(),
        project_checkpointer=ProjectCheckpointer(),
        store=object(),
        event_store=object(),
        asset_runtime=AssetRuntime(),
        model_materializer=Materializer(),
        runtime_policy_materializer=RuntimePolicyMaterializer(),
        agent_factory=object(),
        runner=runner,
    )

    push_current_app_config(outer_config)
    try:
        result = await executor.execute(execution, authority)

        assert result.status == "succeeded"
        runtime_config = captured["run_context_config"]
        assert runtime_config is captured["asset_context_config"]
        assert runtime_config is captured["runner_context_config"]
        assert runtime_config is not base_config
        assert [model.name for model in runtime_config.models] == [
            "db-exact-model",
            "title-exact-model",
            "summary-exact-model",
            "memory-exact-model",
        ]
        assert runtime_config.max_recursion_limit == 73
        assert runtime_config.token_budget.enabled is True
        assert runtime_config.token_budget.max_tokens == 12_000
        assert base_config.models == []
        assert base_config.max_recursion_limit == 1_000
        assert base_config.token_budget.enabled is False
        assert peek_current_app_config() is outer_config
        assert captured["materializer"] == [
            {
                "project_id": context.project_id,
                "owner_user_id": str(context.user_id),
                "run_id": execution.run.run_id,
                "purpose": purpose,
            }
            for purpose in ("lead", "title", "summarization", "memory")
        ]
        assert captured["runtime_policy_materializer"] == {
            "project_id": context.project_id,
            "owner_user_id": str(context.user_id),
            "run_id": execution.run.run_id,
        }
        assert captured["closed"] is True
    finally:
        pop_current_app_config()


@pytest.mark.anyio
async def test_worker_materializes_every_frozen_delegate_agent_model() -> None:
    context = _private_context()
    delegate_version_id = uuid.uuid4()
    execution, authority = _worker_execution(context)
    execution = replace(
        execution,
        snapshot=PersistedRunSnapshot(
            assets=(
                RunAssetSnapshot(
                    asset_kind=AssetKind.AGENT.value,
                    dependency_order=0,
                    asset_scope=AssetScope.SYSTEM.value,
                    asset_id=uuid.uuid4(),
                    version_id=uuid.uuid4(),
                    payload_checksum="a" * 64,
                    catalog_generation=1,
                ),
                RunAssetSnapshot(
                    asset_kind=AssetKind.AGENT.value,
                    dependency_order=1,
                    asset_scope=AssetScope.PROJECT.value,
                    asset_id=uuid.uuid4(),
                    version_id=delegate_version_id,
                    payload_checksum="b" * 64,
                    catalog_generation=1,
                ),
            ),
            mcp_grants=(),
            catalog_generation=1,
        ),
    )
    base_config = _base_config()
    lead_model = _model("db-exact-model")
    delegate_model = _model("delegate-exact-model")
    materialized_purposes: list[str] = []

    class Materializer:
        async def materialize_snapshot(self, **kwargs):
            purpose = kwargs["purpose"]
            materialized_purposes.append(purpose)
            if purpose == "lead":
                return lead_model
            if purpose == agent_model_snapshot_purpose(delegate_version_id):
                return delegate_model
            raise AssertionError(f"unexpected model purpose: {purpose}")

    class PrivateRuntime:
        model_ref = "default"
        skill_root = None

        async def aclose(self) -> None:
            return None

    class AssetRuntime:
        async def materialize(self, *_args, **kwargs):
            captured["delegate_model_names"] = kwargs.get("delegate_model_names")
            return PrivateRuntime()

    class ProjectCheckpointer:
        def for_context(self, _context):
            return SimpleNamespace(
                set_authorization_boundary=lambda _boundary: None,
            )

    captured: dict[str, object] = {}

    async def runner(_bridge, run_manager, record, *, ctx, **_kwargs):
        captured["model_names"] = tuple(model.name for model in ctx.app_config.models)
        await run_manager.set_status(record.run_id, RunStatus.success)

    executor = RunAgentPrivateExecutor(
        object(),
        app_config=base_config,
        bridge=object(),
        project_checkpointer=ProjectCheckpointer(),
        store=object(),
        event_store=object(),
        asset_runtime=AssetRuntime(),
        model_materializer=Materializer(),
        agent_factory=object(),
        runner=runner,
    )

    result = await executor.execute(execution, authority)

    assert result.status == "succeeded"
    assert materialized_purposes == [
        "lead",
        agent_model_snapshot_purpose(delegate_version_id),
    ]
    assert captured["model_names"] == (
        "db-exact-model",
        "delegate-exact-model",
    )
    assert captured["delegate_model_names"] == {
        delegate_version_id: "delegate-exact-model",
    }


@pytest.mark.anyio
async def test_worker_model_materialization_failure_is_terminal_and_does_not_enter_runtime() -> None:
    context = _private_context()
    execution, authority = _worker_execution(context)
    base_config = _base_config()
    outer_config = base_config.with_runtime_models((_model("outer-model"),))
    asset_runtime = SimpleNamespace(materialize=AsyncMock())

    class UnavailableMaterializer:
        async def materialize_snapshot(self, **_kwargs):
            raise RuntimeError("sensitive backend detail")

    executor = RunAgentPrivateExecutor(
        object(),
        app_config=base_config,
        bridge=object(),
        project_checkpointer=object(),
        store=object(),
        event_store=object(),
        asset_runtime=asset_runtime,
        model_materializer=UnavailableMaterializer(),
        agent_factory=object(),
    )

    push_current_app_config(outer_config)
    try:
        with pytest.raises(PermanentExecutionError) as raised:
            await executor.execute(execution, authority)

        assert raised.value.public_error_code == "RUN_ASSET_STALE"
        assert "sensitive backend detail" not in str(raised.value)
        asset_runtime.materialize.assert_not_awaited()
        assert peek_current_app_config() is outer_config
    finally:
        pop_current_app_config()


@pytest.mark.anyio
async def test_worker_treats_exact_asset_runtime_staleness_as_terminal() -> None:
    context = _private_context()
    execution, authority = _worker_execution(context)
    base_config = _base_config()

    class Materializer:
        async def materialize_snapshot(self, **_kwargs):
            return _model("db-exact-model")

    class StaleAssetRuntime:
        async def materialize(self, *_args, **_kwargs):
            raise PrivateWorkAssetStale("sensitive-stale-detail")

    executor = RunAgentPrivateExecutor(
        object(),
        app_config=base_config,
        bridge=object(),
        project_checkpointer=object(),
        store=object(),
        event_store=object(),
        asset_runtime=StaleAssetRuntime(),
        model_materializer=Materializer(),
        agent_factory=object(),
    )

    with pytest.raises(PermanentExecutionError) as raised:
        await executor.execute(execution, authority)

    assert raised.value.public_error_code == "RUN_ASSET_STALE"
    assert "sensitive-stale-detail" not in str(raised.value)


@pytest.mark.anyio
async def test_worker_restores_contextvar_when_runtime_fails_after_materialization() -> None:
    context = _private_context()
    execution, authority = _worker_execution(context)
    base_config = _base_config()
    outer_config = base_config.with_runtime_models((_model("outer-model"),))

    class Materializer:
        async def materialize_snapshot(self, **_kwargs):
            return _model("db-exact-model")

    class FailingAssetRuntime:
        async def materialize(self, *_args, **_kwargs):
            assert peek_current_app_config().models[0].name == "db-exact-model"
            raise RuntimeError("runtime detail")

    executor = RunAgentPrivateExecutor(
        object(),
        app_config=base_config,
        bridge=object(),
        project_checkpointer=object(),
        store=object(),
        event_store=object(),
        asset_runtime=FailingAssetRuntime(),
        model_materializer=Materializer(),
        agent_factory=object(),
    )

    push_current_app_config(outer_config)
    try:
        with pytest.raises(TransientExecutionError) as raised:
            await executor.execute(execution, authority)

        assert raised.value.public_error_code == "PRIVATE_RUN_EXECUTION_FAILED"
        assert "runtime detail" not in str(raised.value)
        assert peek_current_app_config() is outer_config
    finally:
        pop_current_app_config()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_run_admission_pins_exact_model_version_and_worker_materializes_it(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"system-model-run-{uuid.uuid4()}"
    second_thread_id = f"system-model-run-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    second_run_id = str(uuid.uuid4())
    try:
        await bootstrap_system_runtime_policies(seed.factory)
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE users SET system_role='system_admin' WHERE id=:user_id",
                ),
                {"user_id": str(seed.owner_a.user_id)},
            )
        admin = resolve_system_audit_context(
            SimpleNamespace(
                id=seed.owner_a.user_id,
                system_role="system_admin",
            ),
            request_id="system-model-admin",
        )
        catalog = SystemModelCatalogService(seed.factory)
        created = await catalog.create_model(
            admin,
            CreateSystemModel(
                logical_name="test-model",
                display_name="Database model",
                description="version one",
                status="active",
                provider_adapter="codex_cli",
                provider_model="provider-version-one",
                settings={},
                supports_thinking=False,
                supports_reasoning_effort=False,
                supports_vision=False,
                credential_id=None,
                credential_version_id=None,
                credential_env_key=None,
            ),
        )
        runtime_policy = SystemRuntimePolicyService(
            seed.factory,
            AuditService(
                seed.factory,
                AuditHmacKeyring.from_environment(),
            ),
        )
        runtime_value = default_policy_value(
            RuntimePolicySection.AGENT_RUNTIME,
        )
        assert isinstance(runtime_value, AgentRuntimePolicyValue)
        await runtime_policy.update_policy(
            admin,
            RuntimePolicySection.AGENT_RUNTIME,
            expected_revision=1,
            value=runtime_value.model_copy(
                update={
                    "title": runtime_value.title.model_copy(
                        update={"model_name": "test-model"},
                    )
                }
            ),
        )
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=second_thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        admitted = await PrivateRunAdmissionService(
            seed.factory,
            model_catalog=catalog,
            runtime_policy=runtime_policy,
        ).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                run_id=run_id,
                origin_trace_id=seed.owner_a.request_id,
            ),
        )

        assert admitted.run.model_name == "test-model"
        async with seed.factory() as session:
            pinned = (
                await session.execute(
                    text(
                        """SELECT r.model_name,s.logical_name,
                        s.model_config_version_id,v.provider_model
                        FROM runs r
                        JOIN run_model_config_snapshots s
                          ON s.project_id=r.project_id
                         AND s.owner_user_id=r.owner_user_id
                         AND s.run_id=r.run_id
                         AND s.purpose='lead'
                        JOIN system_model_config_versions v
                          ON v.id=s.model_config_version_id
                         AND v.model_config_id=s.model_config_id
                        WHERE r.run_id=:run_id""",
                    ),
                    {"run_id": run_id},
                )
            ).one()
        assert tuple(pinned) == (
            "test-model",
            "test-model",
            created.current_version.id,
            "provider-version-one",
        )

        updated = await catalog.update_model(
            admin,
            created.id,
            UpdateSystemModel(
                display_name="Database model",
                description="version two",
                provider_adapter="codex_cli",
                provider_model="provider-version-two",
                settings={},
                supports_thinking=False,
                supports_reasoning_effort=False,
                supports_vision=False,
                credential_id=None,
                credential_version_id=None,
                credential_env_key=None,
                sort_order=0,
            ),
            expected_revision=created.revision,
        )
        assert updated.current_version.id != created.current_version.id

        second_admitted = await PrivateRunAdmissionService(
            seed.factory,
            model_catalog=catalog,
            runtime_policy=runtime_policy,
        ).admit(
            seed.owner_a,
            second_thread_id,
            PrivateRunCreate(
                run_id=second_run_id,
                origin_trace_id=seed.owner_a.request_id,
            ),
        )
        assert second_admitted.run.model_name == "test-model"

        runtime = await SystemModelMaterializer(
            seed.factory,
        ).materialize_snapshot(
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            run_id=run_id,
            purpose="lead",
        )

        assert runtime.name == "test-model"
        assert runtime.model == "provider-version-one"
        assert runtime.use == ("deerflow.models.openai_codex_provider:CodexChatModel")
        title_v1 = await SystemModelMaterializer(
            seed.factory,
        ).materialize_snapshot(
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            run_id=run_id,
            purpose="title",
        )
        title_v2 = await SystemModelMaterializer(
            seed.factory,
        ).materialize_snapshot(
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            run_id=second_run_id,
            purpose="title",
        )
        assert title_v1.model == "provider-version-one"
        assert title_v2.model == "provider-version-two"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_model_catalog_failure_rolls_back_the_partial_run(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"system-model-fail-closed-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())

    class MissingCatalog:
        async def admit_model_snapshot(self, session, **_kwargs):
            assert session.in_transaction()
            raise SystemModelNotFound("internal-model-request")

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        with pytest.raises(PrivateWorkAssetStale) as raised:
            await PrivateRunAdmissionService(
                seed.factory,
                model_catalog=MissingCatalog(),
            ).admit(
                seed.owner_a,
                thread_id,
                PrivateRunCreate(
                    run_id=run_id,
                    origin_trace_id=seed.owner_a.request_id,
                ),
            )

        assert raised.value.request_id == seed.owner_a.request_id
        assert "internal-model-request" not in str(raised.value)
        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM runs WHERE run_id=:run_id),
                        (SELECT count(*) FROM run_asset_versions WHERE run_id=:run_id),
                        (SELECT count(*) FROM run_model_config_snapshots WHERE run_id=:run_id)""",
                    ),
                    {"run_id": run_id},
                )
            ).one()
        assert tuple(counts) == (0, 0, 0)
    finally:
        await seed.engine.dispose()
