from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

RunSkillSnapshotWriterMode = Literal["v4_reference", "legacy_v3"]

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class RunSkillSnapshotConfig(BaseModel):
    """Restart-required operator selection for the Run Skill writer."""

    model_config = ConfigDict(extra="forbid", strict=True)

    writer_mode: RunSkillSnapshotWriterMode = "v4_reference"
    expected_artifact_version: str | None = None
    expected_legacy_policy_digest: str | None = None

    @model_validator(mode="after")
    def validate_release_identity(self) -> Self:
        if self.writer_mode == "v4_reference":
            if self.expected_artifact_version is not None or self.expected_legacy_policy_digest is not None:
                raise ValueError(
                    "v4_reference does not accept legacy release identity",
                )
            return self
        if (
            not isinstance(self.expected_artifact_version, str)
            or not self.expected_artifact_version
            or len(self.expected_artifact_version) > 128
            or not isinstance(self.expected_legacy_policy_digest, str)
            or _DIGEST.fullmatch(self.expected_legacy_policy_digest) is None
        ):
            raise ValueError(
                "legacy_v3 requires an exact artifact version and policy digest",
            )
        return self


__all__ = ["RunSkillSnapshotConfig", "RunSkillSnapshotWriterMode"]
