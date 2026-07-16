from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecoveryConfig(BaseModel):
    """Restart-required, non-secret paths and limits for backup recovery."""

    model_config = ConfigDict(extra="forbid")

    archive_chunk_bytes: int = Field(default=1_048_576, ge=65_536, le=67_108_864)
    archive_root: Path = Path(".deer-flow/backups")
    tombstone_journal_path: Path = Path(".deer-flow/recovery/tombstones.journal")
    journal_fsync_policy: Literal["always"] = "always"
    retention_days: int = Field(default=30, ge=1, le=3650)

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        if self.archive_root == self.tombstone_journal_path:
            raise ValueError("archive_root and tombstone_journal_path must differ")
        return self


__all__ = ["RecoveryConfig"]
