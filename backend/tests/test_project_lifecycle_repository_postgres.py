from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext, resolve_project_context
from app.projects.errors import ProjectDeletionStateConflict, ProjectForbidden, ProjectNotFound
from app.projects.invitation_repository import InvitationRepository
from app.projects.invitation_service import InvitationService
from app.projects.lifecycle_repository import ProjectLifecycleRepository
from app.projects.lifecycle_service import ProjectLifecycleService
from app.projects.membership_repository import MembershipRepository
from app.projects.membership_service import MembershipService
from app.projects.models import ProjectChanges, ProjectRole
from app.projects.repository import ProjectRepository
from app.projects.service import ProjectService

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


async def _insert_membership(
    connection,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: ProjectRole,
) -> uuid.UUID:
    membership_id = uuid.uuid4()
    await connection.execute(
        text(
            """INSERT INTO project_memberships (id,project_id,user_id,role)
            VALUES (:id,:project_id,:user_id,:role)"""
        ),
        {
            "id": membership_id,
            "project_id": project_id,
            "user_id": str(user_id),
            "role": role.value,
        },
    )
    return membership_id


async def test_request_deletion_revokes_normal_scope_and_restore_reactivates_project(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id, editor_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await _insert_user(connection, admin_id, "lifecycle-admin@example.com")
            await _insert_user(connection, editor_id, "lifecycle-editor@example.com")
            project_id = await _insert_project(connection, admin_id, "lifecycle-main")
            await _insert_membership(connection, project_id, admin_id, ProjectRole.ADMIN)
            await _insert_membership(connection, project_id, editor_id, ProjectRole.EDITOR)

        async with factory() as session:
            admin_context = await resolve_project_context(session, admin_id, project_id, "req-delete")
        async with factory() as session:
            pending = await ProjectLifecycleService(ProjectLifecycleRepository(session)).request_deletion(
                admin_context,
                now,
            )

        assert pending.status == "pending_deletion"
        assert pending.deletion_effective_at == now + timedelta(days=30)
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """SELECT status,deletion_requested_at,deletion_effective_at,
                        deletion_requested_by_user_id,membership_version
                        FROM projects WHERE id=:project_id"""
                    ),
                    {"project_id": project_id},
                )
            ).one()
        assert row == (
            "pending_deletion",
            now,
            now + timedelta(days=30),
            str(admin_id),
            2,
        )

        for user_id in (admin_id, editor_id):
            async with factory() as session:
                with pytest.raises(ProjectNotFound):
                    await resolve_project_context(session, user_id, project_id, "req-hidden")

        for operation in ("get", "update", "enter"):
            async with factory() as session:
                repository = ProjectRepository(session)
                with pytest.raises(ProjectNotFound):
                    if operation == "get":
                        await repository.get(admin_context)
                    elif operation == "update":
                        await repository.update(admin_context, ProjectChanges(display_name="Hidden"))
                    else:
                        await repository.enter(admin_context, now)

        async with factory() as session:
            with pytest.raises(ProjectNotFound):
                await MembershipService(MembershipRepository(session)).list_members(admin_context)
        async with factory() as session:
            with pytest.raises(ProjectNotFound):
                await InvitationService(InvitationRepository(session)).create(
                    admin_context,
                    "new-member@example.com",
                    ProjectRole.VIEWER,
                    now,
                )

        async with factory() as session:
            default_page = await ProjectService(ProjectRepository(session)).list(
                admin_id,
                request_id="req-list-default",
            )
        assert default_page.items == ()

        async with factory() as session:
            admin_page = await ProjectService(ProjectRepository(session)).list(
                admin_id,
                include_recoverable=True,
                request_id="req-list-recoverable",
            )
        assert [item.id for item in admin_page.items] == [project_id]
        assert admin_page.items[0].deletion_effective_at == now + timedelta(days=30)

        async with factory() as session:
            editor_page = await ProjectService(ProjectRepository(session)).list(
                editor_id,
                include_recoverable=True,
                request_id="req-list-editor",
            )
        assert editor_page.items == ()

        async with factory() as session:
            restored = await ProjectLifecycleService(ProjectLifecycleRepository(session)).restore(
                admin_id,
                project_id,
                "req-restore",
                now + timedelta(days=29),
            )
        assert restored.status == "active"
        assert restored.deletion_effective_at is None
        async with engine.connect() as connection:
            restored_row = (
                await connection.execute(
                    text(
                        """SELECT status,deletion_requested_at,deletion_effective_at,
                        deletion_requested_by_user_id,membership_version
                        FROM projects WHERE id=:project_id"""
                    ),
                    {"project_id": project_id},
                )
            ).one()
        assert restored_row == ("active", None, None, None, 3)
        async with factory() as session:
            resolved = await resolve_project_context(session, admin_id, project_id, "req-restored")
        assert resolved.project_id == project_id
    finally:
        await engine.dispose()


