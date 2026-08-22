from __future__ import annotations

import os
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from deerflow.secrets.key import SecretKey, _material_for

_NONCE_BYTES = 12
_TAG_BYTES = 16
_MAX_PLAINTEXT_BYTES = 64 * 1024
_MAX_RECIPIENT_BYTES = 1024
_AAD_PREFIX = b"actweave-configuration-secret:v1:"


class SecretProtectionFailed(Exception):
    """Stable, secret-free failure raised when protection cannot complete."""

    def __init__(self) -> None:
        super().__init__("Configuration secret protection failed")


class SecretMaterializationFailed(Exception):
    """Stable, secret-free failure for an unreadable envelope."""

    def __init__(self) -> None:
        super().__init__("Configuration secret materialization failed")


def _additional_authenticated_data(recipient: str) -> bytes:
    if not isinstance(recipient, str) or not recipient:
        raise ValueError
    encoded = recipient.encode("utf-8")
    if len(encoded) > _MAX_RECIPIENT_BYTES:
        raise ValueError
    return _AAD_PREFIX + encoded


@dataclass(frozen=True)
class SecretEnvelope:
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)

    @classmethod
    def protect(
        cls,
        plaintext: bytes,
        *,
        recipient: str,
        key: SecretKey,
    ) -> SecretEnvelope:
        try:
            if type(plaintext) is not bytes or not plaintext:
                raise ValueError
            if len(plaintext) > _MAX_PLAINTEXT_BYTES:
                raise ValueError
            nonce = os.urandom(_NONCE_BYTES)
            if len(nonce) != _NONCE_BYTES:
                raise ValueError
            ciphertext = AESGCM(_material_for(key)).encrypt(
                nonce,
                plaintext,
                _additional_authenticated_data(recipient),
            )
            return cls(nonce=nonce, ciphertext=ciphertext)
        except Exception:
            raise SecretProtectionFailed from None

    def materialize(
        self,
        *,
        recipient: str,
        key: SecretKey,
    ) -> bytes:
        try:
            if type(self.nonce) is not bytes or len(self.nonce) != _NONCE_BYTES:
                raise ValueError
            if type(self.ciphertext) is not bytes or len(self.ciphertext) < _TAG_BYTES:
                raise ValueError
            return AESGCM(_material_for(key)).decrypt(
                self.nonce,
                self.ciphertext,
                _additional_authenticated_data(recipient),
            )
        except Exception:
            raise SecretMaterializationFailed from None
