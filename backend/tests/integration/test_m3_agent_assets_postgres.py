from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.projects.context import ProjectContext, resolve_project_context
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetNotFound, AssetValidationFailed
from app.shared_assets.models import AgentModelSettings, AgentPayload, WorkflowStatus
from deerflow.persistence.shared_assets import AgentRow, AgentVersionMcpRefRow, AgentVersionRow, AgentVersionSkillRefRow


async def _seed_actor_and_project(
    engine: AsyncEngine,
    factory: async_sessionmaker,
    *,
    label: str,
    role: str = "editor",
) -> ProjectContext:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'user',:now,false,0)"""
            ),
            {"id": str(user_id), "email": f"{label}-{user_id}@example.com", "now": now},
        )
        await connection.execute(
            text(
                """INSERT INTO projects
                (id,slug,display_name,created_by_user_id,created_at,updated_at)
                VALUES (:id,:slug,:name,:user,:now,:now)"""
            ),
            {
                "id": project_id,
                "slug": f"{label}-{str(project_id)[:8]}",
                "name": label,
                "user": str(user_id),
                "now": now,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO project_memberships
                (id,project_id,user_id,role,status,version)
                VALUES (:id,:project,:user,:role,'active',1)"""
            ),
            {
                "id": membership_id,
                "project": project_id,
                "user": str(user_id),
                "role": role,
            },
        )
    async with factory() as session:
        return await resolve_project_context(session, user_id, project_id, f"req-{label}")


async def _seed_system_admin(engine: AsyncEngine) -> SystemAssetGovernanceContext:
    user_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {
                "id": str(user_id),
                "email": f"system-{user_id}@example.com",
                "now": datetime.now(UTC),
            },
        )
    return SystemAssetGovernanceContext(user_id=user_id, request_id="req-system")


async def _seed_dependency(
    engine: AsyncEngine,
    *,
    kind: str,
    scope: str,
    project_id: uuid.UUID | None,
    user_id: uuid.UUID,
    asset_status: str = "active",
    workflow_status: str = "published",
) -> tuple[uuid.UUID, uuid.UUID]:
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    slug = f"{kind}-{str(asset_id)[:8]}"
    table, version_table, parent_column = {
        "skill": ("skills", "skill_versions", "skill_id"),
        "mcp": ("mcp_servers", "mcp_server_versions", "mcp_server_id"),
    }[kind]
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"""INSERT INTO {table}
                (id,scope,project_id,slug,display_name,status,created_by_user_id)
                VALUES (:id,:scope,:project,:slug,:slug,:status,:user)"""
            ),
            {
                "id": asset_id,
                "scope": scope,
                "project": project_id,
                "slug": slug,
                "status": asset_status,
                "user": str(user_id),
            },
        )
        if kind == "skill":
            await connection.execute(
                text(
                    f"""INSERT INTO {version_table}
                    (id,{parent_column},version_number,workflow_status,description,
                     frontmatter,secret_requirements,scan_decision,scan_summary,
                     payload_checksum,created_by_user_id)
                    VALUES (:id,:asset,1,:workflow,'','{{}}'::jsonb,'[]'::jsonb,
                            'allow','{{}}'::jsonb,:checksum,:user)"""
                ),
                {
                    "id": version_id,
                    "asset": asset_id,
                    "workflow": workflow_status,
                    "checksum": "1" * 64,
                    "user": str(user_id),
                },
            )
        else:
            await connection.execute(
                text(
                    f"""INSERT INTO {version_table}
                    (id,{parent_column},version_number,workflow_status,description,
                     transport,args,non_secret_env,non_secret_headers,oauth_metadata,
                     routing,tool_overrides,timeout_seconds,payload_checksum,created_by_user_id)
                    VALUES (:id,:asset,1,:workflow,'','stdio','[]'::jsonb,
                            '{{}}'::jsonb,'{{}}'::jsonb,'{{}}'::jsonb,'{{}}'::jsonb,
                            '{{}}'::jsonb,30,:checksum,:user)"""
                ),
                {
                    "id": version_id,
                    "asset": asset_id,
                    "workflow": workflow_status,
                    "checksum": "2" * 64,
                    "user": str(user_id),
                },
            )
        if workflow_status == "published":
            await connection.execute(
                text(f"UPDATE {table} SET current_published_version_id=:version WHERE id=:asset"),
                {"version": version_id, "asset": asset_id},
            )
    return asset_id, version_id


async def _bind_system_dependency(
    engine: AsyncEngine,
    *,
    kind: str,
    project_id: uuid.UUID,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    table, asset_column, version_column = {
        "skill": ("project_system_skill_bindings", "system_skill_id", "skill_version_id"),
        "mcp": ("project_system_mcp_bindings", "system_mcp_server_id", "mcp_server_version_id"),
    }[kind]
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"""INSERT INTO {table}
                (project_id,{asset_column},{version_column},created_by_user_id,updated_by_user_id)
                VALUES (:project,:asset,:version,:user,:user)"""
            ),
            {
                "project": project_id,
                "asset": asset_id,
                "version": version_id,
                "user": str(user_id),
            },
        )


