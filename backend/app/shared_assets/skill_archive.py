"""Bounded, filesystem-independent parsing for uploaded Skill packages."""

from __future__ import annotations

import gzip as gzip_lib
import io
import mimetypes
import posixpath
import stat
import struct
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from app.shared_assets.errors import AssetValidationFailed, SkillArchiveLimitExceeded
from app.shared_assets.models import SkillArchiveFile

MAX_SKILL_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_SKILL_ARCHIVE_FILE_BYTES = 64 * 1024 * 1024
MAX_SKILL_ARCHIVE_FILES = 16_384
MAX_SKILL_ARCHIVE_MEMBERS = MAX_SKILL_ARCHIVE_FILES
MAX_SKILL_ARCHIVE_UPLOAD_BYTES = 160 * 1024 * 1024

_ZIP_SUFFIXES = (".zip", ".skill")
_TAR_SUFFIXES = (".tar",)
_GZIP_TAR_SUFFIXES = (".tar.gz", ".tgz")
_TAR_BLOCK_BYTES = 512
_TAR_MEMBER_OVERHEAD_BUDGET = 6 * _TAR_BLOCK_BYTES
_TAR_END_RECORD_BUDGET = 20 * _TAR_BLOCK_BYTES
_MAX_TAR_PAX_METADATA_BYTES = 64 * 1024
_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_EOCD_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_ZIP_EOCD_FIXED_BYTES = 22
_ZIP_EOCD_MAX_COMMENT_BYTES = 65_535
_ZIP64_EOCD_FIXED_BYTES = 56
_ZIP64_EOCD_LOCATOR_BYTES = 20
_ZIP_CENTRAL_DIRECTORY_HEADER_BYTES = 46
_MAX_ZIP_CENTRAL_DIRECTORY_BYTES = 32 * 1024 * 1024


class _ArchiveStreamLimitExceeded(Exception):
    pass


class _BoundedReader:
    """Count bytes returned from an uncompressed archive stream."""

    def __init__(self, source, *, limit: int) -> None:
        self._source = source
        self._limit = limit
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._limit - self.bytes_read
        bounded_size = remaining + 1
        if size >= 0:
            bounded_size = min(size, bounded_size)
        content = self._source.read(bounded_size)
        self.bytes_read += len(content)
        if self.bytes_read > self._limit:
            raise _ArchiveStreamLimitExceeded
        return content


@dataclass(frozen=True)
class _LoadedArchiveFile:
    path: str
    content: bytes


@dataclass(frozen=True)
class _ZipCentralDirectory:
    entry_count: int
    size_bytes: int
    offset: int


def _invalid(request_id: str) -> AssetValidationFailed:
    return AssetValidationFailed(request_id)


def _limit_exceeded(request_id: str) -> SkillArchiveLimitExceeded:
    return SkillArchiveLimitExceeded(request_id)


def _archive_kind(filename: str, request_id: str) -> str:
    if not isinstance(filename, str) or not filename.strip() or "\x00" in filename:
        raise _invalid(request_id)
    lowered = filename.strip().casefold()
    if lowered.endswith(_GZIP_TAR_SUFFIXES):
        return "tar.gz"
    if lowered.endswith(_TAR_SUFFIXES):
        return "tar"
    if lowered.endswith(_ZIP_SUFFIXES):
        return "zip"
    raise _invalid(request_id)


def _canonical_member_path(raw_path: object, request_id: str) -> str:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise _invalid(request_id)
    windows_path = PureWindowsPath(raw_path)
    posix_path = raw_path.replace("\\", "/")
    if ":" in raw_path or windows_path.drive or windows_path.is_absolute() or posix_path.startswith("/") or ".." in PurePosixPath(posix_path).parts:
        raise _invalid(request_id)
    normalized = unicodedata.normalize(
        "NFC",
        posixpath.normpath(posix_path).removeprefix("./"),
    )
    if not normalized or normalized == "." or len(normalized) > 1024:
        raise _invalid(request_id)
    return normalized


def _bounded_read(
    source,
    *,
    expected_size: int,
    remaining_bytes: int,
    request_id: str,
) -> bytes:
    if expected_size < 0 or expected_size > remaining_bytes:
        raise _limit_exceeded(request_id)
    content = source.read(expected_size + 1)
    if len(content) != expected_size:
        raise _invalid(request_id)
    return content


def _validate_tar_extended_metadata(
    member: tarfile.TarInfo,
    request_id: str,
) -> None:
    total_bytes = 0
    for key, value in member.pax_headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise _invalid(request_id)
        try:
            total_bytes += len(key.encode("utf-8"))
            total_bytes += len(value.encode("utf-8"))
        except UnicodeError:
            raise _invalid(request_id) from None
        if total_bytes > _MAX_TAR_PAX_METADATA_BYTES:
            raise _limit_exceeded(request_id)


