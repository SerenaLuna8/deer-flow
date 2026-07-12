from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.gateway.auth.invitation_claim import InvitationClaimSigner
from app.projects.invitation_models import InvitationClaim, ProjectInvitationInvalid

NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
SECRET = "test-auth-jwt-secret-at-least-32-bytes"
KEY_LABEL = b"deerflow:project-invitation-claim:key:v1\x00"
AAD = b"deerflow:project_invitation_claim:v1"


def _decode_payload(signed: str, secret: str = SECRET) -> dict:
    raw = base64.urlsafe_b64decode(signed + "=" * (-len(signed) % 4))
    nonce, ciphertext = raw[:12], raw[12:]
    key = hashlib.sha256(KEY_LABEL + secret.encode("utf-8")).digest()
    return json.loads(AESGCM(key).decrypt(nonce, ciphertext, AAD))


def test_claim_signer_uses_only_required_encrypted_claims_and_round_trips() -> None:
    claim = InvitationClaim(invitation_id=uuid.uuid4(), token_hash="a" * 64)
    signer = InvitationClaimSigner(secret=SECRET)

    signed = signer.issue(claim, NOW)
    payload = _decode_payload(signed)

    assert set(payload) == {"invitation_id", "token_hash", "iat", "exp"}
    assert payload["invitation_id"] == str(claim.invitation_id)
    assert payload["token_hash"] == claim.token_hash
    assert payload["iat"] == int(NOW.timestamp())
    assert payload["exp"] == int((NOW + timedelta(minutes=10)).timestamp())
    assert str(claim.invitation_id) not in signed
    assert claim.token_hash not in signed
    assert "." not in signed
    assert signer.verify(signed, NOW + timedelta(minutes=9, seconds=59)) == claim


@pytest.mark.parametrize("value", ["not-a-jwt", ""])
def test_claim_signer_rejects_malformed_cookie_without_leaking_reason(value: str) -> None:
    with pytest.raises(ProjectInvitationInvalid) as exc_info:
        InvitationClaimSigner(secret=SECRET).verify(value, NOW)
    assert exc_info.value.__dict__ == {}


def test_claim_signer_rejects_tampering_expiry_and_extra_payload_fields() -> None:
    claim = InvitationClaim(invitation_id=uuid.uuid4(), token_hash="b" * 64)
    signer = InvitationClaimSigner(secret=SECRET)
    signed = signer.issue(claim, NOW)

    with pytest.raises(ProjectInvitationInvalid):
        InvitationClaimSigner(secret="different-secret-at-least-32-bytes").verify(signed, NOW)
    with pytest.raises(ProjectInvitationInvalid):
        signer.verify(signed, NOW + timedelta(minutes=10))

    raw = base64.urlsafe_b64decode(signed + "=" * (-len(signed) % 4))
    tampered = bytearray(raw)
    tampered[-1] ^= 1
    with pytest.raises(ProjectInvitationInvalid):
        signer.verify(base64.urlsafe_b64encode(tampered).rstrip(b"=").decode(), NOW)

    issued_at = int(NOW.timestamp())
    nonce = b"0" * 12
    key = hashlib.sha256(KEY_LABEL + SECRET.encode("utf-8")).digest()
    extra_payload = {
        "invitation_id": str(claim.invitation_id),
        "token_hash": claim.token_hash,
        "iat": issued_at,
        "exp": issued_at + 600,
        "email": "member@example.com",
    }
    ciphertext = AESGCM(key).encrypt(
        nonce,
        json.dumps(extra_payload, separators=(",", ":"), sort_keys=True).encode(),
        AAD,
    )
    extra = base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=").decode()
    with pytest.raises(ProjectInvitationInvalid):
        signer.verify(extra, NOW)
