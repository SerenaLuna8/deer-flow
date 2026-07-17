from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from support.m4_private_threads import seed_m4_thread_database

from app.audit.models import AuditAuthorityRejected
from app.audit.service import AuditService
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.audit import DurableSharedAssetGovernanceEventSink
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.user.model import UserRow


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id="audit-v1",
        _keys={"audit-v1": b"1" * 32},
    )


async def _make_system_admin(seed) -> None:
    async with seed.factory() as session, session.begin():
        user = await session.get(UserRow, str(seed.owner_a.user_id))
        assert user is not None
        user.system_role = "system_admin"


@pytest.mark.postgres
@pytest.mark.anyio
async def test_m3_asset_adapter_appends_only_formal_allowlisted_audit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = DurableSharedAssetGovernanceEventSink(
        AuditService(seed.factory, _keyring()),
    )
    try:
        await _make_system_admin(seed)
        async with seed.factory() as session, session.begin():
            await sink.append_override(
                session,
                actor=seed.owner_a.user_id,
                project_id=seed.owner_a.project_id,
                asset_id=seed.project_agent_id,
                version_id=uuid.uuid4(),
                action="agent.publish",
                request_id="asset-audit",
            )

        async with seed.factory() as session:
            row = (
                await session.execute(
                    select(AuditLogRow).where(
                        AuditLogRow.action == "asset.published",
                    )
                )
            ).scalar_one()
        assert row.metadata_json == {"asset_kind": "agent"}
        assert row.project_id == seed.owner_a.project_id
        assert row.target_kind == "asset"
        assert str(seed.project_agent_id) not in repr(row.__dict__)
        assert "version_id" not in row.metadata_json
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_asset_adapter_participates_in_caller_rollback(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = DurableSharedAssetGovernanceEventSink(
        AuditService(seed.factory, _keyring()),
    )
    try:
        await _make_system_admin(seed)
        with pytest.raises(RuntimeError, match="domain rollback"):
            async with seed.factory() as session, session.begin():
                await sink.append_override(
                    session,
                    actor=seed.owner_a.user_id,
                    project_id=seed.owner_a.project_id,
                    asset_id=seed.project_agent_id,
                    version_id=None,
                    action="agent.create",
                    request_id="asset-rollback",
                )
                raise RuntimeError("domain rollback")

        async with seed.factory() as session:
            assert await session.scalar(select(AuditLogRow.id)) is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_override_adapter_rejects_unbound_system_actor(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = DurableSharedAssetGovernanceEventSink(
        AuditService(seed.factory, _keyring()),
    )
    try:
        with pytest.raises(AuditAuthorityRejected):
            async with seed.factory() as session, session.begin():
                await sink.append_override(
                    session,
                    actor=seed.owner_a.user_id,
                    project_id=seed.owner_a.project_id,
                    asset_id=seed.project_agent_id,
                    version_id=None,
                    action="agent.create",
                    request_id="forged-system-audit",
                )

        async with seed.factory() as session:
            assert await session.scalar(select(AuditLogRow.id)) is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_adapter_rejects_cross_project_actor_binding(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = DurableSharedAssetGovernanceEventSink(
        AuditService(seed.factory, _keyring()),
    )
    try:
        with pytest.raises(AuditAuthorityRejected):
            async with seed.factory() as session, session.begin():
                await sink.append_project(
                    session,
                    actor=seed.owner_b.user_id,
                    project_id=seed.project_b_owner_a.project_id,
                    asset_id=seed.project_b_agent_id,
                    version_id=None,
                    action="agent.create",
                    request_id="cross-project-audit",
                    asset_kind="agent",
                )

        async with seed.factory() as session:
            assert await session.scalar(select(AuditLogRow.id)) is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_asset_adapter_preserves_user_and_project_authority(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    sink = DurableSharedAssetGovernanceEventSink(
        AuditService(seed.factory, _keyring()),
    )
    try:
        async with seed.factory() as session, session.begin():
            await sink.append_project(
                session,
                actor=seed.owner_a.user_id,
                project_id=seed.owner_a.project_id,
                asset_id=seed.project_agent_id,
                version_id=None,
                action="agent.create",
                request_id="project-asset-audit",
                asset_kind="agent",
            )

        async with seed.factory() as session:
            row = (
                await session.execute(
                    select(AuditLogRow).where(
                        AuditLogRow.action == "asset.created",
                    )
                )
            ).scalar_one()
        assert row.actor_user_id == str(seed.owner_a.user_id)
        assert row.actor_platform_role is None
        assert row.project_id == seed.owner_a.project_id
        assert row.metadata_json == {"asset_kind": "agent"}
    finally:
        await seed.engine.dispose()
