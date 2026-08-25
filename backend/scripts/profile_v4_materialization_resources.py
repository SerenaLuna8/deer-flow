#!/usr/bin/env python3
"""Profile eight concurrent mixed v2/v3/v4 Skill materializations.

This is an opt-in release-evidence harness, not a normal test.  It creates a
random ``deerflow_test_*`` database, installs the current full Schema V1,
persists the supplied real archive once as an immutable Version, derives full
v2 and v3 compatibility snapshots from those same files, and launches one fresh child
process for eight mixed Run-local trees. The child receives the database URL
only through its environment; reports and command output never include
credentials. The dual-reader total gate is configured independently from the
retained 256 MiB v4 aggregate, and this harness asserts both counters.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextvars
import gc
import hashlib
import json
import os
import re
import resource
import statistics
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

from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.private_work.run_skill_tree_materializer import (
    MAX_LEGACY_V2_RUN_SKILL_SNAPSHOT_JSON_BYTES,
    LegacyInlineRunSkillPlan,
    LegacyInlineRunSkillSourceAdapter,
    MaterializationAttemptIdentity,
    MaterializationAuthorityReadback,
    PinnedSkillVersionPlan,
    PinnedSkillVersionSourceAdapter,
    RunSkillTreeMaterializationPlan,
    RunSkillTreeMaterializer,
    SkillVersionFileMetadata,
)
from app.reliability.workers import WorkerRegistry
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedSkillSnapshot,
    SkillArchiveFile,
    SkillAssetRef,
)
from app.shared_assets.run_snapshot_codec import (
    MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES,
    encode_run_asset_snapshot,
    encoded_run_asset_snapshot_json_size,
)
from app.shared_assets.skill_archive import (
    load_skill_archive_package,
)
from app.shared_assets.skill_service import normalize_skill_files
from app.shared_assets.skill_version_facts import skill_version_archive_facts
from deerflow.config.worker_config import (
    DEFAULT_MATERIALIZATION_MAX_INFLIGHT_BYTES,
    DEFAULT_MATERIALIZATION_V4_MAX_INFLIGHT_BYTES,
    RELEASE_WORKER_MAX_CONCURRENT_JOBS,
    RELEASE_WORKER_PROCESS_COUNT,
    WorkerConfig,
    require_supported_worker_release_topology,
)
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.shared_assets.skill_model import SkillVersionFileRow

_DATABASE_ENV = "ACTWEAVE_MIXED_PROFILE_DATABASE_URL"
_TEST_DATABASE_PATTERN = re.compile(r"deerflow_test_[0-9]+_[0-9a-f]{32}\Z")
_MIB = 1024 * 1024
type _SourceKind = Literal["v2", "v3", "v4"]


@dataclass(frozen=True, slots=True)
class _RunCoordinates:
    run_id: str
    source_kind: _SourceKind
    skill_id: uuid.UUID
    version_id: uuid.UUID
    checksum: str
    file_count: int
    content_size_bytes: int
    snapshot_schema_version: Literal[2, 3, 4]

    def as_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "source_kind": self.source_kind,
            "skill_id": str(self.skill_id),
            "version_id": str(self.version_id),
            "checksum": self.checksum,
            "file_count": self.file_count,
            "content_size_bytes": self.content_size_bytes,
            "snapshot_schema_version": self.snapshot_schema_version,
        }

    @classmethod
    def from_json(cls, value: dict[str, object]) -> _RunCoordinates:
        source_kind = str(value["source_kind"])
        schema_version = value["snapshot_schema_version"]
        if source_kind not in {"v2", "v3", "v4"} or schema_version not in {
            2,
            3,
            4,
        }:
            raise ValueError("invalid mixed materialization Run coordinate")
        return cls(
            run_id=str(value["run_id"]),
            source_kind=source_kind,  # type: ignore[arg-type]
            skill_id=uuid.UUID(str(value["skill_id"])),
            version_id=uuid.UUID(str(value["version_id"])),
            checksum=str(value["checksum"]),
            file_count=int(value["file_count"]),
            content_size_bytes=int(value["content_size_bytes"]),
            snapshot_schema_version=schema_version,  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class _SeedCoordinates:
    database_name: str
    user_id: uuid.UUID
    project_id: uuid.UUID
    membership_id: uuid.UUID
    agent_id: uuid.UUID
    thread_id: str
    runs: tuple[_RunCoordinates, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "database_name": self.database_name,
            "user_id": str(self.user_id),
            "project_id": str(self.project_id),
            "membership_id": str(self.membership_id),
            "agent_id": str(self.agent_id),
            "thread_id": self.thread_id,
            "runs": [value.as_json() for value in self.runs],
        }

    @classmethod
    def from_json(cls, value: dict[str, object]) -> _SeedCoordinates:
        return cls(
            database_name=str(value["database_name"]),
            user_id=uuid.UUID(str(value["user_id"])),
            project_id=uuid.UUID(str(value["project_id"])),
            membership_id=uuid.UUID(str(value["membership_id"])),
            agent_id=uuid.UUID(str(value["agent_id"])),
            thread_id=str(value["thread_id"]),
            runs=tuple(
                _RunCoordinates.from_json(item)
                for item in value["runs"]  # type: ignore[union-attr]
            ),
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


def _release_source_mix(concurrency: int) -> tuple[_SourceKind, ...]:
    if concurrency != RELEASE_WORKER_MAX_CONCURRENT_JOBS:
        raise ValueError("mixed release profile requires concurrency=8")
    return ("v4", "v4", "v4", "v3", "v4", "v2", "v4", "v4")


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


def _legacy_v2_snapshot(
    files: Sequence[SkillArchiveFile],
    *,
    scope: str,
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    catalog_generation: int,
) -> tuple[dict[str, object], ResolvedSkillSnapshot]:
    canonical_files = tuple(files)
    facts = _archive_facts(canonical_files)
    snapshot = ResolvedSkillSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope(scope),
        asset_id=skill_id,
        version_id=version_id,
        checksum=facts.payload_checksum,
        catalog_generation=catalog_generation,
        dependency_version_ids=(),
        files=canonical_files,
        secret_requirements=(),
    )
    encoded: dict[str, object] = {
        "schema_version": 2,
        "kind": AssetKind.SKILL.value,
        "scope": scope,
        "asset_id": str(skill_id),
        "version_id": str(version_id),
        "checksum": facts.payload_checksum,
        "catalog_generation": catalog_generation,
        "dependency_version_ids": [],
        "skill": {
            "files": [
                {
                    "path": item.path,
                    "media_type": item.media_type,
                    "content_base64": base64.b64encode(item.content).decode("ascii"),
                }
                for item in canonical_files
            ],
            "secret_requirements": [],
        },
    }
    if encoded_run_asset_snapshot_json_size(encoded) > MAX_LEGACY_V2_RUN_SKILL_SNAPSHOT_JSON_BYTES:
        raise ValueError("v2 resource fixture exceeds the encoded snapshot limit")
    return encoded, snapshot


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
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            try:
                async with admin_engine.connect() as connection:
                    await connection.execute(
                        text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :database AND pid <> pg_backend_pid()"),
                        {"database": database},
                    )
                    await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
                    remaining = int(
                        await connection.scalar(
                            text("SELECT count(*) FROM pg_database WHERE datname = :database"),
                            {"database": database},
                        )
                    )
                    if remaining != 0:
                        raise RuntimeError("isolated PostgreSQL profile database was not removed")
            except BaseException:
                if body_error is None:
                    raise RuntimeError("unable to remove isolated PostgreSQL profile database") from None
                body_error.add_note("cleanup of isolated PostgreSQL profile database also failed")
    finally:
        await admin_engine.dispose()


async def _seed_scope(session: AsyncSession) -> tuple[uuid.UUID, ...]:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (
                   :user_id, :email, 'v4_resource_admin',
                   'system_admin', now(), false, 1
               )"""
        ),
        {
            "user_id": str(user_id),
            "email": f"{user_id.hex}@example.invalid",
        },
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (
                   :project_id, :slug, 'V4 resource profile', :user_id
               )"""
        ),
        {
            "project_id": project_id,
            "slug": f"v4-resource-{project_id.hex[:12]}",
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text(
            """INSERT INTO project_memberships (
                   id, project_id, user_id, role
               ) VALUES (
                   :membership_id, :project_id, :user_id, 'admin'
               )"""
        ),
        {
            "membership_id": membership_id,
            "project_id": project_id,
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text(
            """INSERT INTO agents (
                   id, scope, project_id, slug, display_name,
                   status, created_by_user_id
               ) VALUES (
                   :agent_id, 'project', :project_id,
                   'v4-resource-agent', 'V4 resource agent',
                   'active', :user_id
               )"""
        ),
        {
            "agent_id": agent_id,
            "project_id": project_id,
            "user_id": str(user_id),
        },
    )
    return user_id, project_id, membership_id, agent_id


async def _seed_run(
    session: AsyncSession,
    coordinates: _SeedCoordinates,
    run: _RunCoordinates,
    legacy_snapshots: Mapping[_SourceKind, Mapping[str, object]],
) -> None:
    run_id = run.run_id
    await session.execute(
        text(
            """INSERT INTO runs (
                   run_id, thread_id, owner_user_id, status,
                   multitask_strategy, metadata_json, kwargs_json,
                   origin_trace_id, message_count, total_input_tokens,
                   total_output_tokens, total_tokens, llm_call_count,
                   lead_agent_tokens, subagent_tokens, middleware_tokens,
                   token_usage_by_model, created_at, updated_at, project_id,
                   finalization_status, asset_closure_sealed
               ) VALUES (
                   :run_id, :thread_id, :owner_user_id, 'pending',
                   'reject', '{}'::json, '{}'::json, :trace_id,
                   0, 0, 0, 0, 0, 0, 0, 0, '{}'::json, now(), now(),
                   :project_id, 'pending', false
               )"""
        ),
        {
            "run_id": run_id,
            "thread_id": coordinates.thread_id,
            "owner_user_id": str(coordinates.user_id),
            "trace_id": f"trace-{run_id}",
            "project_id": coordinates.project_id,
        },
    )
    await session.execute(
        text("SELECT set_config('deerflow.run_asset_closure_assembly', :run_id, true)"),
        {"run_id": run_id},
    )
    agent_version_id = uuid.uuid4()
    payload = AgentPayload(
        description="Mixed materialization resource profile",
        soul="Materialize one exact Skill from its admitted source.",
        model_ref="resource-profile-model",
        tool_groups=(),
        skill_refs=(
            SkillAssetRef(
                scope=AssetScope.PROJECT,
                asset_id=run.skill_id,
            ),
        ),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
    checksum = agent_payload_checksum(payload)
    snapshot = ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=coordinates.agent_id,
        version_id=agent_version_id,
        checksum=checksum,
        catalog_generation=7,
        dependency_version_ids=(run.version_id,),
        payload=payload,
        skill_version_ids=(run.version_id,),
        slug="v4-resource-agent",
    )
    await session.execute(
        text(
            """INSERT INTO run_asset_versions (
                   project_id, owner_user_id, thread_id, run_id,
                   asset_kind, dependency_order, asset_scope, asset_id,
                   version_id, payload_checksum, catalog_generation,
                   snapshot_schema_version, snapshot_json
               ) VALUES (
                   :project_id, :owner_user_id, :thread_id, :run_id,
                   'agent', 0, 'project', :asset_id, :version_id,
                   :checksum, 7, 3, CAST(:snapshot AS jsonb)
               )"""
        ),
        {
            "project_id": coordinates.project_id,
            "owner_user_id": str(coordinates.user_id),
            "thread_id": coordinates.thread_id,
            "run_id": run_id,
            "asset_id": coordinates.agent_id,
            "version_id": agent_version_id,
            "checksum": checksum,
            "snapshot": json.dumps(encode_run_asset_snapshot(snapshot)),
        },
    )
    if run.source_kind == "v4":
        skill_snapshot: Mapping[str, object] = {
            "schema_version": 4,
            "kind": "skill",
            "scope": "project",
            "asset_id": str(run.skill_id),
            "version_id": str(run.version_id),
            "checksum": run.checksum,
            "catalog_generation": 7,
            "dependency_version_ids": [],
            "skill": {
                "source": "skill_version_ref",
                "file_count": run.file_count,
                "content_size_bytes": run.content_size_bytes,
            },
        }
    else:
        skill_snapshot = legacy_snapshots[run.source_kind]
    await session.execute(
        text(
            """INSERT INTO run_asset_versions (
                   project_id, owner_user_id, thread_id, run_id,
                   asset_kind, dependency_order, asset_scope, asset_id,
                   version_id, payload_checksum, catalog_generation,
                   snapshot_schema_version, snapshot_json
               ) VALUES (
                   :project_id, :owner_user_id, :thread_id, :run_id,
                   'skill', 1, 'project', :asset_id, :version_id,
                   :checksum, 7, :schema_version, CAST(:snapshot AS jsonb)
               )"""
        ),
        {
            "project_id": coordinates.project_id,
            "owner_user_id": str(coordinates.user_id),
            "thread_id": coordinates.thread_id,
            "run_id": run_id,
            "asset_id": run.skill_id,
            "version_id": run.version_id,
            "checksum": run.checksum,
            "schema_version": run.snapshot_schema_version,
            "snapshot": json.dumps(skill_snapshot),
        },
    )
    if run.source_kind == "v4":
        await session.execute(
            text(
                """INSERT INTO run_skill_version_refs (
                       project_id, owner_user_id, thread_id, run_id,
                       asset_kind, dependency_order, asset_scope,
                       snapshot_schema_version, skill_project_id, skill_id,
                       skill_version_id, payload_checksum, file_count,
                       content_size_bytes
                   ) VALUES (
                       :project_id, :owner_user_id, :thread_id, :run_id,
                       'skill', 1, 'project', 4, :project_id, :skill_id,
                       :version_id, :checksum, :file_count, :content_size
                   )"""
            ),
            {
                "project_id": coordinates.project_id,
                "owner_user_id": str(coordinates.user_id),
                "thread_id": coordinates.thread_id,
                "run_id": run_id,
                "skill_id": run.skill_id,
                "version_id": run.version_id,
                "checksum": run.checksum,
                "file_count": run.file_count,
                "content_size": run.content_size_bytes,
            },
        )
    await session.execute(
        text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
        {"run_id": run_id},
    )


async def _install_and_seed(
    database_url: str,
    database_name: str,
    archive_path: Path,
    concurrency: int,
) -> tuple[_SeedCoordinates, dict[str, object]]:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        archive_payload = await asyncio.to_thread(archive_path.read_bytes)
        archive_sha256 = hashlib.sha256(archive_payload).hexdigest()
        archive_size = len(archive_payload)
        files = normalize_skill_files(
            load_skill_archive_package(
                archive_payload,
                filename=archive_path.name,
                request_id="v4-resource-profile",
            ),
            request_id="v4-resource-profile",
        )
        del archive_payload
        full_files = tuple(files)
        full_facts = _archive_facts(full_files)
        max_file_bytes = max(len(item.content) for item in full_files)
        source_mix = _release_source_mix(concurrency)

        async with factory() as session, session.begin():
            user_id, project_id, membership_id, agent_id = await _seed_scope(session)
            thread_id = f"v4-resource-{uuid.uuid4().hex}"
            skill_id = uuid.uuid4()
            full_version_id = uuid.uuid4()
            await session.execute(
                text(
                    """INSERT INTO threads_meta (
                           thread_id, owner_user_id, status, metadata_json,
                           created_at, updated_at, project_id, agent_asset_id,
                           agent_scope
                       ) VALUES (
                           :thread_id, :user_id, 'idle', '{}'::json,
                           now(), now(), :project_id, :agent_id, 'project'
                       )"""
                ),
                {
                    "thread_id": thread_id,
                    "user_id": str(user_id),
                    "project_id": project_id,
                    "agent_id": agent_id,
                },
            )
            await session.execute(
                text(
                    """INSERT INTO skills (
                           id, scope, project_id, slug, display_name,
                           status, created_by_user_id
                       ) VALUES (
                           :skill_id, 'project', :project_id, 'ppt-master',
                           'PPT Master resource fixture', 'active', :user_id
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
                           id, skill_id, version_number, secret_requirements,
                           scan_decision, payload_checksum, file_count,
                           content_size_bytes, files_sealed,
                           created_by_user_id
                       ) VALUES (
                           :version_id, :skill_id, :version_number,
                           '[]'::jsonb, 'allow',
                           :checksum, :file_count, :content_size, false,
                           :user_id
                       )"""
                ),
                {
                    "version_id": full_version_id,
                    "skill_id": skill_id,
                    "version_number": 1,
                    "checksum": full_facts.payload_checksum,
                    "file_count": full_facts.file_count,
                    "content_size": full_facts.content_size_bytes,
                    "user_id": str(user_id),
                },
            )
            await session.execute(
                text("SELECT set_config('deerflow.asset_version_assembly', :version_id, true)"),
                {"version_id": str(full_version_id)},
            )
            insert_file = text(
                """INSERT INTO skill_version_files (
                       skill_version_id, path, media_type, size_bytes,
                       sha256, content
                   ) VALUES (
                       :version_id, :path, :media_type, :size_bytes,
                       :sha256, :content
                   )"""
            )
            for offset in range(0, len(full_files), 128):
                batch = full_files[offset : offset + 128]
                await session.execute(
                    insert_file,
                    [
                        {
                            "version_id": full_version_id,
                            "path": item.path,
                            "media_type": item.media_type,
                            "size_bytes": len(item.content),
                            "sha256": hashlib.sha256(item.content).hexdigest(),
                            "content": item.content,
                        }
                        for item in batch
                    ],
                )
            await session.execute(
                text("UPDATE skill_versions SET files_sealed=true WHERE id=:version_id"),
                {"version_id": full_version_id},
            )
            v2_encoded, _v2_snapshot = _legacy_v2_snapshot(
                full_files,
                scope=AssetScope.PROJECT.value,
                skill_id=skill_id,
                version_id=full_version_id,
                catalog_generation=7,
            )
            v3_snapshot = ResolvedSkillSnapshot(
                kind=AssetKind.SKILL,
                scope=AssetScope.PROJECT,
                asset_id=skill_id,
                version_id=full_version_id,
                checksum=full_facts.payload_checksum,
                catalog_generation=7,
                dependency_version_ids=(),
                files=full_files,
                secret_requirements=(),
            )
            v3_encoded = encode_run_asset_snapshot(v3_snapshot)
            runs = tuple(
                _RunCoordinates(
                    run_id=(f"mixed-resource-{source_kind}-{index}-{uuid.uuid4().hex}"),
                    source_kind=source_kind,
                    skill_id=skill_id,
                    version_id=full_version_id,
                    checksum=full_facts.payload_checksum,
                    file_count=full_facts.file_count,
                    content_size_bytes=full_facts.content_size_bytes,
                    snapshot_schema_version={"v2": 2, "v3": 3, "v4": 4}[source_kind],  # type: ignore[arg-type]
                )
                for index, source_kind in enumerate(source_mix)
            )
            coordinates = _SeedCoordinates(
                database_name=database_name,
                user_id=user_id,
                project_id=project_id,
                membership_id=membership_id,
                agent_id=agent_id,
                thread_id=thread_id,
                runs=runs,
            )
            legacy_snapshots = {"v2": v2_encoded, "v3": v3_encoded}
            v2_encoded_bytes = encoded_run_asset_snapshot_json_size(v2_encoded)
            v3_encoded_bytes = encoded_run_asset_snapshot_json_size(v3_encoded)
        for run in runs:
            async with factory() as session, session.begin():
                if run.source_kind in {"v2", "v3"}:
                    await session.execute(text("ALTER TABLE runs DISABLE TRIGGER trg_runs_asset_closure_complete"))
                await _seed_run(
                    session,
                    coordinates,
                    run,
                    legacy_snapshots,
                )
                if run.source_kind in {"v2", "v3"}:
                    await session.execute(text("ALTER TABLE runs ENABLE TRIGGER trg_runs_asset_closure_complete"))
        del (
            files,
            full_files,
            _v2_snapshot,
            v3_snapshot,
            legacy_snapshots,
            v2_encoded,
            v3_encoded,
        )
        gc.collect()
        async with engine.connect() as connection:
            raw_connection = await connection.get_raw_connection()
            await raw_connection.driver_connection.execute("CHECKPOINT")
        async with engine.connect() as connection:
            database_size_bytes = int(await connection.scalar(text("SELECT pg_database_size(current_database())")))
        return coordinates, {
            "archive_path": str(archive_path),
            "archive_size_bytes": archive_size,
            "archive_sha256": archive_sha256,
            "normalized_file_count": full_facts.file_count,
            "logical_content_bytes": full_facts.content_size_bytes,
            "max_file_bytes": max_file_bytes,
            "source_mix": list(source_mix),
            "legacy_fixture_deferred_closure_verifier_bypassed": True,
            "sources": {
                "v2": {
                    "full_archive": True,
                    "file_count": full_facts.file_count,
                    "logical_content_bytes": full_facts.content_size_bytes,
                    "payload_checksum": full_facts.payload_checksum,
                    "encoded_snapshot_bytes": v2_encoded_bytes,
                    "read_compatibility_ceiling_bytes": (MAX_LEGACY_V2_RUN_SKILL_SNAPSHOT_JSON_BYTES),
                },
                "v3": {
                    "full_archive": True,
                    "file_count": full_facts.file_count,
                    "logical_content_bytes": full_facts.content_size_bytes,
                    "payload_checksum": full_facts.payload_checksum,
                    "encoded_snapshot_bytes": v3_encoded_bytes,
                },
                "v4": {
                    "full_archive": True,
                    "file_count": full_facts.file_count,
                    "logical_content_bytes": full_facts.content_size_bytes,
                    "payload_checksum": full_facts.payload_checksum,
                    "encoded_snapshot_bytes": None,
                },
            },
            "database_size_bytes_after_seed": database_size_bytes,
        }
    finally:
        await engine.dispose()


def _current_rss_bytes() -> int:
    output = subprocess.check_output(
        ["ps", "-o", "rss=", "-p", str(os.getpid())],
        text=True,
    )
    return int(output.strip()) * 1024


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile)))
    return ordered[index]


async def _worker_profile(
    spec_path: Path,
    output_path: Path,
    materialization_root: Path,
    *,
    budget_bytes: int,
    v4_budget_bytes: int,
    batch_bytes: int,
    batch_files: int,
) -> None:
    database_url = os.environ.get(_DATABASE_ENV, "").strip()
    if not database_url:
        raise RuntimeError("profile child database URL is unavailable")
    coordinates = _SeedCoordinates.from_json(json.loads(spec_path.read_text(encoding="utf-8")))
    config = WorkerConfig(
        max_concurrent_jobs=RELEASE_WORKER_MAX_CONCURRENT_JOBS,
        materialization_max_inflight_bytes=budget_bytes,
        materialization_v4_max_inflight_bytes=v4_budget_bytes,
        materialization_batch_max_bytes=batch_bytes,
        materialization_batch_max_files=batch_files,
    )
    require_supported_worker_release_topology(config)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_id = uuid.uuid4()
    materializer = RunSkillTreeMaterializer(
        materialization_root=materialization_root,
        worker_config=config,
        legacy_source_adapter=LegacyInlineRunSkillSourceAdapter(factory),
        pinned_source_adapter=PinnedSkillVersionSourceAdapter(factory),
    )
    budget = materializer._memory_budget
    active_source_kind: contextvars.ContextVar[_SourceKind | None] = contextvars.ContextVar("active_mixed_source_kind", default=None)
    content_query_budget_values: list[int] = []
    content_query_v4_budget_values: list[int] = []
    legacy_query_reservations: list[dict[str, object]] = []
    v4_source_query_reservations: list[dict[str, object]] = []
    content_query_count = 0
    source_query_count = 0

    def capture_content_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal content_query_count, source_query_count
        source_kind = active_source_kind.get()
        source_query = source_kind is not None and ("run_asset_versions" in statement or "skill_version_files" in statement)
        if source_query:
            source_query_count += 1
        if source_kind == "v4" and source_query:
            v4_source_query_reservations.append(
                {
                    "query_kind": ("content" if "skill_version_files.content" in statement else ("file_metadata" if "skill_version_files.media_type" in statement else "exact_version_metadata")),
                    "total_in_use_bytes": budget.in_use_bytes,
                    "v4_in_use_bytes": budget.v4_in_use_bytes,
                }
            )
        if "skill_version_files.content" in statement:
            content_query_count += 1
            content_query_budget_values.append(budget.in_use_bytes)
            content_query_v4_budget_values.append(budget.v4_in_use_bytes)
        if "run_asset_versions.snapshot_json" in statement and "NOT (EXISTS" in statement and "skill_versions" not in statement:
            legacy_query_reservations.append(
                {
                    "source_kind": source_kind,
                    "total_in_use_bytes": budget.in_use_bytes,
                    "v4_in_use_bytes": budget.v4_in_use_bytes,
                    "total_capacity_bytes": budget.capacity_bytes,
                }
            )

    event.listen(engine.sync_engine, "before_cursor_execute", capture_content_query)
    try:
        registry = WorkerRegistry(factory, version="mixed-resource-profile")
        await registry.register(
            worker_id,
            frozenset({"private_run"}),
            RELEASE_WORKER_MAX_CONCURRENT_JOBS,
            execution_domain_affinity=None,
            now=datetime.now(UTC),
        )
        async with factory() as session:
            registration = (await session.execute(text("SELECT count(*), min(max_concurrent_jobs), max(max_concurrent_jobs) FROM worker_nodes"))).one()
            v4_run = next(value for value in coordinates.runs if value.source_kind == "v4")
            metadata_rows = (
                await session.execute(
                    select(
                        SkillVersionFileRow.path,
                        SkillVersionFileRow.media_type,
                        SkillVersionFileRow.size_bytes,
                        SkillVersionFileRow.sha256,
                    )
                    .where(SkillVersionFileRow.skill_version_id == v4_run.version_id)
                    .order_by(SkillVersionFileRow.path.collate("C"))
                )
            ).all()
        metadata = tuple(
            SkillVersionFileMetadata(
                path=path,
                media_type=media_type,
                size_bytes=size_bytes,
                sha256=sha256,
            )
            for path, media_type, size_bytes, sha256 in metadata_rows
        )
        batches = materializer.plan_v4_content_batches(metadata)
        del metadata_rows, metadata
        gc.collect()
        baseline_rss = _current_rss_bytes()
        baseline_peak_rss = _peak_rss_bytes()
        start = asyncio.Event()
        sampling = True
        peak_sampled_weight = 0
        peak_sampled_v4_weight = 0
        peak_waiters = 0
        budget_samples = 0
        event_loop_lag_seconds = 0.0

        async def sample_budget() -> None:
            nonlocal budget_samples, event_loop_lag_seconds
            nonlocal peak_sampled_weight, peak_sampled_v4_weight, peak_waiters
            expected = time.perf_counter()
            while sampling:
                now = time.perf_counter()
                event_loop_lag_seconds = max(
                    event_loop_lag_seconds,
                    max(0.0, now - expected),
                )
                with budget._lock:
                    peak_sampled_weight = max(
                        peak_sampled_weight,
                        budget._in_use_bytes,
                    )
                    peak_sampled_v4_weight = max(
                        peak_sampled_v4_weight,
                        budget._v4_in_use_bytes,
                    )
                    peak_waiters = max(peak_waiters, len(budget._waiters))
                budget_samples += 1
                expected = now + 0.01
                await asyncio.sleep(0.01)

        def version_plan(
            run: _RunCoordinates,
        ) -> PinnedSkillVersionPlan | LegacyInlineRunSkillPlan:
            if run.source_kind == "v4":
                return PinnedSkillVersionPlan(
                    dependency_order=1,
                    scope=AssetScope.PROJECT,
                    asset_id=run.skill_id,
                    version_id=run.version_id,
                    payload_checksum=run.checksum,
                    catalog_generation=7,
                    dependency_version_ids=(),
                    file_count=run.file_count,
                    content_size_bytes=run.content_size_bytes,
                    secret_requirements=(),
                )
            return LegacyInlineRunSkillPlan(
                dependency_order=1,
                scope=AssetScope.PROJECT,
                asset_id=run.skill_id,
                version_id=run.version_id,
                payload_checksum=run.checksum,
                catalog_generation=7,
                snapshot_schema_version=run.snapshot_schema_version,
                file_count=run.file_count,
                content_size_bytes=run.content_size_bytes,
                secret_requirements=(),
            )

        async def materialize_one(
            index: int,
            run: _RunCoordinates,
            *,
            wait_for_release_start: bool = True,
        ) -> dict[str, object]:
            identity = MaterializationAttemptIdentity(
                job_id=uuid.uuid4(),
                attempt_id=uuid.uuid4(),
                worker_id=worker_id,
            )
            fingerprint = hashlib.sha256(run.run_id.encode()).hexdigest()
            plan = RunSkillTreeMaterializationPlan(
                project_id=coordinates.project_id,
                owner_user_id=str(coordinates.user_id),
                thread_id=coordinates.thread_id,
                run_id=run.run_id,
                runtime_kind="chat",
                attempt_identity=identity,
                plan_fingerprint=fingerprint,
                skill_versions=(version_plan(run),),
            )
            authority = _Authority(
                MaterializationAuthorityReadback(
                    attempt_identity=identity,
                    plan_fingerprint=fingerprint,
                )
            )
            if wait_for_release_start:
                await start.wait()
            started = time.perf_counter()
            token = active_source_kind.set(run.source_kind)
            pending = None
            try:
                pending = await materializer.materialize(
                    plan=plan,
                    authority=authority,  # type: ignore[arg-type]
                )
                materialized = time.perf_counter()
                tree_file_count = sum(1 for value in pending.source.worker_root.rglob("*") if value.is_file())
                run_mount_manifest_present = (pending.source.worker_root / ".actweave-run-mount.json").is_file()
                await pending.aclose()
                pending = None
                finished = time.perf_counter()
                return {
                    "index": index,
                    "run_id": run.run_id,
                    "source_kind": run.source_kind,
                    "snapshot_schema_version": run.snapshot_schema_version,
                    "source_weight_bytes": (run.content_size_bytes if run.source_kind == "v4" else budget.capacity_bytes),
                    "full_archive": True,
                    "expected_file_count": run.file_count,
                    "logical_content_bytes": run.content_size_bytes,
                    "materialization_seconds": materialized - started,
                    "cleanup_seconds": finished - materialized,
                    "total_seconds": finished - started,
                    "tree_regular_file_count": tree_file_count,
                    "run_mount_manifest_present": run_mount_manifest_present,
                }
            finally:
                if pending is not None:
                    await pending.aclose()
                active_source_kind.reset(token)

        async def wait_for_blocked_probe() -> tuple[int, int]:
            while True:
                with budget._lock:
                    waiter_count = len(budget._waiters)
                owner_count = len(tuple(materialization_root.iterdir())) if materialization_root.exists() else 0
                if waiter_count > 0 and owner_count > 0:
                    return waiter_count, owner_count
                await asyncio.sleep(0.01)

        sampler = asyncio.create_task(sample_budget())
        probe_content_query_count = content_query_count
        probe_source_query_count = source_query_count
        probe_task: asyncio.Task[dict[str, object]] | None = None
        probe_waiter_count = 0
        probe_owner_count = 0
        probe_content_query_count_after = 0
        probe_source_query_count_after = 0
        probe_cancelled = False
        cancellation_waiter_removed = False
        cancellation_owner_cleaned = False
        cancellation_finally_released = False
        cancellation_held_exclusive_capacity = False
        try:
            async with budget.reserve_legacy(
                envelope_bytes=budget.capacity_bytes,
            ):
                cancellation_held_exclusive_capacity = budget.in_use_bytes == budget.capacity_bytes and budget.v4_in_use_bytes == 0
                probe_task = asyncio.create_task(
                    materialize_one(
                        -1,
                        v4_run,
                        wait_for_release_start=False,
                    )
                )
                probe_waiter_count, probe_owner_count = await asyncio.wait_for(
                    wait_for_blocked_probe(),
                    timeout=30.0,
                )
                probe_task.cancel()
                try:
                    await probe_task
                except asyncio.CancelledError:
                    probe_cancelled = True
                with budget._lock:
                    cancellation_waiter_removed = not budget._waiters
                cancellation_owner_cleaned = not materialization_root.exists() or not any(materialization_root.iterdir())
                probe_content_query_count_after = content_query_count
                probe_source_query_count_after = source_query_count
            cancellation_finally_released = budget.in_use_bytes == 0 and budget.v4_in_use_bytes == 0

            tasks = [asyncio.create_task(materialize_one(index, run)) for index, run in enumerate(coordinates.runs)]
            wall_started = time.perf_counter()
            start.set()
            results = await asyncio.gather(*tasks)
            wall_seconds = time.perf_counter() - wall_started
        finally:
            if probe_task is not None and not probe_task.done():
                probe_task.cancel()
                try:
                    await probe_task
                except asyncio.CancelledError:
                    pass
            sampling = False
            await sampler
        gc.collect()
        final_rss = _current_rss_bytes()
        peak_rss = _peak_rss_bytes()
        worker_peak_delta_from_baseline_bytes = max(0, peak_rss - baseline_rss)
        roots_remaining = sorted(value.name for value in materialization_root.iterdir()) if materialization_root.exists() else []
        latencies = [float(item["materialization_seconds"]) for item in results]
        source_counts = {source_kind: sum(1 for item in results if item["source_kind"] == source_kind) for source_kind in ("v2", "v3", "v4")}
        legacy_pre_detoast_exclusive = (
            len(legacy_query_reservations) == 2
            and {item["source_kind"] for item in legacy_query_reservations} == {"v2", "v3"}
            and all(item["total_in_use_bytes"] == budget.capacity_bytes and item["v4_in_use_bytes"] == 0 for item in legacy_query_reservations)
        )
        source_kind_coverage = source_counts == {"v2": 1, "v3": 1, "v4": 6}
        cancellation_probe = {
            "blocked_source_kind": "v4",
            "exclusive_holder_weight_bytes": budget.capacity_bytes,
            "waiter_count_observed": probe_waiter_count,
            "owner_count_observed_while_waiting": probe_owner_count,
            "task_cancelled": probe_cancelled,
            "content_query_count_before": probe_content_query_count,
            "content_query_count_after": probe_content_query_count_after,
            "source_query_count_before": probe_source_query_count,
            "source_query_count_after": probe_source_query_count_after,
            "held_exclusive_capacity": cancellation_held_exclusive_capacity,
            "waiter_removed": cancellation_waiter_removed,
            "owner_cleaned": cancellation_owner_cleaned,
            "finally_released": cancellation_finally_released,
        }
        assertions = {
            "all_attempts_completed": len(results) == len(coordinates.runs),
            "single_worker_registered": int(registration[0]) == 1,
            "capacity_readback_is_eight": registration[1:]
            == (
                RELEASE_WORKER_MAX_CONCURRENT_JOBS,
                RELEASE_WORKER_MAX_CONCURRENT_JOBS,
            ),
            "source_kind_coverage": source_kind_coverage,
            "weighted_budget_bounded": (budget.peak_in_use_bytes <= budget.capacity_bytes),
            "v4_weighted_budget_bounded": (budget.peak_v4_in_use_bytes <= budget.v4_capacity_bytes),
            "worker_rss_delta_within_total_envelope": (worker_peak_delta_from_baseline_bytes <= budget.capacity_bytes),
            "legacy_reached_exclusive_capacity": (budget.peak_in_use_bytes == budget.capacity_bytes),
            "weighted_budget_released": budget.in_use_bytes == 0,
            "v4_weighted_budget_released": budget.v4_in_use_bytes == 0,
            "legacy_pre_detoast_exclusive": legacy_pre_detoast_exclusive,
            "v4_source_queries_under_reservation": (bool(v4_source_query_reservations) and all(int(item["total_in_use_bytes"]) > 0 and int(item["v4_in_use_bytes"]) > 0 for item in v4_source_query_reservations)),
            "content_queries_under_reservation": (bool(content_query_v4_budget_values) and min(content_query_v4_budget_values) > 0),
            "content_query_count_matches_batch_plan": (content_query_count == len(batches) * source_counts["v4"]),
            "source_query_count_matches_plan": (source_query_count == source_counts["v4"] * (len(batches) + 2) + 2),
            "batch_rows_bounded": (max(batch.file_count for batch in batches) <= batch_files),
            "batch_bytes_bounded": all(batch.content_size_bytes <= batch_bytes or batch.oversized_singleton for batch in batches),
            "all_materialized_trees_complete": all(int(item["tree_regular_file_count"]) == int(item["expected_file_count"]) + 1 and bool(item["run_mount_manifest_present"]) for item in results),
            "owner_roots_cleaned": not roots_remaining,
            "cancellation_waiter_observed": probe_waiter_count > 0,
            "cancellation_waiter_removed": cancellation_waiter_removed,
            "cancellation_owner_cleaned": cancellation_owner_cleaned,
            "cancellation_finally_released": cancellation_finally_released,
            "cancellation_prevented_content_query": (probe_content_query_count == probe_content_query_count_after),
            "cancellation_prevented_source_query": (probe_source_query_count == probe_source_query_count_after),
        }
        report = {
            "worker_process_count": RELEASE_WORKER_PROCESS_COUNT,
            "configured_capacity": config.max_concurrent_jobs,
            "registered_worker_count": int(registration[0]),
            "registered_capacity_min": int(registration[1]),
            "registered_capacity_max": int(registration[2]),
            "concurrent_attempts": len(coordinates.runs),
            "source_kind": "mixed_v2_v3_v4",
            "source_mix": [run.source_kind for run in coordinates.runs],
            "source_kind_counts": source_counts,
            "source_weights_bytes": {
                "v2": budget.capacity_bytes,
                "v3": budget.capacity_bytes,
                "v4": v4_run.content_size_bytes,
            },
            "budget_capacity_bytes": budget.capacity_bytes,
            "v4_budget_capacity_bytes": budget.v4_capacity_bytes,
            "budget_peak_in_use_bytes": budget.peak_in_use_bytes,
            "v4_budget_peak_in_use_bytes": budget.peak_v4_in_use_bytes,
            "budget_peak_sampled_bytes": peak_sampled_weight,
            "v4_budget_peak_sampled_bytes": peak_sampled_v4_weight,
            "budget_peak_waiters": max(peak_waiters, probe_waiter_count),
            "budget_samples": budget_samples,
            "budget_released_bytes_at_end": budget.in_use_bytes,
            "v4_budget_released_bytes_at_end": budget.v4_in_use_bytes,
            "planned_batch_count_per_attempt": len(batches),
            "planned_max_batch_rows": max(batch.file_count for batch in batches),
            "planned_max_batch_bytes": max(batch.content_size_bytes for batch in batches),
            "planned_oversized_singleton_batches": sum(1 for batch in batches if batch.oversized_singleton),
            "content_query_count": content_query_count,
            "content_query_min_active_weight_bytes": min(
                content_query_budget_values,
                default=0,
            ),
            "content_query_max_active_weight_bytes": max(
                content_query_budget_values,
                default=0,
            ),
            "content_query_min_active_v4_weight_bytes": min(
                content_query_v4_budget_values,
                default=0,
            ),
            "content_query_max_active_v4_weight_bytes": max(
                content_query_v4_budget_values,
                default=0,
            ),
            "legacy_query_reservations": legacy_query_reservations,
            "v4_source_query_reservations": v4_source_query_reservations,
            "cancellation_probe": cancellation_probe,
            "worker_baseline_rss_bytes": baseline_rss,
            "worker_baseline_peak_rss_bytes": baseline_peak_rss,
            "worker_peak_rss_bytes": peak_rss,
            "worker_peak_delta_from_baseline_bytes": (worker_peak_delta_from_baseline_bytes),
            "worker_final_rss_bytes": final_rss,
            "wall_seconds": wall_seconds,
            "latency_seconds": {
                "min": min(latencies),
                "median": statistics.median(latencies),
                "p95": _percentile(latencies, 0.95),
                "max": max(latencies),
            },
            "max_event_loop_lag_seconds": event_loop_lag_seconds,
            "attempts": results,
            "roots_remaining": roots_remaining,
            "assertions": assertions,
        }
        output_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_content_query)
        await engine.dispose()


async def _run_command(*arguments: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
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
        raise RuntimeError("unable to read PostgreSQL cgroup OOM counters")
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


async def _orchestrate(args: argparse.Namespace) -> None:
    base_url = os.environ.get("POSTGRES_ADMIN_URL", "").strip() or os.environ.get("DATABASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("DATABASE_URL or POSTGRES_ADMIN_URL is required; values are never logged")
    archive_path = args.archive.resolve()
    evidence_path = args.evidence.resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    command_summary = (
        "PYTHONPATH=. uv run python scripts/profile_v4_materialization_resources.py "
        f"--archive {archive_path} --evidence {evidence_path} "
        f"--postgres-container {args.postgres_container} "
        f"--concurrency {args.concurrency} --budget-mib {args.budget_mib} "
        f"--v4-budget-mib {args.v4_budget_mib} "
        f"--batch-mib {args.batch_mib} --batch-files {args.batch_files}"
    )
    async with _temporary_database(base_url) as (database_url, database_name):
        before_identity = await _postgres_identity(database_url)
        oom_before = await _postgres_oom_events(args.postgres_container)
        baseline = await _postgres_container_sample(args.postgres_container)
        samples = [baseline]
        seed_task = asyncio.create_task(
            _install_and_seed(
                database_url,
                database_name,
                archive_path,
                args.concurrency,
            )
        )
        while not seed_task.done():
            try:
                samples.append(await _postgres_container_sample(args.postgres_container))
            except RuntimeError:
                pass
            await asyncio.sleep(0.2)
        coordinates, fixture = await seed_task
        samples.append(await _postgres_container_sample(args.postgres_container))
        seed_sample_count = len(samples)
        oom_after_seed = await _postgres_oom_events(args.postgres_container)
        identity_after_seed = await _postgres_identity(database_url)
        oom_keys = ("oom", "oom_kill", "oom_group_kill")
        if any(oom_after_seed.get(key, 0) != oom_before.get(key, 0) for key in oom_keys) or identity_after_seed["postmaster_started_at"] != before_identity["postmaster_started_at"] or identity_after_seed["in_recovery"]:
            raise RuntimeError("PostgreSQL OOM, restart, or recovery occurred during mixed fixture seed")
        with tempfile.TemporaryDirectory(prefix="actweave-mixed-resource-profile-") as temporary:
            temporary_root = Path(temporary)
            spec_path = temporary_root / "spec.json"
            child_output_path = temporary_root / "worker.json"
            materialization_root = temporary_root / "materialized"
            spec_path.write_text(
                json.dumps(coordinates.as_json(), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            child_env = dict(os.environ)
            child_env[_DATABASE_ENV] = database_url
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--spec",
                str(spec_path),
                "--worker-output",
                str(child_output_path),
                "--materialization-root",
                str(materialization_root),
                "--budget-mib",
                str(args.budget_mib),
                "--v4-budget-mib",
                str(args.v4_budget_mib),
                "--batch-mib",
                str(args.batch_mib),
                "--batch-files",
                str(args.batch_files),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
            while child.returncode is None:
                try:
                    samples.append(await _postgres_container_sample(args.postgres_container))
                except RuntimeError:
                    pass
                try:
                    await asyncio.wait_for(child.wait(), timeout=0.2)
                except TimeoutError:
                    continue
            stdout, stderr = await child.communicate()
            if child.returncode != 0:
                raise RuntimeError("mixed resource profile child failed: " + stderr.decode("utf-8", errors="replace")[-4000:])
            if stdout.strip():
                raise RuntimeError("profile child emitted unexpected stdout")
            worker = json.loads(child_output_path.read_text(encoding="utf-8"))
        final_sample = await _postgres_container_sample(args.postgres_container)
        samples.append(final_sample)
        oom_after = await _postgres_oom_events(args.postgres_container)
        after_identity = await _postgres_identity(database_url)
        postgres = {
            "container_name": args.postgres_container,
            "baseline": baseline,
            "peak": {key: max(int(sample[key]) for sample in samples) for key in baseline},
            "final": final_sample,
            "sample_count": len(samples),
            "oom_events_before": oom_before,
            "oom_events_after": oom_after,
            "identity_before": before_identity,
            "identity_after_seed": identity_after_seed,
            "identity_after": after_identity,
            "seed_phase": {
                "sample_count": seed_sample_count,
                "peak": {key: max(int(sample[key]) for sample in samples[:seed_sample_count]) for key in baseline},
                "oom_events_before": oom_before,
                "oom_events_after": oom_after_seed,
            },
            "cgroup_headroom_at_peak_bytes": baseline["cgroup_limit_bytes"] - max(int(sample["cgroup_memory_bytes"]) for sample in samples),
        }
        sources = fixture["sources"]
        source_values = tuple(sources[source_kind] for source_kind in ("v2", "v3", "v4"))
        assertions = {
            **worker["assertions"],
            "all_sources_are_full_archive": all(
                bool(source["full_archive"])
                and int(source["file_count"]) == fixture["normalized_file_count"]
                and int(source["logical_content_bytes"]) == fixture["logical_content_bytes"]
                and source["payload_checksum"] == source_values[0]["payload_checksum"]
                for source in source_values
            ),
            "fixture_is_real_ppt_master_scale": (archive_path.name == "ppt-master.zip" and int(fixture["normalized_file_count"]) >= 12_922 and int(fixture["logical_content_bytes"]) >= 79_000_000),
            "v2_snapshot_exercises_read_compatibility_ceiling": (
                int(sources["v2"]["encoded_snapshot_bytes"]) > MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES and int(sources["v2"]["encoded_snapshot_bytes"]) <= MAX_LEGACY_V2_RUN_SKILL_SNAPSHOT_JSON_BYTES
            ),
            "v3_snapshot_within_codec_limit": (int(sources["v3"]["encoded_snapshot_bytes"]) <= MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES),
            "postgres_limit_is_one_gib": (baseline["cgroup_limit_bytes"] == 1024 * _MIB),
            "postgres_no_oom_increment": all(oom_after.get(key, 0) == oom_before.get(key, 0) for key in oom_keys),
            "postgres_postmaster_unchanged": before_identity["postmaster_started_at"] == after_identity["postmaster_started_at"],
            "postgres_healthy_after": not after_identity["in_recovery"],
            "postgres_peak_within_cgroup_limit": postgres["peak"]["cgroup_memory_bytes"] <= baseline["cgroup_limit_bytes"],
        }
        report = {
            "profile": "mixed_v2_v3_v4_materialization_release_topology",
            "generated_at": datetime.now(UTC).isoformat(),
            "command": command_summary,
            "fixture": fixture,
            "topology": {
                "worker_processes": RELEASE_WORKER_PROCESS_COUNT,
                "worker_capacity": RELEASE_WORKER_MAX_CONCURRENT_JOBS,
                "concurrent_attempts": args.concurrency,
                "source_mix": fixture["source_mix"],
                "source_kind_counts": worker["source_kind_counts"],
                "materialization_budget_bytes": args.budget_mib * _MIB,
                "materialization_v4_budget_bytes": (args.v4_budget_mib * _MIB),
                "batch_max_bytes": args.batch_mib * _MIB,
                "batch_max_files": args.batch_files,
                "postgres_memory_limit_bytes": baseline["cgroup_limit_bytes"],
            },
            "worker": worker,
            "postgres": postgres,
            "assertions": assertions,
            "passed": all(bool(value) for value in assertions.values()),
            "scope_limits": [
                "The child profiles the production mixed Run Skill materializer plus its v2/v3 legacy and v4 PostgreSQL source adapters, not a full claimed Job/Agent Graph execution.",
                "Every v2, v3, and v4 attempt uses the same full supplied archive and exact immutable Skill Version facts; no reduced subset is used.",
                (
                    "Because current admission writes only v4, the isolated historical "
                    "v2/v3 fixture temporarily disables only the deferred Run closure "
                    "completeness verifier while inserting each legacy Run; foreign keys, "
                    "child-mutation/seal gates, codec checks, and every production reader "
                    "check remain active."
                ),
                (
                    "Historical v2 uses a reader-only 128 MiB encoded compatibility "
                    "ceiling; the v3/current writer ceiling remains 80 MiB, and both "
                    "legacy codecs reserve the full 1.5 GiB release-calibrated process "
                    "envelope before PostgreSQL detoast."
                ),
                "PostgreSQL cgroup memory is authoritative for the 1 GiB container; aggregate process RSS double-counts shared pages and is retained only as a diagnostic sample.",
                "The Worker process has no configured cgroup memory ceiling in this local topology, so its measured RSS delta is reported without inventing a formal headroom limit.",
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
                    "fixture_files": fixture["normalized_file_count"],
                    "fixture_bytes": fixture["logical_content_bytes"],
                    "worker_peak_rss_bytes": worker["worker_peak_rss_bytes"],
                    "postgres_peak_cgroup_bytes": postgres["peak"]["cgroup_memory_bytes"],
                },
                sort_keys=True,
            )
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--postgres-container", default="postgres")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--budget-mib",
        type=int,
        default=DEFAULT_MATERIALIZATION_MAX_INFLIGHT_BYTES // _MIB,
    )
    parser.add_argument(
        "--v4-budget-mib",
        type=int,
        default=DEFAULT_MATERIALIZATION_V4_MAX_INFLIGHT_BYTES // _MIB,
    )
    parser.add_argument("--batch-mib", type=int, default=8)
    parser.add_argument("--batch-files", type=int, default=50)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--spec", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--materialization-root", type=Path, help=argparse.SUPPRESS)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.worker:
        if not args.spec or not args.worker_output or not args.materialization_root:
            raise SystemExit("worker mode requires internal profile coordinates")
        asyncio.run(
            _worker_profile(
                args.spec.resolve(),
                args.worker_output.resolve(),
                args.materialization_root.resolve(),
                budget_bytes=args.budget_mib * _MIB,
                v4_budget_bytes=args.v4_budget_mib * _MIB,
                batch_bytes=args.batch_mib * _MIB,
                batch_files=args.batch_files,
            )
        )
        return
    if not args.archive or not args.evidence:
        raise SystemExit("--archive and --evidence are required")
    if (
        args.concurrency != RELEASE_WORKER_MAX_CONCURRENT_JOBS
        or args.budget_mib != DEFAULT_MATERIALIZATION_MAX_INFLIGHT_BYTES // _MIB
        or args.v4_budget_mib != DEFAULT_MATERIALIZATION_V4_MAX_INFLIGHT_BYTES // _MIB
        or args.batch_mib <= 0
        or args.batch_files <= 0
    ):
        raise SystemExit("release profile requires concurrency=8, budget-mib=1536, v4-budget-mib=256, and positive batch bounds")
    asyncio.run(_orchestrate(args))


if __name__ == "__main__":
    main()
