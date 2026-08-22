from __future__ import annotations

import dataclasses
from base64 import b64encode

import pytest

from deerflow.secrets import (
    SecretEnvelope,
    SecretKey,
    SecretKeyInvalid,
    SecretMaterializationFailed,
)


def test_secret_key_loads_exact_32_byte_canonical_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", encoded)

    key = SecretKey.from_environment()

    assert key.byte_length == 32
    assert encoded not in repr(key)
    assert repr(b"k" * 32) not in repr(key)


@pytest.mark.parametrize(
    "configured",
    [
        None,
        "not-base64",
        b64encode(b"short").decode("ascii"),
        f"{b64encode(b'k' * 32).decode('ascii')[:-2]}F=",
    ],
)
def test_secret_key_rejects_missing_malformed_or_wrong_sized_values(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
) -> None:
    if configured is None:
        monkeypatch.delenv("ACT_WEAVE_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", configured)

    with pytest.raises(SecretKeyInvalid) as error:
        SecretKey.from_environment()

    assert str(error.value) == "ACT_WEAVE_SECRET_KEY is missing or invalid"
    assert error.value.__dict__ == {}
    if configured is not None:
        assert configured not in str(error.value)


def test_secret_envelope_round_trips_only_for_the_exact_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        b64encode(b"k" * 32).decode("ascii"),
    )
    key = SecretKey.from_environment()
    plaintext = b"domain-owned-secret"
    recipient = "model:10000000-0000-0000-0000-000000000001:api-key"

    envelope = SecretEnvelope.protect(plaintext, recipient=recipient, key=key)

    assert envelope.materialize(recipient=recipient, key=key) == plaintext
    with pytest.raises(
        SecretMaterializationFailed,
        match="^Configuration secret materialization failed$",
    ):
        envelope.materialize(recipient=f"{recipient}:other", key=key)


def test_secret_envelope_uses_fresh_nonce_and_rejects_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        b64encode(b"k" * 32).decode("ascii"),
    )
    key = SecretKey.from_environment()
    recipient = "skill:project:version:secret-name"

    first = SecretEnvelope.protect(b"same-value", recipient=recipient, key=key)
    second = SecretEnvelope.protect(b"same-value", recipient=recipient, key=key)

    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    tampered = dataclasses.replace(
        first,
        ciphertext=first.ciphertext[:-1] + bytes([first.ciphertext[-1] ^ 1]),
    )
    with pytest.raises(SecretMaterializationFailed) as error:
        tampered.materialize(recipient=recipient, key=key)
    assert str(error.value) == "Configuration secret materialization failed"
    assert plaintext_absent(error, "same-value")


def plaintext_absent(error: BaseException, value: str) -> bool:
    return value not in str(error) and value not in repr(error)


def test_secret_envelope_repr_redacts_protected_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        b64encode(b"k" * 32).decode("ascii"),
    )
    envelope = SecretEnvelope.protect(
        b"write-only-secret",
        recipient="channel:instance:provider-bundle",
        key=SecretKey.from_environment(),
    )

    assert "write-only-secret" not in repr(envelope)
    assert repr(envelope.nonce) not in repr(envelope)
    assert repr(envelope.ciphertext) not in repr(envelope)
