"""Secret-free execution payloads frozen by admitting domains."""

from __future__ import annotations

import uuid

from app.system_settings.models import (
    FrozenSystemModelExecution,
    LockedSystemModelMaterial,
)
from app.system_settings.validation import provider_api_key_required
from deerflow.config.model_config import ModelConfig
from deerflow.config.model_execution import SystemModelExecutionProvenance
from deerflow.persistence.system_settings import SystemModelConfigRow


def system_model_provider_payload(
    row: SystemModelConfigRow,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "model_config_id": str(uuid.UUID(str(row.id))),
        "provider_adapter": row.provider_adapter,
        "provider_model": row.provider_model,
        "settings": dict(row.settings),
        "supports_thinking": row.supports_thinking,
        "supports_reasoning_effort": row.supports_reasoning_effort,
        "supports_vision": row.supports_vision,
    }


def freeze_system_model_material(
    material: LockedSystemModelMaterial,
) -> FrozenSystemModelExecution:
    if not isinstance(material, LockedSystemModelMaterial):
        raise ValueError("System Model material is invalid")
    model = material.model
    generation = material.secret_generation
    if provider_api_key_required(model.provider_adapter) and generation is None:
        raise ValueError("System Model is secret-unready")
    if generation is not None and generation.model_config_id != model.id:
        raise ValueError("System Model secret owner is invalid")
    return FrozenSystemModelExecution(
        model_config_id=uuid.UUID(str(model.id)),
        provider_payload=system_model_provider_payload(model),
        payload_checksum=model.payload_checksum,
        secret_generation_id=(uuid.UUID(str(generation.id)) if generation is not None else None),
        secret_envelope_digest=(generation.envelope_digest if generation is not None else None),
    )


def model_execution_provenance(
    model: ModelConfig,
) -> SystemModelExecutionProvenance:
    """Read non-serializing execution provenance from a materialized model."""

    if not isinstance(model, ModelConfig):
        raise ValueError("Materialized System Model is invalid")
    return SystemModelExecutionProvenance(
        model_config_id=model._system_model_config_id,
        payload_checksum=model._system_model_payload_checksum,
        secret_generation_id=model._system_model_secret_generation_id,
        secret_envelope_digest=model._system_model_secret_envelope_digest,
    )


__all__ = [
    "freeze_system_model_material",
    "model_execution_provenance",
    "system_model_provider_payload",
]
