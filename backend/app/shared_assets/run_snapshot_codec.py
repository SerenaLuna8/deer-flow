from __future__ import annotations

import base64
import hashlib
import io
import json
import posixpath
import re
import struct
import unicodedata
import uuid
import zlib
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType

from pydantic import ValidationError

from app.shared_assets.agent_payload_checksum import (
    resolved_agent_payload_checksum_matches,
)
from app.shared_assets.models import (
    AgentModelSettings,
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedAssetSnapshot,
    ResolvedMcpSnapshot,
    ResolvedSkillSnapshot,
    ResolvedSkillVersionSnapshot,
    RunSkillVersionManifest,
    SkillArchiveFile,
    SkillAssetRef,
    SkillSecretRequirementSnapshot,
)
from app.shared_assets.skill_archive import (
    MAX_SKILL_ARCHIVE_BYTES,
    MAX_SKILL_ARCHIVE_FILE_BYTES,
    MAX_SKILL_ARCHIVE_FILES,
)

RUN_ASSET_SNAPSHOT_SCHEMA_VERSION = 3
_LEGACY_RUN_ASSET_SNAPSHOT_SCHEMA_VERSION = 2
RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION = 4
# Bound the one JSONB parameter before persistence.  The 80 MiB budget admits
# the measured compressed ppt-master payload class; deployment memory safety
# still requires the real PostgreSQL acceptance gate.
MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES = 80 * 1024 * 1024
MAX_RUN_SKILL_REFERENCE_MANIFEST_JSON_BYTES = 256 * 1024

_SKILL_ARCHIVE_CODEC = "canonical-frame-zlib-6"
_SKILL_ARCHIVE_COMPRESSION_LEVEL = 6
_SKILL_FRAME_MAGIC = b"DFSKV3\x00\x01"
_SKILL_FRAME_HEADER = struct.Struct(">8sIQ")
_SKILL_FILE_HEADER = struct.Struct(">IIQ")
LEGACY_SKILL_ARCHIVE_CODEC = _SKILL_ARCHIVE_CODEC
LEGACY_SKILL_FRAME_FIXED_BYTES = _SKILL_FRAME_HEADER.size
LEGACY_SKILL_FILE_FIXED_BYTES = _SKILL_FILE_HEADER.size
_MAX_SKILL_PATH_CHARS = 1024
_MAX_SKILL_MEDIA_TYPE_CHARS = 255
_MAX_SKILL_PATH_BYTES = _MAX_SKILL_PATH_CHARS * 4
_MAX_SKILL_MEDIA_TYPE_BYTES = _MAX_SKILL_MEDIA_TYPE_CHARS * 4
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")


class RunAssetSnapshotInvalid(ValueError):
    pass


class RunAssetSnapshotTooLarge(RunAssetSnapshotInvalid):
    """The final encoded Run snapshot exceeds its persistence budget."""


def encode_run_skill_version_manifest(
    snapshot: ResolvedSkillVersionSnapshot,
) -> dict[str, object]:
    """Encode the byte-free persisted manifest for one exact Skill Version."""

    if (
        type(snapshot) is not ResolvedSkillVersionSnapshot
        or snapshot.kind is not AssetKind.SKILL
        or type(snapshot.file_count) is not int
        or not 1 <= snapshot.file_count <= MAX_SKILL_ARCHIVE_FILES
        or type(snapshot.content_size_bytes) is not int
        or not 0 <= snapshot.content_size_bytes <= MAX_SKILL_ARCHIVE_BYTES
        or _CHECKSUM.fullmatch(snapshot.checksum) is None
        or type(snapshot.catalog_generation) is not int
        or snapshot.catalog_generation < 0
        or any(type(value) is not uuid.UUID for value in snapshot.dependency_version_ids)
    ):
        raise RunAssetSnapshotInvalid("Run Skill Version snapshot metadata is invalid")
    manifest: dict[str, object] = {
        "schema_version": RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION,
        "kind": AssetKind.SKILL.value,
        "scope": snapshot.scope.value,
        "asset_id": str(snapshot.asset_id),
        "version_id": str(snapshot.version_id),
        "checksum": snapshot.checksum,
        "catalog_generation": snapshot.catalog_generation,
        "dependency_version_ids": [str(value) for value in snapshot.dependency_version_ids],
        "skill": {
            "source": "skill_version_ref",
            "file_count": snapshot.file_count,
            "content_size_bytes": snapshot.content_size_bytes,
        },
    }
    if encoded_run_asset_snapshot_json_size(manifest) > MAX_RUN_SKILL_REFERENCE_MANIFEST_JSON_BYTES:
        raise RunAssetSnapshotTooLarge(
            "Run Skill Version manifest exceeds the encoded JSON size limit",
        )
    return manifest


