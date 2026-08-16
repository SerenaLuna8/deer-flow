"""Explicit execution-boundary decryption for system model credentials."""

from __future__ import annotations

import uuid

from pydantic import SecretStr

from app.shared_assets.crypto import (
    CredentialDecryptFailed,
    EncryptedEnvelope,
    decrypt_credential_payload,
)
from app.shared_assets.keyring import CredentialKeyring, CredentialKeyringInvalid
from app.shared_assets.models import AssetScope
from app.system_settings.models import (
    ConnectionTestSystemModelMaterial,
    LockedSystemModelMaterial,
)
from app.system_settings.validation import (
    ModelSettingsInvalid,
    provider_class_path,
    provider_credential_required,
    validate_model_settings,
)
from deerflow.config.model_config import ModelConfig


class SystemModelMaterializationUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("System model materialization unavailable")


class SystemModelCredentialAdapter:
    """Decrypt one exact System Credential and return only a runtime config."""

    def __init__(self, *, keyring: CredentialKeyring | None = None) -> None:
        self._keyring = keyring

    def materialize(
        self,
        material: LockedSystemModelMaterial,
    ) -> ModelConfig:
        payload: dict[str, object] | None = None
        try:
            if not isinstance(material, LockedSystemModelMaterial) or material.model.status != "active" or material.version.model_config_id != material.model.id:
                raise ValueError
            settings = validate_model_settings(
                material.version.settings,
                provider_adapter=material.version.provider_adapter,
            )
            class_path = provider_class_path(
                material.version.provider_adapter,
            )
            credential_group = (
                material.version.credential_id,
                material.version.credential_version_id,
                material.version.credential_env_key,
            )
            if provider_credential_required(material.version.provider_adapter) != (credential_group != (None, None, None)):
                raise ValueError
            api_key: SecretStr | None = None
            if credential_group != (None, None, None):
                credential = material.credential
                version = material.credential_version
                envelope = material.envelope
                if (
                    credential is None
                    or version is None
                    or envelope is None
                    or credential.id != material.version.credential_id
                    or version.id != material.version.credential_version_id
                    or version.credential_id != credential.id
                    or envelope.credential_version_id != version.id
                    or credential.scope != "system"
                    or credential.project_id is not None
                    or credential.credential_type != "model_api_key"
                    or credential.status != "active"
                    or credential.is_delete
                    or version.status not in {"active", "retired"}
                    or not envelope.is_active
                ):
                    raise ValueError
                env_schema = version.payload_schema.get("env") if isinstance(version.payload_schema, dict) else None
                env_key = material.version.credential_env_key
                if not isinstance(env_schema, list) or not isinstance(env_key, str) or env_key not in env_schema:
                    raise ValueError
                keyring = self._keyring or CredentialKeyring.from_environment()
                payload = decrypt_credential_payload(
                    EncryptedEnvelope(
                        key_id=envelope.key_id,
                        nonce=bytes(envelope.nonce),
                        ciphertext=bytes(envelope.ciphertext),
                    ),
                    AssetScope.SYSTEM,
                    None,
                    uuid.UUID(str(version.id)),
                    keyring,
                )
                env = payload.get("env")
                value = env.get(env_key) if isinstance(env, dict) else None
                if not isinstance(value, str) or not value:
                    raise ValueError
                api_key = SecretStr(value)
            elif any(value is not None for value in credential_group):
                raise ValueError

            kwargs: dict[str, object] = {
                **settings,
                "name": str(uuid.UUID(str(material.model.id))),
                "display_name": material.model.display_name,
                "description": "",
                "use": class_path,
                "model": material.version.provider_model,
                "supports_thinking": material.version.supports_thinking,
                "supports_reasoning_effort": (material.version.supports_reasoning_effort),
                "supports_vision": material.version.supports_vision,
            }
            if api_key is not None:
                kwargs["api_key"] = api_key
            result = ModelConfig.model_validate(kwargs)
            result._system_model_config_version_id = uuid.UUID(
                str(material.version.id),
            )
            result._system_provider_adapter = material.version.provider_adapter
            return result
        except (
            AttributeError,
            CredentialDecryptFailed,
            CredentialKeyringInvalid,
            ModelSettingsInvalid,
            TypeError,
            ValueError,
        ):
            raise SystemModelMaterializationUnavailable() from None
        finally:
            if isinstance(payload, dict):
                for section in payload.values():
                    if isinstance(section, dict):
                        section.clear()
                payload.clear()

    def materialize_connection_test(
        self,
        material: ConnectionTestSystemModelMaterial,
    ) -> ModelConfig:
        """Return a transient runtime config for one authenticated admin probe."""

        payload: dict[str, object] | None = None
        try:
            if not isinstance(material, ConnectionTestSystemModelMaterial):
                raise ValueError
            command = material.command
            settings = validate_model_settings(
                command.settings,
                provider_adapter=command.provider_adapter,
            )
            class_path = provider_class_path(command.provider_adapter)
            credential_group = (
                command.credential_id,
                command.credential_version_id,
                command.credential_env_key,
            )
            if provider_credential_required(command.provider_adapter) != (credential_group != (None, None, None)):
                raise ValueError
            api_key: SecretStr | None = None
            if credential_group != (None, None, None):
                credential = material.credential
                version = material.credential_version
                envelope = material.envelope
                if (
                    credential is None
                    or version is None
                    or envelope is None
                    or credential.id != command.credential_id
                    or version.id != command.credential_version_id
                    or version.credential_id != credential.id
                    or envelope.credential_version_id != version.id
                    or credential.scope != "system"
                    or credential.project_id is not None
                    or credential.credential_type != "model_api_key"
                    or credential.status != "active"
                    or credential.is_delete
                    or version.status != "active"
                    or not envelope.is_active
                ):
                    raise ValueError
                env_schema = version.payload_schema.get("env") if isinstance(version.payload_schema, dict) else None
                env_key = command.credential_env_key
                if not isinstance(env_schema, list) or not isinstance(env_key, str) or env_key not in env_schema:
                    raise ValueError
                keyring = self._keyring or CredentialKeyring.from_environment()
                payload = decrypt_credential_payload(
                    EncryptedEnvelope(
                        key_id=envelope.key_id,
                        nonce=bytes(envelope.nonce),
                        ciphertext=bytes(envelope.ciphertext),
                    ),
                    AssetScope.SYSTEM,
                    None,
                    uuid.UUID(str(version.id)),
                    keyring,
                )
                env = payload.get("env")
                value = env.get(env_key) if isinstance(env, dict) else None
                if not isinstance(value, str) or not value:
                    raise ValueError
                api_key = SecretStr(value)
            elif any(value is not None for value in credential_group):
                raise ValueError

            kwargs: dict[str, object] = {
                **settings,
                "name": "model-connection-test",
                "display_name": "Model connection test",
                "description": "",
                "use": class_path,
                "model": command.provider_model,
                "supports_thinking": False,
                "supports_reasoning_effort": False,
                "supports_vision": command.supports_vision,
            }
            if api_key is not None:
                kwargs["api_key"] = api_key
            result = ModelConfig.model_validate(kwargs)
            result._system_provider_adapter = command.provider_adapter
            return result
        except (
            AttributeError,
            CredentialDecryptFailed,
            CredentialKeyringInvalid,
            ModelSettingsInvalid,
            TypeError,
            ValueError,
        ):
            raise SystemModelMaterializationUnavailable() from None
        finally:
            if isinstance(payload, dict):
                for section in payload.values():
                    if isinstance(section, dict):
                        section.clear()
                payload.clear()


__all__ = [
    "SystemModelCredentialAdapter",
    "SystemModelMaterializationUnavailable",
]
