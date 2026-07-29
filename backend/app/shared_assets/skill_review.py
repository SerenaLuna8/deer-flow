"""Project-scoped deterministic review of exact PostgreSQL Skill versions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.skill_repository import (
    SkillRepository,
    SkillVersionRecord,
)
from deerflow.skills.review.analyzer import analyze_skill_package
from deerflow.skills.review.models import (
    DEFAULT_PACKAGE_LIMITS,
    ProfileName,
    normalize_relative_path,
)
from deerflow.skills.review.readers import build_bytes_snapshot
from deerflow.skills.review.renderer import (
    build_static_report,
    render_report_markdown,
)

_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SCOPE_ITEMS = 32
_MAX_SCOPE_ITEM_CHARS = 64


@dataclass(frozen=True)
class SkillReviewResult:
    """Secret-free deterministic facts and renders for one exact version."""

    facts: dict[str, Any]
    report: dict[str, Any]
    markdown_en: str
    markdown_zh: str


class PostgresSkillVersionReader:
    """Read one exact project Skill version through trusted repository scope."""

    def __init__(self, repository: SkillRepository) -> None:
        self._repository = repository

    async def read(
        self,
        actor: ProjectContext,
        *,
        skill_id: uuid.UUID,
        version_id: uuid.UUID,
        expected_checksum: str,
    ) -> dict[str, Any]:
        self._validate_request(
            actor,
            skill_id=skill_id,
            version_id=version_id,
            expected_checksum=expected_checksum,
        )
        record = await self._repository.get_project_version(
            actor,
            skill_id,
            version_id,
        )
        return self._snapshot(
            actor,
            record,
            skill_id=skill_id,
            version_id=version_id,
            expected_checksum=expected_checksum,
        )

    @staticmethod
    def _validate_request(
        actor: ProjectContext,
        *,
        skill_id: uuid.UUID,
        version_id: uuid.UUID,
        expected_checksum: str,
    ) -> None:
        request_id = getattr(actor, "request_id", "unknown")
        if not isinstance(actor, ProjectContext):
            raise AssetForbidden(request_id)
        if Capability.SHARED_ASSETS_READ not in actor.capabilities:
            raise AssetForbidden(actor.request_id)
        if not isinstance(skill_id, uuid.UUID) or not isinstance(version_id, uuid.UUID) or not isinstance(expected_checksum, str) or _CHECKSUM_RE.fullmatch(expected_checksum) is None:
            raise AssetValidationFailed(actor.request_id)

    @staticmethod
    def _snapshot(
        actor: ProjectContext,
        record: SkillVersionRecord,
        *,
        skill_id: uuid.UUID,
        version_id: uuid.UUID,
        expected_checksum: str,
    ) -> dict[str, Any]:
        row = getattr(record, "row", None)
        if row is None or getattr(row, "id", None) != version_id or getattr(row, "skill_id", None) != skill_id:
            raise AssetValidationFailed(actor.request_id)
        version_number = getattr(row, "version_number", None)
        payload_checksum = getattr(row, "payload_checksum", None)
        created_at = getattr(row, "created_at", None)
        if not isinstance(version_number, int) or isinstance(version_number, bool) or version_number < 1 or not isinstance(payload_checksum, str) or _CHECKSUM_RE.fullmatch(payload_checksum) is None:
            raise AssetValidationFailed(actor.request_id)
        if not isinstance(created_at, datetime) or created_at.tzinfo is None or created_at.utcoffset() is None:
            raise AssetValidationFailed(actor.request_id)
        if payload_checksum != expected_checksum:
            raise AssetConflict(actor.request_id)

        raw_files = getattr(record, "files", None)
        if not isinstance(raw_files, (tuple, list)):
            raise AssetValidationFailed(actor.request_id)
        if not raw_files or len(raw_files) > DEFAULT_PACKAGE_LIMITS.max_files:
            raise AssetValidationFailed(actor.request_id)

        files: list[tuple[str, bytes]] = []
        checksum_records: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        total_bytes = 0
        for file_row in sorted(
            raw_files,
            key=lambda item: str(getattr(item, "path", "")),
        ):
            path = getattr(file_row, "path", None)
            content = getattr(file_row, "content", None)
            size_bytes = getattr(file_row, "size_bytes", None)
            sha256 = getattr(file_row, "sha256", None)
            row_version_id = getattr(file_row, "skill_version_id", None)
            if (
                row_version_id != version_id
                or not isinstance(path, str)
                or not isinstance(content, bytes)
                or not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or size_bytes < 0
                or not isinstance(sha256, str)
                or _CHECKSUM_RE.fullmatch(sha256) is None
            ):
                raise AssetValidationFailed(actor.request_id)
            try:
                normalized_path = normalize_relative_path(path)
            except ValueError:
                raise AssetValidationFailed(actor.request_id) from None
            if normalized_path != path or path in seen_paths or size_bytes != len(content) or size_bytes > DEFAULT_PACKAGE_LIMITS.max_file_bytes or hashlib.sha256(content).hexdigest() != sha256:
                raise AssetValidationFailed(actor.request_id)
            seen_paths.add(path)
            total_bytes += size_bytes
            if total_bytes > DEFAULT_PACKAGE_LIMITS.max_total_bytes:
                raise AssetValidationFailed(actor.request_id)
            files.append((path, content))
            checksum_records.append(
                {
                    "path": path,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                }
            )

        canonical = json.dumps(
            checksum_records,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        if hashlib.sha256(canonical).hexdigest() != payload_checksum:
            raise AssetValidationFailed(actor.request_id)

        snapshot = build_bytes_snapshot(
            files,
            subject={
                "source": "postgres_skill_version",
                "category": "project",
                "name_hint": None,
                "display_ref": (f"skill-version://{skill_id}/{version_id}"),
                "skill_id": str(skill_id),
                "version_id": str(version_id),
                "version_number": version_number,
                "payload_checksum": payload_checksum,
                "version_created_at": (created_at.astimezone(UTC).isoformat().replace("+00:00", "Z")),
            },
        )
        if snapshot["truncated"] or snapshot["reader_errors"]:
            raise AssetValidationFailed(actor.request_id)
        return snapshot


class PostgresSkillReviewService:
    """Review an exact immutable version without persisting review state."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | Callable[[], Any],
    ) -> None:
        self._session_factory = session_factory

    async def review(
        self,
        actor: ProjectContext,
        *,
        skill_id: uuid.UUID,
        version_id: uuid.UUID,
        expected_checksum: str,
        profile: ProfileName = "deerflow",
        scope: Sequence[str] | None = None,
    ) -> SkillReviewResult:
        request_id = getattr(actor, "request_id", "unknown")
        review_scope = self._validate_options(
            request_id,
            profile=profile,
            scope=scope,
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    snapshot = await PostgresSkillVersionReader(SkillRepository(session)).read(
                        actor,
                        skill_id=skill_id,
                        version_id=version_id,
                        expected_checksum=expected_checksum,
                    )
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(request_id) from None

        facts = await asyncio.to_thread(
            analyze_skill_package,
            snapshot,
            profile=profile,
        )
        completed_at = snapshot["subject"]["version_created_at"]
        report, markdown_en, markdown_zh = await asyncio.to_thread(
            _render_review,
            facts,
            review_scope,
            completed_at,
        )
        return SkillReviewResult(
            facts=facts,
            report=report,
            markdown_en=markdown_en,
            markdown_zh=markdown_zh,
        )

    @staticmethod
    def _validate_options(
        request_id: str,
        *,
        profile: object,
        scope: Sequence[str] | None,
    ) -> list[str]:
        if profile not in {"deerflow", "agentskills"}:
            raise AssetValidationFailed(request_id)
        if scope is None:
            return ["all"]
        if isinstance(scope, (str, bytes)) or not isinstance(scope, Sequence) or not scope or len(scope) > _MAX_SCOPE_ITEMS:
            raise AssetValidationFailed(request_id)
        normalized: list[str] = []
        for item in scope:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > _MAX_SCOPE_ITEM_CHARS:
                raise AssetValidationFailed(request_id)
            value = item.strip()
            if value in normalized:
                raise AssetValidationFailed(request_id)
            normalized.append(value)
        return normalized


def _render_review(
    facts: dict[str, Any],
    scope: list[str],
    completed_at: str,
) -> tuple[dict[str, Any], str, str]:
    report = build_static_report(
        facts,
        scope=scope,
        completed_at=completed_at,
    )
    return (
        report,
        render_report_markdown(report, facts, locale="en"),
        render_report_markdown(report, facts, locale="zh"),
    )
