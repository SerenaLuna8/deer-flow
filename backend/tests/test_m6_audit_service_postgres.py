from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from support.m4_private_threads import seed_m4_thread_database

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditAuthorityRejected,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
    SystemAuditContext,
    resolve_system_audit_context,
)
from app.audit.service import AuditService
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow


def _keyring(active: str = "audit-v2") -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id=active,
        _keys={"audit-v1": b"1" * 32, "audit-v2": b"2" * 32},
    )


def _project_context(context) -> ProjectContext:
    return ProjectContext(
        user_id=context.user_id,
        project_id=context.project_id,
        membership_id=context.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=context.capabilities,
        membership_version=context.membership_version,
        request_id=context.request_id,
    )


async def _append_run(
    service: AuditService,
    session,
    context,
    target_id: uuid.UUID,
    *,
    request_id: str,
):
    return await service.append(
        session,
        AuditActor.user(context.user_id),
        AuditAction.RUN_ADMITTED,
        AuditTarget(
            kind=AuditTargetKind.RUN,
            authority_id=target_id,
            project_id=context.project_id,
        ),
        AuditOutcome.SUCCESS,
        {"job_type": "private_run", "non_interactive": False},
        request_id=request_id,
    )


async def _invalidate_project_audit_authority(factory, context, change: str) -> None:
    async with factory.begin() as session:
        if change == "project_suspended":
            await session.execute(update(ProjectRow).where(ProjectRow.id == context.project_id).values(is_suspended=True))
            return
        if change == "membership_removed":
            await session.execute(
                update(ProjectMembershipRow)
                .where(ProjectMembershipRow.id == context.membership_id)
                .values(
                    status="removed",
                    version=ProjectMembershipRow.version + 1,
                )
            )
            return
        if change == "admin_downgraded":
            await session.execute(
                update(ProjectMembershipRow)
                .where(ProjectMembershipRow.id == context.membership_id)
                .values(
                    role=ProjectRole.VIEWER.value,
                    version=ProjectMembershipRow.version + 1,
                )
            )
            return
        raise AssertionError(f"unsupported authority change: {change}")