def decode_run_skill_version_manifest(
    value: Mapping[str, object],
) -> RunSkillVersionManifest:
    """Strictly decode one byte-free v4 Skill manifest.

    Version secret declarations deliberately are not carried by this value;
    the runtime plan joins and validates them from the exact immutable Version.
    """

    try:
        if encoded_run_asset_snapshot_json_size(value) > MAX_RUN_SKILL_REFERENCE_MANIFEST_JSON_BYTES:
            raise RunAssetSnapshotTooLarge(
                "Run Skill Version manifest exceeds the encoded JSON size limit",
            )
        if set(value) != {
            "schema_version",
            "kind",
            "scope",
            "asset_id",
            "version_id",
            "checksum",
            "catalog_generation",
            "dependency_version_ids",
            "skill",
        }:
            raise RunAssetSnapshotInvalid(
                "Run Skill Version manifest shape is invalid",
            )
        if value["schema_version"] != RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION or value["kind"] != AssetKind.SKILL.value:
            raise RunAssetSnapshotInvalid(
                "Run Skill Version manifest schema is unsupported",
            )
        raw = _mapping(value["skill"])
        if set(raw) != {"source", "file_count", "content_size_bytes"}:
            raise RunAssetSnapshotInvalid(
                "Run Skill Version manifest shape is invalid",
            )
        if raw["source"] != "skill_version_ref":
            raise RunAssetSnapshotInvalid(
                "Run Skill Version manifest source is invalid",
            )
        checksum = str(value["checksum"])
        generation = value["catalog_generation"]
        if _CHECKSUM.fullmatch(checksum) is None or type(generation) is not int or generation < 0:
            raise RunAssetSnapshotInvalid(
                "Run Skill Version manifest metadata is invalid",
            )
        return RunSkillVersionManifest(
            kind=AssetKind.SKILL,
            scope=AssetScope(str(value["scope"])),
            asset_id=uuid.UUID(str(value["asset_id"])),
            version_id=uuid.UUID(str(value["version_id"])),
            checksum=checksum,
            catalog_generation=generation,
            dependency_version_ids=_uuids(value["dependency_version_ids"]),
            file_count=_bounded_int(
                raw["file_count"],
                minimum=1,
                maximum=MAX_SKILL_ARCHIVE_FILES,
            ),
            content_size_bytes=_bounded_int(
                raw["content_size_bytes"],
                minimum=0,
                maximum=MAX_SKILL_ARCHIVE_BYTES,
            ),
        )
    except (RunAssetSnapshotInvalid, RunAssetSnapshotTooLarge):
        raise
    except (KeyError, TypeError, ValueError):
        raise RunAssetSnapshotInvalid(
            "Run Skill Version manifest is invalid",
        ) from None


