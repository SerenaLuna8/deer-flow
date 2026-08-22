"""Authorized execution-boundary materialization for System Models."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping

from pydantic import SecretStr

from app.system_settings.models import (
    ConnectionTestSystemModelMaterial,
    LockedSystemModelMaterial,
)
from app.system_settings.secrets import model_secret_recipient
from app.system_settings.validation import (
    ModelSettingsInvalid,
    provider_api_key_required,
    provider_class_path,
    validate_materialized_model_settings,
    validate_model_settings,
)
from deerflow.config.model_config import ModelConfig
from deerflow.secrets import (
    SecretEnvelope,
    SecretKey,
    SecretKeyInvalid,
    SecretMaterializationFailed,
)


class SystemModelMaterializationUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("System model materialization unavailable")


def _payload_checksum(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _material_payload(
    material: LockedSystemModelMaterial,
) -> tuple[dict[str, object], str, uuid.UUID | None, str | None]:
    model = material.model
    execution = material.execution
    if execution is None:
        if model.status != "active":
            raise ValueError
        payload = {
            "schema_version": 1,
            "model_config_id": str(uuid.UUID(str(model.id))),
            "provider_adapter": model.provider_adapter,
            "provider_model": model.provider_model,
            "settings": dict(model.settings),
            "supports_thinking": model.supports_thinking,
            "supports_reasoning_effort": model.supports_reasoning_effort,
            "supports_vision": model.supports_vision,
        }
        checksum = model.payload_checksum
        generation_id = model.current_secret_generation_id
        generation_digest = material.secret_generation.envelope_digest if material.secret_generation is not None else None
    else:
        if execution.model_config_id != model.id:
            raise ValueError
        payload = dict(execution.provider_payload)
        checksum = execution.payload_checksum
        generation_id = execution.secret_generation_id
        generation_digest = execution.secret_envelope_digest
    if _payload_checksum(payload) != checksum:
        raise ValueError
    return payload, checksum, generation_id, generation_digest


class SystemModelExecutionAdapter:
    """Materialize only one exact active or admitted model payload."""

    def __init__(self, *, secret_key: SecretKey | None = None) -> None:
        self._secret_key = secret_key

    def materialize(self, material: LockedSystemModelMaterial) -> ModelConfig:
        try:
            if not isinstance(material, LockedSystemModelMaterial):
                raise ValueError
            payload, checksum, generation_id, generation_digest = _material_payload(material)
            model_config_id = uuid.UUID(str(payload["model_config_id"]))
            provider_adapter = payload["provider_adapter"]
            provider_model = payload["provider_model"]
            settings_value = payload["settings"]
            supports_thinking = payload["supports_thinking"]
            supports_reasoning_effort = payload["supports_reasoning_effort"]
            supports_vision = payload["supports_vision"]
            if (
                type(provider_adapter) is not str
                or type(provider_model) is not str
                or not isinstance(settings_value, Mapping)
                or type(supports_thinking) is not bool
                or type(supports_reasoning_effort) is not bool
                or type(supports_vision) is not bool
            ):
                raise ValueError
            settings = validate_materialized_model_settings(
                settings_value,
                provider_adapter=provider_adapter,
            )
            api_key_required = provider_api_key_required(provider_adapter)
            if api_key_required != (generation_id is not None):
                raise ValueError
            api_key: SecretStr | None = None
            generation = material.secret_generation
            if generation_id is not None:
                if generation is None or generation.id != generation_id or generation.model_config_id != model_config_id or generation.envelope_digest != generation_digest:
                    raise ValueError
                plaintext = SecretEnvelope(
                    nonce=bytes(generation.nonce),
                    ciphertext=bytes(generation.ciphertext),
                ).materialize(
                    recipient=model_secret_recipient(
                        model_config_id,
                        provider_adapter,
                        settings,
                    ),
                    key=self._secret_key or SecretKey.from_environment(),
                )
                value = plaintext.decode("utf-8")
                if not value:
                    raise ValueError
                api_key = SecretStr(value)
            elif generation_digest is not None or generation is not None:
                raise ValueError

            kwargs: dict[str, object] = {
                **settings,
                "name": str(model_config_id),
                "display_name": material.model.display_name,
                "description": "",
                "use": provider_class_path(provider_adapter),
                "model": provider_model,
                "supports_thinking": supports_thinking,
                "supports_reasoning_effort": supports_reasoning_effort,
                "supports_vision": supports_vision,
            }
            if api_key is not None:
                kwargs["api_key"] = api_key
            result = ModelConfig.model_validate(kwargs)
            result._system_model_config_id = model_config_id
            result._system_model_payload_checksum = checksum
            result._system_model_secret_generation_id = generation_id
            result._system_model_secret_envelope_digest = generation_digest
            result._system_provider_adapter = provider_adapter
            return result
        except (
            AttributeError,
            KeyError,
            ModelSettingsInvalid,
            SecretKeyInvalid,
            SecretMaterializationFailed,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            raise SystemModelMaterializationUnavailable() from None

    def materialize_connection_test(
        self,
        material: ConnectionTestSystemModelMaterial,
    ) -> ModelConfig:
        try:
            if not isinstance(material, ConnectionTestSystemModelMaterial):
                raise ValueError
            command = material.command
            settings = validate_model_settings(
                command.settings,
                provider_adapter=command.provider_adapter,
            )
            kwargs: dict[str, object] = {
                **settings,
                "name": "model-connection-test",
                "display_name": "Model connection test",
                "description": "",
                "use": provider_class_path(command.provider_adapter),
                "model": command.provider_model,
                "supports_thinking": False,
                "supports_reasoning_effort": False,
                "supports_vision": command.supports_vision,
            }
            if provider_api_key_required(command.provider_adapter):
                if not command.api_key:
                    raise ValueError
                kwargs["api_key"] = SecretStr(command.api_key)
            result = ModelConfig.model_validate(kwargs)
            result._system_provider_adapter = command.provider_adapter
            return result
        except (AttributeError, ModelSettingsInvalid, TypeError, ValueError):
            raise SystemModelMaterializationUnavailable() from None


__all__ = [
    "SystemModelExecutionAdapter",
    "SystemModelMaterializationUnavailable",
]
