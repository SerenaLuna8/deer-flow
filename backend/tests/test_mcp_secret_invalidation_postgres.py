from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import func, select, text
from support.private_thread_seed import seed_private_thread_database

from app.audit.service import AuditService, _bind_operator_audit_process
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.audit import DurableSharedAssetGovernanceEventSink
from app.shared_assets.bootstrap.service import (
    BUILTIN_ASSET_EMAIL,
    BUILTIN_ASSET_USER_ID,
    _invalidate_project_mcp_secrets,
)
from app.shared_assets.mcp_secret_store import McpSecretStore
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.shared_assets import (
    McpSecretSlotRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectMcpSecretGenerationRow,
    ProjectMcpSecretStateRow,
    ProjectMcpSecretTombstoneRow,
    ProjectSystemMcpBindingRow,
)
from deerflow.persistence.user import UserRow


@pytest.mark.asyncio
async def test_system_mcp_definition_invalidation_destroys_secret_and_audits(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"x" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    mcp_server_id = uuid.uuid4()
    version_id = uuid.uuid4()
    slot_id = uuid.uuid4()
    generation_id: uuid.UUID | None = None
    try:
        async with seed.factory() as session, session.begin():
            await session.execute(text("SELECT set_config('deerflow.system_asset_upgrade', 'on', true)"))
            if await session.get(UserRow, str(BUILTIN_ASSET_USER_ID)) is None:
                session.add(
                    UserRow(
                        id=str(BUILTIN_ASSET_USER_ID),
                        email=BUILTIN_ASSET_EMAIL,
                        password_hash=None,
                        system_role="user",
                        oauth_provider=None,
                        oauth_id=None,
                        needs_setup=False,
                        token_version=0,
                    )
                )
                await session.flush()
            asset = McpServerRow(
                id=mcp_server_id,
                scope="system",
                project_id=None,
                slug=f"invalidation-{mcp_server_id.hex[:12]}",
                display_name="MCP invalidation test",
                status="active",
                current_published_version_id=None,
                version=1,
                source_key=f"test:mcp:{mcp_server_id}",
                created_by_user_id=str(BUILTIN_ASSET_USER_ID),
            )
            session.add(asset)
            await session.flush()
            version = McpServerVersionRow(
                id=version_id,
                mcp_server_id=mcp_server_id,
                version_number=1,
                workflow_status="published",
                description="Definition invalidation test",
                transport="http",
                url="https://mcp.invalid/test",
                payload_checksum="a" * 64,
                created_by_user_id=str(BUILTIN_ASSET_USER_ID),
            )
            session.add(version)
            await session.flush()
            slot = McpSecretSlotRow(
                id=slot_id,
                mcp_server_version_id=version_id,
                name="authorization",
                purpose="Test authentication",
                payload_schema={"headers": ["Authorization"]},
                required=True,
            )
            session.add(slot)
            await session.flush()
            asset.current_published_version_id = version_id
            session.add(
                ProjectSystemMcpBindingRow(
                    project_id=seed.owner_a.project_id,
                    system_mcp_server_id=mcp_server_id,
                    mcp_server_version_id=version_id,
                    enabled=True,
                    created_by_user_id=str(seed.owner_a.user_id),
                    updated_by_user_id=str(seed.owner_a.user_id),
                )
            )
            state = await McpSecretStore(session).replace(
                project_id=seed.owner_a.project_id,
                mcp_server_id=mcp_server_id,
                mcp_server_version_id=version_id,
                slots=(slot,),
                slot_name="authorization",
                payload={"headers": {"Authorization": "test-only-secret"}},
                actor_user_id=str(seed.owner_a.user_id),
                request_id="mcp-invalidation-test",
            )
            generation_id = state.current_generation_id

        audit_service = AuditService(
            seed.factory,
            AuditHmacKeyring("test", {"test": b"a" * 32}),
        )
        process_context = _bind_operator_audit_process(audit_service)
        async with seed.factory() as session, session.begin():
            await _invalidate_project_mcp_secrets(
                session,
                mcp_server_id=mcp_server_id,
                mcp_server_version_id=version_id,
                governance_sink=DurableSharedAssetGovernanceEventSink(audit_service),
                process_context=process_context,
            )

        assert generation_id is not None
        async with seed.factory() as session:
            state_count = await session.scalar(
                select(func.count())
                .select_from(ProjectMcpSecretStateRow)
                .where(
                    ProjectMcpSecretStateRow.project_id == seed.owner_a.project_id,
                    ProjectMcpSecretStateRow.mcp_server_id == mcp_server_id,
                )
            )
            generation_count = await session.scalar(
                select(func.count())
                .select_from(ProjectMcpSecretGenerationRow)
                .where(
                    ProjectMcpSecretGenerationRow.project_id == seed.owner_a.project_id,
                    ProjectMcpSecretGenerationRow.mcp_server_id == mcp_server_id,
                )
            )
            tombstone = (
                await session.execute(
                    select(ProjectMcpSecretTombstoneRow).where(
                        ProjectMcpSecretTombstoneRow.project_id == seed.owner_a.project_id,
                        ProjectMcpSecretTombstoneRow.mcp_server_id == mcp_server_id,
                    )
                )
            ).scalar_one()
            audit = (
                await session.execute(
                    select(AuditLogRow).where(
                        AuditLogRow.project_id == seed.owner_a.project_id,
                        AuditLogRow.actor_process == "operator",
                        AuditLogRow.action == "asset.updated",
                    )
                )
            ).scalar_one()

        assert state_count == 0
        assert generation_count == 0
        assert tombstone.destroyed_generation_id == generation_id
        assert tombstone.revision == 2
        assert tombstone.reason == "definition_change"
        assert audit.metadata_json == {
            "asset_kind": "mcp",
            "operation": "mcp.secret.invalidate",
            "version_id": str(version_id),
            "slot_id": str(slot_id),
            "secret_name": "authorization",
            "generation_id": str(generation_id),
            "revision": 2,
            "result": "invalidated",
            "reason": "definition_change",
            "readiness": "unready",
        }
    finally:
        await seed.engine.dispose()