async def test_request_deletion_revalidates_actor_version_and_database_role(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id, editor_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await _insert_user(connection, admin_id, "revalidate-admin@example.com")
            await _insert_user(connection, editor_id, "revalidate-editor@example.com")
            project_id = await _insert_project(connection, admin_id, "lifecycle-revalidate")
            await _insert_membership(connection, project_id, admin_id, ProjectRole.ADMIN)
            editor_membership_id = await _insert_membership(connection, project_id, editor_id, ProjectRole.EDITOR)

        forged_admin_context = ProjectContext(
            user_id=editor_id,
            project_id=project_id,
            membership_id=editor_membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="req-forged",
        )
        async with factory() as session:
            with pytest.raises(ProjectForbidden):
                await ProjectLifecycleService(ProjectLifecycleRepository(session)).request_deletion(
                    forged_admin_context,
                    now,
                )

        stale_context = ProjectContext(
            user_id=editor_id,
            project_id=project_id,
            membership_id=editor_membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=2,
            request_id="req-stale",
        )
        async with factory() as session:
            with pytest.raises(ProjectNotFound):
                await ProjectLifecycleService(ProjectLifecycleRepository(session)).request_deletion(
                    stale_context,
                    now,
                )

        async with engine.connect() as connection:
            state = (
                await connection.execute(
                    text("SELECT status,membership_version FROM projects WHERE id=:project_id"),
                    {"project_id": project_id},
                )
            ).one()
        assert state == ("active", 1)
    finally:
        await engine.dispose()


async def test_restore_deadline_non_admin_and_unknown_project_share_safe_conflict(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id, editor_id = uuid.uuid4(), uuid.uuid4()
    requested_at = datetime.now(UTC)
    effective_at = requested_at + timedelta(days=30)
    try:
        async with engine.begin() as connection:
            await _insert_user(connection, admin_id, "deadline-admin@example.com")
            await _insert_user(connection, editor_id, "deadline-editor@example.com")
            project_id = await _insert_project(connection, admin_id, "lifecycle-deadline")
            await _insert_membership(connection, project_id, admin_id, ProjectRole.ADMIN)
            await _insert_membership(connection, project_id, editor_id, ProjectRole.EDITOR)
            await connection.execute(
                text(
                    """UPDATE projects SET status='pending_deletion',
                    deletion_requested_at=:requested_at,
                    deletion_effective_at=:effective_at,
                    deletion_requested_by_user_id=:admin_id
                    WHERE id=:project_id"""
                ),
                {
                    "requested_at": requested_at,
                    "effective_at": effective_at,
                    "admin_id": str(admin_id),
                    "project_id": project_id,
                },
            )

        attempts = (
            (admin_id, project_id, effective_at),
            (editor_id, project_id, effective_at - timedelta(seconds=1)),
            (admin_id, uuid.uuid4(), effective_at - timedelta(seconds=1)),
        )
        for user_id, target_project_id, attempt_at in attempts:
            async with factory() as session:
                with pytest.raises(ProjectDeletionStateConflict) as exc_info:
                    await ProjectLifecycleService(ProjectLifecycleRepository(session)).restore(
                        user_id,
                        target_project_id,
                        "req-safe-conflict",
                        attempt_at,
                    )
            assert exc_info.value.code == "project_deletion_state_conflict"
            assert exc_info.value.__dict__ == {}

        async with factory() as session:
            page = await ProjectService(ProjectRepository(session)).list(
                admin_id,
                include_recoverable=True,
                request_id="req-expired-list",
            )
        # The real database clock is before the future deadline, so the admin
        # can still see the project even though a restore attempt at the exact
        # deadline was correctly rejected.
        assert [item.id for item in page.items] == [project_id]

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE projects SET deletion_effective_at=:expired_at
                    WHERE id=:project_id"""
                ),
                {
                    "expired_at": requested_at - timedelta(seconds=1),
                    "project_id": project_id,
                },
            )
        async with factory() as session:
            expired_page = await ProjectService(ProjectRepository(session)).list(
                admin_id,
                include_recoverable=True,
                request_id="req-expired-list",
            )
        assert expired_page.items == ()
    finally:
        await engine.dispose()


async def test_delete_and_restore_lock_project_row(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = uuid.uuid4()
    statements: list[str] = []
    now = datetime.now(UTC)

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "FOR UPDATE" in statement:
            statements.append(statement)

    try:
        async with engine.begin() as connection:
            await _insert_user(connection, admin_id, "lock-admin@example.com")
            project_id = await _insert_project(connection, admin_id, "lifecycle-lock")
            await _insert_membership(connection, project_id, admin_id, ProjectRole.ADMIN)
        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-lock")

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with factory() as session:
            await ProjectLifecycleService(ProjectLifecycleRepository(session)).request_deletion(context, now)
        async with factory() as session:
            await ProjectLifecycleService(ProjectLifecycleRepository(session)).restore(
                admin_id,
                project_id,
                "req-lock-restore",
                now + timedelta(days=1),
            )
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

        assert len(statements) >= 7
        assert "FROM projects" in statements[0]
        assert "FROM project_memberships" in statements[1]
        assert "FROM project_memberships" in statements[2]
        assert "FROM runs" in statements[3]
        assert "FROM projects" in statements[4]
        assert "FROM project_memberships" in statements[5]
        assert "FROM project_memberships" in statements[6]
    finally:
        if event.contains(engine.sync_engine, "before_cursor_execute", capture):
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        await engine.dispose()
