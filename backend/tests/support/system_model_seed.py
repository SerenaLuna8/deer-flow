"""Schema V1 System Model fixture helpers for PostgreSQL tests."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping

from sqlalchemy import text

from deerflow.config.model_execution import FrozenSystemModelExecution


def system_model_payload(
    *,
    model_id: uuid.UUID,
    provider_adapter: str,
    provider_model: str,
    max_input_tokens: int = 64_000,
    settings: Mapping[str, object] | None,
    supports_thinking: bool,
    supports_reasoning_effort: bool,
    supports_vision: bool,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "model_config_id": str(model_id),
        "provider_adapter": provider_adapter,
        "provider_model": provider_model,
        "max_input_tokens": max_input_tokens,
        "settings": dict(settings or {}),
        "supports_thinking": supports_thinking,
        "supports_reasoning_effort": supports_reasoning_effort,
        "supports_vision": supports_vision,
    }


def system_model_payload_checksum(
    *,
    model_id: uuid.UUID,
    provider_adapter: str,
    provider_model: str,
    max_input_tokens: int = 64_000,
    settings: Mapping[str, object] | None,
    supports_thinking: bool,
    supports_reasoning_effort: bool,
    supports_vision: bool,
) -> str:
    payload = system_model_payload(
        model_id=model_id,
        provider_adapter=provider_adapter,
        provider_model=provider_model,
        max_input_tokens=max_input_tokens,
        settings=settings,
        supports_thinking=supports_thinking,
        supports_reasoning_effort=supports_reasoning_effort,
        supports_vision=supports_vision,
    )
    return hashlib.sha256(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


# Fixed identity for the shared test Provider row that satisfies the
# required ``system_model_configs.provider_id`` binding. The envelope bytes
# are shape-valid placeholders only; tests that must decrypt a Provider Key
# seed their own Provider with a real SecretEnvelope instead.
SEED_PROVIDER_ID = uuid.UUID("6f7d17e5-a2a6-5a0b-9c62-6c6f64656c00")
SEED_PROVIDER_NAME = "Seed Test Provider"
SEED_PROVIDER_BASE_URL = "https://provider.seed.invalid/v1"


async def seed_model_provider(
    executor: object,
    *,
    provider_id: uuid.UUID | None = None,
    name: str | None = None,
    base_url: str = SEED_PROVIDER_BASE_URL,
    request_timeout_seconds: int = 30,
    api_key_nonce: bytes = b"\x00" * 12,
    api_key_ciphertext: bytes = b"\x00" * 16,
) -> uuid.UUID:
    """Insert one Model Provider row (idempotent for the fixed seed identity)."""

    execute = getattr(executor, "execute", None)
    if execute is None:
        raise TypeError("executor must provide execute()")
    resolved_id = provider_id if provider_id is not None else SEED_PROVIDER_ID
    resolved_name = name if name is not None else (SEED_PROVIDER_NAME if provider_id is None else f"provider-{resolved_id.hex[:12]}")
    await execute(
        text(
            """INSERT INTO model_providers
            (id,name,base_url,request_timeout_seconds,api_key_nonce,api_key_ciphertext)
            VALUES (:id,:name,:base_url,:timeout,:nonce,:ciphertext)
            ON CONFLICT (id) DO NOTHING"""
        ),
        {
            "id": resolved_id,
            "name": resolved_name,
            "base_url": base_url,
            "timeout": request_timeout_seconds,
            "nonce": api_key_nonce,
            "ciphertext": api_key_ciphertext,
        },
    )
    return resolved_id


async def seed_system_model_config(
    executor: object,
    *,
    model_id: uuid.UUID,
    owner_user_id: str,
    display_name: str,
    provider_model: str,
    provider_id: uuid.UUID | None = None,
    provider_adapter: str = "vision_bridge_fake",
    max_input_tokens: int = 64_000,
    supports_thinking: bool = False,
    supports_reasoning_effort: bool = False,
    supports_vision: bool = False,
    settings: Mapping[str, object] | None = None,
) -> uuid.UUID:
    """Insert one secret-free stable Model configuration for a focused test."""

    execute = getattr(executor, "execute", None)
    if execute is None:
        raise TypeError("executor must provide execute()")
    if provider_id is None:
        provider_id = await seed_model_provider(executor)
    canonical_checksum = system_model_payload_checksum(
        model_id=model_id,
        provider_adapter=provider_adapter,
        provider_model=provider_model,
        max_input_tokens=max_input_tokens,
        settings=settings,
        supports_thinking=supports_thinking,
        supports_reasoning_effort=supports_reasoning_effort,
        supports_vision=supports_vision,
    )
    await execute(
        text(
            """INSERT INTO system_model_configs
            (id,display_name,status,provider_id,provider_adapter,provider_model,max_input_tokens,settings,
             supports_thinking,supports_reasoning_effort,supports_vision,
             payload_checksum,current_secret_generation_id,secret_revision,
             revision,created_by_user_id,updated_by_user_id)
            VALUES (:id,:display_name,'active',:provider_id,:provider_adapter,:provider_model,
                    :max_input_tokens,CAST(:settings AS jsonb),:supports_thinking,:supports_reasoning_effort,
                    :supports_vision,:payload_checksum,NULL,0,1,:owner,:owner)"""
        ),
        {
            "id": model_id,
            "display_name": display_name,
            "provider_id": provider_id,
            "provider_adapter": provider_adapter,
            "provider_model": provider_model,
            "max_input_tokens": max_input_tokens,
            "settings": json.dumps(dict(settings or {}), sort_keys=True),
            "supports_thinking": supports_thinking,
            "supports_reasoning_effort": supports_reasoning_effort,
            "supports_vision": supports_vision,
            "payload_checksum": canonical_checksum,
            "owner": owner_user_id,
        },
    )
    return model_id


def frozen_system_model_execution(
    *,
    model_id: uuid.UUID,
    provider_model: str,
    provider_adapter: str = "vision_bridge_fake",
    max_input_tokens: int = 64_000,
    supports_thinking: bool = False,
    supports_reasoning_effort: bool = False,
    supports_vision: bool = False,
) -> FrozenSystemModelExecution:
    """Build the secret-free immutable execution payload used by old Run fixtures."""

    payload = system_model_payload(
        model_id=model_id,
        provider_adapter=provider_adapter,
        provider_model=provider_model,
        max_input_tokens=max_input_tokens,
        settings=None,
        supports_thinking=supports_thinking,
        supports_reasoning_effort=supports_reasoning_effort,
        supports_vision=supports_vision,
    )
    canonical_checksum = system_model_payload_checksum(
        model_id=model_id,
        provider_adapter=provider_adapter,
        provider_model=provider_model,
        max_input_tokens=max_input_tokens,
        settings=None,
        supports_thinking=supports_thinking,
        supports_reasoning_effort=supports_reasoning_effort,
        supports_vision=supports_vision,
    )
    return FrozenSystemModelExecution(
        model_config_id=model_id,
        provider_payload=payload,
        payload_checksum=canonical_checksum,
        secret_generation_id=None,
        secret_envelope_digest=None,
    )


__all__ = [
    "SEED_PROVIDER_BASE_URL",
    "SEED_PROVIDER_ID",
    "SEED_PROVIDER_NAME",
    "frozen_system_model_execution",
    "seed_model_provider",
    "seed_system_model_config",
    "system_model_payload",
    "system_model_payload_checksum",
]
