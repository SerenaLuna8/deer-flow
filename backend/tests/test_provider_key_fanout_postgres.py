"""Provider Key fan-out over bound text models: locks, 409, change matrix.

These tests run the real ModelRegistryService and SystemModelCatalogService
against PostgreSQL with two distinct system admins, so the per-admin advisory
row lock cannot mask lock-ordering bugs between fan-out and catalog writes.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_INVALID_REQUEST,
    KnowledgeError,
)
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from support.private_thread_seed import seed_private_thread_database

from app.audit.models import SystemAuditContext, resolve_system_audit_context
from app.model_registry.secrets import materialize_provider_api_key
from app.model_registry.service import ModelRegistryService
from app.system_settings.execution_payload import freeze_system_model_material
from app.system_settings.models import CreateSystemModel, UpdateSystemModel
from app.system_settings.repository import SystemModelRepository
from app.system_settings.secrets import model_secret_recipient
from app.system_settings.service import SystemModelCatalogService
from deerflow.persistence.model_registry import ModelProviderRow
from deerflow.persistence.system_settings import (
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
    SystemModelSecretTombstoneRow,
)
from deerflow.persistence.user import UserRow
from deerflow.secrets import SecretEnvelope, SecretKey

_SECRET_KEY = SecretKey(b"f" * 32)
_BASE_URL = "https://fanout.invalid/v1"


class _Harness:
    def __init__(self, seed, registry, catalog, context_a, context_b) -> None:  # noqa: ANN001
        self.seed = seed
        self.factory: async_sessionmaker[AsyncSession] = seed.factory
        self.registry = registry
        self.catalog = catalog
        # Two distinct admins: catalog writes use A, registry writes use B,
        # so the per-admin user-row lock never serializes the two services.
        self.context_a = context_a
        self.context_b = context_b


async def _never_in_use(_session: AsyncSession, _model_id: uuid.UUID) -> bool:
    return False


def _admin_context(user_id: uuid.UUID, request_id: str) -> SystemAuditContext:
    return resolve_system_audit_context(
        SimpleNamespace(id=user_id, system_role="system_admin"),
        request_id=request_id,
    )


async def _harness(database_url: str) -> _Harness:
    seed = await seed_private_thread_database(database_url)
    async with seed.factory() as session, session.begin():
        await session.execute(
            update(UserRow)
            .where(
                UserRow.id.in_(
                    [
                        str(seed.owner_a.user_id),
                        str(seed.owner_b.user_id),
                    ]
                )
            )
            .values(system_role="system_admin")
        )
    registry = ModelRegistryService(
        seed.factory,
        secret_key=_SECRET_KEY,
        client=object(),  # type: ignore[arg-type]  # no retrieval models here
        model_in_use=_never_in_use,
        audit_service=None,
    )
    catalog = SystemModelCatalogService(seed.factory, secret_key=_SECRET_KEY)
    return _Harness(
        seed,
        registry,
        catalog,
        _admin_context(seed.owner_a.user_id, "fanout-admin-a"),
        _admin_context(seed.owner_b.user_id, "fanout-admin-b"),
    )


async def _create_provider(
    harness: _Harness,
    *,
    name: str,
    base_url: str = _BASE_URL,
    api_key: str,
) -> uuid.UUID:
    view = await harness.registry.create_provider(
        harness.context_b,
        name=name,
        base_url=base_url,
        request_timeout_seconds=30,
        api_key=api_key,
    )
    return view.id


async def _create_text_model(
    harness: _Harness,
    provider_id: uuid.UUID,
    *,
    display_name: str,
    status: str = "active",
) -> uuid.UUID:
    view = await harness.catalog.create_model(
        harness.context_a,
        CreateSystemModel(
            display_name=display_name,
            status=status,
            provider_id=provider_id,
            provider_adapter="deepseek",
            provider_model="deepseek-v4-flash",
            max_input_tokens=64_000,
            settings={},
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_vision=False,
        ),
    )
    return view.id


async def _model_state(
    factory: async_sessionmaker[AsyncSession],
    model_id: uuid.UUID,
) -> dict[str, object]:
    async with factory() as session:
        model = await session.get(SystemModelConfigRow, model_id)
        assert model is not None
        return {
            "revision": int(model.revision),
            "secret_revision": int(model.secret_revision),
            "generation_id": model.current_secret_generation_id,
            "payload_checksum": model.payload_checksum,
            "base_url": model.settings.get("base_url"),
            "status": model.status,
            "deleted_at": model.deleted_at,
        }


async def _catalog_revision(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        revision = await session.scalar(
            text("SELECT revision FROM system_model_catalog_state WHERE id = 1"),
        )
        return int(revision)


async def _provider_key(
    factory: async_sessionmaker[AsyncSession],
    provider_id: uuid.UUID,
) -> str:
    async with factory() as session:
        provider = await session.get(ModelProviderRow, provider_id)
        assert provider is not None
        return materialize_provider_api_key(
            provider_id=uuid.UUID(str(provider.id)),
            base_url=provider.base_url,
            nonce=bytes(provider.api_key_nonce),
            ciphertext=bytes(provider.api_key_ciphertext),
            key=_SECRET_KEY,
        )


async def _model_generation_key(
    factory: async_sessionmaker[AsyncSession],
    model_id: uuid.UUID,
) -> str:
    async with factory() as session:
        model = await session.get(SystemModelConfigRow, model_id)
        assert model is not None
        generation = await session.get(
            SystemModelSecretGenerationRow,
            model.current_secret_generation_id,
        )
        assert generation is not None
        recipient = model_secret_recipient(
            uuid.UUID(str(model.id)),
            model.provider_adapter,
            model.settings,
        )
        envelope = SecretEnvelope(
            nonce=bytes(generation.nonce),
            ciphertext=bytes(generation.ciphertext),
        )
        return envelope.materialize(
            recipient=recipient,
            key=_SECRET_KEY,
        ).decode("utf-8")


async def _tombstone_reasons(
    factory: async_sessionmaker[AsyncSession],
    model_id: uuid.UUID,
) -> list[str]:
    async with factory() as session:
        rows = (
            await session.execute(
                select(SystemModelSecretTombstoneRow.reason).where(SystemModelSecretTombstoneRow.model_config_id == model_id).order_by(SystemModelSecretTombstoneRow.destroyed_at),
            )
        ).scalars()
        return list(rows)


@pytest.mark.asyncio
async def test_fanout_is_409_with_zero_partial_commit_while_a_model_row_is_locked(
    migrated_postgres_database_url: str,
) -> None:
    """A busy model-row lock rolls the whole settle back; retry succeeds."""

    harness = await _harness(migrated_postgres_database_url)
    try:
        provider_id = await _create_provider(
            harness,
            name="Fanout Provider",
            api_key="fanout-key-one",
        )
        active_id = await _create_text_model(
            harness,
            provider_id,
            display_name="Fanout active",
        )
        suspended_id = await _create_text_model(
            harness,
            provider_id,
            display_name="Fanout suspended",
            status="suspended",
        )
        before_active = await _model_state(harness.factory, active_id)
        before_suspended = await _model_state(harness.factory, suspended_id)
        before_catalog = await _catalog_revision(harness.factory)

        # Admin A holds the suspended model's row lock, standing in for an
        # in-flight catalog write; admin B's rotation must answer 409.
        async with harness.factory() as locker:
            transaction = await locker.begin()
            await locker.execute(select(SystemModelConfigRow).where(SystemModelConfigRow.id == suspended_id).with_for_update())
            with pytest.raises(KnowledgeError) as busy:
                await harness.registry.update_provider(
                    harness.context_b,
                    provider_id,
                    api_key="fanout-key-two",
                )
            assert busy.value.code == KNOWLEDGE_CONFLICT
            # Zero partial commit while the lock is still held: the provider
            # keeps the old Key and no model changed.
            assert await _provider_key(harness.factory, provider_id) == "fanout-key-one"
            assert await _model_state(harness.factory, active_id) == before_active
            assert await _model_state(harness.factory, suspended_id) == before_suspended
            assert await _catalog_revision(harness.factory) == before_catalog
            await transaction.rollback()

        # A deliberate retry re-runs the whole flow and rotates every bound
        # model, suspended included, exactly once.
        await harness.registry.update_provider(
            harness.context_b,
            provider_id,
            api_key="fanout-key-two",
        )
        after_active = await _model_state(harness.factory, active_id)
        after_suspended = await _model_state(harness.factory, suspended_id)
        for before, after in (
            (before_active, after_active),
            (before_suspended, after_suspended),
        ):
            assert after["secret_revision"] == before["secret_revision"] + 1
            assert after["revision"] == before["revision"] + 1
            assert after["generation_id"] != before["generation_id"]
            # A pure Key rotation never touches the payload.
            assert after["payload_checksum"] == before["payload_checksum"]
            assert after["status"] == before["status"]
        assert await _catalog_revision(harness.factory) == before_catalog + 1
        assert await _provider_key(harness.factory, provider_id) == "fanout-key-two"
        assert await _model_generation_key(harness.factory, active_id) == "fanout-key-two"
        assert await _model_generation_key(harness.factory, suspended_id) == "fanout-key-two"
        assert await _tombstone_reasons(harness.factory, active_id) == ["replaced"]
        assert await _tombstone_reasons(harness.factory, suspended_id) == ["replaced"]
    finally:
        await harness.seed.engine.dispose()


@pytest.mark.asyncio
async def test_generation_lock_alone_forces_409_and_releases_model_locks(
    migrated_postgres_database_url: str,
) -> None:
    """A busy current-generation lock aborts fan-out after the model locks."""

    harness = await _harness(migrated_postgres_database_url)
    try:
        provider_id = await _create_provider(
            harness,
            name="Generation Lock Provider",
            api_key="generation-key-one",
        )
        model_id = await _create_text_model(
            harness,
            provider_id,
            display_name="Generation locked",
        )
        before = await _model_state(harness.factory, model_id)

        async with harness.factory() as locker:
            transaction = await locker.begin()
            await locker.execute(select(SystemModelSecretGenerationRow).where(SystemModelSecretGenerationRow.id == before["generation_id"]).with_for_update())
            with pytest.raises(KnowledgeError) as busy:
                await harness.registry.update_provider(
                    harness.context_b,
                    provider_id,
                    api_key="generation-key-two",
                )
            assert busy.value.code == KNOWLEDGE_CONFLICT
            # The failed settle released its model lock: another session can
            # take it NOWAIT while the generation lock is still held.
            async with harness.factory() as prober, prober.begin():
                await prober.execute(select(SystemModelConfigRow).where(SystemModelConfigRow.id == model_id).with_for_update(nowait=True))
            assert await _model_state(harness.factory, model_id) == before
            assert await _provider_key(harness.factory, provider_id) == "generation-key-one"
            await transaction.rollback()

        await harness.registry.update_provider(
            harness.context_b,
            provider_id,
            api_key="generation-key-two",
        )
        assert await _model_generation_key(harness.factory, model_id) == "generation-key-two"
    finally:
        await harness.seed.engine.dispose()


@pytest.mark.asyncio
async def test_second_model_failure_rolls_back_the_whole_settle(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _harness(migrated_postgres_database_url)
    try:
        provider_id = await _create_provider(
            harness,
            name="Partial Failure Provider",
            api_key="partial-key-one",
        )
        first_id = await _create_text_model(
            harness,
            provider_id,
            display_name="Partial first",
        )
        second_id = await _create_text_model(
            harness,
            provider_id,
            display_name="Partial second",
        )
        before_first = await _model_state(harness.factory, first_id)
        before_second = await _model_state(harness.factory, second_id)
        before_catalog = await _catalog_revision(harness.factory)

        from app.system_settings import provider_key_fanout

        real_builder = provider_key_fanout.build_model_secret_generation
        calls = {"count": 0}

        def failing_builder(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("simulated re-encryption failure")
            return real_builder(*args, **kwargs)

        monkeypatch.setattr(
            provider_key_fanout,
            "build_model_secret_generation",
            failing_builder,
        )
        with pytest.raises(RuntimeError, match="simulated re-encryption failure"):
            await harness.registry.update_provider(
                harness.context_b,
                provider_id,
                api_key="partial-key-two",
            )
        monkeypatch.undo()
        assert calls["count"] == 2

        # No provider, model, generation, tombstone, or revision write survived.
        assert await _provider_key(harness.factory, provider_id) == "partial-key-one"
        assert await _model_state(harness.factory, first_id) == before_first
        assert await _model_state(harness.factory, second_id) == before_second
        assert await _catalog_revision(harness.factory) == before_catalog
        assert await _tombstone_reasons(harness.factory, first_id) == []
        assert await _tombstone_reasons(harness.factory, second_id) == []
    finally:
        await harness.seed.engine.dispose()


@pytest.mark.asyncio
async def test_provider_change_matrix_over_bound_text_models(
    migrated_postgres_database_url: str,
) -> None:
    """Rename, timeout, Key-only, same-origin URL, and cross-origin changes."""

    harness = await _harness(migrated_postgres_database_url)
    try:
        provider_id = await _create_provider(
            harness,
            name="Matrix Provider",
            api_key="matrix-key-one",
        )
        model_id = await _create_text_model(
            harness,
            provider_id,
            display_name="Matrix model",
        )
        state = await _model_state(harness.factory, model_id)
        catalog = await _catalog_revision(harness.factory)

        # Rename-only: no regeneration, but the catalog advances because the
        # displayed provider_name changed for bound text models.
        await harness.registry.update_provider(
            harness.context_b,
            provider_id,
            name="Matrix Provider Renamed",
        )
        assert await _model_state(harness.factory, model_id) == state
        assert await _catalog_revision(harness.factory) == catalog + 1
        catalog += 1
        listed = {view.id: view for view in await harness.registry.list_providers(harness.context_b)}
        assert listed[provider_id].name == "Matrix Provider Renamed"
        # Provider counts aggregate bound text models.
        assert listed[provider_id].model_count == 1
        assert listed[provider_id].active_model_count == 1

        # Retrieval-timeout-only: retrieval-scoped, catalog untouched.
        await harness.registry.update_provider(
            harness.context_b,
            provider_id,
            request_timeout_seconds=60,
        )
        assert await _model_state(harness.factory, model_id) == state
        assert await _catalog_revision(harness.factory) == catalog

        # Key-only rotation: regeneration without payload changes.
        await harness.registry.update_provider(
            harness.context_b,
            provider_id,
            api_key="matrix-key-two",
        )
        rotated = await _model_state(harness.factory, model_id)
        assert rotated["secret_revision"] == state["secret_revision"] + 1
        assert rotated["revision"] == state["revision"] + 1
        assert rotated["payload_checksum"] == state["payload_checksum"]
        assert rotated["base_url"] == _BASE_URL
        assert await _catalog_revision(harness.factory) == catalog + 1
        catalog += 1
        state = rotated

        # Same-origin URL path change (requires a Key): the recipient is
        # origin-bound, so the tombstone reason stays "replaced", but the
        # derived URL changes the payload checksum.
        await harness.registry.update_provider(
            harness.context_b,
            provider_id,
            base_url="https://fanout.invalid/v2",
            api_key="matrix-key-three",
        )
        moved_path = await _model_state(harness.factory, model_id)
        assert moved_path["secret_revision"] == state["secret_revision"] + 1
        assert moved_path["payload_checksum"] != state["payload_checksum"]
        assert moved_path["base_url"] == "https://fanout.invalid/v2"
        state = moved_path

        # Cross-origin URL change (requires a Key): recipient changes.
        await harness.registry.update_provider(
            harness.context_b,
            provider_id,
            base_url="https://fanout-elsewhere.invalid/v1",
            api_key="matrix-key-four",
        )
        moved_origin = await _model_state(harness.factory, model_id)
        assert moved_origin["secret_revision"] == state["secret_revision"] + 1
        assert moved_origin["payload_checksum"] != state["payload_checksum"]
        assert moved_origin["base_url"] == "https://fanout-elsewhere.invalid/v1"
        assert await _tombstone_reasons(harness.factory, model_id) == [
            "replaced",
            "replaced",
            "recipient_changed",
        ]
        assert await _model_generation_key(harness.factory, model_id) == "matrix-key-four"

        # A URL change without a Key is rejected before any write.
        with pytest.raises(KnowledgeError) as invalid:
            await harness.registry.update_provider(
                harness.context_b,
                provider_id,
                base_url="https://fanout-third.invalid/v1",
            )
        assert invalid.value.code == KNOWLEDGE_INVALID_REQUEST
        assert await _model_state(harness.factory, model_id) == moved_origin
    finally:
        await harness.seed.engine.dispose()


@pytest.mark.asyncio
async def test_same_origin_rebind_regenerates_with_the_target_provider_key(
    migrated_postgres_database_url: str,
) -> None:
    """Rebinding always regenerates: the Provider identity selects the Key."""

    harness = await _harness(migrated_postgres_database_url)
    try:
        source_id = await _create_provider(
            harness,
            name="Rebind Source",
            api_key="rebind-source-key",
        )
        target_id = await _create_provider(
            harness,
            name="Rebind Target",
            api_key="rebind-target-key",
        )
        model_id = await _create_text_model(
            harness,
            source_id,
            display_name="Rebind model",
        )
        before = await _model_state(harness.factory, model_id)
        catalog = await _catalog_revision(harness.factory)

        rebound = await harness.catalog.update_model(
            harness.context_a,
            model_id,
            UpdateSystemModel(
                display_name="Rebind model",
                provider_id=target_id,
                provider_adapter="deepseek",
                provider_model="deepseek-v4-flash",
                max_input_tokens=64_000,
                settings={},
                supports_thinking=True,
                supports_reasoning_effort=True,
                supports_vision=False,
            ),
        )
        assert rebound.provider_id == target_id
        after = await _model_state(harness.factory, model_id)
        # Same origin and identical payload: the checksum stays identical,
        # but the generation must still be replaced with the target's Key.
        assert after["secret_revision"] == before["secret_revision"] + 1
        assert after["revision"] == before["revision"] + 1
        assert after["generation_id"] != before["generation_id"]
        assert after["payload_checksum"] == before["payload_checksum"]
        assert after["base_url"] == _BASE_URL
        assert await _catalog_revision(harness.factory) == catalog + 1
        assert await _tombstone_reasons(harness.factory, model_id) == ["replaced"]
        assert await _model_generation_key(harness.factory, model_id) == "rebind-target-key"

        # The target provider cannot be deleted while the binding exists; the
        # source no longer holds bound text models and deletes cleanly.
        with pytest.raises(KnowledgeError) as blocked:
            await harness.registry.delete_provider(harness.context_b, target_id)
        assert blocked.value.code == KNOWLEDGE_INVALID_REQUEST
        await harness.registry.delete_provider(harness.context_b, source_id)
    finally:
        await harness.seed.engine.dispose()


@pytest.mark.asyncio
async def test_rename_only_settle_preserves_a_concurrent_key_rotation(
    migrated_postgres_database_url: str,
) -> None:
    """A rotation landing between rename freeze and settle survives the rename."""

    harness = await _harness(migrated_postgres_database_url)
    try:
        provider_id = await _create_provider(
            harness,
            name="Rename Race Provider",
            api_key="race-key-one",
        )
        model_id = await _create_text_model(
            harness,
            provider_id,
            display_name="Rename race model",
        )

        rotation_done = {"value": False}

        async def rotate_between_freeze_and_settle() -> None:
            if rotation_done["value"]:
                return
            rotation_done["value"] = True
            await harness.registry.update_provider(
                harness.context_b,
                provider_id,
                api_key="race-key-two",
            )

        class _HookedFactory:
            """Awaits the rotation right before the rename's settle session."""

            def __init__(self, inner) -> None:  # noqa: ANN001
                self._inner = inner
                self._calls = 0

            def __call__(self):  # noqa: ANN204
                self._calls += 1
                session = self._inner()
                if self._calls != 2:
                    return session

                class _Wrapped:
                    async def __aenter__(self):  # noqa: ANN204
                        await rotate_between_freeze_and_settle()
                        return await session.__aenter__()

                    async def __aexit__(self, *args):  # noqa: ANN002, ANN204
                        return await session.__aexit__(*args)

                return _Wrapped()

        racing_registry = ModelRegistryService(
            _HookedFactory(harness.factory),  # type: ignore[arg-type]
            secret_key=_SECRET_KEY,
            client=object(),  # type: ignore[arg-type]
            model_in_use=_never_in_use,
            audit_service=None,
        )
        renamed = await racing_registry.update_provider(
            harness.context_a,
            provider_id,
            name="Rename Race Provider Renamed",
        )
        assert renamed.name == "Rename Race Provider Renamed"
        # Both results hold: the rename landed and the rotation was not
        # reverted by frozen material.
        assert await _provider_key(harness.factory, provider_id) == "race-key-two"
        assert await _model_generation_key(harness.factory, model_id) == "race-key-two"
    finally:
        await harness.seed.engine.dispose()


