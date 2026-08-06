from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.models import AssetScope

_NONCE_BYTES = 12
_TAG_BYTES = 16
_MAX_PLAINTEXT_BYTES = 64 * 1024
_PAYLOAD_SCHEMA_VERSION = 1
_PAYLOAD_SECTIONS = frozenset({"env", "headers", "oauth", "query"})


class CredentialPayloadInvalid(Exception):
    """Stable, secret-free failure raised for invalid credential plaintext."""

    def __init__(self) -> None:
        super().__init__("Credential payload invalid")


class CredentialEncryptFailed(Exception):
    """Stable, secret-free failure raised when encryption cannot complete."""

    def __init__(self) -> None:
        super().__init__("Credential encryption failed")


class CredentialDecryptFailed(Exception):
    """Stable, secret-free failure for every unreadable credential envelope."""

    def __init__(self) -> None:
        super().__init__("Credential decryption failed")


class _CredentialContextInvalid(Exception):
    pass


@dataclass(frozen=True)
class EncryptedEnvelope:
    key_id: str
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


def _aad(version_id: uuid.UUID, scope: AssetScope | str, project_id: uuid.UUID | None) -> bytes:
    try:
        normalized_scope = AssetScope(scope)
    except (TypeError, ValueError):
        raise _CredentialContextInvalid from None
    if not isinstance(version_id, uuid.UUID):
        raise _CredentialContextInvalid
    if normalized_scope is AssetScope.SYSTEM:
        if project_id is not None:
            raise _CredentialContextInvalid
        owner = "system"
    else:
        if not isinstance(project_id, uuid.UUID):
            raise _CredentialContextInvalid
        owner = str(project_id)
    return f"deerflow-credential:v{_PAYLOAD_SCHEMA_VERSION}:{version_id}:{normalized_scope.value}:{owner}".encode("ascii")


def _validate_json_value(value: object) -> None:
    if value is None or type(value) in {str, bool, int, float}:
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError
            _validate_json_value(item)
        return
    raise ValueError


def _canonical_plaintext(payload: object) -> tuple[dict[str, Any], bytes]:
    try:
        if not isinstance(payload, Mapping) or not payload:
            raise ValueError
        normalized = dict(payload)
        if not set(normalized).issubset(_PAYLOAD_SECTIONS):
            raise ValueError
        for section, values in normalized.items():
            if section not in _PAYLOAD_SECTIONS or not isinstance(values, Mapping):
                raise ValueError
            _validate_json_value(values)
        plaintext = json.dumps(
            normalized,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(plaintext) > _MAX_PLAINTEXT_BYTES:
            raise ValueError
    except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError):
        raise CredentialPayloadInvalid() from None
    return normalized, plaintext


def encrypt_credential_payload(
    payload: object,
    scope: AssetScope | str,
    project_id: uuid.UUID | None,
    version_id: uuid.UUID,
    keyring: CredentialKeyring,
) -> EncryptedEnvelope:
    _, plaintext = _canonical_plaintext(payload)
    try:
        aad = _aad(version_id, scope, project_id)
    except _CredentialContextInvalid:
        raise CredentialPayloadInvalid() from None
    try:
        nonce = os.urandom(_NONCE_BYTES)
        if len(nonce) != _NONCE_BYTES:
            raise ValueError
        ciphertext = AESGCM(keyring.active_key).encrypt(nonce, plaintext, aad)
        return EncryptedEnvelope(
            key_id=keyring.active_key_id,
            nonce=nonce,
            ciphertext=ciphertext,
        )
    except Exception:
        raise CredentialEncryptFailed() from None


def decrypt_credential_payload(
    envelope: EncryptedEnvelope,
    scope: AssetScope | str,
    project_id: uuid.UUID | None,
    version_id: uuid.UUID,
    keyring: CredentialKeyring,
) -> dict[str, Any]:
    try:
        if not isinstance(envelope, EncryptedEnvelope):
            raise ValueError
        if not isinstance(envelope.key_id, str) or not envelope.key_id:
            raise ValueError
        if type(envelope.nonce) is not bytes or len(envelope.nonce) != _NONCE_BYTES:
            raise ValueError
        if type(envelope.ciphertext) is not bytes or len(envelope.ciphertext) < _TAG_BYTES:
            raise ValueError
        key = keyring.key_for(envelope.key_id)
        plaintext = AESGCM(key).decrypt(
            envelope.nonce,
            envelope.ciphertext,
            _aad(version_id, scope, project_id),
        )
        payload = json.loads(plaintext.decode("utf-8"))
        normalized, canonical_plaintext = _canonical_plaintext(payload)
        if plaintext != canonical_plaintext:
            raise ValueError
        return normalized
    except Exception:
        raise CredentialDecryptFailed() from None
