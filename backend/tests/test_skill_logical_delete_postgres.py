from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.run_closure import (
    add_legacy_test_run_asset,
    begin_test_run_closure,
    seal_test_run_closure,
)
from support.skill_version_fixture import (
    assemble_and_seal_skill_version,
    sealed_skill_version_fixture,
)

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext
from app.private_work.run_service import PrivateRunService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.agent_service import AgentService
from app.shared_assets.errors import AssetConflict, AssetNotFound
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedSkillVersionSnapshot,
    SkillAssetRef,
)
from app.shared_assets.run_snapshot_codec import encode_run_skill_version_manifest
from app.shared_assets.skill_deletion import ArchivedSkillPurger
from app.shared_assets.skill_design_repository import SkillDesignRepository
from app.shared_assets.skill_service import SkillService
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.private_work.model import (
    RunAssetVersionRow,
    RunSkillVersionRefRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentSkillRefRow,
    ProjectSkillSecretGenerationRow,
    ProjectSkillSecretStateRow,
    ProjectSkillSecretTombstoneRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow

pytestmark = pytest.mark.run_skill_writer_cohort_control


@dataclass(frozen=True, slots=True)
class _LogicalDeleteSeed:
    context: ProjectContext
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    agent_id: uuid.UUID
    agent_definition_id: uuid.UUID
    archived_agent_id: uuid.UUID
    archived_agent_definition_id: uuid.UUID
    secret_generation_id: uuid.UUID
    slug: str
    display_name: str
    content_size_bytes: int


async def _seed_logical_delete_scope(session) -> _LogicalDeleteSeed:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent_definition_id = uuid.uuid4()
    archived_agent_id = uuid.uuid4()
    archived_agent_definition_id = uuid.uuid4()
    secret_generation_id = uuid.uuid4()
    slug = f"logical-delete-{skill_id.hex[:12]}"
    display_name = f"Logical delete {skill_id.hex[:8]}"

    session.add(
        UserRow(
            id=str(user_id),
            email=f"{user_id}@example.invalid",
            username=f"u{user_id.hex[:12]}",
            system_role="user",
            needs_setup=False,
            token_version=0,
        )
    )
    await session.flush()
    session.add(
        ProjectRow(
            id=project_id,
            slug=f"logical-delete-{project_id.hex[:12]}",
            display_name="Logical delete project",
            created_by_user_id=str(user_id),
        )
    )
    await session.flush()
    session.add(
        ProjectMembershipRow(
            id=membership_id,
            project_id=project_id,
            user_id=str(user_id),
            role="admin",
            status="active",
            version=1,
        )
    )
    await session.flush()

    skill = SkillRow(
        id=skill_id,
        scope="project",
        project_id=project_id,
        slug=slug,
        display_name=display_name,
        status="active",
        revision=4,
        created_by_user_id=str(user_id),
    )
    session.add(skill)
    await session.flush()
    fixture = sealed_skill_version_fixture(skill_version_id, name=slug)
    version = SkillVersionRow(
        id=skill_version_id,
        skill_id=skill_id,
        version_number=1,
        description="Logical deletion fixture",
        frontmatter={"name": slug},
        compatibility=None,
        secret_requirements=[
            {
                "name": "API_KEY",
                "target_env": "API_KEY",
                "optional": False,
            }
        ],
        scan_decision="allow",
        scan_summary={},
        supersedes_version_id=None,
        payload_checksum=fixture.payload_checksum,
        file_count=fixture.file_count,
        content_size_bytes=fixture.content_size_bytes,
        files_sealed=False,
        created_by_user_id=str(user_id),
    )
    session.add(version)
    await session.flush()
    await assemble_and_seal_skill_version(session, fixture)
    skill.current_version_id = skill_version_id

    payload = AgentPayload(
        description="Agent that uses the deleted Skill",
        soul="Keep running",
        model_ref="test-model",
        tool_groups=(),
        skill_refs=(SkillAssetRef(AssetScope.PROJECT, skill_id),),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
    session.add(
        AgentRow(
            id=agent_id,
            scope="project",
            project_id=project_id,
            slug=f"logical-agent-{agent_id.hex[:12]}",
            display_name="Logical deletion Agent",
            status="active",
            definition_id=agent_definition_id,
            description=payload.description,
            soul=payload.soul,
            model_ref=payload.model_ref,
            model_settings={},
            tool_groups=[],
            payload_checksum=agent_payload_checksum(
                payload,
                payload_schema_version=4,
            ),
            agents_instructions="",
            identity="",
            user_context="",
            payload_schema_version=4,
            revision=7,
            created_by_user_id=str(user_id),
            updated_by_user_id=str(user_id),
        )
    )
    session.add(
        AgentRow(
            id=archived_agent_id,
            scope="project",
            project_id=project_id,
            slug=f"archived-logical-agent-{archived_agent_id.hex[:12]}",
            display_name="Archived logical deletion Agent",
            status="archived",
            definition_id=archived_agent_definition_id,
            description=payload.description,
            soul=payload.soul,
            model_ref=payload.model_ref,
            model_settings={},
            tool_groups=[],
            payload_checksum=agent_payload_checksum(
                payload,
                payload_schema_version=4,
            ),
            agents_instructions="",
            identity="",
            user_context="",
            payload_schema_version=4,
            revision=3,
            created_by_user_id=str(user_id),
            updated_by_user_id=str(user_id),
        )
    )
    await session.flush()
    await session.scalar(
        select(
            func.set_config(
                "deerflow.agent_definition_mutation_id",
                str(agent_id),
                True,
            )
        )
    )
    session.add(
        AgentSkillRefRow(
            agent_id=agent_id,
            sort_order=0,
            skill_asset_scope="project",
            skill_asset_id=skill_id,
        )
    )
    await session.flush()
    await session.scalar(
        select(
            func.set_config(
                "deerflow.agent_definition_mutation_id",
                str(archived_agent_id),
                True,
            )
        )
    )
    session.add(
        AgentSkillRefRow(
            agent_id=archived_agent_id,
            sort_order=0,
            skill_asset_scope="project",
            skill_asset_id=skill_id,
        )
    )

    session.add(
        ProjectSkillSecretGenerationRow(
            id=secret_generation_id,
            project_id=project_id,
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            secret_name="API_KEY",
            revision=1,
            nonce=b"n" * 12,
            ciphertext=b"c" * 16,
            envelope_digest="d" * 64,
            created_by_user_id=str(user_id),
        )
    )
    await session.flush()
    session.add(
        ProjectSkillSecretStateRow(
            project_id=project_id,
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            secret_name="API_KEY",
            optional=False,
            current_generation_id=secret_generation_id,
            revision=1,
            updated_by_user_id=str(user_id),
        )
    )
    await session.flush()
    return _LogicalDeleteSeed(
        context=ProjectContext(
            user_id=user_id,
            project_id=project_id,
            membership_id=membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="skill-logical-delete-postgres",
        ),
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        agent_id=agent_id,
        agent_definition_id=agent_definition_id,
        archived_agent_id=archived_agent_id,
        archived_agent_definition_id=archived_agent_definition_id,
        secret_generation_id=secret_generation_id,
        slug=slug,
        display_name=display_name,
        content_size_bytes=fixture.content_size_bytes,
    )


class _Quota:
    def __init__(self) -> None:
        self.released: list[tuple[uuid.UUID, uuid.UUID, int]] = []
        self.reconciled: list[uuid.UUID] = []

    async def release_skill_version_if_reserved(
        self,
        _session,
        project_id: uuid.UUID,
        *,
        version_id: uuid.UUID,
        size: int,
    ) -> bool:
        self.released.append((project_id, version_id, size))
        return True

    async def reconcile_project_storage(
        self,
        _session,
        project_id: uuid.UUID,
    ) -> None:
        self.reconciled.append(project_id)


class _PurgeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[uuid.UUID, uuid.UUID, int, str]] = []

    async def archived_skill_purged(
        self,
        _session,
        *,
        project_id: uuid.UUID,
        skill_id: uuid.UUID,
        version_count: int,
        request_id: str,
    ) -> None:
        self.events.append((project_id, skill_id, version_count, request_id))


class _FailingPurger:
    def __init__(self) -> None:
        self.calls = 0

    async def purge_project(
        self,
        _project_id: uuid.UUID,
        *,
        request_id: str,
    ) -> None:
        del request_id
        self.calls += 1
        raise RuntimeError("temporary archived Skill purge failure")


class _RawCheckpointSaver(BaseCheckpointSaver):
    def __init__(self) -> None:
        super().__init__()
        self.deleted_threads: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)


