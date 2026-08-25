#!/usr/bin/env python3
"""Profile the R1 legacy Admission writer and rollback topology.

The harness is opt-in release evidence. It creates one random ``deerflow_test_*``
database, seeds the supplied archive as immutable Skill Versions, launches real
Gateway/Scheduler-labelled OS processes, and removes the database afterwards.
Database credentials stay in child environments and never enter the report.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import gc
import hashlib
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import asyncpg
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.private_work.errors import LegacyAdmissionBusy, PrivateWorkTooLarge
from app.private_work.legacy_run_skill_snapshot_writer import (
    LEGACY_ADMISSION_BYTE_GATE_KEY,
    LEGACY_ADMISSION_POLICY,
    RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION,
    LegacyAdmissionByteGate,
    LegacyRunSkillSnapshotWriter,
    _legacy_v3_envelope,
    freeze_run_skill_snapshot_writer,
)
from app.private_work.run_skill_tree_materializer import (
    LegacyInlineRunSkillPlan,
    LegacyInlineRunSkillSourceAdapter,
    MaterializationAttemptIdentity,
    MaterializationAuthorityReadback,
    PinnedSkillVersionPlan,
    PinnedSkillVersionSourceAdapter,
    RunSkillTreeMaterializationPlan,
    RunSkillTreeMaterializer,
)
from app.private_work.run_skill_writer_cohort import (
    RunSkillWriterCohortLease,
    require_active_run_skill_writer_cohort,
)
from app.reliability.workers import WorkerRegistry
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedSkillVersionSnapshot,
    SkillArchiveFile,
    SkillAssetRef,
)
from app.shared_assets.run_snapshot_codec import encode_run_asset_snapshot
from app.shared_assets.skill_archive import load_skill_archive_package
from app.shared_assets.skill_service import normalize_skill_files
from app.shared_assets.skill_version_facts import skill_version_archive_facts
from deerflow.config.run_skill_snapshot_config import RunSkillSnapshotConfig
from deerflow.config.worker_config import WorkerConfig
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.shared_assets.skill_model import (
    SkillRow,
    SkillVersionRow,
)

_DATABASE_ENV = "ACTWEAVE_R1_PROFILE_DATABASE_URL"
_TEST_DATABASE_PATTERN = re.compile(r"deerflow_test_[0-9]+_[0-9a-f]{32}\Z")
_MIB = 1024 * 1024
_PROFILE_SCHEMA_VERSION = 1
type _WriterMode = Literal["v4_reference", "legacy_v3"]


@dataclass(frozen=True, slots=True)
class _R1VersionCoordinates:
    skill_id: uuid.UUID
    version_id: uuid.UUID
    checksum: str
    file_count: int
    content_size_bytes: int

    def as_json(self) -> dict[str, object]:
        return {
            "skill_id": str(self.skill_id),
            "version_id": str(self.version_id),
            "checksum": self.checksum,
            "file_count": self.file_count,
            "content_size_bytes": self.content_size_bytes,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> _R1VersionCoordinates:
        return cls(
            skill_id=uuid.UUID(str(value["skill_id"])),
            version_id=uuid.UUID(str(value["version_id"])),
            checksum=str(value["checksum"]),
            file_count=int(value["file_count"]),
            content_size_bytes=int(value["content_size_bytes"]),
        )


@dataclass(frozen=True, slots=True)
class _R1SeedCoordinates:
    database_name: str
    user_id: uuid.UUID
    project_id: uuid.UUID
    agent_id: uuid.UUID
    thread_id: str
    full: _R1VersionCoordinates
    near: _R1VersionCoordinates

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": _PROFILE_SCHEMA_VERSION,
            "database_name": self.database_name,
            "user_id": str(self.user_id),
            "project_id": str(self.project_id),
            "agent_id": str(self.agent_id),
            "thread_id": self.thread_id,
            "full": self.full.as_json(),
            "near": self.near.as_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> _R1SeedCoordinates:
        if value.get("schema_version") != _PROFILE_SCHEMA_VERSION:
            raise ValueError("invalid R1 resource profile coordinates")
        full = value["full"]
        near = value["near"]
        if not isinstance(full, Mapping) or not isinstance(near, Mapping):
            raise ValueError("invalid R1 resource profile versions")
        return cls(
            database_name=str(value["database_name"]),
            user_id=uuid.UUID(str(value["user_id"])),
            project_id=uuid.UUID(str(value["project_id"])),
            agent_id=uuid.UUID(str(value["agent_id"])),
            thread_id=str(value["thread_id"]),
            full=_R1VersionCoordinates.from_json(full),
            near=_R1VersionCoordinates.from_json(near),
        )


class _Authority:
    def __init__(self, readback: MaterializationAuthorityReadback) -> None:
        self._readback = readback

    async def read_materialization_authority(
        self,
        *,
        boundary: str,
        dependency_order: int | None,
    ) -> MaterializationAuthorityReadback:
        del boundary, dependency_order
        return self._readback


class _R1ProfilePhaseError(RuntimeError):
    def __init__(
        self,
        phase: str,
        *,
        child_failures: Sequence[Mapping[str, object]] = (),
    ) -> None:
        super().__init__("R1 profile phase failed")
        self.phase = phase
        self.child_failures = tuple(dict(value) for value in child_failures)


def _exception_facts(error: BaseException) -> dict[str, object]:
    chain: list[dict[str, str]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        item = {
            "module": type(current).__module__,
            "type": type(current).__name__,
        }
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and re.fullmatch(r"[0-9A-Z]{5}", value):
                item[attribute] = value
        chain.append(item)
        original = getattr(current, "orig", None)
        if isinstance(original, BaseException) and id(original) not in seen:
            current = original
        elif isinstance(current.__cause__, BaseException):
            current = current.__cause__
        elif isinstance(current.__context__, BaseException):
            current = current.__context__
        else:
            current = None
    facts: dict[str, object] = {"chain": chain}
    if isinstance(error, _R1ProfilePhaseError):
        facts["phase"] = error.phase
        if error.child_failures:
            facts["child_failures"] = list(error.child_failures)
    return facts


async def _run_internal_child(
    operation,
    *,
    output_path: Path,
    phase: str,
) -> bool:
    try:
        await operation
    except BaseException as error:
        _atomic_write_json(
            output_path,
            {
                "failure": {
                    "phase": phase,
                    "exception": _exception_facts(error),
                }
            },
        )
        return False
    return True


def _release_role_attempts() -> tuple[tuple[str, int], ...]:
    return (("gateway-a", 3), ("gateway-b", 3), ("scheduler", 2))


def _archive_facts(files: Sequence[SkillArchiveFile]):
    return skill_version_archive_facts(
        tuple(
            (
                item.path,
                hashlib.sha256(item.content).hexdigest(),
                len(item.content),
            )
            for item in files
        )
    )


def _deterministic_near_ceiling_subset(
    files: Sequence[SkillArchiveFile],
    *,
    max_content_bytes: int,
) -> tuple[SkillArchiveFile, ...]:
    canonical = tuple(files)
    manifests = tuple(item for item in canonical if item.path == "SKILL.md")
    if len(manifests) != 1 or max_content_bytes < len(manifests[0].content):
        raise ValueError("R1 resource fixture lacks a bounded root SKILL.md")
    selected = [manifests[0]]
    selected_bytes = len(manifests[0].content)
    for item in sorted(
        (value for value in canonical if value.path != "SKILL.md"),
        key=lambda value: (-len(value.content), value.path),
    ):
        if selected_bytes + len(item.content) <= max_content_bytes:
            selected.append(item)
            selected_bytes += len(item.content)
    return tuple(sorted(selected, key=lambda value: value.path))


def _version_snapshot(
    version: _R1VersionCoordinates,
) -> ResolvedSkillVersionSnapshot:
    return ResolvedSkillVersionSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope.PROJECT,
        asset_id=version.skill_id,
        version_id=version.version_id,
        checksum=version.checksum,
        catalog_generation=7,
        dependency_version_ids=(),
        file_count=version.file_count,
        content_size_bytes=version.content_size_bytes,
        secret_requirements=(),
    )


def _release_near_ceiling_files(
    files: Sequence[SkillArchiveFile],
) -> tuple[tuple[SkillArchiveFile, ...], dict[str, int]]:
    canonical = tuple(files)
    root = next((item for item in canonical if item.path == "SKILL.md"), None)
    if root is None:
        raise ValueError("R1 resource fixture lacks SKILL.md")
    selected: list[SkillArchiveFile] = [root]
    content_size = len(root.content)
    path_bytes = len(root.path.encode("utf-8"))
    media_bytes = len(root.media_type.encode("utf-8"))
    skill_id = uuid.UUID(int=1)
    version_id = uuid.UUID(int=2)

    def envelope_for(
        *,
        file_count: int,
        source_bytes: int,
        paths: int,
        media: int,
    ):
        snapshot = ResolvedSkillVersionSnapshot(
            kind=AssetKind.SKILL,
            scope=AssetScope.PROJECT,
            asset_id=skill_id,
            version_id=version_id,
            checksum="0" * 64,
            catalog_generation=7,
            dependency_version_ids=(),
            file_count=file_count,
            content_size_bytes=source_bytes,
            secret_requirements=(),
        )
        return _legacy_v3_envelope(
            snapshot,
            path_bytes=paths,
            media_type_bytes=media,
        )

    for item in sorted(
        (value for value in canonical if value.path != "SKILL.md"),
        key=lambda value: (-len(value.content), value.path),
    ):
        trial_content = content_size + len(item.content)
        trial_path = path_bytes + len(item.path.encode("utf-8"))
        trial_media = media_bytes + len(item.media_type.encode("utf-8"))
        envelope = envelope_for(
            file_count=len(selected) + 1,
            source_bytes=trial_content,
            paths=trial_path,
            media=trial_media,
        )
        if (
            envelope.source_bytes <= LEGACY_ADMISSION_POLICY.max_source_bytes_per_skill
            and envelope.codec_working_set_bytes <= LEGACY_ADMISSION_POLICY.max_codec_working_set_bytes_per_skill
            and envelope.encoded_upper_bound_bytes <= LEGACY_ADMISSION_POLICY.max_encoded_bytes_per_run
        ):
            selected.append(item)
            content_size = trial_content
            path_bytes = trial_path
            media_bytes = trial_media
    final = tuple(sorted(selected, key=lambda value: value.path))
    envelope = envelope_for(
        file_count=len(final),
        source_bytes=content_size,
        paths=path_bytes,
        media=media_bytes,
    )
    return final, {
        "source_bytes": envelope.source_bytes,
        "codec_working_set_bytes": envelope.codec_working_set_bytes,
        "encoded_upper_bound_bytes": envelope.encoded_upper_bound_bytes,
    }


def _database_url(base_url: str, database: str) -> str:
    parsed = make_url(base_url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+asyncpg")
    return parsed.set(database=database).render_as_string(hide_password=False)


@asynccontextmanager
async def _temporary_database(admin_url: str) -> AsyncIterator[tuple[str, str]]:
    database = f"deerflow_test_{os.getpid()}_{uuid.uuid4().hex}"
    if _TEST_DATABASE_PATTERN.fullmatch(database) is None:
        raise RuntimeError("unsafe disposable database coordinate")
    admin_engine = create_async_engine(
        _database_url(admin_url, "postgres"),
        isolation_level="AUTOCOMMIT",
    )
    body_error: BaseException | None = None
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f"CREATE DATABASE \"{database}\" TEMPLATE template0 ENCODING 'UTF8'"))
        try:
            yield _database_url(admin_url, database), database
        except BaseException as error:
            body_error = error
            raise
        finally:
            try:
                await admin_engine.dispose()
                cleanup_engine = create_async_engine(
                    _database_url(admin_url, "postgres"),
                    isolation_level="AUTOCOMMIT",
                )
                try:
                    async with cleanup_engine.connect() as connection:
                        await connection.execute(
                            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:database AND pid <> pg_backend_pid()"),
                            {"database": database},
                        )
                        await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
                finally:
                    await cleanup_engine.dispose()
            except BaseException:
                if body_error is None:
                    raise RuntimeError("unable to remove isolated R1 profile database") from None
                body_error.add_note("cleanup of isolated R1 profile database also failed")
    finally:
        await admin_engine.dispose()


async def _seed_scope(
    session: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    definition_id = uuid.uuid4()
    thread_id = f"r1-profile-{uuid.uuid4().hex}"
    agent_payload = AgentPayload(
        description="R1 resource agent",
        soul="",
        model_ref="default",
        tool_groups=(),
        skill_refs=(),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
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
            "username": f"r1_{user_id.hex[:16]}",
        },
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id,slug,display_name,created_by_user_id
               ) VALUES (:project_id,:slug,'R1 resource profile',:user_id)"""
        ),
        {
            "project_id": project_id,
            "slug": f"r1-resource-{project_id.hex[:12]}",
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
            "id": uuid.uuid4(),
            "project_id": project_id,
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text(
            """INSERT INTO agents (
                   id,scope,project_id,slug,display_name,status,
                   definition_id,description,agents_instructions,soul,
                   identity,user_context,model_ref,model_settings,tool_groups,
                   payload_schema_version,payload_checksum,revision,
                   created_by_user_id,updated_by_user_id
               ) VALUES (
                   :agent_id,'project',:project_id,'r1-resource-agent',
                   'R1 resource agent','active',:definition_id,:description,
                   '','', '', '',:model_ref,'{}'::jsonb,'[]'::jsonb,4,
                   :payload_checksum,1,:user_id,:user_id
               )"""
        ),
        {
            "agent_id": agent_id,
            "project_id": project_id,
            "user_id": str(user_id),
            "definition_id": definition_id,
            "description": agent_payload.description,
            "model_ref": agent_payload.model_ref,
            "payload_checksum": agent_payload_checksum(agent_payload),
        },
    )
    await session.execute(
        text(
            """INSERT INTO threads_meta (
                   thread_id,owner_user_id,status,metadata_json,
                   created_at,updated_at,project_id,agent_asset_id,agent_scope
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
    return user_id, project_id, agent_id, thread_id


async def _seed_version_parent(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    slug: str,
    facts: object,
) -> _R1VersionCoordinates:
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    await session.execute(
        text(
            """INSERT INTO skills (
                   id,scope,project_id,slug,display_name,status,
                   created_by_user_id
               ) VALUES (
                   :skill_id,'project',:project_id,:slug,:display_name,
                   'active',:user_id
               )"""
        ),
        {
            "skill_id": skill_id,
            "project_id": project_id,
            "slug": slug,
            "display_name": slug,
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text(
            """INSERT INTO skill_versions (
                   id,skill_id,version_number,secret_requirements,
                   scan_decision,payload_checksum,file_count,
                   content_size_bytes,files_sealed,created_by_user_id
               ) VALUES (
                   :version_id,:skill_id,1,'[]'::jsonb,'allow',:checksum,
                   :file_count,:content_size,false,:user_id
               )"""
        ),
        {
            "version_id": version_id,
            "skill_id": skill_id,
            "checksum": facts.payload_checksum,
            "file_count": facts.file_count,
            "content_size": facts.content_size_bytes,
            "user_id": str(user_id),
        },
    )
    return _R1VersionCoordinates(
        skill_id=skill_id,
        version_id=version_id,
        checksum=facts.payload_checksum,
        file_count=facts.file_count,
        content_size_bytes=facts.content_size_bytes,
    )


async def _assemble_version(
    session: AsyncSession,
    *,
    version: _R1VersionCoordinates,
    files: Sequence[SkillArchiveFile],
) -> None:
    await session.execute(
        text("SELECT set_config('deerflow.asset_version_assembly', :version_id,true)"),
        {"version_id": str(version.version_id)},
    )
    insert_file = text(
        """INSERT INTO skill_version_files (
               skill_version_id,path,media_type,size_bytes,sha256,content
           ) VALUES (
               :version_id,:path,:media_type,:size_bytes,:sha256,:content
           )"""
    )
    canonical = tuple(files)
    for offset in range(0, len(canonical), 128):
        await session.execute(
            insert_file,
            [
                {
                    "version_id": version.version_id,
                    "path": item.path,
                    "media_type": item.media_type,
                    "size_bytes": len(item.content),
                    "sha256": hashlib.sha256(item.content).hexdigest(),
                    "content": item.content,
                }
                for item in canonical[offset : offset + 128]
            ],
        )
    await session.execute(
        text("UPDATE skill_versions SET files_sealed=true WHERE id=:version_id"),
        {"version_id": version.version_id},
    )
    await session.execute(
        text("UPDATE skills SET current_version_id=:version_id WHERE id=:skill_id"),
        {"version_id": version.version_id, "skill_id": version.skill_id},
    )


async def _install_and_seed(
    database_url: str,
    database_name: str,
    archive_path: Path,
) -> tuple[_R1SeedCoordinates, dict[str, object]]:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        archive = await asyncio.to_thread(archive_path.read_bytes)
        archive_sha256 = hashlib.sha256(archive).hexdigest()
        archive_size = len(archive)
        files = tuple(
            normalize_skill_files(
                load_skill_archive_package(
                    archive,
                    filename=archive_path.name,
                    request_id="r1-resource-profile",
                ),
                request_id="r1-resource-profile",
            )
        )
        del archive
        full_facts = _archive_facts(files)
        near_files, near_envelope = _release_near_ceiling_files(files)
        near_facts = _archive_facts(near_files)
        if near_files == files:
            raise RuntimeError("R1 accepted fixture must not claim full ppt-master")
        async with factory() as session, session.begin():
            user_id, project_id, agent_id, thread_id = await _seed_scope(session)
        async with factory() as session, session.begin():
            full = await _seed_version_parent(
                session,
                project_id=project_id,
                user_id=user_id,
                slug="ppt-master-full-r1-rejected",
                facts=full_facts,
            )
            await _assemble_version(
                session,
                version=full,
                files=files,
            )
        async with factory() as session, session.begin():
            near = await _seed_version_parent(
                session,
                project_id=project_id,
                user_id=user_id,
                slug="ppt-master-near-r1-accepted",
                facts=near_facts,
            )
            await _assemble_version(
                session,
                version=near,
                files=near_files,
            )
        coordinates = _R1SeedCoordinates(
            database_name=database_name,
            user_id=user_id,
            project_id=project_id,
            agent_id=agent_id,
            thread_id=thread_id,
            full=full,
            near=near,
        )
        del files, near_files
        gc.collect()
        async with engine.connect() as connection:
            raw = await connection.get_raw_connection()
            await raw.driver_connection.execute("CHECKPOINT")
            database_size = int(await connection.scalar(text("SELECT pg_database_size(current_database())")))
        return coordinates, {
            "archive_filename": archive_path.name,
            "archive_sha256": archive_sha256,
            "archive_size_bytes": archive_size,
            "full": {
                "full_archive": True,
                "file_count": full.file_count,
                "content_size_bytes": full.content_size_bytes,
            },
            "accepted_near_ceiling": {
                "full_archive": False,
                "whole_file_subset": True,
                "file_count": near.file_count,
                "content_size_bytes": near.content_size_bytes,
                **near_envelope,
                "encoded_ceiling_utilization": (near_envelope["encoded_upper_bound_bytes"] / LEGACY_ADMISSION_POLICY.max_encoded_bytes_per_run),
            },
            "database_size_bytes_after_seed": database_size,
        }
    finally:
        await engine.dispose()


async def _locked_skill(
    session: AsyncSession,
    version: _R1VersionCoordinates,
) -> tuple[SkillRow, SkillVersionRow]:
    row = (
        await session.execute(
            select(SkillRow, SkillVersionRow)
            .join(
                SkillVersionRow,
                SkillVersionRow.skill_id == SkillRow.id,
            )
            .where(
                SkillRow.id == version.skill_id,
                SkillVersionRow.id == version.version_id,
            )
            .with_for_update(read=True, of=[SkillRow, SkillVersionRow])
        )
    ).one_or_none()
    if row is None:
        raise RuntimeError("R1 profile exact Skill Version is missing")
    return row


async def _persist_run(
    session: AsyncSession,
    *,
    coordinates: _R1SeedCoordinates,
    version: _R1VersionCoordinates,
    writer_mode: _WriterMode,
    legacy_snapshot: Mapping[str, object] | None,
    run_label: str,
) -> str:
    if writer_mode == "legacy_v3" and legacy_snapshot is None:
        raise ValueError("legacy R1 run requires its prepared v3 payload")
    if writer_mode == "v4_reference" and legacy_snapshot is not None:
        raise ValueError("v4 R1 run forbids a legacy payload")
    run_id = f"r1-{run_label}-{uuid.uuid4().hex}"
    if len(run_id) > 64:
        raise ValueError("R1 profile Run coordinate is too long")
    await session.execute(
        text(
            """INSERT INTO runs (
                   run_id,thread_id,owner_user_id,status,multitask_strategy,
                   metadata_json,kwargs_json,origin_trace_id,message_count,
                   total_input_tokens,total_output_tokens,total_tokens,
                   llm_call_count,lead_agent_tokens,subagent_tokens,
                   middleware_tokens,token_usage_by_model,created_at,updated_at,
                   project_id,finalization_status,asset_closure_sealed
               ) VALUES (
                   :run_id,:thread_id,:user_id,'pending','reject','{}'::json,
                   '{}'::json,:trace_id,0,0,0,0,0,0,0,0,'{}'::json,now(),now(),
                   :project_id,'pending',false
               )"""
        ),
        {
            "run_id": run_id,
            "thread_id": coordinates.thread_id,
            "user_id": str(coordinates.user_id),
            "trace_id": f"trace-{run_id}",
            "project_id": coordinates.project_id,
        },
    )
    await session.execute(
        text("SELECT set_config('deerflow.run_asset_closure_assembly', :run_id,true)"),
        {"run_id": run_id},
    )
    agent_definition_id = uuid.uuid4()
    agent_payload = AgentPayload(
        description="R1 rollback resource profile",
        soul="Materialize one exact admitted Skill.",
        model_ref="r1-resource-profile-model",
        tool_groups=(),
        skill_refs=(
            SkillAssetRef(
                scope=AssetScope.PROJECT,
                asset_id=version.skill_id,
            ),
        ),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
    agent_checksum = agent_payload_checksum(agent_payload)
    agent_snapshot = encode_run_asset_snapshot(
        ResolvedAgentSnapshot(
            kind=AssetKind.AGENT,
            scope=AssetScope.PROJECT,
            asset_id=coordinates.agent_id,
            version_id=agent_definition_id,
            checksum=agent_checksum,
            catalog_generation=7,
            dependency_version_ids=(version.version_id,),
            payload=agent_payload,
            skill_version_ids=(version.version_id,),
            slug="r1-resource-agent",
        )
    )
    await session.execute(
        text(
            """INSERT INTO run_asset_versions (
                   project_id,owner_user_id,thread_id,run_id,asset_kind,
                   dependency_order,asset_scope,asset_id,version_id,
                   payload_checksum,catalog_generation,snapshot_schema_version,
                   snapshot_json
               ) VALUES (
                   :project_id,:user_id,:thread_id,:run_id,'agent',0,'project',
                   :asset_id,:version_id,:checksum,7,3,CAST(:snapshot AS jsonb)
               )"""
        ),
        {
            "project_id": coordinates.project_id,
            "user_id": str(coordinates.user_id),
            "thread_id": coordinates.thread_id,
            "run_id": run_id,
            "asset_id": coordinates.agent_id,
            "version_id": agent_definition_id,
            "checksum": agent_checksum,
            "snapshot": json.dumps(agent_snapshot),
        },
    )
    if writer_mode == "legacy_v3":
        skill_snapshot = legacy_snapshot
        schema_version = 3
    else:
        skill_snapshot = {
            "schema_version": 4,
            "kind": "skill",
            "scope": "project",
            "asset_id": str(version.skill_id),
            "version_id": str(version.version_id),
            "checksum": version.checksum,
            "catalog_generation": 7,
            "dependency_version_ids": [],
            "skill": {
                "source": "skill_version_ref",
                "file_count": version.file_count,
                "content_size_bytes": version.content_size_bytes,
            },
        }
        schema_version = 4
    await session.execute(
        text(
            """INSERT INTO run_asset_versions (
                   project_id,owner_user_id,thread_id,run_id,asset_kind,
                   dependency_order,asset_scope,asset_id,version_id,
                   payload_checksum,catalog_generation,snapshot_schema_version,
                   snapshot_json
               ) VALUES (
                   :project_id,:user_id,:thread_id,:run_id,'skill',1,'project',
                   :asset_id,:version_id,:checksum,7,:schema_version,
                   CAST(:snapshot AS jsonb)
               )"""
        ),
        {
            "project_id": coordinates.project_id,
            "user_id": str(coordinates.user_id),
            "thread_id": coordinates.thread_id,
            "run_id": run_id,
            "asset_id": version.skill_id,
            "version_id": version.version_id,
            "checksum": version.checksum,
            "schema_version": schema_version,
            "snapshot": json.dumps(skill_snapshot),
        },
    )
    if writer_mode == "v4_reference":
        await session.execute(
            text(
                """INSERT INTO run_skill_version_refs (
                       project_id,owner_user_id,thread_id,run_id,asset_kind,
                       dependency_order,asset_scope,snapshot_schema_version,
                       skill_project_id,skill_id,skill_version_id,
                       payload_checksum,file_count,content_size_bytes
                   ) VALUES (
                       :project_id,:user_id,:thread_id,:run_id,'skill',1,
                       'project',4,:project_id,:skill_id,:version_id,:checksum,
                       :file_count,:content_size
                   )"""
            ),
            {
                "project_id": coordinates.project_id,
                "user_id": str(coordinates.user_id),
                "thread_id": coordinates.thread_id,
                "run_id": run_id,
                "skill_id": version.skill_id,
                "version_id": version.version_id,
                "checksum": version.checksum,
                "file_count": version.file_count,
                "content_size": version.content_size_bytes,
            },
        )
    await session.execute(
        text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
        {"run_id": run_id},
    )
    return run_id


def _current_rss_bytes(process_id: int | None = None) -> int:
    target = os.getpid() if process_id is None else process_id
    output = subprocess.check_output(
        ["ps", "-o", "rss=", "-p", str(target)],
        text=True,
    )
    return int(output.strip()) * 1024


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


async def _wait_for_path(path: Path, *, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("R1 profile coordination timed out")
        await asyncio.sleep(0.01)


async def _full_preflight(
    database_url: str,
    coordinates: _R1SeedCoordinates,
) -> dict[str, object]:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    writer_readback = freeze_run_skill_snapshot_writer(
        RunSkillSnapshotConfig(
            writer_mode="legacy_v3",
            expected_artifact_version=(RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION),
            expected_legacy_policy_digest=(LEGACY_ADMISSION_POLICY.canonical_digest()),
        )
    )
    cohort_lease = await RunSkillWriterCohortLease.acquire(
        engine,
        writer_readback,
        process_role="gateway",
        heartbeat_interval_seconds=0.1,
        process_authority=True,
    )
    permit_attempt_count = 0
    content_query_count = 0

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal permit_attempt_count, content_query_count
        if "pg_try_advisory_xact_lock" in statement:
            permit_attempt_count += 1
        if "skill_version_files.content" in statement:
            content_query_count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    baseline_rss = _current_rss_bytes()
    started = time.perf_counter()
    outcome = "unexpected_success"
    try:
        async with factory() as session, session.begin():
            await require_active_run_skill_writer_cohort(
                session,
                writer_readback,
            )
            locked = await _locked_skill(session, coordinates.full)
            try:
                await LegacyRunSkillSnapshotWriter().prepare(
                    session,
                    request_id="r1-full-preflight",
                    locked_skills=(locked,),
                    snapshots=(_version_snapshot(coordinates.full),),
                )
            except PrivateWorkTooLarge:
                outcome = "too_large"
        return {
            "source_kind": "full_ppt_master",
            "full_archive": True,
            "outcome": outcome,
            "http_status": 413 if outcome == "too_large" else None,
            "permit_attempt_count": permit_attempt_count,
            "content_query_count": content_query_count,
            "latency_seconds": time.perf_counter() - started,
            "baseline_rss_bytes": baseline_rss,
            "peak_rss_bytes": _peak_rss_bytes(),
            "cohort_readback": {
                "writer_mode": cohort_lease.readback.writer_mode,
                "artifact_version": cohort_lease.readback.artifact_version,
                "legacy_policy_digest": (cohort_lease.readback.legacy_policy_digest),
                "process_role": cohort_lease.readback.process_role,
                "ready": cohort_lease.readback.ready,
            },
        }
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        await cohort_lease.close()
        await engine.dispose()


async def _mode_readback_worker(
    *,
    writer_mode: _WriterMode,
    output_path: Path,
) -> None:
    config = (
        RunSkillSnapshotConfig()
        if writer_mode == "v4_reference"
        else RunSkillSnapshotConfig(
            writer_mode="legacy_v3",
            expected_artifact_version=(RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION),
            expected_legacy_policy_digest=(LEGACY_ADMISSION_POLICY.canonical_digest()),
        )
    )
    readback = freeze_run_skill_snapshot_writer(config)
    cohort_readback: dict[str, object] | None = None
    database_url = os.environ.get(_DATABASE_ENV, "").strip()
    if database_url:
        engine = create_async_engine(database_url)
        cohort_lease = await RunSkillWriterCohortLease.acquire(
            engine,
            readback,
            process_role="gateway",
            heartbeat_interval_seconds=0.1,
            process_authority=True,
        )
        try:
            cohort_readback = {
                "writer_mode": cohort_lease.readback.writer_mode,
                "artifact_version": cohort_lease.readback.artifact_version,
                "legacy_policy_digest": (cohort_lease.readback.legacy_policy_digest),
                "process_role": cohort_lease.readback.process_role,
                "ready": cohort_lease.readback.ready,
            }
        finally:
            await cohort_lease.close()
            await engine.dispose()
    _atomic_write_json(
        output_path,
        {
            "writer_mode": readback.writer_mode,
            "artifact_version": readback.artifact_version,
            "legacy_policy_digest": readback.legacy_policy_digest,
            "ready": readback.ready,
            "cohort_readback": cohort_readback,
        },
    )


async def _attempt_worker(
    *,
    spec_path: Path,
    coordination_root: Path,
    output_path: Path,
    role: str,
    attempt_count: int,
) -> None:
    database_url = os.environ.get(_DATABASE_ENV, "").strip()
    if not database_url:
        raise RuntimeError("R1 profile child database URL is unavailable")
    if (role, attempt_count) not in _release_role_attempts() and not (role == "gateway-coexist" and attempt_count == 1):
        raise ValueError("invalid R1 writer process role")
    coordinates = _R1SeedCoordinates.from_json(json.loads(spec_path.read_text(encoding="utf-8")))
    readback = freeze_run_skill_snapshot_writer(
        RunSkillSnapshotConfig(
            writer_mode="legacy_v3",
            expected_artifact_version=(RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION),
            expected_legacy_policy_digest=(LEGACY_ADMISSION_POLICY.canonical_digest()),
        )
    )
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cohort_role: Literal["gateway", "scheduler"] = "scheduler" if role == "scheduler" else "gateway"
    cohort_lease = await RunSkillWriterCohortLease.acquire(
        engine,
        readback,
        process_role=cohort_role,
        heartbeat_interval_seconds=0.1,
        process_authority=True,
    )
    active_attempt: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "r1_profile_active_attempt",
        default=None,
    )
    permit_attempt_count: dict[str, int] = {}
    content_query_count: dict[str, int] = {}
    gate_started: dict[str, float] = {}
    gate_latency: dict[str, float] = {}

    def before_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        label = active_attempt.get()
        if label is None:
            return
        if "pg_try_advisory_xact_lock" in statement:
            permit_attempt_count[label] = permit_attempt_count.get(label, 0) + 1
            gate_started[label] = time.perf_counter()
        if "skill_version_files.content" in statement:
            content_query_count[label] = content_query_count.get(label, 0) + 1

    def after_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        label = active_attempt.get()
        if label is not None and "pg_try_advisory_xact_lock" in statement:
            gate_latency[label] = time.perf_counter() - gate_started[label]

    event.listen(engine.sync_engine, "before_cursor_execute", before_query)
    event.listen(engine.sync_engine, "after_cursor_execute", after_query)
    baseline_rss = _current_rss_bytes()
    process_ready = coordination_root / f"process-ready-{role}"
    process_ready.touch()
    await _wait_for_path(coordination_root / "start")

    async def attempt(index: int) -> dict[str, object]:
        label = f"{role}-{index}"
        token = active_attempt.set(label)
        started = time.perf_counter()
        outcome = "error"
        run_id: str | None = None
        encoded_upper: int | None = None
        actual_encoded: int | None = None
        try:
            try:
                async with factory() as session, session.begin():
                    await require_active_run_skill_writer_cohort(
                        session,
                        readback,
                    )
                    locked = await _locked_skill(session, coordinates.near)
                    prepared = await LegacyRunSkillSnapshotWriter().prepare(
                        session,
                        request_id=f"r1-{label}",
                        locked_skills=(locked,),
                        snapshots=(_version_snapshot(coordinates.near),),
                    )
                    encoded_upper = prepared.encoded_upper_bound_bytes
                    actual_encoded = prepared.actual_encoded_bytes
                    run_id = await _persist_run(
                        session,
                        coordinates=coordinates,
                        version=coordinates.near,
                        writer_mode="legacy_v3",
                        legacy_snapshot=prepared.snapshot_jsons[0],
                        run_label=label,
                    )
                    outcome = "winner-ready"
                    _atomic_write_json(
                        coordination_root / f"winner-ready-{label}.json",
                        {
                            "label": label,
                            "outcome": outcome,
                        },
                    )
                    await _wait_for_path(
                        coordination_root / "release",
                        timeout_seconds=120.0,
                    )
                outcome = "success"
            except LegacyAdmissionBusy:
                outcome = "retryable_busy"
            except PrivateWorkTooLarge:
                outcome = "unexpected_too_large"
            result = {
                "role": role,
                "attempt_label": label,
                "outcome": outcome,
                "run_id": run_id,
                "permit_attempt_count": permit_attempt_count.get(label, 0),
                "content_query_count": content_query_count.get(label, 0),
                "gate_query_seconds": gate_latency.get(label, 0.0),
                "latency_seconds": time.perf_counter() - started,
                "encoded_upper_bound_bytes": encoded_upper,
                "actual_encoded_bytes": actual_encoded,
            }
            _atomic_write_json(
                coordination_root / f"done-{label}.json",
                result,
            )
            return result
        finally:
            active_attempt.reset(token)

    try:
        results = await asyncio.gather(*(attempt(index) for index in range(attempt_count)))
        _atomic_write_json(
            output_path,
            {
                "process_label": role,
                "writer_readback": readback.as_public_dict(),
                "cohort_readback": {
                    "writer_mode": cohort_lease.readback.writer_mode,
                    "artifact_version": cohort_lease.readback.artifact_version,
                    "legacy_policy_digest": (cohort_lease.readback.legacy_policy_digest),
                    "process_role": cohort_lease.readback.process_role,
                    "ready": cohort_lease.readback.ready,
                },
                "baseline_rss_bytes": baseline_rss,
                "peak_rss_bytes": _peak_rss_bytes(),
                "final_rss_bytes": _current_rss_bytes(),
                "attempts": results,
            },
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_query)
        event.remove(engine.sync_engine, "after_cursor_execute", after_query)
        await cohort_lease.close()
        await engine.dispose()


async def _materializer_worker(
    *,
    spec_path: Path,
    runs_path: Path,
    output_path: Path,
    materialization_root: Path,
    writer_mode: _WriterMode,
    coordination_root: Path | None,
) -> None:
    database_url = os.environ.get(_DATABASE_ENV, "").strip()
    if not database_url:
        raise RuntimeError("R1 materializer database URL is unavailable")
    coordinates = _R1SeedCoordinates.from_json(json.loads(spec_path.read_text(encoding="utf-8")))
    runs_value = json.loads(runs_path.read_text(encoding="utf-8"))
    runs = tuple(runs_value["runs"])
    mode_config = (
        RunSkillSnapshotConfig()
        if writer_mode == "v4_reference"
        else RunSkillSnapshotConfig(
            writer_mode="legacy_v3",
            expected_artifact_version=(RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION),
            expected_legacy_policy_digest=(LEGACY_ADMISSION_POLICY.canonical_digest()),
        )
    )
    readback = freeze_run_skill_snapshot_writer(mode_config)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_id = uuid.uuid4()
    config = WorkerConfig()
    materializer = RunSkillTreeMaterializer(
        materialization_root=materialization_root,
        worker_config=config,
        legacy_source_adapter=LegacyInlineRunSkillSourceAdapter(factory),
        pinned_source_adapter=PinnedSkillVersionSourceAdapter(factory),
    )
    legacy_snapshot_queries = 0
    v4_content_queries = 0

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal legacy_snapshot_queries, v4_content_queries
        if "run_asset_versions.snapshot_json" in statement and "NOT (EXISTS" in statement:
            legacy_snapshot_queries += 1
        if "skill_version_files.content" in statement:
            v4_content_queries += 1

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    baseline_rss = _current_rss_bytes()
    try:
        await WorkerRegistry(factory, version="r1-resource-profile").register(
            worker_id,
            frozenset({"private_run"}),
            config.max_concurrent_jobs,
            execution_domain_affinity=None,
            now=datetime.now(UTC),
        )
        if coordination_root is not None:
            (coordination_root / "process-ready-materializer").touch()
            await _wait_for_path(coordination_root / "start")
        results: list[dict[str, object]] = []
        for index, raw in enumerate(runs):
            if not isinstance(raw, Mapping):
                raise ValueError("invalid materializer Run coordinate")
            run_id = str(raw["run_id"])
            schema_version = int(raw["snapshot_schema_version"])
            identity = MaterializationAttemptIdentity(
                job_id=uuid.uuid4(),
                attempt_id=uuid.uuid4(),
                worker_id=worker_id,
            )
            fingerprint = hashlib.sha256(run_id.encode()).hexdigest()
            if schema_version == 3:
                version_plan = LegacyInlineRunSkillPlan(
                    dependency_order=1,
                    scope=AssetScope.PROJECT,
                    asset_id=coordinates.near.skill_id,
                    version_id=coordinates.near.version_id,
                    payload_checksum=coordinates.near.checksum,
                    catalog_generation=7,
                    snapshot_schema_version=3,
                    file_count=coordinates.near.file_count,
                    content_size_bytes=coordinates.near.content_size_bytes,
                    secret_requirements=(),
                )
            elif schema_version == 4:
                version_plan = PinnedSkillVersionPlan(
                    dependency_order=1,
                    scope=AssetScope.PROJECT,
                    asset_id=coordinates.near.skill_id,
                    version_id=coordinates.near.version_id,
                    payload_checksum=coordinates.near.checksum,
                    catalog_generation=7,
                    dependency_version_ids=(),
                    file_count=coordinates.near.file_count,
                    content_size_bytes=coordinates.near.content_size_bytes,
                    secret_requirements=(),
                )
            else:
                raise ValueError("unsupported R1 profile Run schema")
            plan = RunSkillTreeMaterializationPlan(
                project_id=coordinates.project_id,
                owner_user_id=str(coordinates.user_id),
                thread_id=coordinates.thread_id,
                run_id=run_id,
                runtime_kind="chat",
                attempt_identity=identity,
                plan_fingerprint=fingerprint,
                skill_versions=(version_plan,),
            )
            authority = _Authority(
                MaterializationAuthorityReadback(
                    attempt_identity=identity,
                    plan_fingerprint=fingerprint,
                )
            )
            started = time.perf_counter()
            pending = await materializer.materialize(
                plan=plan,
                authority=authority,  # type: ignore[arg-type]
            )
            tree_file_count = sum(1 for path in pending.source.worker_root.rglob("*") if path.is_file())
            manifest_present = (pending.source.worker_root / ".actweave-run-mount.json").is_file()
            await pending.aclose()
            results.append(
                {
                    "index": index,
                    "snapshot_schema_version": schema_version,
                    "latency_seconds": time.perf_counter() - started,
                    "tree_file_count": tree_file_count,
                    "expected_tree_file_count": coordinates.near.file_count + 1,
                    "run_mount_manifest_present": manifest_present,
                }
            )
        roots_remaining = sorted(path.name for path in materialization_root.iterdir()) if materialization_root.exists() else []
        _atomic_write_json(
            output_path,
            {
                "writer_readback": readback.as_public_dict(),
                "baseline_rss_bytes": baseline_rss,
                "peak_rss_bytes": _peak_rss_bytes(),
                "final_rss_bytes": _current_rss_bytes(),
                "legacy_snapshot_query_count": legacy_snapshot_queries,
                "v4_content_query_count": v4_content_queries,
                "roots_remaining": roots_remaining,
                "runs": results,
            },
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        await engine.dispose()


async def _gate_release_probes(database_url: str) -> dict[str, bool]:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    gate = LegacyAdmissionByteGate()

    async def reacquire(request_id: str) -> bool:
        async with factory() as session, session.begin():
            await gate.acquire(session, request_id=request_id)
        return True

    try:
        async with factory() as session, session.begin():
            await gate.acquire(session, request_id="profile-commit")
        commit_released = await reacquire("profile-after-commit")

        async with factory() as session:
            transaction = await session.begin()
            await gate.acquire(session, request_id="profile-rollback")
            await transaction.rollback()
        rollback_released = await reacquire("profile-after-rollback")

        acquired = asyncio.Event()
        hold = asyncio.Event()

        async def cancelled_holder() -> None:
            async with factory() as session, session.begin():
                await gate.acquire(session, request_id="profile-cancel")
                acquired.set()
                await hold.wait()

        task = asyncio.create_task(cancelled_holder())
        await acquired.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        cancel_released = await reacquire("profile-after-cancel")

        parsed = make_url(database_url).set(drivername="postgresql")
        physical = await asyncpg.connect(parsed.render_as_string(hide_password=False))
        await physical.execute("BEGIN")
        physical_acquired = await physical.fetchval(
            "SELECT pg_try_advisory_xact_lock($1)",
            LEGACY_ADMISSION_BYTE_GATE_KEY,
        )
        await physical.close()
        await asyncio.sleep(0.05)
        physical_connection_close_releases_permit = physical_acquired is True and await reacquire("profile-after-physical-close")
        return {
            "commit_releases_permit": commit_released,
            "rollback_releases_permit": rollback_released,
            "cancel_releases_permit": cancel_released,
            "physical_connection_close_releases_permit": (physical_connection_close_releases_permit),
        }
    finally:
        await engine.dispose()


async def _run_command(
    *arguments: str,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await process.communicate()
    return (
        int(process.returncode or 0),
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _postgres_container_sample(container_name: str) -> dict[str, int]:
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
postmaster_kib=$(awk '/^VmRSS:/ {print $2}' /proc/1/status)
printf '%s %s %s %s\n' \
  "$(cat /sys/fs/cgroup/memory.current)" \
  "$(cat /sys/fs/cgroup/memory.max)" \
  "$((postmaster_kib * 1024))" \
  "$aggregate"
"""
    code, stdout, _stderr = await _run_command(
        "container",
        "exec",
        container_name,
        "sh",
        "-c",
        shell,
    )
    if code != 0:
        raise RuntimeError("unable to sample PostgreSQL container memory")
    values = tuple(int(item) for item in stdout.split())
    if len(values) != 4:
        raise RuntimeError("invalid PostgreSQL container memory sample")
    return {
        "cgroup_memory_bytes": values[0],
        "cgroup_limit_bytes": values[1],
        "postmaster_rss_bytes": values[2],
        "aggregate_process_rss_bytes": values[3],
    }


async def _postgres_oom_events(container_name: str) -> dict[str, int]:
    code, stdout, _stderr = await _run_command(
        "container",
        "exec",
        container_name,
        "cat",
        "/sys/fs/cgroup/memory.events",
    )
    if code != 0:
        raise RuntimeError("unable to read PostgreSQL OOM counters")
    return {key: int(value) for line in stdout.splitlines() for key, value in [line.split()]}


async def _postgres_identity(database_url: str) -> dict[str, object]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT pg_postmaster_start_time()::text, pg_is_in_recovery(), pg_database_size(current_database())"))).one()
        return {
            "postmaster_started_at": str(row[0]),
            "in_recovery": bool(row[1]),
            "database_size_bytes": int(row[2]),
        }
    finally:
        await engine.dispose()


async def _postgres_wal_lsn(database_url: str) -> str:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return str(await connection.scalar(text("SELECT pg_current_wal_lsn()::text")))
    finally:
        await engine.dispose()


async def _postgres_wal_bytes(
    database_url: str,
    *,
    before: str,
    after: str,
) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return int(
                await connection.scalar(
                    text("SELECT pg_wal_lsn_diff(CAST(:after AS text)::pg_lsn, CAST(:before AS text)::pg_lsn)"),
                    {"before": before, "after": after},
                )
            )
    finally:
        await engine.dispose()


def _oom_unchanged(
    before: Mapping[str, int],
    after: Mapping[str, int],
) -> bool:
    return all(after.get(key, 0) == before.get(key, 0) for key in ("oom", "oom_kill", "oom_group_kill"))


async def _sample_child_processes(
    children: Sequence[tuple[str, asyncio.subprocess.Process]],
    peaks: dict[str, int],
) -> None:
    for label, process in children:
        if process.returncode is not None or process.pid is None:
            continue
        try:
            peaks[label] = max(
                peaks.get(label, 0),
                _current_rss_bytes(process.pid),
            )
        except (subprocess.CalledProcessError, ValueError):
            pass


def _child_failure_facts(
    label: str,
    output_path: Path,
    *,
    stderr: bytes = b"",
) -> dict[str, object]:
    failure: object = None
    if output_path.exists():
        try:
            value = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = None
        if isinstance(value, Mapping):
            failure = value.get("failure")
    stderr_types = sorted(
        {
            match.group(1).rsplit(".", 1)[-1]
            for line in stderr.decode("utf-8", errors="replace").splitlines()
            if (
                match := re.match(
                    r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))(?::|$)",
                    line,
                )
            )
        }
    )
    return {
        "process_label": label,
        "failure": failure if isinstance(failure, Mapping) else None,
        "stderr_exception_types": stderr_types,
    }


