from __future__ import annotations

import base64
import json
import uuid

import pytest

from app.reliability.owner_refs import (
    AuditHmacKeyring,
    AuditHmacKeyringInvalid,
)


def test_audit_hmac_keyring_hashes_job_owner_with_active_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEER_FLOW_AUDIT_ACTIVE_KEY_ID", "audit-v1")
    monkeypatch.setenv(
        "DEER_FLOW_AUDIT_KEYRING_JSON",
        json.dumps(
            {
                "audit-v1": base64.b64encode(b"a" * 32).decode("ascii"),
                "audit-old": base64.b64encode(b"b" * 32).decode("ascii"),
            }
        ),
    )
    owner = str(uuid.uuid4())

    first = AuditHmacKeyring.from_environment().job_owner_ref(owner)
    second = AuditHmacKeyring.from_environment().job_owner_ref(owner)

    assert first == second
    assert first.key_id == "audit-v1"
    assert len(first.hmac_hex) == 64
    assert owner not in repr(first)


def test_audit_hmac_keyring_hashes_quota_source_with_separate_domain(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEER_FLOW_AUDIT_ACTIVE_KEY_ID", "audit-v1")
    monkeypatch.setenv(
        "DEER_FLOW_AUDIT_KEYRING_JSON",
        json.dumps(
            {
                "audit-v1": base64.b64encode(b"a" * 32).decode("ascii"),
            }
        ),
    )
    keyring = AuditHmacKeyring.from_environment()
    payload = b"server-authority-coordinate"

    first = keyring.quota_source_ref(payload)
    second = keyring.quota_source_ref(payload)

    assert first == second
    assert first.key_id == "audit-v1"
    assert len(first.hmac_hex) == 64
    assert payload.hex() not in first.hmac_hex
    assert first.hmac_hex != keyring.job_owner_ref(str(uuid.uuid4())).hmac_hex


@pytest.mark.parametrize(
    ("active", "keyring"),
    [
        (None, None),
        ("missing", {}),
        ("audit-v1", {"audit-v1": "not-base64"}),
        (
            "audit-v1",
            {"audit-v1": base64.b64encode(b"short").decode("ascii")},
        ),
        (
            "a" * 65,
            {"a" * 65: base64.b64encode(b"a" * 32).decode("ascii")},
        ),
    ],
)
def test_audit_hmac_keyring_fails_closed(
    monkeypatch,
    active,
    keyring,
) -> None:
    if active is None:
        monkeypatch.delenv("DEER_FLOW_AUDIT_ACTIVE_KEY_ID", raising=False)
    else:
        monkeypatch.setenv("DEER_FLOW_AUDIT_ACTIVE_KEY_ID", active)
    if keyring is None:
        monkeypatch.delenv("DEER_FLOW_AUDIT_KEYRING_JSON", raising=False)
    else:
        monkeypatch.setenv(
            "DEER_FLOW_AUDIT_KEYRING_JSON",
            json.dumps(keyring),
        )

    with pytest.raises(AuditHmacKeyringInvalid):
        AuditHmacKeyring.from_environment()
