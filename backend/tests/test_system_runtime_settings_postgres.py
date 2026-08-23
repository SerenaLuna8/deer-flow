from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.system_model_seed import seed_system_model_config

from app.audit.models import resolve_system_audit_context
from app.audit.service import AuditService
from app.reliability.owner_refs import AuditHmacKeyring
from app.system_runtime_settings.bootstrap import (
    bootstrap_system_runtime_policies,
)
from app.system_runtime_settings.errors import (
    SystemRuntimePolicyConflict,
    SystemRuntimePolicyInvalid,
    SystemRuntimePolicyUnavailable,
)
from app.system_runtime_settings.materializer import (
    SystemRuntimePolicyMaterializer,
)
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    MemoryDocumentPolicy,
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_runtime_settings.validation import canonical_policy_payload_for_schema
from deerflow.persistence.system_runtime_settings import (
    RunRuntimePolicySnapshotRow,
    SystemRuntimePolicyVersionRow,
)


@pytest.fixture(autouse=True)
def _audit_hmac_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACT_WEAVE_AUDIT_ACTIVE_KEY_ID", "test-audit-v1")
    monkeypatch.setenv(
        "ACT_WEAVE_AUDIT_KEYRING_JSON",
        '{"test-audit-v1":"YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="}',
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_current_v3_policy_remains_admissible_after_v4_deploy(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        assert await bootstrap_system_runtime_policies(factory) == 1
        canonical_v3 = canonical_policy_payload_for_schema(
            RuntimePolicySection.AGENT_RUNTIME,
            {},
            schema_version=3,
        )
        version_id = uuid.uuid4()
        async with engine.begin() as connection:
            current = (
                await connection.execute(
                    text(
                        """SELECT current_version_id,updated_by_user_id
                             FROM system_runtime_policies
                            WHERE section='agent_runtime'"""
                    )
                )
            ).one()
            await connection.execute(
                text(
                    """INSERT INTO system_runtime_policy_versions
                       (id,section,version_number,schema_version,value,
                        payload_checksum,supersedes_version_id,created_by_user_id)
                       VALUES (:id,'agent_runtime',2,3,CAST(:value AS jsonb),
                               :checksum,:supersedes,:actor)"""
                ),
                {
                    "id": version_id,
                    "value": json.dumps(canonical_v3.value),
                    "checksum": canonical_v3.checksum,
                    "supersedes": current.current_version_id,
                    "actor": current.updated_by_user_id,
                },
            )
            await connection.execute(
                text(
                    """UPDATE system_runtime_policies
                          SET current_version_id=:version,revision=2
                        WHERE section='agent_runtime'"""
                ),
                {"version": version_id},
            )
            await connection.execute(
                text("UPDATE system_runtime_policy_catalog_state SET revision=2"),
            )

        assert await bootstrap_system_runtime_policies(factory) == 2
        async with factory() as session, session.begin():
            locked = await SystemRuntimePolicyService.lock_agent_runtime_for_admission(
                session,
            )

        assert locked.schema_version == 3
        assert isinstance(locked.value, AgentRuntimePolicyValue)
        assert locked.value.tool_call_budget.profiles.interactive.lead.default.warn == 30
        assert locked.value.vision_bridge.model_name is None
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_runtime_policy_bootstrap_cas_snapshot_and_audit(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    thread_id = f"runtime-policy-{uuid.uuid4()}"
    run_one = str(uuid.uuid4())
    run_two = str(uuid.uuid4())
    run_bad = str(uuid.uuid4())
    try:
        assert await bootstrap_system_runtime_policies(factory) == 1
        assert await bootstrap_system_runtime_policies(factory) == 1
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"{admin_id}@example.com",
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,created_by_user_id)
                    VALUES (:id,:slug,'Runtime Policy',:owner)"""
                ),
                {
                    "id": project_id,
                    "slug": f"runtime-{project_id.hex[:12]}",
                    "owner": str(admin_id),
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                    (id,project_id,user_id,role,status,version)
                    VALUES (:id,:project,:user,'admin','active',1)"""
                ),
                {
                    "id": membership_id,
                    "project": project_id,
                    "user": str(admin_id),
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO agents
                    (id,scope,project_id,slug,display_name,status,revision,created_by_user_id)
                    VALUES (:id,'project',:project,'runtime-agent','Runtime Agent','active',1,:user)"""
                ),
                {
                    "id": agent_id,
                    "project": project_id,
                    "user": str(admin_id),
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO threads_meta
                    (thread_id,owner_user_id,status,metadata_json,created_at,updated_at,
                     project_id,agent_asset_id,agent_scope)
                    VALUES (:thread,:user,'idle','{}',now(),now(),:project,:agent,'project')"""
                ),
                {
                    "thread": thread_id,
                    "user": str(admin_id),
                    "project": project_id,
                    "agent": agent_id,
                },
            )
            for run_id in (run_one, run_two, run_bad):
                await connection.execute(
                    text(
                        """INSERT INTO runs
                        (run_id,thread_id,owner_user_id,status,multitask_strategy,
                         metadata_json,kwargs_json,origin_trace_id,message_count,
                         total_input_tokens,total_output_tokens,total_tokens,llm_call_count,
                         lead_agent_tokens,subagent_tokens,middleware_tokens,
                         created_at,updated_at,project_id)
                        VALUES (:run,:thread,:user,'pending','reject','{}','{}',:trace,
                                0,0,0,0,0,0,0,0,now(),now(),:project)"""
                    ),
                    {
                        "run": run_id,
                        "thread": thread_id,
                        "user": str(admin_id),
                        "trace": str(uuid.uuid4()),
                        "project": project_id,
                    },
                )

        audit = AuditService(factory, AuditHmacKeyring.from_environment())
        service = SystemRuntimePolicyService(factory, audit)
        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id=str(uuid.uuid4()),
        )

        async with factory() as session, session.begin():
            locked_v1 = await service.lock_agent_runtime_for_admission(session)
            await service.admit_run_snapshot(
                session,
                project_id=project_id,
                owner_user_id=str(admin_id),
                thread_id=thread_id,
                run_id=run_one,
                locked_policy=locked_v1,
            )

        updated_value = default_policy_value(
            RuntimePolicySection.AGENT_RUNTIME,
        ).model_dump(mode="python")
        # This isolated policy test intentionally has no System Model catalog.
        updated_value["vision_bridge"]["model_name"] = None
        updated_value["max_recursion_limit"] = 77
        updated_value["memory"]["dream_interval_minutes"] = 45
        updated = await service.update_policy(
            context,
            RuntimePolicySection.AGENT_RUNTIME,
            expected_revision=1,
            value=updated_value,
        )
        assert updated.catalog_revision == 2
        assert updated.policy.revision == 2

        with pytest.raises(SystemRuntimePolicyConflict):
            await service.update_policy(
                context,
                RuntimePolicySection.AGENT_RUNTIME,
                expected_revision=1,
                value=updated_value,
            )

        async with factory() as session, session.begin():
            locked_v2 = await service.lock_agent_runtime_for_admission(session)
            await service.admit_run_snapshot(
                session,
                project_id=project_id,
                owner_user_id=str(admin_id),
                thread_id=thread_id,
                run_id=run_two,
                locked_policy=locked_v2,
            )
        assert locked_v1.policy_version_id != locked_v2.policy_version_id
        assert locked_v1.revision == 1
        assert locked_v2.revision == 2
        assert locked_v1.value.vision_bridge.timeout_seconds == 60
        assert locked_v2.value.vision_bridge.timeout_seconds == 60
        assert locked_v1.value.max_recursion_limit == 1_000
        assert locked_v2.value.max_recursion_limit == 77
        assert locked_v1.value.memory.dream_interval_minutes == 120
        assert locked_v2.value.memory.dream_interval_minutes == 45
        materializer = SystemRuntimePolicyMaterializer(factory)
        materialized_v1 = await materializer.materialize_run_snapshot(
            project_id=project_id,
            owner_user_id=str(admin_id),
            run_id=run_one,
        )
        materialized_v2 = await materializer.materialize_run_snapshot(
            project_id=project_id,
            owner_user_id=str(admin_id),
            run_id=run_two,
        )
        materialized_v1_envelope = await materializer.materialize_run_snapshot_envelope(
            project_id=project_id,
            owner_user_id=str(admin_id),
            run_id=run_one,
        )
        assert materialized_v1_envelope.schema_version == 4
        assert materialized_v1_envelope.value == materialized_v1
        assert materialized_v1.max_recursion_limit == 1_000
        assert materialized_v2.max_recursion_limit == 77
        assert materialized_v1.memory.dream_interval_minutes == 120
        assert materialized_v2.memory.dream_interval_minutes == 45
        exact_v1 = await materializer.materialize_revision(
            RuntimePolicySection.AGENT_RUNTIME,
            1,
        )
        exact_v2 = await materializer.materialize_revision(
            RuntimePolicySection.AGENT_RUNTIME,
            2,
        )
        assert exact_v1.max_recursion_limit == 1_000
        assert exact_v2.max_recursion_limit == 77
        with pytest.raises(SystemRuntimePolicyUnavailable):
            await materializer.materialize_revision(
                RuntimePolicySection.AGENT_RUNTIME,
                0,
            )
        with pytest.raises(SystemRuntimePolicyUnavailable):
            await materializer.materialize_revision(
                RuntimePolicySection.AGENT_RUNTIME,
                99,
            )

        async with factory() as session:
            snapshots = tuple(
                (
                    await session.execute(
                        select(RunRuntimePolicySnapshotRow)
                        .where(
                            RunRuntimePolicySnapshotRow.run_id.in_(
                                (run_one, run_two),
                            )
                        )
                        .order_by(RunRuntimePolicySnapshotRow.run_id)
                    )
                ).scalars()
            )
            by_run = {row.run_id: row for row in snapshots}
            assert by_run[run_one].policy_version_id == locked_v1.policy_version_id
            assert by_run[run_two].policy_version_id == locked_v2.policy_version_id
            audit_row = (
                await session.execute(
                    text(
                        """SELECT action,target_kind,metadata_json::text
                           FROM audit_logs
                           WHERE action='system_setting.updated'"""
                    )
                )
            ).one()
            assert audit_row.action == "system_setting.updated"
            assert audit_row.target_kind == "system_setting"
            assert "agent_runtime" in audit_row.metadata_json
            for forbidden in ("password", "api_key", "token", "value"):
                assert forbidden not in audit_row.metadata_json.lower()

        assert await bootstrap_system_runtime_policies(factory) == 2

        async with factory() as session, session.begin():
            session.add(
                RunRuntimePolicySnapshotRow(
                    project_id=project_id,
                    owner_user_id=str(admin_id),
                    thread_id=thread_id,
                    run_id=run_bad,
                    section=RuntimePolicySection.AGENT_RUNTIME.value,
                    policy_version_id=locked_v1.policy_version_id,
                    schema_version=locked_v1.schema_version + 1,
                    payload_checksum=locked_v1.payload_checksum,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()

        async with factory() as session, session.begin():
            version = await session.get(
                SystemRuntimePolicyVersionRow,
                locked_v1.policy_version_id,
            )
            assert version is not None
            version.value = {"corrupt": True}
            with pytest.raises(DBAPIError):
                await session.flush()

        async with factory() as session, session.begin():
            snapshot = (
                await session.execute(
                    select(RunRuntimePolicySnapshotRow).where(
                        RunRuntimePolicySnapshotRow.run_id == run_one,
                    )
                )
            ).scalar_one()
            snapshot.schema_version += 1
            with pytest.raises(DBAPIError):
                await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_vision_bridge_policy_requires_compatible_active_model(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    compatible_model_id = uuid.uuid4()
    incompatible_model_id = uuid.uuid4()
    compatible_name = str(compatible_model_id)
    incompatible_name = str(incompatible_model_id)
    try:
        assert await bootstrap_system_runtime_policies(factory) == 1
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"{admin_id}@example.com",
                },
            )
            for model_id, name, adapter, checksum in (
                (
                    compatible_model_id,
                    compatible_name,
                    "vision_bridge_fake",
                    "d" * 64,
                ),
                (
                    incompatible_model_id,
                    incompatible_name,
                    "vision_openai_compatible_v1",
                    "e" * 64,
                ),
            ):
                await seed_system_model_config(
                    connection,
                    model_id=model_id,
                    owner_user_id=str(admin_id),
                    display_name=name,
                    provider_model=name,
                    provider_adapter=adapter,
                    supports_vision=True,
                    settings={
                        "base_url": "https://vision.example.test/v1",
                    },
                )

        audit = AuditService(factory, AuditHmacKeyring.from_environment())
        service = SystemRuntimePolicyService(factory, audit)
        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id=str(uuid.uuid4()),
        )
        base_value = default_policy_value(
            RuntimePolicySection.AGENT_RUNTIME,
        ).model_dump(mode="python")

        incompatible_value = dict(base_value)
        incompatible_value["vision_bridge"] = {
            **base_value["vision_bridge"],
            "model_name": incompatible_name,
        }
        with pytest.raises(SystemRuntimePolicyInvalid):
            await service.update_policy(
                context,
                RuntimePolicySection.AGENT_RUNTIME,
                expected_revision=1,
                value=incompatible_value,
            )

        retired_auxiliary_value = dict(base_value)
        retired_auxiliary_value["title"] = {
            **base_value["title"],
            "model_name": incompatible_name,
        }
        with pytest.raises(SystemRuntimePolicyInvalid):
            await service.update_policy(
                context,
                RuntimePolicySection.AGENT_RUNTIME,
                expected_revision=1,
                value=retired_auxiliary_value,
            )

        compatible_value = dict(base_value)
        compatible_value["vision_bridge"] = {
            **base_value["vision_bridge"],
            "model_name": compatible_name,
        }
        updated = await service.update_policy(
            context,
            RuntimePolicySection.AGENT_RUNTIME,
            expected_revision=1,
            value=compatible_value,
        )
        assert updated.policy.revision == 2
        assert updated.policy.value.vision_bridge.model_name == compatible_name
        materialized = await SystemRuntimePolicyMaterializer(
            factory,
        ).materialize_revision(RuntimePolicySection.AGENT_RUNTIME, 2)
        assert materialized.vision_bridge.model_name == compatible_name
        assert materialized.vision_bridge.contract_version == "vision.bridge.v1"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_postgres_memory_document_policy_bootstrap_cas_lock_and_audit(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    try:
        assert await bootstrap_system_runtime_policies(factory) == 1
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(admin_id),
                    "email": f"{admin_id}@example.com",
                },
            )

        service = SystemRuntimePolicyService(
            factory,
            AuditService(factory, AuditHmacKeyring.from_environment()),
        )
        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id=str(uuid.uuid4()),
        )
        catalog = await service.list_policies(context)
        assert set(catalog.sections) == set(RuntimePolicySection)
        memory_document = catalog.sections[RuntimePolicySection.MEMORY_DOCUMENT]
        assert memory_document.effect_scope == "new_memory_documents"
        assert isinstance(memory_document.value, MemoryDocumentPolicy)

        async with factory() as session, session.begin():
            locked_v1 = await service.lock_memory_document_for_creation(session)
        assert locked_v1.revision == 1
        assert locked_v1.value.sections == [
            "用户偏好与协作方式",
            "项目背景",
            "长期约束与架构决策",
            "当前仍有效的目标",
        ]

        updated = await service.update_policy(
            context,
            RuntimePolicySection.MEMORY_DOCUMENT,
            expected_revision=1,
            value={"sections": ["  Personal context ", "Architecture decisions"]},
        )
        assert updated.catalog_revision == 2
        assert updated.policy.revision == 2
        assert updated.policy.effect_scope == "new_memory_documents"
        assert isinstance(updated.policy.value, MemoryDocumentPolicy)
        assert updated.policy.value.sections == [
            "Personal context",
            "Architecture decisions",
        ]
        with pytest.raises(SystemRuntimePolicyConflict):
            await service.update_policy(
                context,
                RuntimePolicySection.MEMORY_DOCUMENT,
                expected_revision=1,
                value={"sections": ["First", "Second"]},
            )

        async with factory() as session, session.begin():
            locked_v2 = await service.lock_memory_document_for_creation(session)
        assert locked_v2.revision == 2
        assert locked_v2.policy_version_id != locked_v1.policy_version_id
        assert locked_v2.value.sections == [
            "Personal context",
            "Architecture decisions",
        ]

        async with factory() as session:
            assert await session.scalar(text("SELECT count(*) FROM system_runtime_policies")) == 5
            assert (
                await session.scalar(
                    text(
                        """SELECT count(*)
                           FROM system_runtime_policy_versions
                          WHERE section='memory_document'"""
                    )
                )
                == 2
            )
            audit_row = (
                await session.execute(
                    text(
                        """SELECT metadata_json->>'section' AS section,
                                  metadata_json->>'effect_scope' AS effect_scope
                             FROM audit_logs
                            WHERE action='system_setting.updated'
                              AND metadata_json->>'section'='memory_document'"""
                    )
                )
            ).one()
            assert audit_row.section == "memory_document"
            assert audit_row.effect_scope == "new_memory_documents"
    finally:
        await engine.dispose()
