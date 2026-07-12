from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.context import resolve_project_context
from app.projects.errors import ProjectLastAdmin, ProjectMembershipVersionConflict, ProjectNotFound
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectRole

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]


async def _insert_user(connection, user_id: uuid.UUID, email: str) -> None:
    await connection.execute(
        text(
            """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
            VALUES (:id,:email,'user',:now,false,0)"""
        ),
        {"id": str(user_id), "email": email, "now": datetime.now(UTC)},
    )


async def _insert_project(connection, owner_id: uuid.UUID, slug: str) -> uuid.UUID:
    project_id = uuid.uuid4()
    await connection.execute(
        text(
            """INSERT INTO projects (id,slug,display_name,created_by_user_id)
            VALUES (:id,:slug,:name,:owner)"""
        ),
        {"id": project_id, "slug": slug, "name": slug, "owner": str(owner_id)},
    )
    return project_id


async def _insert_membership(connection, project_id: uuid.UUID, user_id: uuid.UUID, role: ProjectRole) -> uuid.UUID:
    membership_id = uuid.uuid4()
    await connection.execute(
        text(
            """INSERT INTO project_memberships (id,project_id,user_id,role)
            VALUES (:id,:project_id,:user_id,:role)"""
        ),
        {"id": membership_id, "project_id": project_id, "user_id": str(user_id), "role": role.value},
    )
    return membership_id


