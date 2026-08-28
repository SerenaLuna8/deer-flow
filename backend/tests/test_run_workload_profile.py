from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from support.private_thread_seed import seed_private_thread_database

from app.audit.service import AuditService
from app.gateway.private_work_schemas import PrivateRunCreateRequest
from app.gateway.routers.private_work import _run_response
from app.private_work.http_runtime import start_private_run
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRecord
from app.private_work.sandbox_files import RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.private_work.workload_profile import (
    RUN_WORKLOAD_PROFILE_KWARG,
    EffectiveRunWorkloadProfile,
    RequestedRunWorkloadProfile,
    RunWorkloadProfileUnsupported,
    effective_run_workload_profile_from_kwargs,
    freeze_admitted_run_workload_profile,
    parse_persisted_run_workload_profile,
    persisted_run_workload_profile,
    resolve_admitted_run_workload_profile,
)
from app.projects.context import resolve_project_context_in_transaction
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.models import AssetKind, AssetSelection
from app.shared_assets.resolver import ProjectAssetResolver
from app.system_runtime_settings.bootstrap import bootstrap_system_runtime_policies
from app.system_runtime_settings.service import SystemRuntimePolicyService
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.system_runtime_settings import RunRuntimePolicySnapshotRow


def test_private_run_request_exposes_only_a_typed_top_level_workload_choice() -> None:
    default_request = PrivateRunCreateRequest.model_validate({})
    research_request = PrivateRunCreateRequest.model_validate(
        {
            "workload_profile": "research",
            "metadata": {"workload_profile": "research"},
            "config": {"context": {"workload_profile": "research"}},
            "context": {"workload_profile": "research"},
        }
    )

    assert default_request.workload_profile == "interactive"
    assert research_request.workload_profile == "research"
    assert research_request.metadata == {}
    assert research_request.config == {"context": {}}
    assert research_request.context == {}

    with pytest.raises(ValidationError):
        PrivateRunCreateRequest.model_validate({"workload_profile": "unknown"})


def test_run_response_exposes_the_server_frozen_effective_workload_profile() -> None:
    now = datetime.now(UTC)
    record = PrivateRunRecord(
        run_id=str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        assistant_id=str(uuid.uuid4()),
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={
            RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
                RequestedRunWorkloadProfile(name="interactive"),
                EffectiveRunWorkloadProfile(name="research"),
            )
        },
        origin_trace_id="a" * 32,
        error=None,
        model_name=None,
        created_at=now,
        updated_at=now,
    )

    response = _run_response(record)

    assert response.workload_profile == "research"


@pytest.mark.asyncio
async def test_private_run_launcher_passes_the_typed_workload_choice_to_admission() -> None:
    captured: dict[str, object] = {}
    now = datetime.now(UTC)
    run = SimpleNamespace(
        run_id=str(uuid.uuid4()),
        assistant_id=str(uuid.uuid4()),
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={},
        owner_user_id=str(uuid.uuid4()),
        created_at=now,
        updated_at=now,
        model_name=None,
    )

    class Admission:
        async def admit(
            self,
            context: object,
            thread_id: str,
            request: object,
            *,
            server_context: object,
        ) -> object:
            captured.update(
                context=context,
                thread_id=thread_id,
                request=request,
                server_context=server_context,
            )
            return SimpleNamespace(
                run=run,
                thread_id=thread_id,
                opaque_runtime_scope=object(),
                inbound_delivery_replay=False,
            )

    body = PrivateRunCreateRequest.model_validate(
        {
            "input": {"messages": [{"role": "user", "content": "research"}]},
            "workload_profile": "research",
        }
    )
    context = SimpleNamespace(
        request_id="workload-profile-launch",
        resource_scope=object(),
    )

    await start_private_run(
        body,
        str(uuid.uuid4()),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        context,
        admission_service=Admission(),
    )

    assert captured["request"].workload_profile == RequestedRunWorkloadProfile(name="research")


