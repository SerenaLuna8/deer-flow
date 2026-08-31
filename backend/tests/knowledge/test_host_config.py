"""DB-only startup, fail-closed credentials, and isolated Knowledge degradation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from actweave_knowledge import KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError, KnowledgeSettings
from actweave_knowledge.contracts import KnowledgeMinioSettings
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.knowledge import composition
from app.knowledge.config import load_knowledge_settings_from_db
from app.knowledge_settings.service import KnowledgeSettingsError, default_knowledge_settings_row, knowledge_minio_secret_recipient
from app.model_registry.secrets import (
    materialize_provider_api_key,
    protect_provider_api_key,
)
from deerflow.config.app_config import AppConfig
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
from deerflow.secrets import SecretEnvelope, SecretKey, SecretMaterializationFailed


@pytest.mark.asyncio
async def test_db_loading_missing_row_disables_without_seeding_and_roundtrips_secret(postgres_database_url):
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key = SecretKey(b"0" * 32)
    try:
        await _install_full_schema(engine)
        assert not (await load_knowledge_settings_from_db(factory, secret_key=key)).enabled
        async with factory() as session, session.begin():
            assert await session.get(KnowledgeSystemSettingsRow, 1) is None
            row = default_knowledge_settings_row()
            row.enabled, row.worker_concurrency = True, 4
            row.minio_endpoint, row.minio_bucket, row.minio_access_key = "localhost:9000", "knowledge", "operator"
            sealed = SecretEnvelope.protect(b"storage-secret", recipient=knowledge_minio_secret_recipient(row.minio_endpoint), key=key)
            row.minio_secret_nonce, row.minio_secret_ciphertext = sealed.nonce, sealed.ciphertext
            session.add(row)
        loaded = await load_knowledge_settings_from_db(factory, secret_key=key)
        assert loaded.enabled and loaded.worker_concurrency == 4
        assert loaded.minio.secret_key.get_secret_value() == "storage-secret"
        async with factory() as session, session.begin():
            row = await session.get(KnowledgeSystemSettingsRow, 1)
            row.minio_secret_ciphertext = b"x" * 32
        with pytest.raises(KnowledgeSettingsError, match="暂不可用"):
            await load_knowledge_settings_from_db(factory, secret_key=key)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("raw", [{"enabled": False}, {"enabled": True}, "invalid"])
def test_legacy_knowledge_yaml_has_safe_migration_guidance(raw):
    with pytest.raises(ValidationError) as caught:
        AppConfig.model_validate({"knowledge": raw}, context={"config_source": "yaml"})
    assert "LEGACY_CONFIG_REMOVED" in str(caught.value)
    assert "scripts/migrate_knowledge_config.py" in str(caught.value)


def test_removed_config_error_does_not_print_resolved_credentials():
    with pytest.raises(ValidationError) as caught:
        AppConfig.model_validate({"knowledge": {"minio": {"secret_key": "never-log-me"}}}, context={"config_source": "yaml"})
    assert "never-log-me" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,storage_ok,expected", [(False, True, "disabled"), (True, True, "ready"), (True, False, "storage_failed")])
async def test_composition_degrades_only_knowledge_and_closes_failed_module(monkeypatch, enabled, storage_ok, expected):
    import deerflow.persistence.engine as engine_module

    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    settings = KnowledgeSettings(enabled=enabled, minio=KnowledgeMinioSettings(endpoint="localhost:9000", bucket="knowledge", access_key="test", secret_key="test"))
    monkeypatch.setattr(composition, "load_knowledge_settings_from_db", AsyncMock(return_value=settings))

    def factory():
        return None

    monkeypatch.setattr(engine_module, "get_session_factory", lambda: factory)
    module = SimpleNamespace(settings=settings, aclose=AsyncMock())
    probe = AsyncMock(side_effect=None if storage_ok else KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "private storage error"))
    monkeypatch.setattr(composition, "probe_knowledge_storage", probe)
    builds = []

    def build(**kwargs):
        builds.append(kwargs)
        return module

    monkeypatch.setattr(composition, "create_knowledge_module", build)
    actual, state = await composition.create_knowledge_module_from_database(app_config=SimpleNamespace())
    assert state == expected
    assert (actual is module) == (expected == "ready")
    assert len(builds) == int(enabled)
    assert module.aclose.await_count == int(expected == "storage_failed")
    assert probe.await_count == int(enabled)
    if enabled:
        assert builds[0]["session_factory"] is factory


@pytest.mark.asyncio
async def test_unreadable_storage_still_composes_fail_closed_retention(monkeypatch):
    import deerflow.persistence.engine as engine_module

    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    monkeypatch.setattr(engine_module, "get_session_factory", lambda: lambda: None)
    monkeypatch.setattr(composition, "load_knowledge_settings_from_db", AsyncMock(side_effect=KnowledgeSettingsError("KNOWLEDGE_SETTINGS_UNAVAILABLE", 503)))
    resources = await composition.create_knowledge_worker_resources_from_database(app_config=SimpleNamespace())
    assert resources.feature_module is None and resources.startup_state == "storage_failed"
    assert callable(resources.project_purge)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_worker_malformed_endpoint_degrades_with_independent_retention(monkeypatch, enabled):
    import deerflow.persistence.engine as engine_module

    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    # Legacy/disabled settings can pass the package's no-scheme validator but
    # still be rejected by the MinIO constructor. No socket is opened here.
    settings = KnowledgeSettings(enabled=enabled, minio=KnowledgeMinioSettings(endpoint="storage.example.test:9000/path", bucket="fixture", access_key="test-access", secret_key="test-secret"))

    def forbidden_session():
        raise AssertionError("resource composition must not open a database session")

    monkeypatch.setattr(engine_module, "get_session_factory", lambda: forbidden_session)
    monkeypatch.setattr(composition, "load_knowledge_settings_from_db", AsyncMock(return_value=settings))
    resources = await composition.create_knowledge_worker_resources_from_database(app_config=SimpleNamespace())
    assert resources.feature_module is None
    assert resources.startup_state == "storage_failed"
    assert callable(resources.project_purge)


def test_provider_secret_roundtrip_binds_provider_id_and_endpoint() -> None:
    key = SecretKey(b"0" * 32)
    provider_id = uuid4()
    base_url = "https://api.siliconflow.cn/v1"

    protected = protect_provider_api_key(
        provider_id=provider_id,
        base_url=base_url,
        api_key="sk-secret-value",
        key=key,
    )
    assert len(protected.nonce) == 12
    assert protected.ciphertext != b"sk-secret-value"
    assert b"sk-secret-value" not in protected.ciphertext

    assert (
        materialize_provider_api_key(
            provider_id=provider_id,
            base_url=base_url,
            nonce=protected.nonce,
            ciphertext=protected.ciphertext,
            key=key,
        )
        == "sk-secret-value"
    )

    with pytest.raises(SecretMaterializationFailed):
        materialize_provider_api_key(
            provider_id=uuid4(),
            base_url=base_url,
            nonce=protected.nonce,
            ciphertext=protected.ciphertext,
            key=key,
        )

    # The recipient also binds the endpoint origin: a stored ciphertext can
    # never be redirected to a different base_url without a new API Key.
    with pytest.raises(SecretMaterializationFailed):
        materialize_provider_api_key(
            provider_id=provider_id,
            base_url="https://other-endpoint.invalid/v1",
            nonce=protected.nonce,
            ciphertext=protected.ciphertext,
            key=key,
        )
