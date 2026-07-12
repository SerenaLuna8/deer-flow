from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.context import resolve_project_context
from app.projects.invitation_models import ProjectInvitationConflict, ProjectInvitationInvalid
from app.projects.invitation_repository import InvitationRepository
from app.projects.invitation_service import InvitationService
from app.projects.models import ProjectRole

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]
NOW = datetime(2026, 7, 12, 8, 30, tzinfo=UTC)


async def _insert_user(connection, user_id: uuid.UUID, email: str) -> None:
    await connection.execute(
        text(
            """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
            VALUES (:id,:email,'user',:now,false,0)"""
        ),
        {"id": str(user_id), "email": email, "now": NOW},
    )


async def _insert_project(connection, owner_id: uuid.UUID, slug: str) -> uuid.UUID:
    project_id = uuid.uuid4()
    await connection.execute(
        text(
            """INSERT INTO projects (id,slug,display_name,created_by_user_id)
            VALUES (:id,:slug,:slug,:owner_id)"""
        ),
        {"id": project_id, "slug": slug, "owner_id": str(owner_id)},
    )
    return project_id


async def _insert_membership(
    connection,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: ProjectRole,
    *,
    status: str = "active",
    version: int = 1,
) -> uuid.UUID:
    membership_id = uuid.uuid4()
    ended = NOW - timedelta(days=1) if status != "active" else None
    await connection.execute(
        text(
            """INSERT INTO project_memberships
            (id,project_id,user_id,role,status,version,ended_at,retention_until,ended_by_user_id,end_reason)
            VALUES (:id,:project_id,:user_id,:role,:status,:version,:ended_at,:retention_until,:ended_by,:end_reason)"""
        ),
        {
            "id": membership_id,
            "project_id": project_id,
            "user_id": str(user_id),
            "role": role.value,
            "status": status,
            "version": version,
            "ended_at": ended,
            "retention_until": ended + timedelta(days=30) if ended else None,
            "ended_by": str(user_id) if ended else None,
            "end_reason": status if ended else None,
        },
    )
    return membership_id


async def _setup_project(engine, slug: str = "invitation-project"):
    admin_id = uuid.uuid4()
    async with engine.begin() as connection:
        await _insert_user(connection, admin_id, "admin@example.com")
        project_id = await _insert_project(connection, admin_id, slug)
        await _insert_membership(connection, project_id, admin_id, ProjectRole.ADMIN)
    return admin_id, project_id


async def test_create_returns_plaintext_once_and_persists_only_hash(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        admin_id, project_id = await _setup_project(engine)
        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-create")
        async with factory() as session:
            service = InvitationService(InvitationRepository(session))
            created = await service.create(context, " MEMBER@Example.com ", ProjectRole.EDITOR, NOW)
            stored = await service.repository.get(created.invitation.id)

        assert created.token
        assert created.token not in repr(created)
        assert created.invitation.invited_email == "member@example.com"
        assert created.invitation.expires_at == NOW + timedelta(days=7)
        assert stored is not None
        assert stored.token_hash == hashlib.sha256(created.token.encode("utf-8")).hexdigest()
        assert len(stored.token_hash) == 64 and stored.token_hash == stored.token_hash.lower()
        assert created.token not in repr(stored)
        assert not hasattr(created.invitation, "token_hash")
    finally:
        await engine.dispose()


async def test_create_expires_old_pending_before_reinvite_and_rejects_live_duplicate(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        admin_id, project_id = await _setup_project(engine, "invitation-reinvite")
        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-reinvite")
        async with factory() as session:
            first = await InvitationService(InvitationRepository(session)).create(
                context,
                "member@example.com",
                ProjectRole.VIEWER,
                NOW - timedelta(days=8),
            )
        async with factory() as session:
            second = await InvitationService(InvitationRepository(session)).create(
                context,
                "member@example.com",
                ProjectRole.RUNNER,
                NOW,
            )

        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """SELECT id,status,version FROM project_invitations
                        WHERE project_id=:project_id ORDER BY created_at,id"""
                    ),
                    {"project_id": project_id},
                )
            ).all()
        by_id = {row.id: row for row in rows}
        assert by_id[first.invitation.id].status == "expired"
        assert by_id[first.invitation.id].version == 2
        assert by_id[second.invitation.id].status == "pending"

        with pytest.raises(ProjectInvitationConflict):
            async with factory() as session:
                await InvitationService(InvitationRepository(session)).create(
                    context,
                    "member@example.com",
                    ProjectRole.EDITOR,
                    NOW,
                )
    finally:
        await engine.dispose()


