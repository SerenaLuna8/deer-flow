from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.context import resolve_project_context
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound, ProjectSlugConflict, ProjectValidationFailed
from app.projects.models import CreateProject, ProjectChanges
from app.projects.repository import ProjectRepository
from deerflow.persistence.shared_assets.agent_model import AgentRow
from deerflow.persistence.shared_assets.binding_model import (
    ProjectSystemAgentBindingRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
)
from deerflow.persistence.shared_assets.mcp_model import McpServerRow
from deerflow.persistence.shared_assets.skill_model import SkillRow, SkillVersionRow


async def _insert_system_skill(
    connection,
    *,
    actor_id: uuid.UUID,
    slug: str,
    status: str = "active",
    make_current: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    await connection.execute(
        text(
            """INSERT INTO skills
            (id,scope,slug,display_name,status,created_by_user_id)
            VALUES (:id,'system',:slug,:slug,:status,:actor)"""
        ),
        {
            "id": skill_id,
            "slug": slug,
            "status": status,
            "actor": str(actor_id),
        },
    )
    await connection.execute(
        text(
            """INSERT INTO skill_versions
            (id,skill_id,version_number,scan_decision,payload_checksum,created_by_user_id)
            VALUES (:version,:skill,1,'allow',:checksum,:actor)"""
        ),
        {
            "version": version_id,
            "skill": skill_id,
            "checksum": uuid.uuid4().hex * 2,
            "actor": str(actor_id),
        },
    )
    if make_current:
        await connection.execute(
            text("UPDATE skills SET current_version_id=:version WHERE id=:skill"),
            {"version": version_id, "skill": skill_id},
        )
    return skill_id, version_id


@pytest.mark.asyncio
async def test_repository_create_scope_personal_state_and_cursor(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner, outsider = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as connection:
            for user_id, email in ((owner, "owner@example.com"), (outsider, "out@example.com")):
                await connection.execute(
                    text("""INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'user',:now,false,0)"""),
                    {"id": str(user_id), "email": email, "now": datetime.now(UTC)},
                )
        async with factory() as session:
            repository = ProjectRepository(session)
            context = await repository.create_with_admin(owner, CreateProject("alpha", "Alpha"), "req")
            assert session.in_transaction() is False
            context = await resolve_project_context(session, owner, context.project_id, "req-resolved")
            assert session.in_transaction() is False
            view = await repository.get(context)
            assert session.in_transaction() is False
            assert view.member_count == 1 and view.agent_count == view.skill_count == view.mcp_count == 0
            async with session.begin():
                agent = AgentRow(scope="project", project_id=context.project_id, slug="alpha-agent", display_name="Alpha Agent", created_by_user_id=str(owner))
                skill = SkillRow(scope="project", project_id=context.project_id, slug="alpha-skill", display_name="Alpha Skill", created_by_user_id=str(owner))
                mcp = McpServerRow(scope="project", project_id=context.project_id, slug="alpha-mcp", display_name="Alpha MCP", created_by_user_id=str(owner))
                session.add_all((agent, skill, mcp))
            draft_summary = await repository.get(context)
            assert (draft_summary.agent_count, draft_summary.skill_count, draft_summary.mcp_count) == (0, 0, 0)
            agent_version_id, skill_version_id, mcp_version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            async with session.begin():
                await session.execute(
                    text("""INSERT INTO agent_versions
                    (id,agent_id,version_number,soul,model_ref,payload_checksum,created_by_user_id)
                    VALUES (:version,:asset,1,'Helpful','test-model',:checksum,:user)"""),
                    {"version": agent_version_id, "asset": agent.id, "checksum": "a" * 64, "user": str(owner)},
                )
                await session.execute(
                    text("""INSERT INTO skill_versions
                    (id,skill_id,version_number,scan_decision,payload_checksum,created_by_user_id)
                    VALUES (:version,:asset,1,'allow',:checksum,:user)"""),
                    {"version": skill_version_id, "asset": skill.id, "checksum": "b" * 64, "user": str(owner)},
                )
                await session.execute(
                    text("""INSERT INTO mcp_server_versions
                    (id,mcp_server_id,version_number,workflow_status,transport,payload_checksum,created_by_user_id)
                    VALUES (:version,:asset,1,'published','http',:checksum,:user)"""),
                    {"version": mcp_version_id, "asset": mcp.id, "checksum": "c" * 64, "user": str(owner)},
                )
                await session.execute(
                    text("UPDATE agents SET current_version_id=:version WHERE id=:asset"),
                    {"version": agent_version_id, "asset": agent.id},
                )
                await session.execute(
                    text("UPDATE skills SET current_version_id=:version WHERE id=:asset"),
                    {"version": skill_version_id, "asset": skill.id},
                )
                await session.execute(
                    text("UPDATE mcp_servers SET current_published_version_id=:version WHERE id=:asset"),
                    {"version": mcp_version_id, "asset": mcp.id},
                )
            summary = await repository.get(context)
            assert (summary.agent_count, summary.skill_count, summary.mcp_count) == (1, 1, 1)
            assert (await repository.update(context, ProjectChanges(display_name="Alpha Updated"))).display_name == "Alpha Updated"
            assert session.in_transaction() is False
            await repository.pin(context, True)
            assert session.in_transaction() is False
            entered = await repository.enter(context, datetime.now(UTC))
            assert session.in_transaction() is False
            assert entered.is_pinned is True and entered.last_entered_at is not None
            page = await repository.list_for_user(owner, None, None, None, 1, "req-list")
            assert [item.id for item in page.items] == [context.project_id]
            with pytest.raises(ProjectValidationFailed):
                await repository.list_for_user(owner, None, None, "not-a-cursor", 1, "req")
        async with factory() as session:
            outsider_context = context.__class__(outsider, context.project_id, context.membership_id, context.role, context.capabilities, context.membership_version, "req")
            with pytest.raises(ProjectNotFound):
                await ProjectRepository(session).get(outsider_context)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_new_project_binds_every_active_current_system_skill_by_default(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,'default-skills@example.com','user',:now,false,0)"""
                ),
                {"id": str(owner), "now": datetime.now(UTC)},
            )
            active_skill = await _insert_system_skill(
                connection,
                actor_id=owner,
                slug=f"default-active-{uuid.uuid4().hex[:8]}",
            )
            await _insert_system_skill(
                connection,
                actor_id=owner,
                slug=f"default-suspended-{uuid.uuid4().hex[:8]}",
                status="suspended",
            )
            await _insert_system_skill(
                connection,
                actor_id=owner,
                slug=f"default-draft-{uuid.uuid4().hex[:8]}",
                make_current=False,
            )

        async with factory() as session:
            context = await ProjectRepository(session).create_with_admin(
                owner,
                CreateProject("default-skills", "Default Skills"),
                "req-default-skills",
            )

        async with factory() as session:
            expected = tuple(
                (
                    await session.execute(
                        select(SkillRow.id, SkillRow.current_version_id)
                        .join(
                            SkillVersionRow,
                            SkillVersionRow.id == SkillRow.current_version_id,
                        )
                        .where(
                            SkillRow.scope == "system",
                            SkillRow.project_id.is_(None),
                            SkillRow.status == "active",
                            SkillRow.current_version_id.is_not(None),
                        )
                        .order_by(SkillRow.id)
                    )
                ).all()
            )
            bindings = tuple((await session.execute(select(ProjectSystemSkillBindingRow).where(ProjectSystemSkillBindingRow.project_id == context.project_id).order_by(ProjectSystemSkillBindingRow.system_skill_id))).scalars().all())
            agent_binding_count = await session.scalar(select(func.count()).select_from(ProjectSystemAgentBindingRow).where(ProjectSystemAgentBindingRow.project_id == context.project_id))
            mcp_binding_count = await session.scalar(select(func.count()).select_from(ProjectSystemMcpBindingRow).where(ProjectSystemMcpBindingRow.project_id == context.project_id))

        assert expected == (active_skill,)
        assert tuple(row.system_skill_id for row in bindings) == tuple(skill_id for skill_id, _version_id in expected)
        assert all(row.enabled and row.version == 1 for row in bindings)
        assert all(row.created_by_user_id == str(owner) and row.updated_by_user_id == str(owner) for row in bindings)
        assert agent_binding_count == 0
        assert mcp_binding_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_second_insert_failure_rolls_back_project(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    missing_user = uuid.uuid4()
    try:
        async with factory() as session:
            with pytest.raises(ProjectDatabaseUnavailable):
                await ProjectRepository(session).create_with_admin(missing_user, CreateProject("rolled-back", "Rollback"), "req")
        async with engine.connect() as connection:
            count = (await connection.execute(text("SELECT count(*) FROM projects WHERE slug='rolled-back'"))).scalar_one()
        assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_slug_pagination_query_and_stale_scope(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner, second = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            for user_id, email in ((owner, "owner2@example.com"), (second, "second@example.com")):
                await connection.execute(
                    text("""INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'user',:now,false,0)"""),
                    {"id": str(user_id), "email": email, "now": now},
                )

        async def create_same():
            async with factory() as session:
                return await ProjectRepository(session).create_with_admin(owner, CreateProject("same-slug", "Same"), "req")

        results = await asyncio.gather(create_same(), create_same(), return_exceptions=True)
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, ProjectSlugConflict) for result in results) == 1
        context = next(result for result in results if not isinstance(result, Exception))
        second_membership = uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE projects DROP CONSTRAINT ck_projects_status"))
            await connection.execute(text("ALTER TABLE project_memberships DROP CONSTRAINT ck_project_memberships_status"))
            await connection.execute(
                text("""INSERT INTO project_memberships
                (id,project_id,user_id,role,status) VALUES (:id,:project,:user,'viewer','disabled')"""),
                {"id": second_membership, "project": context.project_id, "user": str(second)},
            )
            for slug, status, suspended in (("hidden-status", "archived", False), ("hidden-suspended", "active", True)):
                hidden_id = uuid.uuid4()
                await connection.execute(
                    text("""INSERT INTO projects
                    (id,slug,display_name,status,is_suspended,created_by_user_id)
                    VALUES (:id,:slug,'Hidden',:status,:suspended,:user)"""),
                    {"id": hidden_id, "slug": slug, "status": status, "suspended": suspended, "user": str(owner)},
                )
                await connection.execute(
                    text("""INSERT INTO project_memberships
                    (id,project_id,user_id,role) VALUES (:id,:project,:user,'admin')"""),
                    {"id": uuid.uuid4(), "project": hidden_id, "user": str(owner)},
                )
        async with factory() as session:
            assert (await ProjectRepository(session).get(context)).member_count == 1
        async with factory() as session:
            assert (await ProjectRepository(session).list_for_user(second, None, None, None, 20, "req")).items == ()

        async with engine.begin() as connection:
            for index, (pinned, entered, label) in enumerate(((True, now, "percent%"), (True, now, "uuid-tie"), (True, None, "under_score"), (False, now, "back\\slash"), (False, None, "plain"))):
                project_id, membership_id = uuid.uuid4(), uuid.uuid4()
                created = now if index < 2 else now - timedelta(seconds=1)
                await connection.execute(
                    text("""INSERT INTO projects
                    (id,slug,display_name,created_by_user_id,created_at,updated_at)
                    VALUES (:id,:slug,:name,:user,:created,:created)"""),
                    {"id": project_id, "slug": f"page-{index}", "name": label, "user": str(owner), "created": created},
                )
                await connection.execute(
                    text("""INSERT INTO project_memberships
                    (id,project_id,user_id,role,is_pinned,last_entered_at)
                    VALUES (:id,:project,:user,'admin',:pinned,:entered)"""),
                    {"id": membership_id, "project": project_id, "user": str(owner), "pinned": pinned, "entered": entered},
                )
        async with engine.connect() as connection:
            expected = (
                (
                    await connection.execute(
                        text("""SELECT p.id FROM project_memberships m
                JOIN projects p ON p.id=m.project_id
                WHERE m.user_id=:user AND m.status='active' AND p.status='active' AND NOT p.is_suspended
                ORDER BY m.is_pinned DESC,m.last_entered_at DESC NULLS LAST,p.created_at DESC,p.id DESC"""),
                        {"user": str(owner)},
                    )
                )
                .scalars()
                .all()
            )
        seen, cursor = [], None
        while True:
            async with factory() as session:
                page = await ProjectRepository(session).list_for_user(owner, None, None, cursor, 2, "req")
            seen.extend(item.id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == expected
        assert len(seen) == len(set(seen))
        statement_count = 0

        def count_statement(*_args):
            nonlocal statement_count
            statement_count += 1

        event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
        async with factory() as session:
            await ProjectRepository(session).list_for_user(owner, None, None, None, 20, "req")
        event.remove(engine.sync_engine, "before_cursor_execute", count_statement)
        assert statement_count == 1
        filtered_ids: dict[bool, set[uuid.UUID]] = {}
        for pinned_value in (True, False):
            async with factory() as session:
                filtered = await ProjectRepository(session).list_for_user(owner, None, pinned_value, None, 20, "req")
            assert filtered.items and all(item.is_pinned is pinned_value for item in filtered.items)
            filtered_ids[pinned_value] = {item.id for item in filtered.items}
        assert filtered_ids[True].isdisjoint(filtered_ids[False])
        assert filtered_ids[True] | filtered_ids[False] == set(expected)
        for literal, expected_name in (("%", "percent%"), ("_", "under_score"), ("\\", "back\\slash")):
            async with factory() as session:
                page = await ProjectRepository(session).list_for_user(owner, literal, None, None, 20, "req")
            assert [item.display_name for item in page.items] == [expected_name]

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE project_memberships SET status='active' WHERE id=:id"),
                {"id": second_membership},
            )
        async with factory() as session:
            repository = ProjectRepository(session)
            await repository.pin(context, True)
            await repository.enter(context, now)
        async with engine.connect() as connection:
            personal = (
                await connection.execute(
                    text("""SELECT is_pinned,last_entered_at
                FROM project_memberships WHERE id=:id"""),
                    {"id": second_membership},
                )
            ).one()
        assert personal == (False, None)
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE project_memberships SET version=version+1 WHERE id=:id"), {"id": context.membership_id})
        for operation in ("get", "update", "enter", "pin"):
            async with factory() as session:
                repository = ProjectRepository(session)
                with pytest.raises(ProjectNotFound):
                    if operation == "get":
                        await repository.get(context)
                    elif operation == "update":
                        await repository.update(context, ProjectChanges(display_name="No"))
                    elif operation == "enter":
                        await repository.enter(context, now)
                    else:
                        await repository.pin(context, True)
                await session.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "pin", "enter"])
async def test_readback_dbapi_failure_rolls_back_scoped_mutation(migrated_postgres_database_url: str, operation: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,'atomic@example.com','user',:now,false,0)"""),
                {"id": str(user_id), "now": datetime.now(UTC)},
            )
        async with factory() as session:
            context = await ProjectRepository(session).create_with_admin(user_id, CreateProject("atomic", "Original"), "req")
        async with factory() as session:
            original_execute = session.execute
            calls = 0

            async def fail_readback(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise DBAPIError("SELECT secret", {"url": "postgresql://secret"}, Exception("read failed"), False)
                return await original_execute(*args, **kwargs)

            session.execute = fail_readback  # type: ignore[method-assign]
            repository = ProjectRepository(session)
            with pytest.raises(ProjectDatabaseUnavailable):
                if operation == "update":
                    await repository.update(context, ProjectChanges(display_name="Changed"))
                elif operation == "pin":
                    await repository.pin(context, True)
                else:
                    await repository.enter(context, datetime.now(UTC))
            session.execute = original_execute  # type: ignore[method-assign]
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        async with engine.connect() as connection:
            state = (
                await connection.execute(
                    text("""SELECT p.display_name,m.is_pinned,m.last_entered_at
                FROM projects p JOIN project_memberships m ON m.project_id=p.id
                WHERE p.id=:id"""),
                    {"id": context.project_id},
                )
            ).one()
        assert state == ("Original", False, None)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "list"])
async def test_public_read_dbapi_failure_rolls_back_and_session_is_reusable(migrated_postgres_database_url: str, operation: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,'read-failure@example.com','user',:now,false,0)"""),
                {"id": str(user_id), "now": datetime.now(UTC)},
            )
        async with factory() as session:
            context = await ProjectRepository(session).create_with_admin(user_id, CreateProject("read-failure", "Read"), "req")
        async with factory() as session:
            original_execute = session.execute

            async def fail_statement(*_args, **_kwargs):
                return await original_execute(text("SELECT 1 / 0"))

            session.execute = fail_statement  # type: ignore[method-assign]
            repository = ProjectRepository(session)
            with pytest.raises(ProjectDatabaseUnavailable):
                if operation == "get":
                    await repository.get(context)
                else:
                    await repository.list_for_user(user_id, None, None, None, 20, "req")
            session.execute = original_execute  # type: ignore[method-assign]
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()