def encode_run_asset_snapshot(snapshot: ResolvedAssetSnapshot) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": RUN_ASSET_SNAPSHOT_SCHEMA_VERSION,
        "kind": snapshot.kind.value,
        "scope": snapshot.scope.value,
        "asset_id": str(snapshot.asset_id),
        "version_id": str(snapshot.version_id),
        "checksum": snapshot.checksum,
        "catalog_generation": snapshot.catalog_generation,
        "dependency_version_ids": [str(value) for value in snapshot.dependency_version_ids],
    }
    if type(snapshot) is ResolvedAgentSnapshot:
        payload = snapshot.payload
        base["agent"] = {
            "slug": snapshot.slug,
            "source_key": snapshot.source_key,
            "description": payload.description,
            "agents_instructions": payload.agents_instructions,
            "soul": payload.soul,
            "identity": payload.identity,
            "user_context": payload.user_context,
            "model_ref": payload.model_ref,
            "model_settings": payload.model_settings.model_dump(mode="json"),
            "tool_groups": list(payload.tool_groups),
            "skill_refs": [{"scope": item.scope.value, "asset_id": str(item.asset_id)} for item in payload.skill_refs],
            "mcp_version_ids": [str(value) for value in payload.mcp_version_ids],
            "payload_schema_version": payload.payload_schema_version,
            "resolved_skill_version_ids": [str(value) for value in snapshot.skill_version_ids],
        }
    elif type(snapshot) is ResolvedSkillSnapshot:
        files, content_size = _validated_skill_files(snapshot.files)
        if _skill_checksum(files) != snapshot.checksum:
            raise RunAssetSnapshotInvalid("Run Skill snapshot checksum is invalid")
        compressed, uncompressed_size = _compress_skill_frame(
            files,
            content_size=content_size,
        )
        base["skill"] = {
            "codec": _SKILL_ARCHIVE_CODEC,
            "file_count": len(files),
            "content_size": content_size,
            "uncompressed_size": uncompressed_size,
            "compressed_size": len(compressed),
            "archive_base64": base64.b64encode(compressed).decode("ascii"),
            "secret_requirements": [
                {
                    "name": item.name,
                    "target_env": item.target_env,
                    "optional": item.optional,
                }
                for item in snapshot.secret_requirements
            ],
        }
    elif type(snapshot) is ResolvedMcpSnapshot:
        base["mcp"] = {
            "definition": _json_value(snapshot.definition),
            "secret_generation_ids": [str(value) for value in snapshot.secret_generation_ids],
            "secret_digest": snapshot.secret_digest,
        }
    else:
        raise RunAssetSnapshotInvalid("unsupported Run asset snapshot")
    if encoded_run_asset_snapshot_json_size(base) > MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES:
        raise RunAssetSnapshotTooLarge(
            "Run asset snapshot exceeds the encoded JSON size limit",
        )
    return base