@pytest.mark.postgres
@pytest.mark.anyio
async def test_append_persists_only_allowlisted_metadata_and_hmac_target(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = AuditService(seed.factory, _keyring())
    target_id = uuid.uuid4()
    try:
        async with seed.factory() as session, session.begin():
            record = await _append_run(
                service,
                session,
                seed.owner_a,
                target_id,
                request_id="audit-append",
            )

        async with seed.factory() as session:
            row = await session.get(AuditLogRow, record.id)
        assert row is not None
        assert row.project_id == seed.owner_a.project_id
        assert row.actor_user_id == str(seed.owner_a.user_id)
        assert row.actor_process is None
        assert row.action == "run.admitted"
        assert row.target_kind == "run"
        assert row.target_ref_key_id == "audit-v2"
        assert len(row.target_ref_hmac) == 64
        assert str(target_id) not in repr(row.__dict__)
        assert row.request_id != "audit-append"
        assert len(row.request_id) == 64
        assert row.metadata_json == {
            "job_type": "private_run",
            "non_interactive": False,
        }
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_committed_audit_rows_are_database_immutable(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = AuditService(seed.factory, _keyring())
    try:
        async with seed.factory() as session, session.begin():
            record = await _append_run(
                service,
                session,
                seed.owner_a,
                uuid.uuid4(),
                request_id="audit-immutable",
            )

        with pytest.raises(DBAPIError):
            async with seed.factory() as session, session.begin():
                await session.execute(update(AuditLogRow).where(AuditLogRow.id == record.id).values(outcome="failed"))
        with pytest.raises(DBAPIError):
            async with seed.factory() as session, session.begin():
                await session.execute(delete(AuditLogRow).where(AuditLogRow.id == record.id))
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_audit_append_uses_caller_transaction(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = AuditService(seed.factory, _keyring())
    try:
        with pytest.raises(RuntimeError, match="domain rollback"):
            async with seed.factory() as session, session.begin():
                await _append_run(
                    service,
                    session,
                    seed.owner_a,
                    uuid.uuid4(),
                    request_id="audit-rollback",
                )
                raise RuntimeError("domain rollback")

        async with seed.factory() as session:
            assert await session.scalar(select(AuditLogRow.id)) is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_reader_is_scoped_and_rotation_lookup_finds_old_ref(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    old_service = AuditService(seed.factory, _keyring("audit-v1"))
    rotated_service = AuditService(seed.factory, _keyring("audit-v2"))
    shared_target = uuid.uuid4()
    try:
        async with seed.factory() as session, session.begin():
            first = await _append_run(
                old_service,
                session,
                seed.owner_a,
                shared_target,
                request_id="audit-project-a",
            )
            second = await _append_run(
                old_service,
                session,
                seed.project_b_owner_a,
                uuid.uuid4(),
                request_id="audit-project-b",
            )

        project_a = await rotated_service.list_project_new_session(
            _project_context(seed.owner_a),
            target=AuditTarget(
                kind=AuditTargetKind.RUN,
                authority_id=shared_target,
                project_id=seed.owner_a.project_id,
            ),
        )
        project_b = await rotated_service.list_project_new_session(
            _project_context(seed.project_b_owner_a),
        )

        assert tuple(item.id for item in project_a.items) == (first.id,)
        assert tuple(item.id for item in project_b.items) == (second.id,)
        assert all(item.target_kind is AuditTargetKind.RUN for item in project_a.items)
        assert all(not hasattr(item, "target_ref_hmac") for item in project_a.items)
    finally:
        await seed.engine.dispose()


@pytest.mark.parametrize(
    "authority_change",
    ("project_suspended", "membership_removed", "admin_downgraded"),
)
@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_new_session_reader_rejects_stale_project_authority(
    migrated_postgres_database_url: str,
    authority_change: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = AuditService(seed.factory, _keyring())
    context = _project_context(seed.owner_a)
    try:
        await _invalidate_project_audit_authority(
            seed.factory,
            context,
            authority_change,
        )

        with pytest.raises(AuditAuthorityRejected):
            await service.list_project_new_session(context)
    finally:
        await seed.engine.dispose()


@pytest.mark.parametrize(
    "authority_change",
    ("project_suspended", "membership_removed", "admin_downgraded"),
)
@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_session_reader_rejects_stale_project_authority(
    migrated_postgres_database_url: str,
    authority_change: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = AuditService(seed.factory, _keyring())
    context = _project_context(seed.owner_a)
    try:
        await _invalidate_project_audit_authority(
            seed.factory,
            context,
            authority_change,
        )

        with pytest.raises(AuditAuthorityRejected):
            async with seed.factory.begin() as session:
                await service.list_project(session, context)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_platform_reader_requires_issued_system_governance_context(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = AuditService(seed.factory, _keyring())
    try:
        async with seed.factory() as session, session.begin():
            first = await _append_run(
                service,
                session,
                seed.owner_a,
                uuid.uuid4(),
                request_id="audit-platform-a",
            )
            second = await _append_run(
                service,
                session,
                seed.project_b_owner_a,
                uuid.uuid4(),
                request_id="audit-platform-b",
            )

        context = resolve_system_audit_context(
            SimpleNamespace(
                id=seed.owner_a.user_id,
                system_role="system_admin",
            ),
            request_id="P" * 512,
        )
        async with seed.factory() as session, session.begin():
            third = await service.append(
                session,
                AuditActor.system_admin(context),
                AuditAction.BACKUP_CREATED,
                AuditTarget(
                    kind=AuditTargetKind.BACKUP,
                    authority_id=uuid.uuid4(),
                    project_id=None,
                ),
                AuditOutcome.SUCCESS,
                {"table_count": 12, "tombstone_high_watermark": 0},
                request_id=context.request_id,
            )
        page = await service.list_platform_new_session(context)
        assert {item.id for item in page.items} == {first.id, second.id, third.id}
        assert next(item for item in page.items if item.id == third.id).request_id != context.request_id

        forged = object.__new__(SystemAuditContext)
        object.__setattr__(forged, "user_id", seed.owner_a.user_id)
        object.__setattr__(forged, "request_id", "forged")
        with pytest.raises(TypeError, match="issued system audit context"):
            await service.list_platform_new_session(forged)
    finally:
        await seed.engine.dispose()
