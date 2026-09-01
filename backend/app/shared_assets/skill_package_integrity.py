from __future__ import annotations

import hashlib
import json
import posixpath
import re
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from app.shared_assets.errors import AssetConflict, AssetValidationFailed
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.skill_archive import MAX_SKILL_ARCHIVE_BYTES, MAX_SKILL_ARCHIVE_FILES
from app.shared_assets.skill_repository import (
    SkillVersionFileMetadataRecord,
    SkillVersionRecord,
)
from app.shared_assets.skill_version_facts import skill_version_archive_facts
from deerflow.skills.frontmatter import (
    MAX_SKILL_FRONTMATTER_BYTES,
    parse_skill_frontmatter_document,
)
from deerflow.skills.parser import parse_skill_file
from deerflow.skills.types import SkillCategory
from deerflow.skills.validation import _validate_skill_frontmatter

MAX_SKILL_TEXT_PREVIEW_BYTES = 1024 * 1024
MAX_SKILL_EDIT_TEXT_BYTES = 5 * 1024 * 1024
MAX_SKILL_FILE_CHANGES = 256
_SYMLINK_MEDIA_TYPES = frozenset({"application/symlink", "application/x-symlink", "inode/symlink"})
_WIN32_INVALID_SEGMENT_CHARS = frozenset('<>:"|?*')
_WIN32_RESERVED_BASENAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_EXECUTABLE_MEDIA_TYPES = frozenset(
    {
        "application/vnd.microsoft.portable-executable",
        "application/x-dosexec",
        "application/x-executable",
        "application/x-mach-binary",
        "application/x-pie-executable",
        "application/x-sharedlib",
    }
)


@dataclass(frozen=True)
class SkillFileView:
    path: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class SkillFileContentView:
    path: str
    media_type: str
    size_bytes: int
    sha256: str
    preview_status: Literal["ready", "binary", "too_large"]
    encoding: Literal["utf-8"] | None
    content: str | None
    source_payload_checksum: str
    asset_revision: int


@dataclass(frozen=True)
class SkillFileChange:
    op: Literal["create", "replace", "delete"]
    path: str
    content: str | None = None
    media_type: str | None = None


@dataclass(frozen=True)
class SkillSecretRequirementView:
    name: str
    target_env: str
    optional: bool


@dataclass(frozen=True)
class SkillArchivePreview:
    checksum: str
    files: tuple[SkillArchiveFile, ...]
    file_views: tuple[SkillFileView, ...]
    description: str
    frontmatter: Mapping[str, object]
    compatibility: str | None
    secret_requirements: tuple[SkillSecretRequirementView, ...]


@dataclass(frozen=True)
class SkillDraftSnapshot:
    checksum: str
    files: tuple[SkillArchiveFile, ...]


def _validate_archive_file(item: SkillArchiveFile, request_id: str) -> SkillArchiveFile:
    if not isinstance(item, SkillArchiveFile) or not isinstance(item.path, str) or not isinstance(item.content, bytes) or not isinstance(item.media_type, str):
        raise AssetValidationFailed(request_id)

    raw_path = item.path
    windows_path = PureWindowsPath(raw_path)
    posix_path = raw_path.replace("\\", "/")
    if not raw_path or "\x00" in raw_path or raw_path.endswith(("/", "\\")) or windows_path.drive or windows_path.is_absolute() or posix_path.startswith("/") or ".." in PurePosixPath(posix_path).parts:
        raise AssetValidationFailed(request_id)

    normalized_path = unicodedata.normalize("NFC", posixpath.normpath(posix_path).removeprefix("./"))
    if not normalized_path or normalized_path == "." or len(normalized_path) > 1024:
        raise AssetValidationFailed(request_id)
    for segment in PurePosixPath(normalized_path).parts:
        reserved_basename = segment.partition(".")[0].casefold()
        if segment.endswith((".", " ")) or any(character in _WIN32_INVALID_SEGMENT_CHARS or unicodedata.category(character) == "Cc" for character in segment) or reserved_basename in _WIN32_RESERVED_BASENAMES:
            raise AssetValidationFailed(request_id)

    media_type = item.media_type.strip()
    media_type_base = media_type.partition(";")[0].strip().lower()
    if not media_type or len(media_type) > 255 or media_type_base in _SYMLINK_MEDIA_TYPES or media_type_base in _EXECUTABLE_MEDIA_TYPES:
        raise AssetValidationFailed(request_id)
    return SkillArchiveFile(normalized_path, item.content, media_type)


