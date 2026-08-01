from __future__ import annotations

import dataclasses
import uuid
from base64 import b64encode
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.system_settings.bootstrap as bootstrap_module
from app.bootstrap_identities import (
    BUILTIN_ASSET_USER_ID,
    BUILTIN_MODEL_USER_ID,
)
from app.shared_assets.crypto import decrypt_credential_payload
from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.models import AssetScope
from app.system_settings.bootstrap import (
    DEFAULT_CREDENTIAL_ID,
    DEFAULT_CREDENTIAL_VERSION_ID,
    DEFAULT_MODEL_ID,
    DefaultSystemModelBootstrapConfigurationInvalid,
    DefaultSystemModelBootstrapConflict,
    prepare_default_system_model_bootstrap,
)
from app.system_settings.credential_adapter import (
    SystemModelMaterializationUnavailable,
)
from app.system_settings.validation import canonical_model_payload_checksum


def _install_keyring(monkeypatch) -> CredentialKeyring:
    key = b"d" * 32
    encoded = b64encode(key).decode("ascii")
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "bootstrap-test")
    monkeypatch.setenv(
        "DEER_FLOW_CREDENTIAL_KEYRING_JSON",
        f'{{"bootstrap-test":"{encoded}"}}',
    )
    return CredentialKeyring(
        active_key_id="bootstrap-test",
        _keys={"bootstrap-test": key},
    )


def test_default_model_bootstrap_material_matches_removed_yaml_and_hides_secret(
    monkeypatch,
) -> None:
    keyring = _install_keyring(monkeypatch)
    secret = "deepseek-bootstrap-secret"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)

    material = prepare_default_system_model_bootstrap()

    assert material.command.logical_name == "deepseek-v4"
    assert material.command.display_name == "DeepSeek V4 Pro"
    assert material.command.status == "active"
    assert material.command.provider_adapter == "patched_deepseek"
    assert material.command.provider_model == "deepseek-v4-pro"
    assert dict(material.command.settings) == {
        "base_url": "https://api.deepseek.com",
        "max_retries": 2,
        "max_tokens": 16384,
        "reasoning_effort": "high",
        "request_timeout": 600.0,
        "temperature": 0.7,
        "when_thinking_disabled": {
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "when_thinking_enabled": {
            "extra_body": {"thinking": {"type": "enabled"}},
        },
    }
    assert material.command.supports_thinking is True
    assert material.command.supports_reasoning_effort is True
    assert material.command.supports_vision is False
    assert material.command.credential_id == material.credential_id
    assert material.command.credential_version_id == DEFAULT_CREDENTIAL_VERSION_ID
    assert material.command.credential_env_key == "DEEPSEEK_API_KEY"
    assert secret not in repr(material)
    assert all(field.name != "api_key" for field in dataclasses.fields(material))

    assert decrypt_credential_payload(
        material.envelope,
        AssetScope.SYSTEM,
        None,
        DEFAULT_CREDENTIAL_VERSION_ID,
        keyring,
    ) == {"env": {"DEEPSEEK_API_KEY": secret}}


@pytest.mark.parametrize(
    ("missing_name", "invalid_value"),
    [
        ("DEEPSEEK_API_KEY", None),
        ("DEEPSEEK_API_KEY", ""),
        ("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", None),
        ("DEER_FLOW_CREDENTIAL_KEYRING_JSON", None),
        ("DEER_FLOW_CREDENTIAL_KEYRING_JSON", "{}"),
    ],
)
def test_default_model_bootstrap_preflight_fails_secret_free(
    monkeypatch,
    missing_name: str,
    invalid_value: str | None,
) -> None:
    _install_keyring(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    if invalid_value is None:
        monkeypatch.delenv(missing_name, raising=False)
    else:
        monkeypatch.setenv(missing_name, invalid_value)

    with pytest.raises(
        DefaultSystemModelBootstrapConfigurationInvalid,
    ) as exc_info:
        prepare_default_system_model_bootstrap()

    rendered = "".join(__import__("traceback").format_exception(exc_info.value))
    assert "must-not-leak" not in rendered
    assert "bootstrap-test" not in rendered
    assert "postgresql://" not in rendered


def test_default_model_uses_a_distinct_non_human_bootstrap_principal() -> None:
    assert isinstance(BUILTIN_MODEL_USER_ID, uuid.UUID)
    assert BUILTIN_MODEL_USER_ID != BUILTIN_ASSET_USER_ID


@pytest.mark.asyncio
async def test_existing_default_catalog_must_decrypt_with_current_keyring(
    monkeypatch,
) -> None:
    command = bootstrap_module._default_model_command()
    persisted = SimpleNamespace(
        model=SimpleNamespace(
            id=DEFAULT_MODEL_ID,
            logical_name=command.logical_name,
            display_name=command.display_name,
            description=command.description,
            status=command.status,
            sort_order=command.sort_order,
        ),
        version=SimpleNamespace(
            provider_adapter=command.provider_adapter,
            provider_model=command.provider_model,
            settings=command.settings,
            supports_thinking=command.supports_thinking,
            supports_reasoning_effort=command.supports_reasoning_effort,
            supports_vision=command.supports_vision,
            credential_id=DEFAULT_CREDENTIAL_ID,
            credential_version_id=DEFAULT_CREDENTIAL_VERSION_ID,
            credential_env_key=command.credential_env_key,
            payload_checksum=canonical_model_payload_checksum(
                DEFAULT_MODEL_ID,
                command,
            ),
        ),
    )

    class _Repository:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active_model(
            self,
            model_ref,
            *,
            load_envelope,
        ):
            assert model_ref == "default"
            assert load_envelope is True
            return persisted

    adapter = MagicMock()
    adapter.materialize.side_effect = SystemModelMaterializationUnavailable()
    monkeypatch.setattr(
        bootstrap_module,
        "SystemModelRepository",
        _Repository,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "SystemModelCredentialAdapter",
        lambda: adapter,
        raising=False,
    )

    with pytest.raises(DefaultSystemModelBootstrapConflict) as exc_info:
        await bootstrap_module._validate_existing_catalog(MagicMock())

    assert str(exc_info.value) == "DEFAULT_SYSTEM_MODEL_BOOTSTRAP_CONFLICT"
    adapter.materialize.assert_called_once_with(persisted)
