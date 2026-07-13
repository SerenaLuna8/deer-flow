from __future__ import annotations

import asyncio
import importlib
import json
import uuid
from base64 import b64encode
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.projects.context import ProjectContext, resolve_project_context
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetNotFound, AssetValidationFailed
from app.shared_assets.models import WorkflowStatus
from deerflow.persistence.shared_assets import CredentialEnvelopeRow, CredentialGrantRow, CredentialVersionRow


async def _seed_project(
    engine: AsyncEngine,
    factory: async_sessionmaker,
    *,
    label: str,
    role: str,
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
            {"id": membership_id, "project": project_id, "user": str(user_id), "role": role},
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
            {"id": str(user_id), "email": f"system-{user_id}@example.com", "now": datetime.now(UTC)},
        )
    return SystemAssetGovernanceContext(user_id=user_id, request_id="req-system-mcp")


def _configure_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "integration-key")
    monkeypatch.setenv(
        "DEER_FLOW_CREDENTIAL_KEYRING_JSON",
        json.dumps({"integration-key": b64encode(b"i" * 32).decode("ascii")}),
    )


def _safe_definition(mcp_module, *, credential: bool):
    slots = ()
    if credential:
        slots = (
            mcp_module.McpCredentialSlot(
                name="primary",
                purpose="ERP authentication",
                payload_schema={"env": ["ERP_TOKEN"]},
            ),
        )
    return mcp_module.McpDefinition(
        description="ERP tools",
        transport="http",
        url="https://mcp.example.test",
        headers={"Accept": "application/json"},
        credential_slots=slots,
    )


