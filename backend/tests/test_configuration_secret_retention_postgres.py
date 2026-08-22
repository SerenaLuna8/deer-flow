from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from support.private_thread_seed import TEST_MODEL_REF, seed_private_thread_database

from app.audit.service import AuditService, _bind_worker_audit_process
from app.audit.sinks import TrustedOperationAuditSink
from app.private_work.execution_approval_audit import NoopHostExecutionApprovalAudit
from app.private_work.retention_purge import RetentionCandidate, RetentionPurger
from app.project_channels.runtime import ProjectChannelSecretMaterializer
from app.project_channels.secret_store import ProjectChannelSecretStore
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.lifecycle_repository import ProjectLifecycleRepository
from app.projects.lifecycle_service import ProjectLifecycleService
from app.projects.models import ProjectRole
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.errors import AssetConflict, AssetInUse
from app.shared_assets.mcp_repository import McpRepository
from app.shared_assets.mcp_secret_store import McpSecretStore
from app.shared_assets.skill_repository import SkillRepository
from app.shared_assets.skill_secret_store import SkillSecretStore
from app.system_settings.secrets import (
    model_secret_envelope_digest,
    model_secret_recipient,
)
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.channel_connections.model import (
    ProjectChannelInstanceRow,
    ProjectChannelSecretGenerationRow,
    ProjectChannelSecretStateRow,
    ProjectChannelSecretTombstoneRow,
)
from deerflow.persistence.private_work.model import RunAssetVersionRow
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionMcpRefRow,
    AgentVersionRow,
    AgentVersionSkillRefRow,
    McpSecretSlotRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectMcpSecretGenerationRow,
    ProjectMcpSecretStateRow,
    ProjectMcpSecretTombstoneRow,
    SkillRow,
    SkillVersionRow,
)
from deerflow.persistence.shared_assets.skill_secret_model import (
    ProjectSkillSecretGenerationRow,
    ProjectSkillSecretStateRow,
    ProjectSkillSecretTombstoneRow,
)
from deerflow.persistence.system_settings import (
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.secrets import SecretEnvelope, SecretKey


@dataclass(frozen=True)
class _ProjectSecretFixture:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    mcp_server_id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    mcp_slot_id: uuid.UUID
    channel_instance_id: uuid.UUID
    model_id: uuid.UUID
    model_generation_id: uuid.UUID


def _context(seed, *, request_id: str) -> ProjectContext:
    owner = seed.owner_a
    return ProjectContext(
        user_id=owner.user_id,
        project_id=owner.project_id,
        membership_id=owner.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=owner.membership_version,
        request_id=request_id,
    )


async def _add_skill_secret_pair(
    session,
    context: ProjectContext,
    *,
    slug: str,
) -> tuple[SkillRow, SkillVersionRow]:
    skill = SkillRow(
        id=uuid.uuid4(),
        scope="project",
        project_id=context.project_id,
        slug=slug,
        display_name=slug,
        status="active",
        revision=1,
        created_by_user_id=str(context.user_id),
    )
    version = SkillVersionRow(
        id=uuid.uuid4(),
        skill_id=skill.id,
        version_number=1,
        description="Secret retention",
        frontmatter={"name": slug},
        compatibility=None,
        secret_requirements=[
            {
                "name": "provider_key",
                "target_env": "PROVIDER_API_KEY",
                "optional": False,
            }
        ],
        scan_decision="allow",
        scan_summary={"rule_ids": []},
        payload_checksum="a" * 64,
        created_by_user_id=str(context.user_id),
    )
    session.add_all((skill, version))
    await session.flush()
    skill.current_version_id = version.id
    store = SkillSecretStore(session)
    for value in ("skill-secret-one", "skill-secret-two"):
        await store.replace_values(
            project_id=context.project_id,
            skill_id=skill.id,
            skill_version_id=version.id,
            requirements=(("provider_key", False),),
            values={"provider_key": value},
            actor_user_id=str(context.user_id),
            request_id=context.request_id,
        )
    return skill, version


async def _add_mcp_secret_pair(
    session,
    context: ProjectContext,
    *,
    slug: str,
) -> tuple[McpServerRow, McpServerVersionRow, McpSecretSlotRow]:
    server = McpServerRow(
        id=uuid.uuid4(),
        scope="project",
        project_id=context.project_id,
        slug=slug,
        display_name=slug,
        status="active",
        version=1,
        created_by_user_id=str(context.user_id),
    )
    version = McpServerVersionRow(
        id=uuid.uuid4(),
        mcp_server_id=server.id,
        version_number=1,
        workflow_status="draft",
        description="Secret retention",
        transport="http",
        url="http://127.0.0.1:6553/mcp",
        payload_checksum="b" * 64,
        created_by_user_id=str(context.user_id),
    )
    slot = McpSecretSlotRow(
        id=uuid.uuid4(),
        mcp_server_version_id=version.id,
        name="authorization",
        purpose="Authenticate the test recipient",
        payload_schema={"headers": ["Authorization"]},
        required=True,
    )
    session.add_all((server, version))
    await session.flush()
    await session.scalar(
        select(
            func.set_config(
                "deerflow.asset_version_assembly",
                str(version.id),
                True,
            )
        )
    )
    session.add(slot)
    await session.flush()
    version.workflow_status = "published"
    server.current_published_version_id = version.id
    store = McpSecretStore(session)
    for value in ("mcp-secret-one", "mcp-secret-two"):
        await store.replace(
            project_id=context.project_id,
            mcp_server_id=server.id,
            mcp_server_version_id=version.id,
            slots=(slot,),
            slot_name=slot.name,
            payload={"headers": {"Authorization": value}},
            actor_user_id=str(context.user_id),
            request_id=context.request_id,
        )
    return server, version, slot


async def _add_all_domain_secrets(session, context: ProjectContext) -> _ProjectSecretFixture:
    suffix = uuid.uuid4().hex[:12]
    skill, skill_version = await _add_skill_secret_pair(
        session,
        context,
        slug=f"retention-skill-{suffix}",
    )
    mcp, mcp_version, _slot = await _add_mcp_secret_pair(
        session,
        context,
        slug=f"retention-mcp-{suffix}",
    )

    channel = ProjectChannelInstanceRow(
        id=uuid.uuid4(),
        project_id=context.project_id,
        provider="dingtalk",
        display_name="Retention channel",
        desired_status="enabled",
        observed_status="running",
        public_config={"client_id": f"client-{suffix}"},
        provider_identity_digest="c" * 64,
        revision=1,
        created_by_user_id=str(context.user_id),
        updated_by_user_id=str(context.user_id),
    )
    session.add(channel)
    await session.flush()
    channel_store = ProjectChannelSecretStore(session)
    for value in ("channel-secret-one", "channel-secret-two"):
        await channel_store.replace(
            context,
            instance=channel,
            payload={"client_secret": value},
        )

    model = SystemModelConfigRow(
        id=uuid.uuid4(),
        display_name="Retention model",
        status="active",
        provider_adapter="patched_deepseek",
        provider_model="deepseek-v4-flash",
        settings={"base_url": "https://api.deepseek.com"},
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_vision=False,
        payload_checksum="d" * 64,
        secret_revision=0,
        revision=1,
        created_by_user_id=str(context.user_id),
        updated_by_user_id=str(context.user_id),
    )
    session.add(model)
    await session.flush()
    recipient = model_secret_recipient(
        model.id,
        model.provider_adapter,
        model.settings,
    )
    envelope = SecretEnvelope.protect(
        b"model-secret-owned-outside-project",
        recipient=recipient,
        key=SecretKey.from_environment(),
    )
    generation = SystemModelSecretGenerationRow(
        id=uuid.uuid4(),
        model_config_id=model.id,
        revision=1,
        nonce=envelope.nonce,
        ciphertext=envelope.ciphertext,
        envelope_digest=model_secret_envelope_digest(recipient, envelope),
        created_by_user_id=str(context.user_id),
    )
    session.add(generation)
    await session.flush()
    model.current_secret_generation_id = generation.id
    model.secret_revision = 1
    await session.flush()

    return _ProjectSecretFixture(
        skill_id=skill.id,
        skill_version_id=skill_version.id,
        mcp_server_id=mcp.id,
        mcp_server_version_id=mcp_version.id,
        mcp_slot_id=_slot.id,
        channel_instance_id=channel.id,
        model_id=model.id,
        model_generation_id=generation.id,
    )


async def _project_secret_counts(session, project_id: uuid.UUID) -> dict[str, int]:
    row_types = (
        ProjectSkillSecretStateRow,
        ProjectSkillSecretGenerationRow,
        ProjectSkillSecretTombstoneRow,
        ProjectMcpSecretStateRow,
        ProjectMcpSecretGenerationRow,
        ProjectMcpSecretTombstoneRow,
        ProjectChannelSecretStateRow,
        ProjectChannelSecretGenerationRow,
        ProjectChannelSecretTombstoneRow,
    )
    return {row_type.__tablename__: int(await session.scalar(select(func.count()).select_from(row_type).where(row_type.project_id == project_id)) or 0) for row_type in row_types}


async def _project_secret_snapshot(
    session,
    project_id: uuid.UUID,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    row_types = (
        ProjectSkillSecretStateRow,
        ProjectSkillSecretGenerationRow,
        ProjectSkillSecretTombstoneRow,
        ProjectMcpSecretStateRow,
        ProjectMcpSecretGenerationRow,
        ProjectMcpSecretTombstoneRow,
        ProjectChannelSecretStateRow,
        ProjectChannelSecretGenerationRow,
        ProjectChannelSecretTombstoneRow,
    )
    safe_columns = {
        "id",
        "project_id",
        "skill_id",
        "skill_version_id",
        "secret_name",
        "optional",
        "mcp_server_id",
        "mcp_server_version_id",
        "slot_id",
        "channel_instance_id",
        "current_generation_id",
        "destroyed_generation_id",
        "revision",
        "envelope_digest",
        "reason",
    }
    result: dict[str, tuple[tuple[object, ...], ...]] = {}
    for row_type in row_types:
        table = row_type.__table__
        projected = tuple(column for column in table.c if column.name in safe_columns)
        statement = select(*projected).where(table.c.project_id == project_id).order_by(*table.primary_key.columns)
        result[table.name] = tuple(tuple(row) for row in (await session.execute(statement)).all())
    return result


async def _add_run_asset_snapshots(
    session,
    seed,
    context: ProjectContext,
    fixture: _ProjectSecretFixture,
) -> str:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    session.add(
        ThreadMetaRow(
            thread_id=thread_id,
            assistant_id=str(seed.project_agent_id),
            owner_user_id=str(context.user_id),
            display_name="Retention snapshot gate",
            status="idle",
            metadata_json={},
            project_id=context.project_id,
            agent_asset_id=seed.project_agent_id,
            agent_scope="project",
        )
    )
    await session.flush()
    session.add(
        RunRow(
            run_id=run_id,
            thread_id=thread_id,
            assistant_id=str(seed.project_agent_id),
            owner_user_id=str(context.user_id),
            status="success",
            model_name="retention-test-model",
            multitask_strategy="reject",
            metadata_json={},
            kwargs_json={},
            origin_trace_id=uuid.uuid4().hex,
            project_id=context.project_id,
            finalization_status="complete",
        )
    )
    await session.flush()
    session.add_all(
        (
            RunAssetVersionRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run_id,
                asset_kind="skill",
                dependency_order=0,
                asset_scope="project",
                asset_id=fixture.skill_id,
                version_id=fixture.skill_version_id,
                payload_checksum="a" * 64,
                catalog_generation=1,
                snapshot_json={},
            ),
            RunAssetVersionRow(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                run_id=run_id,
                asset_kind="mcp",
                dependency_order=0,
                asset_scope="project",
                asset_id=fixture.mcp_server_id,
                version_id=fixture.mcp_server_version_id,
                payload_checksum="b" * 64,
                catalog_generation=1,
                snapshot_json={},
            ),
        )
    )
    await session.flush()
    return run_id


class _RetentionApprovalAudit(NoopHostExecutionApprovalAudit):
    async def run_terminal(self, *args, **kwargs) -> None:
        del args, kwargs

    async def run_cancel_requested(self, *args, **kwargs) -> None:
        del args, kwargs


@pytest.mark.asyncio
async def test_project_pending_deletion_restore_and_final_purge_secret_ownership(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"r" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    context = _context(seed, request_id="project-secret-retention")
    now = datetime.now(UTC)
    try:
        async with seed.factory() as session, session.begin():
            fixture = await _add_all_domain_secrets(session, context)
            run_id = await _add_run_asset_snapshots(
                session,
                seed,
                context,
                fixture,
            )
        async with seed.factory() as session:
            expected_counts = await _project_secret_counts(session, context.project_id)
            expected_snapshot = await _project_secret_snapshot(
                session,
                context.project_id,
            )
        assert set(expected_counts.values()) == {1}

        async with seed.factory() as session, session.begin():
            skill_repository = SkillRepository(session)
            await skill_repository.lock_project_delete_scope(context)
            skill = await skill_repository.get_project_asset(
                context,
                fixture.skill_id,
                for_update=True,
            )
            with pytest.raises(AssetInUse):
                await skill_repository.plan_project_asset_deletion(context, skill)

        async with seed.factory() as session, session.begin():
            mcp_repository = McpRepository(session)
            mcp = await mcp_repository.get_project_asset(
                context,
                fixture.mcp_server_id,
                for_update=True,
            )
            with pytest.raises(AssetConflict):
                await mcp_repository.plan_project_asset_deletion(context, mcp)

        retention = SimpleNamespace(
            freeze_owner=AsyncMock(),
            restore_owners=AsyncMock(),
        )
        authorization = SimpleNamespace(mark_revoked=AsyncMock())
        jobs = SimpleNamespace(
            admit_project=AsyncMock(),
            restore_project=AsyncMock(),
        )
        async with seed.factory() as session:
            lifecycle = ProjectLifecycleService(
                ProjectLifecycleRepository(session),
                authorization=authorization,
                retention=retention,
                retention_jobs=jobs,
            )
            pending = await lifecycle.request_deletion(context, now)
        assert pending.status == "pending_deletion"
        async with seed.factory() as session:
            assert await _project_secret_counts(session, context.project_id) == expected_counts
            assert await _project_secret_snapshot(session, context.project_id) == expected_snapshot

        async with seed.factory() as session:
            lifecycle = ProjectLifecycleService(
                ProjectLifecycleRepository(session),
                authorization=authorization,
                retention=retention,
                retention_jobs=jobs,
            )
            restored = await lifecycle.restore(
                context.user_id,
                context.project_id,
                "project-secret-restore",
                now + timedelta(days=1),
            )
        assert restored.status == "active"
        async with seed.factory() as session:
            assert await _project_secret_counts(session, context.project_id) == expected_counts
            assert await _project_secret_snapshot(session, context.project_id) == expected_snapshot
            skill_store = SkillSecretStore(session)
            skill_material = (
                await skill_store.load_materials(
                    project_id=context.project_id,
                    skill_id=fixture.skill_id,
                    skill_version_id=fixture.skill_version_id,
                    requirements=(("provider_key", False),),
                    require_required=True,
                    for_update=False,
                    request_id=context.request_id,
                )
            )[0]
            assert (
                skill_store.materialize(
                    skill_material,
                    request_id=context.request_id,
                )
                == "skill-secret-two"
            )
            slot = await session.get(McpSecretSlotRow, fixture.mcp_slot_id)
            assert slot is not None
            mcp_store = McpSecretStore(session)
            mcp_material = (
                await mcp_store.load_materials(
                    project_id=context.project_id,
                    mcp_server_id=fixture.mcp_server_id,
                    mcp_server_version_id=fixture.mcp_server_version_id,
                    slots=(slot,),
                    require_required=True,
                    for_update=False,
                    request_id=context.request_id,
                )
            )[0]
            assert mcp_store.materialize(
                mcp_material,
                request_id=context.request_id,
            ) == {"headers": {"Authorization": "mcp-secret-two"}}
        channel_material = await ProjectChannelSecretMaterializer(
            seed.factory,
        ).load(fixture.channel_instance_id)
        assert channel_material.config["client_secret"] == "channel-secret-two"

        async with seed.factory() as session:
            lifecycle = ProjectLifecycleService(
                ProjectLifecycleRepository(session),
                authorization=authorization,
                retention=retention,
                retention_jobs=jobs,
            )
            await lifecycle.request_deletion(context, now + timedelta(days=2))
        deletion_effective_at = now - timedelta(seconds=1)
        async with seed.factory() as session, session.begin():
            await session.execute(update(ProjectRow).where(ProjectRow.id == context.project_id).values(deletion_effective_at=deletion_effective_at))
        keyring = AuditHmacKeyring("retention-test", {"retention-test": b"a" * 32})
        audit_service = AuditService(seed.factory, keyring)
        purger = RetentionPurger(
            seed.factory,
            audit=TrustedOperationAuditSink(
                audit_service,
                process_context=_bind_worker_audit_process(audit_service),
            ),
            approval_audit=_RetentionApprovalAudit(),
            quota=ProjectQuotaEnforcer(
                QuotaService(
                    seed.factory,
                    QuotaConfig(),
                    source_ref_hasher=keyring,
                )
            ),
        )
        result = await purger.purge(
            RetentionCandidate.project(
                project_id=context.project_id,
                deletion_effective_at=deletion_effective_at,
                idempotency_key=f"project-secret-retention:{context.project_id}",
                request_id="project-secret-final-purge",
            ),
            now=now,
        )
        assert result.purged_count == 1

        async with seed.factory() as session:
            assert set((await _project_secret_counts(session, context.project_id)).values()) == {0}
            model = await session.get(SystemModelConfigRow, fixture.model_id)
            generation = await session.get(
                SystemModelSecretGenerationRow,
                fixture.model_generation_id,
            )
            run_snapshot_count = await session.scalar(
                select(func.count())
                .select_from(RunAssetVersionRow)
                .where(
                    RunAssetVersionRow.project_id == context.project_id,
                    RunAssetVersionRow.run_id == run_id,
                )
            )
        assert model is not None
        assert generation is not None
        assert model.current_secret_generation_id == fixture.model_generation_id
        assert run_snapshot_count == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_skill_and_mcp_hard_delete_reference_gates_and_secret_cascades(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"h" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    context = _context(seed, request_id="asset-hard-delete-secrets")
    try:
        async with seed.factory() as session, session.begin():
            referenced_skill, referenced_skill_version = await _add_skill_secret_pair(
                session,
                context,
                slug=f"referenced-skill-{uuid.uuid4().hex[:12]}",
            )
            referenced_mcp, referenced_mcp_version, _ = await _add_mcp_secret_pair(
                session,
                context,
                slug=f"referenced-mcp-{uuid.uuid4().hex[:12]}",
            )
            deletable_skill, deletable_skill_version = await _add_skill_secret_pair(
                session,
                context,
                slug=f"deletable-skill-{uuid.uuid4().hex[:12]}",
            )
            deletable_mcp, deletable_mcp_version, _ = await _add_mcp_secret_pair(
                session,
                context,
                slug=f"deletable-mcp-{uuid.uuid4().hex[:12]}",
            )

            agent = AgentRow(
                id=uuid.uuid4(),
                scope="project",
                project_id=context.project_id,
                slug=f"secret-reference-agent-{uuid.uuid4().hex[:12]}",
                display_name="Secret reference gate",
                status="active",
                revision=1,
                created_by_user_id=str(context.user_id),
            )
            agent_version = AgentVersionRow(
                id=uuid.uuid4(),
                agent_id=agent.id,
                version_number=1,
                description="Secret reference gate",
                soul="Reference gate",
                model_ref=TEST_MODEL_REF,
                tool_groups=[],
                payload_checksum="e" * 64,
                created_by_user_id=str(context.user_id),
            )
            session.add_all((agent, agent_version))
            await session.flush()
            await session.scalar(
                select(
                    func.set_config(
                        "deerflow.asset_version_assembly",
                        str(agent_version.id),
                        True,
                    )
                )
            )
            session.add_all(
                (
                    AgentVersionSkillRefRow(
                        agent_version_id=agent_version.id,
                        skill_asset_scope="project",
                        skill_asset_id=referenced_skill.id,
                        sort_order=0,
                    ),
                    AgentVersionMcpRefRow(
                        agent_version_id=agent_version.id,
                        mcp_server_version_id=referenced_mcp_version.id,
                        sort_order=0,
                    ),
                )
            )
            await session.flush()
            agent.current_version_id = agent_version.id

        async with seed.factory() as session, session.begin():
            skill_repository = SkillRepository(session)
            await skill_repository.lock_project_delete_scope(context)
            skill = await skill_repository.get_project_asset(
                context,
                referenced_skill.id,
                for_update=True,
            )
            with pytest.raises(AssetInUse):
                await skill_repository.plan_project_asset_deletion(context, skill)

        async with seed.factory() as session, session.begin():
            mcp_repository = McpRepository(session)
            mcp = await mcp_repository.get_project_asset(
                context,
                referenced_mcp.id,
                for_update=True,
            )
            with pytest.raises(AssetConflict):
                await mcp_repository.plan_project_asset_deletion(context, mcp)

        async with seed.factory() as session, session.begin():
            skill_repository = SkillRepository(session)
            await skill_repository.lock_project_delete_scope(context)
            skill = await skill_repository.get_project_asset(
                context,
                deletable_skill.id,
                for_update=True,
            )
            skill_versions = await skill_repository.plan_project_asset_deletion(
                context,
                skill,
            )
            await skill_repository.delete_project_asset(
                context,
                skill,
                tuple(record.version_id for record in skill_versions),
            )

        async with seed.factory() as session, session.begin():
            mcp_repository = McpRepository(session)
            mcp = await mcp_repository.get_project_asset(
                context,
                deletable_mcp.id,
                for_update=True,
            )
            mcp_versions = await mcp_repository.plan_project_asset_deletion(
                context,
                mcp,
            )
            await mcp_repository.delete_project_asset(
                context,
                mcp,
                mcp_versions,
            )

        async with seed.factory() as session:
            assert await session.get(SkillRow, deletable_skill.id) is None
            assert await session.get(SkillVersionRow, deletable_skill_version.id) is None
            assert await session.get(McpServerRow, deletable_mcp.id) is None
            assert await session.get(McpServerVersionRow, deletable_mcp_version.id) is None
            for row_type, asset_column, asset_id in (
                (ProjectSkillSecretStateRow, ProjectSkillSecretStateRow.skill_id, deletable_skill.id),
                (
                    ProjectSkillSecretGenerationRow,
                    ProjectSkillSecretGenerationRow.skill_id,
                    deletable_skill.id,
                ),
                (
                    ProjectSkillSecretTombstoneRow,
                    ProjectSkillSecretTombstoneRow.skill_id,
                    deletable_skill.id,
                ),
                (
                    ProjectMcpSecretStateRow,
                    ProjectMcpSecretStateRow.mcp_server_id,
                    deletable_mcp.id,
                ),
                (
                    ProjectMcpSecretGenerationRow,
                    ProjectMcpSecretGenerationRow.mcp_server_id,
                    deletable_mcp.id,
                ),
                (
                    ProjectMcpSecretTombstoneRow,
                    ProjectMcpSecretTombstoneRow.mcp_server_id,
                    deletable_mcp.id,
                ),
            ):
                count = await session.scalar(select(func.count()).select_from(row_type).where(asset_column == asset_id))
                assert count == 0

            assert await session.get(SkillRow, referenced_skill.id) is not None
            assert (
                await session.get(
                    SkillVersionRow,
                    referenced_skill_version.id,
                )
                is not None
            )
            assert await session.get(McpServerRow, referenced_mcp.id) is not None
            assert (
                await session.get(
                    McpServerVersionRow,
                    referenced_mcp_version.id,
                )
                is not None
            )
    finally:
        await seed.engine.dispose()