def _payload(
    *,
    skill_version_ids: tuple[uuid.UUID, ...] = (),
    mcp_version_ids: tuple[uuid.UUID, ...] = (),
    soul: str = "Verify sources before answering.",
    model_settings: AgentModelSettings | None = None,
) -> AgentPayload:
    return AgentPayload(
        description="Research analyst",
        soul=soul,
        model_ref="default",
        tool_groups=("research",),
        skill_version_ids=skill_version_ids,
        mcp_version_ids=mcp_version_ids,
        model_settings=model_settings or AgentModelSettings(),
    )


@pytest.mark.asyncio
async def test_project_agent_publish_pins_dependencies_and_hides_other_project(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="first")
    outsider = await _seed_actor_and_project(engine, factory, label="other")
    _, skill_version_id = await _seed_dependency(
        engine,
        kind="skill",
        scope="project",
        project_id=editor.project_id,
        user_id=editor.user_id,
    )
    _, mcp_version_id = await _seed_dependency(
        engine,
        kind="mcp",
        scope="project",
        project_id=editor.project_id,
        user_id=editor.user_id,
    )
    service = service_module.AgentService(factory)
    try:
        asset = await service.create_asset(editor, service_module.CreateAgent("analyst", "Analyst"))
        outsider_asset = await service.create_asset(outsider, service_module.CreateAgent("outsider", "Outsider"))
        assert await service.get_version_history(editor, asset.id) == ()

        repository_module = importlib.import_module("app.shared_assets.agent_repository")
        forged_asset = AgentRow(id=asset.id, scope="project", project_id=editor.project_id)
        forged_version = AgentVersionRow(
            agent_id=outsider_asset.id,
            version_number=1,
            workflow_status="draft",
            description="",
            soul="cross-project",
            model_ref="default",
            tool_groups=[],
            payload_checksum="f" * 64,
            created_by_user_id=str(editor.user_id),
        )
        async with factory() as session:
            repository = repository_module.AgentRepository(session)
            async with session.begin():
                with pytest.raises(AssetNotFound):
                    await repository.create_project_version(
                        editor,
                        forged_asset.id,
                        forged_version,
                        (),
                        (),
                    )
        async with engine.connect() as connection:
            cross_project_versions = (
                await connection.execute(
                    text("SELECT count(*) FROM agent_versions WHERE agent_id=:asset"),
                    {"asset": outsider_asset.id},
                )
            ).scalar_one()
        assert cross_project_versions == 0

        draft = await service.create_version(
            editor,
            asset.id,
            _payload(
                skill_version_ids=(skill_version_id,),
                mcp_version_ids=(mcp_version_id,),
                model_settings=AgentModelSettings(
                    temperature=0.2,
                    thinking_enabled=False,
                ),
            ),
            expected_asset_version=1,
        )
        published = await service.publish(
            editor,
            asset.id,
            draft.id,
            expected_asset_version=2,
        )

        assert published.workflow_status is WorkflowStatus.PUBLISHED
        assert published.payload_schema_version == 3
        assert published.model_settings == AgentModelSettings(
            temperature=0.2,
            thinking_enabled=False,
        )
        assert published.skill_version_ids == (skill_version_id,)
        assert published.mcp_version_ids == (mcp_version_id,)
        assert (await service.get(editor, asset.id)).current_published_version_id == published.id
        assert [item.id for item in await service.list_visible(editor)] == [asset.id]
        history = await service.get_version_history(editor, asset.id)
        assert history == (published,)
        with pytest.raises(AssetNotFound):
            await service.get(outsider, asset.id)

        async with factory() as session:
            skill_refs = (await session.execute(select(AgentVersionSkillRefRow.skill_version_id).where(AgentVersionSkillRefRow.agent_version_id == published.id))).scalars().all()
            mcp_refs = (await session.execute(select(AgentVersionMcpRefRow.mcp_server_version_id).where(AgentVersionMcpRefRow.agent_version_id == published.id))).scalars().all()
        assert skill_refs == [skill_version_id]
        assert mcp_refs == [mcp_version_id]

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE agent_versions SET soul='mutated' WHERE id=:id"),
                    {"id": published.id},
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE agent_versions
                        SET model_settings='{"temperature":0.8}'::jsonb
                        WHERE id=:id"""
                    ),
                    {"id": published.id},
                )
        with pytest.raises(dataclasses.FrozenInstanceError):
            published.soul = "mutated"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_virtual_instructions_survive_runtime_configuration_and_hot_publish(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="instructions")
    _, skill_version_id = await _seed_dependency(
        engine,
        kind="skill",
        scope="project",
        project_id=editor.project_id,
        user_id=editor.user_id,
    )
    service = service_module.AgentService(factory)
    try:
        asset = await service.create_asset(
            editor,
            service_module.CreateAgent("instruction-agent", "Instruction Agent"),
        )
        instruction_draft = await service.update_instructions(
            editor,
            asset.id,
            service_module.AgentInstructions(
                agents_instructions="# Agent rules",
                soul="# Soul",
                identity="# Identity",
                user_context="# User",
            ),
            expected_asset_version=1,
        )
        assert instruction_draft.workflow_status is WorkflowStatus.DRAFT
        assert instruction_draft.payload_schema_version == 2

        runtime_draft = await service.create_version(
            editor,
            asset.id,
            AgentPayload(
                description="Configured runtime",
                soul="",
                model_ref="default",
                tool_groups=("research",),
                skill_version_ids=(skill_version_id,),
                mcp_version_ids=(),
            ),
            expected_asset_version=2,
        )
        assert runtime_draft.agents_instructions == "# Agent rules"
        assert runtime_draft.soul == "# Soul"
        assert runtime_draft.identity == "# Identity"
        assert runtime_draft.user_context == "# User"

        newer_instruction_draft = await service.update_instructions(
            editor,
            asset.id,
            service_module.AgentInstructions(
                agents_instructions="# Updated Agent rules",
                soul="",
                identity="# Updated Identity",
                user_context="",
            ),
            expected_asset_version=3,
        )
        assert newer_instruction_draft.workflow_status is WorkflowStatus.DRAFT
        assert newer_instruction_draft.version_number == 3

        published = await service.publish(
            editor,
            asset.id,
            runtime_draft.id,
            expected_asset_version=4,
        )
        assert published.id != runtime_draft.id
        assert published.version_number == 4
        assert published.workflow_status is WorkflowStatus.PUBLISHED
        assert published.description == runtime_draft.description
        assert published.model_ref == runtime_draft.model_ref
        assert published.tool_groups == runtime_draft.tool_groups
        assert published.agents_instructions == newer_instruction_draft.agents_instructions
        assert published.soul == newer_instruction_draft.soul
        assert published.identity == newer_instruction_draft.identity
        assert published.user_context == newer_instruction_draft.user_context
        assert published.skill_version_ids == (skill_version_id,)
        assert published.payload_schema_version == 2
        async with engine.connect() as connection:
            generation_before = int(await connection.scalar(text("SELECT generation FROM asset_catalog_state WHERE id=1")))
            version_count_before_retry = int(
                await connection.scalar(
                    text("SELECT count(*) FROM agent_versions WHERE agent_id=:agent_id"),
                    {"agent_id": asset.id},
                )
            )
            source_status = await connection.scalar(
                text("SELECT workflow_status FROM agent_versions WHERE id=:version_id"),
                {"version_id": runtime_draft.id},
            )
        assert source_status == WorkflowStatus.REJECTED.value

        with pytest.raises(AssetConflict):
            await service.publish(
                editor,
                asset.id,
                runtime_draft.id,
                expected_asset_version=5,
            )
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM agent_versions WHERE agent_id=:agent_id"),
                    {"agent_id": asset.id},
                )
                == version_count_before_retry
            )

        updated = await service.update_instructions(
            editor,
            asset.id,
            service_module.AgentInstructions(
                agents_instructions="# Final Agent rules",
                soul="# Final Soul",
                identity="# Final Identity",
                user_context="# Final User",
            ),
            expected_asset_version=5,
        )

        assert updated.workflow_status is WorkflowStatus.PUBLISHED
        assert updated.version_number == 5
        assert updated.supersedes_version_id == published.id
        assert updated.skill_version_ids == (skill_version_id,)
        assert updated.payload_schema_version == 2
        assert (await service.get(editor, asset.id)).current_published_version_id == updated.id
        async with engine.connect() as connection:
            generation_after = int(await connection.scalar(text("SELECT generation FROM asset_catalog_state WHERE id=1")))
            refs = (await connection.execute(select(AgentVersionSkillRefRow.skill_version_id).where(AgentVersionSkillRefRow.agent_version_id == updated.id))).scalars().all()
        assert generation_after == generation_before + 2
        assert refs == [skill_version_id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_dependency_closure_enforces_scope_binding_and_dependency_status(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="deps")
    other = await _seed_actor_and_project(engine, factory, label="deps-other")
    system = await _seed_system_admin(engine)
    service = service_module.AgentService(factory)
    try:
        project_skill_id, project_skill_version = await _seed_dependency(
            engine,
            kind="skill",
            scope="project",
            project_id=editor.project_id,
            user_id=editor.user_id,
        )
        _, other_skill_version = await _seed_dependency(
            engine,
            kind="skill",
            scope="project",
            project_id=other.project_id,
            user_id=other.user_id,
        )
        system_skill_id, system_skill_version = await _seed_dependency(
            engine,
            kind="skill",
            scope="system",
            project_id=None,
            user_id=system.user_id,
        )
        system_mcp_id, system_mcp_version = await _seed_dependency(
            engine,
            kind="mcp",
            scope="system",
            project_id=None,
            user_id=system.user_id,
        )

        system_agent = await service.create_asset(system, service_module.CreateAgent("system-agent", "System Agent"))
        with pytest.raises(AssetValidationFailed):
            await service.create_version(
                system,
                system_agent.id,
                _payload(skill_version_ids=(project_skill_version,)),
                expected_asset_version=1,
            )
        system_draft = await service.create_version(
            system,
            system_agent.id,
            _payload(
                skill_version_ids=(system_skill_version,),
                mcp_version_ids=(system_mcp_version,),
            ),
            expected_asset_version=1,
        )
        system_published = await service.publish(system, system_agent.id, system_draft.id, expected_asset_version=2)

        project_agent = await service.create_asset(editor, service_module.CreateAgent("project-agent", "Project Agent"))
        with pytest.raises(AssetValidationFailed):
            await service.create_version(
                editor,
                project_agent.id,
                _payload(
                    skill_version_ids=(system_skill_version,),
                    mcp_version_ids=(system_mcp_version,),
                ),
                expected_asset_version=1,
            )
        with pytest.raises(AssetValidationFailed):
            await service.create_version(
                editor,
                project_agent.id,
                _payload(skill_version_ids=(other_skill_version,)),
                expected_asset_version=1,
            )

        await _bind_system_dependency(
            engine,
            kind="skill",
            project_id=editor.project_id,
            asset_id=system_skill_id,
            version_id=system_skill_version,
            user_id=editor.user_id,
        )
        await _bind_system_dependency(
            engine,
            kind="mcp",
            project_id=editor.project_id,
            asset_id=system_mcp_id,
            version_id=system_mcp_version,
            user_id=editor.user_id,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO project_system_agent_bindings
                    (project_id,system_agent_id,agent_version_id,
                     created_by_user_id,updated_by_user_id)
                    VALUES (:project,:asset,:version,:user,:user)"""
                ),
                {
                    "project": editor.project_id,
                    "asset": system_agent.id,
                    "version": system_published.id,
                    "user": str(editor.user_id),
                },
            )
        bound_draft = await service.create_version(
            editor,
            project_agent.id,
            _payload(
                skill_version_ids=(system_skill_version,),
                mcp_version_ids=(system_mcp_version,),
            ),
            expected_asset_version=1,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE project_system_mcp_bindings SET enabled=false
                    WHERE project_id=:project AND system_mcp_server_id=:asset"""
                ),
                {"project": editor.project_id, "asset": system_mcp_id},
            )
        with pytest.raises(AssetValidationFailed):
            await service.publish(editor, project_agent.id, bound_draft.id, expected_asset_version=2)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE project_system_mcp_bindings SET enabled=true
                    WHERE project_id=:project AND system_mcp_server_id=:asset"""
                ),
                {"project": editor.project_id, "asset": system_mcp_id},
            )
        await service.publish(editor, project_agent.id, bound_draft.id, expected_asset_version=2)
        assert system_agent.id in {item.id for item in await service.list_visible(editor)}

        second_agent = await service.create_asset(editor, service_module.CreateAgent("archived-dep", "Archived Dep"))
        draft = await service.create_version(
            editor,
            second_agent.id,
            _payload(skill_version_ids=(project_skill_version,)),
            expected_asset_version=1,
        )
        published = await service.publish(editor, second_agent.id, draft.id, expected_asset_version=2)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE skills SET status='archived' WHERE id=:id"),
                {"id": project_skill_id},
            )
        assert (await service.get_version_history(editor, second_agent.id))[0] == published
        with pytest.raises(AssetValidationFailed):
            await service.create_version(
                editor,
                second_agent.id,
                _payload(skill_version_ids=(project_skill_version,), soul="new soul"),
                expected_asset_version=3,
            )
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE skills SET status='suspended' WHERE id=:id"),
                {"id": project_skill_id},
            )
        third_agent = await service.create_asset(editor, service_module.CreateAgent("suspended-dep", "Suspended Dep"))
        with pytest.raises(AssetValidationFailed):
            await service.create_version(
                editor,
                third_agent.id,
                _payload(skill_version_ids=(project_skill_version,)),
                expected_asset_version=1,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_publish_is_optimistic_and_lifecycle_is_scope_authorized(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    repository_module = importlib.import_module("app.shared_assets.agent_repository")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="concurrency")
    admin = await _seed_actor_and_project(engine, factory, label="project-admin", role="admin")
    system = await _seed_system_admin(engine)
    service = service_module.AgentService(factory)
    try:
        asset = await service.create_asset(editor, service_module.CreateAgent("race", "Race"))
        draft = await service.create_version(editor, asset.id, _payload(), expected_asset_version=1)
        publish_ready = asyncio.Barrier(2)
        ready_tasks: set[asyncio.Task] = set()
        racing_asset_locks_remaining = 2
        original_get_project_asset = repository_module.AgentRepository.get_project_asset

        async def race_from_the_same_lock_boundary(repository, context, asset_id, *, for_update=False):
            nonlocal racing_asset_locks_remaining
            if asset_id == asset.id and for_update and racing_asset_locks_remaining:
                racing_asset_locks_remaining -= 1
                current_task = asyncio.current_task()
                assert current_task is not None
                ready_tasks.add(current_task)
                await asyncio.wait_for(publish_ready.wait(), timeout=2)
            return await original_get_project_asset(
                repository,
                context,
                asset_id,
                for_update=for_update,
            )

        monkeypatch.setattr(
            repository_module.AgentRepository,
            "get_project_asset",
            race_from_the_same_lock_boundary,
        )

        async def publish_once():
            return await service_module.AgentService(factory).publish(
                editor,
                asset.id,
                draft.id,
                expected_asset_version=2,
            )

        results = await asyncio.gather(publish_once(), publish_once(), return_exceptions=True)
        assert len(ready_tasks) == 2
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, AssetConflict) for result in results) == 1
        assert (await service.get(editor, asset.id)).version == 3

        with pytest.raises(AssetForbidden):
            await service.suspend(editor, asset.id, expected_asset_version=3)

        admin_asset = await service.create_asset(admin, service_module.CreateAgent("stoppable", "Stoppable"))
        suspended = await service.suspend(admin, admin_asset.id, expected_asset_version=1)
        assert suspended.status == "suspended" and suspended.version == 2
        with pytest.raises(AssetConflict):
            await service.create_version(admin, admin_asset.id, _payload(), expected_asset_version=2)

        system_asset = await service.create_asset(system, service_module.CreateAgent("system-only", "System Only"))
        assert system_asset.scope == "system" and system_asset.project_id is None
        async with factory() as session:
            repository = repository_module.AgentRepository(session)
            async with session.begin():
                with pytest.raises(AssetForbidden):
                    await repository.create_system_asset(
                        editor,
                        service_module.CreateAgent("forbidden", "Forbidden"),
                    )
        with pytest.raises(AssetNotFound):
            await service.get(editor, system_asset.id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_publish_rejects_draft_dependency_checksum_drift(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="checksum-drift")
    _, original_skill_version = await _seed_dependency(
        engine,
        kind="skill",
        scope="project",
        project_id=editor.project_id,
        user_id=editor.user_id,
    )
    _, replacement_skill_version = await _seed_dependency(
        engine,
        kind="skill",
        scope="project",
        project_id=editor.project_id,
        user_id=editor.user_id,
    )
    service = service_module.AgentService(factory)
    try:
        asset = await service.create_asset(
            editor,
            service_module.CreateAgent("checksum-drift", "Checksum Drift"),
        )
        draft = await service.create_version(
            editor,
            asset.id,
            _payload(skill_version_ids=(original_skill_version,)),
            expected_asset_version=1,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """DELETE FROM agent_version_skill_refs
                    WHERE agent_version_id=:version AND skill_version_id=:original"""
                ),
                {
                    "version": draft.id,
                    "original": original_skill_version,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO agent_version_skill_refs
                    (agent_version_id,skill_version_id,sort_order)
                    VALUES (:version,:replacement,0)"""
                ),
                {"version": draft.id, "replacement": replacement_skill_version},
            )

        with pytest.raises(AssetValidationFailed):
            await service.publish(
                editor,
                asset.id,
                draft.id,
                expected_asset_version=2,
            )

        unchanged_asset = await service.get(editor, asset.id)
        unchanged_version = (await service.get_version_history(editor, asset.id))[0]
        assert unchanged_asset.version == 2
        assert unchanged_asset.current_published_version_id is None
        assert unchanged_version.workflow_status is WorkflowStatus.DRAFT
        assert unchanged_version.skill_version_ids == (replacement_skill_version,)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_agent_mutation_lock_pins_trusted_context_until_transaction_end(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    repository_module = importlib.import_module("app.shared_assets.agent_repository")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="context-lock")
    service = service_module.AgentService(factory)
    try:
        asset = await service.create_asset(editor, service_module.CreateAgent("locked", "Locked"))
        async with factory() as mutation_session:
            async with mutation_session.begin():
                repository = repository_module.AgentRepository(mutation_session)
                await repository.get_project_asset(editor, asset.id, for_update=True)

                with pytest.raises(DBAPIError):
                    async with factory() as invalidation_session:
                        async with invalidation_session.begin():
                            await invalidation_session.execute(text("SET LOCAL lock_timeout = '100ms'"))
                            await invalidation_session.execute(
                                text(
                                    """UPDATE project_memberships
                                    SET version=version+1 WHERE id=:membership"""
                                ),
                                {"membership": editor.membership_id},
                            )
        completed = await service.create_version(editor, asset.id, _payload(), expected_asset_version=1)
        assert completed.agent_id == asset.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_project_agents_rejects_stale_membership_version(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="list-stale")
    service = service_module.AgentService(factory)
    try:
        await service.create_asset(editor, service_module.CreateAgent("visible", "Visible"))
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE project_memberships SET version=version+1 WHERE id=:membership"),
                {"membership": editor.membership_id},
            )

        with pytest.raises(AssetNotFound):
            await service.list_visible(editor)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_project_agents_pins_context_across_both_visibility_queries(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="list-lock")
    system = await _seed_system_admin(engine)
    service = service_module.AgentService(factory)
    first_agent_query_ready = asyncio.Event()
    release_list_query = asyncio.Event()

    class CoordinatedListSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            sql = str(statement)
            if not self.info.get("agent_list_paused") and "FROM agents" in sql:
                self.info["agent_list_paused"] = True
                first_agent_query_ready.set()
                await release_list_query.wait()
            return result

    coordinated_factory = async_sessionmaker(
        engine,
        class_=CoordinatedListSession,
        expire_on_commit=False,
    )
    list_task: asyncio.Task | None = None
    try:
        project_agent = await service.create_asset(
            editor,
            service_module.CreateAgent("project-visible", "Project Visible"),
        )
        system_agent = await service.create_asset(
            system,
            service_module.CreateAgent("system-visible", "System Visible"),
        )
        system_draft = await service.create_version(
            system,
            system_agent.id,
            _payload(),
            expected_asset_version=1,
        )
        system_published = await service.publish(
            system,
            system_agent.id,
            system_draft.id,
            expected_asset_version=2,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO project_system_agent_bindings
                    (project_id,system_agent_id,agent_version_id,created_by_user_id,updated_by_user_id)
                    VALUES (:project,:agent,:version,:user,:user)"""
                ),
                {
                    "project": editor.project_id,
                    "agent": system_agent.id,
                    "version": system_published.id,
                    "user": str(editor.user_id),
                },
            )

        list_task = asyncio.create_task(service_module.AgentService(coordinated_factory).list_visible(editor))
        await asyncio.wait_for(first_agent_query_ready.wait(), timeout=2)

        invalidation_error: DBAPIError | None = None
        try:
            async with factory() as invalidation_session:
                async with invalidation_session.begin():
                    await invalidation_session.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    await invalidation_session.execute(
                        text("UPDATE project_memberships SET version=version+1 WHERE id=:membership"),
                        {"membership": editor.membership_id},
                    )
        except DBAPIError as exc:
            invalidation_error = exc
        finally:
            release_list_query.set()

        visible = await list_task
        assert isinstance(invalidation_error, DBAPIError)
        assert {row.id for row in visible} == {project_agent.id, system_agent.id}
    finally:
        release_list_query.set()
        if list_task is not None and not list_task.done():
            await list_task
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_version_snapshots_payload_collections_before_database_awaits(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    repository_module = importlib.import_module("app.shared_assets.agent_repository")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="payload-snapshot")
    outsider = await _seed_actor_and_project(engine, factory, label="payload-outsider")
    _, allowed_skill_version = await _seed_dependency(
        engine,
        kind="skill",
        scope="project",
        project_id=editor.project_id,
        user_id=editor.user_id,
    )
    _, outsider_skill_version = await _seed_dependency(
        engine,
        kind="skill",
        scope="project",
        project_id=outsider.project_id,
        user_id=outsider.user_id,
    )
    mutable_skill_ids = [allowed_skill_version]
    payload = AgentPayload(
        description="snapshot",
        soul="Keep the validated dependency.",
        model_ref="default",
        tool_groups=["research"],  # type: ignore[arg-type]
        skill_version_ids=mutable_skill_ids,  # type: ignore[arg-type]
        mcp_version_ids=[],  # type: ignore[arg-type]
    )
    original_next = repository_module.AgentRepository.next_project_version_number

    async def mutate_after_validation(repository, context, asset):
        number = await original_next(repository, context, asset)
        mutable_skill_ids[:] = [outsider_skill_version]
        return number

    monkeypatch.setattr(
        repository_module.AgentRepository,
        "next_project_version_number",
        mutate_after_validation,
    )
    service = service_module.AgentService(factory)
    try:
        asset = await service.create_asset(editor, service_module.CreateAgent("snapshot", "Snapshot"))
        draft = await service.create_version(editor, asset.id, payload, expected_asset_version=1)
        assert draft.skill_version_ids == (allowed_skill_version,)
        assert draft.tool_groups == ("research",)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_agent_hard_delete_removes_the_package_and_preserves_private_builder_history(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(
        engine,
        factory,
        label="agent-hard-delete",
    )
    skill_id, skill_version_id = await _seed_dependency(
        engine,
        kind="skill",
        scope="project",
        project_id=editor.project_id,
        user_id=editor.user_id,
    )
    mcp_id, mcp_version_id = await _seed_dependency(
        engine,
        kind="mcp",
        scope="project",
        project_id=editor.project_id,
        user_id=editor.user_id,
    )
    service = service_module.AgentService(factory)
    try:
        asset = await service.create_asset(
            editor,
            service_module.CreateAgent("delete-package", "Delete Package"),
        )
        first = await service.create_version(
            editor,
            asset.id,
            _payload(
                skill_version_ids=(skill_version_id,),
                mcp_version_ids=(mcp_version_id,),
            ),
            expected_asset_version=1,
        )
        published = await service.publish(
            editor,
            asset.id,
            first.id,
            expected_asset_version=2,
        )
        second = await service.create_version(
            editor,
            asset.id,
            _payload(
                skill_version_ids=(skill_version_id,),
                mcp_version_ids=(mcp_version_id,),
                soul="A newer draft.",
            ),
            expected_asset_version=3,
        )
        builder_session_id = uuid.uuid4()
        private_messages = [{"role": "user", "content": "keep this private"}]
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO agent_design_sessions
                    (id,project_id,owner_user_id,thread_id,slug,display_name,
                     status,revision,messages_json,progress_json,
                     blueprint_json,blueprint_checksum,created_agent_id,
                     created_agent_version_id,create_idempotency_key_hash,
                     create_request_checksum)
                    VALUES
                    (:id,:project,:owner,:thread,:slug,:display,'completed',4,
                     CAST(:messages AS jsonb),'[]'::jsonb,'{}'::jsonb,:checksum,
                     :agent,:version,:idempotency,:request_checksum)"""
                ),
                {
                    "id": builder_session_id,
                    "project": editor.project_id,
                    "owner": str(editor.user_id),
                    "thread": uuid.uuid4(),
                    "slug": asset.slug,
                    "display": asset.display_name,
                    "messages": json.dumps(private_messages),
                    "checksum": "a" * 64,
                    "agent": asset.id,
                    "version": published.id,
                    "idempotency": "b" * 64,
                    "request_checksum": "c" * 64,
                },
            )

        current = await service.get(editor, asset.id)
        await service.delete(
            editor,
            asset.id,
            expected_asset_version=current.version,
        )

        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM agents WHERE id=:id"),
                    {"id": asset.id},
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM agent_versions WHERE agent_id=:id"),
                    {"id": asset.id},
                )
                == 0
            )
            for table in (
                "agent_version_skill_refs",
                "agent_version_mcp_refs",
            ):
                assert (
                    await connection.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE agent_version_id IN (:first,:published,:second)"),
                        {
                            "first": first.id,
                            "published": published.id,
                            "second": second.id,
                        },
                    )
                    == 0
                )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM skills WHERE id=:id"),
                    {"id": skill_id},
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM mcp_servers WHERE id=:id"),
                    {"id": mcp_id},
                )
                == 1
            )
            builder_row = (
                await connection.execute(
                    text(
                        """SELECT status,messages_json,created_agent_id,
                                  created_agent_version_id,
                                  created_agent_deleted
                           FROM agent_design_sessions
                           WHERE id=:id"""
                    ),
                    {"id": builder_session_id},
                )
            ).one()
        assert builder_row[0] == "completed"
        assert builder_row[1] == private_messages
        assert builder_row[2] is None
        assert builder_row[3] is None
        assert builder_row[4] is True

        recreated = await service.create_asset(
            editor,
            service_module.CreateAgent("delete-package", "Delete Package"),
        )
        assert recreated.id != asset.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_agent_hard_delete_rejects_retained_thread_reference(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(
        engine,
        factory,
        label="agent-delete-thread",
    )
    service = service_module.AgentService(factory)
    try:
        asset = await service.create_asset(
            editor,
            service_module.CreateAgent("thread-agent", "Thread Agent"),
        )
        now = datetime.now(UTC)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO threads_meta
                    (thread_id,owner_user_id,status,metadata_json,created_at,
                     updated_at,project_id,agent_asset_id,agent_scope,version)
                    VALUES
                    (:thread,:owner,'idle','{}'::jsonb,:now,:now,:project,
                     :agent,'project',1)"""
                ),
                {
                    "thread": f"thread-{uuid.uuid4()}",
                    "owner": str(editor.user_id),
                    "now": now,
                    "project": editor.project_id,
                    "agent": asset.id,
                },
            )

        with pytest.raises(AssetConflict):
            await service.delete(
                editor,
                asset.id,
                expected_asset_version=asset.version,
            )

        assert (await service.get(editor, asset.id)).id == asset.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_agent_hard_delete_rejects_retained_automation_reference(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(
        engine,
        factory,
        label="agent-delete-automation",
    )
    service = service_module.AgentService(factory)
    try:
        asset = await service.create_asset(
            editor,
            service_module.CreateAgent(
                "automation-agent",
                "Automation Agent",
            ),
        )
        now = datetime.now(UTC)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO scheduled_tasks
                    (id,project_id,owner_user_id,thread_id,context_mode,
                     agent_asset_id,agent_scope,title,prompt,schedule_type,
                     schedule_spec,timezone,status,overlap_policy,next_run_at,
                     last_run_at,last_outcome,last_error_code,run_count,version,
                     frozen_at,deleted_at,created_at,updated_at)
                    VALUES
                    (:id,:project,:owner,NULL,'fresh_thread_per_run',
                     :agent,'project','Retained automation','test','once',
                     '{}'::json,'UTC','cancelled','skip',NULL,NULL,NULL,NULL,
                     0,1,:now,:now,:now,:now)"""
                ),
                {
                    "id": f"task-{uuid.uuid4()}",
                    "project": editor.project_id,
                    "owner": str(editor.user_id),
                    "agent": asset.id,
                    "now": now,
                },
            )

        with pytest.raises(AssetConflict):
            await service.delete(
                editor,
                asset.id,
                expected_asset_version=asset.version,
            )

        assert (await service.get(editor, asset.id)).id == asset.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_agent_hard_delete_rejects_an_exact_terminal_run_snapshot(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.agent_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(
        engine,
        factory,
        label="agent-delete-run",
    )
    service = service_module.AgentService(factory)
    try:
        target = await service.create_asset(
            editor,
            service_module.CreateAgent("snapshot-target", "Snapshot Target"),
        )
        draft = await service.create_version(
            editor,
            target.id,
            _payload(),
            expected_asset_version=1,
        )
        published = await service.publish(
            editor,
            target.id,
            draft.id,
            expected_asset_version=2,
        )
        thread_agent = await service.create_asset(
            editor,
            service_module.CreateAgent("thread-owner", "Thread Owner"),
        )
        thread_id = f"thread-{uuid.uuid4()}"
        run_id = f"run-{uuid.uuid4()}"
        now = datetime.now(UTC)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO threads_meta
                    (thread_id,owner_user_id,status,metadata_json,created_at,
                     updated_at,project_id,agent_asset_id,agent_scope,version)
                    VALUES
                    (:thread,:owner,'idle','{}'::jsonb,:now,:now,:project,
                     :thread_agent,'project',1)"""
                ),
                {
                    "thread": thread_id,
                    "owner": str(editor.user_id),
                    "now": now,
                    "project": editor.project_id,
                    "thread_agent": thread_agent.id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO runs
                    (run_id,thread_id,project_id,owner_user_id,status,
                     multitask_strategy,metadata_json,kwargs_json,
                     finalization_status,message_count,total_input_tokens,
                     total_output_tokens,total_tokens,llm_call_count,
                     lead_agent_tokens,subagent_tokens,middleware_tokens,
                     token_usage_by_model,created_at,updated_at)
                    VALUES
                    (:run,:thread,:project,:owner,'success','reject',
                     '{}'::json,'{}'::json,'complete',0,0,0,0,0,0,0,0,
                     '{}'::json,:now,:now)"""
                ),
                {
                    "run": run_id,
                    "thread": thread_id,
                    "project": editor.project_id,
                    "owner": str(editor.user_id),
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO run_asset_versions
                    (project_id,owner_user_id,thread_id,run_id,asset_kind,
                     dependency_order,asset_scope,asset_id,version_id,
                     payload_checksum,catalog_generation)
                    VALUES
                    (:project,:owner,:thread,:run,'agent',0,'project',
                     :asset,:version,:checksum,
                     (SELECT generation FROM asset_catalog_state WHERE id=1))"""
                ),
                {
                    "project": editor.project_id,
                    "owner": str(editor.user_id),
                    "thread": thread_id,
                    "run": run_id,
                    "asset": target.id,
                    "version": published.id,
                    "checksum": published.payload_checksum,
                },
            )

        current = await service.get(editor, target.id)
        with pytest.raises(AssetConflict):
            await service.delete(
                editor,
                target.id,
                expected_asset_version=current.version,
            )

        assert (await service.get(editor, target.id)).id == target.id
    finally:
        await engine.dispose()