async def _failed_children_facts(
    children: Sequence[tuple[str, asyncio.subprocess.Process, Path]],
) -> list[dict[str, object]]:
    failures: list[dict[str, object]] = []
    for label, process, output_path in children:
        if process.returncode in {None, 0}:
            continue
        _stdout, stderr = await process.communicate()
        failures.append(
            _child_failure_facts(
                label,
                output_path,
                stderr=stderr,
            )
        )
    return failures


async def _stop_children(
    children: Sequence[tuple[str, asyncio.subprocess.Process, Path]],
) -> None:
    for _label, process, _path in children:
        if process.returncode is None:
            process.terminate()
    for _label, process, _path in children:
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.communicate(), timeout=10.0)
            except TimeoutError:
                process.kill()
                await process.communicate()


async def _finish_children(
    children: Sequence[tuple[str, asyncio.subprocess.Process, Path]],
    *,
    postgres_samples: list[dict[str, int]],
    container_name: str,
    process_peaks: dict[str, int],
    timeout_seconds: float = 180.0,
) -> list[dict[str, object]]:
    deadline = time.monotonic() + timeout_seconds
    process_pairs = tuple((label, process) for label, process, _path in children)
    while any(process.returncode is None for _label, process in process_pairs):
        if time.monotonic() >= deadline:
            for _label, process in process_pairs:
                if process.returncode is None:
                    process.terminate()
            raise TimeoutError("R1 child process completion timed out")
        postgres_samples.append(await _postgres_container_sample(container_name))
        await _sample_child_processes(process_pairs, process_peaks)
        await asyncio.sleep(0.1)
    outputs: list[dict[str, object]] = []
    for label, process, output_path in children:
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise _R1ProfilePhaseError(
                "child_completion",
                child_failures=(
                    _child_failure_facts(
                        label,
                        output_path,
                        stderr=stderr,
                    ),
                ),
            )
        if stdout.strip() or stderr.strip():
            raise RuntimeError(f"R1 child process emitted output for role {label}")
        outputs.append(json.loads(output_path.read_text(encoding="utf-8")))
    return outputs


