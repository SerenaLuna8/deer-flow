from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound, ProjectSlugConflict, ProjectValidationFailed
from app.projects.models import CreateProject, ProjectChanges
from app.projects.repository import ProjectRepository


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
            view = await repository.get(context)
            assert view.member_count == 1 and view.agent_count == view.skill_count == view.mcp_count == 0
            await repository.pin(context, True)
            entered = await repository.enter(context, datetime.now(UTC))
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
        for literal, expected_name in (("%", "percent%"), ("_", "under_score"), ("\\", "back\\slash")):
            async with factory() as session:
                page = await ProjectRepository(session).list_for_user(owner, literal, None, None, 20, "req")
            assert [item.display_name for item in page.items] == [expected_name]

        context = next(result for result in results if not isinstance(result, Exception))
        second_membership = uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text("""INSERT INTO project_memberships
                (id,project_id,user_id,role) VALUES (:id,:project,:user,'viewer')"""),
                {"id": second_membership, "project": context.project_id, "user": str(second)},
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