async def test_member_mutations_are_scoped_versioned_and_record_lifecycle(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id, editor_id, viewer_id, outsider_id = (uuid.uuid4() for _ in range(4))
    try:
        async with engine.begin() as connection:
            for user_id, email in (
                (admin_id, "admin@example.com"),
                (editor_id, "editor@example.com"),
                (viewer_id, "viewer@example.com"),
                (outsider_id, "outsider@example.com"),
            ):
                await _insert_user(connection, user_id, email)
            project_id = await _insert_project(connection, admin_id, "members-main")
            admin_membership_id = await _insert_membership(connection, project_id, admin_id, ProjectRole.ADMIN)
            editor_membership_id = await _insert_membership(connection, project_id, editor_id, ProjectRole.EDITOR)
            viewer_membership_id = await _insert_membership(connection, project_id, viewer_id, ProjectRole.VIEWER)
            other_project_id = await _insert_project(connection, outsider_id, "members-other")
            other_membership_id = await _insert_membership(connection, other_project_id, outsider_id, ProjectRole.ADMIN)

        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-members")
        async with factory() as session:
            members = await MembershipService(MembershipRepository(session)).list_members(context)
        assert sorted([(member.membership_id, member.account_email, member.role) for member in members], key=lambda item: item[1]) == [
            (admin_membership_id, "admin@example.com", ProjectRole.ADMIN),
            (editor_membership_id, "editor@example.com", ProjectRole.EDITOR),
            (viewer_membership_id, "viewer@example.com", ProjectRole.VIEWER),
        ]
        assert all(member.status == "active" and member.version == 1 for member in members)

        async with factory() as session:
            changed = await MembershipService(MembershipRepository(session)).change_role(
                context,
                editor_membership_id,
                ProjectRole.RUNNER,
                expected_version=1,
            )
        assert changed.role is ProjectRole.RUNNER
        assert changed.version == 2
        async with engine.connect() as connection:
            project_version = (await connection.execute(text("SELECT membership_version FROM projects WHERE id=:id"), {"id": project_id})).scalar_one()
        assert project_version == 2

        with pytest.raises(ProjectMembershipVersionConflict):
            async with factory() as session:
                await MembershipService(MembershipRepository(session)).change_role(
                    context,
                    editor_membership_id,
                    ProjectRole.EDITOR,
                    expected_version=1,
                )

        for operation in ("change", "remove"):
            with pytest.raises(ProjectNotFound):
                async with factory() as session:
                    service = MembershipService(MembershipRepository(session))
                    if operation == "change":
                        await service.change_role(context, other_membership_id, ProjectRole.VIEWER, expected_version=1)
                    else:
                        await service.remove(context, other_membership_id, expected_version=1)

        before_remove = datetime.now(UTC)
        async with factory() as session:
            removed = await MembershipService(MembershipRepository(session)).remove(context, viewer_membership_id, expected_version=1)
        assert removed.status == "removed"
        assert removed.version == 2
        assert removed.account_email == "viewer@example.com"
        async with engine.connect() as connection:
            removed_row = (
                await connection.execute(
                    text(
                        """SELECT ended_at,retention_until,ended_by_user_id,end_reason
                        FROM project_memberships WHERE id=:id"""
                    ),
                    {"id": viewer_membership_id},
                )
            ).one()
            project_version = (await connection.execute(text("SELECT membership_version FROM projects WHERE id=:id"), {"id": project_id})).scalar_one()
        assert removed_row.end_reason == "removed"
        assert removed_row.ended_by_user_id == str(admin_id)
        assert removed_row.ended_at >= before_remove
        assert removed_row.retention_until == removed_row.ended_at + timedelta(days=30)
        assert project_version == 3

        async with factory() as session:
            remaining = await MembershipService(MembershipRepository(session)).list_members(context)
        assert {member.membership_id for member in remaining} == {admin_membership_id, editor_membership_id}

        with pytest.raises(ProjectNotFound):
            async with factory() as session:
                await MembershipService(MembershipRepository(session)).remove(context, viewer_membership_id, expected_version=2)
    finally:
        await engine.dispose()


async def test_leave_and_remove_immediately_revoke_project_context(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id, leaver_id, removed_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as connection:
            for user_id, email in ((admin_id, "admin@example.com"), (leaver_id, "leaver@example.com"), (removed_id, "removed@example.com")):
                await _insert_user(connection, user_id, email)
            project_id = await _insert_project(connection, admin_id, "members-revoked")
            await _insert_membership(connection, project_id, admin_id, ProjectRole.ADMIN)
            leaver_membership_id = await _insert_membership(connection, project_id, leaver_id, ProjectRole.EDITOR)
            removed_membership_id = await _insert_membership(connection, project_id, removed_id, ProjectRole.VIEWER)
        async with factory() as session:
            admin_context = await resolve_project_context(session, admin_id, project_id, "req-admin")
        async with factory() as session:
            leaver_context = await resolve_project_context(session, leaver_id, project_id, "req-leaver")

        async with factory() as session:
            left = await MembershipService(MembershipRepository(session)).leave(leaver_context, expected_version=1)
        assert left.membership_id == leaver_membership_id
        assert left.status == "left"
        assert left.version == 2
        async with factory() as session:
            with pytest.raises(ProjectNotFound):
                await resolve_project_context(session, leaver_id, project_id, "req-left")

        async with factory() as session:
            removed = await MembershipService(MembershipRepository(session)).remove(admin_context, removed_membership_id, expected_version=1)
        assert removed.status == "removed"
        assert removed.version == 2
        async with factory() as session:
            with pytest.raises(ProjectNotFound):
                await resolve_project_context(session, removed_id, project_id, "req-removed")
        async with engine.connect() as connection:
            project_version = (await connection.execute(text("SELECT membership_version FROM projects WHERE id=:id"), {"id": project_id})).scalar_one()
        assert project_version == 3
    finally:
        await engine.dispose()


async def test_project_row_is_locked_before_target_membership(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id, viewer_id = uuid.uuid4(), uuid.uuid4()
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "FOR UPDATE" in statement:
            statements.append(statement)

    try:
        async with engine.begin() as connection:
            await _insert_user(connection, admin_id, "admin@example.com")
            await _insert_user(connection, viewer_id, "viewer@example.com")
            project_id = await _insert_project(connection, admin_id, "members-lock-order")
            await _insert_membership(connection, project_id, admin_id, ProjectRole.ADMIN)
            viewer_membership_id = await _insert_membership(connection, project_id, viewer_id, ProjectRole.VIEWER)
        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-lock")

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with factory() as session:
            await MembershipService(MembershipRepository(session)).change_role(context, viewer_membership_id, ProjectRole.RUNNER, expected_version=1)
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

        assert len(statements) >= 2
        assert "FROM projects" in statements[0]
        assert "FROM project_memberships" in statements[1]
    finally:
        if event.contains(engine.sync_engine, "before_cursor_execute", capture):
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        await engine.dispose()


async def test_concurrent_admin_demotions_leave_one_active_admin(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await _insert_user(connection, first_id, "first-admin@example.com")
            await _insert_user(connection, second_id, "second-admin@example.com")
            project_id = await _insert_project(connection, first_id, "members-race")
            first_membership_id = await _insert_membership(connection, project_id, first_id, ProjectRole.ADMIN)
            second_membership_id = await _insert_membership(connection, project_id, second_id, ProjectRole.ADMIN)
        async with factory() as session:
            first_context = await resolve_project_context(session, first_id, project_id, "req-first")
        async with factory() as session:
            second_context = await resolve_project_context(session, second_id, project_id, "req-second")

        async def demote(context, membership_id):
            async with factory() as session:
                return await MembershipService(MembershipRepository(session)).change_role(context, membership_id, ProjectRole.VIEWER, expected_version=1)

        results = await asyncio.gather(
            demote(first_context, first_membership_id),
            demote(second_context, second_membership_id),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1 and isinstance(failures[0], ProjectLastAdmin)

        async with engine.connect() as connection:
            active_admin_count = (
                await connection.execute(
                    text(
                        """SELECT count(*) FROM project_memberships
                        WHERE project_id=:project_id AND status='active' AND role='admin'"""
                    ),
                    {"project_id": project_id},
                )
            ).scalar_one()
            membership_version = (await connection.execute(text("SELECT membership_version FROM projects WHERE id=:id"), {"id": project_id})).scalar_one()
        assert active_admin_count == 1
        assert membership_version == 2
    finally:
        await engine.dispose()
