from __future__ import annotations

import dataclasses
import json
import uuid
from base64 import b64encode

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.shared_assets.crypto import (
    CredentialDecryptFailed,
    CredentialEncryptFailed,
    CredentialPayloadInvalid,
    EncryptedEnvelope,
    decrypt_credential_payload,
    encrypt_credential_payload,
)
from app.shared_assets.keyring import CredentialKeyring, CredentialKeyringInvalid
from app.shared_assets.models import AssetScope

PROJECT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
OTHER_PROJECT_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")
VERSION_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
OTHER_VERSION_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")
PAYLOAD = {
    "env": {"API_TOKEN": "token-value"},
    "headers": {"Authorization": "Bearer header-secret"},
    "oauth": {"client_secret": "oauth-secret", "refresh_token": "refresh-secret"},
}


def _configure_keyring(monkeypatch: pytest.MonkeyPatch, *, key_id: str = "k1", key: bytes = b"1" * 32) -> str:
    encoded_key = b64encode(key).decode("ascii")
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", key_id)
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_KEYRING_JSON", json.dumps({key_id: encoded_key}))
    return encoded_key


def test_encrypts_with_12_byte_nonce_and_bound_aad(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()

    envelope = encrypt_credential_payload(PAYLOAD, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring)

    assert envelope.key_id == "k1"
    assert len(envelope.nonce) == 12
    assert len(envelope.ciphertext) > 16
    assert decrypt_credential_payload(envelope, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring) == PAYLOAD


def test_accepts_uuid_subclasses_returned_by_database_drivers(monkeypatch: pytest.MonkeyPatch) -> None:
    class DriverUUID(uuid.UUID):
        pass

    _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()
    project_id = DriverUUID(str(PROJECT_ID))
    version_id = DriverUUID(str(VERSION_ID))

    envelope = encrypt_credential_payload(
        PAYLOAD,
        AssetScope.PROJECT,
        project_id,
        version_id,
        keyring,
    )

    assert (
        decrypt_credential_payload(
            envelope,
            AssetScope.PROJECT,
            project_id,
            version_id,
            keyring,
        )
        == PAYLOAD
    )


@pytest.mark.parametrize(
    ("scope", "project_id", "version_id"),
    [
        (AssetScope.SYSTEM, None, VERSION_ID),
        (AssetScope.PROJECT, OTHER_PROJECT_ID, VERSION_ID),
        (AssetScope.PROJECT, PROJECT_ID, OTHER_VERSION_ID),
    ],
)
def test_tamper_and_wrong_aad_fail_without_secret_in_error(
    monkeypatch: pytest.MonkeyPatch,
    scope: AssetScope,
    project_id: uuid.UUID | None,
    version_id: uuid.UUID,
) -> None:
    _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()
    envelope = encrypt_credential_payload(PAYLOAD, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring)
    tampered = dataclasses.replace(
        envelope,
        ciphertext=envelope.ciphertext[:-1] + bytes([envelope.ciphertext[-1] ^ 1]),
    )
    assert tampered.ciphertext != envelope.ciphertext

    attempts = (
        (envelope, scope, project_id, version_id),
        (tampered, AssetScope.PROJECT, PROJECT_ID, VERSION_ID),
    )
    for candidate, candidate_scope, candidate_project_id, candidate_version_id in attempts:
        with pytest.raises(CredentialDecryptFailed) as error:
            decrypt_credential_payload(
                candidate,
                candidate_scope,
                candidate_project_id,
                candidate_version_id,
                keyring,
            )
        assert str(error.value) == "Credential decryption failed"
        assert error.value.__dict__ == {}
        assert "token-value" not in str(error.value)


def test_unknown_key_id_and_malformed_envelope_fail_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()
    envelope = encrypt_credential_payload(PAYLOAD, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring)
    candidates = (
        dataclasses.replace(envelope, key_id="unknown"),
        dataclasses.replace(envelope, nonce=b"short"),
        dataclasses.replace(envelope, ciphertext=b"too-short"),
    )

    for candidate in candidates:
        with pytest.raises(CredentialDecryptFailed, match="^Credential decryption failed$"):
            decrypt_credential_payload(candidate, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring)


def test_decrypted_payload_schema_failure_is_indistinguishable(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()
    nonce = b"n" * 12
    aad = f"deerflow-credential:v1:{VERSION_ID}:project:{PROJECT_ID}".encode("ascii")
    ciphertext = AESGCM(keyring.active_key).encrypt(
        nonce,
        b'{"unsupported":{"token":"token-value"}}',
        aad,
    )
    envelope = EncryptedEnvelope(key_id="k1", nonce=nonce, ciphertext=ciphertext)

    with pytest.raises(CredentialDecryptFailed) as error:
        decrypt_credential_payload(envelope, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring)

    assert str(error.value) == "Credential decryption failed"
    assert "token-value" not in str(error.value)


@pytest.mark.parametrize(
    "keyring_json",
    [
        "not-json",
        "[]",
        json.dumps({"k1": "not-base64"}),
        json.dumps({"k1": b64encode(b"short").decode("ascii")}),
        json.dumps({"": b64encode(b"1" * 32).decode("ascii")}),
    ],
)
def test_invalid_keyring_environment_fails_safely_without_logging_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    keyring_json: str,
) -> None:
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "k1")
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_KEYRING_JSON", keyring_json)

    with pytest.raises(CredentialKeyringInvalid) as error:
        CredentialKeyring.from_environment()

    assert str(error.value) == "Credential keyring configuration invalid"
    assert error.value.__dict__ == {}
    assert keyring_json not in caplog.text


