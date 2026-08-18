from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from support.private_thread_seed import (
    PrivateThreadSeed,
    seed_private_thread_database,
)

from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.errors import PrivateWorkAgentArchived, PrivateWorkAssetStale
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.context import ProjectContext
from app.shared_assets import agent_service as agent_service_module
from app.shared_assets.agent_design_service import (
    AgentDesignService,
    CreateAgentDesignSession,
)
from app.shared_assets.agent_repository import AgentRepository
from app.shared_assets.agent_service import AgentService, CreateAgent
from app.shared_assets.errors import AssetConflict
from app.shared_assets.models import AgentPayload
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.persistence.private_work import RunAssetVersionRow
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow


class _AcceptingAgentCatalogValidator:
    async def validate(self, *_args: object, **_kwargs: object) -> None:
        return None


def _project_context(seed: PrivateThreadSeed) -> ProjectContext:
    private = seed.owner_a
    return ProjectContext(
        user_id=private.user_id,
        project_id=private.project_id,
        membership_id=private.membership_id,
        role=private.role,
        capabilities=private.capabilities,
        membership_version=private.membership_version,
        request_id=private.request_id,
    )


async def _create_thread(seed: PrivateThreadSeed, thread_id: str) -> None:
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_archived_agent_slug_can_be_reused_by_builder_and_new_agent(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    context = _project_context(seed)
    try:
        async with seed.factory() as session:
            original = await session.get(AgentRow, seed.project_agent_id)
            assert original is not None
            slug = original.slug
            display_name = original.display_name

        await AgentService(seed.factory).delete(
            context,
            seed.project_agent_id,
            expected_asset_version=1,
        )

        design = await AgentDesignService(seed.factory).create(
            context,
            CreateAgentDesignSession(
                slug=slug,
                display_name=display_name,
                idempotency_key=f"reuse-{uuid.uuid4()}",
            ),
        )
        assert design.slug == slug

        agent_service = AgentService(
            seed.factory,
            catalog_validator=_AcceptingAgentCatalogValidator(),
        )
        replacement_payload = AgentPayload(
            description="Replacement Agent with a reused archived slug",
            soul="Review carefully.",
            model_ref="default",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
            payload_schema_version=3,
        )
        created = await agent_service.create_project(
            context,
            CreateAgent(slug=slug, display_name=display_name),
            replacement_payload,
        )

        assert created.asset.id != seed.project_agent_id
        assert created.asset.slug == slug
        assert created.asset.status == "suspended"

        with pytest.raises(AssetConflict):
            await agent_service.create_project(
                context,
                CreateAgent(slug=slug, display_name=display_name),
                replacement_payload,
            )

        async with seed.factory() as session:
            rows = tuple(
                (
                    await session.execute(
                        select(AgentRow)
                        .where(
                            AgentRow.project_id == context.project_id,
                            AgentRow.slug == slug,
                        )
                        .order_by(AgentRow.created_at, AgentRow.id),
                    )
                ).scalars()
            )

        assert [(row.id, row.status) for row in rows] == [
            (seed.project_agent_id, "archived"),
            (created.asset.id, "suspended"),
        ]
    finally:
        await seed.engine.dispose()


class _PauseAfterAdmissionResolve(ProjectAssetResolver):
    def __init__(
        self,
        seed: PrivateThreadSeed,
        *,
        resolved: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        super().__init__(seed.factory)
        self._resolved = resolved
        self._resume = resume

    async def resolve_run_asset_closure_in_session(
        self,
        session: AsyncSession,
        context: ProjectContext,
        selection,
    ):
        closure = await super().resolve_run_asset_closure_in_session(
            session,
            context,
            selection,
        )
        self._resolved.set()
        await self._resume.wait()
        return closure


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admission_snapshot_first_survives_concurrent_agent_archive(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    resolved = asyncio.Event()
    resume = asyncio.Event()
    thread_id = f"archive-admit-{uuid.uuid4()}"
    run_id = f"agent-archive-run-{uuid.uuid4()}"
    admission: asyncio.Task | None = None
    archive: asyncio.Task | None = None
    try:
        await _create_thread(seed, thread_id)
        resolver = _PauseAfterAdmissionResolve(
            seed,
            resolved=resolved,
            resume=resume,
        )
        admission = asyncio.create_task(
            PrivateRunAdmissionService(
                seed.factory,
                resolver=resolver,
            ).admit(
                seed.owner_a,
                thread_id,
                PrivateRunCreate(run_id=run_id),
            )
        )
        await asyncio.wait_for(resolved.wait(), timeout=5)

        archive = asyncio.create_task(
            AgentService(seed.factory).delete(
                _project_context(seed),
                seed.project_agent_id,
                expected_asset_version=1,
            )
        )
        done, _pending = await asyncio.wait({archive}, timeout=0.2)
        assert not done, "archive must wait for the admission project/Agent locks"

        resume.set()
        admitted = await asyncio.wait_for(admission, timeout=5)
        await asyncio.wait_for(archive, timeout=5)

        async with seed.factory() as session:
            agent = await session.get(AgentRow, seed.project_agent_id)
            version = await session.get(
                AgentVersionRow,
                admitted.snapshot.assets[0].version_id,
            )
            persisted = await session.scalar(
                select(RunAssetVersionRow).where(
                    RunAssetVersionRow.project_id == seed.owner_a.project_id,
                    RunAssetVersionRow.owner_user_id == str(seed.owner_a.user_id),
                    RunAssetVersionRow.run_id == run_id,
                    RunAssetVersionRow.asset_kind == "agent",
                    RunAssetVersionRow.dependency_order == 0,
                )
            )
        assert agent is not None and agent.status == "archived"
        assert version is not None and version.workflow_status == "published"
        assert persisted is not None
        assert persisted.version_id == version.id
        assert persisted.payload_checksum == version.payload_checksum

        runtime = await PrivateAssetRuntime(seed.factory).materialize(
            seed.owner_a,
            admitted,
        )
        try:
            assert runtime.safe_manifest.agent_asset_id == seed.project_agent_id
            assert runtime.safe_manifest.agent_version_id == version.id
        finally:
            await runtime.aclose()
    finally:
        resume.set()
        for task in (admission, archive):
            if task is not None and not task.done():
                task.cancel()
        if admission is not None or archive is not None:
            await asyncio.gather(
                *(task for task in (admission, archive) if task is not None),
                return_exceptions=True,
            )
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_archive_first_blocks_new_run_with_stable_error(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    archived = asyncio.Event()
    resume = asyncio.Event()
    thread_id = f"agent-archive-first-{uuid.uuid4()}"
    run_id = f"agent-archive-rejected-{uuid.uuid4()}"
    archive: asyncio.Task | None = None
    admission: asyncio.Task | None = None

    class _PauseAfterArchiveRepository(AgentRepository):
        async def archive_project_asset(self, context, asset) -> None:
            await super().archive_project_asset(context, asset)
            archived.set()
            await resume.wait()

    try:
        await _create_thread(seed, thread_id)
        monkeypatch.setattr(
            agent_service_module,
            "AgentRepository",
            _PauseAfterArchiveRepository,
        )
        archive = asyncio.create_task(
            AgentService(seed.factory).delete(
                _project_context(seed),
                seed.project_agent_id,
                expected_asset_version=1,
            )
        )
        await asyncio.wait_for(archived.wait(), timeout=5)

        admission = asyncio.create_task(
            PrivateRunAdmissionService(seed.factory).admit(
                seed.owner_a,
                thread_id,
                PrivateRunCreate(run_id=run_id),
            )
        )
        done, _pending = await asyncio.wait({admission}, timeout=0.2)
        assert not done, "admission must wait for the archive transaction"

        resume.set()
        await asyncio.wait_for(archive, timeout=5)
        with pytest.raises(PrivateWorkAgentArchived) as exc_info:
            await asyncio.wait_for(admission, timeout=5)
        assert exc_info.value.request_id == seed.owner_a.request_id
        assert str(exc_info.value) == PrivateWorkAgentArchived.public_message

        async with seed.factory() as session:
            agent = await session.get(AgentRow, seed.project_agent_id)
            snapshot = await session.scalar(
                select(RunAssetVersionRow).where(
                    RunAssetVersionRow.project_id == seed.owner_a.project_id,
                    RunAssetVersionRow.owner_user_id == str(seed.owner_a.user_id),
                    RunAssetVersionRow.run_id == run_id,
                )
            )
        assert agent is not None and agent.status == "archived"
        assert snapshot is None
    finally:
        resume.set()
        for task in (archive, admission):
            if task is not None and not task.done():
                task.cancel()
        if archive is not None or admission is not None:
            await asyncio.gather(
                *(task for task in (archive, admission) if task is not None),
                return_exceptions=True,
            )
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_exact_run_materialization_still_rejects_suspended_agent(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"suspend-exact-{uuid.uuid4()}"
    run_id = f"suspend-exact-run-{uuid.uuid4()}"
    try:
        await _create_thread(seed, thread_id)
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(run_id=run_id),
        )
        await AgentService(seed.factory).suspend(
            _project_context(seed),
            seed.project_agent_id,
            expected_asset_version=1,
        )

        with pytest.raises(PrivateWorkAssetStale) as exc_info:
            await PrivateAssetRuntime(seed.factory).materialize(
                seed.owner_a,
                admitted,
            )
        assert exc_info.value.request_id == seed.owner_a.request_id
    finally:
        await seed.engine.dispose()
