from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from app.quotas.models import QuotaSourceRef
from deerflow.persistence.jobs.sql import JobOwnerRef

_ACTIVE_KEY_ID_ENV = "DEER_FLOW_AUDIT_ACTIVE_KEY_ID"
_KEYRING_JSON_ENV = "DEER_FLOW_AUDIT_KEYRING_JSON"
_KEY_BYTES = 32
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_JOB_OWNER_DOMAIN = b"deerflow.m6.job-owner-ref.v1\x00"
_QUOTA_SOURCE_DOMAIN = b"deerflow.m6.quota-source-ref-hmac.v1\x00"
_AUDIT_TARGET_DOMAIN = b"deerflow.m6.audit-target-ref.v1\x00"
_AUDIT_REQUEST_DOMAIN = b"deerflow.m6.audit-request-ref.v1\x00"
_CHANNEL_EXTERNAL_DOMAIN = b"deerflow.channel.external-ref.v1\x00"
_AUDIT_TARGET_KIND = re.compile(r"[a-z][a-z0-9_]{0,31}")
_CHANNEL_EXTERNAL_KIND = re.compile(r"(?:group|account|topic)")
_CHANNEL_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class AuditHmacKeyringInvalid(Exception):
    """Secret-free failure for an invalid deployment audit HMAC keyring."""

    def __init__(self) -> None:
        super().__init__("Audit HMAC keyring configuration invalid")


@dataclass(frozen=True, slots=True)
class AuditTargetRef:
    key_id: str
    hmac_hex: str

    def __post_init__(self) -> None:
        if _KEY_ID.fullmatch(self.key_id) is None or re.fullmatch(r"[0-9a-f]{64}", self.hmac_hex) is None:
            raise ValueError("audit target reference is invalid")


@dataclass(frozen=True, slots=True)
class AuditRequestRef:
    hmac_hex: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.hmac_hex) is None:
            raise ValueError("audit request reference is invalid")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


