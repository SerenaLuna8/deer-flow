#!/usr/bin/env python3
"""Prove that v4 Run Admission references one immutable large Skill Version.

This opt-in release-evidence harness creates and drops one random
``deerflow_test_*`` PostgreSQL database. It validates and persists the supplied
Skill archive exactly once, then uses ``RunSnapshotRepository`` and the real
v4 writer-cohort authority to admit 100 Runs. SQL parameter values and database
credentials are never written to the report.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import re
import statistics
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.private_work.context import PrivateWorkContext
from app.private_work.legacy_run_skill_snapshot_writer import (
    freeze_run_skill_snapshot_writer,
    reset_run_skill_snapshot_writer_for_testing,
)
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.run_skill_writer_cohort import RunSkillWriterCohortLease
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedRunAssetClosure,
    ResolvedSkillVersionSnapshot,
    SkillArchiveFile,
    SkillAssetRef,
    SkillSecretRequirementSnapshot,
)
from app.shared_assets.skill_archive import load_skill_archive_package
from app.shared_assets.skill_service import (
    SkillArchivePreview,
    _analyze_skill_files,
    normalize_skill_files,
)
from deerflow.config.run_skill_snapshot_config import RunSkillSnapshotConfig
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.private_work.model import RunAssetVersionRow

_MIB = 1024 * 1024
_RUN_COUNT = 100
_LARGE_JSON_PARAMETER_BYTES = 50 * _MIB
_TEST_DATABASE_PATTERN = re.compile(r"deerflow_test_[0-9]+_[0-9a-f]{32}\Z")
_FORBIDDEN_SKILL_SNAPSHOT_KEYS = frozenset(
    {
        "archive_base64",
        "codec",
        "content",
        "content_base64",
        "files",
    }
)


class StorageAcceptanceError(RuntimeError):
    """One v4 storage acceptance invariant was violated."""


def _constraint_name(error: BaseException) -> str | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str) and name:
            return name
        current = getattr(current, "orig", None) or getattr(
            current,
            "__cause__",
            None,
        )
    return None


def _json_items(value: object):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _json_items(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield None, item
            yield from _json_items(item)


def assert_byte_free_v4_skill_snapshot(snapshot: Mapping[str, object]) -> None:
    """Reject every v2/v3 whole-package shape before measuring storage."""

    if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != 4 or snapshot.get("kind") != "skill":
        raise StorageAcceptanceError(
            "Run Skill snapshot is not a byte-free v4 Skill manifest",
        )
    skill = snapshot.get("skill")
    if (
        not isinstance(skill, Mapping)
        or set(skill)
        != {
            "source",
            "file_count",
            "content_size_bytes",
        }
        or skill.get("source") != "skill_version_ref"
    ):
        raise StorageAcceptanceError(
            "Run Skill snapshot is not a byte-free v4 Skill manifest",
        )
    for key, value in _json_items(snapshot):
        normalized = "" if key is None else key.casefold()
        if normalized in _FORBIDDEN_SKILL_SNAPSHOT_KEYS or "base64" in normalized or (isinstance(value, str) and "zlib" in value.casefold()):
            raise StorageAcceptanceError(
                "Run Skill snapshot is not a byte-free v4 Skill manifest",
            )


def _legacy_whole_package_redline_rejects() -> bool:
    try:
        assert_byte_free_v4_skill_snapshot(
            {
                "schema_version": 3,
                "kind": "skill",
                "skill": {
                    "codec": "canonical-frame-zlib-6",
                    "archive_base64": "redline-only",
                },
            }
        )
    except StorageAcceptanceError:
        return True
    return False


def _parameter_payload_bytes(value: object, *, seen: set[int] | None = None) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, Mapping):
        identities = set() if seen is None else seen
        identity = id(value)
        if identity in identities:
            return 0
        identities.add(identity)
        return max(
            (_parameter_payload_bytes(key, seen=identities) for pair in value.items() for key in pair),
            default=0,
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        identities = set() if seen is None else seen
        identity = id(value)
        if identity in identities:
            return 0
        identities.add(identity)
        return max(
            (_parameter_payload_bytes(item, seen=identities) for item in value),
            default=0,
        )
    return 0


@dataclass(slots=True)
class AdmissionSQLCapture:
    """Secret-free statement and parameter-size observations."""

    statement_count: int = 0
    select_count: int = 0
    file_content_select_count: int = 0
    run_snapshot_select_count: int = 0
    skill_content_write_count: int = 0
    max_parameter_bytes: int = 0
    max_json_parameter_bytes: int = 0
    capture_error_count: int = 0
    _statements: dict[str, dict[str, object]] = field(default_factory=dict)

    def record(self, statement: str, parameters: object) -> None:
        normalized = " ".join(str(statement).split())
        lowered = normalized.casefold()
        is_select = lowered.startswith("select") or lowered.startswith("with")
        selects_skill_content = is_select and "skill_version_files" in lowered and ("skill_version_files.content" in lowered or re.search(r"\bselect\s+[^;]*\bcontent\b[^;]*\bfrom\s+skill_version_files\b", lowered) is not None)
        selects_run_snapshot = is_select and "run_asset_versions" in lowered and "snapshot_json" in lowered
        writes_skill_content = not is_select and "skill_version_files" in lowered and "content" in lowered
        parameter_bytes = _parameter_payload_bytes(parameters)
        json_parameter_bytes = parameter_bytes if "snapshot_json" in lowered else 0

        self.statement_count += 1
        self.select_count += int(is_select)
        self.file_content_select_count += int(selects_skill_content)
        self.run_snapshot_select_count += int(selects_run_snapshot)
        self.skill_content_write_count += int(writes_skill_content)
        self.max_parameter_bytes = max(self.max_parameter_bytes, parameter_bytes)
        self.max_json_parameter_bytes = max(
            self.max_json_parameter_bytes,
            json_parameter_bytes,
        )

        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        record = self._statements.setdefault(
            digest,
            {
                "sha256": digest,
                "verb": lowered.split(" ", 1)[0].upper() if lowered else "",
                "calls": 0,
                "selects_skill_content": selects_skill_content,
                "selects_run_snapshot": selects_run_snapshot,
                "writes_skill_content": writes_skill_content,
                "max_parameter_bytes": 0,
            },
        )
        record["calls"] = int(record["calls"]) + 1
        record["max_parameter_bytes"] = max(
            int(record["max_parameter_bytes"]),
            parameter_bytes,
        )

    def assertions(self) -> dict[str, bool]:
        return {
            "gateway_admission_did_not_select_skill_content": (self.file_content_select_count == 0),
            "gateway_admission_did_not_select_run_snapshot_payload": (self.run_snapshot_select_count == 0),
            "gateway_admission_did_not_write_skill_content": (self.skill_content_write_count == 0),
            "gateway_admission_did_not_send_large_jsonb": (self.max_json_parameter_bytes < _LARGE_JSON_PARAMETER_BYTES),
            "gateway_metadata_reads_do_not_implicitly_detoast_payloads": (self.file_content_select_count == 0 and self.run_snapshot_select_count == 0),
            "sql_capture_completed_without_error": self.capture_error_count == 0,
        }

    def as_json(self) -> dict[str, object]:
        return {
            "statement_count": self.statement_count,
            "select_count": self.select_count,
            "unique_statement_count": len(self._statements),
            "file_content_select_count": self.file_content_select_count,
            "run_snapshot_select_count": self.run_snapshot_select_count,
            "skill_content_write_count": self.skill_content_write_count,
            "max_parameter_bytes": self.max_parameter_bytes,
            "max_json_parameter_bytes": self.max_json_parameter_bytes,
            "capture_error_count": self.capture_error_count,
            "statement_fingerprints": sorted(
                self._statements.values(),
                key=lambda item: str(item["sha256"]),
            ),
        }


@dataclass(frozen=True, slots=True)
class _SeedCoordinates:
    user_id: uuid.UUID
    project_id: uuid.UUID
    membership_id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    agent_checksum: str
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    skill_checksum: str
    skill_file_count: int
    skill_content_size_bytes: int
    skill_secret_requirements: tuple[SkillSecretRequirementSnapshot, ...]
    thread_id: str
    catalog_generation: int


def _database_url(base_url: str, database: str) -> str:
    parsed = make_url(base_url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+asyncpg")
    return parsed.set(database=database).render_as_string(hide_password=False)


@asynccontextmanager
async def _temporary_database(
    admin_url: str,
) -> AsyncIterator[tuple[str, str]]:
    database_name = f"deerflow_test_{os.getpid()}_{uuid.uuid4().hex}"
    if _TEST_DATABASE_PATTERN.fullmatch(database_name) is None:
        raise StorageAcceptanceError("unsafe disposable database coordinate")
    admin_engine = create_async_engine(
        _database_url(admin_url, "postgres"),
        isolation_level="AUTOCOMMIT",
    )
    body_error: BaseException | None = None
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(
                    f"CREATE DATABASE \"{database_name}\" TEMPLATE template0 ENCODING 'UTF8'",
                )
            )
        try:
            yield _database_url(admin_url, database_name), database_name
        except BaseException as error:
            body_error = error
            raise
    finally:
        try:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:database_name AND pid<>pg_backend_pid()",
                    ),
                    {"database_name": database_name},
                )
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        except BaseException:
            if body_error is None:
                raise
        finally:
            await admin_engine.dispose()


async def _database_exists(admin_url: str, database_name: str) -> bool:
    engine = create_async_engine(_database_url(admin_url, "postgres"))
    try:
        async with engine.connect() as connection:
            return bool(
                await connection.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:database_name)",
                    ),
                    {"database_name": database_name},
                )
            )
    finally:
        await engine.dispose()


def _agent_payload(skill_id: uuid.UUID) -> AgentPayload:
    return AgentPayload(
        description="v4 100 Run storage acceptance",
        soul="Use one exact immutable Skill Version.",
        model_ref="00000000-0000-4000-8000-000000000305",
        tool_groups=(),
        skill_refs=(
            SkillAssetRef(
                scope=AssetScope.PROJECT,
                asset_id=skill_id,
            ),
        ),
        mcp_version_ids=(),
        payload_schema_version=4,
    )


async def _load_archive(
    archive_path: Path,
) -> tuple[bytes, tuple[SkillArchiveFile, ...], SkillArchivePreview]:
    archive = await asyncio.to_thread(archive_path.read_bytes)
    files = tuple(
        normalize_skill_files(
            load_skill_archive_package(
                archive,
                filename=archive_path.name,
                request_id="v4-100-run-storage",
            ),
            request_id="v4-100-run-storage",
        )
    )
    preview = await asyncio.to_thread(
        _analyze_skill_files,
        files,
        "v4-100-run-storage",
    )
    return archive, files, preview


async def _seed_version_once(
    factory: async_sessionmaker[AsyncSession],
    *,
    files: Sequence[SkillArchiveFile],
    preview: SkillArchivePreview,
) -> _SeedCoordinates:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    thread_id = f"v4-storage-{uuid.uuid4().hex}"
    payload = _agent_payload(skill_id)
    agent_checksum = agent_payload_checksum(payload)
    requirements_json = [
        {
            "name": item.name,
            "target_env": item.target_env,
            "optional": item.optional,
        }
        for item in preview.secret_requirements
    ]

    async with factory() as session, session.begin():
        await session.execute(
            text(
                """INSERT INTO users (
                       id,email,username,system_role,created_at,
                       needs_setup,token_version
                   ) VALUES (
                       :user_id,:email,:username,'system_admin',now(),false,1
                   )"""
            ),
            {
                "user_id": str(user_id),
                "email": f"{user_id.hex}@example.invalid",
                "username": f"v4_{user_id.hex[:16]}",
            },
        )
        await session.execute(
            text(
                """INSERT INTO projects (
                       id,slug,display_name,created_by_user_id
                   ) VALUES (
                       :project_id,:slug,'v4 storage acceptance',:user_id
                   )"""
            ),
            {
                "project_id": project_id,
                "slug": f"v4-storage-{project_id.hex[:12]}",
                "user_id": str(user_id),
            },
        )
        await session.execute(
            text(
                """INSERT INTO project_memberships (
                       id,project_id,user_id,role
                   ) VALUES (:id,:project_id,:user_id,'admin')"""
            ),
            {
                "id": membership_id,
                "project_id": project_id,
                "user_id": str(user_id),
            },
        )
        await session.execute(
            text(
                """INSERT INTO skills (
                       id,scope,project_id,slug,display_name,status,
                       created_by_user_id
                   ) VALUES (
                       :skill_id,'project',:project_id,'ppt-master',
                       'ppt-master','active',:user_id
                   )"""
            ),
            {
                "skill_id": skill_id,
                "project_id": project_id,
                "user_id": str(user_id),
            },
        )
        await session.execute(
            text(
                """INSERT INTO skill_versions (
                       id,skill_id,version_number,description,frontmatter,
                       compatibility,secret_requirements,scan_decision,
                       scan_summary,payload_checksum,file_count,
                       content_size_bytes,files_sealed,created_by_user_id
                   ) VALUES (
                       :version_id,:skill_id,1,:description,
                       CAST(:frontmatter AS jsonb),:compatibility,
                       CAST(:requirements AS jsonb),'allow','{}'::jsonb,
                       :checksum,:file_count,:content_size,false,:user_id
                   )"""
            ),
            {
                "version_id": skill_version_id,
                "skill_id": skill_id,
                "description": preview.description,
                "frontmatter": json.dumps(
                    preview.frontmatter,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "compatibility": preview.compatibility,
                "requirements": json.dumps(
                    requirements_json,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "checksum": preview.checksum,
                "file_count": len(preview.file_views),
                "content_size": sum(item.size_bytes for item in preview.file_views),
                "user_id": str(user_id),
            },
        )
        await session.execute(
            text(
                "SELECT set_config('deerflow.asset_version_assembly', :version_id, true)",
            ),
            {"version_id": str(skill_version_id)},
        )
        insert_file = text(
            """INSERT INTO skill_version_files (
                   skill_version_id,path,media_type,size_bytes,sha256,content
               ) VALUES (
                   :version_id,:path,:media_type,:size_bytes,:sha256,:content
               )"""
        )
        for offset in range(0, len(files), 128):
            batch_files = files[offset : offset + 128]
            batch_views = preview.file_views[offset : offset + 128]
            await session.execute(
                insert_file,
                [
                    {
                        "version_id": skill_version_id,
                        "path": item.path,
                        "media_type": item.media_type,
                        "size_bytes": len(item.content),
                        "sha256": view.sha256,
                        "content": item.content,
                    }
                    for item, view in zip(batch_files, batch_views, strict=True)
                ],
            )
        await session.execute(
            text(
                "UPDATE skill_versions SET files_sealed=true WHERE id=:version_id",
            ),
            {"version_id": skill_version_id},
        )
        await session.execute(
            text(
                "UPDATE skills SET current_version_id=:version_id WHERE id=:skill_id",
            ),
            {"version_id": skill_version_id, "skill_id": skill_id},
        )

        await session.execute(
            text(
                """INSERT INTO agents (
                       id,scope,project_id,slug,display_name,status,
                       created_by_user_id
                   ) VALUES (
                       :agent_id,'project',:project_id,'storage-agent',
                       'Storage Agent','active',:user_id
                   )"""
            ),
            {
                "agent_id": agent_id,
                "project_id": project_id,
                "user_id": str(user_id),
            },
        )
        await session.execute(
            text(
                """INSERT INTO agent_versions (
                       id,agent_id,version_number,description,soul,model_ref,
                       model_settings,tool_groups,payload_checksum,
                       created_by_user_id,agents_instructions,identity,
                       user_context,payload_schema_version
                   ) VALUES (
                       :version_id,:agent_id,1,:description,:soul,:model_ref,
                       '{}'::jsonb,'[]'::jsonb,:checksum,:user_id,'','','',4
                   )"""
            ),
            {
                "version_id": agent_version_id,
                "agent_id": agent_id,
                "description": payload.description,
                "soul": payload.soul,
                "model_ref": payload.model_ref,
                "checksum": agent_checksum,
                "user_id": str(user_id),
            },
        )
        await session.execute(
            text(
                "SELECT set_config('deerflow.asset_version_assembly', :version_id, true)",
            ),
            {"version_id": str(agent_version_id)},
        )
        await session.execute(
            text(
                """INSERT INTO agent_version_skill_refs (
                       agent_version_id,sort_order,skill_asset_scope,
                       skill_asset_id
                   ) VALUES (:version_id,0,'project',:skill_id)"""
            ),
            {"version_id": agent_version_id, "skill_id": skill_id},
        )
        await session.execute(
            text(
                "UPDATE agents SET current_version_id=:version_id WHERE id=:agent_id",
            ),
            {"version_id": agent_version_id, "agent_id": agent_id},
        )
        await session.execute(
            text(
                """INSERT INTO threads_meta (
                       thread_id,owner_user_id,status,metadata_json,
                       created_at,updated_at,project_id,agent_asset_id,
                       agent_scope
                   ) VALUES (
                       :thread_id,:user_id,'idle','{}'::json,now(),now(),
                       :project_id,:agent_id,'project'
                   )"""
            ),
            {
                "thread_id": thread_id,
                "user_id": str(user_id),
                "project_id": project_id,
                "agent_id": agent_id,
            },
        )

    async with factory() as session:
        generation = int(
            await session.scalar(
                text("SELECT generation FROM asset_catalog_state WHERE id=1"),
            )
        )
    return _SeedCoordinates(
        user_id=user_id,
        project_id=project_id,
        membership_id=membership_id,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        agent_checksum=agent_checksum,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        skill_checksum=preview.checksum,
        skill_file_count=len(preview.file_views),
        skill_content_size_bytes=sum(item.size_bytes for item in preview.file_views),
        skill_secret_requirements=tuple(
            SkillSecretRequirementSnapshot(
                name=item.name,
                target_env=item.target_env,
                optional=item.optional,
            )
            for item in preview.secret_requirements
        ),
        thread_id=thread_id,
        catalog_generation=generation,
    )


def _private_context(seed: _SeedCoordinates) -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=seed.user_id,
            project_id=seed.project_id,
            membership_id=seed.membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="v4-100-run-storage",
        )
    )


def _resolved_closure(seed: _SeedCoordinates) -> ResolvedRunAssetClosure:
    payload = _agent_payload(seed.skill_id)
    lead = ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=seed.agent_id,
        version_id=seed.agent_version_id,
        checksum=seed.agent_checksum,
        catalog_generation=seed.catalog_generation,
        dependency_version_ids=(seed.skill_version_id,),
        payload=payload,
        skill_version_ids=(seed.skill_version_id,),
        slug="storage-agent",
    )
    skill = ResolvedSkillVersionSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope.PROJECT,
        asset_id=seed.skill_id,
        version_id=seed.skill_version_id,
        checksum=seed.skill_checksum,
        catalog_generation=seed.catalog_generation,
        dependency_version_ids=(),
        file_count=seed.skill_file_count,
        content_size_bytes=seed.skill_content_size_bytes,
        secret_requirements=seed.skill_secret_requirements,
    )
    return ResolvedRunAssetClosure(
        lead_agent=lead,
        delegated_agents=(),
        skills=(skill,),
        mcps=(),
        main_skill_version_ids=(skill.version_id,),
        main_mcp_version_ids=(),
    )


async def _checkpoint(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        raw = await connection.get_raw_connection()
        await raw.driver_connection.execute("CHECKPOINT")


async def _version_file_stats(
    factory: async_sessionmaker[AsyncSession],
    version_id: uuid.UUID,
) -> dict[str, object]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    """SELECT version.files_sealed,version.file_count,
                              version.content_size_bytes,count(file.path),
                              coalesce(sum(file.size_bytes),0),
                              coalesce(max(file.size_bytes),0)
                       FROM skill_versions version
                       LEFT JOIN skill_version_files file
                         ON file.skill_version_id=version.id
                       WHERE version.id=:version_id
                       GROUP BY version.files_sealed,version.file_count,
                                version.content_size_bytes"""
                ),
                {"version_id": version_id},
            )
        ).one()
    return {
        "files_sealed": bool(row[0]),
        "declared_file_count": int(row[1]),
        "declared_content_size_bytes": int(row[2]),
        "row_count": int(row[3]),
        "logical_content_bytes": int(row[4]),
        "max_file_bytes": int(row[5]),
    }


async def _relation_stats(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    """WITH target AS (
                           SELECT oid,reltoastrelid
                           FROM pg_class
                           WHERE oid='run_asset_versions'::regclass
                       )
                       SELECT pg_relation_size(oid::regclass),
                              CASE WHEN reltoastrelid=0 THEN 0
                                   ELSE pg_relation_size(reltoastrelid::regclass)
                              END,
                              pg_indexes_size(oid::regclass),
                              pg_total_relation_size(oid::regclass),
                              CASE WHEN reltoastrelid=0 THEN 0
                                   ELSE pg_total_relation_size(reltoastrelid::regclass)
                              END
                       FROM target"""
                )
            )
        ).one()
    main = int(row[0])
    toast = int(row[1])
    return {
        "main_heap_bytes": main,
        "toast_heap_bytes": toast,
        "main_plus_toast_heap_bytes": main + toast,
        "main_index_bytes": int(row[2]),
        "relation_total_bytes": int(row[3]),
        "toast_total_bytes": int(row[4]),
    }


async def _run_storage_stats(
    factory: async_sessionmaker[AsyncSession],
    seed: _SeedCoordinates,
) -> tuple[dict[str, int], tuple[Mapping[str, object], ...]]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    """SELECT
                         (SELECT count(*) FROM runs
                           WHERE run_id LIKE 'v4-storage-run-%'),
                         (SELECT count(*) FROM run_asset_versions
                           WHERE run_id LIKE 'v4-storage-run-%'),
                         (SELECT count(*) FROM run_asset_versions
                           WHERE run_id LIKE 'v4-storage-run-%'
                             AND asset_kind='skill'
                             AND snapshot_schema_version=4),
                         (SELECT count(*) FROM run_skill_version_refs
                           WHERE run_id LIKE 'v4-storage-run-%'),
                         (SELECT count(*) FROM run_skill_version_refs
                           WHERE run_id LIKE 'v4-storage-run-%'
                             AND skill_version_id=:version_id),
                         (SELECT coalesce(max(octet_length(snapshot_json::text)),0)
                            FROM run_asset_versions
                           WHERE run_id LIKE 'v4-storage-run-%'),
                         (SELECT coalesce(max(octet_length(snapshot_json::text)),0)
                            FROM run_asset_versions
                           WHERE run_id LIKE 'v4-storage-run-%'
                             AND asset_kind='skill'),
                         (SELECT count(*) FROM run_asset_versions
                           WHERE run_id LIKE 'v4-storage-run-%'
                             AND (
                               snapshot_json::text ILIKE '%archive_base64%'
                               OR snapshot_json::text ILIKE '%content_base64%'
                               OR snapshot_json::text ILIKE '%canonical-frame-zlib%'
                               OR snapshot_json::text ILIKE '%\"files\"%'
                             ))"""
                ),
                {"version_id": seed.skill_version_id},
            )
        ).one()
        snapshots = tuple(
            (
                await session.execute(
                    select(RunAssetVersionRow.snapshot_json).where(
                        RunAssetVersionRow.run_id.like("v4-storage-run-%"),
                        RunAssetVersionRow.asset_kind == AssetKind.SKILL.value,
                    )
                )
            )
            .scalars()
            .all()
        )
    return (
        {
            "run_count": int(row[0]),
            "run_asset_version_count": int(row[1]),
            "v4_skill_manifest_count": int(row[2]),
            "skill_ref_count": int(row[3]),
            "exact_version_ref_count": int(row[4]),
            "max_snapshot_json_bytes": int(row[5]),
            "max_skill_snapshot_json_bytes": int(row[6]),
            "forbidden_payload_snapshot_count": int(row[7]),
        },
        snapshots,
    )


async def _wal_lsn(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(await connection.scalar(text("SELECT pg_current_wal_lsn()::text")))


async def _wal_diff(engine: AsyncEngine, *, before: str, after: str) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                text(
                    "SELECT pg_wal_lsn_diff(CAST(:after AS text)::pg_lsn, CAST(:before AS text)::pg_lsn)",
                ),
                {"before": before, "after": after},
            )
        )


async def _run_command(*arguments: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    return int(process.returncode or 0), stdout.decode(
        "utf-8",
        errors="replace",
    )


async def _postgres_oom_events(container_name: str) -> dict[str, int]:
    code, output = await _run_command(
        "container",
        "exec",
        container_name,
        "cat",
        "/sys/fs/cgroup/memory.events",
    )
    if code != 0:
        raise StorageAcceptanceError("unable to read PostgreSQL OOM counters")
    return {key: int(value) for line in output.splitlines() for key, value in [line.split()]}


async def _postgres_memory(container_name: str) -> dict[str, int]:
    shell = """
