from __future__ import annotations

import base64
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from support.private_thread_seed import seed_private_thread_database
from support.system_model_seed import seed_model_provider

from app.audit.models import resolve_system_audit_context
from app.audit.service import (
    AuditService,
    _bind_gateway_audit_process,
)
from app.audit.sinks import OperationalAuditSink
from app.model_registry.secrets import protect_provider_api_key
from app.model_registry.service import ModelRegistryService
from app.project_channels.errors import (
    ChannelInstanceForbidden,
    ChannelInstanceValidationFailed,
)
from app.project_channels.models import ConfigureProjectChannelInstance
from app.project_channels.service import ProjectChannelInstanceService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.owner_refs import AuditHmacKeyring
from app.system_settings.materializer import SystemModelMaterializer
from app.system_settings.models import CreateSystemModel, UpdateSystemModel
from app.system_settings.repository import SystemModelRepository
from app.system_settings.service import SystemModelCatalogService
from app.system_settings.validation import PROVIDER_ADAPTERS
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.channel_connections.model import (
    ProjectChannelSecretGenerationRow,
    ProjectChannelSecretStateRow,
    ProjectChannelSecretTombstoneRow,
)
from deerflow.persistence.system_settings import (
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
    SystemModelSecretTombstoneRow,
)
from deerflow.persistence.user import UserRow
from deerflow.secrets import SecretKey


def _project_context(seed, *, role: ProjectRole = ProjectRole.ADMIN):
    owner = seed.owner_a
    return ProjectContext(
        user_id=owner.user_id,
        project_id=owner.project_id,
        membership_id=owner.membership_id,
        role=role,
        capabilities=capabilities_for(role),
        membership_version=owner.membership_version,
        request_id=f"configuration-secret-{role.value}",
    )


def _model_update(
    *,
    provider_id: uuid.UUID,
    display_name: str = "DeepSeek lifecycle",
) -> UpdateSystemModel:
    return UpdateSystemModel(
        display_name=display_name,
        provider_id=provider_id,
        provider_adapter="deepseek",
        provider_model="deepseek-v4-flash",
        max_input_tokens=64_000,
        settings={},
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_vision=False,
    )


def _never_in_use(_session: object, _model_id: uuid.UUID) -> bool:
    raise AssertionError("retrieval model_in_use must not be consulted here")


