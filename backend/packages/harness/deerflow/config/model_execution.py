"""Secret-free execution provenance for one admitted System Model call."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class SystemModelExecutionProvenance:
    """Secret-free identity retained after an exact model call finishes."""

    model_config_id: uuid.UUID
    payload_checksum: str
    secret_generation_id: uuid.UUID | None
    secret_envelope_digest: str | None

    def __post_init__(self) -> None:
        secret_group = (
            self.secret_generation_id is not None,
            self.secret_envelope_digest is not None,
        )
        if (
            not isinstance(self.model_config_id, uuid.UUID)
            or type(self.payload_checksum) is not str
            or _DIGEST.fullmatch(self.payload_checksum) is None
            or secret_group not in {(False, False), (True, True)}
            or (self.secret_generation_id is not None and not isinstance(self.secret_generation_id, uuid.UUID))
            or (self.secret_envelope_digest is not None and _DIGEST.fullmatch(self.secret_envelope_digest) is None)
        ):
            raise ValueError("System Model execution provenance is invalid")


@dataclass(frozen=True, slots=True)
class FrozenSystemModelExecution:
    model_config_id: uuid.UUID
    provider_payload: Mapping[str, object]
    payload_checksum: str
    secret_generation_id: uuid.UUID | None
    secret_envelope_digest: str | None

    def __post_init__(self) -> None:
        secret_group = (
            self.secret_generation_id is not None,
            self.secret_envelope_digest is not None,
        )
        if (
            not isinstance(self.model_config_id, uuid.UUID)
            or not isinstance(self.provider_payload, Mapping)
            or not self.provider_payload
            or type(self.payload_checksum) is not str
            or _DIGEST.fullmatch(self.payload_checksum) is None
            or secret_group not in {(False, False), (True, True)}
            or (self.secret_generation_id is not None and not isinstance(self.secret_generation_id, uuid.UUID))
            or (self.secret_envelope_digest is not None and _DIGEST.fullmatch(self.secret_envelope_digest) is None)
        ):
            raise ValueError("Frozen System Model execution is invalid")

    @property
    def provenance(self) -> SystemModelExecutionProvenance:
        return SystemModelExecutionProvenance(
            model_config_id=self.model_config_id,
            payload_checksum=self.payload_checksum,
            secret_generation_id=self.secret_generation_id,
            secret_envelope_digest=self.secret_envelope_digest,
        )


__all__ = ["FrozenSystemModelExecution", "SystemModelExecutionProvenance"]
