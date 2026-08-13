"""Neutral Worker/service contract for staged Skill Builder draft operations."""

from __future__ import annotations

import unicodedata
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, Protocol

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.shared_assets.skill_design_generation import (
    NeedsClarificationResult,
    SkillBuilderDependencySnapshot,
)

MAX_SKILL_BUILDER_DRAFT_FILES = 128
MAX_SKILL_BUILDER_DRAFT_FILE_BYTES = 512 * 1024
MAX_SKILL_BUILDER_DRAFT_TOTAL_BYTES = 2 * 1024 * 1024
MAX_SKILL_BUILDER_WRITE_CHUNK_BYTES = 32 * 1024
MAX_SKILL_BUILDER_READ_CHUNK_BYTES = 16 * 1024

_Checksum = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"\A[0-9a-f]{64}\z"),
]


def _canonical_candidate_path(value: str) -> str:
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or value.endswith("/")
        or PureWindowsPath(value).drive
        or PureWindowsPath(value).is_absolute()
        or PurePosixPath(value).is_absolute()
        or PurePosixPath(value).as_posix() != value
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ValueError("candidate file path is not canonical")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise ValueError("candidate file path is not canonical")
    return value


_Path = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=1024),
    AfterValidator(_canonical_candidate_path),
]
_Summary = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=500,
    ),
]


class _StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SkillBuilderDraftFileMetadata(_StrictContractModel):
    path: _Path
    media_type: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=True,
            min_length=1,
            max_length=255,
        ),
    ]
    size_bytes: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_SKILL_BUILDER_DRAFT_FILE_BYTES),
    ]
    sha256: _Checksum


class SkillBuilderCandidateFileList(_StrictContractModel):
    expected_draft_checksum: _Checksum | None = None
    offset: Annotated[
        int,
        Field(strict=True, ge=0, lt=MAX_SKILL_BUILDER_DRAFT_FILES),
    ] = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 20


class SkillBuilderDraftFilePage(_StrictContractModel):
    draft_checksum: _Checksum | None
    items: tuple[SkillBuilderDraftFileMetadata, ...] = Field(
        default=(),
        max_length=20,
    )
    offset: Annotated[
        int,
        Field(strict=True, ge=0, lt=MAX_SKILL_BUILDER_DRAFT_FILES),
    ]
    next_offset: (
        Annotated[
            int,
            Field(strict=True, ge=1, lt=MAX_SKILL_BUILDER_DRAFT_FILES),
        ]
        | None
    )
    total_file_count: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_SKILL_BUILDER_DRAFT_FILES),
    ]
    total_size_bytes: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_SKILL_BUILDER_DRAFT_TOTAL_BYTES),
    ]

    @field_validator("items", mode="before")
    @classmethod
    def _json_items_to_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_page(self) -> SkillBuilderDraftFilePage:
        paths = [item.path for item in self.items]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("draft files must be unique and path-sorted")
        if (self.total_file_count > 0) != (self.draft_checksum is not None):
            raise ValueError("draft checksum presence is inconsistent")
        if self.offset > self.total_file_count:
            raise ValueError("draft page offset exceeds the file count")
        expected_next = self.offset + len(self.items) if self.offset + len(self.items) < self.total_file_count else None
        if self.next_offset != expected_next:
            raise ValueError("draft page continuation is inconsistent")
        return self


class SkillBuilderDraftMutationReceipt(_StrictContractModel):
    mutation: Literal["upsert", "delete"]
    draft_checksum: _Checksum | None
    file: SkillBuilderDraftFileMetadata | None
    total_file_count: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_SKILL_BUILDER_DRAFT_FILES),
    ]
    total_size_bytes: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_SKILL_BUILDER_DRAFT_TOTAL_BYTES),
    ]

    @model_validator(mode="after")
    def _validate_receipt(self) -> SkillBuilderDraftMutationReceipt:
        if (self.total_file_count > 0) != (self.draft_checksum is not None):
            raise ValueError("draft checksum presence is inconsistent")
        if (self.mutation == "upsert") != (self.file is not None):
            raise ValueError("draft mutation target metadata is inconsistent")
        if self.file is not None and self.file.size_bytes > self.total_size_bytes:
            raise ValueError("draft mutation size is inconsistent")
        return self


class SkillBuilderCandidateFileRead(_StrictContractModel):
    path: _Path
    expected_draft_checksum: _Checksum
    offset_bytes: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_SKILL_BUILDER_DRAFT_FILE_BYTES),
    ] = 0
    limit_bytes: Annotated[
        int,
        Field(strict=True, ge=1, le=MAX_SKILL_BUILDER_READ_CHUNK_BYTES),
    ] = MAX_SKILL_BUILDER_READ_CHUNK_BYTES


