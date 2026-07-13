from __future__ import annotations

import asyncio
import dataclasses
import importlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.projects.context import ProjectContext, resolve_project_context
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetNotFound, AssetValidationFailed
from app.shared_assets.models import AgentPayload, WorkflowStatus
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
) -> AgentPayload:
    return AgentPayload(
        description="Research analyst",
        soul=soul,
        model_ref="default",
        tool_groups=("research",),
        skill_version_ids=skill_version_ids,
        mcp_version_ids=mcp_version_ids,
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
            _payload(skill_version_ids=(skill_version_id,), mcp_version_ids=(mcp_version_id,)),
            expected_asset_version=1,
        )
        published = await service.publish(
            editor,
            asset.id,
            draft.id,
            expected_asset_version=2,
        )

        assert published.workflow_status is WorkflowStatus.PUBLISHED
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
        with pytest.raises(dataclasses.FrozenInstanceError):
            published.soul = "mutated"
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

        async def publish_once():
            return await service_module.AgentService(factory).publish(
                editor,
                asset.id,
                draft.id,
                expected_asset_version=2,
            )

        results = await asyncio.gather(publish_once(), publish_once(), return_exceptions=True)
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, AssetConflict) for result in results) == 1
        assert (await service.get(editor, asset.id)).version == 3

        with pytest.raises(AssetConflict):
            await service.archive(editor, asset.id, expected_asset_version=2)
        archived = await service.archive(editor, asset.id, expected_asset_version=3)
        assert archived.status == "archived" and archived.version == 4
        with pytest.raises(AssetForbidden):
            await service.suspend(editor, asset.id, expected_asset_version=4)

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