def decode_run_asset_snapshot(value: Mapping[str, object]) -> ResolvedAssetSnapshot:
    try:
        if set(value) != {
            "schema_version",
            "kind",
            "scope",
            "asset_id",
            "version_id",
            "checksum",
            "catalog_generation",
            "dependency_version_ids",
            AssetKind(str(value["kind"])).value,
        }:
            raise RunAssetSnapshotInvalid("Run asset snapshot shape is invalid")
        schema_version = value["schema_version"]
        if type(schema_version) is not int or schema_version not in {
            _LEGACY_RUN_ASSET_SNAPSHOT_SCHEMA_VERSION,
            RUN_ASSET_SNAPSHOT_SCHEMA_VERSION,
        }:
            raise RunAssetSnapshotInvalid("Run asset snapshot schema is unsupported")
        kind = AssetKind(str(value["kind"]))
        scope = AssetScope(str(value["scope"]))
        asset_id = uuid.UUID(str(value["asset_id"]))
        version_id = uuid.UUID(str(value["version_id"]))
        checksum = str(value["checksum"])
        generation = value["catalog_generation"]
        if _CHECKSUM.fullmatch(checksum) is None or not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise RunAssetSnapshotInvalid("Run asset snapshot metadata is invalid")
        dependency_ids = _uuids(value["dependency_version_ids"])
        common = dict(
            kind=kind,
            scope=scope,
            asset_id=asset_id,
            version_id=version_id,
            checksum=checksum,
            catalog_generation=generation,
            dependency_version_ids=dependency_ids,
        )
        if kind is AssetKind.AGENT:
            raw = _mapping(value["agent"])
            if set(raw) != {
                "description",
                "agents_instructions",
                "soul",
                "identity",
                "user_context",
                "model_ref",
                "model_settings",
                "tool_groups",
                "skill_refs",
                "mcp_version_ids",
                "payload_schema_version",
                "resolved_skill_version_ids",
                "slug",
                "source_key",
            }:
                raise RunAssetSnapshotInvalid("Run Agent snapshot shape is invalid")
            schema_version = raw["payload_schema_version"]
            if not isinstance(schema_version, int) or isinstance(schema_version, bool):
                raise RunAssetSnapshotInvalid("Run Agent schema is invalid")
            skill_refs = tuple(
                SkillAssetRef(
                    AssetScope(str(item["scope"])),
                    uuid.UUID(str(item["asset_id"])),
                )
                for item in _mapping_sequence(raw["skill_refs"], {"scope", "asset_id"})
            )
            payload = AgentPayload(
                description=_string(raw["description"]),
                agents_instructions=_string(raw["agents_instructions"]),
                soul=_string(raw["soul"]),
                identity=_string(raw["identity"]),
                user_context=_string(raw["user_context"]),
                model_ref=_string(raw["model_ref"]),
                model_settings=AgentModelSettings.model_validate(_mapping(raw["model_settings"])),
                tool_groups=_strings(raw["tool_groups"]),
                skill_refs=skill_refs,
                mcp_version_ids=_uuids(raw["mcp_version_ids"]),
                payload_schema_version=schema_version,
            )
            resolved_skill_ids = _uuids(raw["resolved_skill_version_ids"])
            if not resolved_agent_payload_checksum_matches(
                payload,
                checksum,
                skill_version_ids=resolved_skill_ids,
            ):
                raise RunAssetSnapshotInvalid("Run Agent snapshot checksum is invalid")
            return ResolvedAgentSnapshot(
                **common,
                payload=payload,
                skill_version_ids=resolved_skill_ids,
                slug=_string(raw["slug"]),
                source_key=(None if raw["source_key"] is None else _string(raw["source_key"])),
            )
        if kind is AssetKind.SKILL:
            raw = _mapping(value["skill"])
            if schema_version == _LEGACY_RUN_ASSET_SNAPSHOT_SCHEMA_VERSION:
                files = _decode_legacy_skill_files(raw)
            else:
                files = _decode_compressed_skill_files(value, raw)
            if _skill_checksum(files) != checksum:
                raise RunAssetSnapshotInvalid("Run Skill snapshot checksum is invalid")
            requirements = _decode_skill_requirements(raw["secret_requirements"])
            return ResolvedSkillSnapshot(
                **common,
                files=files,
                secret_requirements=requirements,
            )
        raw = _mapping(value["mcp"])
        if set(raw) != {"definition", "secret_generation_ids", "secret_digest"}:
            raise RunAssetSnapshotInvalid("Run MCP snapshot shape is invalid")
        definition = _mapping(raw["definition"])
        if _mcp_checksum(definition) != checksum:
            raise RunAssetSnapshotInvalid("Run MCP snapshot checksum is invalid")
        return ResolvedMcpSnapshot(
            **common,
            definition=_freeze_json(definition),
            secret_generation_ids=_uuids(raw["secret_generation_ids"]),
            secret_digest=_digest(raw["secret_digest"]),
        )
    except RunAssetSnapshotInvalid:
        raise
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise RunAssetSnapshotInvalid("Run asset snapshot is invalid") from error