@pytest.mark.asyncio
async def test_system_model_update_keeps_json_settings_and_checksum_atomic(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON number representation change must reach PostgreSQL with its checksum."""

    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"n" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    admin_id = seed.owner_a.user_id
    try:
        async with seed.factory() as session, session.begin():
            await session.execute(update(UserRow).where(UserRow.id == str(admin_id)).values(system_role="system_admin"))

        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id="system-model-json-checksum-atomicity",
        )
        service = SystemModelCatalogService(
            seed.factory,
            audit_service=AuditService(
                seed.factory,
                AuditHmacKeyring("test", {"test": b"a" * 32}),
            ),
        )
        provider_id = uuid.uuid4()
        envelope = protect_provider_api_key(
            provider_id=provider_id,
            base_url="https://api.deepseek.com",
            api_key="numeric-settings-secret",
            key=SecretKey(b"n" * 32),
        )
        async with seed.factory() as session, session.begin():
            await seed_model_provider(
                session,
                provider_id=provider_id,
                name="DeepSeek JSON provider",
                base_url="https://api.deepseek.com",
                api_key_nonce=envelope.nonce,
                api_key_ciphertext=envelope.ciphertext,
            )
        created = await service.create_model(
            context,
            CreateSystemModel(
                display_name="DeepSeek numeric settings",
                status="active",
                provider_id=provider_id,
                provider_adapter="deepseek",
                provider_model="deepseek-v4-flash",
                max_input_tokens=64_000,
                settings={"request_timeout": 600.0},
                supports_thinking=True,
                supports_reasoning_effort=True,
                supports_vision=False,
            ),
        )

        await service.update_model(
            context,
            created.id,
            UpdateSystemModel(
                display_name="DeepSeek numeric settings",
                provider_id=provider_id,
                provider_adapter="deepseek",
                provider_model="deepseek-v4-flash",
                max_input_tokens=64_000,
                settings={
                    # Browser JSON round-tripping serializes 600.0 as 600.
                    "request_timeout": 600,
                },
                supports_thinking=True,
                supports_reasoning_effort=True,
                supports_vision=False,
            ),
        )

        async with seed.factory() as session:
            model = await session.get(SystemModelConfigRow, created.id)
            assert model is not None
            assert type(model.settings["request_timeout"]) is int

        runtime = await SystemModelMaterializer(
            seed.factory,
        ).materialize_active(str(created.id))
        assert runtime.name == str(created.id)
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_system_model_secret_lifecycle_is_write_only_and_recipient_bound(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"m" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    admin_id = seed.owner_a.user_id
    try:
        async with seed.factory() as session, session.begin():
            await session.execute(update(UserRow).where(UserRow.id == str(admin_id)).values(system_role="system_admin"))

        context = resolve_system_audit_context(
            SimpleNamespace(id=admin_id, system_role="system_admin"),
            request_id="system-model-secret-lifecycle",
        )
        audit = AuditService(
            seed.factory,
            AuditHmacKeyring("test", {"test": b"a" * 32}),
        )
        service = SystemModelCatalogService(
            seed.factory,
            audit_service=audit,
        )
        registry = ModelRegistryService(
            seed.factory,
            secret_key=SecretKey(b"m" * 32),
            client=object(),  # type: ignore[arg-type]  # no retrieval models exist here
            model_in_use=_never_in_use,  # type: ignore[arg-type]
            audit_service=audit,
        )

        # The Provider owns the Key: a model bound at create is immediately
        # ready without ever receiving a model-level Key.
        first_secret = "model-test-value-one"
        provider = await registry.create_provider(
            context,
            name="DeepSeek",
            base_url="https://api.deepseek.com",
            request_timeout_seconds=30,
            api_key=first_secret,
        )
        created = await service.create_model(
            context,
            CreateSystemModel(
                display_name="DeepSeek lifecycle",
                status="active",
                provider_id=provider.id,
                provider_adapter="deepseek",
                provider_model="deepseek-v4-flash",
                max_input_tokens=64_000,
                settings={},
                supports_thinking=True,
                supports_reasoning_effort=True,
                supports_vision=False,
            ),
        )
        assert created.api_key_configured is True
        assert created.secret_readiness == "ready"
        assert created.provider_id == provider.id
        assert created.provider_name == "DeepSeek"
        assert first_secret not in repr(created)

        async with seed.factory() as session:
            admissible = await SystemModelRepository(
                session,
            ).resolve_admissible_active_model(str(created.id))
            assert admissible is not None
            assert admissible.secret_generation is None
            state = await session.get(SystemModelCatalogStateRow, 1)
            assert state is not None
            assert state.default_model_config_id == created.id
            model = await session.get(SystemModelConfigRow, created.id)
            assert model is not None
            # ``base_url`` is derived from the bound Provider, never authored.
            assert model.settings["base_url"] == "https://api.deepseek.com"
            first_generation_id = model.current_secret_generation_id
            assert first_generation_id is not None
            first_generation = await session.get(
                SystemModelSecretGenerationRow,
                first_generation_id,
            )
            assert first_generation is not None
            assert first_secret.encode() not in bytes(first_generation.ciphertext)

        with monkeypatch.context() as retired_adapter:
            retired_adapter.delitem(PROVIDER_ADAPTERS, "deepseek")
            async with seed.factory() as session:
                assert (
                    await SystemModelRepository(
                        session,
                    ).resolve_admissible_active_model(str(created.id))
                    is None
                )

        # A display-only edit never reads or re-encrypts the Provider Key.
        preserved = await service.update_model(
            context,
            created.id,
            _model_update(
                provider_id=provider.id,
                display_name="Renamed without replacement",
            ),
        )
        assert preserved.secret_revision == created.secret_revision
        async with seed.factory() as session:
            model = await session.get(SystemModelConfigRow, created.id)
            assert model is not None
            assert model.current_secret_generation_id == first_generation_id

        # Rotating the Provider Key fans out to every bound text model.
        second_secret = "model-test-value-two"
        await registry.update_provider(
            context,
            provider.id,
            api_key=second_secret,
        )
        async with seed.factory() as session:
            model = await session.get(SystemModelConfigRow, created.id)
            assert model is not None
            assert model.secret_revision == created.secret_revision + 1
            second_generation_id = model.current_secret_generation_id
            assert second_generation_id not in {None, first_generation_id}
            assert (
                await session.get(
                    SystemModelSecretGenerationRow,
                    first_generation_id,
                )
                is None
            )
            first_tombstone = await session.get(
                SystemModelSecretTombstoneRow,
                first_generation_id,
            )
            assert first_tombstone is not None
            assert first_tombstone.reason == "replaced"
            second_generation = await session.get(
                SystemModelSecretGenerationRow,
                second_generation_id,
            )
            assert second_generation is not None
            assert second_secret.encode() not in bytes(second_generation.ciphertext)

        # Rebinding to a Provider with another origin changes the recipient
        # and re-protects that Provider's Key for this model.
        third_secret = "model-test-value-three"
        alternate = await registry.create_provider(
            context,
            name="DeepSeek Alternate",
            base_url="https://alternate.deepseek.invalid",
            request_timeout_seconds=30,
            api_key=third_secret,
        )
        rebound = await service.update_model(
            context,
            created.id,
            _model_update(
                provider_id=alternate.id,
                display_name="Renamed without replacement",
            ),
        )
        assert rebound.provider_id == alternate.id
        assert rebound.api_key_configured is True
        assert rebound.secret_revision == created.secret_revision + 2
        async with seed.factory() as session:
            model = await session.get(SystemModelConfigRow, created.id)
            assert model is not None
            assert model.settings["base_url"] == "https://alternate.deepseek.invalid"
            third_generation_id = model.current_secret_generation_id
            assert third_generation_id not in {None, second_generation_id}
            second_tombstone = await session.get(
                SystemModelSecretTombstoneRow,
                second_generation_id,
            )
            assert second_tombstone is not None
            assert second_tombstone.reason == "recipient_changed"
            third_generation = await session.get(
                SystemModelSecretGenerationRow,
                third_generation_id,
            )
            assert third_generation is not None
            assert third_secret.encode() not in bytes(third_generation.ciphertext)
            tombstones = tuple((await session.execute(select(SystemModelSecretTombstoneRow).where(SystemModelSecretTombstoneRow.model_config_id == created.id))).scalars().all())
            audits = tuple(
                (
                    await session.execute(
                        select(AuditLogRow).where(
                            AuditLogRow.actor_user_id == str(admin_id),
                            AuditLogRow.action == "asset.updated",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert {row.reason for row in tombstones} == {
            "replaced",
            "recipient_changed",
        }
        model_secret_audits = [row for row in audits if isinstance(row.metadata_json, dict) and row.metadata_json.get("asset_kind") == "model"]
        assert len(model_secret_audits) == 3
        serialized_audits = json.dumps(
            [row.metadata_json for row in audits],
            sort_keys=True,
        )
        for secret in (first_secret, second_secret, third_secret):
            assert secret not in serialized_audits
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_channel_instance_owns_bundle_with_blank_preserve_and_clear(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"c" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    context = _project_context(seed)
    audit_service = AuditService(
        seed.factory,
        AuditHmacKeyring("test", {"test": b"b" * 32}),
    )
    audit = OperationalAuditSink(
        audit_service,
        process_context=_bind_gateway_audit_process(audit_service),
    )
    service = ProjectChannelInstanceService(seed.factory, audit=audit)
    first_secret = "channel-test-value-one"
    try:
        unready = await service.configure(
            context,
            "dingtalk",
            ConfigureProjectChannelInstance(
                display_name="DingTalk lifecycle",
                public_config={"client_id": "client-one"},
                secrets={},
                enabled=True,
            ),
        )
        assert unready.configured is True
        assert unready.enabled is False
        assert unready.secret_configured is False
        assert unready.secret_readiness == "unready"

        created = await service.configure(
            context,
            "dingtalk",
            ConfigureProjectChannelInstance(
                display_name="DingTalk lifecycle",
                public_config={"client_id": "client-one"},
                secrets={"client_secret": first_secret},
                enabled=True,
            ),
        )
        assert created.id == unready.id
        assert created.configured is True
        assert created.enabled is True
        assert created.secret_configured is True
        assert created.secret_readiness == "ready"
        assert first_secret not in repr(created)
        assert "secrets" not in vars(created)

        async with seed.factory() as session:
            state = (
                await session.execute(
                    select(ProjectChannelSecretStateRow).where(
                        ProjectChannelSecretStateRow.project_id == context.project_id,
                        ProjectChannelSecretStateRow.channel_instance_id == created.id,
                    )
                )
            ).scalar_one()
            first_generation_id = state.current_generation_id

        preserved = await service.configure(
            context,
            "dingtalk",
            ConfigureProjectChannelInstance(
                display_name="Renamed DingTalk",
                public_config={"client_id": "client-one"},
                secrets={},
                enabled=True,
            ),
        )
        assert preserved.secret_revision == created.secret_revision
        async with seed.factory() as session:
            state = (
                await session.execute(
                    select(ProjectChannelSecretStateRow).where(
                        ProjectChannelSecretStateRow.project_id == context.project_id,
                        ProjectChannelSecretStateRow.channel_instance_id == created.id,
                    )
                )
            ).scalar_one()
            assert state.current_generation_id == first_generation_id

        with pytest.raises(ChannelInstanceValidationFailed):
            await service.configure(
                context,
                "dingtalk",
                ConfigureProjectChannelInstance(
                    display_name="Changed identity",
                    public_config={"client_id": "client-two"},
                    secrets={},
                    enabled=True,
                ),
            )

        second_secret = "channel-test-value-two"
        replaced = await service.configure(
            context,
            "dingtalk",
            ConfigureProjectChannelInstance(
                display_name="Changed identity",
                public_config={"client_id": "client-two"},
                secrets={"client_secret": second_secret},
                enabled=True,
            ),
        )
        assert replaced.secret_revision == created.secret_revision + 1
        async with seed.factory() as session:
            state = (
                await session.execute(
                    select(ProjectChannelSecretStateRow).where(
                        ProjectChannelSecretStateRow.project_id == context.project_id,
                        ProjectChannelSecretStateRow.channel_instance_id == created.id,
                    )
                )
            ).scalar_one()
            second_generation_id = state.current_generation_id
            assert second_generation_id not in {None, first_generation_id}
            assert (
                await session.get(
                    ProjectChannelSecretGenerationRow,
                    first_generation_id,
                )
                is None
            )
            tombstone = (await session.execute(select(ProjectChannelSecretTombstoneRow).where(ProjectChannelSecretTombstoneRow.destroyed_generation_id == first_generation_id))).scalar_one()
            assert tombstone.reason == "recipient_change"

        with pytest.raises(ChannelInstanceValidationFailed):
            await service.clear_secret(
                context,
                "dingtalk",
                confirmed=False,
            )
        cleared = await service.clear_secret(
            context,
            "dingtalk",
            confirmed=True,
        )
        assert cleared.enabled is True
        assert cleared.secret_configured is False
        assert cleared.secret_readiness == "unready"

        changed_after_clear = await service.configure(
            context,
            "dingtalk",
            ConfigureProjectChannelInstance(
                display_name="Changed after independent clear",
                public_config={"client_id": "client-three"},
                secrets={},
                enabled=True,
            ),
        )
        assert changed_after_clear.public_config["client_id"] == "client-three"
        assert changed_after_clear.secret_configured is False
        assert changed_after_clear.secret_readiness == "unready"

        with pytest.raises(ChannelInstanceForbidden):
            await ProjectChannelInstanceService(seed.factory).list(_project_context(seed, role=ProjectRole.RUNNER))

        async with seed.factory() as session:
            generation_count = await session.scalar(
                select(func.count())
                .select_from(ProjectChannelSecretGenerationRow)
                .where(
                    ProjectChannelSecretGenerationRow.project_id == context.project_id,
                    ProjectChannelSecretGenerationRow.channel_instance_id == created.id,
                )
            )
            tombstones = tuple(
                (
                    await session.execute(
                        select(ProjectChannelSecretTombstoneRow).where(
                            ProjectChannelSecretTombstoneRow.project_id == context.project_id,
                            ProjectChannelSecretTombstoneRow.channel_instance_id == created.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            audits = tuple(
                (
                    await session.execute(
                        select(AuditLogRow).where(
                            AuditLogRow.project_id == context.project_id,
                            AuditLogRow.action == "asset.updated",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert generation_count == 0
        assert {row.reason for row in tombstones} == {
            "recipient_change",
            "clear",
        }
        serialized_audits = json.dumps(
            [row.metadata_json for row in audits],
            sort_keys=True,
        )
        assert len(audits) == 3
        for secret in (first_secret, second_secret):
            assert secret not in serialized_audits
    finally:
        await seed.engine.dispose()
