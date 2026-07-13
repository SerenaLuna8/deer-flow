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
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
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
            {"primary": credential.current_version_id},
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
    mcp_service = mcp_module.McpService(factory)
    credential_service = credential_module.CredentialService(factory)
    try:
        primary = await credential_service.create(
            admin,
            credential_module.CreateCredential("primary", "Primary", "token"),
            {"env": {"ERP_TOKEN": "primary-secret"}},
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
                    {"env": ["ERP_TOKEN"]},
                ),
                mcp_module.McpCredentialSlot(
                    "secondary",
                    "API header",
                    {"headers": ["X_API_KEY"]},
                ),
                mcp_module.McpCredentialSlot(
                    "refresh",
                    "Optional refresh token",
                    {"oauth": ["refresh_token"]},
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
            {"primary": credential.current_version_id},
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
                {"primary": project_credential.current_version_id},
                expected_asset_version=2,
            )
        approved = await mcp_service.approve(
            system,
            system_asset.id,
            system_draft.id,
            {"primary": system_credential.current_version_id},
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
    service = mcp_module.McpService(factory)
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
    mcp_service = mcp_module.McpService(factory)
    credential_service = credential_module.CredentialService(factory)
    try:
        credential_a = await credential_service.create(
            system,
            credential_module.CreateCredential("system-a", "System A", "token"),
            {"env": {"SYSTEM_TOKEN": "system-secret-a"}},
        )
        credential_b = await credential_service.create(
            system,
            credential_module.CreateCredential("system-b", "System B", "token"),
            {"env": {"SYSTEM_TOKEN": "system-secret-b"}},
        )
        definition = mcp_module.McpDefinition(
            description="Two interchangeable system credentials",
            transport="http",
            url="https://mcp.example.test",
            credential_slots=(
                mcp_module.McpCredentialSlot(
                    "first",
                    "First system credential",
                    {"env": ["SYSTEM_TOKEN"]},
                ),
                mcp_module.McpCredentialSlot(
                    "second",
                    "Second system credential",
                    {"env": ["SYSTEM_TOKEN"]},
                ),
            ),
        )
        asset_one = await mcp_service.create_asset(
            system,
            mcp_module.CreateMcpServer("system-one", "System One"),
        )
        version_one = await mcp_service.create_version(
            system,
            asset_one.id,
            definition,
            expected_asset_version=1,
        )
        asset_two = await mcp_service.create_asset(
            system,
            mcp_module.CreateMcpServer("system-two", "System Two"),
        )
        version_two = await mcp_service.create_version(
            system,
            asset_two.id,
            definition,
            expected_asset_version=1,
        )

        original_single = repository_module.CredentialRepository.lock_system_credential_version
        original_bulk = getattr(
            repository_module.CredentialRepository,
            "lock_system_credential_versions",
            None,
        )
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

        async def wait_after_first_singular_lock(
            repository,
            context,
            credential_version_id,
        ):
            locked = await original_single(
                repository,
                context,
                credential_version_id,
            )
            await mark_ready_once()
            return locked

        async def wait_before_bulk_lock(
            repository,
            context,
            credential_version_ids,
        ):
            await mark_ready_once()
            assert original_bulk is not None
            return await original_bulk(
                repository,
                context,
                credential_version_ids,
            )

        monkeypatch.setattr(
            repository_module.CredentialRepository,
            "lock_system_credential_version",
            wait_after_first_singular_lock,
        )
        monkeypatch.setattr(
            repository_module.CredentialRepository,
            "lock_system_credential_versions",
            wait_before_bulk_lock,
            raising=False,
        )
        tasks = [
            asyncio.create_task(
                mcp_service.approve(
                    system,
                    asset_one.id,
                    version_one.id,
                    {
                        "first": credential_a.current_version_id,
                        "second": credential_b.current_version_id,
                    },
                    expected_asset_version=2,
                )
            ),
            asyncio.create_task(
                mcp_service.approve(
                    system,
                    asset_two.id,
                    version_two.id,
                    {
                        "first": credential_b.current_version_id,
                        "second": credential_a.current_version_id,
                    },
                    expected_asset_version=2,
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
        assert all(asset is not None and asset.version == 3 for asset in stored_assets)
        assert [asset.current_published_version_id for asset in stored_assets] == [
            version_one.id,
            version_two.id,
        ]
        assert all(version is not None and version.workflow_status == WorkflowStatus.PUBLISHED.value and version.reviewed_at is not None for version in stored_versions)
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