def normalize_skill_files(
    files: Sequence[SkillArchiveFile],
    *,
    request_id: str = "unknown",
) -> tuple[SkillArchiveFile, ...]:
    try:
        snapshot = tuple(files)
    except TypeError:
        raise AssetValidationFailed(request_id) from None
    if not snapshot or len(snapshot) > MAX_SKILL_ARCHIVE_FILES:
        raise AssetValidationFailed(request_id)
    normalized = tuple(sorted((_validate_archive_file(item, request_id) for item in snapshot), key=lambda item: item.path))
    paths = {item.path for item in normalized}
    if len(paths) != len(normalized):
        raise AssetValidationFailed(request_id)
    filesystem_identities = {unicodedata.normalize("NFC", path.casefold()): path for path in paths}
    if len(filesystem_identities) != len(paths):
        raise AssetValidationFailed(request_id)
    for identity in filesystem_identities:
        parts = PurePosixPath(identity).parts
        if any(PurePosixPath(*parts[:index]).as_posix() in filesystem_identities for index in range(1, len(parts))):
            raise AssetValidationFailed(request_id)
    if sum(len(item.content) for item in normalized) > MAX_SKILL_ARCHIVE_BYTES:
        raise AssetValidationFailed(request_id)
    if "SKILL.md" not in paths:
        raise AssetValidationFailed(request_id)
    return normalized


def _canonical_skill_path(value: object, request_id: str) -> str:
    if not isinstance(value, str):
        raise AssetValidationFailed(request_id)
    normalized = _validate_archive_file(
        SkillArchiveFile(value, b"", "text/plain"),
        request_id,
    ).path
    if normalized != value:
        raise AssetValidationFailed(request_id)
    return normalized


def _validate_file_changes(
    changes: Sequence[SkillFileChange],
    request_id: str,
) -> tuple[SkillFileChange, ...]:
    try:
        snapshot = tuple(changes)
    except TypeError:
        raise AssetValidationFailed(request_id) from None
    if not snapshot or len(snapshot) > MAX_SKILL_FILE_CHANGES:
        raise AssetValidationFailed(request_id)

    normalized: list[SkillFileChange] = []
    total_text_bytes = 0
    paths: set[str] = set()
    for change in snapshot:
        if not isinstance(change, SkillFileChange) or change.op not in {"create", "replace", "delete"}:
            raise AssetValidationFailed(request_id)
        path = _canonical_skill_path(change.path, request_id)
        if path in paths:
            raise AssetValidationFailed(request_id)
        paths.add(path)

        if change.op == "delete":
            if path == "SKILL.md" or change.content is not None or change.media_type is not None:
                raise AssetValidationFailed(request_id)
            normalized.append(SkillFileChange("delete", path))
            continue

        if not isinstance(change.content, str) or "\x00" in change.content:
            raise AssetValidationFailed(request_id)
        try:
            content = change.content.encode("utf-8")
        except UnicodeError:
            raise AssetValidationFailed(request_id) from None
        if len(content) > MAX_SKILL_TEXT_PREVIEW_BYTES:
            raise AssetValidationFailed(request_id)
        total_text_bytes += len(content)
        if total_text_bytes > MAX_SKILL_EDIT_TEXT_BYTES:
            raise AssetValidationFailed(request_id)

        if change.op == "create" and not isinstance(change.media_type, str):
            raise AssetValidationFailed(request_id)
        if change.media_type is not None and not isinstance(change.media_type, str):
            raise AssetValidationFailed(request_id)
        checked = _validate_archive_file(
            SkillArchiveFile(path, content, change.media_type or "text/plain"),
            request_id,
        )
        if checked.path != path:
            raise AssetValidationFailed(request_id)
        normalized.append(
            SkillFileChange(
                change.op,
                path,
                change.content,
                checked.media_type if change.media_type is not None else None,
            )
        )
    return tuple(normalized)