set -eu
aggregate=0
for comm_file in /proc/[0-9]*/comm; do
  read process_name < "$comm_file" 2>/dev/null || continue
  case "$process_name" in
    postgres*)
      process_dir=${comm_file%/comm}
      rss_kib=$(awk '/^VmRSS:/ {print $2}' "$process_dir/status" 2>/dev/null || true)
      aggregate=$((aggregate + ${rss_kib:-0} * 1024))
      ;;
  esac
done
printf '%s %s %s\n' \
  "$(cat /sys/fs/cgroup/memory.current)" \
  "$(cat /sys/fs/cgroup/memory.max)" \
  "$aggregate"
"""
    code, output = await _run_command(
        "container",
        "exec",
        container_name,
        "sh",
        "-c",
        shell,
    )
    if code != 0:
        raise StorageAcceptanceError("unable to sample PostgreSQL memory")
    values = tuple(int(item) for item in output.split())
    if len(values) != 3:
        raise StorageAcceptanceError("invalid PostgreSQL memory sample")
    return {
        "cgroup_memory_bytes": values[0],
        "cgroup_limit_bytes": values[1],
        "aggregate_postgres_rss_bytes": values[2],
    }


async def _postgres_identity(admin_url: str) -> dict[str, object]:
    engine = create_async_engine(_database_url(admin_url, "postgres"))
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT pg_postmaster_start_time()::text,pg_is_in_recovery()",
                    )
                )
            ).one()
        return {
            "postmaster_started_at": str(row[0]),
            "in_recovery": bool(row[1]),
        }
    finally:
        await engine.dispose()


def _oom_unchanged(
    before: Mapping[str, int],
    after: Mapping[str, int],
) -> bool:
    return all(after.get(key, 0) == before.get(key, 0) for key in ("oom", "oom_kill", "oom_group_kill"))


def _latency_stats(values: Sequence[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return {
        "sample_count": len(ordered),
        "total_seconds": sum(ordered),
        "mean_seconds": statistics.fmean(ordered),
        "p50_seconds": statistics.median(ordered),
        "p95_seconds": ordered[p95_index],
        "max_seconds": max(ordered),
    }


def _atomic_write_report(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


async def _orchestrate(args: argparse.Namespace) -> bool:
    evidence_path = args.evidence.resolve()
    archive_path = args.archive.resolve()
    admin_url = str(args.database_url).strip()
    if not admin_url:
        raise SystemExit(
            "POSTGRES_ADMIN_URL must be loaded explicitly before running the profile",
        )
    if not archive_path.is_file():
        raise SystemExit("the supplied Skill archive does not exist")

    stage = "baseline"
    failure: dict[str, str] | None = None
    database_name: str | None = None
    scratch_path: Path | None = None
    database_dropped = False
    scratch_removed = False
    fixture: dict[str, object] = {}
    storage: dict[str, object] = {}
    sql_capture = AdmissionSQLCapture()
    assertions: dict[str, bool] = {}
    oom_before: dict[str, int] = {}
    oom_after: dict[str, int] = {}
    identity_before: dict[str, object] = {}
    identity_after: dict[str, object] = {}
    memory_samples: list[dict[str, int]] = []
    legacy_redline_rejected = _legacy_whole_package_redline_rejects()

    try:
        oom_before = await _postgres_oom_events(args.postgres_container)
        identity_before = await _postgres_identity(admin_url)
        memory_samples.append(await _postgres_memory(args.postgres_container))
        with tempfile.TemporaryDirectory(
            prefix="actweave-v4-100-run-storage-",
        ) as scratch:
            scratch_path = Path(scratch)
            async with _temporary_database(admin_url) as (
                database_url,
                current_database_name,
            ):
                database_name = current_database_name
                engine = create_async_engine(database_url)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                lease: RunSkillWriterCohortLease | None = None
                try:
                    stage = "install_schema_v1"
                    await _install_full_schema(engine)

                    stage = "load_and_validate_archive"
                    archive, files, preview = await _load_archive(archive_path)
                    archive_sha256 = hashlib.sha256(archive).hexdigest()
                    fixture = {
                        "archive_filename": archive_path.name,
                        "archive_sha256": archive_sha256,
                        "archive_size_bytes": len(archive),
                        "normalized_file_count": len(files),
                        "logical_content_bytes": sum(len(item.content) for item in files),
                        "validated_preview_checksum": preview.checksum,
                        "secret_requirement_count": len(preview.secret_requirements),
                    }

                    stage = "persist_sealed_version_once"
                    seed = await _seed_version_once(
                        factory,
                        files=files,
                        preview=preview,
                    )
                    del archive, files, preview
                    gc.collect()
                    await _checkpoint(engine)
                    memory_samples.append(await _postgres_memory(args.postgres_container))

                    file_stats_before = await _version_file_stats(
                        factory,
                        seed.skill_version_id,
                    )
                    relation_before = await _relation_stats(factory)
                    wal_before = await _wal_lsn(engine)

                    reset_run_skill_snapshot_writer_for_testing()
                    writer_readback = freeze_run_skill_snapshot_writer(RunSkillSnapshotConfig())
                    lease = await RunSkillWriterCohortLease.acquire(
                        engine,
                        writer_readback,
                        process_role="gateway",
                        process_authority=True,
                    )
                    repository = RunSnapshotRepository(factory)
                    context = _private_context(seed)
                    closure = _resolved_closure(seed)
                    latency_seconds: list[float] = []

                    def capture_statement(
                        _connection: object,
                        _cursor: object,
                        statement: str,
                        parameters: object,
                        _context: object,
                        _executemany: bool,
                    ) -> None:
                        try:
                            sql_capture.record(statement, parameters)
                        except BaseException:
                            sql_capture.capture_error_count += 1

                    stage = "admit_100_runs"
                    event.listen(
                        engine.sync_engine,
                        "before_cursor_execute",
                        capture_statement,
                    )
                    try:
                        for index in range(_RUN_COUNT):
                            started_at = time.monotonic()
                            created = await repository.create_run_with_snapshot(
                                context,
                                seed.thread_id,
                                PrivateRunCreate(run_id=(f"v4-storage-run-{index:03d}-{uuid.uuid4().hex[:16]}")),
                                closure,
                            )
                            latency_seconds.append(time.monotonic() - started_at)
                            if not created.run_id.startswith("v4-storage-run-"):
                                raise StorageAcceptanceError(
                                    "production Admission returned an unexpected Run",
                                )
                            if (index + 1) % 20 == 0:
                                memory_samples.append(await _postgres_memory(args.postgres_container))
                    finally:
                        event.remove(
                            engine.sync_engine,
                            "before_cursor_execute",
                            capture_statement,
                        )

                    wal_after = await _wal_lsn(engine)
                    wal_bytes = await _wal_diff(
                        engine,
                        before=wal_before,
                        after=wal_after,
                    )
                    file_stats_after = await _version_file_stats(
                        factory,
                        seed.skill_version_id,
                    )
                    relation_after = await _relation_stats(factory)
                    run_stats, skill_snapshots = await _run_storage_stats(
                        factory,
                        seed,
                    )
                    for snapshot in skill_snapshots:
                        assert_byte_free_v4_skill_snapshot(snapshot)

                    relation_delta = {key: relation_after[key] - relation_before[key] for key in relation_before}
                    main_toast_delta = relation_delta["main_plus_toast_heap_bytes"]
                    storage = {
                        "version_files_before": file_stats_before,
                        "version_files_after": file_stats_after,
                        "run_asset_versions_before": relation_before,
                        "run_asset_versions_after": relation_after,
                        "run_asset_versions_delta": relation_delta,
                        "run_asset_versions_main_plus_toast_bytes_per_run": (main_toast_delta / _RUN_COUNT),
                        "wal": {
                            "before_lsn": wal_before,
                            "after_lsn": wal_after,
                            "diff_bytes": wal_bytes,
                            "bytes_per_run": wal_bytes / _RUN_COUNT,
                        },
                        "latency": _latency_stats(latency_seconds),
                        "counts_and_snapshot_shape": run_stats,
                        "writer_readback": {
                            "writer_mode": writer_readback.writer_mode,
                            "artifact_version": writer_readback.artifact_version,
                            "ready": writer_readback.ready,
                            "cohort_ready": lease.ready,
                        },
                        "version_content_write_transactions": 1,
                    }

                    proportional_limit = max(
                        1,
                        seed.skill_content_size_bytes // 100,
                    )
                    assertions = {
                        "real_fixture_has_12922_files": (seed.skill_file_count == 12_922),
                        "real_fixture_is_about_79_mib": (75 * _MIB <= seed.skill_content_size_bytes <= 85 * _MIB),
                        "skill_version_is_sealed": (file_stats_before["files_sealed"] is True and file_stats_after["files_sealed"] is True),
                        "skill_version_file_rows_do_not_grow": (file_stats_before["row_count"] == file_stats_after["row_count"] == seed.skill_file_count),
                        "skill_version_logical_bytes_do_not_grow": (file_stats_before["logical_content_bytes"] == file_stats_after["logical_content_bytes"] == seed.skill_content_size_bytes),
                        "exactly_100_runs_created": (run_stats["run_count"] == _RUN_COUNT),
                        "each_run_has_agent_and_skill_parent": (run_stats["run_asset_version_count"] == _RUN_COUNT * 2),
                        "each_run_has_one_v4_skill_manifest": (run_stats["v4_skill_manifest_count"] == _RUN_COUNT),
                        "each_run_has_one_exact_version_ref": (run_stats["skill_ref_count"] == run_stats["exact_version_ref_count"] == _RUN_COUNT),
                        "snapshots_have_no_base64_zlib_or_file_array": (run_stats["forbidden_payload_snapshot_count"] == 0),
                        "skill_manifest_is_small": (run_stats["max_skill_snapshot_json_bytes"] < 2_048),
                        "all_run_snapshots_are_small": (run_stats["max_snapshot_json_bytes"] < 4_096),
                        "run_asset_main_plus_toast_per_run_is_sublinear": (main_toast_delta / _RUN_COUNT < proportional_limit),
                        "run_asset_main_plus_toast_total_is_below_one_skill_copy": (main_toast_delta < seed.skill_content_size_bytes),
                        "wal_per_run_is_sublinear": (wal_bytes / _RUN_COUNT < proportional_limit),
                        "wal_for_100_runs_is_below_one_skill_copy": (wal_bytes < seed.skill_content_size_bytes),
                        "all_100_latencies_recorded": (len(latency_seconds) == _RUN_COUNT and all(value > 0 for value in latency_seconds)),
                        "v4_writer_and_cohort_are_ready": (writer_readback.writer_mode == "v4_reference" and writer_readback.ready is True and lease.ready),
                        **sql_capture.assertions(),
                    }
                finally:
                    if lease is not None:
                        await lease.close()
                    reset_run_skill_snapshot_writer_for_testing()
                    await engine.dispose()
            database_dropped = not await _database_exists(
                admin_url,
                current_database_name,
            )
        scratch_removed = scratch_path is not None and not scratch_path.exists()
    except BaseException as error:
        failure = {
            "stage": stage,
            "error_type": type(error).__name__,
        }
        constraint_name = _constraint_name(error)
        if constraint_name is not None:
            failure["constraint_name"] = constraint_name
        reset_run_skill_snapshot_writer_for_testing()

    if database_name is not None:
        try:
            database_dropped = not await _database_exists(
                admin_url,
                database_name,
            )
        except BaseException:
            database_dropped = False
    if scratch_path is not None:
        scratch_removed = not scratch_path.exists()

    try:
        oom_after = await _postgres_oom_events(args.postgres_container)
        identity_after = await _postgres_identity(admin_url)
        memory_samples.append(await _postgres_memory(args.postgres_container))
    except BaseException as error:
        if failure is None:
            failure = {
                "stage": "post_profile_postgres_readback",
                "error_type": type(error).__name__,
            }

    assertions.update(
        {
            "legacy_v3_whole_package_fails_pre_v4_redline": (legacy_redline_rejected),
            "random_database_removed": database_dropped,
            "temporary_directory_removed": scratch_removed,
            "postgres_container_limit_is_one_gib": (bool(memory_samples) and all(sample["cgroup_limit_bytes"] == 1024 * _MIB for sample in memory_samples)),
            "postgres_oom_counters_did_not_increment": (bool(oom_before) and bool(oom_after) and _oom_unchanged(oom_before, oom_after)),
            "postgres_postmaster_did_not_restart": (bool(identity_before) and bool(identity_after) and identity_before["postmaster_started_at"] == identity_after["postmaster_started_at"]),
            "postgres_not_in_recovery_after_profile": (bool(identity_after) and identity_after["in_recovery"] is False),
        }
    )
    postgres = {
        "container": args.postgres_container,
        "oom_events_before": oom_before,
        "oom_events_after": oom_after,
        "identity_before": identity_before,
        "identity_after": identity_after,
        "memory": {
            "sample_count": len(memory_samples),
            "baseline": memory_samples[0] if memory_samples else {},
            "peak": ({key: max(sample[key] for sample in memory_samples) for key in memory_samples[0]} if memory_samples else {}),
            "final": memory_samples[-1] if memory_samples else {},
        },
    }
    report = {
        "profile": "v4_100_run_storage_acceptance",
        "generated_at": datetime.now(UTC).isoformat(),
        "command": (f"PYTHONPATH=. uv run python scripts/profile_v4_100_run_storage.py --archive <ppt-master.zip> --evidence <report.json> --postgres-container {args.postgres_container}"),
        "disposable_database": {
            "name": database_name,
            "dropped": database_dropped,
        },
        "fixture": fixture,
        "pre_v4_redline": {
            "legacy_schema_version": 3,
            "legacy_codec": "canonical-frame-zlib-6",
            "whole_package_shape_rejected": legacy_redline_rejected,
        },
        "storage": storage,
        "gateway_admission_sql_capture": sql_capture.as_json(),
        "postgres": postgres,
        "assertions": assertions,
        "passed": (failure is None and bool(assertions) and all(assertions.values())),
        "failure": failure,
        "scope_limits": [
            "The profile uses a random disposable Schema V1 database and never connects to or modifies the user target database.",
            "The 100 sequential writes use RunSnapshotRepository.create_run_with_snapshot with the production v4 writer-cohort authority; this is the Gateway persistence seam, not an HTTP or Worker execution test.",
            "SQL capture stores only statement hashes, classifications, call counts, and parameter lengths; it stores no parameter values or database URL.",
            "WAL and relation deltas include the 100 Run snapshot transactions in this isolated database and are not extrapolated to concurrent replicas.",
        ],
    }
    _atomic_write_report(evidence_path, report)
    print(
        json.dumps(
            {
                "evidence": str(evidence_path),
                "passed": report["passed"],
                "failure_stage": (None if failure is None else failure["stage"]),
            },
            sort_keys=True,
        )
    )
    return bool(report["passed"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--database-url",
        default=os.getenv("POSTGRES_ADMIN_URL", ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--postgres-container", default="postgres")
    return parser


def main() -> None:
    if not asyncio.run(_orchestrate(_parser().parse_args())):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
