from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

_ACTIVE_KEY_ID_ENV = "DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID"
_KEYRING_JSON_ENV = "DEER_FLOW_CREDENTIAL_KEYRING_JSON"
_KEY_BYTES = 32
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}")


class CredentialKeyringInvalid(Exception):
    """Stable, secret-free failure raised for invalid keyring configuration."""

    def __init__(self) -> None:
        super().__init__("Credential keyring configuration invalid")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


@dataclass(frozen=True)
class CredentialKeyring:
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
            raise CredentialKeyringInvalid() from None
        object.__setattr__(self, "_keys", MappingProxyType(keys))

    @classmethod
    def from_environment(cls) -> CredentialKeyring:
        active_key_id = os.environ.get(_ACTIVE_KEY_ID_ENV)
        raw_keyring = os.environ.get(_KEYRING_JSON_ENV)
        try:
            if active_key_id is None or raw_keyring is None:
                raise ValueError
            encoded_keys = json.loads(raw_keyring, object_pairs_hook=_unique_object)
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
        except (binascii.Error, json.JSONDecodeError, TypeError, ValueError):
            raise CredentialKeyringInvalid() from None

    @property
    def active_key(self) -> bytes:
        return self._keys[self.active_key_id]

    def key_for(self, key_id: str) -> bytes:
        return self._keys[key_id]