def _zip_member_file_type(info: zipfile.ZipInfo) -> int:
    unix_mode = info.external_attr >> 16
    return stat.S_IFMT(unix_mode)


def _find_zip_eocd(payload: bytes, request_id: str) -> int:
    """Find an EOCD whose declared comment ends exactly at the payload end."""

    search_start = max(
        0,
        len(payload) - _ZIP_EOCD_FIXED_BYTES - _ZIP_EOCD_MAX_COMMENT_BYTES,
    )
    search_end = len(payload)
    while True:
        offset = payload.rfind(
            _ZIP_EOCD_SIGNATURE,
            search_start,
            search_end,
        )
        if offset < 0:
            raise _invalid(request_id)
        if offset + _ZIP_EOCD_FIXED_BYTES <= len(payload):
            try:
                comment_bytes = struct.unpack_from("<H", payload, offset + 20)[0]
            except struct.error:
                raise _invalid(request_id) from None
            if offset + _ZIP_EOCD_FIXED_BYTES + comment_bytes == len(payload):
                return offset
        search_end = offset


def _zip64_central_directory(
    payload: bytes,
    *,
    eocd_offset: int,
    request_id: str,
) -> tuple[_ZipCentralDirectory, int]:
    locator_offset = eocd_offset - _ZIP64_EOCD_LOCATOR_BYTES
    if locator_offset < 0:
        raise _invalid(request_id)
    try:
        (
            locator_signature,
            zip64_disk,
            zip64_eocd_offset,
            disk_count,
        ) = struct.unpack_from("<4sLQL", payload, locator_offset)
    except struct.error:
        raise _invalid(request_id) from None
    if locator_signature != _ZIP64_EOCD_LOCATOR_SIGNATURE or zip64_disk != 0 or disk_count != 1 or zip64_eocd_offset + _ZIP64_EOCD_FIXED_BYTES > locator_offset:
        raise _invalid(request_id)

    try:
        (
            zip64_signature,
            record_size,
            _version_made_by,
            _version_needed,
            disk_number,
            central_directory_disk,
            entries_on_disk,
            entry_count,
            central_directory_size,
            central_directory_offset,
        ) = struct.unpack_from(
            "<4sQ2H2L4Q",
            payload,
            zip64_eocd_offset,
        )
    except struct.error:
        raise _invalid(request_id) from None
    if zip64_signature != _ZIP64_EOCD_SIGNATURE or record_size < _ZIP64_EOCD_FIXED_BYTES - 12 or zip64_eocd_offset + 12 + record_size != locator_offset or disk_number != 0 or central_directory_disk != 0 or entries_on_disk != entry_count:
        raise _invalid(request_id)
    return (
        _ZipCentralDirectory(
            entry_count=entry_count,
            size_bytes=central_directory_size,
            offset=central_directory_offset,
        ),
        zip64_eocd_offset,
    )


def _zip_central_directory_metadata(
    payload: bytes,
    request_id: str,
) -> tuple[_ZipCentralDirectory, int]:
    eocd_offset = _find_zip_eocd(payload, request_id)
    try:
        (
            signature,
            disk_number,
            central_directory_disk,
            entries_on_disk,
            entry_count,
            central_directory_size,
            central_directory_offset,
            _comment_bytes,
        ) = struct.unpack_from("<4s4H2LH", payload, eocd_offset)
    except struct.error:
        raise _invalid(request_id) from None
    if signature != _ZIP_EOCD_SIGNATURE or disk_number != 0 or central_directory_disk != 0 or entries_on_disk != entry_count:
        raise _invalid(request_id)

    uses_zip64 = entry_count == 0xFFFF or central_directory_size == 0xFFFFFFFF or central_directory_offset == 0xFFFFFFFF
    if uses_zip64:
        return _zip64_central_directory(
            payload,
            eocd_offset=eocd_offset,
            request_id=request_id,
        )
    return (
        _ZipCentralDirectory(
            entry_count=entry_count,
            size_bytes=central_directory_size,
            offset=central_directory_offset,
        ),
        eocd_offset,
    )