async def _add_retained_run(
    session,
    seeded: _LogicalDeleteSeed,
    *,
    schema_version: int,
) -> tuple[str, str]:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    session.add(
        ThreadMetaRow(
            thread_id=thread_id,
            assistant_id=str(seeded.agent_id),
            owner_user_id=str(seeded.context.user_id),
            display_name="Retained archived Skill Run",
            status="idle",
            metadata_json={},
            project_id=seeded.context.project_id,
            agent_asset_id=seeded.agent_id,
            agent_scope="project",
        )
    )
    await session.flush()
    run = RunRow(
        run_id=run_id,
        thread_id=thread_id,
        assistant_id=str(seeded.agent_id),
        owner_user_id=str(seeded.context.user_id),
        status="success",
        model_name="logical-delete-test-model",
        multitask_strategy="reject",
        metadata_json={},
        kwargs_json={},
        origin_trace_id=uuid.uuid4().hex,
        project_id=seeded.context.project_id,
        finalization_status="complete",
    )
    await begin_test_run_closure(session, run)
    add_legacy_test_run_asset(
        session,
        run,
        asset_kind="agent",
        dependency_order=0,
        asset_id=seeded.agent_id,
        version_id=seeded.agent_definition_id,
        payload_checksum="a" * 64,
        catalog_generation=1,
    )
    if schema_version == 3:
        add_legacy_test_run_asset(
            session,
            run,
            asset_kind="skill",
            dependency_order=1,
            asset_id=seeded.skill_id,
            version_id=seeded.skill_version_id,
            payload_checksum=(
                await session.scalar(
                    select(SkillVersionRow.payload_checksum).where(
                        SkillVersionRow.id == seeded.skill_version_id,
                    )
                )
            ),
            catalog_generation=1,
        )
    else:
        version = await session.get(SkillVersionRow, seeded.skill_version_id)
        assert version is not None
        snapshot = ResolvedSkillVersionSnapshot(
            kind=AssetKind.SKILL,
            scope=AssetScope.PROJECT,
            asset_id=seeded.skill_id,
            version_id=seeded.skill_version_id,
            checksum=version.payload_checksum,
            catalog_generation=1,
            dependency_version_ids=(),
            file_count=version.file_count,
            content_size_bytes=version.content_size_bytes,
            secret_requirements=(),
        )
        session.add(
            RunAssetVersionRow(
                project_id=seeded.context.project_id,
                owner_user_id=str(seeded.context.user_id),
                thread_id=thread_id,
                run_id=run_id,
                asset_kind="skill",
                dependency_order=1,
                asset_scope="project",
                asset_id=seeded.skill_id,
                version_id=seeded.skill_version_id,
                payload_checksum=version.payload_checksum,
                catalog_generation=1,
                snapshot_schema_version=4,
                snapshot_json=encode_run_skill_version_manifest(snapshot),
            )
        )
        await session.flush()
        session.add(
            RunSkillVersionRefRow(
                project_id=seeded.context.project_id,
                owner_user_id=str(seeded.context.user_id),
                thread_id=thread_id,
                run_id=run_id,
                asset_kind="skill",
                dependency_order=1,
                asset_scope="project",
                snapshot_schema_version=4,
                skill_project_id=seeded.context.project_id,
                skill_id=seeded.skill_id,
                skill_version_id=seeded.skill_version_id,
                payload_checksum=version.payload_checksum,
                file_count=version.file_count,
                content_size_bytes=version.content_size_bytes,
            )
        )
    await seal_test_run_closure(session, run)
    return thread_id, run_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_archived_skill_name_is_available_to_builder_preflight_and_recreation(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            seeded = await _seed_logical_delete_scope(session)
            archived = await session.get(SkillRow, seeded.skill_id)
            assert archived is not None
            archived.status = "archived"
            archived.revision += 1
            await session.flush()

            repository = SkillDesignRepository(session)
            assert not await repository.project_skill_name_exists(
                seeded.context,
                slug=seeded.slug,
                display_name=seeded.display_name,
            )

            session.add(
                SkillRow(
                    scope="project",
                    project_id=seeded.context.project_id,
                    slug=seeded.slug,
                    display_name=seeded.display_name,
                    status="suspended",
                    created_by_user_id=str(seeded.context.user_id),
                )
            )
            await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_skill_delete_archives_unbinds_hides_and_destroys_secret_ciphertext(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            seeded = await _seed_logical_delete_scope(session)

        service = SkillService(factory)
        result = await service.delete(
            seeded.context,
            seeded.skill_id,
            expected_asset_version=4,
        )

        assert result.affected_agent_count == 2
        assert all(item.id != seeded.skill_id for item in await service.list_visible(seeded.context))
        with pytest.raises(AssetNotFound):
            await service.get(seeded.context, seeded.skill_id)
        with pytest.raises(AssetNotFound):
            await service.get_version_history(seeded.context, seeded.skill_id)

        agent = await AgentService(factory).get(seeded.context, seeded.agent_id)
        assert agent.asset.status == "active"
        assert agent.asset.revision == 8
        assert agent.asset.definition_id != seeded.agent_definition_id
        assert agent.definition.skill_refs == ()

        async with factory() as session, session.begin():
            archived_agent = await session.get(
                AgentRow,
                seeded.archived_agent_id,
            )
            assert archived_agent is not None
            assert archived_agent.status == "archived"
            assert archived_agent.revision == 4
            assert archived_agent.definition_id != seeded.archived_agent_definition_id
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AgentSkillRefRow)
                    .where(
                        AgentSkillRefRow.agent_id == seeded.archived_agent_id,
                    )
                )
                == 0
            )
            archived = await session.get(SkillRow, seeded.skill_id)
            assert archived is not None
            assert archived.status == "archived"
            assert archived.current_version_id == seeded.skill_version_id
            assert archived.revision == 5
            assert await session.get(SkillVersionRow, seeded.skill_version_id) is not None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(SkillVersionFileRow)
                    .where(
                        SkillVersionFileRow.skill_version_id == seeded.skill_version_id,
                    )
                )
                == 1
            )
            assert (
                await session.get(
                    ProjectSkillSecretGenerationRow,
                    seeded.secret_generation_id,
                )
                is None
            )
            state = await session.get(
                ProjectSkillSecretStateRow,
                (
                    seeded.context.project_id,
                    seeded.skill_id,
                    seeded.skill_version_id,
                    "API_KEY",
                ),
            )
            assert state is not None
            assert state.current_generation_id is None
            assert state.revision == 2
            tombstones = tuple(
                (
                    await session.execute(
                        select(ProjectSkillSecretTombstoneRow).where(
                            ProjectSkillSecretTombstoneRow.project_id == seeded.context.project_id,
                            ProjectSkillSecretTombstoneRow.skill_id == seeded.skill_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(tombstones) == 1
            assert tombstones[0].reason == "skill_delete"

            session.add(
                SkillRow(
                    id=uuid.uuid4(),
                    scope="project",
                    project_id=seeded.context.project_id,
                    slug=seeded.slug,
                    display_name=seeded.display_name,
                    status="suspended",
                    revision=1,
                    created_by_user_id=str(seeded.context.user_id),
                )
            )
            await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_skill_delete_rejects_outsider_without_mutating_the_skill(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            seeded = await _seed_logical_delete_scope(session)
        outsider = ProjectContext(
            user_id=uuid.uuid4(),
            project_id=seeded.context.project_id,
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="skill-delete-outsider",
        )

        with pytest.raises(AssetNotFound):
            await SkillService(factory).delete(
                outsider,
                seeded.skill_id,
                expected_asset_version=4,
            )

        async with factory() as session:
            skill = await session.get(SkillRow, seeded.skill_id)
            assert skill is not None
            assert (skill.status, skill.revision) == ("active", 4)
            assert (
                await session.get(
                    ProjectSkillSecretGenerationRow,
                    seeded.secret_generation_id,
                )
                is not None
            )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_skill_delete_rejects_stale_revision_without_side_effects(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            seeded = await _seed_logical_delete_scope(session)

        with pytest.raises(AssetConflict):
            await SkillService(factory).delete(
                seeded.context,
                seeded.skill_id,
                expected_asset_version=3,
            )

        async with factory() as session:
            skill = await session.get(SkillRow, seeded.skill_id)
            assert skill is not None
            assert (skill.status, skill.revision) == ("active", 4)
            assert await session.scalar(select(func.count()).select_from(AgentSkillRefRow).where(AgentSkillRefRow.skill_asset_id == seeded.skill_id)) == 2
            assert (
                await session.get(
                    ProjectSkillSecretGenerationRow,
                    seeded.secret_generation_id,
                )
                is not None
            )
    finally:
        await engine.dispose()


class _FailingDeleteAuditSink:
    async def append_project(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected Skill delete audit failure")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_skill_delete_rolls_back_every_mutation_when_audit_fails(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            seeded = await _seed_logical_delete_scope(session)

        with pytest.raises(RuntimeError, match="injected Skill delete audit failure"):
            await SkillService(
                factory,
                governance_sink=_FailingDeleteAuditSink(),  # type: ignore[arg-type]
            ).delete(
                seeded.context,
                seeded.skill_id,
                expected_asset_version=4,
            )

        async with factory() as session:
            skill = await session.get(SkillRow, seeded.skill_id)
            active_agent = await session.get(AgentRow, seeded.agent_id)
            archived_agent = await session.get(AgentRow, seeded.archived_agent_id)
            assert skill is not None
            assert (skill.status, skill.revision, skill.current_version_id) == (
                "active",
                4,
                seeded.skill_version_id,
            )
            assert active_agent is not None
            assert (active_agent.definition_id, active_agent.revision) == (
                seeded.agent_definition_id,
                7,
            )
            assert archived_agent is not None
            assert (archived_agent.definition_id, archived_agent.revision) == (
                seeded.archived_agent_definition_id,
                3,
            )
            assert await session.scalar(select(func.count()).select_from(AgentSkillRefRow).where(AgentSkillRefRow.skill_asset_id == seeded.skill_id)) == 2
            assert (
                await session.get(
                    ProjectSkillSecretGenerationRow,
                    seeded.secret_generation_id,
                )
                is not None
            )
            assert await session.scalar(select(func.count()).select_from(ProjectSkillSecretTombstoneRow).where(ProjectSkillSecretTombstoneRow.skill_id == seeded.skill_id)) == 0
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_archived_skill_purge_removes_package_and_keeps_tombstones(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    quota = _Quota()
    audit = _PurgeAudit()
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            seeded = await _seed_logical_delete_scope(session)
        await SkillService(factory).delete(
            seeded.context,
            seeded.skill_id,
            expected_asset_version=4,
        )

        report = await ArchivedSkillPurger(
            factory,
            quota=quota,
            audit=audit,
        ).purge_project(seeded.context.project_id)

        assert report.skills_examined == 1
        assert report.skills_purged == 1
        assert report.versions_purged == 1
        assert report.released_bytes == seeded.content_size_bytes
        assert quota.released == [
            (
                seeded.context.project_id,
                seeded.skill_version_id,
                seeded.content_size_bytes,
            )
        ]
        assert quota.reconciled == [seeded.context.project_id]
        assert audit.events == [
            (
                seeded.context.project_id,
                seeded.skill_id,
                1,
                "archived-skill-purge",
            )
        ]

        async with factory() as session:
            tombstone = await session.get(SkillRow, seeded.skill_id)
            assert tombstone is not None
            assert tombstone.status == "archived"
            assert tombstone.current_version_id is None
            assert await session.get(SkillVersionRow, seeded.skill_version_id) is None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ProjectSkillSecretTombstoneRow)
                    .where(
                        ProjectSkillSecretTombstoneRow.skill_id == seeded.skill_id,
                    )
                )
                == 1
            )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_low_frequency_sweep_skips_project_before_retention_is_due(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    quota = _Quota()
    audit = _PurgeAudit()
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            seeded = await _seed_logical_delete_scope(session)
        await SkillService(factory).delete(
            seeded.context,
            seeded.skill_id,
            expected_asset_version=4,
        )
        async with factory() as session, session.begin():
            project = await session.get(ProjectRow, seeded.context.project_id)
            assert project is not None
            requested_at = datetime.now(UTC)
            project.status = "pending_deletion"
            project.deletion_requested_at = requested_at
            project.deletion_effective_at = requested_at + timedelta(days=7)
            project.deletion_requested_by_user_id = str(seeded.context.user_id)

        purger = ArchivedSkillPurger(
            factory,
            quota=quota,
            audit=audit,
        )
        with pytest.raises(AssetNotFound):
            await purger.purge_project(seeded.context.project_id)

        async def unexpected_project_purge(*_args: object, **_kwargs: object):
            raise AssertionError("not-yet-due Project entered the sweep batch")

        monkeypatch.setattr(purger, "purge_project", unexpected_project_purge)
        report = await purger.sweep()

        assert (
            report.projects_scanned,
            report.skills_examined,
            report.skills_purged,
            report.versions_purged,
            report.released_bytes,
        ) == (0, 0, 0, 0, 0)
        assert quota.released == []
        assert audit.events == []
        async with factory() as session:
            archived = await session.get(SkillRow, seeded.skill_id)
            assert archived is not None
            assert archived.status == "archived"
            assert archived.current_version_id == seeded.skill_version_id
            assert await session.get(SkillVersionRow, seeded.skill_version_id) is not None
    finally:
        await engine.dispose()


@pytest.mark.parametrize("schema_version", (3, 4), ids=("legacy-v3", "reference-v4"))
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retained_run_blocks_purge_until_single_run_delete_releases_it(
    postgres_database_url: str,
    schema_version: int,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    quota = _Quota()
    audit = _PurgeAudit()
    purger = ArchivedSkillPurger(factory, quota=quota, audit=audit)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            seeded = await _seed_logical_delete_scope(session)
            thread_id, run_id = await _add_retained_run(
                session,
                seeded,
                schema_version=schema_version,
            )
        await SkillService(factory).delete(
            seeded.context,
            seeded.skill_id,
            expected_asset_version=4,
        )

        swept = await purger.sweep(limit=1)
        assert swept.projects_scanned == 0
        assert swept.skills_examined == 0
        blocked = await purger.purge_project(seeded.context.project_id)
        assert blocked.skills_examined == 0
        assert blocked.skills_purged == 0
        assert quota.released == []
        assert audit.events == []

        await PrivateRunService(
            factory,
            archived_skill_purger=purger,
        ).delete(
            PrivateWorkContext.from_project(seeded.context),
            thread_id,
            run_id,
        )

        async with factory() as session:
            assert await session.get(SkillVersionRow, seeded.skill_version_id) is None
            tombstone = await session.get(SkillRow, seeded.skill_id)
            assert tombstone is not None
            assert tombstone.current_version_id is None
        assert quota.released == [
            (
                seeded.context.project_id,
                seeded.skill_version_id,
                seeded.content_size_bytes,
            )
        ]
        assert len(audit.events) == 1
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_thread_delete_keeps_run_reference_and_does_not_release_skill_package(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    quota = _Quota()
    audit = _PurgeAudit()
    purger = ArchivedSkillPurger(factory, quota=quota, audit=audit)
    raw = _RawCheckpointSaver()
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            seeded = await _seed_logical_delete_scope(session)
            thread_id, run_id = await _add_retained_run(
                session,
                seeded,
                schema_version=4,
            )
        await SkillService(factory).delete(
            seeded.context,
            seeded.skill_id,
            expected_asset_version=4,
        )

        await (
            ProjectScopedCheckpointer(raw, factory)
            .for_context(PrivateWorkContext.from_project(seeded.context))
            .adelete_thread(
                thread_id,
                expected_version=1,
            )
        )
        report = await purger.purge_project(seeded.context.project_id)

        assert raw.deleted_threads == [thread_id]
        assert report.skills_examined == 0
        assert report.skills_purged == 0
        assert quota.released == []
        assert audit.events == []
        async with factory() as session:
            thread = await session.get(ThreadMetaRow, thread_id)
            assert thread is not None
            assert thread.deleted_at is not None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RunSkillVersionRefRow)
                    .where(
                        RunSkillVersionRefRow.project_id == seeded.context.project_id,
                        RunSkillVersionRefRow.owner_user_id == str(seeded.context.user_id),
                        RunSkillVersionRefRow.thread_id == thread_id,
                        RunSkillVersionRefRow.run_id == run_id,
                    )
                )
                == 1
            )
            assert await session.get(SkillVersionRow, seeded.skill_version_id) is not None
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_failed_post_commit_purge_does_not_roll_back_single_run_delete(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    failing_purger = _FailingPurger()
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            seeded = await _seed_logical_delete_scope(session)
            thread_id, run_id = await _add_retained_run(
                session,
                seeded,
                schema_version=4,
            )
        await SkillService(factory).delete(
            seeded.context,
            seeded.skill_id,
            expected_asset_version=4,
        )

        await PrivateRunService(
            factory,
            archived_skill_purger=failing_purger,  # type: ignore[arg-type]
        ).delete(
            PrivateWorkContext.from_project(seeded.context),
            thread_id,
            run_id,
        )

        assert failing_purger.calls == 1
        async with factory() as session:
            deleted_run = await session.scalar(
                select(RunRow).where(
                    RunRow.project_id == seeded.context.project_id,
                    RunRow.owner_user_id == str(seeded.context.user_id),
                    RunRow.thread_id == thread_id,
                    RunRow.run_id == run_id,
                )
            )
            assert deleted_run is not None
            assert deleted_run.status == "deleted"
            assert deleted_run.metadata_json == {}
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RunSkillVersionRefRow)
                    .where(
                        RunSkillVersionRefRow.project_id == seeded.context.project_id,
                        RunSkillVersionRefRow.owner_user_id == str(seeded.context.user_id),
                        RunSkillVersionRefRow.thread_id == thread_id,
                        RunSkillVersionRefRow.run_id == run_id,
                    )
                )
                == 0
            )
            assert await session.get(SkillVersionRow, seeded.skill_version_id) is not None
    finally:
        await engine.dispose()
