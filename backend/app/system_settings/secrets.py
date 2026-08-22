"""Recipient binding for System Model-owned API Keys."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from urllib.parse import urlsplit

from app.system_settings.validation import ModelSettingsInvalid
from deerflow.secrets import SecretEnvelope


def model_secret_recipient(
    model_config_id: uuid.UUID,
    provider_adapter: str,
    settings: Mapping[str, object],
) -> str:
    configured = settings.get("base_url")
    if not isinstance(configured, str):
        origin = f"{provider_adapter}:provider-default"
    else:
        parsed = urlsplit(configured)
        if not parsed.scheme or parsed.hostname is None:
            raise ModelSettingsInvalid
        port = f":{parsed.port}" if parsed.port is not None else ""
        origin = f"{provider_adapter}:{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"
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