def test_keyring_rejects_missing_or_unknown_active_key(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded_key = b64encode(b"1" * 32).decode("ascii")
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_KEYRING_JSON", json.dumps({"k1": encoded_key}))
    monkeypatch.delenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", raising=False)

    with pytest.raises(CredentialKeyringInvalid, match="^Credential keyring configuration invalid$"):
        CredentialKeyring.from_environment()

    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "unknown")
    with pytest.raises(CredentialKeyringInvalid, match="^Credential keyring configuration invalid$"):
        CredentialKeyring.from_environment()


def test_keyring_rejects_noncanonical_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded_key = b64encode(b"1" * 32).decode("ascii")
    noncanonical_key = f"{encoded_key[:-2]}F="
    assert noncanonical_key != encoded_key

    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "k1")
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_KEYRING_JSON", json.dumps({"k1": noncanonical_key}))

    with pytest.raises(CredentialKeyringInvalid, match="^Credential keyring configuration invalid$"):
        CredentialKeyring.from_environment()


def test_keyring_rejects_duplicate_key_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded_key = b64encode(b"1" * 32).decode("ascii")
    duplicate_keyring_json = f'{{"k1":"{encoded_key}","k1":"{encoded_key}"}}'
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "k1")
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_KEYRING_JSON", duplicate_keyring_json)

    with pytest.raises(CredentialKeyringInvalid, match="^Credential keyring configuration invalid$"):
        CredentialKeyring.from_environment()


def test_keyring_normalizes_deep_json_recursion_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_value = "deep-secret-value"
    raw_keyring = '{"k1":' + "[" * 10_000 + f'"{secret_value}"' + "]" * 10_000 + "}"
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID", "k1")
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_KEYRING_JSON", raw_keyring)

    with pytest.raises(CredentialKeyringInvalid) as error:
        CredentialKeyring.from_environment()

    assert str(error.value) == "Credential keyring configuration invalid"
    assert error.value.__dict__ == {}
    assert secret_value not in str(error.value)
    assert raw_keyring not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"command": {"token": "token-value"}},
        {"env": "token-value"},
        {"headers": ["token-value"]},
        {"oauth": None},
        {"env": {"TOKEN": object()}},
        [],
    ],
)
def test_rejects_payload_outside_supported_credential_sections(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()

    with pytest.raises(CredentialPayloadInvalid) as error:
        encrypt_credential_payload(payload, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring)

    assert str(error.value) == "Credential payload invalid"
    assert error.value.__dict__ == {}
    assert "token-value" not in str(error.value)


def test_rejects_plaintext_over_64_kib(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()
    payload = {"env": {"TOKEN": "x" * (64 * 1024)}}

    with pytest.raises(CredentialPayloadInvalid, match="^Credential payload invalid$"):
        encrypt_credential_payload(payload, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring)


def test_encryption_runtime_failure_is_stable_and_secret_free(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()

    def _fail_nonce(_size: int) -> bytes:
        raise OSError("token-value")

    monkeypatch.setattr("app.shared_assets.crypto.os.urandom", _fail_nonce)

    with pytest.raises(CredentialEncryptFailed) as error:
        encrypt_credential_payload(PAYLOAD, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring)

    assert str(error.value) == "Credential encryption failed"
    assert error.value.__dict__ == {}
    assert "token-value" not in str(error.value)


def test_canonical_json_produces_identical_ciphertext_for_key_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()
    monkeypatch.setattr("app.shared_assets.crypto.os.urandom", lambda size: b"n" * size)
    first = {"headers": {"Z": "last", "A": "first"}, "env": {"B": "2", "A": "1"}}
    second = {"env": {"A": "1", "B": "2"}, "headers": {"A": "first", "Z": "last"}}

    first_envelope = encrypt_credential_payload(first, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring)
    second_envelope = encrypt_credential_payload(second, AssetScope.PROJECT, PROJECT_ID, VERSION_ID, keyring)

    assert first_envelope.ciphertext == second_envelope.ciphertext


def test_secret_bearing_dataclass_repr_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded_key = _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()
    envelope = EncryptedEnvelope(key_id="k1", nonce=b"nonce-secret", ciphertext=b"ciphertext-secret")

    assert encoded_key not in repr(keyring)
    assert repr(b"1" * 32) not in repr(keyring)
    assert "nonce-secret" not in repr(envelope)
    assert "ciphertext-secret" not in repr(envelope)


def test_scope_owner_shape_is_validated_before_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_keyring(monkeypatch)
    keyring = CredentialKeyring.from_environment()

    with pytest.raises(CredentialPayloadInvalid, match="^Credential payload invalid$"):
        encrypt_credential_payload(PAYLOAD, AssetScope.PROJECT, None, VERSION_ID, keyring)
    with pytest.raises(CredentialPayloadInvalid, match="^Credential payload invalid$"):
        encrypt_credential_payload(PAYLOAD, AssetScope.SYSTEM, PROJECT_ID, VERSION_ID, keyring)