@pytest.mark.asyncio
async def test_admission_ignores_research_claims_outside_the_typed_workload_field() -> None:
    captured: dict[str, object] = {}
    now = datetime.now(UTC)
    run = SimpleNamespace(
        run_id=str(uuid.uuid4()),
        assistant_id=str(uuid.uuid4()),
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={},
        owner_user_id=str(uuid.uuid4()),
        created_at=now,
        updated_at=now,
        model_name=None,
    )

    class Admission:
        async def admit(
            self,
            context: object,
            thread_id: str,
            request: PrivateRunCreate,
            *,
            server_context: object,
        ) -> object:
            effective, frozen_kwargs = freeze_admitted_run_workload_profile(
                request.kwargs,
                requested=request.workload_profile,
                policy_schema_version=1,
            )
            captured.update(
                context=context,
                thread_id=thread_id,
                request=request,
                server_context=server_context,
                effective=effective,
                frozen_kwargs=frozen_kwargs,
            )
            return SimpleNamespace(
                run=run,
                thread_id=thread_id,
                opaque_runtime_scope=object(),
                inbound_delivery_replay=False,
            )

    opaque_tool_args = '{"workload_profile":"research"}'
    body = PrivateRunCreateRequest.model_validate(
        {
            "input": {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "model output requests research",
                        "tool_calls": [
                            {
                                "id": "forged-call",
                                "name": "task",
                                "args": {"workload_profile": "research"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": "ordinary interactive request",
                        "additional_kwargs": {
                            "workload_profile": "research",
                            "model_output": {"workload_profile": "research"},
                            "opaque_tool_args": opaque_tool_args,
                        },
                    },
                ]
            },
            "command": {"resume": {"tool_args": {"workload_profile": "research"}}},
            "metadata": {"workload_profile": "research"},
            "config": {
                "context": {"workload_profile": "research"},
                "metadata": {"workload_profile": "research"},
            },
            "context": {"workload_profile": "research"},
        }
    )
    context = SimpleNamespace(
        request_id="workload-authority-acceptance",
        resource_scope=object(),
    )

    await start_private_run(
        body,
        str(uuid.uuid4()),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        context,
        admission_service=Admission(),
    )

    admitted_request = captured["request"]
    assert admitted_request.workload_profile == RequestedRunWorkloadProfile(name="interactive")
    assert captured["effective"] == EffectiveRunWorkloadProfile(name="interactive")
    assert captured["frozen_kwargs"][RUN_WORKLOAD_PROFILE_KWARG] == (
        persisted_run_workload_profile(
            RequestedRunWorkloadProfile(name="interactive"),
            EffectiveRunWorkloadProfile(name="interactive"),
        )
    )
    assert body.metadata == {}
    assert body.context == {}
    assert body.config == {"context": {}, "metadata": {}}
    assert body.input == {
        "messages": [
            {
                "role": "user",
                "content": "ordinary interactive request",
                "additional_kwargs": {
                    "model_output": {},
                    "opaque_tool_args": opaque_tool_args,
                },
            }
        ]
    }
    assert admitted_request.kwargs["command"] == {"resume": {"tool_args": {"workload_profile": "research"}}}


def test_run_idempotency_includes_the_requested_workload_profile() -> None:
    now = datetime.now(UTC)
    run_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    record = PrivateRunRecord(
        run_id=run_id,
        thread_id=thread_id,
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        assistant_id=str(uuid.uuid4()),
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={
            "input": {"messages": []},
            RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG: [],
            RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
                RequestedRunWorkloadProfile(name="research"),
                EffectiveRunWorkloadProfile(name="research"),
            ),
        },
        origin_trace_id="a" * 32,
        error=None,
        model_name=None,
        created_at=now,
        updated_at=now,
    )

    assert PrivateRunAdmissionService._is_same_request(
        record,
        thread_id=thread_id,
        request=PrivateRunCreate(
            run_id=run_id,
            kwargs={"input": {"messages": []}},
            workload_profile=RequestedRunWorkloadProfile(name="research"),
        ),
    )
    assert not PrivateRunAdmissionService._is_same_request(
        record,
        thread_id=thread_id,
        request=PrivateRunCreate(
            run_id=run_id,
            kwargs={"input": {"messages": []}},
            workload_profile=RequestedRunWorkloadProfile(),
        ),
    )