def _validated_skill_files(
    value: Sequence[SkillArchiveFile],
) -> tuple[tuple[SkillArchiveFile, ...], int]:
    if isinstance(value, (str, bytes, bytearray)):
        raise RunAssetSnapshotInvalid("Run Skill files are invalid")
    files = tuple(value)
    if not files or len(files) > MAX_SKILL_ARCHIVE_FILES:
        raise RunAssetSnapshotInvalid("Run Skill file count is invalid")
    if any(type(item) is not SkillArchiveFile or type(item.content) is not bytes or not isinstance(item.path, str) or not isinstance(item.media_type, str) for item in files):
        raise RunAssetSnapshotInvalid("Run Skill file is invalid")
    if files != tuple(sorted(files, key=lambda item: item.path)):
        raise RunAssetSnapshotInvalid("Run Skill file order is not canonical")

    paths: set[str] = set()
    filesystem_identities: set[str] = set()
    total_size = 0
    for item in files:
        path = item.path
        media_type = item.media_type
        canonical_path = _canonical_skill_path(path)
        filesystem_identity = unicodedata.normalize("NFC", path.casefold())
        if canonical_path != path or len(path.encode("utf-8")) > _MAX_SKILL_PATH_BYTES or path in paths or filesystem_identity in filesystem_identities:
            raise RunAssetSnapshotInvalid("Run Skill file path is invalid")
        if not isinstance(media_type, str) or not media_type or media_type != media_type.strip() or len(media_type) > _MAX_SKILL_MEDIA_TYPE_CHARS or len(media_type.encode("utf-8")) > _MAX_SKILL_MEDIA_TYPE_BYTES:
            raise RunAssetSnapshotInvalid("Run Skill media type is invalid")
        size = len(item.content)
        total_size += size
        if size > MAX_SKILL_ARCHIVE_FILE_BYTES or total_size > MAX_SKILL_ARCHIVE_BYTES:
            raise RunAssetSnapshotInvalid("Run Skill file content is too large")
        paths.add(path)
        filesystem_identities.add(filesystem_identity)
    for identity in filesystem_identities:
        parts = PurePosixPath(identity).parts
        if any(PurePosixPath(*parts[:index]).as_posix() in filesystem_identities for index in range(1, len(parts))):
            raise RunAssetSnapshotInvalid("Run Skill file path is invalid")
    return files, total_size


def _canonical_skill_path(raw_path: str) -> str:
    windows_path = PureWindowsPath(raw_path)
    posix_path = raw_path.replace("\\", "/")
    if not raw_path or "\x00" in raw_path or ":" in raw_path or windows_path.drive or windows_path.is_absolute() or posix_path.startswith("/") or ".." in PurePosixPath(posix_path).parts:
        raise RunAssetSnapshotInvalid("Run Skill file path is invalid")
    normalized = unicodedata.normalize(
        "NFC",
        posixpath.normpath(posix_path).removeprefix("./"),
    )
    if not normalized or normalized == "." or len(normalized) > _MAX_SKILL_PATH_CHARS:
        raise RunAssetSnapshotInvalid("Run Skill file path is invalid")
    return normalized


def _compress_skill_frame(
    files: tuple[SkillArchiveFile, ...],
    *,
    content_size: int,
) -> tuple[bytes, int]:
    output = io.BytesIO()
    compressor = zlib.compressobj(_SKILL_ARCHIVE_COMPRESSION_LEVEL)
    uncompressed_size = 0

    def feed(value: bytes) -> None:
        nonlocal uncompressed_size
        uncompressed_size += len(value)
        output.write(compressor.compress(value))
        if output.tell() > _max_compressed_skill_archive_bytes():
            raise RunAssetSnapshotTooLarge(
                "Run Skill snapshot exceeds the encoded JSON size limit",
            )

    feed(
        _SKILL_FRAME_HEADER.pack(
            _SKILL_FRAME_MAGIC,
            len(files),
            content_size,
        )
    )
    for item in files:
        path = item.path.encode("utf-8")
        media_type = item.media_type.encode("utf-8")
        feed(_SKILL_FILE_HEADER.pack(len(path), len(media_type), len(item.content)))
        feed(path)
        feed(media_type)
        feed(item.content)
    output.write(compressor.flush())
    compressed = output.getvalue()
    if len(compressed) > _max_compressed_skill_archive_bytes():
        raise RunAssetSnapshotTooLarge(
            "Run Skill snapshot exceeds the encoded JSON size limit",
        )
    return compressed, uncompressed_size