async def _run_attempt_topology(
    *,
    script_path: Path,
    database_url: str,
    spec_path: Path,
    temporary_root: Path,
    container_name: str,
    postgres_samples: list[dict[str, int]],
) -> dict[str, object]:
    coordination_root = temporary_root / "attempt-coordination"
    coordination_root.mkdir(mode=0o700)
    child_env = dict(os.environ)
    child_env[_DATABASE_ENV] = database_url
    children: list[tuple[str, asyncio.subprocess.Process, Path]] = []
    process_peaks: dict[str, int] = {}
    phase = "spawn_children"
    try:
        for role, attempt_count in _release_role_attempts():
            output_path = temporary_root / f"attempt-{role}.json"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script_path),
                "--attempt-worker",
                "--spec",
                str(spec_path),
                "--coordination-root",
                str(coordination_root),
                "--worker-output",
                str(output_path),
                "--role",
                role,
                "--attempt-count",
                str(attempt_count),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
            children.append((role, process, output_path))
        distinct_os_processes = len({process.pid for _role, process, _path in children}) == 3
        phase = "wait_process_ready"
        ready_paths = {role: coordination_root / f"process-ready-{role}" for role, _attempt_count in _release_role_attempts()}
        ready_deadline = time.monotonic() + 60.0
        while not all(path.exists() for path in ready_paths.values()):
            failed = await _failed_children_facts(children)
            if failed:
                raise _R1ProfilePhaseError(
                    phase,
                    child_failures=failed,
                )
            if time.monotonic() >= ready_deadline:
                raise TimeoutError("R1 profile coordination timed out")
            await asyncio.sleep(0.01)
        phase = "wal_before"
        wal_before = await _postgres_wal_lsn(database_url)
        started = time.perf_counter()
        (coordination_root / "start").touch()
        deadline = time.monotonic() + 180.0
        phase = "wait_backpressure"
        while True:
            winners = tuple(coordination_root.glob("winner-ready-*.json"))
            done = tuple(coordination_root.glob("done-*.json"))
            busy_done = sum(1 for path in done if json.loads(path.read_text(encoding="utf-8")).get("outcome") == "retryable_busy")
            if len(winners) == 1 and busy_done == 7:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("R1 attempt backpressure did not converge")
            failed = await _failed_children_facts(children)
            if failed:
                raise _R1ProfilePhaseError(
                    phase,
                    child_failures=failed,
                )
            postgres_samples.append(await _postgres_container_sample(container_name))
            await _sample_child_processes(
                tuple((role, process) for role, process, _path in children),
                process_peaks,
            )
            await asyncio.sleep(0.1)
        phase = "commit_winner"
        (coordination_root / "release").touch()
        process_outputs = await _finish_children(
            children,
            postgres_samples=postgres_samples,
            container_name=container_name,
            process_peaks=process_peaks,
        )
        phase = "wal_after"
        wal_after = await _postgres_wal_lsn(database_url)
        attempts = [attempt for output in process_outputs for attempt in output["attempts"]]
        winner = next(attempt for attempt in attempts if attempt["outcome"] == "success")
        safe_attempts = [{key: value for key, value in attempt.items() if key != "run_id"} for attempt in attempts]
        safe_process_outputs = [
            {
                **output,
                "attempts": [{key: value for key, value in attempt.items() if key != "run_id"} for attempt in output["attempts"]],
            }
            for output in process_outputs
        ]
        phase = "wal_diff"
        wal_bytes = await _postgres_wal_bytes(
            database_url,
            before=wal_before,
            after=wal_after,
        )
        return {
            "roles": [role for role, _count in _release_role_attempts()],
            "process_count": len(children),
            "distinct_os_processes": distinct_os_processes,
            "attempt_count": len(attempts),
            "wall_seconds": time.perf_counter() - started,
            "process_peak_rss_bytes": process_peaks,
            "processes": safe_process_outputs,
            "attempts": safe_attempts,
            "winner_run_id": winner["run_id"],
            "wal": {"bytes": wal_bytes},
        }
    except BaseException as error:
        await _stop_children(children)
        if isinstance(error, _R1ProfilePhaseError):
            raise
        raise _R1ProfilePhaseError(phase) from error


