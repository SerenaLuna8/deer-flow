from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import re
import tempfile
import unicodedata
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TypeVar

import yaml
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.models import AssetScope, SkillArchiveFile, WorkflowStatus
from app.shared_assets.skill_repository import SkillRepository, SkillVersionRecord
from deerflow.persistence.shared_assets import SkillRow, SkillVersionFileRow, SkillVersionRow
from deerflow.skills.parser import parse_skill_file
from deerflow.skills.skillscan import (
    StaticScanBlockedError,
    StaticScannerError,
    enforce_static_scan_result,
)
from deerflow.skills.types import SkillCategory
from deerflow.skills.validation import _validate_skill_frontmatter

MAX_SKILL_ARCHIVE_BYTES = 100 * 1024 * 1024
_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ENV_VAR_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
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
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "uq_skills_project_slug",
        "uq_skills_system_slug",
        "uq_skill_versions_asset_number",
    }
)
_Actor = ProjectContext | SystemAssetGovernanceContext
_T = TypeVar("_T")


@dataclass(frozen=True)
class _SkillScanConfig:
    enabled: bool = True


@dataclass(frozen=True)
class _M3SkillScanConfig:
    skill_scan: _SkillScanConfig = _SkillScanConfig()


_M3_SKILL_SCAN_CONFIG = _M3SkillScanConfig()


