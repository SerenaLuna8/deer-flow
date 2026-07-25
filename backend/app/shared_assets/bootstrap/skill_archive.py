"""Strict deterministic archive format for packaged multi-file system Skills."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.shared_assets.bootstrap.catalog import BootstrapCatalogError
from app.shared_assets.models import SkillArchiveFile

_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


class _ArchiveFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1024)
    media_type: str = Field(min_length=1, max_length=255)
    content_base64: str


class _SkillArchive(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    files: tuple[_ArchiveFile, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _unique_safe_paths(self) -> _SkillArchive:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)) or "SKILL.md" not in paths:
            raise ValueError("bootstrap Skill archive paths are invalid")
        for value in paths:
            relative = PurePosixPath(value)
            if "\\" in value or relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("bootstrap Skill archive path is invalid")
        return self


def load_skill_archive(payload: bytes) -> tuple[SkillArchiveFile, ...]:
    """Decode one authenticated archive without exposing partial results."""

    try:
        archive = _SkillArchive.model_validate_json(payload)
        files: list[SkillArchiveFile] = []
        total_bytes = 0
        for item in archive.files:
            content = base64.b64decode(item.content_base64, validate=True)
            total_bytes += len(content)
            if total_bytes > _MAX_ARCHIVE_BYTES:
                raise BootstrapCatalogError("bootstrap Skill archive is too large")
            files.append(
                SkillArchiveFile(
                    path=item.path,
                    content=content,
                    media_type=item.media_type,
                )
            )
    except BootstrapCatalogError:
        raise
    except (ValidationError, ValueError, UnicodeError, binascii.Error):
        raise BootstrapCatalogError("bootstrap Skill archive is invalid") from None
    return tuple(sorted(files, key=lambda item: item.path))


def dump_skill_archive(files: Sequence[SkillArchiveFile]) -> bytes:
    """Encode a canonical archive for the checked-in catalog generator."""

    archive = _SkillArchive(
        schema_version=1,
        files=tuple(
            _ArchiveFile(
                path=item.path,
                media_type=item.media_type,
                content_base64=base64.b64encode(item.content).decode("ascii"),
            )
            for item in sorted(files, key=lambda item: item.path)
        ),
    )
    return (
        json.dumps(
            archive.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
    )


__all__ = ["dump_skill_archive", "load_skill_archive"]