async def _run_coexistence(
    *,
    script_path: Path,
    database_url: str,
    spec_path: Path,
    existing_v3_run_id: str,
    temporary_root: Path,
    container_name: str,
    postgres_samples: list[dict[str, int]],
) -> dict[str, object]:
    coordination_root = temporary_root / "coexist-coordination"
    coordination_root.mkdir(mode=0o700)
    writer_output = temporary_root / "coexist-writer.json"
    materializer_output = temporary_root / "coexist-materializer.json"
    materialization_root = temporary_root / "coexist-materialized"
    runs_path = temporary_root / "coexist-runs.json"
    _atomic_write_json(
        runs_path,
        {
            "runs": [
                {
                    "run_id": existing_v3_run_id,
                    "snapshot_schema_version": 3,
                }
            ]
        },
    )
    child_env = dict(os.environ)
    child_env[_DATABASE_ENV] = database_url
    writer = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        "--attempt-worker",
        "--spec",
        str(spec_path),
        "--coordination-root",
        str(coordination_root),
        "--worker-output",
        str(writer_output),
        "--role",
        "gateway-coexist",
        "--attempt-count",
        "1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
    )
    materializer = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        "--materializer-worker",
        "--spec",
        str(spec_path),
        "--runs",
        str(runs_path),
        "--worker-output",
        str(materializer_output),
        "--materialization-root",
        str(materialization_root),
        "--writer-mode",
        "legacy_v3",
        "--coordination-root",
        str(coordination_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
    )
    children = [
        ("gateway-coexist", writer, writer_output),
        ("legacy-materializer", materializer, materializer_output),
    ]
    process_peaks: dict[str, int] = {}
    await _wait_for_path(
        coordination_root / "process-ready-gateway-coexist",
        timeout_seconds=60.0,
    )
    await _wait_for_path(
        coordination_root / "process-ready-materializer",
        timeout_seconds=60.0,
    )
    wal_before = await _postgres_wal_lsn(database_url)
    started = time.perf_counter()
    (coordination_root / "start").touch()
    deadline = time.monotonic() + 180.0
    while True:
        winner_ready = any(coordination_root.glob("winner-ready-gateway-coexist-*.json"))
        materializer_done = materializer_output.exists()
        if winner_ready and materializer_done:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("R1 coexistence did not converge")
        if writer.returncode not in {None, 0} or materializer.returncode not in {
            None,
            0,
        }:
            raise RuntimeError("R1 coexistence child failed")
        postgres_samples.append(await _postgres_container_sample(container_name))
        await _sample_child_processes(
            tuple((label, process) for label, process, _path in children),
            process_peaks,
        )
        await asyncio.sleep(0.1)
    (coordination_root / "release").touch()
    outputs = await _finish_children(
        children,
        postgres_samples=postgres_samples,
        container_name=container_name,
        process_peaks=process_peaks,
    )
    wal_after = await _postgres_wal_lsn(database_url)
    safe_writer = {
        **outputs[0],
        "attempts": [{key: value for key, value in attempt.items() if key != "run_id"} for attempt in outputs[0]["attempts"]],
    }
    return {
        "wall_seconds": time.perf_counter() - started,
        "process_peak_rss_bytes": process_peaks,
        "writer": safe_writer,
        "materializer": outputs[1],
        "wal": {
            "bytes": await _postgres_wal_bytes(
                database_url,
                before=wal_before,
                after=wal_after,
            ),
        },
    }


