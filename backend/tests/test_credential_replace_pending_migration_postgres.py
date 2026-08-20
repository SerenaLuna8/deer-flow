"""Replacing a Credential must say how much is still on the previous version.

Replacement only mints a new version: MCP grants, Skill environment bindings,
and system models keep resolving their exact ``credential_version_id`` and the
runtime adapter deliberately accepts a ``retired`` version. Without a
server-computed count the administrator sees a plain success and has no way to
know that the rotated key is not in use yet.

The reported total is deliberately the same set ``migrate_grants`` moves, so
the number can never promise work the migration will not perform.
"""

from __future__ import annotations

import uuid
from base64 import b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.credential_service import CreateCredential, CredentialService
from app.system_settings.bootstrap import (
    bootstrap_default_system_model,
    prepare_default_system_model_bootstrap,
)
from app.system_settings.credential_migration import (
    SystemModelCredentialMigrationAdapter,
)
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.shared_assets import (
    CredentialGrantRow,
    CredentialRow,
    McpCredentialSlotRow,
    McpServerRow,
    McpServerVersionRow,
)
from deerflow.persistence.user import UserRow

DEEPSEEK_CREDENTIAL_NAME = "deepseek-v4-api-key"
OPENCODE_CREDENTIAL_NAME = "opencode-api-key"
ROTATED_DEEPSEEK_SECRET = "rotated-deepseek-secret"
ROTATED_OPENCODE_SECRET = "rotated-opencode-secret"


@pytest.fixture()
def bootstrap_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = b64encode(b"s" * 32).decode("ascii")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-deepseek-secret")
    monkeypatch.setenv("OPENCODE_API_KEY", "unit-opencode-secret")
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "unit-bootstrap")
    monkeypatch.setenv(
        "DEER_FLOW_CREDENTIAL_KEYRING_JSON",
        f'{{"unit-bootstrap":"{encoded}"}}',
    )


async def _admin_user(factory: async_sessionmaker) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            UserRow(
                id=str(user_id),
                email=f"{user_id}@example.com",
                password_hash=None,
                system_role="system_admin",
                needs_setup=False,
                token_version=0,
            )
        )
        await session.commit()
    return user_id


async def _credential(factory: async_sessionmaker, name: str) -> CredentialRow:
    async with factory() as session:
        return (await session.execute(select(CredentialRow).where(CredentialRow.name == name))).scalar_one()


@dataclass(frozen=True)
class _Bootstrapped:
    engine: AsyncEngine
    factory: async_sessionmaker
    actor: SystemAssetGovernanceContext
    deepseek_id: uuid.UUID
    opencode_id: uuid.UUID


@asynccontextmanager
async def _bootstrapped(url: str) -> AsyncIterator[_Bootstrapped]:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        assert await bootstrap_default_system_model(
            factory,
            prepare_default_system_model_bootstrap(),
        )
        user_id = await _admin_user(factory)
        deepseek = await _credential(factory, DEEPSEEK_CREDENTIAL_NAME)
        opencode = await _credential(factory, OPENCODE_CREDENTIAL_NAME)
        yield _Bootstrapped(
            engine=engine,
            factory=factory,
            actor=SystemAssetGovernanceContext(
                user_id=user_id,
                request_id=f"req-{user_id.hex}",
            ),
            deepseek_id=uuid.UUID(str(deepseek.id)),
            opencode_id=uuid.UUID(str(opencode.id)),
        )
    finally:
        await engine.dispose()


