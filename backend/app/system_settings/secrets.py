"""Recipient binding for System Model-owned API Keys."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from urllib.parse import urlsplit

from app.system_settings.validation import (
    ModelSettingsInvalid,
    materialize_effective_model_settings,
)
from deerflow.secrets import SecretEnvelope


def model_secret_recipient(
    model_config_id: uuid.UUID,
    provider_adapter: str,
    settings: Mapping[str, object],
) -> str:
    configured = materialize_effective_model_settings(
        settings,
        provider_adapter=provider_adapter,
    ).get("base_url")
    if not isinstance(configured, str):
        raise ModelSettingsInvalid
    try:
        parsed = urlsplit(configured)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ModelSettingsInvalid from None
    if scheme not in {"http", "https"} or hostname is None:
        raise ModelSettingsInvalid
    if port is None:
        port = 443 if scheme == "https" else 80
    normalized_host = hostname.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    origin = f"{provider_adapter}:{scheme}://{normalized_host}:{port}"
    return f"system-model:{model_config_id}:api-key:{origin}"


def model_secret_envelope_digest(
    recipient: str,
    envelope: SecretEnvelope,
) -> str:
    digest = hashlib.sha256()
    digest.update(recipient.encode("utf-8"))
    digest.update(envelope.nonce)
    digest.update(envelope.ciphertext)
    return digest.hexdigest()


__all__ = ["model_secret_envelope_digest", "model_secret_recipient"]