@dataclass(frozen=True)
class AuditHmacKeyring:
    active_key_id: str
    _keys: Mapping[str, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        try:
            keys = dict(self._keys)
            if _KEY_ID.fullmatch(self.active_key_id) is None or self.active_key_id not in keys:
                raise ValueError
            if not keys:
                raise ValueError
            for key_id, key in keys.items():
                if _KEY_ID.fullmatch(key_id) is None or type(key) is not bytes or len(key) != _KEY_BYTES:
                    raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise AuditHmacKeyringInvalid() from None
        object.__setattr__(self, "_keys", MappingProxyType(keys))

    @classmethod
    def from_environment(cls) -> AuditHmacKeyring:
        active_key_id = os.environ.get(_ACTIVE_KEY_ID_ENV)
        raw_keyring = os.environ.get(_KEYRING_JSON_ENV)
        try:
            if active_key_id is None or raw_keyring is None:
                raise ValueError
            encoded_keys = json.loads(
                raw_keyring,
                object_pairs_hook=_unique_object,
            )
            if not isinstance(encoded_keys, dict) or not encoded_keys:
                raise ValueError
            keys: dict[str, bytes] = {}
            for key_id, encoded_key in encoded_keys.items():
                if not isinstance(key_id, str) or not isinstance(encoded_key, str):
                    raise ValueError
                key = base64.b64decode(encoded_key, validate=True)
                if len(key) != _KEY_BYTES or base64.b64encode(key).decode("ascii") != encoded_key:
                    raise ValueError
                keys[key_id] = key
            return cls(active_key_id=active_key_id, _keys=keys)
        except (
            binascii.Error,
            json.JSONDecodeError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            raise AuditHmacKeyringInvalid() from None

    def job_owner_ref(self, owner_user_id: str) -> JobOwnerRef:
        try:
            owner = uuid.UUID(owner_user_id)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("job owner reference requires a UUID") from None
        digest = hmac.new(
            self._keys[self.active_key_id],
            _JOB_OWNER_DOMAIN + owner.bytes,
            hashlib.sha256,
        ).hexdigest()
        return JobOwnerRef(
            key_id=self.active_key_id,
            hmac_hex=digest,
        )

    def quota_source_ref(self, payload: bytes) -> QuotaSourceRef:
        if type(payload) is not bytes or not payload:
            raise ValueError("quota source reference requires bytes")
        digest = hmac.new(
            self._keys[self.active_key_id],
            _QUOTA_SOURCE_DOMAIN + payload,
            hashlib.sha256,
        ).hexdigest()
        return QuotaSourceRef(
            key_id=self.active_key_id,
            hmac_hex=digest,
        )

    def quota_source_refs(self, payload: bytes) -> tuple[QuotaSourceRef, ...]:
        """Return the active ref first, then every retained rotation ref."""

        if type(payload) is not bytes or not payload:
            raise ValueError("quota source reference requires bytes")
        key_ids = (self.active_key_id, *sorted(set(self._keys) - {self.active_key_id}))
        return tuple(
            QuotaSourceRef(
                key_id=key_id,
                hmac_hex=hmac.new(
                    self._keys[key_id],
                    _QUOTA_SOURCE_DOMAIN + payload,
                    hashlib.sha256,
                ).hexdigest(),
            )
            for key_id in key_ids
        )

    def audit_target_refs(
        self,
        target_kind: str,
        authority_id: uuid.UUID,
    ) -> tuple[AuditTargetRef, ...]:
        """Return active and retained target refs without preserving raw authority."""

        if type(target_kind) is not str or _AUDIT_TARGET_KIND.fullmatch(target_kind) is None or type(authority_id) is not uuid.UUID:
            raise ValueError("audit target reference requires typed authority")
        kind = target_kind.encode("ascii")
        payload = _AUDIT_TARGET_DOMAIN + len(kind).to_bytes(2, "big") + kind + authority_id.bytes
        key_ids = (self.active_key_id, *sorted(set(self._keys) - {self.active_key_id}))
        return tuple(
            AuditTargetRef(
                key_id=key_id,
                hmac_hex=hmac.new(
                    self._keys[key_id],
                    payload,
                    hashlib.sha256,
                ).hexdigest(),
            )
            for key_id in key_ids
        )

    def audit_target_ref(
        self,
        target_kind: str,
        authority_id: uuid.UUID,
    ) -> AuditTargetRef:
        return self.audit_target_refs(target_kind, authority_id)[0]

    def audit_request_ref(self, request_id: str) -> AuditRequestRef:
        """Return a privacy-safe correlation ref for a normalized trace ID."""

        if type(request_id) is not str or not 1 <= len(request_id) <= 512 or request_id != request_id.strip() or any(ord(character) < 32 or ord(character) > 126 for character in request_id):
            raise ValueError("audit request reference requires a normalized trace ID")
        digest = hmac.new(
            self._keys[self.active_key_id],
            _AUDIT_REQUEST_DOMAIN + request_id.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return AuditRequestRef(hmac_hex=digest)

    @staticmethod
    def _channel_external_payload(
        identity_kind: str,
        provider: str,
        instance_id: uuid.UUID,
        external_id: str,
    ) -> bytes:
        if (
            type(identity_kind) is not str
            or _CHANNEL_EXTERNAL_KIND.fullmatch(identity_kind) is None
            or type(provider) is not str
            or _CHANNEL_PROVIDER.fullmatch(provider) is None
            or type(instance_id) is not uuid.UUID
            or type(external_id) is not str
            or not external_id
            or external_id != external_id.strip()
        ):
            raise ValueError("channel external reference requires normalized identity")
        encoded_provider = provider.encode("ascii")
        encoded_kind = identity_kind.encode("ascii")
        encoded_external_id = external_id.encode("utf-8")
        if len(encoded_external_id) > 512:
            raise ValueError("channel external reference identity is too long")
        return b"".join(
            (
                _CHANNEL_EXTERNAL_DOMAIN,
                len(encoded_kind).to_bytes(1, "big"),
                encoded_kind,
                len(encoded_provider).to_bytes(1, "big"),
                encoded_provider,
                instance_id.bytes,
                len(encoded_external_id).to_bytes(2, "big"),
                encoded_external_id,
            )
        )

    def channel_external_refs(
        self,
        identity_kind: str,
        provider: str,
        instance_id: uuid.UUID,
        external_id: str,
    ) -> tuple[str, ...]:
        """Return active-first irreversible refs for every retained HMAC key.

        The raw provider identifier remains transient in the channel adapter.
        Length prefixes keep the payload unambiguous and the dedicated domain
        prevents a group/account reference from colliding with audit or quota
        references made with the same retained keyring.
        """

        payload = self._channel_external_payload(
            identity_kind,
            provider,
            instance_id,
            external_id,
        )
        key_ids = (
            self.active_key_id,
            *sorted(set(self._keys) - {self.active_key_id}),
        )
        return tuple(hmac.new(self._keys[key_id], payload, hashlib.sha256).hexdigest() for key_id in key_ids)

    def channel_external_ref(
        self,
        identity_kind: str,
        provider: str,
        instance_id: uuid.UUID,
        external_id: str,
    ) -> str:
        return self.channel_external_refs(
            identity_kind,
            provider,
            instance_id,
            external_id,
        )[0]

    def __call__(self, payload: bytes) -> QuotaSourceRef:
        return self.quota_source_ref(payload)
