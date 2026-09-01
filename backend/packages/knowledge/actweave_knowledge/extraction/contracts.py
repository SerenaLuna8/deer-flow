"""Immutable, safe data transfer objects for local document extraction."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from actweave_knowledge.contracts import (
    KNOWLEDGE_PARSE_FAILED,
    KnowledgeError,
)

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

_POSITION_KEYS = frozenset(
    {
        "page",
        "paragraph",
        "table",
        "row",
        "row_end",
        "column",
        "sheet",
        "slide",
        "chapter",
        "line",
        "line_end",
        "element",
        "image_index",
        "table_path",
        "encoding",
    }
)
_NUMERIC_POSITION_KEYS = _POSITION_KEYS - {"sheet", "table_path", "encoding"}
_TABLE_PATH_PATTERN = re.compile(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*")


class FrozenModel(BaseModel):
    """Pydantic DTO base that prevents field reassignment and unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ExtractionError(KnowledgeError):
    """A safe parse failure that retains an internal reason for later P3 projection."""

    def __init__(self, reason_code: str, message: str = "文件解析失败") -> None:
        super().__init__(KNOWLEDGE_PARSE_FAILED, message)
        self.reason_code = reason_code


class ExtractionLimits(FrozenModel):
    max_source_bytes: int = Field(default=50 * 1024**2, gt=0)
    max_text_chars: int = Field(default=5_000_000, gt=0)
    max_images: int = Field(default=100, gt=0)
    max_image_bytes: int = Field(default=5 * 1024**2, gt=0)
    max_image_pixels: int = Field(default=20_000_000, gt=0)
    max_total_image_bytes: int = Field(default=50 * 1024**2, gt=0)
    max_manifest_bytes: int = Field(default=50 * 1024**2, gt=0)
    max_work_dir_bytes: int = Field(default=512 * 1024**2, gt=0)


def _copy_and_validate_position(value: object) -> dict[str, str | int]:
    if not isinstance(value, dict):
        raise ValueError("source position must be a mapping")
    copied = dict(value)
    if set(copied) - _POSITION_KEYS:
        raise ValueError("source position contains unsafe key")
    for key, item in copied.items():
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            raise ValueError("source position values must be text or integers")
        if key in _NUMERIC_POSITION_KEYS and (not isinstance(item, int) or item < 1):
            raise ValueError("numeric source positions start at one")
        if key == "table_path" and (not isinstance(item, str) or _TABLE_PATH_PATTERN.fullmatch(item) is None):
            raise ValueError("table path must be numeric hierarchy")
    return copied


class SourceSpan(FrozenModel):
    block_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    location: dict[str, str | int] = Field(default_factory=dict)
    role: Literal["source", "context_prefix"] = "source"

    @field_validator("location", mode="before")
    @classmethod
    def _validate_location(cls, value: object) -> dict[str, str | int]:
        return _copy_and_validate_position(value)

    @model_validator(mode="after")
    def _validate_interval(self) -> SourceSpan:
        if self.end < self.start:
            raise ValueError("invalid source interval")
        return self


class ParseWarning(FrozenModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    source_position: dict[str, str | int] = Field(default_factory=dict)

    @field_validator("source_position", mode="before")
    @classmethod
    def _validate_source_position(cls, value: object) -> dict[str, str | int]:
        return _copy_and_validate_position(value)


class HeaderRule(FrozenModel):
    sheet: str | None = None
    mode: Literal["auto", "none", "explicit"] = "auto"
    row: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_row(self) -> HeaderRule:
        if (self.mode == "explicit") != (self.row is not None):
            raise ValueError("explicit header requires a row")
        return self


class ParseProfile(FrozenModel):
    etl_type: Literal["builtin", "unstructured_local"]
    extractor_id: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    normalization_version: str = Field(min_length=1)
    image_policy_version: str = Field(min_length=1)
    header_rules: tuple[HeaderRule, ...] = ()


class ChunkProfile(FrozenModel):
    unit: Literal["character", "token"]
    mode: Literal["general", "parent_child"]
    size: int = Field(gt=0)
    overlap: int = Field(ge=0)
    separator: str
    child_size: int = Field(gt=0)
    child_separator: str
    remove_extra_spaces: bool
    remove_urls_emails: bool
    tokenizer_profile_id: str | None
    tokenizer_digest: Digest | None
    cleaner_version: str = Field(min_length=1)
    splitter_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_overlap(self) -> ChunkProfile:
        if self.overlap >= self.size:
            raise ValueError("overlap must be smaller than size")
        return self


class ProcessingProfile(FrozenModel):
    parse: ParseProfile
    chunk: ChunkProfile


class Attachment(FrozenModel):
    ref: Digest
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class LocalAttachment(FrozenModel):
    attachment: Attachment
    relative_path: str = Field(min_length=1)


class AttachmentOccurrence(FrozenModel):
    ref: Digest
    alt_text: str
    source: SourceSpan


class Document(FrozenModel):
    page_content: str
    source_spans: tuple[SourceSpan, ...] = ()
    heading_path: tuple[str, ...] = ()
    kind: str = "paragraph"
    attachments: tuple[AttachmentOccurrence, ...] = ()
    warnings: tuple[ParseWarning, ...] = ()

    @model_validator(mode="after")
    def _validate_offsets(self) -> Document:
        spans = self.source_spans + tuple(occurrence.source for occurrence in self.attachments)
        if any(span.end > len(self.page_content) for span in spans):
            raise ValueError("source interval outside content")
        return self

    @property
    def metadata(self) -> dict[str, object]:
        """Return a new compatibility projection containing only safe source metadata."""

        return {
            "source_spans": [span.model_dump() for span in self.source_spans],
            "heading_path": list(self.heading_path),
            "kind": self.kind,
            "warnings": [warning.model_dump() for warning in self.warnings],
        }


class ExtractionResult(FrozenModel):
    documents: tuple[Document, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    warnings: tuple[ParseWarning, ...] = ()
    source_sha256: Digest
    parse_fingerprint: Digest


class ExtractSetting(FrozenModel):
    source_path: Path
    original_name: str = Field(min_length=1)
    datasource_type: Literal["file"] = "file"
    profile: ParseProfile


@runtime_checkable
class AttachmentSink(Protocol):
    def accept(self, source_path: Path, *, alt_text: str, source: SourceSpan) -> Attachment: ...


class ExtractionContext(FrozenModel):
    """Runtime-only dependencies that must never enter an extraction manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    work_dir: Path
    sink: AttachmentSink
    limits: ExtractionLimits
    check_cancelled: Callable[[], None]