def _preflight_zip_central_directory(
    payload: bytes,
    request_id: str,
) -> None:
    """Bound central-directory work before ``ZipFile`` allocates ``ZipInfo`` rows."""

    metadata, directory_end = _zip_central_directory_metadata(
        payload,
        request_id,
    )
    if metadata.entry_count > MAX_SKILL_ARCHIVE_MEMBERS or metadata.size_bytes > _MAX_ZIP_CENTRAL_DIRECTORY_BYTES:
        raise _limit_exceeded(request_id)
    if metadata.entry_count < 1 or metadata.size_bytes < _ZIP_CENTRAL_DIRECTORY_HEADER_BYTES or metadata.offset < 0 or metadata.offset + metadata.size_bytes != directory_end or directory_end > len(payload):
        raise _invalid(request_id)

    directory = memoryview(payload)[metadata.offset : directory_end]
    cursor = 0
    for _ in range(metadata.entry_count):
        if cursor + _ZIP_CENTRAL_DIRECTORY_HEADER_BYTES > len(directory):
            raise _invalid(request_id)
        try:
            header = struct.unpack_from(
                "<4s6H3L5H2L",
                directory,
                cursor,
            )
        except struct.error:
            raise _invalid(request_id) from None
        if header[0] != _ZIP_CENTRAL_DIRECTORY_SIGNATURE or header[13] != 0:
            raise _invalid(request_id)
        entry_bytes = _ZIP_CENTRAL_DIRECTORY_HEADER_BYTES + header[10] + header[11] + header[12]
        cursor += entry_bytes
        if cursor > len(directory):
            raise _invalid(request_id)
    if cursor != len(directory):
        raise _invalid(request_id)


def _load_zip(
    payload: bytes,
    *,
    request_id: str,
) -> tuple[_LoadedArchiveFile, ...]:
    loaded: list[_LoadedArchiveFile] = []
    try:
        _preflight_zip_central_directory(payload, request_id)
        with zipfile.ZipFile(io.BytesIO(payload), mode="r") as archive:
            members = archive.infolist()
            if not members:
                raise _invalid(request_id)
            if len(members) > MAX_SKILL_ARCHIVE_MEMBERS:
                raise _limit_exceeded(request_id)

            regular_members: list[tuple[zipfile.ZipInfo, str]] = []
            total_bytes = 0
            for info in members:
                path = _canonical_member_path(info.filename, request_id)
                if info.flag_bits & 0x1:
                    raise _invalid(request_id)
                file_type = _zip_member_file_type(info)
                if info.is_dir():
                    if file_type not in {0, stat.S_IFDIR}:
                        raise _invalid(request_id)
                    continue
                if file_type not in {0, stat.S_IFREG}:
                    raise _invalid(request_id)
                if info.file_size < 0:
                    raise _invalid(request_id)
                if info.file_size > MAX_SKILL_ARCHIVE_FILE_BYTES:
                    raise _limit_exceeded(request_id)
                total_bytes += info.file_size
                if len(regular_members) >= MAX_SKILL_ARCHIVE_FILES or total_bytes > MAX_SKILL_ARCHIVE_BYTES:
                    raise _limit_exceeded(request_id)
                regular_members.append((info, path))

            if not regular_members:
                raise _invalid(request_id)
            remaining = MAX_SKILL_ARCHIVE_BYTES
            for info, path in regular_members:
                with archive.open(info, mode="r") as source:
                    content = _bounded_read(
                        source,
                        expected_size=info.file_size,
                        remaining_bytes=remaining,
                        request_id=request_id,
                    )
                remaining -= len(content)
                loaded.append(_LoadedArchiveFile(path, content))
    except AssetValidationFailed:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise _invalid(request_id) from None
    return tuple(loaded)