def test_admission_resolves_research_and_inherits_continuations() -> None:
    requested_research = RequestedRunWorkloadProfile(name="research")
    inherited_interactive = EffectiveRunWorkloadProfile(name="interactive")

    assert resolve_admitted_run_workload_profile(
        requested=requested_research,
        policy_schema_version=1,
    ) == EffectiveRunWorkloadProfile(name="research")
    assert (
        resolve_admitted_run_workload_profile(
            requested=requested_research,
            policy_schema_version=1,
            inherited_effective=inherited_interactive,
        )
        == inherited_interactive
    )

    with pytest.raises(RunWorkloadProfileUnsupported):
        resolve_admitted_run_workload_profile(
            requested=requested_research,
            policy_schema_version=3,
        )


def test_admission_always_freezes_the_workload_profile_kwarg() -> None:
    original = {"input": {"messages": []}}
    effective, frozen_kwargs = freeze_admitted_run_workload_profile(
        original,
        requested=RequestedRunWorkloadProfile(name="research"),
        policy_schema_version=1,
    )

    assert effective == EffectiveRunWorkloadProfile(name="research")
    assert frozen_kwargs[RUN_WORKLOAD_PROFILE_KWARG] == persisted_run_workload_profile(
        RequestedRunWorkloadProfile(name="research"),
        effective,
    )
    assert original == {"input": {"messages": []}}

    with pytest.raises(RunWorkloadProfileUnsupported):
        freeze_admitted_run_workload_profile(
            {RUN_WORKLOAD_PROFILE_KWARG: {}},
            requested=RequestedRunWorkloadProfile(),
            policy_schema_version=1,
        )


@pytest.mark.asyncio
async def test_continuation_reads_the_source_runs_frozen_effective_profile() -> None:
    source_kwargs = {
        RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
            RequestedRunWorkloadProfile(name="research"),
            EffectiveRunWorkloadProfile(name="research"),
        )
    }

    class Result:
        def one_or_none(self) -> object:
            return SimpleNamespace(
                kwargs_json=source_kwargs,
                schema_version=1,
            )

    class Session:
        async def execute(self, _statement: object) -> Result:
            return Result()

    context = SimpleNamespace(
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )

    assert await RunSnapshotRepository._continuation_workload_profile(
        Session(),
        context,
        thread_id=str(uuid.uuid4()),
        source_run_id=str(uuid.uuid4()),
    ) == EffectiveRunWorkloadProfile(name="research")


def test_interactive_workload_profile_round_trips_through_server_owned_kwargs() -> None:
    requested = RequestedRunWorkloadProfile()
    effective = EffectiveRunWorkloadProfile(name="interactive")

    kwargs = {
        RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
            requested,
            effective,
        )
    }

    assert requested.name == "interactive"
    assert (
        effective_run_workload_profile_from_kwargs(
            kwargs,
            policy_schema_version=1,
        )
        == effective
    )


def test_schema_v1_requires_a_frozen_workload_profile() -> None:
    with pytest.raises(RunWorkloadProfileUnsupported):
        effective_run_workload_profile_from_kwargs(
            {},
            policy_schema_version=1,
        )


@pytest.mark.parametrize("policy_schema_version", [0, 2, 5, 7, True])
def test_unknown_policy_schema_version_fails_closed(
    policy_schema_version: object,
) -> None:
    kwargs = {
        RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
            RequestedRunWorkloadProfile(),
            EffectiveRunWorkloadProfile(name="interactive"),
        )
    }

    with pytest.raises(RunWorkloadProfileUnsupported):
        effective_run_workload_profile_from_kwargs(
            kwargs,
            policy_schema_version=policy_schema_version,
        )


