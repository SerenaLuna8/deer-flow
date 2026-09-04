"""M9 gates: host model registry service rules over Schema V1.

Providers own one endpoint plus one encrypted API Key; models are typed and
identity-immutable. These tests run the real service against PostgreSQL with
a stubbed probe client, covering create/update/delete rules, the freeze →
probe → settle conflict window, reference protection, audit metadata, and the
project-facing options query.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_EMBEDDING_FAILED,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_RERANK_FAILED,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeRerankMaterial,
)
from registry_helpers import TEST_REGISTRY_API_KEY, registry_secret_key
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.audit.models import AssetAuditMetadata, AuditAction, AuditOutcome, SystemAuditContext
from app.model_registry.secrets import materialize_provider_api_key, protect_provider_api_key
from app.model_registry.service import (
    ModelRegistryService,
    list_active_retrieval_model_options,
)
from app.reliability.operations import resolve_current_system_audit_context
from deerflow.persistence.model_registry import ModelProviderModelRow, ModelProviderRow

_REQUEST_ID = "registry-service-test"


# ---------------------------------------------------------------------------
# Fakes and harness
# ---------------------------------------------------------------------------


class _StubProbeClient:
    """Records typed probe materials; scriptable failure and side effects."""

    def __init__(self) -> None:
        self.materials: list[tuple[str, object]] = []
        self.error: KnowledgeError | None = None
        self.on_probe = None

    async def _record(self, kind: str, material: object) -> None:
        self.materials.append((kind, material))
        if self.on_probe is not None:
            await self.on_probe()
        if self.error is not None:
            raise self.error

    async def verify_embedding(self, material: KnowledgeEmbeddingMaterial) -> None:
        await self._record("embedding", material)

    async def verify_rerank(self, material: KnowledgeRerankMaterial) -> None:
        await self._record("rerank", material)


class _RecordingAudit:
    """AuditService double that validates metadata against the real contract."""

    def __init__(self) -> None:
        self.records: list[tuple[AuditAction, uuid.UUID, AuditOutcome, dict[str, object]]] = []

    async def append(  # noqa: ANN201 - mirrors AuditService.append
        self,
        session,  # noqa: ANN001
        actor,  # noqa: ANN001
        action,  # noqa: ANN001
        target,  # noqa: ANN001
        outcome,  # noqa: ANN001
        metadata,  # noqa: ANN001
        *,
        request_id: str | None = None,
        **_: object,
    ):
        # The real service validates asset metadata; a shape drift must fail here.
        AssetAuditMetadata.model_validate(metadata)
        assert request_id == _REQUEST_ID
        self.records.append((action, target.authority_id, outcome, dict(metadata)))

    def operations(self) -> list[str]:
        return [str(metadata["operation"]) for _, _, _, metadata in self.records]


class _Harness:
    def __init__(self, engine, factory, service, client, audit, context, in_use_ids) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.service = service
        self.client = client
        self.audit = audit
        self.context = context
        self.in_use_ids: set[uuid.UUID] = in_use_ids


class _InjectBeforeSecondSession:
    """Session-factory wrapper awaiting a hook right before the second
    transaction of one service call (the settle phase) is entered.

    Rename-only updates never probe, so ``on_probe`` cannot reach their
    freeze -> settle window; this hook can.
    """

    def __init__(self, factory, hook) -> None:  # noqa: ANN001
        self._factory = factory
        self._hook = hook
        self._calls = 0

    def __call__(self):  # noqa: ANN204 - mirrors async_sessionmaker.__call__
        self._calls += 1
        inner = self._factory()
        if self._calls != 2:
            return inner
        hook = self._hook

        class _Wrapped:
            async def __aenter__(self):  # noqa: ANN204
                await hook()
                return await inner.__aenter__()

            async def __aexit__(self, *args):  # noqa: ANN002, ANN204
                return await inner.__aexit__(*args)

        return _Wrapped()


async def _seed_admin(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                """INSERT INTO users (
                       id, email, username, system_role, created_at,
                       needs_setup, token_version
                   ) VALUES (
                       :user_id, :email, :username, 'system_admin', now(),
                       false, 1
                   )"""
            ),
            {
                "user_id": str(user_id),
                "email": f"{user_id.hex}@example.invalid",
                "username": f"m9_admin_{user_id.hex[:8]}",
            },
        )
    return user_id


async def _issued_context(
    factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> SystemAuditContext:
    async with factory() as session, session.begin():
        return await resolve_current_system_audit_context(session, user_id, _REQUEST_ID)


async def _harness(postgres_database_url: str) -> _Harness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = await _seed_admin(factory)
    context = await _issued_context(factory, admin_id)
    client = _StubProbeClient()
    audit = _RecordingAudit()
    in_use_ids: set[uuid.UUID] = set()

    async def _model_in_use(session: AsyncSession, model_id: uuid.UUID) -> bool:
        del session
        return model_id in in_use_ids

    service = ModelRegistryService(
        factory,
        secret_key=registry_secret_key(),
        client=client,  # type: ignore[arg-type]
        model_in_use=_model_in_use,
        audit_service=audit,  # type: ignore[arg-type]
    )
    return _Harness(engine, factory, service, client, audit, context, in_use_ids)


async def _create_provider(harness: _Harness, *, name: str | None = None) -> uuid.UUID:
    view = await harness.service.create_provider(
        harness.context,
        name=name or f"Provider {uuid.uuid4().hex[:8]}",
        base_url="https://provider.invalid/v1",
        request_timeout_seconds=30,
        api_key=TEST_REGISTRY_API_KEY,
    )
    return view.id


async def _create_embedding(
    harness: _Harness,
    provider_id: uuid.UUID,
    *,
    model_name: str | None = None,
) -> uuid.UUID:
    view = await harness.service.create_model(
        harness.context,
        provider_id,
        model_type="embedding",
        model_name=model_name or f"embed-{uuid.uuid4().hex[:8]}",
        embedding_dimension=1024,
        max_batch=64,
    )
    return view.id


async def _create_rerank(
    harness: _Harness,
    provider_id: uuid.UUID,
    *,
    model_name: str | None = None,
) -> uuid.UUID:
    view = await harness.service.create_model(
        harness.context,
        provider_id,
        model_type="rerank",
        model_name=model_name or f"rerank-{uuid.uuid4().hex[:8]}",
        embedding_dimension=None,
        max_batch=32,
    )
    return view.id


async def _provider_row(harness: _Harness, provider_id: uuid.UUID) -> ModelProviderRow:
    async with harness.factory() as session:
        row = await session.get(ModelProviderRow, provider_id)
        assert row is not None
        return row


def test_provider_row_repr_excludes_encrypted_key_material() -> None:
    """The generic all-columns repr must not leak the encrypted API Key
    components into logs or diagnostics."""

    row = ModelProviderRow(
        id=uuid.uuid4(),
        name="Provider",
        base_url="https://provider.invalid/v1",
        request_timeout_seconds=30,
        api_key_nonce=b"n" * 12,
        api_key_ciphertext=b"c" * 16,
    )
    text = repr(row)
    assert "api_key_nonce" not in text
    assert "api_key_ciphertext" not in text
    assert "Provider" in text


# ---------------------------------------------------------------------------
# Admin authority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unissued_contexts_and_demoted_admins_read_as_not_found(postgres_database_url: str) -> None:
    """Forged contexts and mid-flight role demotion both collapse to 404."""

    harness = await _harness(postgres_database_url)
    try:
        forged = SimpleNamespace(user_id=uuid.uuid4(), request_id=_REQUEST_ID)
        with pytest.raises(KnowledgeError) as error:
            await harness.service.list_providers(forged)
        assert error.value.code == KNOWLEDGE_NOT_FOUND

        # The context was issued, but the admin row lost its role since.
        async with harness.factory() as session, session.begin():
            await session.execute(
                text("UPDATE users SET system_role = 'user' WHERE id = :id"),
                {"id": str(harness.context.user_id)},
            )
        with pytest.raises(KnowledgeError) as error:
            await harness.service.list_providers(harness.context)
        assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Providers: create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_provider_persists_encrypted_key_and_audits(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        view = await harness.service.create_provider(
            harness.context,
            name="  SiliconFlow  ",
            base_url="https://provider.invalid/v1/",
            request_timeout_seconds=30,
            api_key="sk-plain-secret",
        )

        assert view.name == "SiliconFlow"
        assert view.base_url == "https://provider.invalid/v1"  # trailing slash trimmed
        assert view.api_key_configured is True
        assert view.model_count == 0
        assert view.active_model_count == 0
        assert view.endpoint_frozen is False

        row = await _provider_row(harness, view.id)
        assert b"sk-plain-secret" not in bytes(row.api_key_ciphertext)
        decrypted = materialize_provider_api_key(
            provider_id=view.id,
            base_url=row.base_url,
            nonce=row.api_key_nonce,
            ciphertext=row.api_key_ciphertext,
            key=registry_secret_key(),
        )
        assert decrypted == "sk-plain-secret"

        assert harness.audit.records == [
            (
                AuditAction.ASSET_CREATED,
                view.id,
                AuditOutcome.SUCCESS,
                {
                    "asset_kind": "model_provider",
                    "operation": "model_provider.create",
                },
            )
        ]
        # No probe on provider creation: models are probed when they are added.
        assert harness.client.materials == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_provider_rejects_duplicates_and_invalid_fields(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        await _create_provider(harness, name="Duplicated")
        with pytest.raises(KnowledgeError) as conflict:
            await _create_provider(harness, name="Duplicated")
        assert conflict.value.code == KNOWLEDGE_NAME_CONFLICT

        for kwargs in (
            {"name": "   "},
            {"name": "x" * 121},
            {"base_url": "ftp://provider.invalid"},
            {"base_url": "not-a-url"},
            {"base_url": "https://provider.invalid/v1?tenant=1"},
            {"base_url": "https://user:pass@provider.invalid/v1"},
            {"request_timeout_seconds": 0},
            {"request_timeout_seconds": 301},
            {"api_key": "   "},
        ):
            values: dict[str, object] = {
                "name": f"P-{uuid.uuid4().hex[:8]}",
                "base_url": "https://provider.invalid/v1",
                "request_timeout_seconds": 30,
                "api_key": TEST_REGISTRY_API_KEY,
                **kwargs,
            }
            with pytest.raises(KnowledgeError) as error:
                await harness.service.create_provider(harness.context, **values)  # type: ignore[arg-type]
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST

        async with harness.factory() as session:
            count = await session.scalar(select(func.count()).select_from(ModelProviderRow))
        assert count == 1
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Models: create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_model_probes_with_decrypted_material_before_persisting(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)

        embedding_view = await harness.service.create_model(
            harness.context,
            provider_id,
            model_type="embedding",
            model_name="bge-m3",
            embedding_dimension=1024,
            max_batch=64,
        )
        rerank_view = await harness.service.create_model(
            harness.context,
            provider_id,
            model_type="rerank",
            model_name="bge-reranker",
            embedding_dimension=None,
            max_batch=32,
        )

        (embedding_kind, embedding_material), (rerank_kind, rerank_material) = harness.client.materials
        assert embedding_kind == "embedding"
        assert isinstance(embedding_material, KnowledgeEmbeddingMaterial)
        assert embedding_material.model_id == embedding_view.id
        assert embedding_material.base_url == "https://provider.invalid/v1"
        assert embedding_material.api_key == TEST_REGISTRY_API_KEY  # decrypted stored key
        assert embedding_material.dimension == 1024
        assert rerank_kind == "rerank"
        assert isinstance(rerank_material, KnowledgeRerankMaterial)
        assert rerank_material.model_id == rerank_view.id

        assert embedding_view.status == "active"
        assert embedding_view.in_use is False
        assert harness.audit.operations() == [
            "model_provider.create",
            "provider_model.create",
            "provider_model.create",
        ]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_model_failing_probe_leaves_no_row(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)
        harness.client.error = KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "embedding broken")

        with pytest.raises(KnowledgeError) as error:
            await _create_embedding(harness, provider_id)
        assert error.value.code == KNOWLEDGE_EMBEDDING_FAILED
        async with harness.factory() as session:
            count = await session.scalar(select(func.count()).select_from(ModelProviderModelRow))
        assert count == 0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_model_validates_shape_before_any_probe(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)
        for kwargs in (
            {"model_type": "chat", "embedding_dimension": None, "max_batch": 32},
            {"model_type": "embedding", "embedding_dimension": None, "max_batch": 64},
            {"model_type": "embedding", "embedding_dimension": 0, "max_batch": 64},
            {"model_type": "embedding", "embedding_dimension": 16001, "max_batch": 64},
            {"model_type": "embedding", "embedding_dimension": 1024, "max_batch": 2049},
            {"model_type": "rerank", "embedding_dimension": 1024, "max_batch": 32},
            {"model_type": "rerank", "embedding_dimension": None, "max_batch": 257},
            {"model_type": "rerank", "embedding_dimension": None, "max_batch": 32, "model_name": " "},
        ):
            values: dict[str, object] = {"model_name": "valid-name", **kwargs}
            with pytest.raises(KnowledgeError) as error:
                await harness.service.create_model(harness.context, provider_id, **values)  # type: ignore[arg-type]
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert harness.client.materials == []

        with pytest.raises(KnowledgeError) as missing:
            await _create_embedding(harness, uuid.uuid4())
        assert missing.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Providers: update (freeze -> probe -> settle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_provider_rename_skips_probe_and_key_rotation_probes_actives(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)
        await _create_embedding(harness, provider_id)
        rerank_id = await _create_rerank(harness, provider_id)
        disabled_id = await _create_rerank(harness, provider_id)
        await harness.service.set_model_status(harness.context, disabled_id, "disabled")
        harness.client.materials.clear()

        renamed = await harness.service.update_provider(
            harness.context,
            provider_id,
            name="Renamed Provider",
        )
        assert renamed.name == "Renamed Provider"
        assert harness.client.materials == []  # endpoint material untouched

        rotated = await harness.service.update_provider(
            harness.context,
            provider_id,
            api_key="sk-rotated",
        )
        # Every active model answered with the new key; the disabled one did not.
        probed = {(kind, material.model_id) for kind, material in harness.client.materials}  # type: ignore[attr-defined]
        assert len(harness.client.materials) == 2
        assert all(material.api_key == "sk-rotated" for _, material in harness.client.materials)  # type: ignore[attr-defined]
        assert (kind_ids := {model_id for _, model_id in probed}) and disabled_id not in kind_ids
        assert rerank_id in kind_ids

        row = await _provider_row(harness, provider_id)
        decrypted = materialize_provider_api_key(
            provider_id=provider_id,
            base_url=row.base_url,
            nonce=row.api_key_nonce,
            ciphertext=row.api_key_ciphertext,
            key=registry_secret_key(),
        )
        assert decrypted == "sk-rotated"
        assert rotated.api_key_configured is True
        assert harness.audit.operations()[-1] == "model_provider.secret.replace"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_provider_base_url_needs_a_new_key_and_no_bound_embedding(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)
        embedding_id = await _create_embedding(harness, provider_id)
        harness.client.materials.clear()

        with pytest.raises(KnowledgeError) as no_key:
            await harness.service.update_provider(
                harness.context,
                provider_id,
                base_url="https://other.invalid/v1",
            )
        assert no_key.value.code == KNOWLEDGE_INVALID_REQUEST

        harness.in_use_ids.add(embedding_id)
        with pytest.raises(KnowledgeError) as frozen:
            await harness.service.update_provider(
                harness.context,
                provider_id,
                base_url="https://other.invalid/v1",
                api_key="sk-moved",
            )
        assert frozen.value.code == KNOWLEDGE_INVALID_REQUEST
        assert harness.client.materials == []  # rejected before any probe

        harness.in_use_ids.clear()
        moved = await harness.service.update_provider(
            harness.context,
            provider_id,
            base_url="https://other.invalid/v1",
            api_key="sk-moved",
        )
        assert moved.base_url == "https://other.invalid/v1"
        # The probe exercised the merged (new) endpoint with the new key.
        _, material = harness.client.materials[0]
        assert material.base_url == "https://other.invalid/v1"  # type: ignore[attr-defined]
        assert material.api_key == "sk-moved"  # type: ignore[attr-defined]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_provider_settle_rejects_a_concurrent_material_change(postgres_database_url: str) -> None:
    """The probe runs outside locks; settle re-locks and rejects stale material."""

    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)
        await _create_embedding(harness, provider_id)
        harness.client.materials.clear()

        async def _concurrent_admin_change() -> None:
            async with harness.factory() as session, session.begin():
                row = await session.get(ModelProviderRow, provider_id)
                assert row is not None
                row.request_timeout_seconds = 120

        harness.client.on_probe = _concurrent_admin_change

        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_provider(
                harness.context,
                provider_id,
                request_timeout_seconds=60,
            )
        assert error.value.code == KNOWLEDGE_CONFLICT

        row = await _provider_row(harness, provider_id)
        assert row.request_timeout_seconds == 120, "the concurrent write must survive"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_provider_rename_only_must_not_revert_a_concurrent_endpoint_change(postgres_database_url: str) -> None:
    """A rename-only settle writes no endpoint material: a base_url + Key
    replacement committed between its freeze and settle must survive, and the
    stored ciphertext must stay decryptable against the new origin."""

    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)

        async def _concurrent_endpoint_replacement() -> None:
            envelope = protect_provider_api_key(
                provider_id=provider_id,
                base_url="https://moved.invalid/v1",
                api_key="sk-concurrent",
                key=registry_secret_key(),
            )
            async with harness.factory() as session, session.begin():
                row = await session.get(ModelProviderRow, provider_id)
                assert row is not None
                row.base_url = "https://moved.invalid/v1"
                row.request_timeout_seconds = 90
                row.api_key_nonce = envelope.nonce
                row.api_key_ciphertext = envelope.ciphertext

        async def _model_in_use(session: AsyncSession, model_id: uuid.UUID) -> bool:
            del session
            return model_id in harness.in_use_ids

        service = ModelRegistryService(
            _InjectBeforeSecondSession(harness.factory, _concurrent_endpoint_replacement),  # type: ignore[arg-type]
            secret_key=registry_secret_key(),
            client=harness.client,  # type: ignore[arg-type]
            model_in_use=_model_in_use,
            audit_service=harness.audit,  # type: ignore[arg-type]
        )

        renamed = await service.update_provider(
            harness.context,
            provider_id,
            name="Renamed During Concurrency",
        )
        assert renamed.name == "Renamed During Concurrency"
        assert renamed.base_url == "https://moved.invalid/v1"

        row = await _provider_row(harness, provider_id)
        assert row.base_url == "https://moved.invalid/v1", "the concurrent endpoint change must survive"
        assert row.request_timeout_seconds == 90
        decrypted = materialize_provider_api_key(
            provider_id=provider_id,
            base_url=row.base_url,
            nonce=row.api_key_nonce,
            ciphertext=row.api_key_ciphertext,
            key=registry_secret_key(),
        )
        assert decrypted == "sk-concurrent"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_provider_failing_probe_keeps_the_stored_material(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)
        await _create_embedding(harness, provider_id)
        harness.client.error = KnowledgeError(KNOWLEDGE_RERANK_FAILED, "rerank broken")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_provider(
                harness.context,
                provider_id,
                api_key="sk-should-not-stick",
            )
        assert error.value.code == KNOWLEDGE_RERANK_FAILED

        row = await _provider_row(harness, provider_id)
        decrypted = materialize_provider_api_key(
            provider_id=provider_id,
            base_url=row.base_url,
            nonce=row.api_key_nonce,
            ciphertext=row.api_key_ciphertext,
            key=registry_secret_key(),
        )
        assert decrypted == TEST_REGISTRY_API_KEY
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Providers: delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_provider_requires_no_live_models_and_keeps_a_hidden_tombstone(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_name = "Soft-deleted Provider"
        provider_id = await _create_provider(harness, name=provider_name)
        model_id = await _create_embedding(harness, provider_id)

        with pytest.raises(KnowledgeError) as blocked:
            await harness.service.delete_provider(harness.context, provider_id)
        assert blocked.value.code == KNOWLEDGE_INVALID_REQUEST

        await harness.service.delete_model(harness.context, model_id)
        await harness.service.delete_provider(harness.context, provider_id)

        async with harness.factory() as session:
            provider = await session.get(ModelProviderRow, provider_id)
            model = await session.get(ModelProviderModelRow, model_id)
            assert provider is not None
            assert getattr(provider, "deleted_at", None) is not None
            assert model is not None
            assert model.provider_id == provider_id
            assert getattr(model, "deleted_at", None) is not None
        assert await harness.service.list_providers(harness.context) == []

        with pytest.raises(KnowledgeError) as missing:
            await harness.service.delete_provider(harness.context, provider_id)
        assert missing.value.code == KNOWLEDGE_NOT_FOUND
        assert harness.audit.operations()[-2:] == ["provider_model.delete", "model_provider.delete"]
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Models: status, delete, test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_requires_unreferenced_and_enable_reprobes(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)
        model_id = await _create_embedding(harness, provider_id)

        harness.in_use_ids.add(model_id)
        with pytest.raises(KnowledgeError) as referenced:
            await harness.service.set_model_status(harness.context, model_id, "disabled")
        assert referenced.value.code == KNOWLEDGE_INVALID_REQUEST

        harness.in_use_ids.clear()
        disabled = await harness.service.set_model_status(harness.context, model_id, "disabled")
        assert disabled.status == "disabled"
        harness.client.materials.clear()

        # Idempotent disable performs no work and no probe.
        again = await harness.service.set_model_status(harness.context, model_id, "disabled")
        assert again.status == "disabled"
        assert harness.client.materials == []

        # Re-enabling must prove the model still answers.
        harness.client.error = KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "still broken")
        with pytest.raises(KnowledgeError):
            await harness.service.set_model_status(harness.context, model_id, "active")
        async with harness.factory() as session:
            row = await session.get(ModelProviderModelRow, model_id)
            assert row is not None and row.status == "disabled"

        harness.client.error = None
        enabled = await harness.service.set_model_status(harness.context, model_id, "active")
        assert enabled.status == "active"
        assert [kind for kind, _ in harness.client.materials][-1] == "embedding"
        assert harness.audit.operations()[-2:] == ["provider_model.disable", "provider_model.enable"]

        with pytest.raises(KnowledgeError) as invalid:
            await harness.service.set_model_status(harness.context, model_id, "deleted")
        assert invalid.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_model_rejects_an_in_use_model_without_tombstoning(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)
        model_id = await _create_rerank(harness, provider_id)

        harness.in_use_ids.add(model_id)
        with pytest.raises(KnowledgeError) as referenced:
            await harness.service.delete_model(harness.context, model_id)
        assert referenced.value.code == KNOWLEDGE_INVALID_REQUEST

        async with harness.factory() as session:
            model = await session.get(ModelProviderModelRow, model_id)
            assert model is not None
            assert model.status == "active"
            assert getattr(model, "deleted_at", None) is None
        assert "provider_model.delete" not in harness.audit.operations()
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_model_keeps_a_disabled_tombstone_and_hides_it_from_catalogs(
    postgres_database_url: str,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness, name="Soft-delete Models")
        model_id = await _create_rerank(
            harness,
            provider_id,
            model_name="reusable-reranker",
        )

        await harness.service.delete_model(harness.context, model_id)

        async with harness.factory() as session:
            model = await session.get(ModelProviderModelRow, model_id)
            assert model is not None
            assert model.status == "disabled"
            assert getattr(model, "deleted_at", None) is not None

        assert await harness.service.list_models(harness.context, provider_id) == []
        providers = await harness.service.list_providers(harness.context)
        assert [(provider.id, provider.model_count, provider.active_model_count) for provider in providers] == [(provider_id, 0, 0)]
        async with harness.factory() as session, session.begin():
            embedding_options, rerank_options = await list_active_retrieval_model_options(session)
        assert embedding_options == []
        assert rerank_options == []

        with pytest.raises(KnowledgeError) as missing:
            await harness.service.delete_model(harness.context, model_id)
        assert missing.value.code == KNOWLEDGE_NOT_FOUND
        assert harness.audit.operations().count("provider_model.delete") == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_test_model_reports_failure_as_a_result_and_audits_the_outcome(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness)
        model_id = await _create_rerank(harness, provider_id)
        harness.client.materials.clear()

        succeeded = await harness.service.test_model(harness.context, model_id)
        assert succeeded.ok is True
        assert harness.client.materials[0][0] == "rerank"
        assert harness.client.materials[0][1].api_key == TEST_REGISTRY_API_KEY  # type: ignore[attr-defined]

        harness.client.error = KnowledgeError(KNOWLEDGE_RERANK_FAILED, "Reranker 服务不可用")
        failed = await harness.service.test_model(harness.context, model_id)
        assert failed.ok is False
        assert failed.message == "Reranker 服务不可用"

        outcomes = [(metadata["operation"], outcome) for _, _, outcome, metadata in harness.audit.records if metadata["operation"] == "provider_model.test"]
        assert outcomes == [
            ("provider_model.test", AuditOutcome.SUCCESS),
            ("provider_model.test", AuditOutcome.FAILED),
        ]

        with pytest.raises(KnowledgeError) as missing:
            await harness.service.test_model(harness.context, uuid.uuid4())
        assert missing.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Listing and project-facing options
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_options_return_only_active_models_split_by_type(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        provider_id = await _create_provider(harness, name="Alpha")
        embedding_id = await _create_embedding(harness, provider_id, model_name="bge-m3")
        rerank_id = await _create_rerank(harness, provider_id, model_name="bge-reranker")
        disabled_id = await _create_embedding(harness, provider_id, model_name="disabled-embed")
        await harness.service.set_model_status(harness.context, disabled_id, "disabled")

        async with harness.factory() as session, session.begin():
            embedding_options, rerank_options = await list_active_retrieval_model_options(session)

        assert [(option.id, option.provider_name, option.model_name, option.embedding_dimension) for option in embedding_options] == [
            (embedding_id, "Alpha", "bge-m3", 1024),
        ]
        assert [(option.id, option.model_name, option.embedding_dimension) for option in rerank_options] == [
            (rerank_id, "bge-reranker", None),
        ]
    finally:
        await harness.engine.dispose()
