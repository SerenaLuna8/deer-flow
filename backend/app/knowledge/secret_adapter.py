"""Host secret adapter backed by the shared ``SecretKey``/``SecretEnvelope``.

The Knowledge Package holds only opaque nonce/ciphertext pairs; recipients are
derived from the exact model-configuration id so envelopes cannot be replayed
across rows.
"""

from __future__ import annotations

from uuid import UUID

from actweave_knowledge import KnowledgeProtectedSecret

from deerflow.secrets import SecretEnvelope, SecretKey


def _recipient(configuration_id: UUID) -> str:
    return f"knowledge-model-configuration:{configuration_id}"


class EnvelopeKnowledgeSecretAdapter:
    """Production ``KnowledgeSecretPort`` implementation."""

    def __init__(self, key: SecretKey) -> None:
        self._key = key

    @classmethod
    def from_environment(cls) -> EnvelopeKnowledgeSecretAdapter:
        return cls(SecretKey.from_environment())

    def protect_api_key(self, configuration_id: UUID, api_key: str) -> KnowledgeProtectedSecret:
        envelope = SecretEnvelope.protect(
            api_key.encode("utf-8"),
            recipient=_recipient(configuration_id),
            key=self._key,
        )
        return KnowledgeProtectedSecret(nonce=envelope.nonce, ciphertext=envelope.ciphertext)

    def materialize_api_key(self, configuration_id: UUID, secret: KnowledgeProtectedSecret) -> str:
        envelope = SecretEnvelope(nonce=secret.nonce, ciphertext=secret.ciphertext)
        plaintext = envelope.materialize(recipient=_recipient(configuration_id), key=self._key)
        return plaintext.decode("utf-8")