async def _spawn_mode_readback(
    *,
    script_path: Path,
    writer_mode: _WriterMode,
    output_path: Path,
    database_url: str,
) -> dict[str, object]:
    child_env = dict(os.environ)
    child_env[_DATABASE_ENV] = database_url
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        "--mode-readback",
        "--writer-mode",
        writer_mode,
        "--worker-output",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0 or stdout.strip() or stderr.strip():
        raise RuntimeError("R1 mode readback process failed")
    return json.loads(output_path.read_text(encoding="utf-8"))


async def _run_final_reader(
    *,
    script_path: Path,
    database_url: str,
    spec_path: Path,
    v3_run_id: str,
    v4_run_id: str,
    temporary_root: Path,
) -> dict[str, object]:
    output_path = temporary_root / "rollback-v4-reader.json"
    runs_path = temporary_root / "rollback-v4-runs.json"
    materialization_root = temporary_root / "rollback-v4-materialized"
    _atomic_write_json(
        runs_path,
        {
            "runs": [
                {
                    "run_id": v3_run_id,
                    "snapshot_schema_version": 3,
                },
                {
                    "run_id": v4_run_id,
                    "snapshot_schema_version": 4,
                },
            ]
        },
    )
    child_env = dict(os.environ)
    child_env[_DATABASE_ENV] = database_url
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        "--materializer-worker",
        "--spec",
        str(spec_path),
        "--runs",
        str(runs_path),
        "--worker-output",
        str(output_path),
        "--materialization-root",
        str(materialization_root),
        "--writer-mode",
        "v4_reference",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0 or stdout.strip() or stderr.strip():
        raise RuntimeError("R1 rollback v4 reader process failed")
    return json.loads(output_path.read_text(encoding="utf-8"))