def _load_tar(
    payload: bytes,
    *,
    gzip: bool,
    request_id: str,
) -> tuple[_LoadedArchiveFile, ...]:
    loaded: list[_LoadedArchiveFile] = []
    raw_stream = io.BytesIO(payload)
    decompressed_stream = gzip_lib.GzipFile(fileobj=raw_stream, mode="rb") if gzip else raw_stream
    hard_stream_limit = MAX_SKILL_ARCHIVE_BYTES + MAX_SKILL_ARCHIVE_FILES * _TAR_MEMBER_OVERHEAD_BUDGET + _TAR_END_RECORD_BUDGET
    bounded_stream = _BoundedReader(
        decompressed_stream,
        limit=hard_stream_limit,
    )
    try:
        with tarfile.open(fileobj=bounded_stream, mode="r|") as archive:
            total_bytes = 0
            padded_content_bytes = 0
            member_count = 0
            for member in archive:
                member_count += 1
                if member_count > MAX_SKILL_ARCHIVE_MEMBERS:
                    raise _limit_exceeded(request_id)
                path = _canonical_member_path(member.name, request_id)
                _validate_tar_extended_metadata(member, request_id)
                if member.isdir():
                    continue
                if not member.isreg() or member.type == tarfile.GNUTYPE_SPARSE or bool(getattr(member, "sparse", None)):
                    raise _invalid(request_id)
                if member.size < 0:
                    raise _invalid(request_id)
                if member.size > MAX_SKILL_ARCHIVE_FILE_BYTES:
                    raise _limit_exceeded(request_id)
                total_bytes += member.size
                if len(loaded) >= MAX_SKILL_ARCHIVE_FILES or total_bytes > MAX_SKILL_ARCHIVE_BYTES:
                    raise _limit_exceeded(request_id)
                source = archive.extractfile(member)
                if source is None:
                    raise _invalid(request_id)
                with source:
                    content = _bounded_read(
                        source,
                        expected_size=member.size,
                        remaining_bytes=MAX_SKILL_ARCHIVE_BYTES - (total_bytes - member.size),
                        request_id=request_id,
                    )
                padded_content_bytes += ((member.size + _TAR_BLOCK_BYTES - 1) // _TAR_BLOCK_BYTES) * _TAR_BLOCK_BYTES
                loaded.append(_LoadedArchiveFile(path, content))
        while bounded_stream.read(1024 * 1024):
            pass
        structural_limit = padded_content_bytes + member_count * _TAR_MEMBER_OVERHEAD_BUDGET + _TAR_END_RECORD_BUDGET
        if bounded_stream.bytes_read > structural_limit:
            raise _limit_exceeded(request_id)
        if not loaded:
            raise _invalid(request_id)
    except AssetValidationFailed:
        raise
    except _ArchiveStreamLimitExceeded:
        raise _limit_exceeded(request_id) from None
    except (
        OSError,
        EOFError,
        ValueError,
        tarfile.TarError,
    ):
        raise _invalid(request_id) from None
    finally:
        decompressed_stream.close()
    return tuple(loaded)


def _is_macos_metadata_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and (parts[0] == "__MACOSX" or parts[-1] == ".DS_Store" or parts[-1].startswith("._"))


def _without_macos_metadata(
    files: tuple[_LoadedArchiveFile, ...],
) -> tuple[_LoadedArchiveFile, ...]:
    return tuple(item for item in files if not _is_macos_metadata_path(item.path))


def _strip_single_wrapper(
    files: tuple[_LoadedArchiveFile, ...],
    request_id: str,
) -> tuple[_LoadedArchiveFile, ...]:
    paths = tuple(item.path for item in files)
    if "SKILL.md" in paths:
        stripped = files
    else:
        split_paths = tuple(PurePosixPath(path).parts for path in paths)
        if not split_paths or any(len(parts) < 2 for parts in split_paths) or len({parts[0] for parts in split_paths}) != 1:
            raise _invalid(request_id)
        stripped = tuple(
            _LoadedArchiveFile(
                PurePosixPath(*parts[1:]).as_posix(),
                item.content,
            )
            for item, parts in zip(files, split_paths, strict=True)
        )

    stripped_paths = tuple(item.path for item in stripped)
    if stripped_paths.count("SKILL.md") != 1 or any(PurePosixPath(path).name == "SKILL.md" and path != "SKILL.md" for path in stripped_paths):
        raise _invalid(request_id)

    identities: dict[str, str] = {}
    for path in stripped_paths:
        identity = unicodedata.normalize("NFC", path.casefold())
        if identity in identities:
            raise _invalid(request_id)
        identities[identity] = path
    for identity in identities:
        parts = PurePosixPath(identity).parts
        if any(PurePosixPath(*parts[:index]).as_posix() in identities for index in range(1, len(parts))):
            raise _invalid(request_id)
    return tuple(sorted(stripped, key=lambda item: item.path))


def _media_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path, strict=False)
    return guessed or "application/octet-stream"


def load_skill_archive_package(
    payload: bytes,
    *,
    filename: str,
    request_id: str,
) -> tuple[SkillArchiveFile, ...]:
    """Parse one supported archive without writing untrusted paths to disk."""

    if not isinstance(payload, bytes) or not payload:
        raise _invalid(request_id)
    if len(payload) > MAX_SKILL_ARCHIVE_UPLOAD_BYTES:
        raise _limit_exceeded(request_id)
    kind = _archive_kind(filename, request_id)
    if kind == "zip":
        loaded = _load_zip(payload, request_id=request_id)
    else:
        loaded = _load_tar(
            payload,
            gzip=kind == "tar.gz",
            request_id=request_id,
        )
    normalized = _strip_single_wrapper(_without_macos_metadata(loaded), request_id)
    return tuple(
        SkillArchiveFile(
            path=item.path,
            content=item.content,
            media_type=_media_type(item.path),
        )
        for item in normalized
    )


__all__ = [
    "MAX_SKILL_ARCHIVE_BYTES",
    "MAX_SKILL_ARCHIVE_FILE_BYTES",
    "MAX_SKILL_ARCHIVE_FILES",
    "MAX_SKILL_ARCHIVE_MEMBERS",
    "MAX_SKILL_ARCHIVE_UPLOAD_BYTES",
    "load_skill_archive_package",
]
