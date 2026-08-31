"""Recipient binding and envelope helpers for Model Provider API Keys.

The registry stores one inline nonce/ciphertext pair per Provider row. The
recipient binds the Provider UUID *and* the base URL origin, matching the
endpoint-bound posture of ``app.system_settings.secrets.model_secret_recipient``:
changing ``base_url`` therefore always requires resubmitting the API Key, and a
stored ciphertext can never be redirected to a different endpoint.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from deerflow.secrets import SecretEnvelope, SecretKey


class ModelProviderSecretInvalid(ValueError):
    """One indistinguishable failure for invalid recipient material."""

    def __init__(self) -> None:
        super().__init__("Model provider secret material invalid")


def provider_secret_recipient(provider_id: uuid.UUID, base_url: str) -> str:
    if not isinstance(provider_id, uuid.UUID) or type(base_url) is not str:
        raise ModelProviderSecretInvalid
    try:
        parsed = urlsplit(base_url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ModelProviderSecretInvalid from None
    if scheme not in {"http", "https"} or hostname is None:
        raise ModelProviderSecretInvalid
    if port is None:
        port = 443 if scheme == "https" else 80
    normalized_host = hostname.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    origin = f"{scheme}://{normalized_host}:{port}"
    return f"model-provider:{provider_id}:api-key:{origin}"


def protect_provider_api_key(
    *,
    provider_id: uuid.UUID,
    base_url: str,
    api_key: str,
    key: SecretKey,
) -> SecretEnvelope:
    if type(api_key) is not str or not api_key.strip():
        raise ModelProviderSecretInvalid
    return SecretEnvelope.protect(
        api_key.encode("utf-8"),
        recipient=provider_secret_recipient(provider_id, base_url),
        key=key,
    )


def materialize_provider_api_key(
    *,
    provider_id: uuid.UUID,
    base_url: str,
    nonce: bytes,
    ciphertext: bytes,
    key: SecretKey,
) -> str:
    envelope = SecretEnvelope(nonce=bytes(nonce), ciphertext=bytes(ciphertext))
    plaintext = envelope.materialize(
        recipient=provider_secret_recipient(provider_id, base_url),
        key=key,
    )
    return plaintext.decode("utf-8")


__all__ = [
    "ModelProviderSecretInvalid",
    "materialize_provider_api_key",
    "protect_provider_api_key",
    "provider_secret_recipient",
]
