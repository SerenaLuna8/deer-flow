"""M0 gates: host config loading, composition on/off, provider secret roundtrip."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from app.knowledge.composition import create_knowledge_module_from_app_config
from app.knowledge.config import load_knowledge_settings
from app.model_registry.secrets import (
    materialize_provider_api_key,
    protect_provider_api_key,
)
from deerflow.secrets import SecretKey, SecretMaterializationFailed


class _ExtraConfig(BaseModel):
    """Minimal stand-in mirroring AppConfig's ``extra="allow"`` behavior."""

    model_config = ConfigDict(extra="allow")


def test_missing_knowledge_block_disables_the_feature() -> None:
    settings = load_knowledge_settings(_ExtraConfig())
    assert settings.enabled is False


def test_knowledge_block_is_validated_by_package_settings() -> None:
    config = _ExtraConfig.model_validate(
        {
            "knowledge": {
                "enabled": True,
                "worker_concurrency": 4,
                "minio": {
                    "endpoint": "127.0.0.1:9000",
                    "bucket": "actweave-knowledge",
                    "access_key": "ak",
                    "secret_key": "sk",
                },
            }
        }
    )
    settings = load_knowledge_settings(config)
    assert settings.enabled is True
    assert settings.worker_concurrency == 4
    assert settings.minio is not None


def test_invalid_knowledge_block_fails_loudly() -> None:
    config = _ExtraConfig.model_validate({"knowledge": {"enabled": True}})
    with pytest.raises(Exception):
        load_knowledge_settings(config)

    with pytest.raises(ValueError):
        load_knowledge_settings(_ExtraConfig.model_validate({"knowledge": "yes"}))


def test_composition_returns_none_when_disabled() -> None:
    assert create_knowledge_module_from_app_config(_ExtraConfig()) is None
    assert create_knowledge_module_from_app_config(_ExtraConfig.model_validate({"knowledge": {"enabled": False}})) is None


def test_disabled_worker_composition_keeps_project_cleanup_without_starting_the_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deerflow.persistence.engine as engine_module
    from app.knowledge.composition import create_knowledge_worker_resources_from_app_config

    sentinel_factory = object()
    monkeypatch.setattr(engine_module, "get_session_factory", lambda: sentinel_factory)
    monkeypatch.delenv("ACT_WEAVE_SECRET_KEY", raising=False)

    resources = create_knowledge_worker_resources_from_app_config(_ExtraConfig.model_validate({"knowledge": {"enabled": False}}))

    assert resources.feature_module is None
    assert callable(resources.project_purge)


def test_composition_builds_module_on_host_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import deerflow.persistence.engine as engine_module

    sentinel_factory = object()
    monkeypatch.setattr(engine_module, "get_session_factory", lambda: sentinel_factory)
    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")

    config = _ExtraConfig.model_validate(
        {
            "knowledge": {
                "enabled": True,
                "minio": {
                    "endpoint": "127.0.0.1:9000",
                    "bucket": "actweave-knowledge",
                    "access_key": "ak",
                    "secret_key": "sk",
                },
            }
        }
    )
    module = create_knowledge_module_from_app_config(config)
    assert module is not None
    assert module.settings.enabled is True


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