class SkillBuilderCandidateFileChunk(_StrictContractModel):
    path: _Path
    media_type: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=True,
            min_length=1,
            max_length=255,
        ),
    ]
    draft_checksum: _Checksum
    file_size_bytes: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_SKILL_BUILDER_DRAFT_FILE_BYTES),
    ]
    file_sha256: _Checksum
    offset_bytes: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_SKILL_BUILDER_DRAFT_FILE_BYTES),
    ]
    content: str
    next_offset_bytes: (
        Annotated[
            int,
            Field(strict=True, ge=1, le=MAX_SKILL_BUILDER_DRAFT_FILE_BYTES),
        ]
        | None
    )

    @model_validator(mode="after")
    def _validate_chunk(self) -> SkillBuilderCandidateFileChunk:
        encoded = self.content.encode("utf-8")
        if len(encoded) > MAX_SKILL_BUILDER_READ_CHUNK_BYTES:
            raise ValueError("candidate read chunk is invalid")
        if not encoded and not (self.file_size_bytes == 0 and self.offset_bytes == 0 and self.next_offset_bytes is None):
            raise ValueError("candidate read chunk is empty before EOF")
        end = self.offset_bytes + len(encoded)
        if end > self.file_size_bytes:
            raise ValueError("candidate read chunk exceeds the file")
        expected_next = end if end < self.file_size_bytes else None
        if self.next_offset_bytes != expected_next:
            raise ValueError("candidate read continuation is inconsistent")
        return self


class SkillBuilderCandidateFileUpsert(_StrictContractModel):
    path: _Path
    media_type: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=True,
            min_length=1,
            max_length=255,
        ),
    ]
    content: str
    mode: Literal["replace", "append"]
    expected_draft_checksum: _Checksum | None
    expected_file_size_bytes: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_SKILL_BUILDER_DRAFT_FILE_BYTES),
    ]
    expected_file_sha256: _Checksum | None

    @model_validator(mode="after")
    def _validate_upsert(self) -> SkillBuilderCandidateFileUpsert:
        try:
            encoded = self.content.encode("utf-8")
        except UnicodeError:
            raise ValueError("candidate file chunk must be UTF-8") from None
        if not encoded or b"\x00" in encoded or len(encoded) > MAX_SKILL_BUILDER_WRITE_CHUNK_BYTES:
            raise ValueError("candidate file chunk is invalid")
        if self.expected_file_size_bytes > 0 and self.expected_file_sha256 is None:
            raise ValueError("expected file identity is inconsistent")
        if self.mode == "append" and self.expected_file_sha256 is None:
            raise ValueError("append requires an existing exact file")
        return self


class SkillBuilderCandidateFileDelete(_StrictContractModel):
    path: _Path
    expected_draft_checksum: _Checksum
    expected_file_size_bytes: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_SKILL_BUILDER_DRAFT_FILE_BYTES),
    ]
    expected_file_sha256: _Checksum


class SkillBuilderCandidateFinalize(_StrictContractModel):
    expected_draft_checksum: _Checksum
    summary: _Summary


class SkillBuilderTerminalReceipt(_StrictContractModel):
    accepted: Literal[True] = True
    terminal: Literal["clarification", "candidate"]


class SkillBuilderDraftSink(Protocol):
    """Run-bound callbacks; implementations revalidate authority and lease."""

    async def list_candidate_files(
        self,
        request: SkillBuilderCandidateFileList,
    ) -> SkillBuilderDraftFilePage: ...

    async def read_candidate_file(
        self,
        request: SkillBuilderCandidateFileRead,
    ) -> SkillBuilderCandidateFileChunk: ...

    async def upsert_candidate_file(
        self,
        request: SkillBuilderCandidateFileUpsert,
    ) -> SkillBuilderDraftMutationReceipt: ...

    async def delete_candidate_file(
        self,
        request: SkillBuilderCandidateFileDelete,
    ) -> SkillBuilderDraftMutationReceipt: ...

    async def request_clarification(
        self,
        result: NeedsClarificationResult,
    ) -> SkillBuilderTerminalReceipt | None: ...

    async def finalize_candidate(
        self,
        request: SkillBuilderCandidateFinalize,
        dependencies: SkillBuilderDependencySnapshot,
    ) -> SkillBuilderTerminalReceipt | None: ...


__all__ = [
    "MAX_SKILL_BUILDER_DRAFT_FILE_BYTES",
    "MAX_SKILL_BUILDER_DRAFT_FILES",
    "MAX_SKILL_BUILDER_DRAFT_TOTAL_BYTES",
    "MAX_SKILL_BUILDER_READ_CHUNK_BYTES",
    "MAX_SKILL_BUILDER_WRITE_CHUNK_BYTES",
    "SkillBuilderCandidateFileChunk",
    "SkillBuilderCandidateFileDelete",
    "SkillBuilderCandidateFileList",
    "SkillBuilderCandidateFileRead",
    "SkillBuilderCandidateFileUpsert",
    "SkillBuilderCandidateFinalize",
    "SkillBuilderDraftFileMetadata",
    "SkillBuilderDraftFilePage",
    "SkillBuilderDraftMutationReceipt",
    "SkillBuilderDraftSink",
    "SkillBuilderTerminalReceipt",
]