@pytest.mark.asyncio
async def test_dialog_shaped_rename_conflicts_instead_of_reverting_a_racing_endpoint_change(
    migrated_postgres_database_url: str,
) -> None:
    """Carried-but-unchanged endpoint fields settle as 409 over a racing URL+Key update.

    The admin dialog always submits base_url and request_timeout_seconds, so a
    rename carries the (possibly stale) endpoint values it displayed. Writing
    them back after a concurrent URL+Key update would revert the provider row
    under a ciphertext bound to the new origin; the settle must conflict.
    """

    harness = await _harness(migrated_postgres_database_url)
    try:
        provider_id = await _create_provider(
            harness,
            name="Dialog Race Provider",
            api_key="dialog-key-one",
        )
        model_id = await _create_text_model(
            harness,
            provider_id,
            display_name="Dialog race model",
        )

        moved_url = "https://fanout-moved.invalid/v1"
        move_done = {"value": False}

        async def move_endpoint_between_freeze_and_settle() -> None:
            if move_done["value"]:
                return
            move_done["value"] = True
            await harness.registry.update_provider(
                harness.context_b,
                provider_id,
                base_url=moved_url,
                api_key="dialog-key-two",
            )

        class _HookedFactory:
            """Awaits the endpoint move right before the rename's settle session."""

            def __init__(self, inner) -> None:  # noqa: ANN001
                self._inner = inner
                self._calls = 0

            def __call__(self):  # noqa: ANN204
                self._calls += 1
                session = self._inner()
                if self._calls != 2:
                    return session

                class _Wrapped:
                    async def __aenter__(self):  # noqa: ANN204
                        await move_endpoint_between_freeze_and_settle()
                        return await session.__aenter__()

                    async def __aexit__(self, *args):  # noqa: ANN002, ANN204
                        return await session.__aexit__(*args)

                return _Wrapped()

        racing_registry = ModelRegistryService(
            _HookedFactory(harness.factory),  # type: ignore[arg-type]
            secret_key=_SECRET_KEY,
            client=object(),  # type: ignore[arg-type]
            model_in_use=_never_in_use,
            audit_service=None,
        )
        with pytest.raises(KnowledgeError) as conflict:
            await racing_registry.update_provider(
                harness.context_a,
                provider_id,
                name="Dialog Race Provider Renamed",
                base_url=_BASE_URL,
                request_timeout_seconds=30,
            )
        assert conflict.value.code == KNOWLEDGE_CONFLICT

        # The racing update survives untouched: the provider row keeps the new
        # origin, its ciphertext still materializes against that origin, the
        # bound model carries the fanned-out copy, and the rejected rename
        # left no partial write behind.
        async with harness.factory() as session:
            provider = await session.get(ModelProviderRow, provider_id)
            assert provider is not None
            assert provider.name == "Dialog Race Provider"
            assert provider.base_url == moved_url
        assert await _provider_key(harness.factory, provider_id) == "dialog-key-two"
        assert await _model_generation_key(harness.factory, model_id) == "dialog-key-two"
        assert (await _model_state(harness.factory, model_id))["base_url"] == moved_url
    finally:
        await harness.seed.engine.dispose()


