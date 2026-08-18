#!/usr/bin/env python3
"""显式校验并把文件系统 Skill 导入指定 PostgreSQL 项目。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import os
import stat
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.audit.service import AuditService
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext, resolve_project_context
from app.projects.errors import ProjectDatabaseUnavailable, ProjectForbidden, ProjectNotFound
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.quotas.system_policy import SystemQuotaPolicyReader
from app.reliability.owner_refs import AuditHmacKeyring, AuditHmacKeyringInvalid
from app.shared_assets.audit import DurableSharedAssetGovernanceEventSink
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageQuotaExceeded,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.skill_service import (
    MAX_PROJECT_SKILL_BATCH_BYTES,
    MAX_PROJECT_SKILL_BATCH_FILES,
    MAX_PROJECT_SKILL_BATCH_ITEMS,
    MAX_SKILL_ARCHIVE_BYTES,
    MAX_SKILL_ARCHIVE_FILES,
    ProjectSkillArchiveImport,
    SkillService,
)
from deerflow.config.database_config import DatabaseConfig
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.user.model import UserRow

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SOURCE_ROOT = _REPOSITORY_ROOT / "skills" / "public"
_READ_CHUNK_BYTES = 1024 * 1024
_MEDIA_TYPES = {
    ".css": "text/css",
    ".csv": "text/csv",
    ".html": "text/html",
    ".js": "text/javascript",
    ".json": "application/json",
    ".jsx": "text/jsx",
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".svg": "image/svg+xml",
    ".toml": "application/toml",
    ".ts": "text/typescript",
    ".tsx": "text/tsx",
    ".txt": "text/plain",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


class ProjectSkillImportError(RuntimeError):
    """不包含文件内容、连接信息或私有标识的导入失败。"""


@dataclass(frozen=True, slots=True)
class ProjectSkillSource:
    directory_name: str
    files: tuple[SkillArchiveFile, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProjectSkillImportSummary:
    mode: Literal["dry-run", "execute"]
    discovered_count: int
    planned_create_count: int
    planned_replace_count: int
    unchanged_count: int
    created_count: int
    replaced_count: int

    def as_json_object(self) -> dict[str, int | str]:
        return {
            "created_count": self.created_count,
            "discovered_count": self.discovered_count,
            "mode": self.mode,
            "planned_create_count": self.planned_create_count,
            "planned_replace_count": self.planned_replace_count,
            "replaced_count": self.replaced_count,
            "unchanged_count": self.unchanged_count,
        }


def _invalid_source_tree() -> ProjectSkillImportError:
    return ProjectSkillImportError("project Skill source tree is invalid")


def _directory_entries(directory: Path) -> tuple[os.DirEntry[str], ...]:
    try:
        with os.scandir(directory) as iterator:
            entries = tuple(sorted(iterator, key=lambda entry: entry.name))
    except OSError:
        raise _invalid_source_tree() from None
    if not entries:
        raise _invalid_source_tree()
    return entries


def _entry_mode(entry: os.DirEntry[str]) -> int:
    try:
        return entry.stat(follow_symlinks=False).st_mode
    except OSError:
        raise _invalid_source_tree() from None


def _read_regular_file(path: Path, *, expected_size: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _invalid_source_tree() from None
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or current.st_size != expected_size:
            raise _invalid_source_tree()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SKILL_ARCHIVE_BYTES:
                raise _invalid_source_tree()
            chunks.append(chunk)
        if total != expected_size:
            raise _invalid_source_tree()
        return b"".join(chunks)
    except OSError:
        raise _invalid_source_tree() from None
    finally:
        os.close(descriptor)


def _media_type(path: Path) -> str:
    selected = _MEDIA_TYPES.get(path.suffix.casefold())
    if selected is not None:
        return selected
    guessed, encoding = mimetypes.guess_type(path.name, strict=False)
    if encoding is not None:
        return "application/octet-stream"
    return guessed or "application/octet-stream"


def _walk_skill_directory(
    skill_root: Path,
    directory: Path,
    *,
    resolved_source_root: Path,
    max_files: int,
    max_bytes: int,
) -> tuple[SkillArchiveFile, ...]:
    files: list[SkillArchiveFile] = []
    total_bytes = 0
    stack = [directory]
    while stack:
        current = stack.pop()
        entries = _directory_entries(current)
        child_directories: list[Path] = []
        for entry in entries:
            mode = _entry_mode(entry)
            candidate = Path(entry.path)
            if stat.S_ISLNK(mode):
                raise _invalid_source_tree()
            if stat.S_ISDIR(mode):
                child_directories.append(candidate)
                continue
            if not stat.S_ISREG(mode):
                raise _invalid_source_tree()
            try:
                resolved_candidate = candidate.resolve(strict=True)
                if not resolved_candidate.is_relative_to(resolved_source_root):
                    raise _invalid_source_tree()
                relative_path = candidate.relative_to(skill_root).as_posix()
                size_bytes = entry.stat(follow_symlinks=False).st_size
            except (OSError, ValueError):
                raise _invalid_source_tree() from None
            if size_bytes < 0 or size_bytes > MAX_SKILL_ARCHIVE_BYTES:
                raise _invalid_source_tree()
            if len(files) >= min(MAX_SKILL_ARCHIVE_FILES, max_files):
                raise _invalid_source_tree()
            total_bytes += size_bytes
            if total_bytes > min(MAX_SKILL_ARCHIVE_BYTES, max_bytes):
                raise _invalid_source_tree()
            files.append(
                SkillArchiveFile(
                    path=relative_path,
                    content=_read_regular_file(candidate, expected_size=size_bytes),
                    media_type=_media_type(candidate),
                )
            )
        stack.extend(reversed(child_directories))
    return tuple(sorted(files, key=lambda item: item.path))


def load_project_skill_sources(source_root: Path | str) -> tuple[ProjectSkillSource, ...]:
    """读取一个显式 Skill 根目录；拒绝链接、特殊文件、空目录和不完整子目录。"""

    root = Path(source_root)
    try:
        root_mode = root.lstat().st_mode
        if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
            raise _invalid_source_tree()
        resolved_root = root.resolve(strict=True)
    except OSError:
        raise _invalid_source_tree() from None

    sources: list[ProjectSkillSource] = []
    total_files = 0
    total_bytes = 0
    for entry in _directory_entries(root):
        if len(sources) >= MAX_PROJECT_SKILL_BATCH_ITEMS:
            raise _invalid_source_tree()
        mode = _entry_mode(entry)
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise _invalid_source_tree()
        skill_root = Path(entry.path)
        manifest = skill_root / "SKILL.md"
        try:
            manifest_mode = manifest.lstat().st_mode
        except OSError:
            raise _invalid_source_tree() from None
        if stat.S_ISLNK(manifest_mode) or not stat.S_ISREG(manifest_mode):
            raise _invalid_source_tree()
        files = _walk_skill_directory(
            skill_root,
            skill_root,
            resolved_source_root=resolved_root,
            max_files=MAX_PROJECT_SKILL_BATCH_FILES - total_files,
            max_bytes=MAX_PROJECT_SKILL_BATCH_BYTES - total_bytes,
        )
        if not files:
            raise _invalid_source_tree()
        total_files += len(files)
        total_bytes += sum(len(file.content) for file in files)
        sources.append(ProjectSkillSource(directory_name=entry.name, files=files))
    return tuple(sources)


async def _resolve_actor(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_email: str,
    project_slug: str,
    request_id: str,
) -> ProjectContext:
    normalized_email = user_email.strip().lower()
    normalized_project_slug = project_slug.strip()
    if not normalized_email or len(normalized_email) > 320 or not normalized_project_slug:
        raise ProjectSkillImportError("project Skill import actor is unavailable")
    try:
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(UserRow.id).where(
                            func.lower(UserRow.email) == normalized_email,
                        )
                    )
                )
                .scalars()
                .all()
            )
        if len(rows) != 1:
            raise ProjectSkillImportError("project Skill import actor is unavailable")
        user_id = uuid.UUID(rows[0])
        async with factory() as session:
            actor = await resolve_project_context(
                session,
                user_id,
                normalized_project_slug,
                request_id,
            )
        actor.require(Capability.SHARED_ASSETS_EDIT)
        return actor
    except ProjectSkillImportError:
        raise
    except ProjectForbidden:
        raise ProjectSkillImportError("project Skill import actor is not allowed") from None
    except (ProjectNotFound, ValueError):
        raise ProjectSkillImportError("project Skill import actor is unavailable") from None
    except ProjectDatabaseUnavailable:
        raise ProjectSkillImportError("project Skill import database is unavailable") from None


async def _run_import(
    database_url: str,
    *,
    source_root: Path | str,
    user_email: str,
    project_slug: str,
    execute: bool,
    replace: bool,
    quota_config: QuotaConfig | None,
) -> ProjectSkillImportSummary:
    sources = load_project_skill_sources(source_root)
    try:
        database_config = DatabaseConfig(url=database_url)
    except ValidationError:
        raise ProjectSkillImportError("DATABASE_URL must be a PostgreSQL URL") from None
    engine = create_async_engine(database_config.sqlalchemy_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    request_id = f"project-skill-import-{uuid.uuid4()}"
    try:
        actor = await _resolve_actor(
            factory,
            user_email=user_email,
            project_slug=project_slug,
            request_id=request_id,
        )
        if execute:
            try:
                keyring = AuditHmacKeyring.from_environment()
            except AuditHmacKeyringInvalid:
                raise ProjectSkillImportError("project Skill audit authority is unavailable") from None
            service = SkillService(
                factory,
                governance_sink=DurableSharedAssetGovernanceEventSink(
                    AuditService(factory, keyring),
                ),
                quota=ProjectQuotaEnforcer(
                    QuotaService(
                        factory,
                        quota_config or QuotaConfig(),
                        source_ref_hasher=keyring,
                        current_policy_reader=SystemQuotaPolicyReader(),
                    )
                ),
            )
        else:
            service = SkillService(factory)
        result = await service.import_project_archives_atomic(
            actor,
            tuple(ProjectSkillArchiveImport(files=source.files) for source in sources),
            execute=execute,
            replace=replace,
        )
        return ProjectSkillImportSummary(
            mode="execute" if execute else "dry-run",
            discovered_count=result.discovered_count,
            planned_create_count=result.planned_create_count,
            planned_replace_count=result.planned_replace_count,
            unchanged_count=result.unchanged_count,
            created_count=result.created_count,
            replaced_count=result.replaced_count,
        )
    finally:
        await engine.dispose()


async def import_project_skills(
    database_url: str,
    *,
    source_root: Path | str,
    user_email: str,
    project_slug: str,
    execute: bool,
    quota_config: QuotaConfig | None,
    replace: bool = False,
) -> ProjectSkillImportSummary:
    """通过 ``SkillService`` 在一个事务内执行完整批次和 durable audit。"""

    try:
        return await _run_import(
            database_url,
            source_root=source_root,
            user_email=user_email,
            project_slug=project_slug,
            execute=execute,
            replace=replace,
            quota_config=quota_config,
        )
    except ProjectSkillImportError:
        raise
    except AssetForbidden:
        raise ProjectSkillImportError("project Skill import actor is not allowed") from None
    except AssetValidationFailed:
        raise ProjectSkillImportError("project Skill source validation failed") from None
    except AssetConflict:
        if not replace:
            raise ProjectSkillImportError("project Skill slug conflict; rerun with explicit replace") from None
        raise ProjectSkillImportError("project Skill changed concurrently") from None
    except AssetNotFound:
        raise ProjectSkillImportError("project Skill existing state is unavailable") from None
    except AssetStorageQuotaExceeded:
        raise ProjectSkillImportError("project Skill storage quota exceeded") from None
    except (AssetStorageUnavailable, SQLAlchemyError):
        raise ProjectSkillImportError("project Skill import database is unavailable") from None
    except Exception:
        raise ProjectSkillImportError("project Skill import failed safely") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="作为项目成员执行导入的账户 email")
    parser.add_argument("--project-slug", required=True, help="目标项目 slug")
    parser.add_argument(
        "--source",
        type=Path,
        default=_DEFAULT_SOURCE_ROOT,
        help="Skill 根目录；默认使用仓库 skills/public",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="完整校验并输出计划，不写数据库")
    mode.add_argument("--execute", action="store_true", help="执行已经完整预检的计划")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="显式允许绑定管理员替换同 slug 的 active Project Skill；相同 checksum 不写入",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("error: DATABASE_URL is required", file=sys.stderr)
        return 2
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        try:
            summary = asyncio.run(
                import_project_skills(
                    database_url,
                    source_root=args.source,
                    user_email=args.email,
                    project_slug=args.project_slug,
                    execute=args.execute,
                    replace=args.replace,
                    # Compatibility-only constructor shape. Authoritative
                    # execute checks read the current DB policy in-transaction.
                    quota_config=QuotaConfig() if args.execute else None,
                )
            )
        except ProjectSkillImportError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    finally:
        logging.disable(previous_logging_disable)
    print(
        json.dumps(
            summary.as_json_object(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