@pytest.mark.parametrize(
    "persisted_value",
    [
        None,
        {},
        {"requested": {"name": "interactive"}},
        {
            "requested": {"name": "interactive"},
            "effective": {"name": "unknown"},
        },
    ],
)
def test_malformed_frozen_workload_profile_fails_closed(
    persisted_value: object,
) -> None:
    with pytest.raises(RunWorkloadProfileUnsupported):
        effective_run_workload_profile_from_kwargs(
            {RUN_WORKLOAD_PROFILE_KWARG: persisted_value},
            policy_schema_version=1,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_freezes_workload_and_continuation_inherits_source_effective(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = str(uuid.uuid4())
    source_run_id = str(uuid.uuid4())
    continuation_run_id = str(uuid.uuid4())
    try:
        assert await bootstrap_system_runtime_policies(seed.factory) == 1
        runtime_policy = SystemRuntimePolicyService(
            seed.factory,
            AuditService(
                seed.factory,
                AuditHmacKeyring(
                    active_key_id="workload-profile-test",
                    _keys={"workload-profile-test": b"w" * 32},
                ),
            ),
        )
        resolver = ProjectAssetResolver(seed.factory)
        snapshots = RunSnapshotRepository(
            seed.factory,
            runtime_policy=runtime_policy,
        )

        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            project_context = await resolve_project_context_in_transaction(
                session,
                seed.owner_a.user_id,
                seed.owner_a.project_id,
                seed.owner_a.request_id,
            )
            resolved = await resolver.resolve_run_asset_closure_in_session(
                session,
                project_context,
                AssetSelection(AssetKind.AGENT, seed.project_agent_id),
            )
            source = await snapshots.create_run_with_snapshot_in_session(
                session,
                seed.owner_a,
                thread_id,
                PrivateRunCreate(
                    run_id=source_run_id,
                    workload_profile=RequestedRunWorkloadProfile(name="research"),
                ),
                resolved,
                admit_memory=False,
            )

        async with seed.factory() as session, session.begin():
            continuation = await snapshots.create_run_with_snapshot_in_session(
                session,
                seed.owner_a,
                thread_id,
                PrivateRunCreate(
                    run_id=continuation_run_id,
                    follow_up_to_run_id=source_run_id,
                    workload_profile=RequestedRunWorkloadProfile(name="interactive"),
                ),
                resolved,
                continuation_source_run_id=source_run_id,
                admit_memory=False,
            )

        assert source.run_id == source_run_id
        assert continuation.run_id == continuation_run_id
        async with seed.factory() as session:
            rows = (
                await session.execute(
                    select(
                        RunRow.run_id,
                        RunRow.kwargs_json,
                        RunRuntimePolicySnapshotRow.schema_version,
                    )
                    .join(
                        RunRuntimePolicySnapshotRow,
                        RunRuntimePolicySnapshotRow.run_id == RunRow.run_id,
                    )
                    .where(RunRow.run_id.in_((source_run_id, continuation_run_id)))
                )
            ).all()
        by_run = {run_id: (dict(kwargs_json), int(schema_version)) for run_id, kwargs_json, schema_version in rows}
        assert set(by_run) == {source_run_id, continuation_run_id}
        source_kwargs, source_schema = by_run[source_run_id]
        continuation_kwargs, continuation_schema = by_run[continuation_run_id]
        assert source_schema == continuation_schema == 1
        assert parse_persisted_run_workload_profile(source_kwargs[RUN_WORKLOAD_PROFILE_KWARG]) == (
            RequestedRunWorkloadProfile(name="research"),
            EffectiveRunWorkloadProfile(name="research"),
        )
        assert parse_persisted_run_workload_profile(continuation_kwargs[RUN_WORKLOAD_PROFILE_KWARG]) == (
            RequestedRunWorkloadProfile(name="interactive"),
            EffectiveRunWorkloadProfile(name="research"),
        )
    finally:
        await seed.engine.dispose()
