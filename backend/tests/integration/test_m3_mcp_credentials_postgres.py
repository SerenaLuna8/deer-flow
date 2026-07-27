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
from app.shared_assets.bootstrap import bootstrap_system_assets
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetNotFound, AssetValidationFailed
from app.shared_assets.models import WorkflowStatus
from deerflow.mcp.definition import ExactMcpEndpointPolicy
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
    McpCredentialSlotRow,
    McpServerRow,
    McpServerVersionRow,
)


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
                payload_schema={"headers": ["X-ERP-Token"]},
            ),
        )
    return mcp_module.McpDefinition(
        description="ERP tools",
        transport="http",
        url="https://mcp.example.test",
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
    mcp_service = mcp_module.McpService(
        factory,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({"https://mcp.example.test"})),
    )
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
            {"headers": {"X-ERP-Token": "never-log-me"}},
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
            {"primary": credential.current_version_id},
            expected_asset_version=3,
        )
        assert approved.workflow_status is WorkflowStatus.PUBLISHED
        assert len(approved.credential_grants) == 1

        foreign = await credential_service.create(
            other_admin,
            credential_module.CreateCredential("other", "Other", "token"),
            {"headers": {"X-ERP-Token": "foreign-secret"}},
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
                {"primary": foreign.current_version_id},
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
                {"primary": credential.current_version_id},
                expected_asset_version=3,
            )
        with pytest.raises(AssetNotFound):
            await mcp_service.get(other_admin, protected_asset.id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_approval_binds_named_required_slots_and_allows_optional_omission(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    mcp_module = importlib.import_module("app.shared_assets.mcp_service")
    credential_module = importlib.import_module("app.shared_assets.credential_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="multi-slot", role="admin")
    mcp_service = mcp_module.McpService(
        factory,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({"https://mcp.example.test"})),
    )
    credential_service = credential_module.CredentialService(factory)
    try:
        primary = await credential_service.create(
            admin,
            credential_module.CreateCredential("primary", "Primary", "token"),
            {"headers": {"X-ERP-Token": "primary-secret"}},
        )
        secondary = await credential_service.create(
            admin,
            credential_module.CreateCredential("secondary", "Secondary", "header"),
            {"headers": {"X_API_KEY": "secondary-secret"}},
        )
        definition = mcp_module.McpDefinition(
            description="Two independent required credentials",
            transport="http",
            url="https://mcp.example.test",
            credential_slots=(
                mcp_module.McpCredentialSlot(
                    "primary",
                    "ERP token",
                    {"headers": ["X-ERP-Token"]},
                ),
                mcp_module.McpCredentialSlot(
                    "secondary",
                    "API header",
                    {"headers": ["X_API_KEY"]},
                ),
                mcp_module.McpCredentialSlot(
                    "refresh",
                    "Optional refresh token",
                    {"headers": ["X-Refresh-Token"]},
                    required=False,
                ),
            ),
        )
        asset = await mcp_service.create_asset(admin, mcp_module.CreateMcpServer("multi", "Multi"))
        draft = await mcp_service.create_version(
            admin,
            asset.id,
            definition,
            expected_asset_version=1,
        )
        await mcp_service.submit_approval(admin, asset.id, draft.id, expected_asset_version=2)

        with pytest.raises(AssetValidationFailed):
            await mcp_service.approve(
                admin,
                asset.id,
                draft.id,
                {"primary": primary.current_version_id},
                expected_asset_version=3,
            )
        with pytest.raises(AssetValidationFailed):
            await mcp_service.approve(
                admin,
                asset.id,
                draft.id,
                {
                    "primary": primary.current_version_id,
                    "secondary": secondary.current_version_id,
                    "unknown": secondary.current_version_id,
                },
                expected_asset_version=3,
            )
        with pytest.raises(AssetValidationFailed):
            await mcp_service.approve(
                admin,
                asset.id,
                draft.id,
                {
                    "primary": secondary.current_version_id,
                    "secondary": secondary.current_version_id,
                },
                expected_asset_version=3,
            )

        approved = await mcp_service.approve(
            admin,
            asset.id,
            draft.id,
            {
                "primary": primary.current_version_id,
                "secondary": secondary.current_version_id,
            },
            expected_asset_version=3,
        )
        slot_names = {slot.id: slot.name for slot in approved.credential_slots}
        grants = {slot_names[grant.credential_slot_id]: grant.credential_version_id for grant in approved.credential_grants}
        assert grants == {
            "primary": primary.current_version_id,
            "secondary": secondary.current_version_id,
        }

        async with factory() as session:
            stored_asset = await session.get(McpServerRow, asset.id)
            stored_version = await session.get(McpServerVersionRow, draft.id)
            stored_grants = (
                (
                    await session.execute(
                        select(CredentialGrantRow).where(
                            CredentialGrantRow.mcp_server_version_id == draft.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert stored_asset is not None
        assert stored_asset.current_published_version_id == draft.id
        assert stored_asset.version == 4
        assert stored_version is not None
        assert stored_version.workflow_status == WorkflowStatus.PUBLISHED.value
        assert len(stored_grants) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_credential_grant_migration_is_explicit_and_revoke_invalidates_all_active_grants(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    mcp_module = importlib.import_module("app.shared_assets.mcp_service")
    credential_module = importlib.import_module("app.shared_assets.credential_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="lifecycle", role="admin")
    mcp_service = mcp_module.McpService(
        factory,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({"https://mcp.example.test"})),
    )
    credential_service = credential_module.CredentialService(factory)
    try:
        credential = await credential_service.create(
            admin,
            credential_module.CreateCredential("erp", "ERP", "token"),
            {"headers": {"X-ERP-Token": "old-secret"}},
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
            {"primary": credential.current_version_id},
            expected_asset_version=3,
        )
        grant = approved.credential_grants[0]

        replacement = await credential_service.replace(
            admin,
            credential.id,
            {"headers": {"X-ERP-Token": "new-secret"}},
            expected_credential_version=1,
        )
        assert replacement.id != grant.credential_version_id
        assert await mcp_service.grant_is_usable(admin, grant.id) is True

        migration = await credential_service.migrate_grants(
            admin,
            credential.id,
            expected_credential_version=2,
        )
        assert migration.credential_id == credential.id
        assert migration.credential_version_id == replacement.id
        assert migration.migrated_count == 1
        assert await mcp_service.grant_is_usable(admin, grant.id) is False

        async with factory() as session:
            migrated_grant = (
                await session.execute(
                    select(CredentialGrantRow).where(
                        CredentialGrantRow.mcp_server_version_id == draft.id,
                        CredentialGrantRow.credential_slot_id == grant.credential_slot_id,
                        CredentialGrantRow.credential_version_id == replacement.id,
                        CredentialGrantRow.status == "active",
                    )
                )
            ).scalar_one()
        assert migrated_grant.id != grant.id
        assert await mcp_service.grant_is_usable(admin, migrated_grant.id) is True

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
                {"primary": grant.credential_version_id},
                expected_asset_version=3,
            )

        revoked = await credential_service.revoke(
            admin,
            credential.id,
            expected_credential_version=2,
        )
        assert revoked.status == "revoked"
        assert await mcp_service.grant_is_usable(admin, grant.id) is False
        assert await mcp_service.grant_is_usable(admin, migrated_grant.id) is False

        async with factory() as session:
            old_version = await session.get(CredentialVersionRow, grant.credential_version_id)
            replacement_row = await session.get(CredentialVersionRow, replacement.id)
            stored_grant = await session.get(CredentialGrantRow, grant.id)
            stored_migrated_grant = await session.get(CredentialGrantRow, migrated_grant.id)
            envelopes = (await session.execute(select(CredentialEnvelopeRow).order_by(CredentialEnvelopeRow.created_at))).scalars().all()
        assert old_version is not None and old_version.status == "revoked"
        assert replacement_row is not None and replacement_row.status == "revoked"
        assert stored_grant is not None and stored_grant.status == "revoked"
        assert stored_grant.credential_version_id == grant.credential_version_id
        assert stored_migrated_grant is not None and stored_migrated_grant.status == "revoked"
        assert stored_migrated_grant.credential_version_id == replacement.id
        assert len(envelopes) == 2
        assert all(b"old-secret" not in row.ciphertext and b"new-secret" not in row.ciphertext for row in envelopes)
        for api_view in (credential, replacement, migration, revoked, grant):
            rendered = repr(api_view)
            assert "ciphertext" not in rendered
            assert "nonce" not in rendered
            assert "integration-key" not in rendered
            assert "old-secret" not in rendered
            assert "new-secret" not in rendered

        await credential_service.delete(
            admin,
            credential.id,
            expected_credential_version=3,
        )
        with pytest.raises(AssetNotFound):
            await credential_service.get(admin, credential.id)
        with pytest.raises(AssetNotFound):
            await credential_service.get_version_history(admin, credential.id)
        with pytest.raises(AssetNotFound):
            await credential_service.delete(
                admin,
                credential.id,
                expected_credential_version=4,
            )
        assert credential.id not in {item.id for item in await credential_service.list_visible(admin)}
        async with factory() as session:
            deleted = await session.get(CredentialRow, credential.id)
        assert deleted is not None
        assert deleted.is_delete is True
        assert deleted.status == "revoked"
        assert deleted.version == 4
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_credential_grant_migration_rejects_incompatible_current_payload_schema_atomically(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    mcp_module = importlib.import_module("app.shared_assets.mcp_service")
    credential_module = importlib.import_module("app.shared_assets.credential_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="migration-schema", role="admin")
    mcp_service = mcp_module.McpService(
        factory,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({"https://mcp.example.test"})),
    )
    credential_service = credential_module.CredentialService(factory)
    try:
        credential = await credential_service.create(
            admin,
            credential_module.CreateCredential("erp", "ERP", "token"),
            {"headers": {"X-ERP-Token": "old-secret"}},
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
            {"primary": credential.current_version_id},
            expected_asset_version=3,
        )
        grant = approved.credential_grants[0]
        replacement = await credential_service.replace(
            admin,
            credential.id,
            {"headers": {"Authorization": "new-secret"}},
            expected_credential_version=1,
        )

        with pytest.raises(AssetValidationFailed):
            await credential_service.migrate_grants(
                admin,
                credential.id,
                expected_credential_version=2,
            )

        async with factory() as session:
            grants = (
                (
                    await session.execute(
                        select(CredentialGrantRow).where(
                            CredentialGrantRow.mcp_server_version_id == draft.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert [(row.id, row.credential_version_id, row.status) for row in grants] == [(grant.id, grant.credential_version_id, "active")]
        assert replacement.id != grant.credential_version_id
        assert await mcp_service.grant_is_usable(admin, grant.id) is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_packaged_system_mcp_only_allows_dedicated_system_credential_grants(
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
    mcp_service = mcp_module.McpService(
        factory,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({"https://mcp.example.test"})),
    )
    credential_service = credential_module.CredentialService(factory)
    try:
        await bootstrap_system_assets(factory)
        async with factory() as session:
            system_asset = (
                await session.execute(
                    select(McpServerRow).where(
                        McpServerRow.source_key == "builtin:mcp:deerflow-docs",
                    )
                )
            ).scalar_one()
            system_version = await session.get(
                McpServerVersionRow,
                system_asset.current_published_version_id,
            )
        assert system_version is not None

        project_credential = await credential_service.create(
            admin,
            credential_module.CreateCredential("project", "Project", "token"),
            {"headers": {"X-DEERFLOW-DOCS-KEY": "project-secret"}},
        )
        system_credential = await credential_service.create(
            system,
            credential_module.CreateCredential("system", "System", "token"),
            {"headers": {"X-DEERFLOW-DOCS-KEY": "system-secret"}},
        )

        with pytest.raises(AssetForbidden):
            await mcp_service.create_asset(
                system,
                mcp_module.CreateMcpServer("runtime-system", "Runtime System"),
            )
        with pytest.raises(AssetForbidden):
            await mcp_service.approve(
                system,
                system_asset.id,
                system_version.id,
                {"api-key": system_credential.current_version_id},
                expected_asset_version=system_asset.version,
            )
        with pytest.raises(AssetValidationFailed):
            await mcp_service.configure_system_credential_grants(
                system,
                system_asset.id,
                system_version.id,
                {"api-key": project_credential.current_version_id},
                {},
            )
        configured = await mcp_service.configure_system_credential_grants(
            system,
            system_asset.id,
            system_version.id,
            {"api-key": system_credential.current_version_id},
            {},
        )
        assert configured.workflow_status is WorkflowStatus.PUBLISHED
        assert len(configured.credential_grants) == 1
        grant = configured.credential_grants[0]
        assert grant.status == "active"
        assert grant.credential_version_id == system_credential.current_version_id

        idempotent = await mcp_service.configure_system_credential_grants(
            system,
            system_asset.id,
            system_version.id,
            {"api-key": system_credential.current_version_id},
            {"api-key": grant.version},
        )
        active_grants = [item for item in idempotent.credential_grants if item.status == "active"]
        assert [item.id for item in active_grants] == [grant.id]

        replacement = await credential_service.replace(
            system,
            system_credential.id,
            {"headers": {"X-DEERFLOW-DOCS-KEY": "replacement-secret"}},
            expected_credential_version=system_credential.version,
        )
        rotated = await mcp_service.configure_system_credential_grants(
            system,
            system_asset.id,
            system_version.id,
            {"api-key": replacement.id},
            {"api-key": grant.version},
        )
        rotated_active = [item for item in rotated.credential_grants if item.status == "active"]
        assert len(rotated_active) == 1
        assert rotated_active[0].credential_version_id == replacement.id
        assert rotated_active[0].id != grant.id

        async with factory() as session:
            unchanged_asset = await session.get(McpServerRow, system_asset.id)
            unchanged_version = await session.get(McpServerVersionRow, system_version.id)
            stored_grants = (await session.execute(select(CredentialGrantRow).where(CredentialGrantRow.mcp_server_version_id == system_version.id).order_by(CredentialGrantRow.created_at, CredentialGrantRow.id))).scalars().all()
        assert unchanged_asset is not None
        assert unchanged_asset.version == system_asset.version
        assert unchanged_asset.current_published_version_id == system_version.id
        assert unchanged_version is not None
        assert unchanged_version.workflow_status == WorkflowStatus.PUBLISHED.value
        assert unchanged_version.payload_checksum == system_version.payload_checksum
        assert [(item.status, item.version) for item in stored_grants] == [
            ("revoked", 2),
            ("active", 1),
        ]

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
                {"primary": project_credential.current_version_id},
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
    repository_module = importlib.import_module("app.shared_assets.credential_repository")
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
        original_get = repository_module.CredentialRepository.get_project_credential
        ready_count = 0
        both_ready = asyncio.Event()
        release = asyncio.Event()

        async def wait_at_credential_lock(
            repository,
            context,
            credential_id,
            *,
            for_update=False,
        ):
            nonlocal ready_count
            if for_update:
                ready_count += 1
                if ready_count == 2:
                    both_ready.set()
                await release.wait()
            return await original_get(
                repository,
                context,
                credential_id,
                for_update=for_update,
            )

        monkeypatch.setattr(
            repository_module.CredentialRepository,
            "get_project_credential",
            wait_at_credential_lock,
        )
        tasks = [
            asyncio.create_task(
                service.replace(
                    admin,
                    credential.id,
                    {"env": {"ERP_TOKEN": "replacement-a"}},
                    expected_credential_version=1,
                )
            ),
            asyncio.create_task(
                service.replace(
                    admin,
                    credential.id,
                    {"env": {"ERP_TOKEN": "replacement-b"}},
                    expected_credential_version=1,
                )
            ),
        ]
        try:
            await asyncio.wait_for(both_ready.wait(), timeout=5)
        except BaseException:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10,
        )
        assert ready_count == 2
        assert sum(not isinstance(result, Exception) for result in results) == 1
        conflicts = [result for result in results if isinstance(result, Exception)]
        assert len(conflicts) == 1
        assert isinstance(conflicts[0], AssetConflict)
        assert "replacement" not in str(conflicts[0])
        replacement = next(result for result in results if not isinstance(result, Exception))

        async with factory() as session:
            stored_credential = await session.get(CredentialRow, credential.id)
            versions = (await session.execute(select(CredentialVersionRow).where(CredentialVersionRow.credential_id == credential.id).order_by(CredentialVersionRow.version_number))).scalars().all()
            envelopes = (await session.execute(select(CredentialEnvelopeRow).where(CredentialEnvelopeRow.credential_version_id.in_([version.id for version in versions])).order_by(CredentialEnvelopeRow.created_at))).scalars().all()
        assert stored_credential is not None
        assert stored_credential.status == "active"
        assert stored_credential.version == 2
        assert stored_credential.current_version_id == replacement.id
        assert [version.version_number for version in versions] == [1, 2]
        assert [version.status for version in versions] == ["retired", "active"]
        assert versions[0].id == credential.current_version_id
        assert versions[1].id == replacement.id
        assert versions[1].supersedes_version_id == versions[0].id
        assert len(envelopes) == 2
        assert all(envelope.is_active for envelope in envelopes)
        assert all(b"replacement" not in envelope.ciphertext for envelope in envelopes)
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
    service = mcp_module.McpService(
        factory,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({"https://mcp.example.test"})),
    )
    try:
        asset = await service.create_asset(editor, mcp_module.CreateMcpServer("checksum", "Checksum"))
        draft = await service.create_version(
            editor,
            asset.id,
            _safe_definition(mcp_module, credential=True),
            expected_asset_version=1,
        )
        assert len(draft.payload_checksum) == 64
        assert draft.payload_checksum == service._checksum(draft.definition)
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE mcp_server_versions SET description='drifted' WHERE id=:id"),
                    {"id": draft.id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_system_multi_slot_approvals_use_one_global_credential_lock_order(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    mcp_module = importlib.import_module("app.shared_assets.mcp_service")
    credential_module = importlib.import_module("app.shared_assets.credential_service")
    repository_module = importlib.import_module("app.shared_assets.credential_repository")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    system = await _seed_system_admin(engine)
    mcp_service = mcp_module.McpService(
        factory,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({"https://mcp.example.test"})),
    )
    credential_service = credential_module.CredentialService(factory)
    try:
        credential_a = await credential_service.create(
            system,
            credential_module.CreateCredential("system-a", "System A", "token"),
            {"headers": {"X-System-Token": "system-secret-a"}},
        )
        credential_b = await credential_service.create(
            system,
            credential_module.CreateCredential("system-b", "System B", "token"),
            {"headers": {"X-System-Token": "system-secret-b"}},
        )
        definition = mcp_module.McpDefinition(
            description="Two interchangeable system credentials",
            transport="http",
            url="https://mcp.example.test",
            credential_slots=(
                mcp_module.McpCredentialSlot(
                    "first",
                    "First system credential",
                    {"headers": ["X-System-Token"]},
                ),
                mcp_module.McpCredentialSlot(
                    "second",
                    "Second system credential",
                    {"headers": ["X-System-Token"]},
                ),
            ),
        )

        async def seed_packaged_mcp(slug: str):
            asset_id = uuid.uuid4()
            version_id = uuid.uuid4()
            asset = McpServerRow(
                id=asset_id,
                scope="system",
                project_id=None,
                slug=slug,
                display_name=slug,
                source_key=f"test:packaged:mcp:{slug}",
                created_by_user_id=str(system.user_id),
            )
            version = McpServerVersionRow(
                id=version_id,
                mcp_server_id=asset_id,
                version_number=1,
                workflow_status=WorkflowStatus.DRAFT.value,
                description=definition.description,
                transport=definition.transport,
                command=definition.command,
                args=list(definition.args),
                url=definition.url,
                non_secret_env=dict(definition.env),
                non_secret_headers=dict(definition.headers),
                oauth_metadata=dict(definition.oauth),
                routing=dict(definition.routing),
                tool_overrides=dict(definition.tool_overrides),
                timeout_seconds=definition.timeout_seconds,
                payload_checksum=mcp_service._checksum(definition),
                created_by_user_id=str(system.user_id),
            )
            async with factory() as session, session.begin():
                session.add(asset)
                await session.flush()
                session.add(version)
                await session.flush()
                session.add_all(
                    [
                        McpCredentialSlotRow(
                            mcp_server_version_id=version.id,
                            name=slot.name,
                            purpose=slot.purpose,
                            payload_schema={key: list(values) for key, values in slot.payload_schema.items()},
                            required=slot.required,
                        )
                        for slot in definition.credential_slots
                    ]
                )
                await session.flush()
                version.workflow_status = WorkflowStatus.PUBLISHED.value
                asset.current_published_version_id = version.id
                await session.flush()
            return asset, version

        asset_one, version_one = await seed_packaged_mcp("system-one")
        asset_two, version_two = await seed_packaged_mcp("system-two")

        original_bulk = repository_module.CredentialRepository.lock_system_credential_versions
        ready_count = 0
        ready_tasks: set[asyncio.Task] = set()
        both_ready = asyncio.Event()
        release = asyncio.Event()

        async def mark_ready_once() -> None:
            nonlocal ready_count
            task = asyncio.current_task()
            assert task is not None
            if task in ready_tasks:
                return
            ready_tasks.add(task)
            ready_count += 1
            if ready_count == 2:
                both_ready.set()
            await release.wait()

        async def wait_before_bulk_lock(
            repository,
            context,
            credential_version_ids,
        ):
            await mark_ready_once()
            return await original_bulk(
                repository,
                context,
                credential_version_ids,
            )

        monkeypatch.setattr(
            repository_module.CredentialRepository,
            "lock_system_credential_versions",
            wait_before_bulk_lock,
            raising=False,
        )
        tasks = [
            asyncio.create_task(
                mcp_service.configure_system_credential_grants(
                    system,
                    asset_one.id,
                    version_one.id,
                    {
                        "first": credential_a.current_version_id,
                        "second": credential_b.current_version_id,
                    },
                    {},
                )
            ),
            asyncio.create_task(
                mcp_service.configure_system_credential_grants(
                    system,
                    asset_two.id,
                    version_two.id,
                    {
                        "first": credential_b.current_version_id,
                        "second": credential_a.current_version_id,
                    },
                    {},
                )
            ),
        ]
        try:
            await asyncio.wait_for(both_ready.wait(), timeout=5)
        except BaseException:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10,
        )
        assert ready_count == 2
        assert all(not isinstance(result, Exception) for result in results)

        expected_bindings = (
            {
                "first": credential_a.current_version_id,
                "second": credential_b.current_version_id,
            },
            {
                "first": credential_b.current_version_id,
                "second": credential_a.current_version_id,
            },
        )
        for result, expected in zip(results, expected_bindings, strict=True):
            assert result.workflow_status is WorkflowStatus.PUBLISHED
            slot_names = {slot.id: slot.name for slot in result.credential_slots}
            actual = {slot_names[grant.credential_slot_id]: grant.credential_version_id for grant in result.credential_grants}
            assert actual == expected

        async with factory() as session:
            stored_assets = [await session.get(McpServerRow, asset_id) for asset_id in (asset_one.id, asset_two.id)]
            stored_versions = [await session.get(McpServerVersionRow, version_id) for version_id in (version_one.id, version_two.id)]
            stored_grants = (
                (
                    await session.execute(
                        select(CredentialGrantRow)
                        .where(CredentialGrantRow.mcp_server_version_id.in_([version_one.id, version_two.id]))
                        .order_by(
                            CredentialGrantRow.mcp_server_version_id,
                            CredentialGrantRow.credential_slot_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert all(asset is not None and asset.version == 1 and asset.source_key is not None for asset in stored_assets)
        assert [asset.current_published_version_id for asset in stored_assets] == [
            version_one.id,
            version_two.id,
        ]
        assert all(version is not None and version.workflow_status == WorkflowStatus.PUBLISHED.value for version in stored_versions)
        assert len(stored_grants) == 4
        assert all(grant.status == "active" for grant in stored_grants)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_mcp_approval_has_one_stable_conflict(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    mcp_module = importlib.import_module("app.shared_assets.mcp_service")
    repository_module = importlib.import_module("app.shared_assets.mcp_repository")
    credential_module = importlib.import_module("app.shared_assets.credential_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_project(engine, factory, label="approval-race", role="admin")
    mcp_service = mcp_module.McpService(
        factory,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({"https://mcp.example.test"})),
    )
    credential_service = credential_module.CredentialService(factory)
    try:
        credential = await credential_service.create(
            admin,
            credential_module.CreateCredential("race", "Race", "token"),
            {"headers": {"X-ERP-Token": "approval-secret"}},
        )
        asset = await mcp_service.create_asset(admin, mcp_module.CreateMcpServer("race", "Race"))
        draft = await mcp_service.create_version(
            admin,
            asset.id,
            _safe_definition(mcp_module, credential=True),
            expected_asset_version=1,
        )
        await mcp_service.submit_approval(admin, asset.id, draft.id, expected_asset_version=2)
        original_lock = repository_module.McpRepository.lock_project
        ready_count = 0
        both_ready = asyncio.Event()
        release = asyncio.Event()

        async def wait_at_project_lock(repository, context):
            nonlocal ready_count
            ready_count += 1
            if ready_count == 2:
                both_ready.set()
            await release.wait()
            return await original_lock(repository, context)

        monkeypatch.setattr(
            repository_module.McpRepository,
            "lock_project",
            wait_at_project_lock,
        )
        tasks = [
            asyncio.create_task(
                mcp_service.approve(
                    admin,
                    asset.id,
                    draft.id,
                    {"primary": credential.current_version_id},
                    expected_asset_version=3,
                )
            ),
            asyncio.create_task(
                mcp_service.approve(
                    admin,
                    asset.id,
                    draft.id,
                    {"primary": credential.current_version_id},
                    expected_asset_version=3,
                )
            ),
        ]
        try:
            await asyncio.wait_for(both_ready.wait(), timeout=5)
        except BaseException:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10,
        )
        assert ready_count == 2
        assert sum(not isinstance(result, Exception) for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1 and isinstance(failures[0], AssetConflict)

        async with factory() as session:
            stored_asset = await session.get(McpServerRow, asset.id)
            stored_version = await session.get(McpServerVersionRow, draft.id)
            stored_credential = await session.get(CredentialRow, credential.id)
            stored_credential_version = await session.get(
                CredentialVersionRow,
                credential.current_version_id,
            )
            grants = (
                (
                    await session.execute(
                        select(CredentialGrantRow).where(
                            CredentialGrantRow.mcp_server_version_id == draft.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert stored_asset is not None
        assert stored_asset.status == "active"
        assert stored_asset.version == 4
        assert stored_asset.current_published_version_id == draft.id
        assert stored_version is not None
        assert stored_version.workflow_status == WorkflowStatus.PUBLISHED.value
        assert stored_version.submitted_at is not None
        assert stored_version.reviewed_at is not None
        assert stored_version.reviewed_by_user_id == str(admin.user_id)
        assert stored_credential is not None and stored_credential.status == "active"
        assert stored_credential_version is not None
        assert stored_credential_version.status == "active"
        assert len(grants) == 1
        assert grants[0].status == "active"
        assert grants[0].credential_slot_id == draft.credential_slots[0].id
        assert grants[0].credential_version_id == credential.current_version_id
    finally:
        await engine.dispose()