@pytest.mark.asyncio
async def test_provider_key_rotation_still_revokes_a_soft_deleted_text_model_generation(
    migrated_postgres_database_url: str,
) -> None:
    """A model tombstone cannot become a bypass around Provider key rotation."""

    harness = await _harness(migrated_postgres_database_url)
    try:
        provider_id = await _create_provider(
            harness,
            name="Deleted Model Fanout Provider",
            api_key="deleted-model-key-one",
        )
        model_id = await _create_text_model(
            harness,
            provider_id,
            display_name="Deleted model fanout",
        )

        async with harness.factory() as session, session.begin():
            admitted = await SystemModelRepository(session).resolve_active_model(
                str(model_id),
                load_secret=True,
            )
            assert admitted is not None
            frozen_execution = freeze_system_model_material(admitted)

        await harness.catalog.delete_model(harness.context_a, model_id)
        deleted = await _model_state(harness.factory, model_id)
        assert deleted["status"] == "suspended"
        assert deleted["deleted_at"] is not None
        deleted_generation = deleted["generation_id"]
        async with harness.factory() as session, session.begin():
            repository = SystemModelRepository(session)
            assert (
                await repository.resolve_active_model(
                    str(model_id),
                    load_secret=True,
                )
                is None
            )
            historical = await repository.lock_frozen_material(frozen_execution)
            assert historical is not None
            assert historical.model.deleted_at == deleted["deleted_at"]
            assert historical.secret_generation is not None
            assert historical.secret_generation.id == deleted_generation

        await harness.registry.update_provider(
            harness.context_b,
            provider_id,
            api_key="deleted-model-key-two",
        )

        rotated = await _model_state(harness.factory, model_id)
        assert rotated["deleted_at"] == deleted["deleted_at"]
        assert rotated["secret_revision"] == int(deleted["secret_revision"]) + 1
        assert rotated["generation_id"] != deleted_generation
        assert await _model_generation_key(harness.factory, model_id) == ("deleted-model-key-two")
        assert await _tombstone_reasons(harness.factory, model_id) == ["replaced"]
    finally:
        await harness.seed.engine.dispose()
