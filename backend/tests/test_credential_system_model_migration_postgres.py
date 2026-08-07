"""Rotating a System Credential must also re-point the models pinned to it.

Replacing a Credential only mints a new version. System models resolve their key
through an exact ``credential_version_id`` and the adapter accepts a retired
version, so without an explicit re-point a rotated model keeps decrypting the
previous envelope and silently runs on the old key.
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
from app.shared_assets.credential_service import CredentialService
from app.shared_assets.errors import AssetStorageUnavailable, AssetValidationFailed
from app.system_settings import SystemModelMaterializer
from app.system_settings.bootstrap import (
    GPT_5_6_LUNA_MODEL_ID,
    bootstrap_default_system_model,
    prepare_default_system_model_bootstrap,
)
from app.system_settings.credential_migration import (
    SystemModelCredentialMigrationAdapter,
)
from deerflow.persistence.shared_assets import CredentialRow
from deerflow.persistence.system_settings import (
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelConfigVersionRow,
)
from deerflow.persistence.user import UserRow

OPENCODE_CREDENTIAL_NAME = "opencode-api-key"
ROTATED_SECRET = "rotated-opencode-secret"


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


async def _admin_actor(factory: async_sessionmaker) -> SystemAssetGovernanceContext:
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
    return SystemAssetGovernanceContext(
        user_id=user_id,
        request_id=f"req-{user_id.hex}",
    )


async def _credential_id(factory: async_sessionmaker) -> uuid.UUID:
    async with factory() as session:
        row = (
            await session.execute(
                select(CredentialRow).where(
                    CredentialRow.name == OPENCODE_CREDENTIAL_NAME,
                )
            )
        ).scalar_one()
        return uuid.UUID(str(row.id))


async def _current_model_version(
    factory: async_sessionmaker,
) -> SystemModelConfigVersionRow:
    async with factory() as session:
        model = await session.get(SystemModelConfigRow, GPT_5_6_LUNA_MODEL_ID)
        assert model is not None
        version = await session.get(
            SystemModelConfigVersionRow,
            model.current_version_id,
        )
        assert version is not None
        return version


@dataclass(frozen=True)
class _Bootstrapped:
    engine: AsyncEngine
    factory: async_sessionmaker
    actor: SystemAssetGovernanceContext
    credential_id: uuid.UUID


@asynccontextmanager
async def _bootstrapped(url: str) -> AsyncIterator[_Bootstrapped]:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        assert await bootstrap_default_system_model(
            factory,
            prepare_default_system_model_bootstrap(),
        )
        yield _Bootstrapped(
            engine=engine,
            factory=factory,
            actor=await _admin_actor(factory),
            credential_id=await _credential_id(factory),
        )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_migrating_grants_repoints_system_models_at_the_rotated_version(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    async with _bootstrapped(migrated_postgres_database_url) as env:
        factory, actor, credential_id = env.factory, env.actor, env.credential_id
        original = await _current_model_version(factory)
        async with factory() as session:
            state = await session.get(SystemModelCatalogStateRow, 1)
            model = await session.get(SystemModelConfigRow, GPT_5_6_LUNA_MODEL_ID)
            assert state is not None and model is not None
            catalog_revision_before = state.revision
            model_revision_before = model.revision

        service = CredentialService(
            factory,
            system_models=SystemModelCredentialMigrationAdapter(),
        )
        replaced = await service.replace(
            actor,
            credential_id,
            {"env": {"OPENCODE_API_KEY": ROTATED_SECRET}},
            expected_credential_version=1,
        )

        # Replacement alone must leave the model pinned to the retired version.
        assert (await _current_model_version(factory)).id == original.id

        view = await service.migrate_grants(
            actor,
            credential_id,
            expected_credential_version=2,
        )

        assert view.credential_version_id == replaced.version.id
        assert view.migrated_model_count == 1
        assert view.migrated_count == 0

        migrated = await _current_model_version(factory)
        assert migrated.id != original.id
        assert migrated.credential_version_id == replaced.version.id
        assert migrated.credential_id == original.credential_id
        assert migrated.credential_env_key == original.credential_env_key
        assert migrated.supersedes_version_id == original.id
        assert migrated.version_number == original.version_number + 1
        assert migrated.payload_checksum != original.payload_checksum
        assert migrated.provider_adapter == original.provider_adapter
        assert migrated.provider_model == original.provider_model
        assert migrated.settings == original.settings
        assert migrated.supports_thinking == original.supports_thinking
        assert migrated.created_by_user_id == str(actor.user_id)

        async with factory() as session:
            model = await session.get(SystemModelConfigRow, GPT_5_6_LUNA_MODEL_ID)
            state = await session.get(SystemModelCatalogStateRow, 1)
            assert model is not None and state is not None
            assert model.revision == model_revision_before + 1
            assert model.updated_by_user_id == str(actor.user_id)
            assert state.revision == catalog_revision_before + 1
            # The superseded version stays readable for frozen Run snapshots.
            assert await session.get(SystemModelConfigVersionRow, original.id) is not None

        materialized = await SystemModelMaterializer(factory).materialize_active(
            "gpt-5.6-luna",
        )
        assert materialized.api_key is not None
        assert materialized.api_key.get_secret_value() == ROTATED_SECRET


@pytest.mark.postgres
@pytest.mark.anyio
async def test_migration_leaves_models_that_are_already_current_untouched(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    async with _bootstrapped(migrated_postgres_database_url) as env:
        factory, actor, credential_id = env.factory, env.actor, env.credential_id
        service = CredentialService(
            factory,
            system_models=SystemModelCredentialMigrationAdapter(),
        )
        await service.replace(
            actor,
            credential_id,
            {"env": {"OPENCODE_API_KEY": ROTATED_SECRET}},
            expected_credential_version=1,
        )
        await service.migrate_grants(
            actor,
            credential_id,
            expected_credential_version=2,
        )
        first = await _current_model_version(factory)

        repeated = await service.migrate_grants(
            actor,
            credential_id,
            expected_credential_version=2,
        )

        assert repeated.migrated_model_count == 0
        assert (await _current_model_version(factory)).id == first.id
        async with factory() as session:
            versions = (
                await session.execute(
                    select(SystemModelConfigVersionRow).where(
                        SystemModelConfigVersionRow.model_config_id == GPT_5_6_LUNA_MODEL_ID,
                    )
                )
            ).scalars()
            assert len(tuple(versions)) == 2


@pytest.mark.postgres
@pytest.mark.anyio
async def test_migration_fails_closed_when_the_new_version_drops_the_env_key(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    async with _bootstrapped(migrated_postgres_database_url) as env:
        factory, actor, credential_id = env.factory, env.actor, env.credential_id
        original = await _current_model_version(factory)
        service = CredentialService(
            factory,
            system_models=SystemModelCredentialMigrationAdapter(),
        )
        await service.replace(
            actor,
            credential_id,
            {"env": {"RENAMED_OPENCODE_KEY": ROTATED_SECRET}},
            expected_credential_version=1,
        )

        with pytest.raises(AssetValidationFailed):
            await service.migrate_grants(
                actor,
                credential_id,
                expected_credential_version=2,
            )

        async with factory() as session:
            model = await session.get(SystemModelConfigRow, GPT_5_6_LUNA_MODEL_ID)
            assert model is not None
            assert model.current_version_id == original.id


@pytest.mark.postgres
@pytest.mark.anyio
async def test_migration_fails_closed_on_a_model_whose_checksum_does_not_describe_it(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    """``system_model_config_versions`` is append-only, so drift can only be
    introduced by an out-of-band insert. Re-pointing such a row would publish a
    fresh checksum over a payload the previous one never described."""

    async with _bootstrapped(migrated_postgres_database_url) as env:
        factory, actor, credential_id = env.factory, env.actor, env.credential_id
        original = await _current_model_version(factory)
        rogue_model_id = uuid.uuid4()
        rogue_version_id = uuid.uuid4()
        async with factory() as session:
            session.add(
                SystemModelConfigRow(
                    id=rogue_model_id,
                    logical_name=f"rogue-{rogue_model_id.hex}",
                    display_name="Rogue model",
                    description="checksum drift",
                    status="suspended",
                    current_version_id=None,
                    revision=1,
                    sort_order=99,
                    created_by_user_id=str(actor.user_id),
                    updated_by_user_id=str(actor.user_id),
                )
            )
            await session.flush()
            session.add(
                SystemModelConfigVersionRow(
                    id=rogue_version_id,
                    model_config_id=rogue_model_id,
                    version_number=1,
                    provider_adapter=original.provider_adapter,
                    provider_model=original.provider_model,
                    settings=dict(original.settings),
                    supports_thinking=original.supports_thinking,
                    supports_reasoning_effort=original.supports_reasoning_effort,
                    supports_vision=original.supports_vision,
                    credential_id=original.credential_id,
                    credential_version_id=original.credential_version_id,
                    credential_env_key=original.credential_env_key,
                    payload_checksum="0" * 64,
                    supersedes_version_id=None,
                    created_by_user_id=str(actor.user_id),
                )
            )
            await session.flush()
            rogue = await session.get(SystemModelConfigRow, rogue_model_id)
            assert rogue is not None
            rogue.current_version_id = rogue_version_id
            await session.commit()

        service = CredentialService(
            factory,
            system_models=SystemModelCredentialMigrationAdapter(),
        )
        await service.replace(
            actor,
            credential_id,
            {"env": {"OPENCODE_API_KEY": ROTATED_SECRET}},
            expected_credential_version=1,
        )

        with pytest.raises(AssetValidationFailed):
            await service.migrate_grants(
                actor,
                credential_id,
                expected_credential_version=2,
            )

        # The healthy model must not be half-migrated by the rejected batch.
        assert (await _current_model_version(factory)).id == original.id
        async with factory() as session:
            rogue = await session.get(SystemModelConfigRow, rogue_model_id)
            assert rogue is not None
            assert rogue.current_version_id == rogue_version_id


@pytest.mark.postgres
@pytest.mark.anyio
async def test_system_scope_migration_without_the_port_fails_closed(
    migrated_postgres_database_url: str,
    bootstrap_environment: None,
) -> None:
    """An unwired deployment must not report a migration it cannot perform."""

    async with _bootstrapped(migrated_postgres_database_url) as env:
        factory, actor, credential_id = env.factory, env.actor, env.credential_id
        original = await _current_model_version(factory)
        service = CredentialService(factory)
        await service.replace(
            actor,
            credential_id,
            {"env": {"OPENCODE_API_KEY": ROTATED_SECRET}},
            expected_credential_version=1,
        )

        with pytest.raises(AssetStorageUnavailable):
            await service.migrate_grants(
                actor,
                credential_id,
                expected_credential_version=2,
            )

        assert (await _current_model_version(factory)).id == original.id