def _apply_file_changes(
    files: Sequence[SkillArchiveFile],
    changes: Sequence[SkillFileChange],
    request_id: str,
) -> tuple[SkillArchiveFile, ...]:
    current = {item.path: item for item in files}
    for change in _validate_file_changes(changes, request_id):
        existing = current.get(change.path)
        if change.op == "create":
            if existing is not None:
                raise AssetConflict(request_id)
            assert change.content is not None and change.media_type is not None
            current[change.path] = SkillArchiveFile(
                change.path,
                change.content.encode("utf-8"),
                change.media_type,
            )
        elif change.op == "replace":
            if existing is None:
                raise AssetConflict(request_id)
            assert change.content is not None
            current[change.path] = SkillArchiveFile(
                change.path,
                change.content.encode("utf-8"),
                change.media_type or existing.media_type,
            )
        else:
            if existing is None:
                raise AssetConflict(request_id)
            del current[change.path]
    return normalize_skill_files(tuple(current.values()), request_id=request_id)


def _decode_preview_content(
    metadata: SkillVersionFileMetadataRecord,
    raw: bytes,
    request_id: str,
) -> tuple[Literal["ready", "binary"], Literal["utf-8"] | None, str | None]:
    if len(raw) != metadata.size_bytes or hashlib.sha256(raw).hexdigest() != metadata.sha256:
        raise AssetValidationFailed(request_id)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "binary", None, None
    if "\x00" in decoded:
        return "binary", None, None
    return "ready", "utf-8", decoded


def _file_views(files: Sequence[SkillArchiveFile]) -> tuple[SkillFileView, ...]:
    return tuple(
        SkillFileView(
            path=item.path,
            media_type=item.media_type,
            size_bytes=len(item.content),
            sha256=hashlib.sha256(item.content).hexdigest(),
        )
        for item in files
    )


def _snapshot_checksum(file_views: Sequence[SkillFileView]) -> str:
    # Keep the revision-0001 persisted checksum contract. Runtime Skill
    # materialization is byte-based; media_type is validated separately, and a
    # Persisted Skill file rows are immutable at the database boundary.
    return skill_version_archive_facts(tuple((item.path, item.sha256, item.size_bytes) for item in file_views)).payload_checksum


def _snapshot_checksum_for_files(files: Sequence[SkillArchiveFile]) -> str:
    return _snapshot_checksum(_file_views(files))


def _preflight_skill_frontmatter(
    skill_file: Path,
    request_id: str,
) -> tuple[dict[str, object], tuple[tuple[str, str, bool], ...]]:
    manifest_text = skill_file.read_text(encoding="utf-8")
    parsed = parse_skill_frontmatter_document(manifest_text)
    if not parsed.valid or parsed.frontmatter is None or parsed.projection is None:
        raise AssetValidationFailed(request_id)
    return dict(parsed.frontmatter), tuple(
        (
            requirement.name,
            str(requirement.target_env),
            requirement.optional,
        )
        for requirement in parsed.projection.required_secrets
    )