class _DuplicateKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects shadowed keys at every mapping level."""


def _construct_unique_mapping(
    loader: _DuplicateKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
):
    loader.flatten_mapping(node)
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "unhashable mapping key",
                key_node.start_mark,
            ) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "duplicate mapping key",
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.constructor.BaseConstructor.construct_mapping(loader, node, deep=deep)


_DuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str):
            return name
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None


@dataclass(frozen=True)
class CreateSkill:
    slug: str
    display_name: str


@dataclass(frozen=True)
class SkillFileView:
    path: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class SkillSecretRequirementView:
    name: str
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
    scan_decision: str
    scan_rule_ids: tuple[str, ...]
    scan_summary: Mapping[str, object]


@dataclass(frozen=True)
class SkillAssetView:
    id: uuid.UUID
    scope: AssetScope
    project_id: uuid.UUID | None
    slug: str
    display_name: str
    status: str
    current_published_version_id: uuid.UUID | None
    version: int
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SkillVersionView:
    id: uuid.UUID
    skill_id: uuid.UUID
    version_number: int
    workflow_status: WorkflowStatus
    description: str
    frontmatter: Mapping[str, object]
    compatibility: str | None
    secret_requirements: tuple[SkillSecretRequirementView, ...]
    scan_decision: str
    scan_rule_ids: tuple[str, ...]
    scan_summary: Mapping[str, object]
    file_views: tuple[SkillFileView, ...]
    supersedes_version_id: uuid.UUID | None
    payload_checksum: str
    created_by_user_id: str
    created_at: datetime


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
    canonical = json.dumps(
        [
            {
                "path": item.path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in file_views
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _preflight_skill_frontmatter(
    skill_file: Path,
    request_id: str,
) -> tuple[dict[str, object], tuple[tuple[str, bool], ...]]:
    manifest_text = skill_file.read_text(encoding="utf-8")
    match = _FRONTMATTER_PATTERN.match(manifest_text)
    if match is None:
        raise AssetValidationFailed(request_id)
    frontmatter = yaml.load(match.group(1), Loader=_DuplicateKeySafeLoader)
    if not isinstance(frontmatter, dict) or any(not isinstance(key, str) for key in frontmatter):
        raise AssetValidationFailed(request_id)

    raw_requirements = frontmatter.get("required-secrets")
    canonical_requirements: list[tuple[str, bool]] = []
    if "required-secrets" in frontmatter:
        if not isinstance(raw_requirements, list):
            raise AssetValidationFailed(request_id)
        for item in raw_requirements:
            if isinstance(item, str):
                name = item.strip()
                optional = False
            elif isinstance(item, dict) and all(isinstance(key, str) for key in item):
                if not set(item).issubset({"name", "optional"}):
                    raise AssetValidationFailed(request_id)
                raw_name = item.get("name")
                optional = item.get("optional", False)
                if not isinstance(raw_name, str) or not isinstance(optional, bool):
                    raise AssetValidationFailed(request_id)
                name = raw_name.strip()
            else:
                raise AssetValidationFailed(request_id)
            if _ENV_VAR_NAME_PATTERN.fullmatch(name) is None:
                raise AssetValidationFailed(request_id)
            canonical_requirements.append((name, optional))
    if len({name for name, _ in canonical_requirements}) != len(canonical_requirements):
        raise AssetValidationFailed(request_id)

    secrets_autonomous = frontmatter.get("secrets-autonomous")
    if "secrets-autonomous" in frontmatter and not isinstance(secrets_autonomous, bool):
        raise AssetValidationFailed(request_id)
    return frontmatter, tuple(canonical_requirements)


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

            parsed_requirements = tuple((requirement.name, requirement.optional) for requirement in parsed.required_secrets)
            if canonical_requirements != parsed_requirements:
                raise AssetValidationFailed(request_id)

            requirement_views = tuple(SkillSecretRequirementView(name=requirement.name, optional=requirement.optional) for requirement in parsed.required_secrets)
            sanitized_frontmatter = dict(frontmatter)
            if "required-secrets" in sanitized_frontmatter:
                sanitized_frontmatter["required-secrets"] = [{"name": requirement.name, "optional": requirement.optional} for requirement in requirement_views]
            compatibility = sanitized_frontmatter.get("compatibility")
            if compatibility is not None and (not isinstance(compatibility, str) or len(compatibility) > 255):
                raise AssetValidationFailed(request_id)
            compatibility = compatibility.strip() if isinstance(compatibility, str) else None
            try:
                sanitized_frontmatter = json.loads(json.dumps(sanitized_frontmatter, ensure_ascii=False))
            except (TypeError, ValueError, RecursionError):
                raise AssetValidationFailed(request_id) from None

            scan_result = enforce_static_scan_result(
                root,
                skill_name=parsed.name,
                app_config=_M3_SKILL_SCAN_CONFIG,
            )
            if scan_result["scanner_errors"]:
                raise AssetValidationFailed(request_id)
            findings = scan_result["findings"]
    except AssetValidationFailed:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, StaticScanBlockedError, StaticScannerError, ValueError):
        raise AssetValidationFailed(request_id) from None

    views = _file_views(files)
    rule_ids = tuple(sorted({finding["rule_id"] for finding in findings}))
    severity_counts = dict(sorted(Counter(finding["severity"] for finding in findings).items()))
    decision = "warn" if findings else "allow"
    scan_summary: dict[str, object] = {
        "rule_ids": list(rule_ids),
        "severity_counts": severity_counts,
    }
    return SkillArchivePreview(
        checksum=_snapshot_checksum(views),
        files=files,
        file_views=views,
        description=parsed.description,
        frontmatter=sanitized_frontmatter,
        compatibility=compatibility,
        secret_requirements=requirement_views,
        scan_decision=decision,
        scan_rule_ids=rule_ids,
        scan_summary=scan_summary,
    )


class SkillService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        governance_sink: SharedAssetGovernanceEventSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()

    async def preview_archive(
        self,
        actor: _Actor,
        files: Sequence[SkillArchiveFile],
    ) -> SkillArchivePreview:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        request_id = getattr(actor, "request_id", "unknown")
        normalized = normalize_skill_files(files, request_id=request_id)
        return await asyncio.to_thread(_analyze_skill_files, normalized, request_id)

    async def create_asset(self, actor: _Actor, command: CreateSkill) -> SkillAssetView:
        command = self._validate_create(actor, command)
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: SkillRepository) -> SkillAssetView:
            if isinstance(actor, ProjectContext):
                row = await repository.create_project_asset(actor, command)
            else:
                row = await repository.create_system_asset(actor, command)
            return self._asset_view(row)

        result = await self._execute(actor, operation)
        self._record_governance(actor, result.id, None, "skill.create")
        return result

    async def create_version_from_archive(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        files: Sequence[SkillArchiveFile],
        *,
        expected_asset_version: int,
    ) -> SkillVersionView:
        preview = await self.preview_archive(actor, files)

        async def operation(repository: SkillRepository) -> SkillVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "active":
                raise AssetConflict(actor.request_id)
            if isinstance(actor, ProjectContext):
                version_number = await repository.next_project_version_number(actor, asset)
            else:
                version_number = await repository.next_system_version_number(actor, asset)
            version_id = uuid.uuid4()
            row = SkillVersionRow(
                id=version_id,
                skill_id=asset.id,
                version_number=version_number,
                workflow_status=WorkflowStatus.DRAFT.value,
                description=preview.description,
                frontmatter=dict(preview.frontmatter),
                compatibility=preview.compatibility,
                secret_requirements=[{"name": requirement.name, "optional": requirement.optional} for requirement in preview.secret_requirements],
                scan_decision=preview.scan_decision,
                scan_summary=dict(preview.scan_summary),
                supersedes_version_id=asset.current_published_version_id,
                payload_checksum=preview.checksum,
                created_by_user_id=str(actor.user_id),
            )
            file_rows = tuple(
                SkillVersionFileRow(
                    skill_version_id=version_id,
                    path=item.path,
                    media_type=item.media_type,
                    size_bytes=len(item.content),
                    sha256=file_view.sha256,
                    content=item.content,
                )
                for item, file_view in zip(preview.files, preview.file_views, strict=True)
            )
            if isinstance(actor, ProjectContext):
                record = await repository.create_project_version(actor, asset.id, row, file_rows)
            else:
                record = await repository.create_system_version(actor, asset.id, row, file_rows)
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        result = await self._execute(actor, operation)
        self._record_governance(actor, asset_id, result.id, "skill.version.create")
        return result

    async def publish(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> SkillVersionView:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)

        async def operation(repository: SkillRepository) -> SkillVersionView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status != "active":
                raise AssetConflict(actor.request_id)
            record = await self._get_version(repository, actor, asset_id, version_id, for_update=True)
            if record.row.workflow_status != WorkflowStatus.DRAFT.value:
                raise AssetConflict(actor.request_id)
            files = await asyncio.to_thread(
                self._archive_files,
                record,
                actor.request_id,
            )
            current = await asyncio.to_thread(_analyze_skill_files, files, actor.request_id)
            expected_requirements = [{"name": requirement.name, "optional": requirement.optional} for requirement in current.secret_requirements]
            if (
                current.checksum != record.row.payload_checksum
                or current.description != record.row.description
                or dict(current.frontmatter) != record.row.frontmatter
                or current.compatibility != record.row.compatibility
                or expected_requirements != record.row.secret_requirements
                or current.scan_decision != record.row.scan_decision
                or dict(current.scan_summary) != record.row.scan_summary
            ):
                raise AssetValidationFailed(actor.request_id)
            record.row.workflow_status = WorkflowStatus.PUBLISHED.value
            asset.current_published_version_id = record.row.id
            asset.version += 1
            await repository.session.flush()
            return self._version_view(record)

        result = await self._execute(actor, operation)
        self._record_governance(actor, asset_id, version_id, "skill.publish")
        return result

    async def archive(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> SkillAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_EDIT)
        result = await self._change_status(actor, asset_id, expected_asset_version=expected_asset_version, status="archived")
        self._record_governance(actor, asset_id, None, "skill.archive")
        return result

    async def suspend(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
    ) -> SkillAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_MANAGE_BINDINGS)
        result = await self._change_status(actor, asset_id, expected_asset_version=expected_asset_version, status="suspended")
        self._record_governance(actor, asset_id, None, "skill.suspend")
        return result

    async def get(self, actor: _Actor, asset_id: uuid.UUID) -> SkillAssetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: SkillRepository) -> SkillAssetView:
            return self._asset_view(await self._get_asset(repository, actor, asset_id))

        return await self._execute(actor, operation)

    async def list_visible(self, actor: _Actor) -> tuple[SkillAssetView, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: SkillRepository) -> tuple[SkillAssetView, ...]:
            if isinstance(actor, ProjectContext):
                rows = await repository.list_project_visible(actor)
            else:
                rows = await repository.list_system_visible(actor)
            return tuple(self._asset_view(row) for row in rows)

        return await self._execute(actor, operation)

    async def get_version_history(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
    ) -> tuple[SkillVersionView, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: SkillRepository) -> tuple[SkillVersionView, ...]:
            if isinstance(actor, ProjectContext):
                records = await repository.get_project_version_history(actor, asset_id)
            else:
                records = await repository.get_system_version_history(actor, asset_id)
            return tuple(self._version_view(record) for record in records)

        return await self._execute(actor, operation)

    async def load_version_files(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> tuple[SkillArchiveFile, ...]:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)

        async def operation(repository: SkillRepository) -> tuple[SkillArchiveFile, ...]:
            if isinstance(actor, ProjectContext):
                record = await repository.load_project_version(actor, asset_id, version_id)
            elif isinstance(actor, SystemAssetGovernanceContext):
                record = await repository.load_system_version(actor, asset_id, version_id)
            else:
                raise AssetForbidden("unknown")
            return await asyncio.to_thread(
                self._verified_archive_files,
                record,
                actor.request_id,
            )

        return await self._execute(actor, operation)

    async def _change_status(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        expected_asset_version: int,
        status: str,
    ) -> SkillAssetView:
        async def operation(repository: SkillRepository) -> SkillAssetView:
            asset = await self._get_asset(repository, actor, asset_id, for_update=True)
            self._require_expected_version(actor, asset, expected_asset_version)
            if asset.status == status:
                raise AssetConflict(actor.request_id)
            asset.status = status
            asset.version += 1
            await repository.session.flush()
            return self._asset_view(asset)

        return await self._execute(actor, operation)

    async def _execute(
        self,
        actor: _Actor,
        operation: Callable[[SkillRepository], Awaitable[_T]],
    ) -> _T:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    return await operation(SkillRepository(session))
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(getattr(actor, "request_id", "unknown")) from None
            raise AssetStorageUnavailable(getattr(actor, "request_id", "unknown")) from None
        except DBAPIError:
            raise AssetStorageUnavailable(getattr(actor, "request_id", "unknown")) from None

    @staticmethod
    async def _get_asset(
        repository: SkillRepository,
        actor: _Actor,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillRow:
        if isinstance(actor, ProjectContext):
            return await repository.get_project_asset(actor, asset_id, for_update=for_update)
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.get_system_asset(actor, asset_id, for_update=for_update)
        raise AssetForbidden("unknown")

    @staticmethod
    async def _get_version(
        repository: SkillRepository,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillVersionRecord:
        if isinstance(actor, ProjectContext):
            return await repository.get_project_version(actor, asset_id, version_id, for_update=for_update)
        if isinstance(actor, SystemAssetGovernanceContext):
            return await repository.get_system_version(actor, asset_id, version_id, for_update=for_update)
        raise AssetForbidden("unknown")

    @staticmethod
    def _validate_create(actor: _Actor, command: CreateSkill) -> CreateSkill:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(command, CreateSkill):
            raise AssetValidationFailed(request_id)
        slug = command.slug.strip()
        display_name = command.display_name.strip()
        if _SLUG_PATTERN.fullmatch(slug) is None or not display_name or len(display_name) > 120:
            raise AssetValidationFailed(request_id)
        return CreateSkill(slug=slug, display_name=display_name)

    @staticmethod
    def _require_capability(actor: _Actor, capability: Capability) -> None:
        if isinstance(actor, SystemAssetGovernanceContext):
            return
        if isinstance(actor, ProjectContext) and capability in actor.capabilities:
            return
        raise AssetForbidden(getattr(actor, "request_id", "unknown"))

    @staticmethod
    def _require_expected_version(actor: _Actor, asset: SkillRow, expected: int) -> None:
        if not isinstance(expected, int) or isinstance(expected, bool) or asset.version != expected:
            raise AssetConflict(actor.request_id)

    @staticmethod
    def _archive_files(
        record: SkillVersionRecord,
        request_id: str,
    ) -> tuple[SkillArchiveFile, ...]:
        files: list[SkillArchiveFile] = []
        for row in record.files:
            if row.size_bytes != len(row.content) or row.sha256 != hashlib.sha256(row.content).hexdigest():
                raise AssetValidationFailed(request_id)
            files.append(SkillArchiveFile(path=row.path, content=bytes(row.content), media_type=row.media_type))
        snapshot = tuple(files)
        normalized = normalize_skill_files(snapshot, request_id=request_id)
        if normalized != snapshot:
            raise AssetValidationFailed(request_id)
        return normalized

    @staticmethod
    def _verified_archive_files(
        record: SkillVersionRecord,
        request_id: str,
    ) -> tuple[SkillArchiveFile, ...]:
        files = SkillService._archive_files(record, request_id)
        if _snapshot_checksum(_file_views(files)) != record.row.payload_checksum:
            raise AssetValidationFailed(request_id)
        return files

    @staticmethod
    def _asset_view(row: SkillRow) -> SkillAssetView:
        return SkillAssetView(
            id=row.id,
            scope=AssetScope(row.scope),
            project_id=row.project_id,
            slug=row.slug,
            display_name=row.display_name,
            status=row.status,
            current_published_version_id=row.current_published_version_id,
            version=row.version,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _version_view(record: SkillVersionRecord) -> SkillVersionView:
        row = record.row
        requirements = tuple(SkillSecretRequirementView(name=str(item["name"]), optional=bool(item.get("optional", False))) for item in row.secret_requirements if isinstance(item, dict) and isinstance(item.get("name"), str))
        file_views = tuple(SkillFileView(path=file.path, media_type=file.media_type, size_bytes=file.size_bytes, sha256=file.sha256) for file in record.files)
        rule_ids = row.scan_summary.get("rule_ids", [])
        return SkillVersionView(
            id=row.id,
            skill_id=row.skill_id,
            version_number=row.version_number,
            workflow_status=WorkflowStatus(row.workflow_status),
            description=row.description,
            frontmatter=dict(row.frontmatter),
            compatibility=row.compatibility,
            secret_requirements=requirements,
            scan_decision=row.scan_decision,
            scan_rule_ids=tuple(str(rule_id) for rule_id in rule_ids if isinstance(rule_id, str)),
            scan_summary=dict(row.scan_summary),
            file_views=file_views,
            supersedes_version_id=row.supersedes_version_id,
            payload_checksum=row.payload_checksum,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
        )

    def _record_governance(
        self,
        actor: _Actor,
        asset_id: uuid.UUID,
        version_id: uuid.UUID | None,
        action: str,
    ) -> None:
        if not isinstance(actor, SystemAssetGovernanceContext):
            return
        self._governance_sink.write_override(
            actor=actor.user_id,
            project_id=actor.project_id,
            asset_id=asset_id,
            version_id=version_id,
            action=action,
            request_id=actor.request_id,
        )