async def _add_active_grant(
    factory: async_sessionmaker,
    *,
    user_id: uuid.UUID,
    credential_version_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> None:
    """Attach one active MCP Credential grant to an exact Credential version."""

    scope = "system" if project_id is None else "project"
    server_id = uuid.uuid4()
    version_id = uuid.uuid4()
    slot_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            McpServerRow(
                id=server_id,
                scope=scope,
                project_id=project_id,
                slug=f"mcp-{server_id.hex[:12]}",
                display_name="Grant holder",
                status="active",
                created_by_user_id=str(user_id),
            )
        )
        await session.flush()
        session.add(
            McpServerVersionRow(
                id=version_id,
                mcp_server_id=server_id,
                version_number=1,
                workflow_status="draft",
                transport="http",
                url="http://127.0.0.1:9/mcp",
                payload_checksum="a" * 64,
                created_by_user_id=str(user_id),
            )
        )
        await session.flush()
        # Credential slots may only be attached while the parent is a Draft.
        session.add(
            McpCredentialSlotRow(
                id=slot_id,
                mcp_server_version_id=version_id,
                name="auth",
                payload_schema={"env": ["DEEPSEEK_API_KEY"]},
            )
        )
        await session.flush()
        version = await session.get(McpServerVersionRow, version_id)
        server = await session.get(McpServerRow, server_id)
        assert version is not None and server is not None
        version.workflow_status = "published"
        await session.flush()
        server.current_published_version_id = version_id
        session.add(
            CredentialGrantRow(
                mcp_server_version_id=version_id,
                credential_slot_id=slot_id,
                credential_version_id=credential_version_id,
                status="active",
                created_by_user_id=str(user_id),
            )
        )
        await session.commit()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_replace_reports_every_system_model_left_on_the_previous_version(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    """DeepSeek Flash and Pro share one Credential, so both stay behind."""

    async with _bootstrapped(migrated_postgres_database_url) as env:
        service = CredentialService(
            env.factory,
            system_models=SystemModelCredentialMigrationAdapter(),
        )

        replaced = await service.replace(
            env.actor,
            env.deepseek_id,
            {"env": {"DEEPSEEK_API_KEY": ROTATED_DEEPSEEK_SECRET}},
            expected_credential_version=1,
        )

        assert replaced.version.version_number == 2
        assert replaced.pending_migration is not None
        assert replaced.pending_migration.total == 2
        assert replaced.pending_migration.mcp_grant_count == 0
        assert replaced.pending_migration.skill_binding_count == 0
        assert replaced.pending_migration.system_model_count == 2
        assert {reference.kind for reference in replaced.pending_migration.references} == {"system_model"}
        assert {reference.display_name for reference in replaced.pending_migration.references} == {
            "DeepSeek V4 Flash",
            "DeepSeek V4 Pro",
        }


@pytest.mark.postgres
@pytest.mark.anyio
async def test_replace_counts_a_stale_mcp_grant_alongside_the_pinned_model(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    """The total is every reference, and the model share stays identifiable."""

    async with _bootstrapped(migrated_postgres_database_url) as env:
        opencode = await _credential(env.factory, OPENCODE_CREDENTIAL_NAME)
        assert opencode.current_version_id is not None
        await _add_active_grant(
            env.factory,
            user_id=env.actor.user_id,
            credential_version_id=uuid.UUID(str(opencode.current_version_id)),
        )
        service = CredentialService(
            env.factory,
            system_models=SystemModelCredentialMigrationAdapter(),
        )

        replaced = await service.replace(
            env.actor,
            env.opencode_id,
            {"env": {"OPENCODE_API_KEY": ROTATED_OPENCODE_SECRET}},
            expected_credential_version=1,
        )

        assert replaced.pending_migration is not None
        assert replaced.pending_migration.total == 2
        assert replaced.pending_migration.mcp_grant_count == 1
        assert replaced.pending_migration.skill_binding_count == 0
        assert replaced.pending_migration.system_model_count == 1
        assert {reference.kind for reference in replaced.pending_migration.references} == {
            "mcp_grant",
            "system_model",
        }


@pytest.mark.postgres
@pytest.mark.anyio
async def test_reported_total_equals_what_migration_actually_moves(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    """The count is a migration preview, not an independent estimate."""

    async with _bootstrapped(migrated_postgres_database_url) as env:
        deepseek = await _credential(env.factory, DEEPSEEK_CREDENTIAL_NAME)
        assert deepseek.current_version_id is not None
        await _add_active_grant(
            env.factory,
            user_id=env.actor.user_id,
            credential_version_id=uuid.UUID(str(deepseek.current_version_id)),
        )
        service = CredentialService(
            env.factory,
            system_models=SystemModelCredentialMigrationAdapter(),
        )
        replaced = await service.replace(
            env.actor,
            env.deepseek_id,
            {"env": {"DEEPSEEK_API_KEY": ROTATED_DEEPSEEK_SECRET}},
            expected_credential_version=1,
        )
        assert replaced.pending_migration is not None
        preview = await service.migration_status(env.actor, env.deepseek_id)
        assert preview == replaced.pending_migration

        migrated = await service.migrate_grants(
            env.actor,
            env.deepseek_id,
            expected_credential_version=2,
        )

        assert migrated.migrated_count + migrated.migrated_model_count == replaced.pending_migration.total
        assert migrated.migrated_model_count == replaced.pending_migration.system_model_count
        migrated_preview = await service.migration_status(env.actor, env.deepseek_id)
        assert migrated_preview.total == 0
        assert migrated_preview.mcp_grant_count == 0
        assert migrated_preview.skill_binding_count == 0
        assert migrated_preview.system_model_count == 0
        assert migrated_preview.references == ()
        assert migrated_preview.current_reference_count == replaced.pending_migration.total
        assert {reference.kind for reference in migrated_preview.current_references} == {"mcp_grant", "system_model"}

        repeated = await service.migrate_grants(
            env.actor,
            env.deepseek_id,
            expected_credential_version=2,
        )
        assert repeated.migrated_count == 0
        assert repeated.migrated_model_count == 0


@pytest.mark.postgres
@pytest.mark.anyio
async def test_replace_reports_nothing_pending_for_an_unreferenced_credential(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    """A silent success is correct when there is nothing to migrate."""

    async with _bootstrapped(migrated_postgres_database_url) as env:
        service = CredentialService(
            env.factory,
            system_models=SystemModelCredentialMigrationAdapter(),
        )
        created = await service.create(
            env.actor,
            CreateCredential("spare-api-key", "Spare API key", "mcp_auth"),
            {"env": {"SPARE_API_KEY": "initial"}},
        )

        replaced = await service.replace(
            env.actor,
            created.id,
            {"env": {"SPARE_API_KEY": "rotated"}},
            expected_credential_version=1,
        )

        assert replaced.pending_migration is not None
        assert replaced.pending_migration.total == 0
        assert replaced.pending_migration.mcp_grant_count == 0
        assert replaced.pending_migration.skill_binding_count == 0
        assert replaced.pending_migration.system_model_count == 0


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_scope_replace_counts_grants_without_the_model_catalog(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    """No system model can reference a project Credential.

    A project replacement must therefore report its own references without
    consulting the singleton model catalog at all, which is proven here by
    leaving the system-model port unwired.
    """

    async with _bootstrapped(migrated_postgres_database_url) as env:
        project_id = uuid.uuid4()
        async with env.factory() as session:
            session.add(
                ProjectRow(
                    id=project_id,
                    slug=f"p-{project_id.hex[:12]}",
                    display_name="Scoped project",
                    created_by_user_id=str(env.actor.user_id),
                )
            )
            await session.commit()
        project_actor = SystemAssetGovernanceContext(
            user_id=env.actor.user_id,
            request_id=env.actor.request_id,
            project_id=project_id,
        )
        service = CredentialService(env.factory)
        created = await service.create(
            project_actor,
            CreateCredential("project-api-key", "Project API key", "mcp_auth"),
            {"env": {"PROJECT_API_KEY": "initial"}},
        )
        assert created.current_version_id is not None
        await _add_active_grant(
            env.factory,
            user_id=env.actor.user_id,
            credential_version_id=uuid.UUID(str(created.current_version_id)),
            project_id=project_id,
        )

        replaced = await service.replace(
            project_actor,
            created.id,
            {"env": {"PROJECT_API_KEY": "rotated"}},
            expected_credential_version=1,
        )

        assert replaced.pending_migration is not None
        assert replaced.pending_migration.total == 1
        assert replaced.pending_migration.mcp_grant_count == 1
        assert replaced.pending_migration.skill_binding_count == 0
        assert replaced.pending_migration.system_model_count == 0


@pytest.mark.postgres
@pytest.mark.anyio
async def test_system_replace_reports_nothing_when_the_model_port_is_unwired(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    """An unwired deployment cannot migrate, so it must not quote a total.

    Counting only grants would report ``0`` while every pinned model is in fact
    still on the retired envelope, which is exactly the silent success this
    feature exists to remove.
    """

    async with _bootstrapped(migrated_postgres_database_url) as env:
        service = CredentialService(env.factory)

        replaced = await service.replace(
            env.actor,
            env.opencode_id,
            {"env": {"OPENCODE_API_KEY": ROTATED_OPENCODE_SECRET}},
            expected_credential_version=1,
        )

        assert replaced.pending_migration is None
