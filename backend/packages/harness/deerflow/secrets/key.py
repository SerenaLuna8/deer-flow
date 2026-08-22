from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

_SECRET_KEY_ENV = "ACT_WEAVE_SECRET_KEY"
_SECRET_KEY_BYTES = 32


class SecretKeyInvalid(Exception):
    """Stable, secret-free failure for invalid master-key configuration."""

    def __init__(self) -> None:
        super().__init__(f"{_SECRET_KEY_ENV} is missing or invalid")


@dataclass(frozen=True)
class SecretKey:
    _material: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self._material) is not bytes or len(self._material) != _SECRET_KEY_BYTES:
            raise SecretKeyInvalid

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> SecretKey:
        source = os.environ if environ is None else environ
        encoded = source.get(_SECRET_KEY_ENV)
        try:
            if not isinstance(encoded, str) or not encoded:
                raise ValueError
            material = base64.b64decode(encoded, validate=True)
            if len(material) != _SECRET_KEY_BYTES or base64.b64encode(material).decode("ascii") != encoded:
                raise ValueError
            return cls(material)
        except (binascii.Error, TypeError, ValueError):
            raise SecretKeyInvalid from None

    @property
    def byte_length(self) -> int:
        return len(self._material)


def _material_for(key: SecretKey) -> bytes:
    if not isinstance(key, SecretKey):
        raise SecretKeyInvalid
    return key._material
