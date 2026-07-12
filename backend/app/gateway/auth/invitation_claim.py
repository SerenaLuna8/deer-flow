from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.gateway.auth.config import get_auth_config
from app.projects.invitation_models import InvitationClaim, ProjectInvitationInvalid

INVITATION_CLAIM_MAX_AGE = 600
INVITATION_CLAIM_COOKIE_NAME = "project_invitation_claim"
INVITATION_CLAIM_COOKIE_PATH = "/api/project-invitations"
_CLAIM_FIELDS = {"invitation_id", "token_hash", "iat", "exp"}
_TOKEN_HASH = re.compile(r"[0-9a-f]{64}")
_KEY_LABEL = b"deerflow:project-invitation-claim:key:v1\x00"
_AAD = b"deerflow:project_invitation_claim:v1"
_NONCE_BYTES = 12
_TAG_BYTES = 16


class InvitationClaimSigner:
    def __init__(self, *, secret: str | None = None):
        auth_secret = secret or get_auth_config().jwt_secret
        self._key = hashlib.sha256(_KEY_LABEL + auth_secret.encode("utf-8")).digest()

    def issue(self, claim: InvitationClaim, now: datetime) -> str:
        issued_at = int(now.timestamp())
        payload = json.dumps(
            {
                "invitation_id": str(claim.invitation_id),
                "token_hash": claim.token_hash,
                "iat": issued_at,
                "exp": issued_at + INVITATION_CLAIM_MAX_AGE,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(self._key).encrypt(nonce, payload, _AAD)
        return base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=").decode("ascii")

    def verify(self, cookie: str, now: datetime) -> InvitationClaim:
        try:
            if not isinstance(cookie, str) or not cookie:
                raise ValueError
            raw = base64.b64decode(
                cookie + "=" * (-len(cookie) % 4),
                altchars=b"-_",
                validate=True,
            )
            if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != cookie:
                raise ValueError
            if len(raw) <= _NONCE_BYTES + _TAG_BYTES:
                raise ValueError
            nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
            plaintext = AESGCM(self._key).decrypt(nonce, ciphertext, _AAD)
            payload = json.loads(plaintext.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            if set(payload) != _CLAIM_FIELDS:
                raise ValueError
            issued_at = payload["iat"]
            expires_at = payload["exp"]
            if type(issued_at) is not int or type(expires_at) is not int:
                raise ValueError
            now_timestamp = int(now.timestamp())
            if issued_at > now_timestamp or expires_at <= now_timestamp:
                raise ValueError
            if expires_at - issued_at != int(timedelta(minutes=10).total_seconds()):
                raise ValueError
            token_hash = payload["token_hash"]
            if not isinstance(token_hash, str) or _TOKEN_HASH.fullmatch(token_hash) is None:
                raise ValueError
            invitation_id = uuid.UUID(payload["invitation_id"])
        except (
            AttributeError,
            binascii.Error,
            InvalidTag,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ):
            raise ProjectInvitationInvalid() from None
        return InvitationClaim(invitation_id=invitation_id, token_hash=token_hash)