async def _orchestrate(args: argparse.Namespace) -> None:
    base_url = os.environ.get("POSTGRES_ADMIN_URL", "").strip() or os.environ.get("DATABASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("DATABASE_URL or POSTGRES_ADMIN_URL is required; values are never logged")
    archive_path = args.archive.resolve()
    evidence_path = args.evidence.resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    prior_failure_evidence: str | None = None
    if evidence_path.exists():
        previous = json.loads(evidence_path.read_text(encoding="utf-8"))
        previous_failure = previous.get("failure")
        if previous.get("passed") is False and isinstance(
            previous_failure,
            Mapping,
        ):
            blocker_code = previous_failure.get("blocker_code")
            archive_name = "pre-policy-v1-oom-report.json" if isinstance(blocker_code, str) and "OOM" in blocker_code else "pre-wal-diff-dbapi-report.json"
            archive_failure_path = evidence_path.with_name(archive_name)
            if not archive_failure_path.exists():
                archive_failure_path.write_text(
                    json.dumps(previous, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            prior_failure_evidence = archive_failure_path.name
    script_path = Path(__file__).resolve()
    stage = "initialization"
    failure: dict[str, object] | None = None
    fixture: dict[str, object] = {}
    full_preflight: dict[str, object] = {}
    topology: dict[str, object] = {}
    gate_release: dict[str, bool] = {}
    coexistence: dict[str, object] = {}
    rollback: dict[str, object] = {}
    postgres: dict[str, object] = {}
    assertions: dict[str, bool] = {}
    postgres_samples: list[dict[str, int]] = []
    oom_before: dict[str, int] = {}
    oom_after: dict[str, int] = {}
    identity_before: dict[str, object] = {}
    identity_after: dict[str, object] = {}
    try:
        async with _temporary_database(base_url) as (
            database_url,
            database_name,
        ):
            stage = "baseline"
            baseline = await _postgres_container_sample(args.postgres_container)
            postgres_samples.append(baseline)
            oom_before = await _postgres_oom_events(args.postgres_container)
            identity_before = await _postgres_identity(database_url)
            with tempfile.TemporaryDirectory(prefix="actweave-r1-resource-profile-") as temporary:
                temporary_root = Path(temporary)
                default_readback = await _spawn_mode_readback(
                    script_path=script_path,
                    writer_mode="v4_reference",
                    output_path=temporary_root / "default-v4-readback.json",
                    database_url=database_url,
                )

                stage = "seed"
                seed_task = asyncio.create_task(
                    _install_and_seed(
                        database_url,
                        database_name,
                        archive_path,
                    )
                )
                while not seed_task.done():
                    postgres_samples.append(await _postgres_container_sample(args.postgres_container))
                    await asyncio.sleep(0.1)
                coordinates, fixture = await seed_task
                oom_after_seed = await _postgres_oom_events(args.postgres_container)
                if not _oom_unchanged(oom_before, oom_after_seed):
                    raise RuntimeError("PostgreSQL OOM during R1 fixture seed")

                spec_path = temporary_root / "coordinates.json"
                _atomic_write_json(spec_path, coordinates.as_json())

                stage = "full_preflight"
                full_preflight = await _full_preflight(
                    database_url,
                    coordinates,
                )
                oom_after_preflight = await _postgres_oom_events(args.postgres_container)
                if not _oom_unchanged(oom_before, oom_after_preflight):
                    raise RuntimeError("PostgreSQL OOM during R1 full preflight")

                stage = "three_process_backpressure"
                topology = await _run_attempt_topology(
                    script_path=script_path,
                    database_url=database_url,
                    spec_path=spec_path,
                    temporary_root=temporary_root,
                    container_name=args.postgres_container,
                    postgres_samples=postgres_samples,
                )
                v3_run_id = str(topology.pop("winner_run_id"))
                oom_after_topology = await _postgres_oom_events(args.postgres_container)
                identity_after_topology = await _postgres_identity(database_url)
                if not _oom_unchanged(oom_before, oom_after_topology):
                    raise RuntimeError("PostgreSQL OOM during R1 three-process backpressure")
                if identity_after_topology["postmaster_started_at"] != identity_before["postmaster_started_at"] or identity_after_topology["in_recovery"]:
                    raise RuntimeError("PostgreSQL restarted during R1 three-process backpressure")

                stage = "permit_release"
                gate_release = await _gate_release_probes(database_url)

                stage = "writer_materializer_coexistence"
                coexist_oom_before = await _postgres_oom_events(args.postgres_container)
                coexist_identity_before = await _postgres_identity(database_url)
                coexistence = await _run_coexistence(
                    script_path=script_path,
                    database_url=database_url,
                    spec_path=spec_path,
                    existing_v3_run_id=v3_run_id,
                    temporary_root=temporary_root,
                    container_name=args.postgres_container,
                    postgres_samples=postgres_samples,
                )
                coexist_oom_after = await _postgres_oom_events(args.postgres_container)
                coexist_identity_after = await _postgres_identity(database_url)
                coexistence_no_oom_increment = _oom_unchanged(
                    coexist_oom_before,
                    coexist_oom_after,
                )
                coexistence["postgres"] = {
                    "oom_events_before": coexist_oom_before,
                    "oom_events_after": coexist_oom_after,
                    "postmaster_unchanged": (coexist_identity_before["postmaster_started_at"] == coexist_identity_after["postmaster_started_at"]),
                    "healthy_after": not coexist_identity_after["in_recovery"],
                }
                if not coexistence_no_oom_increment:
                    raise RuntimeError("PostgreSQL OOM during R1 writer/materializer coexistence")
                if coexist_identity_before["postmaster_started_at"] != coexist_identity_after["postmaster_started_at"] or coexist_identity_after["in_recovery"]:
                    raise RuntimeError("PostgreSQL restarted during R1 coexistence")

                stage = "rollback_to_v4"
                rollback_readback = await _spawn_mode_readback(
                    script_path=script_path,
                    writer_mode="v4_reference",
                    output_path=temporary_root / "rollback-v4-readback.json",
                    database_url=database_url,
                )
                engine = create_async_engine(database_url)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                try:
                    async with factory() as session, session.begin():
                        v4_run_id = await _persist_run(
                            session,
                            coordinates=coordinates,
                            version=coordinates.near,
                            writer_mode="v4_reference",
                            legacy_snapshot=None,
                            run_label="rollback-v4",
                        )
                finally:
                    await engine.dispose()
                rollback_reader = await _run_final_reader(
                    script_path=script_path,
                    database_url=database_url,
                    spec_path=spec_path,
                    v3_run_id=v3_run_id,
                    v4_run_id=v4_run_id,
                    temporary_root=temporary_root,
                )
                validation_engine = create_async_engine(database_url)
                try:
                    async with validation_engine.connect() as connection:
                        source_rows = (
                            await connection.execute(
                                text(
                                    """SELECT a.snapshot_schema_version,
                                              EXISTS (
                                                  SELECT 1
                                                  FROM run_skill_version_refs r
                                                  WHERE r.project_id=a.project_id
                                                    AND r.owner_user_id=a.owner_user_id
                                                    AND r.run_id=a.run_id
                                                    AND r.asset_kind=a.asset_kind
                                                    AND r.dependency_order=a.dependency_order
                                              ) AS has_ref
                                       FROM run_asset_versions a
                                       WHERE a.run_id IN (:v3_run,:v4_run)
                                         AND a.asset_kind='skill'
                                       ORDER BY a.snapshot_schema_version"""
                                ),
                                {"v3_run": v3_run_id, "v4_run": v4_run_id},
                            )
                        ).all()
                finally:
                    await validation_engine.dispose()
                rollback = {
                    "mode_sequence": [
                        default_readback,
                        {
                            "writer_mode": "legacy_v3",
                            "artifact_version": (RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION),
                            "legacy_policy_digest": (LEGACY_ADMISSION_POLICY.canonical_digest()),
                            "ready": True,
                        },
                        rollback_readback,
                    ],
                    "reader": rollback_reader,
                    "persisted_source_shapes": [
                        {
                            "snapshot_schema_version": int(schema),
                            "has_exact_ref": bool(has_ref),
                        }
                        for schema, has_ref in source_rows
                    ],
                }

                stage = "final_readback"
                postgres_samples.append(await _postgres_container_sample(args.postgres_container))
                oom_after = await _postgres_oom_events(args.postgres_container)
                identity_after = await _postgres_identity(database_url)
                final_sample = postgres_samples[-1]
                peak = {key: max(int(sample[key]) for sample in postgres_samples) for key in baseline}
                postgres = {
                    "container_name": args.postgres_container,
                    "baseline": baseline,
                    "peak": peak,
                    "final": final_sample,
                    "sample_count": len(postgres_samples),
                    "oom_events_before": oom_before,
                    "oom_events_after": oom_after,
                    "identity_before": identity_before,
                    "identity_after": identity_after,
                    "cgroup_headroom_at_peak_bytes": (baseline["cgroup_limit_bytes"] - peak["cgroup_memory_bytes"]),
                }

                attempts = topology["attempts"]
                winners = [attempt for attempt in attempts if attempt["outcome"] == "success"]
                busy = [attempt for attempt in attempts if attempt["outcome"] == "retryable_busy"]
                legacy_readbacks = [process["writer_readback"] for process in topology["processes"]]
                cohort_readbacks = [process["cohort_readback"] for process in topology["processes"]]
                rollback_schemas = {int(run["snapshot_schema_version"]) for run in rollback_reader["runs"]}
                rollback_v4_reads_v3_and_v4 = (
                    rollback_reader["writer_readback"]["writer_mode"] == "v4_reference"
                    and rollback_schemas == {3, 4}
                    and all(int(run["tree_file_count"]) == int(run["expected_tree_file_count"]) and bool(run["run_mount_manifest_present"]) for run in rollback_reader["runs"])
                    and not rollback_reader["roots_remaining"]
                )
                assertions = {
                    "full_preflight_is_413": (full_preflight["outcome"] == "too_large" and full_preflight["http_status"] == 413),
                    "full_preflight_permit_attempts_zero": (full_preflight["permit_attempt_count"] == 0),
                    "full_preflight_content_queries_zero": (full_preflight["content_query_count"] == 0),
                    "full_preflight_cohort_ready": (full_preflight["cohort_readback"]["ready"] is True),
                    "accepted_fixture_is_whole_file_subset_not_full": (not fixture["accepted_near_ceiling"]["full_archive"] and fixture["accepted_near_ceiling"]["whole_file_subset"]),
                    "accepted_fixture_is_near_encoded_ceiling": (float(fixture["accepted_near_ceiling"]["encoded_ceiling_utilization"]) >= 0.95),
                    "distinct_os_processes": bool(topology["distinct_os_processes"]),
                    "three_writer_process_roles": (topology["process_count"] == 3 and set(topology["roles"]) == {"gateway-a", "gateway-b", "scheduler"}),
                    "eight_attempts": topology["attempt_count"] == 8,
                    "one_writer_seven_busy": (len(winners) == 1 and len(busy) == 7),
                    "all_attempts_try_one_permit": sum(int(attempt["permit_attempt_count"]) for attempt in attempts) == 8,
                    "at_most_one_content_select": sum(int(attempt["content_query_count"]) for attempt in attempts) == 1,
                    "busy_attempts_never_select_content": all(int(attempt["content_query_count"]) == 0 for attempt in busy),
                    "permit_is_fail_fast": all(float(attempt["gate_query_seconds"]) < 1.0 for attempt in attempts),
                    "homogeneous_legacy_artifact_and_policy": all(
                        readback["writer_mode"] == "legacy_v3"
                        and readback["artifact_version"] == RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION
                        and readback["legacy_policy_digest"] == LEGACY_ADMISSION_POLICY.canonical_digest()
                        and readback["ready"] is True
                        for readback in legacy_readbacks
                    ),
                    "homogeneous_database_cohort_coordinate": (
                        len(cohort_readbacks) == 3
                        and {
                            (
                                readback["writer_mode"],
                                readback["artifact_version"],
                                readback["legacy_policy_digest"],
                            )
                            for readback in cohort_readbacks
                        }
                        == {
                            (
                                "legacy_v3",
                                RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION,
                                LEGACY_ADMISSION_POLICY.canonical_digest(),
                            )
                        }
                        and sorted(readback["process_role"] for readback in cohort_readbacks) == ["gateway", "gateway", "scheduler"]
                        and all(readback["ready"] is True for readback in cohort_readbacks)
                    ),
                    "v4_cutover_modes_use_database_cohort": (
                        default_readback["cohort_readback"]["ready"] is True
                        and default_readback["cohort_readback"]["writer_mode"] == "v4_reference"
                        and rollback_readback["cohort_readback"]["ready"] is True
                        and rollback_readback["cohort_readback"]["writer_mode"] == "v4_reference"
                    ),
                    **gate_release,
                    "topology_wal_recorded": int(topology["wal"]["bytes"]) > 0,
                    "coexistence_no_oom_increment": (coexistence_no_oom_increment),
                    "coexistence_postmaster_unchanged": bool(coexistence["postgres"]["postmaster_unchanged"]),
                    "coexistence_healthy_after": bool(coexistence["postgres"]["healthy_after"]),
                    "coexistence_writer_and_materializer_complete": (
                        coexistence["writer"]["attempts"][0]["outcome"] == "success"
                        and coexistence["materializer"]["runs"][0]["tree_file_count"] == coexistence["materializer"]["runs"][0]["expected_tree_file_count"]
                        and not coexistence["materializer"]["roots_remaining"]
                    ),
                    "coexistence_wal_recorded": (int(coexistence["wal"]["bytes"]) > 0),
                    "rollback_v4_reads_v3_and_v4": (rollback_v4_reads_v3_and_v4),
                    "rollback_source_shapes_preserved": (
                        rollback["persisted_source_shapes"]
                        == [
                            {
                                "snapshot_schema_version": 3,
                                "has_exact_ref": False,
                            },
                            {
                                "snapshot_schema_version": 4,
                                "has_exact_ref": True,
                            },
                        ]
                    ),
                    "postgres_limit_is_one_gib": (baseline["cgroup_limit_bytes"] == 1024 * _MIB),
                    "postgres_no_oom_increment": _oom_unchanged(
                        oom_before,
                        oom_after,
                    ),
                    "postgres_postmaster_unchanged": (identity_before["postmaster_started_at"] == identity_after["postmaster_started_at"]),
                    "postgres_healthy_after": not identity_after["in_recovery"],
                    "postgres_peak_within_cgroup_limit": (peak["cgroup_memory_bytes"] <= baseline["cgroup_limit_bytes"]),
                }
    except BaseException as error:
        failure = {
            "stage": stage,
            "error_type": type(error).__name__,
            "exception": _exception_facts(error),
        }
        try:
            oom_after = await _postgres_oom_events(args.postgres_container)
        except BaseException:
            oom_after = {}
        if oom_before and oom_after and not _oom_unchanged(oom_before, oom_after):
            failure["blocker_code"] = "R1_POSTGRES_OOM"
            failure["release_decision"] = "legacy_writer_not_qualified"
        assertions = {
            "profile_completed": False,
            "postgres_no_oom_increment": (bool(oom_before) and bool(oom_after) and _oom_unchanged(oom_before, oom_after)),
        }
        if postgres_samples:
            baseline = postgres_samples[0]
            postgres = {
                "baseline": baseline,
                "peak": {key: max(int(sample[key]) for sample in postgres_samples) for key in baseline},
                "sample_count": len(postgres_samples),
                "oom_events_before": oom_before,
                "oom_events_after": oom_after,
                "identity_before": identity_before,
            }

    report = {
        "profile": "r1_legacy_admission_rollback_release_topology",
        "generated_at": datetime.now(UTC).isoformat(),
        "command": (f"PYTHONPATH=. uv run python scripts/profile_r1_legacy_admission_resources.py --archive <ppt-master.zip> --evidence <report.json> --postgres-container {args.postgres_container}"),
        "fixture": fixture,
        "full_preflight": full_preflight,
        "writer_topology": topology,
        "permit_release": gate_release,
        "coexistence": coexistence,
        "rollback_rehearsal": rollback,
        "postgres": postgres,
        "assertions": assertions,
        "passed": failure is None and bool(assertions) and all(bool(value) for value in assertions.values()),
        "failure": failure,
        "prior_failure_evidence": prior_failure_evidence,
        "scope_limits": [
            "The profile uses the production LegacyRunSkillSnapshotWriter, database-wide advisory transaction gate, v3 codec, and v2/v3/v4 production materializers against a random isolated Schema V1 database.",
            "Gateway and Scheduler are real independent OS processes with release role labels; the profile does not claim HTTP routing, scheduler polling, model, or Agent Graph execution.",
            "The full supplied ppt-master Version is used only for conservative preflight rejection. The accepted writer fixture is a deterministic whole-file subset from the same archive and is explicitly not reported as full ppt-master.",
            "Existing nonzero PostgreSQL OOM counters are baseline history; release success requires no increment during this profile and an unchanged postmaster outside recovery.",
        ],
    }
    evidence_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "evidence": str(evidence_path),
                "passed": report["passed"],
                "failure_stage": None if failure is None else failure["stage"],
            },
            sort_keys=True,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--postgres-container", default="postgres")
    parser.add_argument("--attempt-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--materializer-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode-readback", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--spec", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--runs", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--coordination-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--materialization-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--role", help=argparse.SUPPRESS)
    parser.add_argument("--attempt-count", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--writer-mode",
        choices=("v4_reference", "legacy_v3"),
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.mode_readback:
        if args.writer_mode is None or args.worker_output is None:
            raise SystemExit("mode readback requires internal coordinates")
        succeeded = asyncio.run(
            _run_internal_child(
                _mode_readback_worker(
                    writer_mode=args.writer_mode,
                    output_path=args.worker_output.resolve(),
                ),
                output_path=args.worker_output.resolve(),
                phase="mode_readback",
            )
        )
        if not succeeded:
            raise SystemExit(1)
        return
    if args.attempt_worker:
        if args.spec is None or args.coordination_root is None or args.worker_output is None or args.role is None or args.attempt_count is None:
            raise SystemExit("attempt worker requires internal coordinates")
        succeeded = asyncio.run(
            _run_internal_child(
                _attempt_worker(
                    spec_path=args.spec.resolve(),
                    coordination_root=args.coordination_root.resolve(),
                    output_path=args.worker_output.resolve(),
                    role=args.role,
                    attempt_count=args.attempt_count,
                ),
                output_path=args.worker_output.resolve(),
                phase="attempt_worker",
            )
        )
        if not succeeded:
            raise SystemExit(1)
        return
    if args.materializer_worker:
        if args.spec is None or args.runs is None or args.worker_output is None or args.materialization_root is None or args.writer_mode is None:
            raise SystemExit("materializer worker requires internal coordinates")
        succeeded = asyncio.run(
            _run_internal_child(
                _materializer_worker(
                    spec_path=args.spec.resolve(),
                    runs_path=args.runs.resolve(),
                    output_path=args.worker_output.resolve(),
                    materialization_root=args.materialization_root.resolve(),
                    writer_mode=args.writer_mode,
                    coordination_root=(None if args.coordination_root is None else args.coordination_root.resolve()),
                ),
                output_path=args.worker_output.resolve(),
                phase="materializer_worker",
            )
        )
        if not succeeded:
            raise SystemExit(1)
        return
    if args.archive is None or args.evidence is None:
        raise SystemExit("--archive and --evidence are required")
    asyncio.run(_orchestrate(args))


if __name__ == "__main__":
    main()