@pytest.mark.asyncio
async def test_project_mcp_direct_publish_and_credential_approval_are_scope_safe(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    mcp_module = importlib.import_module("app.shared_assets.mcp_service")
    credential_module = importlib.import_module("app.shared_assets.credential_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_project(engine, factory, label="editor", role="editor")
    admin = await _seed_project(engine, factory, label="admin", role="admin")
    other_admin = await _seed_project(engine, factory, label="other", role="admin")
    mcp_service = mcp_module.McpService(factory)
    credential_service = credential_module.CredentialService(factory)
    try:
        direct_asset = await mcp_service.create_asset(editor, mcp_module.CreateMcpServer("public", "Public"))
        direct_draft = await mcp_service.create_version(
            editor,
            direct_asset.id,
            _safe_definition(mcp_module, credential=False),
            expected_asset_version=1,
        )
        direct = await mcp_service.publish(
            editor,
            direct_asset.id,
            direct_draft.id,
            expected_asset_version=2,
        )
        assert direct.workflow_status is WorkflowStatus.PUBLISHED

        # Create the credential and MCP under the same Admin-owned project.
        credential = await credential_service.create(
            admin,
            credential_module.CreateCredential("erp", "ERP", "token"),
            {"env": {"ERP_TOKEN": "never-log-me"}},
        )
        protected_asset = await mcp_service.create_asset(admin, mcp_module.CreateMcpServer("erp", "ERP"))
        protected_draft = await mcp_service.create_version(
            admin,
            protected_asset.id,
            _safe_definition(mcp_module, credential=True),
            expected_asset_version=1,
        )
        pending = await mcp_service.submit_approval(
            admin,
            protected_asset.id,
            protected_draft.id,
            expected_asset_version=2,
        )
        assert pending.workflow_status is WorkflowStatus.PENDING_APPROVAL
        approved = await mcp_service.approve(
            admin,
            protected_asset.id,
            protected_draft.id,
            credential.current_version_id,
            expected_asset_version=3,
        )
        assert approved.workflow_status is WorkflowStatus.PUBLISHED
        assert len(approved.credential_grants) == 1

        foreign = await credential_service.create(
            other_admin,
            credential_module.CreateCredential("other", "Other", "token"),
            {"env": {"ERP_TOKEN": "foreign-secret"}},
        )
        second_asset = await mcp_service.create_asset(admin, mcp_module.CreateMcpServer("erp-two", "ERP Two"))
        second_draft = await mcp_service.create_version(
            admin,
            second_asset.id,
            _safe_definition(mcp_module, credential=True),
            expected_asset_version=1,
        )
        await mcp_service.submit_approval(admin, second_asset.id, second_draft.id, expected_asset_version=2)
        with pytest.raises(AssetValidationFailed):
            await mcp_service.approve(
                admin,
                second_asset.id,
                second_draft.id,
                foreign.current_version_id,
                expected_asset_version=3,
            )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO credential_grants
                    (id,mcp_server_version_id,credential_slot_id,credential_version_id,created_by_user_id)
                    VALUES (:id,:version,:slot,:credential,:user)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "version": second_draft.id,
                    "slot": second_draft.credential_slots[0].id,
                    "credential": credential.current_version_id,
                    "user": str(admin.user_id),
                },
            )
        with pytest.raises(AssetConflict):
            await mcp_service.approve(
                admin,
                second_asset.id,
                second_draft.id,
                credential.current_version_id,
                expected_asset_version=3,
            )
        with pytest.raises(AssetNotFound):
            await mcp_service.get(other_admin, protected_asset.id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_credential_replace_keeps_old_grant_retired_then_revoke_invalidates_it(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    mcp_module = importlib.import_module("app.shared_assets.mcp_service")
    credential_module = importlib.import_module("app.shared_assets.credential_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="lifecycle", role="admin")
    mcp_service = mcp_module.McpService(factory)
    credential_service = credential_module.CredentialService(factory)
    try:
        credential = await credential_service.create(
            admin,
            credential_module.CreateCredential("erp", "ERP", "token"),
            {"env": {"ERP_TOKEN": "old-secret"}},
        )
        asset = await mcp_service.create_asset(admin, mcp_module.CreateMcpServer("erp", "ERP"))
        draft = await mcp_service.create_version(
            admin,
            asset.id,
            _safe_definition(mcp_module, credential=True),
            expected_asset_version=1,
        )
        await mcp_service.submit_approval(admin, asset.id, draft.id, expected_asset_version=2)
        approved = await mcp_service.approve(
            admin,
            asset.id,
            draft.id,
            credential.current_version_id,
            expected_asset_version=3,
        )
        grant = approved.credential_grants[0]

        replacement = await credential_service.replace(
            admin,
            credential.id,
            {"env": {"ERP_TOKEN": "new-secret"}},
            expected_credential_version=1,
        )
        assert replacement.id != grant.credential_version_id
        assert await mcp_service.grant_is_usable(admin, grant.id) is True

        second_asset = await mcp_service.create_asset(admin, mcp_module.CreateMcpServer("erp-two", "ERP Two"))
        second_draft = await mcp_service.create_version(
            admin,
            second_asset.id,
            _safe_definition(mcp_module, credential=True),
            expected_asset_version=1,
        )
        await mcp_service.submit_approval(admin, second_asset.id, second_draft.id, expected_asset_version=2)
        with pytest.raises(AssetValidationFailed):
            await mcp_service.approve(
                admin,
                second_asset.id,
                second_draft.id,
                grant.credential_version_id,
                expected_asset_version=3,
            )

        revoked = await credential_service.revoke(
            admin,
            credential.id,
            expected_credential_version=2,
        )
        assert revoked.status == "revoked"
        assert await mcp_service.grant_is_usable(admin, grant.id) is False

        async with factory() as session:
            old_version = await session.get(CredentialVersionRow, grant.credential_version_id)
            replacement_row = await session.get(CredentialVersionRow, replacement.id)
            stored_grant = await session.get(CredentialGrantRow, grant.id)
            envelopes = (await session.execute(select(CredentialEnvelopeRow).order_by(CredentialEnvelopeRow.created_at))).scalars().all()
        assert old_version is not None and old_version.status == "revoked"
        assert replacement_row is not None and replacement_row.status == "revoked"
        assert stored_grant is not None and stored_grant.credential_version_id == grant.credential_version_id
        assert len(envelopes) == 2
        assert all(b"old-secret" not in row.ciphertext and b"new-secret" not in row.ciphertext for row in envelopes)
        for api_view in (credential, replacement, revoked, grant):
            rendered = repr(api_view)
            assert "ciphertext" not in rendered
            assert "nonce" not in rendered
            assert "integration-key" not in rendered
            assert "old-secret" not in rendered
            assert "new-secret" not in rendered
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_system_mcp_requires_system_credential_and_editor_cannot_approve(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    mcp_module = importlib.import_module("app.shared_assets.mcp_service")
    credential_module = importlib.import_module("app.shared_assets.credential_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_project(engine, factory, label="system-editor", role="editor")
    admin = await _seed_project(engine, factory, label="system-admin-project", role="admin")
    system = await _seed_system_admin(engine)
    mcp_service = mcp_module.McpService(factory)
    credential_service = credential_module.CredentialService(factory)
    try:
        project_credential = await credential_service.create(
            admin,
            credential_module.CreateCredential("project", "Project", "token"),
            {"env": {"ERP_TOKEN": "project-secret"}},
        )
        system_credential = await credential_service.create(
            system,
            credential_module.CreateCredential("system", "System", "token"),
            {"env": {"ERP_TOKEN": "system-secret"}},
        )
        system_asset = await mcp_service.create_asset(system, mcp_module.CreateMcpServer("system", "System"))
        system_draft = await mcp_service.create_version(
            system,
            system_asset.id,
            _safe_definition(mcp_module, credential=True),
            expected_asset_version=1,
        )
        with pytest.raises(AssetValidationFailed):
            await mcp_service.approve(
                system,
                system_asset.id,
                system_draft.id,
                project_credential.current_version_id,
                expected_asset_version=2,
            )
        approved = await mcp_service.approve(
            system,
            system_asset.id,
            system_draft.id,
            system_credential.current_version_id,
            expected_asset_version=2,
        )
        assert approved.workflow_status is WorkflowStatus.PUBLISHED

        project_asset = await mcp_service.create_asset(editor, mcp_module.CreateMcpServer("editor", "Editor"))
        project_draft = await mcp_service.create_version(
            editor,
            project_asset.id,
            _safe_definition(mcp_module, credential=True),
            expected_asset_version=1,
        )
        await mcp_service.submit_approval(editor, project_asset.id, project_draft.id, expected_asset_version=2)
        with pytest.raises(AssetForbidden):
            await mcp_service.approve(
                editor,
                project_asset.id,
                project_draft.id,
                project_credential.current_version_id,
                expected_asset_version=3,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_credential_replace_has_one_stable_conflict(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    credential_module = importlib.import_module("app.shared_assets.credential_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="replace-race", role="admin")
    service = credential_module.CredentialService(factory)
    try:
        credential = await service.create(
            admin,
            credential_module.CreateCredential("race", "Race", "token"),
            {"env": {"ERP_TOKEN": "initial"}},
        )
        results = await asyncio.gather(
            service.replace(
                admin,
                credential.id,
                {"env": {"ERP_TOKEN": "replacement-a"}},
                expected_credential_version=1,
            ),
            service.replace(
                admin,
                credential.id,
                {"env": {"ERP_TOKEN": "replacement-b"}},
                expected_credential_version=1,
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        conflicts = [result for result in results if isinstance(result, Exception)]
        assert len(conflicts) == 1
        assert isinstance(conflicts[0], AssetConflict)
        assert "replacement" not in str(conflicts[0])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_definition_rows_reject_draft_checksum_drift(
    migrated_postgres_database_url: str,
) -> None:
    mcp_module = importlib.import_module("app.shared_assets.mcp_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_project(engine, factory, label="checksum", role="editor")
    service = mcp_module.McpService(factory)
    try:
        asset = await service.create_asset(editor, mcp_module.CreateMcpServer("checksum", "Checksum"))
        draft = await service.create_version(
            editor,
            asset.id,
            _safe_definition(mcp_module, credential=True),
            expected_asset_version=1,
        )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE mcp_server_versions SET description='drifted' WHERE id=:id"),
                    {"id": draft.id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_mcp_approval_has_one_stable_conflict(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    mcp_module = importlib.import_module("app.shared_assets.mcp_service")
    credential_module = importlib.import_module("app.shared_assets.credential_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="approval-race", role="admin")
    mcp_service = mcp_module.McpService(factory)
    credential_service = credential_module.CredentialService(factory)
    try:
        credential = await credential_service.create(
            admin,
            credential_module.CreateCredential("race", "Race", "token"),
            {"env": {"ERP_TOKEN": "approval-secret"}},
        )
        asset = await mcp_service.create_asset(admin, mcp_module.CreateMcpServer("race", "Race"))
        draft = await mcp_service.create_version(
            admin,
            asset.id,
            _safe_definition(mcp_module, credential=True),
            expected_asset_version=1,
        )
        await mcp_service.submit_approval(admin, asset.id, draft.id, expected_asset_version=2)
        results = await asyncio.gather(
            mcp_service.approve(
                admin,
                asset.id,
                draft.id,
                credential.current_version_id,
                expected_asset_version=3,
            ),
            mcp_service.approve(
                admin,
                asset.id,
                draft.id,
                credential.current_version_id,
                expected_asset_version=3,
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1 and isinstance(failures[0], AssetConflict)
    finally:
        await engine.dispose()