def _decode_legacy_skill_files(
    raw: Mapping[str, object],
) -> tuple[SkillArchiveFile, ...]:
    if set(raw) != {"files", "secret_requirements"}:
        raise RunAssetSnapshotInvalid("Run Skill snapshot shape is invalid")
    raw_files = raw["files"]
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_SKILL_ARCHIVE_FILES:
        raise RunAssetSnapshotInvalid("Run Skill file count is invalid")
    items = _mapping_sequence(
        raw_files,
        {"path", "media_type", "content_base64"},
    )
    encoded_files: list[tuple[str, str, str]] = []
    declared_total_size = 0
    for item in items:
        encoded = _string(item["content_base64"])
        decoded_size = _base64_decoded_size(encoded)
        declared_total_size += decoded_size
        if decoded_size > MAX_SKILL_ARCHIVE_FILE_BYTES or declared_total_size > MAX_SKILL_ARCHIVE_BYTES:
            raise RunAssetSnapshotInvalid("Run Skill file content is too large")
        encoded_files.append(
            (
                _string(item["path"]),
                _string(item["media_type"]),
                encoded,
            )
        )
    files = tuple(
        SkillArchiveFile(
            path=path,
            media_type=media_type,
            content=base64.b64decode(
                encoded,
                validate=True,
            ),
        )
        for path, media_type, encoded in encoded_files
    )
    validated, actual_total_size = _validated_skill_files(files)
    if actual_total_size != declared_total_size:
        raise RunAssetSnapshotInvalid("Run Skill file content size is invalid")
    return validated