def _analyze_skill_files(
    files: tuple[SkillArchiveFile, ...],
    request_id: str,
) -> SkillArchivePreview:
    try:
        with tempfile.TemporaryDirectory(prefix="deerflow-skill-preview-") as temp_dir:
            root = Path(temp_dir)
            for item in files:
                destination = root / item.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(item.content)

            frontmatter, canonical_requirements = _preflight_skill_frontmatter(
                root / "SKILL.md",
                request_id,
            )
            valid, _, _ = _validate_skill_frontmatter(root)
            if not valid:
                raise AssetValidationFailed(request_id)
            parsed = parse_skill_file(root / "SKILL.md", SkillCategory.CUSTOM)
            if parsed is None:
                raise AssetValidationFailed(request_id)

            parsed_requirements = tuple(
                (
                    requirement.name,
                    str(requirement.target_env),
                    requirement.optional,
                )
                for requirement in parsed.required_secrets
            )
            if canonical_requirements != parsed_requirements:
                raise AssetValidationFailed(request_id)

            requirement_views = tuple(
                SkillSecretRequirementView(
                    name=requirement.name,
                    target_env=str(requirement.target_env),
                    optional=requirement.optional,
                )
                for requirement in parsed.required_secrets
            )
            sanitized_frontmatter = dict(frontmatter)
            if "required-secrets" in sanitized_frontmatter:
                sanitized_frontmatter["required-secrets"] = [
                    {
                        "name": requirement.name,
                        "target_env": requirement.target_env,
                        "optional": requirement.optional,
                    }
                    for requirement in requirement_views
                ]
            compatibility = sanitized_frontmatter.get("compatibility")
            if compatibility is not None and (not isinstance(compatibility, str) or len(compatibility) > 255):
                raise AssetValidationFailed(request_id)
            compatibility = compatibility.strip() if isinstance(compatibility, str) else None
            try:
                canonical_frontmatter = json.dumps(
                    sanitized_frontmatter,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if len(canonical_frontmatter.encode("utf-8")) > MAX_SKILL_FRONTMATTER_BYTES:
                    raise AssetValidationFailed(request_id)
                sanitized_frontmatter = json.loads(canonical_frontmatter)
            except (TypeError, ValueError, RecursionError):
                raise AssetValidationFailed(request_id) from None

    except AssetValidationFailed:
        raise
    except (
        OSError,
        RecursionError,
        UnicodeError,
        ValueError,
    ):
        raise AssetValidationFailed(request_id) from None

    views = _file_views(files)
    return SkillArchivePreview(
        checksum=_snapshot_checksum(views),
        files=files,
        file_views=views,
        description=parsed.description,
        frontmatter=sanitized_frontmatter,
        compatibility=compatibility,
        secret_requirements=requirement_views,
    )


def _archive_files(
    record: SkillVersionRecord,
    request_id: str,
) -> tuple[SkillArchiveFile, ...]:
    files: list[SkillArchiveFile] = []
    for row in record.files:
        if row.size_bytes != len(row.content) or row.sha256 != hashlib.sha256(row.content).hexdigest():
            raise AssetValidationFailed(request_id)
        files.append(SkillArchiveFile(path=row.path, content=bytes(row.content), media_type=row.media_type))
    snapshot = tuple(sorted(files, key=lambda item: item.path))
    normalized = normalize_skill_files(snapshot, request_id=request_id)
    if normalized != snapshot:
        raise AssetValidationFailed(request_id)
    return normalized


def _verified_archive_files(
    record: SkillVersionRecord,
    request_id: str,
) -> tuple[SkillArchiveFile, ...]:
    files = _archive_files(record, request_id)
    if _snapshot_checksum(_file_views(files)) != record.row.payload_checksum:
        raise AssetValidationFailed(request_id)
    return files


def _metadata_file_views(
    files: Sequence[SkillVersionFileMetadataRecord],
    request_id: str,
) -> tuple[SkillFileView, ...]:
    views: list[SkillFileView] = []
    paths: set[str] = set()
    filesystem_identities: set[str] = set()
    for file in files:
        path = _canonical_skill_path(file.path, request_id)
        if path in paths:
            raise AssetValidationFailed(request_id)
        paths.add(path)
        identity = unicodedata.normalize("NFC", path.casefold())
        if identity in filesystem_identities:
            raise AssetValidationFailed(request_id)
        filesystem_identities.add(identity)
        if not isinstance(file.media_type, str):
            raise AssetValidationFailed(request_id)
        checked = _validate_archive_file(
            SkillArchiveFile(path, b"", file.media_type),
            request_id,
        )
        if checked.media_type != file.media_type:
            raise AssetValidationFailed(request_id)
        if not isinstance(file.size_bytes, int) or isinstance(file.size_bytes, bool) or file.size_bytes < 0 or file.size_bytes > MAX_SKILL_ARCHIVE_BYTES:
            raise AssetValidationFailed(request_id)
        if not isinstance(file.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", file.sha256) is None:
            raise AssetValidationFailed(request_id)
        views.append(
            SkillFileView(
                path=path,
                media_type=file.media_type,
                size_bytes=file.size_bytes,
                sha256=file.sha256,
            )
        )
    views.sort(key=lambda item: item.path)
    for identity in filesystem_identities:
        parts = PurePosixPath(identity).parts
        if any(PurePosixPath(*parts[:index]).as_posix() in filesystem_identities for index in range(1, len(parts))):
            raise AssetValidationFailed(request_id)
    if "SKILL.md" not in paths or sum(item.size_bytes for item in views) > MAX_SKILL_ARCHIVE_BYTES:
        raise AssetValidationFailed(request_id)
    return tuple(views)


analyze_skill_files = _analyze_skill_files
verified_archive_files = _verified_archive_files

__all__ = [
    "MAX_SKILL_EDIT_TEXT_BYTES",
    "MAX_SKILL_FILE_CHANGES",
    "MAX_SKILL_TEXT_PREVIEW_BYTES",
    "SkillArchivePreview",
    "SkillDraftSnapshot",
    "SkillFileChange",
    "SkillFileContentView",
    "SkillFileView",
    "SkillSecretRequirementView",
    "analyze_skill_files",
    "normalize_skill_files",
    "verified_archive_files",
]