async def test_revoke_requires_expected_version_and_never_returns_secret_fields(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        admin_id, project_id = await _setup_project(engine, "invitation-revoke")
        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-revoke")
        async with factory() as session:
            created = await InvitationService(InvitationRepository(session)).create(
                context,
                "member@example.com",
                ProjectRole.VIEWER,
                NOW,
            )

        with pytest.raises(ProjectInvitationConflict):
            async with factory() as session:
                await InvitationService(InvitationRepository(session)).revoke(
                    context,
                    created.invitation.id,
                    expected_version=2,
                    now=NOW,
                )

        async with factory() as session:
            revoked = await InvitationService(InvitationRepository(session)).revoke(
                context,
                created.invitation.id,
                expected_version=1,
                now=NOW,
            )
        assert revoked.status == "revoked"
        assert revoked.version == 2
        assert not hasattr(revoked, "token_hash")

        with pytest.raises(ProjectInvitationInvalid) as exc_info:
            async with factory() as session:
                await InvitationService(InvitationRepository(session)).revoke(
                    context,
                    uuid.uuid4(),
                    expected_version=1,
                    now=NOW,
                )
        assert exc_info.value.__dict__ == {}
        assert "member@example.com" not in str(exc_info.value)
        assert created.token not in str(exc_info.value)
    finally:
        await engine.dispose()


async def test_expired_invitation_cannot_be_redeemed(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    member_id = uuid.uuid4()
    try:
        admin_id, project_id = await _setup_project(engine, "invitation-expired")
        async with engine.begin() as connection:
            await _insert_user(connection, member_id, "member@example.com")
        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-expired")
        async with factory() as session:
            service = InvitationService(InvitationRepository(session))
            created = await service.create(
                context,
                "member@example.com",
                ProjectRole.EDITOR,
                NOW,
            )
            claim = await service.claim(created.token, NOW)

        with pytest.raises(ProjectInvitationInvalid):
            async with factory() as session:
                await InvitationService(InvitationRepository(session)).redeem(
                    member_id,
                    "member@example.com",
                    claim,
                    created.invitation.expires_at,
                )

        async with engine.connect() as connection:
            invitation_status = (
                await connection.execute(
                    text("SELECT status FROM project_invitations WHERE id=:id"),
                    {"id": created.invitation.id},
                )
            ).scalar_one()
            membership_count = (
                await connection.execute(
                    text("SELECT count(*) FROM project_memberships WHERE project_id=:project AND user_id=:user"),
                    {"project": project_id, "user": str(member_id)},
                )
            ).scalar_one()
        assert invitation_status == "pending"
        assert membership_count == 0
    finally:
        await engine.dispose()


async def test_redeem_reactivates_membership_reusing_id_and_clearing_end_metadata(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    member_id = uuid.uuid4()
    try:
        admin_id, project_id = await _setup_project(engine, "invitation-reactivate")
        async with engine.begin() as connection:
            await _insert_user(connection, member_id, "member@example.com")
            membership_id = await _insert_membership(
                connection,
                project_id,
                member_id,
                ProjectRole.VIEWER,
                status="removed",
                version=4,
            )
        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-reactivate")
        async with factory() as session:
            service = InvitationService(InvitationRepository(session))
            created = await service.create(context, "member@example.com", ProjectRole.RUNNER, NOW)
            claim = await service.claim(created.token, NOW)
        async with factory() as session:
            redeemed = await InvitationService(InvitationRepository(session)).redeem(
                member_id,
                " MEMBER@example.com ",
                claim,
                NOW,
            )

        assert redeemed.membership_id == membership_id
        assert redeemed.project_slug == "invitation-reactivate"
        assert redeemed.role is ProjectRole.RUNNER
        async with engine.connect() as connection:
            membership = (
                await connection.execute(
                    text(
                        """SELECT role,status,version,ended_at,retention_until,ended_by_user_id,end_reason
                        FROM project_memberships WHERE id=:id"""
                    ),
                    {"id": membership_id},
                )
            ).one()
            invitation = (
                await connection.execute(
                    text("SELECT status,version,redeemed_by_user_id,redeemed_at FROM project_invitations WHERE id=:id"),
                    {"id": created.invitation.id},
                )
            ).one()
            project_version = (
                await connection.execute(
                    text("SELECT membership_version FROM projects WHERE id=:id"),
                    {"id": project_id},
                )
            ).scalar_one()
        assert membership == (ProjectRole.RUNNER.value, "active", 5, None, None, None, None)
        assert invitation.status == "redeemed"
        assert invitation.version == 2
        assert invitation.redeemed_by_user_id == str(member_id)
        assert invitation.redeemed_at == NOW
        assert project_version == 2
    finally:
        await engine.dispose()


async def test_concurrent_redeem_succeeds_once_and_creates_one_membership(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    member_id = uuid.uuid4()
    try:
        admin_id, project_id = await _setup_project(engine, "invitation-race")
        async with engine.begin() as connection:
            await _insert_user(connection, member_id, "member@example.com")
        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-race")
        async with factory() as session:
            service = InvitationService(InvitationRepository(session))
            created = await service.create(context, "member@example.com", ProjectRole.EDITOR, NOW)
            claim = await service.claim(created.token, NOW)

        async def redeem():
            async with factory() as session:
                return await InvitationService(InvitationRepository(session)).redeem(
                    member_id,
                    "member@example.com",
                    claim,
                    NOW,
                )

        results = await asyncio.gather(redeem(), redeem(), return_exceptions=True)
        assert sum(not isinstance(result, Exception) for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], ProjectInvitationInvalid)

        async with engine.connect() as connection:
            membership_count = (
                await connection.execute(
                    text("SELECT count(*) FROM project_memberships WHERE project_id=:project AND user_id=:user"),
                    {"project": project_id, "user": str(member_id)},
                )
            ).scalar_one()
            project_version = (
                await connection.execute(
                    text("SELECT membership_version FROM projects WHERE id=:id"),
                    {"id": project_id},
                )
            ).scalar_one()
        assert membership_count == 1
        assert project_version == 2
    finally:
        await engine.dispose()


async def test_expired_reinvite_and_redeem_do_not_deadlock(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    member_id = uuid.uuid4()
    try:
        admin_id, project_id = await _setup_project(engine, "invitation-expiry-race")
        async with engine.begin() as connection:
            await _insert_user(connection, member_id, "member@example.com")
        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-expiry-race")
        created_at = NOW - timedelta(days=7)
        async with factory() as session:
            service = InvitationService(InvitationRepository(session))
            old = await service.create(context, "member@example.com", ProjectRole.VIEWER, created_at)
            claim = await service.claim(old.token, created_at)

        async def reinvite():
            async with factory() as session:
                return await InvitationService(InvitationRepository(session)).create(
                    context,
                    "member@example.com",
                    ProjectRole.EDITOR,
                    NOW,
                )

        async def redeem_expired():
            async with factory() as session:
                return await InvitationService(InvitationRepository(session)).redeem(
                    member_id,
                    "member@example.com",
                    claim,
                    NOW,
                )

        results = await asyncio.wait_for(
            asyncio.gather(reinvite(), redeem_expired(), return_exceptions=True),
            timeout=5,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert any(isinstance(result, ProjectInvitationInvalid) for result in results)
        async with engine.connect() as connection:
            statuses = (
                (
                    await connection.execute(
                        text(
                            """SELECT status FROM project_invitations
                        WHERE project_id=:project_id ORDER BY created_at,id"""
                        ),
                        {"project_id": project_id},
                    )
                )
                .scalars()
                .all()
            )
        assert statuses == ["expired", "pending"]
    finally:
        await engine.dispose()


async def test_create_and_redeem_use_documented_lock_order(migrated_postgres_database_url: str) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    member_id = uuid.uuid4()
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "FOR UPDATE" in statement:
            statements.append(statement)

    try:
        admin_id, project_id = await _setup_project(engine, "invitation-lock-order")
        async with engine.begin() as connection:
            await _insert_user(connection, member_id, "member@example.com")
        async with factory() as session:
            context = await resolve_project_context(session, admin_id, project_id, "req-lock")

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with factory() as session:
            service = InvitationService(InvitationRepository(session))
            created = await service.create(context, "member@example.com", ProjectRole.VIEWER, NOW)
            claim = await service.claim(created.token, NOW)
        create_statements = list(statements)
        statements.clear()
        async with factory() as session:
            await InvitationService(InvitationRepository(session)).redeem(
                member_id,
                "member@example.com",
                claim,
                NOW,
            )
        redeem_statements = list(statements)
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

        assert create_statements
        assert "FROM projects" in create_statements[0]
        assert "project_invitations" not in create_statements[0]
        assert len(redeem_statements) >= 3
        assert "FROM projects" in redeem_statements[0]
        assert "FROM project_invitations" in redeem_statements[1]
        assert "FROM project_memberships" in redeem_statements[2]
    finally:
        if event.contains(engine.sync_engine, "before_cursor_execute", capture):
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        await engine.dispose()