def _base64_decoded_size(encoded: str) -> int:
    if not encoded.isascii():
        raise RunAssetSnapshotInvalid("Run Skill snapshot base64 is invalid") from None
    if len(encoded) % 4 != 0:
        raise RunAssetSnapshotInvalid("Run Skill snapshot base64 is invalid")
    padding = len(encoded) - len(encoded.rstrip("="))
    if padding > 2 or "=" in encoded[: len(encoded) - padding]:
        raise RunAssetSnapshotInvalid("Run Skill snapshot base64 is invalid")
    return (len(encoded) // 4) * 3 - padding


def _decode_compressed_skill_files(
    snapshot: Mapping[str, object],
    raw: Mapping[str, object],
) -> tuple[SkillArchiveFile, ...]:
    if set(raw) != {
        "archive_base64",
        "codec",
        "compressed_size",
        "content_size",
        "file_count",
        "secret_requirements",
        "uncompressed_size",
    }:
        raise RunAssetSnapshotInvalid("Run Skill snapshot shape is invalid")
    if raw["codec"] != _SKILL_ARCHIVE_CODEC:
        raise RunAssetSnapshotInvalid("Run Skill snapshot codec is unsupported")

    file_count = _bounded_int(
        raw["file_count"],
        minimum=1,
        maximum=MAX_SKILL_ARCHIVE_FILES,
    )
    content_size = _bounded_int(
        raw["content_size"],
        minimum=0,
        maximum=MAX_SKILL_ARCHIVE_BYTES,
    )
    uncompressed_size = _bounded_int(
        raw["uncompressed_size"],
        minimum=_SKILL_FRAME_HEADER.size,
        maximum=_max_skill_frame_bytes(),
    )
    compressed_size = _bounded_int(
        raw["compressed_size"],
        minimum=1,
        maximum=_max_compressed_skill_archive_bytes(),
    )
    encoded = _string(raw["archive_base64"])
    if len(encoded) != ((compressed_size + 2) // 3) * 4:
        raise RunAssetSnapshotInvalid("Run Skill snapshot base64 size is invalid")
    if encoded_run_asset_snapshot_json_size(snapshot) > MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES:
        raise RunAssetSnapshotTooLarge(
            "Run Skill snapshot exceeds the encoded JSON size limit",
        )
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise RunAssetSnapshotInvalid("Run Skill snapshot base64 is invalid") from None
    if len(compressed) != compressed_size:
        raise RunAssetSnapshotInvalid("Run Skill compressed size is invalid")

    decompressor = zlib.decompressobj()
    try:
        frame = decompressor.decompress(compressed, uncompressed_size + 1)
    except zlib.error:
        raise RunAssetSnapshotInvalid("Run Skill compressed archive is invalid") from None
    if len(frame) != uncompressed_size or not decompressor.eof or decompressor.unconsumed_tail or decompressor.unused_data:
        raise RunAssetSnapshotInvalid("Run Skill uncompressed size is invalid")
    return _parse_skill_frame(
        frame,
        declared_file_count=file_count,
        declared_content_size=content_size,
    )


def _parse_skill_frame(
    frame: bytes,
    *,
    declared_file_count: int,
    declared_content_size: int,
) -> tuple[SkillArchiveFile, ...]:
    if len(frame) < _SKILL_FRAME_HEADER.size:
        raise RunAssetSnapshotInvalid("Run Skill archive header is invalid")
    magic, file_count, content_size = _SKILL_FRAME_HEADER.unpack_from(frame)
    if magic != _SKILL_FRAME_MAGIC or file_count != declared_file_count or content_size != declared_content_size:
        raise RunAssetSnapshotInvalid("Run Skill archive declaration is invalid")

    view = memoryview(frame)
    offset = _SKILL_FRAME_HEADER.size
    files: list[SkillArchiveFile] = []
    actual_content_size = 0
    for _index in range(file_count):
        if offset + _SKILL_FILE_HEADER.size > len(view):
            raise RunAssetSnapshotInvalid("Run Skill archive is truncated")
        path_size, media_type_size, file_size = _SKILL_FILE_HEADER.unpack_from(
            view,
            offset,
        )
        offset += _SKILL_FILE_HEADER.size
        if path_size < 1 or path_size > _MAX_SKILL_PATH_BYTES or media_type_size < 1 or media_type_size > _MAX_SKILL_MEDIA_TYPE_BYTES or file_size > MAX_SKILL_ARCHIVE_FILE_BYTES:
            raise RunAssetSnapshotInvalid("Run Skill archive member is invalid")
        member_end = offset + path_size + media_type_size + file_size
        if member_end > len(view):
            raise RunAssetSnapshotInvalid("Run Skill archive is truncated")
        try:
            path = bytes(view[offset : offset + path_size]).decode("utf-8")
            offset += path_size
            media_type = bytes(
                view[offset : offset + media_type_size],
            ).decode("utf-8")
            offset += media_type_size
        except UnicodeDecodeError:
            raise RunAssetSnapshotInvalid("Run Skill archive text is invalid") from None
        content = bytes(view[offset:member_end])
        offset = member_end
        actual_content_size += len(content)
        if actual_content_size > MAX_SKILL_ARCHIVE_BYTES:
            raise RunAssetSnapshotInvalid("Run Skill archive content is too large")
        files.append(SkillArchiveFile(path, content, media_type))
    if offset != len(view) or actual_content_size != content_size:
        raise RunAssetSnapshotInvalid("Run Skill archive size is invalid")
    validated, validated_content_size = _validated_skill_files(files)
    if validated_content_size != content_size:
        raise RunAssetSnapshotInvalid("Run Skill archive content size is invalid")
    return validated


def _decode_skill_requirements(
    value: object,
) -> tuple[SkillSecretRequirementSnapshot, ...]:
    requirements: list[SkillSecretRequirementSnapshot] = []
    for item in _mapping_sequence(
        value,
        {"name", "target_env", "optional"},
    ):
        if not isinstance(item["optional"], bool):
            raise RunAssetSnapshotInvalid(
                "Run Skill secret requirement is invalid",
            )
        requirements.append(
            SkillSecretRequirementSnapshot(
                name=_string(item["name"]),
                target_env=_string(item["target_env"]),
                optional=item["optional"],
            )
        )
    return tuple(requirements)


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise RunAssetSnapshotInvalid("Run Skill snapshot size is invalid")
    return value


def _max_compressed_skill_archive_bytes() -> int:
    # Base64 expands bytes by 4/3.  This is an early bound; the exact final
    # JSON size is checked after the complete snapshot object is assembled.
    return (MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES // 4) * 3


def _max_skill_frame_bytes() -> int:
    return _SKILL_FRAME_HEADER.size + MAX_SKILL_ARCHIVE_BYTES + MAX_SKILL_ARCHIVE_FILES * (_SKILL_FILE_HEADER.size + _MAX_SKILL_PATH_BYTES + _MAX_SKILL_MEDIA_TYPE_BYTES)


def encoded_run_asset_snapshot_json_size(value: Mapping[str, object]) -> int:
    """Return the persistence JSON byte size without copying a large blob."""

    skill = value.get(AssetKind.SKILL.value)
    if value.get("schema_version") == RUN_ASSET_SNAPSHOT_SCHEMA_VERSION and value.get("kind") == AssetKind.SKILL.value and isinstance(skill, Mapping) and isinstance(skill.get("archive_base64"), str):
        encoded_archive = skill["archive_base64"]
        compact_skill = dict(skill)
        compact_skill["archive_base64"] = ""
        compact_snapshot = dict(value)
        compact_snapshot[AssetKind.SKILL.value] = compact_skill
        fixed_size = len(
            json.dumps(
                compact_snapshot,
                ensure_ascii=False,
            ).encode()
        )
        # Base64 contains no JSON escape characters, so replacing the empty
        # string adds exactly one byte for every encoded character.
        return fixed_size + len(encoded_archive)
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
        ).encode()
    )


def _skill_checksum(files: Sequence[SkillArchiveFile]) -> str:
    canonical = json.dumps(
        [
            {
                "path": item.path,
                "sha256": hashlib.sha256(item.content).hexdigest(),
                "size_bytes": len(item.content),
            }
            for item in files
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _mcp_checksum(definition: Mapping[str, object]) -> str:
    expected = {
        "args",
        "command",
        "secret_slots",
        "description",
        "env",
        "headers",
        "oauth",
        "routing",
        "timeout_seconds",
        "tool_overrides",
        "transport",
        "url",
    }
    if set(definition) != expected:
        raise RunAssetSnapshotInvalid("Run MCP definition shape is invalid")
    slots = _mapping_sequence(
        definition["secret_slots"],
        {"name", "payload_schema", "purpose", "required"},
    )
    canonical = {
        "args": list(_strings(definition["args"])),
        "command": definition["command"],
        "secret_slots": [
            {
                "name": _string(slot["name"]),
                "payload_schema": _mapping(slot["payload_schema"]),
                "purpose": _string(slot["purpose"]),
                "required": slot["required"],
            }
            for slot in slots
        ],
        "description": _string(definition["description"]),
        "env": _mapping(definition["env"]),
        "headers": _mapping(definition["headers"]),
        "oauth": _mapping(definition["oauth"]),
        "routing": _mapping(definition["routing"]),
        "timeout_seconds": definition["timeout_seconds"],
        "tool_overrides": _mapping(definition["tool_overrides"]),
        "transport": _string(definition["transport"]),
        "url": definition["url"],
    }
    if (
        (canonical["command"] is not None and not isinstance(canonical["command"], str))
        or (canonical["url"] is not None and not isinstance(canonical["url"], str))
        or not isinstance(canonical["timeout_seconds"], int)
        or isinstance(canonical["timeout_seconds"], bool)
        or any(not isinstance(slot["required"], bool) for slot in canonical["secret_slots"])
    ):
        raise RunAssetSnapshotInvalid("Run MCP definition is invalid")
    encoded = json.dumps(
        canonical,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    raise RunAssetSnapshotInvalid("Run snapshot contains a non-JSON value")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return tuple(_freeze_json(item) for item in value)
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise RunAssetSnapshotInvalid("Run snapshot object is invalid")
    return value


def _mapping_sequence(
    value: object,
    keys: set[str],
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise RunAssetSnapshotInvalid("Run snapshot list is invalid")
    result = tuple(_mapping(item) for item in value)
    if any(set(item) != keys for item in result):
        raise RunAssetSnapshotInvalid("Run snapshot list item is invalid")
    return result


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise RunAssetSnapshotInvalid("Run snapshot string is invalid")
    return value


def _digest(value: object) -> str:
    result = _string(value)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise RunAssetSnapshotInvalid("Run snapshot digest is invalid")
    return result


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RunAssetSnapshotInvalid("Run snapshot string list is invalid")
    return tuple(value)


def _uuids(value: object) -> tuple[uuid.UUID, ...]:
    if not isinstance(value, list):
        raise RunAssetSnapshotInvalid("Run snapshot UUID list is invalid")
    return tuple(uuid.UUID(str(item)) for item in value)
