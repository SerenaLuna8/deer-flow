from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from support.run_skill_writer_cohort import (
    active_test_run_skill_writer_cohort,
    start_test_run_skill_writer_cohort,
)

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkUnavailable
from app.private_work.legacy_run_skill_snapshot_writer import (
    LEGACY_ADMISSION_POLICY,
    RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION,
    freeze_run_skill_snapshot_writer,
    reset_run_skill_snapshot_writer_for_testing,
)
from app.private_work.retention_authority import RetentionPurgeAuthority
from app.private_work.retention_purge import (
    RetentionCandidate,
    RetentionPurgeRepository,
)
from app.private_work.run_repository import (
    PrivateRunConflict,
    PrivateRunCreate,
    PrivateRunRepository,
)
from app.private_work.snapshot_repository import (
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.jobs import (
    JobIdempotencyConflict,
    JobScope,
    PrivateRunJobRepository,
)
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.agent_service import AgentService
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedRunAssetClosure,
    ResolvedSkillVersionSnapshot,
    SkillAssetRef,
)
from app.shared_assets.skill_deletion import SkillDeletionCoordinator
from app.shared_assets.skill_version_facts import skill_version_archive_facts
from deerflow.config.run_skill_snapshot_config import RunSkillSnapshotConfig
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.run.sql import RunRepository as HarnessRunRepository
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentSkillRefRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.user.private_lifecycle import AccountPrivateGeneration

pytestmark = pytest.mark.run_skill_writer_cohort_control


@dataclass(frozen=True)
class _Scope:
    user_id: uuid.UUID
    project_id: uuid.UUID
    membership_id: uuid.UUID
    agent_id: uuid.UUID
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    skill_checksum: str
    skill_file_count: int
    skill_content_size: int
    thread_id: str = "run-closure-thread"


def _project_context(scope: _Scope, request_id: str) -> ProjectContext:
    return ProjectContext(
        user_id=scope.user_id,
        project_id=scope.project_id,
        membership_id=scope.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id=request_id,
    )


async def _wait_for_backend_lock(
    factory: async_sessionmaker[AsyncSession],
    backend_pid: int,
) -> None:
    async with asyncio.timeout(5), factory() as observer:
        while True:
            waiting = await observer.scalar(
                text(
                    """SELECT wait_event_type='Lock'
                       FROM pg_stat_activity WHERE pid=:pid"""
                ),
                {"pid": backend_pid},
            )
            if waiting is True:
                return
            await asyncio.sleep(0)


async def _seed_scope(session: AsyncSession) -> _Scope:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    content = b"---\nname: pinned-skill\ndescription: Pinned Skill.\n---\n"
    file_sha = hashlib.sha256(content).hexdigest()
    facts = skill_version_archive_facts((("SKILL.md", file_sha, len(content)),))

    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (
                   :id, :email, :username, 'system_admin', now(), false, 1
               )"""
        ),
        {
            "id": str(user_id),
            "email": "run-closure@example.invalid",
            "username": "run_closure_admin",
        },
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (
                   :id, 'run-closure', 'Run closure', :user_id
               )"""
        ),
        {"id": project_id, "user_id": str(user_id)},
    )
    await session.execute(
        text(
            """INSERT INTO project_memberships (
                   id, project_id, user_id, role
               ) VALUES (
                   :id, :project_id, :user_id, 'admin'
               )"""
        ),
        {
            "id": membership_id,
            "project_id": project_id,
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text(
            """INSERT INTO agents (
                   id, scope, project_id, slug, display_name,
                   status, definition_id, payload_checksum,
                   created_by_user_id, updated_by_user_id
               ) VALUES (
                   :id, 'project', :project_id, 'run-agent', 'Run Agent',
                   'active', :definition_id, :payload_checksum,
                   :user_id, :user_id
               )"""
        ),
        {
            "id": agent_id,
            "project_id": project_id,
            "definition_id": uuid.uuid4(),
            "payload_checksum": "a" * 64,
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text(
            """INSERT INTO threads_meta (
                   thread_id, owner_user_id, status, metadata_json,
                   created_at, updated_at, project_id, agent_asset_id,
                   agent_scope
               ) VALUES (
                   :thread_id, :user_id, 'idle', '{}'::json, now(), now(),
                   :project_id, :agent_id, 'project'
               )"""
        ),
        {
            "thread_id": "run-closure-thread",
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
                   :id, 'project', :project_id, 'pinned-skill', 'Pinned Skill',
                   'active', :user_id
               )"""
        ),
        {"id": skill_id, "project_id": project_id, "user_id": str(user_id)},
    )
    await session.execute(
        text(
            """INSERT INTO skill_versions (
                   id, skill_id, version_number, scan_decision,
                   payload_checksum, file_count, content_size_bytes,
                   files_sealed, created_by_user_id
               ) VALUES (
                   :id, :skill_id, 1, 'allow', :checksum, 1, :content_size,
                   false, :user_id
               )"""
        ),
        {
            "id": skill_version_id,
            "skill_id": skill_id,
            "checksum": facts.payload_checksum,
            "content_size": len(content),
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text("SELECT set_config('deerflow.asset_version_assembly', :version_id, true)"),
        {"version_id": str(skill_version_id)},
    )
    await session.execute(
        text(
            """INSERT INTO skill_version_files (
                   skill_version_id, path, media_type, size_bytes,
                   sha256, content
               ) VALUES (
                   :version_id, 'SKILL.md', 'text/markdown', :size,
                   :sha256, :content
               )"""
        ),
        {
            "version_id": skill_version_id,
            "size": len(content),
            "sha256": file_sha,
            "content": content,
        },
    )
    await session.execute(
        text("UPDATE skill_versions SET files_sealed=true WHERE id=:version_id"),
        {"version_id": skill_version_id},
    )
    await session.execute(
        text("UPDATE skills SET current_version_id=:version_id WHERE id=:skill_id"),
        {"version_id": skill_version_id, "skill_id": skill_id},
    )
    return _Scope(
        user_id=user_id,
        project_id=project_id,
        membership_id=membership_id,
        agent_id=agent_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        skill_checksum=facts.payload_checksum,
        skill_file_count=1,
        skill_content_size=len(content),
    )


async def _bind_agent_to_skill(
    session: AsyncSession,
    scope: _Scope,
) -> None:
    payload = AgentPayload(
        description="",
        soul="",
        model_ref="default",
        tool_groups=(),
        skill_refs=(SkillAssetRef(AssetScope.PROJECT, scope.skill_id),),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
    await session.scalar(
        select(
            func.set_config(
                "deerflow.agent_definition_mutation_id",
                str(scope.agent_id),
                True,
            )
        )
    )
    await session.execute(
        text(
            """UPDATE agents
                  SET definition_id=:definition_id,
                      payload_checksum=:payload_checksum,
                      revision=revision + 1,
                      updated_by_user_id=:user_id
                WHERE id=:agent_id"""
        ),
        {
            "agent_id": scope.agent_id,
            "definition_id": uuid.uuid4(),
            "payload_checksum": agent_payload_checksum(payload),
            "user_id": str(scope.user_id),
        },
    )
    session.add(
        AgentSkillRefRow(
            agent_id=scope.agent_id,
            skill_asset_scope="project",
            skill_asset_id=scope.skill_id,
            sort_order=0,
        )
    )
    await session.flush()


async def _insert_run(
    session: AsyncSession,
    scope: _Scope,
    run_id: str,
    *,
    status: str = "pending",
    sealed: bool = False,
) -> None:
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
                   :run_id, :thread_id, :owner_user_id, :status,
                   'reject', '{}'::json, '{}'::json, :trace_id, 0, 0, 0, 0,
                   0, 0, 0, 0, '{}'::json, now(), now(), :project_id,
                   'pending', :sealed
               )"""
        ),
        {
            "run_id": run_id,
            "thread_id": scope.thread_id,
            "owner_user_id": str(scope.user_id),
            "status": status,
            "trace_id": f"trace-{run_id}",
            "project_id": scope.project_id,
            "sealed": sealed,
        },
    )


def _snapshot(
    *,
    schema_version: int,
    kind: str,
    scope: str,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    checksum: str,
    catalog_generation: int = 7,
    skill_file_count: int | None = None,
    skill_content_size: int | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": schema_version,
        "kind": kind,
        "scope": scope,
        "asset_id": str(asset_id),
        "version_id": str(version_id),
        "checksum": checksum,
        "catalog_generation": catalog_generation,
        "dependency_version_ids": [],
    }
    if kind == "skill" and schema_version == 4:
        value["skill"] = {
            "source": "skill_version_ref",
            "file_count": skill_file_count,
            "content_size_bytes": skill_content_size,
        }
    else:
        value[kind] = {}
    return value


async def _set_run_assembly(
    session: AsyncSession,
    run_id: str,
) -> None:
    await session.execute(
        text("SELECT set_config('deerflow.run_asset_closure_assembly', :run_id, true)"),
        {"run_id": run_id},
    )


async def _insert_asset(
    session: AsyncSession,
    scope: _Scope,
    run_id: str,
    *,
    kind: str,
    dependency_order: int,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    checksum: str,
    schema_version: int,
    snapshot: dict[str, object],
) -> None:
    await session.execute(
        text(
            """INSERT INTO run_asset_versions (
                   project_id, owner_user_id, thread_id, run_id,
                   asset_kind, dependency_order, asset_scope, asset_id,
                   version_id, payload_checksum, catalog_generation,
                   snapshot_schema_version, snapshot_json
               ) VALUES (
                   :project_id, :owner_user_id, :thread_id, :run_id,
                   :kind, :dependency_order, 'project', :asset_id,
                   :version_id, :checksum, 7, :schema_version,
                   CAST(:snapshot AS jsonb)
               )"""
        ),
        {
            "project_id": scope.project_id,
            "owner_user_id": str(scope.user_id),
            "thread_id": scope.thread_id,
            "run_id": run_id,
            "kind": kind,
            "dependency_order": dependency_order,
            "asset_id": asset_id,
            "version_id": version_id,
            "checksum": checksum,
            "schema_version": schema_version,
            "snapshot": json.dumps(snapshot, separators=(",", ":")),
        },
    )


async def _insert_ref(
    session: AsyncSession,
    scope: _Scope,
    run_id: str,
    *,
    dependency_order: int = 1,
    checksum: str | None = None,
    file_count: int | None = None,
    content_size: int | None = None,
    asset_scope: str = "project",
    skill_project_id: uuid.UUID | None = None,
) -> None:
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
                   'skill', :dependency_order, :asset_scope, 4,
                   :skill_project_id, :skill_id, :skill_version_id,
                   :checksum, :file_count, :content_size
               )"""
        ),
        {
            "project_id": scope.project_id,
            "owner_user_id": str(scope.user_id),
            "thread_id": scope.thread_id,
            "run_id": run_id,
            "dependency_order": dependency_order,
            "asset_scope": asset_scope,
            "skill_project_id": (scope.project_id if skill_project_id is None else skill_project_id),
            "skill_id": scope.skill_id,
            "skill_version_id": scope.skill_version_id,
            "checksum": scope.skill_checksum if checksum is None else checksum,
            "file_count": scope.skill_file_count if file_count is None else file_count,
            "content_size": (scope.skill_content_size if content_size is None else content_size),
        },
    )


async def _insert_valid_closure(
    session: AsyncSession,
    scope: _Scope,
    run_id: str,
) -> None:
    await _set_run_assembly(session, run_id)
    await _insert_agent_parent(session, scope, run_id)
    await _insert_skill_parent(session, scope, run_id)
    await _insert_ref(session, scope, run_id)


async def _insert_agent_parent(
    session: AsyncSession,
    scope: _Scope,
    run_id: str,
    *,
    dependency_order: int = 0,
    schema_version: int = 3,
    typed_checksum: str = "a" * 64,
    json_checksum: str | None = None,
) -> None:
    agent_definition_id = uuid.uuid4()
    await _insert_asset(
        session,
        scope,
        run_id,
        kind="agent",
        dependency_order=dependency_order,
        asset_id=scope.agent_id,
        version_id=agent_definition_id,
        checksum=typed_checksum,
        schema_version=schema_version,
        snapshot=_snapshot(
            schema_version=schema_version,
            kind="agent",
            scope="project",
            asset_id=scope.agent_id,
            version_id=agent_definition_id,
            checksum=(typed_checksum if json_checksum is None else json_checksum),
        ),
    )


async def _insert_skill_parent(
    session: AsyncSession,
    scope: _Scope,
    run_id: str,
    *,
    dependency_order: int = 1,
    schema_version: int = 4,
    snapshot: dict[str, object] | None = None,
) -> None:
    await _insert_asset(
        session,
        scope,
        run_id,
        kind="skill",
        dependency_order=dependency_order,
        asset_id=scope.skill_id,
        version_id=scope.skill_version_id,
        checksum=scope.skill_checksum,
        schema_version=schema_version,
        snapshot=(
            _snapshot(
                schema_version=schema_version,
                kind="skill",
                scope="project",
                asset_id=scope.skill_id,
                version_id=scope.skill_version_id,
                checksum=scope.skill_checksum,
                skill_file_count=scope.skill_file_count,
                skill_content_size=scope.skill_content_size,
            )
            if snapshot is None
            else snapshot
        ),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_run_closure_child_then_seal_is_immutable_and_cascades(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)

        run_id = "closure-valid"
        async with factory() as session, session.begin():
            await _insert_run(session, scope, run_id)
            await _insert_valid_closure(session, scope, run_id)
            await session.execute(
                text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                {"run_id": run_id},
            )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(
                    text("UPDATE runs SET asset_closure_sealed=false WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(
                    text("UPDATE run_asset_versions SET catalog_generation=8 WHERE run_id=:run_id AND dependency_order=0"),
                    {"run_id": run_id},
                )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await _set_run_assembly(session, run_id)
                await _insert_asset(
                    session,
                    scope,
                    run_id,
                    kind="agent",
                    dependency_order=2,
                    asset_id=scope.agent_id,
                    version_id=uuid.uuid4(),
                    checksum="b" * 64,
                    schema_version=3,
                    snapshot=_snapshot(
                        schema_version=3,
                        kind="agent",
                        scope="project",
                        asset_id=scope.agent_id,
                        version_id=uuid.uuid4(),
                        checksum="b" * 64,
                    ),
                )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(
                    text("DELETE FROM run_skill_version_refs WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(text("SELECT set_config('deerflow.system_asset_upgrade', 'on', true)"))
                await session.execute(
                    text("SELECT set_config('deerflow.asset_version_assembly', :version_id, true)"),
                    {"version_id": str(scope.skill_version_id)},
                )
                await _set_run_assembly(session, run_id)
                await session.execute(
                    text("DELETE FROM run_asset_versions WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )

        for statement in (
            """INSERT INTO run_skill_secret_snapshots (
                   project_id, owner_user_id, thread_id, run_id,
                   skill_id, skill_version_id, secret_name, secret_revision,
                   secret_generation_id, secret_generation_digest
               ) VALUES (
                   :project_id, :owner_user_id, :thread_id, :run_id,
                   :asset_id, :version_id, 'TOKEN', 1, :generation_id,
                   :digest
               )""",
            """INSERT INTO run_mcp_secret_snapshots (
                   project_id, owner_user_id, thread_id, run_id,
                   mcp_server_id, mcp_server_version_id, slot_id,
                   secret_revision, secret_generation_id,
                   secret_generation_digest
               ) VALUES (
                   :project_id, :owner_user_id, :thread_id, :run_id,
                   :asset_id, :version_id, :slot_id, 1, :generation_id,
                   :digest
               )""",
        ):
            with pytest.raises(DBAPIError):
                async with factory() as session, session.begin():
                    await _set_run_assembly(session, run_id)
                    await session.execute(
                        text(statement),
                        {
                            "project_id": scope.project_id,
                            "owner_user_id": str(scope.user_id),
                            "thread_id": scope.thread_id,
                            "run_id": run_id,
                            "asset_id": scope.skill_id,
                            "version_id": scope.skill_version_id,
                            "slot_id": uuid.uuid4(),
                            "generation_id": uuid.uuid4(),
                            "digest": "c" * 64,
                        },
                    )

        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM runs WHERE run_id=:run_id"),
                {"run_id": run_id},
            )

        async with factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT count(*) FROM runs WHERE run_id=:run_id),
                           (SELECT count(*) FROM run_asset_versions WHERE run_id=:run_id),
                           (SELECT count(*) FROM run_skill_version_refs WHERE run_id=:run_id)"""
                    ),
                    {"run_id": run_id},
                )
            ).one()
            assert tuple(counts) == (0, 0, 0)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_run_closure_retention_authority_is_exact_and_ref_cascades_only(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    first_run_id = "closure-retention-first"
    other_run_id = "closure-retention-other"
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
            for run_id in (first_run_id, other_run_id):
                await _insert_run(session, scope, run_id, status="success")
                await _insert_valid_closure(session, scope, run_id)
                if run_id == first_run_id:
                    await session.execute(
                        text(
                            """INSERT INTO run_skill_secret_snapshots (
                                   project_id, owner_user_id, thread_id, run_id,
                                   skill_id, skill_version_id, secret_name,
                                   secret_revision, secret_generation_id,
                                   secret_generation_digest
                               ) VALUES (
                                   :project_id, :owner_user_id, :thread_id,
                                   :run_id, :skill_id, :skill_version_id,
                                   'TOKEN', 1, :generation_id, :digest
                               )"""
                        ),
                        {
                            "project_id": scope.project_id,
                            "owner_user_id": str(scope.user_id),
                            "thread_id": scope.thread_id,
                            "run_id": run_id,
                            "skill_id": scope.skill_id,
                            "skill_version_id": scope.skill_version_id,
                            "generation_id": uuid.uuid4(),
                            "digest": "c" * 64,
                        },
                    )
                await session.execute(
                    text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await RetentionPurgeAuthority.issue_single_run(
                    session,
                    purge_id=uuid.uuid4(),
                    project_id=scope.project_id,
                    owner_user_id=str(scope.user_id),
                    thread_id=scope.thread_id,
                    run_id=first_run_id,
                    now=datetime.now(UTC),
                )
                await session.execute(
                    text("DELETE FROM run_skill_version_refs WHERE run_id=:run_id"),
                    {"run_id": first_run_id},
                )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await RetentionPurgeAuthority.issue_single_run(
                    session,
                    purge_id=uuid.uuid4(),
                    project_id=scope.project_id,
                    owner_user_id=str(scope.user_id),
                    thread_id=scope.thread_id,
                    run_id=first_run_id,
                    now=datetime.now(UTC),
                )
                await session.execute(
                    text("DELETE FROM run_asset_versions WHERE run_id=:run_id"),
                    {"run_id": other_run_id},
                )

        async with factory() as session, session.begin():
            authority = await RetentionPurgeAuthority.issue_single_run(
                session,
                purge_id=uuid.uuid4(),
                project_id=scope.project_id,
                owner_user_id=str(scope.user_id),
                thread_id=scope.thread_id,
                run_id=first_run_id,
                now=datetime.now(UTC),
            )
            assert tuple(run.run_id for run in authority.runs) == (first_run_id,)
            await session.execute(
                text("DELETE FROM run_skill_secret_snapshots WHERE run_id=:run_id"),
                {"run_id": first_run_id},
            )
            await session.execute(
                text("DELETE FROM run_asset_versions WHERE run_id=:run_id"),
                {"run_id": first_run_id},
            )

        async with factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT count(*) FROM run_asset_versions
                            WHERE run_id=:first_run_id),
                           (SELECT count(*) FROM run_skill_version_refs
                            WHERE run_id=:first_run_id),
                           (SELECT count(*) FROM run_skill_secret_snapshots
                            WHERE run_id=:first_run_id),
                           (SELECT count(*) FROM run_asset_versions
                            WHERE run_id=:other_run_id),
                           (SELECT count(*) FROM run_skill_version_refs
                            WHERE run_id=:other_run_id),
                           (SELECT asset_closure_sealed FROM runs
                            WHERE run_id=:first_run_id)"""
                    ),
                    {
                        "first_run_id": first_run_id,
                        "other_run_id": other_run_id,
                    },
                )
            ).one()
            assert tuple(counts) == (0, 0, 0, 2, 1, True)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_verified_project_retention_installs_run_closure_authority(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = "closure-project-retention"
    effective_at = datetime.now(UTC)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
            await _insert_run(session, scope, run_id, status="success")
            await _insert_valid_closure(session, scope, run_id)
            await session.execute(
                text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                {"run_id": run_id},
            )
            await session.execute(
                text(
                    """UPDATE projects
                          SET status='pending_deletion',
                              deletion_effective_at=:effective_at
                        WHERE id=:project_id"""
                ),
                {
                    "project_id": scope.project_id,
                    "effective_at": effective_at,
                },
            )

        candidate = RetentionCandidate.project(
            project_id=scope.project_id,
            project_generation=1,
            deletion_effective_at=effective_at,
            idempotency_key="verified-project-retention",
            request_id="verified-project-retention",
        )
        async with factory() as session, session.begin():
            scopes = await RetentionPurgeRepository().verify_still_eligible(
                session,
                candidate,
                now=effective_at,
            )
            assert scopes == ((scope.project_id, str(scope.user_id)),)
            await session.execute(
                text("DELETE FROM run_asset_versions WHERE run_id=:run_id"),
                {"run_id": run_id},
            )

        async with factory() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM run_asset_versions WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )
                == 0
            )
            retained = (
                await session.execute(
                    text("SELECT status, asset_closure_sealed FROM runs WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )
            ).one()
            assert tuple(retained) == ("success", True)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_run_closure_final_verifier_rejects_malformed_state(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await _insert_run(session, scope, "closure-unsealed")
                await _insert_valid_closure(session, scope, "closure-unsealed")

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                run_id = "closure-claimable-job"
                await _insert_run(session, scope, run_id)
                await session.execute(
                    text(
                        """INSERT INTO jobs (
                               id, job_type, project_id, owner_user_id,
                               owner_private_generation,
                               run_id, origin_trace_id, idempotency_key,
                               max_attempts
                           ) VALUES (
                               :id, 'private_run', :project_id, :owner_user_id,
                               1,
                               :run_id, :trace_id, :idempotency_key, 1
                           )"""
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project_id": scope.project_id,
                        "owner_user_id": str(scope.user_id),
                        "run_id": run_id,
                        "trace_id": f"trace-{run_id}",
                        "idempotency_key": "d" * 64,
                    },
                )
                await _set_run_assembly(session, run_id)
                await _insert_agent_parent(session, scope, run_id)

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                run_id = "closure-missing-ref"
                await _insert_run(session, scope, run_id)
                await _set_run_assembly(session, run_id)
                await _insert_agent_parent(session, scope, run_id)
                await _insert_skill_parent(session, scope, run_id)
                await session.execute(
                    text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                run_id = "closure-json-checksum"
                await _insert_run(session, scope, run_id)
                await _set_run_assembly(session, run_id)
                await _insert_agent_parent(
                    session,
                    scope,
                    run_id,
                    json_checksum="b" * 64,
                )
                await session.execute(
                    text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                run_id = "closure-order-gap"
                await _insert_run(session, scope, run_id)
                await _set_run_assembly(session, run_id)
                await _insert_agent_parent(session, scope, run_id)
                await _insert_skill_parent(
                    session,
                    scope,
                    run_id,
                    dependency_order=2,
                )
                await _insert_ref(session, scope, run_id, dependency_order=2)
                await session.execute(
                    text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )

        bad_facts = _snapshot(
            schema_version=4,
            kind="skill",
            scope="project",
            asset_id=scope.skill_id,
            version_id=scope.skill_version_id,
            checksum=scope.skill_checksum,
            skill_file_count=scope.skill_file_count + 1,
            skill_content_size=scope.skill_content_size,
        )
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                run_id = "closure-json-facts"
                await _insert_run(session, scope, run_id)
                await _set_run_assembly(session, run_id)
                await _insert_agent_parent(session, scope, run_id)
                await _insert_skill_parent(
                    session,
                    scope,
                    run_id,
                    snapshot=bad_facts,
                )
                await _insert_ref(session, scope, run_id)
                await session.execute(
                    text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )

        byte_bearing = _snapshot(
            schema_version=4,
            kind="skill",
            scope="project",
            asset_id=scope.skill_id,
            version_id=scope.skill_version_id,
            checksum=scope.skill_checksum,
            skill_file_count=scope.skill_file_count,
            skill_content_size=scope.skill_content_size,
        )
        assert isinstance(byte_bearing["skill"], dict)
        byte_bearing["skill"]["archive_base64"] = "Ynl0ZXM="
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                run_id = "closure-v4-bytes"
                await _insert_run(session, scope, run_id)
                await _set_run_assembly(session, run_id)
                await _insert_agent_parent(session, scope, run_id)
                await _insert_skill_parent(
                    session,
                    scope,
                    run_id,
                    snapshot=byte_bearing,
                )
                await _insert_ref(session, scope, run_id)
                await session.execute(
                    text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                run_id = "closure-agent-v4"
                await _insert_run(session, scope, run_id)
                await _set_run_assembly(session, run_id)
                await _insert_agent_parent(
                    session,
                    scope,
                    run_id,
                    schema_version=4,
                )
                await session.execute(
                    text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )

        for run_id, overrides in (
            ("closure-ref-scope", {"asset_scope": "system"}),
            ("closure-ref-checksum", {"checksum": "0" * 64}),
            (
                "closure-ref-facts",
                {"file_count": scope.skill_file_count + 1},
            ),
        ):
            with pytest.raises(DBAPIError):
                async with factory() as session, session.begin():
                    await _insert_run(session, scope, run_id)
                    await _set_run_assembly(session, run_id)
                    await _insert_agent_parent(session, scope, run_id)
                    await _insert_skill_parent(session, scope, run_id)
                    await _insert_ref(session, scope, run_id, **overrides)

        legacy_run_id = "closure-legacy-v3"
        async with factory() as session, session.begin():
            await _insert_run(session, scope, legacy_run_id)
            await _set_run_assembly(session, legacy_run_id)
            await _insert_agent_parent(session, scope, legacy_run_id)
            await _insert_skill_parent(
                session,
                scope,
                legacy_run_id,
                schema_version=3,
            )
            await session.execute(
                text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                {"run_id": legacy_run_id},
            )

        terminal_shell_id = "closure-purged-shell"
        async with factory() as session, session.begin():
            await _insert_run(
                session,
                scope,
                terminal_shell_id,
                status="success",
                sealed=True,
            )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await _insert_run(
                    session,
                    scope,
                    "closure-empty-pending",
                    status="pending",
                    sealed=True,
                )

        async with factory() as session:
            retained = await session.scalar(
                text("SELECT count(*) FROM runs WHERE run_id IN (:legacy, :shell)"),
                {"legacy": legacy_run_id, "shell": terminal_shell_id},
            )
            assert retained == 2
    finally:
        await engine.dispose()


async def _insert_committed_unsealed_staging_run(
    factory: async_sessionmaker[AsyncSession],
    scope: _Scope,
    run_id: str,
    *,
    status: str,
) -> None:
    async with factory() as session, session.begin():
        await session.execute(text("ALTER TABLE runs DISABLE TRIGGER trg_runs_asset_closure_complete"))
        await _insert_run(session, scope, run_id, status=status)
        await session.execute(text("ALTER TABLE runs ENABLE TRIGGER trg_runs_asset_closure_complete"))


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_private_run_job_requires_a_sealed_closure(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
        job_scope = JobScope(scope.project_id, str(scope.user_id))

        unsealed_run_id = "closure-job-unsealed"
        await _insert_committed_unsealed_staging_run(
            factory,
            scope,
            unsealed_run_id,
            status="pending",
        )
        with pytest.raises(JobIdempotencyConflict):
            async with factory() as session, session.begin():
                await PrivateRunJobRepository(session).enqueue(
                    scope=job_scope,
                    run_id=unsealed_run_id,
                    origin_trace_id=f"trace-{unsealed_run_id}",
                    account_private_generation=AccountPrivateGeneration(
                        owner_user_id=str(scope.user_id),
                        generation=1,
                    ),
                )

        sealed_run_id = "closure-job-sealed"
        async with factory() as session, session.begin():
            await _insert_run(session, scope, sealed_run_id)
            await _insert_valid_closure(session, scope, sealed_run_id)
            await session.execute(
                text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                {"run_id": sealed_run_id},
            )
            admitted = await PrivateRunJobRepository(session).enqueue(
                scope=job_scope,
                run_id=sealed_run_id,
                origin_trace_id=f"trace-{sealed_run_id}",
                account_private_generation=AccountPrivateGeneration(
                    owner_user_id=str(scope.user_id),
                    generation=1,
                ),
            )
            assert admitted.status == "queued"

        async with factory() as session:
            job_rows = (
                await session.execute(
                    text("SELECT run_id, status FROM jobs WHERE run_id IN (:unsealed, :sealed)"),
                    {
                        "unsealed": unsealed_run_id,
                        "sealed": sealed_run_id,
                    },
                )
            ).all()
            assert [(row.run_id, row.status) for row in job_rows] == [(sealed_run_id, "queued")]
    finally:
        await engine.dispose()


def _private_context(scope: _Scope) -> PrivateWorkContext:
    project_context = ProjectContext(
        user_id=scope.user_id,
        project_id=scope.project_id,
        membership_id=scope.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="r1-snapshot-writer",
    )
    return PrivateWorkContext.from_project(project_context)


def _resolved_agent(scope: _Scope, *, checksum: str = "a" * 64) -> ResolvedAgentSnapshot:
    return ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=scope.agent_id,
        version_id=uuid.uuid4(),
        checksum=checksum,
        catalog_generation=7,
        dependency_version_ids=(),
        payload=AgentPayload(
            description="",
            soul="R1 snapshot writer",
            model_ref="00000000-0000-4000-8000-000000000305",
            tool_groups=(),
            skill_refs=(),
            mcp_version_ids=(),
        ),
        skill_version_ids=(),
    )


def _resolved_v4_closure(scope: _Scope) -> ResolvedRunAssetClosure:
    lead = _resolved_agent(scope)
    skill = ResolvedSkillVersionSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope.PROJECT,
        asset_id=scope.skill_id,
        version_id=scope.skill_version_id,
        checksum=scope.skill_checksum,
        catalog_generation=lead.catalog_generation,
        dependency_version_ids=(),
        file_count=scope.skill_file_count,
        content_size_bytes=scope.skill_content_size,
        secret_requirements=(),
    )
    return ResolvedRunAssetClosure(
        lead_agent=lead,
        delegated_agents=(),
        skills=(skill,),
        mcps=(),
        main_skill_version_ids=(skill.version_id,),
        main_mcp_version_ids=(),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_r2_writer_persists_byte_free_v4_parent_and_exact_ref(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
        context = _private_context(scope)
        closure = _resolved_v4_closure(scope)
        repository = RunSnapshotRepository(factory)

        async def validate(session, *_args):
            skill = (
                await session.execute(
                    select(SkillRow, SkillVersionRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.skill_id == SkillRow.id,
                    )
                    .where(SkillVersionRow.id == scope.skill_version_id)
                )
            ).one()
            return (
                [(skill.SkillRow, skill.SkillVersionRow)],
                [],
                {},
                {
                    scope.skill_version_id: SimpleNamespace(materials=()),
                },
            )

        repository.validate_run_asset_closure_in_session = AsyncMock(
            side_effect=validate,
        )

        def capture(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        with pytest.raises(PrivateWorkUnavailable):
            await repository.create_run_with_snapshot(
                context,
                scope.thread_id,
                PrivateRunCreate(run_id="r2-missing-cohort"),
                closure,
            )
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RunRow)
                    .where(
                        RunRow.run_id == "r2-missing-cohort",
                    )
                )
                == 0
            )
        async with active_test_run_skill_writer_cohort(engine):
            created = await repository.create_run_with_snapshot(
                context,
                scope.thread_id,
                PrivateRunCreate(run_id="r2-v4-writer"),
                closure,
            )
        assert created.run_id == "r2-v4-writer"

        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT asset.snapshot_schema_version,
                                  asset.snapshot_json->>'schema_version',
                                  asset.snapshot_json ? 'files',
                                  asset.snapshot_json->'skill'->>'source',
                                  octet_length(asset.snapshot_json::text),
                                  ref.file_count,
                                  ref.content_size_bytes,
                                  ref.payload_checksum,
                                  count(*) OVER () AS ref_count
                           FROM run_asset_versions asset
                           JOIN run_skill_version_refs ref
                             ON ref.project_id=asset.project_id
                            AND ref.owner_user_id=asset.owner_user_id
                            AND ref.run_id=asset.run_id
                            AND ref.asset_kind=asset.asset_kind
                            AND ref.dependency_order=asset.dependency_order
                           WHERE asset.run_id='r2-v4-writer'
                             AND asset.asset_kind='skill'"""
                    )
                )
            ).one()
        assert tuple(row) == (
            4,
            "4",
            False,
            "skill_version_ref",
            row[4],
            scope.skill_file_count,
            scope.skill_content_size,
            scope.skill_checksum,
            1,
        )
        assert row[4] < 2_048
        assert not any("pg_try_advisory_xact_lock" in statement or "skill_version_files.content" in statement for statement in statements)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_legacy_mode_uses_same_repository_to_write_v3_without_ref(
    postgres_database_url: str,
) -> None:
    reset_run_skill_snapshot_writer_for_testing()
    freeze_run_skill_snapshot_writer(
        RunSkillSnapshotConfig(
            writer_mode="legacy_v3",
            expected_artifact_version=(RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION),
            expected_legacy_policy_digest=(LEGACY_ADMISSION_POLICY.canonical_digest()),
        )
    )
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
        context = _private_context(scope)
        closure = _resolved_v4_closure(scope)
        repository = RunSnapshotRepository(factory)

        async def validate(session, *_args):
            skill = (
                await session.execute(
                    select(SkillRow, SkillVersionRow)
                    .join(
                        SkillVersionRow,
                        SkillVersionRow.skill_id == SkillRow.id,
                    )
                    .where(SkillVersionRow.id == scope.skill_version_id)
                    .with_for_update(read=True, of=[SkillRow, SkillVersionRow])
                )
            ).one()
            return (
                [(skill.SkillRow, skill.SkillVersionRow)],
                [],
                {},
                {
                    scope.skill_version_id: SimpleNamespace(materials=()),
                },
            )

        repository.validate_run_asset_closure_in_session = AsyncMock(
            side_effect=validate,
        )

        def capture(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with active_test_run_skill_writer_cohort(engine):
            created = await repository.create_run_with_snapshot(
                context,
                scope.thread_id,
                PrivateRunCreate(run_id="legacy-v3-writer"),
                closure,
            )
        assert created.run_id == "legacy-v3-writer"

        async with factory() as session:
            persisted = (
                await session.execute(
                    text(
                        """SELECT asset.snapshot_schema_version,
                                  asset.snapshot_json->'skill'->>'codec',
                                  asset.snapshot_json->'skill' ? 'archive_base64',
                                  (SELECT count(*) FROM run_skill_version_refs ref
                                    WHERE ref.run_id=asset.run_id)
                           FROM run_asset_versions asset
                           WHERE asset.run_id='legacy-v3-writer'
                             AND asset.asset_kind='skill'"""
                    )
                )
            ).one()
        assert tuple(persisted) == (
            3,
            "canonical-frame-zlib-6",
            True,
            0,
        )
        assert sum("pg_try_advisory_xact_lock" in statement for statement in statements) == 1
        assert sum("skill_version_files.content" in statement for statement in statements) == 1
    finally:
        reset_run_skill_snapshot_writer_for_testing()
        await engine.dispose()


@pytest.mark.parametrize("schema_version", [3, 4], ids=["legacy-v3", "reference-v4"])
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_admission_first_allows_archive_and_retains_skill_package(
    postgres_database_url: str,
    schema_version: int,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admission = factory()
    admission_transaction = await admission.begin()
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
            await _bind_agent_to_skill(session, scope)
        actor = _project_context(scope, f"admission-first-{schema_version}")

        locked = (
            await admission.execute(
                select(SkillRow, SkillVersionRow).join(SkillVersionRow, SkillVersionRow.skill_id == SkillRow.id).where(SkillVersionRow.id == scope.skill_version_id).with_for_update(read=True, of=[SkillRow, SkillVersionRow])
            )
        ).one()
        assert locked.SkillVersionRow.id == scope.skill_version_id

        run_id = f"admission-first-{schema_version}"
        await _insert_run(admission, scope, run_id)
        await _set_run_assembly(admission, run_id)
        await _insert_agent_parent(admission, scope, run_id)
        await _insert_skill_parent(
            admission,
            scope,
            run_id,
            schema_version=schema_version,
        )
        if schema_version == 4:
            await _insert_ref(admission, scope, run_id)
        await admission.execute(
            text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
            {"run_id": run_id},
        )

        pid_ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def archive_after_lock():
            async with factory() as session, session.begin():
                pid = int(await session.scalar(text("SELECT pg_backend_pid()")))
                pid_ready.set_result(pid)
                return await SkillDeletionCoordinator(AgentService(factory)).delete_in_session(
                    session,
                    actor,
                    scope.skill_id,
                    1,
                )

        archive_task = asyncio.create_task(archive_after_lock())
        await _wait_for_backend_lock(factory, await pid_ready)
        await admission_transaction.commit()
        archived = await archive_task
        assert archived.affected_agent_count == 1

        async with factory() as session:
            agent = await session.get(AgentRow, scope.agent_id)
            assert agent is not None
            assert agent.status == "active"
            assert agent.revision == 3
            assert await session.scalar(select(func.count()).select_from(AgentSkillRefRow).where(AgentSkillRefRow.agent_id == scope.agent_id)) == 0
            skill = await session.get(SkillRow, scope.skill_id)
            assert skill is not None
            assert skill.status == "archived"
            assert skill.current_version_id == scope.skill_version_id
            assert await session.get(SkillVersionRow, scope.skill_version_id) is not None
            assert await session.scalar(select(func.count()).select_from(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == scope.skill_version_id)) == scope.skill_file_count
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM run_asset_versions WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )
                == 2
            )
            expected_ref_count = 1 if schema_version == 4 else 0
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM run_skill_version_refs WHERE run_id=:run_id"),
                    {"run_id": run_id},
                )
                == expected_ref_count
            )
    finally:
        if admission.in_transaction():
            await admission.rollback()
        await admission.close()
        await engine.dispose()


@pytest.mark.parametrize(
    "writer_mode",
    ["legacy_v3", "v4_reference"],
    ids=["legacy-v3", "reference-v4"],
)
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_archive_first_rejects_admission_and_retains_skill_package(
    postgres_database_url: str,
    writer_mode: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    deletion = factory()
    deletion_transaction = await deletion.begin()
    reset_run_skill_snapshot_writer_for_testing()
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
            await _bind_agent_to_skill(session, scope)
        actor = _project_context(scope, f"delete-first-{writer_mode}")
        config = (
            RunSkillSnapshotConfig()
            if writer_mode == "v4_reference"
            else RunSkillSnapshotConfig(
                writer_mode="legacy_v3",
                expected_artifact_version=(RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION),
                expected_legacy_policy_digest=(LEGACY_ADMISSION_POLICY.canonical_digest()),
            )
        )
        freeze_run_skill_snapshot_writer(config)

        deleted = await SkillDeletionCoordinator(AgentService(factory)).delete_in_session(
            deletion,
            actor,
            scope.skill_id,
            1,
        )
        assert deleted.affected_agent_count == 1

        pid_ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def validate_after_delete() -> None:
            async with factory() as session, session.begin():
                pid_ready.set_result(int(await session.scalar(text("SELECT pg_backend_pid()"))))
                await RunSnapshotRepository._skills(  # noqa: SLF001
                    session,
                    (scope.skill_version_id,),
                    scope.project_id,
                )

        admission_task = asyncio.create_task(validate_after_delete())
        await _wait_for_backend_lock(factory, await pid_ready)
        await deletion_transaction.commit()
        with pytest.raises(RunSnapshotAssetStale):
            await admission_task

        async with factory() as session:
            agent = await session.get(AgentRow, scope.agent_id)
            assert agent is not None
            assert agent.status == "active"
            assert agent.revision == 3
            assert await session.scalar(select(func.count()).select_from(AgentSkillRefRow).where(AgentSkillRefRow.agent_id == scope.agent_id)) == 0
            skill = await session.get(SkillRow, scope.skill_id)
            assert skill is not None
            assert skill.status == "archived"
            assert skill.current_version_id == scope.skill_version_id
            assert await session.get(SkillVersionRow, scope.skill_version_id) is not None
            assert await session.scalar(select(func.count()).select_from(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == scope.skill_version_id)) == scope.skill_file_count
    finally:
        reset_run_skill_snapshot_writer_for_testing()
        if deletion.in_transaction():
            await deletion.rollback()
        await deletion.close()
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_r1_snapshot_writer_seals_v3_before_job_and_rolls_back(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    writer_cohort = None
    try:
        await _install_full_schema(engine)
        writer_cohort = await start_test_run_skill_writer_cohort(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
        context = _private_context(scope)
        repository = RunSnapshotRepository(factory)
        repository.validate_agent_closure_in_session = AsyncMock(return_value=([], [], {}, {}))

        run_id = "r1-writer-success"
        created = await repository.create_run_with_snapshot(
            context,
            scope.thread_id,
            PrivateRunCreate(run_id=run_id),
            _resolved_agent(scope),
        )
        assert created.run_id == run_id

        async with factory() as session:
            persisted = (
                await session.execute(
                    text(
                        """SELECT run.asset_closure_sealed,
                                  asset.snapshot_schema_version,
                                  asset.snapshot_json->>'schema_version' AS json_schema,
                                  asset.dependency_order,
                                  (SELECT count(*) FROM jobs WHERE run_id=run.run_id)
                                     AS job_count
                           FROM runs run
                           JOIN run_asset_versions asset
                             ON asset.project_id=run.project_id
                            AND asset.owner_user_id=run.owner_user_id
                            AND asset.thread_id=run.thread_id
                            AND asset.run_id=run.run_id
                           WHERE run.run_id=:run_id"""
                    ),
                    {"run_id": run_id},
                )
            ).one()
            assert tuple(persisted) == (True, 3, "3", 0, 0)

        async with factory() as session, session.begin():
            admitted = await PrivateRunJobRepository(session).enqueue(
                scope=JobScope(scope.project_id, str(scope.user_id)),
                run_id=run_id,
                origin_trace_id=created.origin_trace_id,
                account_private_generation=AccountPrivateGeneration(
                    owner_user_id=str(scope.user_id),
                    generation=1,
                ),
            )
            attached = await PrivateRunRepository(session).attach_job(
                scope=context.resource_scope,
                run_id=run_id,
                job_id=admitted.job_id,
            )
            assert attached.job_id == admitted.job_id

        bad_parent_run_id = "r1-writer-parent-failure"
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await repository.create_run_with_snapshot_in_session(
                    session,
                    context,
                    scope.thread_id,
                    PrivateRunCreate(run_id=bad_parent_run_id),
                    _resolved_agent(scope, checksum="bad"),
                )

        bad_secret_run_id = "r1-writer-secret-failure"
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await repository.create_run_with_snapshot_in_session(
                    session,
                    context,
                    scope.thread_id,
                    PrivateRunCreate(run_id=bad_secret_run_id),
                    _resolved_agent(scope),
                )
                await session.execute(
                    text(
                        """INSERT INTO run_skill_secret_snapshots (
                               project_id, owner_user_id, thread_id, run_id,
                               skill_id, skill_version_id, secret_name,
                               secret_revision, secret_generation_id,
                               secret_generation_digest
                           ) VALUES (
                               :project_id, :owner_user_id, :thread_id, :run_id,
                               :skill_id, :skill_version_id, 'TOKEN', 1,
                               :generation_id, :digest
                           )"""
                    ),
                    {
                        "project_id": scope.project_id,
                        "owner_user_id": str(scope.user_id),
                        "thread_id": scope.thread_id,
                        "run_id": bad_secret_run_id,
                        "skill_id": scope.skill_id,
                        "skill_version_id": scope.skill_version_id,
                        "generation_id": uuid.uuid4(),
                        "digest": "e" * 64,
                    },
                )

        bad_job_run_id = "r1-writer-job-failure"
        with pytest.raises(JobIdempotencyConflict):
            async with factory() as session, session.begin():
                run = await repository.create_run_with_snapshot_in_session(
                    session,
                    context,
                    scope.thread_id,
                    PrivateRunCreate(run_id=bad_job_run_id),
                    _resolved_agent(scope),
                )
                await PrivateRunJobRepository(session).enqueue(
                    scope=JobScope(scope.project_id, str(scope.user_id)),
                    run_id=bad_job_run_id,
                    origin_trace_id=f"wrong-{run.origin_trace_id}",
                    account_private_generation=AccountPrivateGeneration(
                        owner_user_id=str(scope.user_id),
                        generation=1,
                    ),
                )

        async with factory() as session:
            failed_counts = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT count(*) FROM runs
                             WHERE run_id IN (:parent, :secret, :job)),
                           (SELECT count(*) FROM run_asset_versions
                             WHERE run_id IN (:parent, :secret, :job)),
                           (SELECT count(*) FROM run_skill_secret_snapshots
                             WHERE run_id IN (:parent, :secret, :job)),
                           (SELECT count(*) FROM jobs
                             WHERE run_id IN (:parent, :secret, :job))"""
                    ),
                    {
                        "parent": bad_parent_run_id,
                        "secret": bad_secret_run_id,
                        "job": bad_job_run_id,
                    },
                )
            ).one()
            assert tuple(failed_counts) == (0, 0, 0, 0)
    finally:
        if writer_cohort is not None:
            await writer_cohort.close()
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_direct_run_creation_is_terminal_shell_only(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
        context = _private_context(scope)

        with pytest.raises(PrivateRunConflict):
            async with factory() as session, session.begin():
                await PrivateRunRepository(session).create(
                    scope=context.resource_scope,
                    thread_id=scope.thread_id,
                    request=PrivateRunCreate(run_id="direct-executable-forbidden"),
                )

        shell_id = "direct-terminal-shell"
        async with factory() as session, session.begin():
            shell = await PrivateRunRepository(session).create_terminal_empty_shell(
                scope=context.resource_scope,
                thread_id=scope.thread_id,
                request=PrivateRunCreate(
                    run_id=shell_id,
                    status="success",
                ),
            )
            assert shell.status == "success"

        with pytest.raises(ValueError, match="snapshot admission"):
            await HarnessRunRepository(factory).put(
                "harness-direct-forbidden",
                thread_id=scope.thread_id,
                scope=context.resource_scope,
            )

        async with factory() as session:
            states = (await session.execute(text("SELECT run_id, status, asset_closure_sealed FROM runs ORDER BY run_id"))).all()
            assert [(row.run_id, row.status, row.asset_closure_sealed) for row in states] == [(shell_id, "success", True)]
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_run_closure_two_connections_serialize_child_and_seal(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)

        child_first_run = "closure-race-child-first"
        await _insert_committed_unsealed_staging_run(
            factory,
            scope,
            child_first_run,
            status="pending",
        )
        seal_started = asyncio.Event()

        async def competing_seal() -> None:
            async with factory() as session, session.begin():
                seal_started.set()
                await session.execute(
                    text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                    {"run_id": child_first_run},
                )

        async with factory() as child_session:
            child_transaction = await child_session.begin()
            await _set_run_assembly(child_session, child_first_run)
            await _insert_agent_parent(
                child_session,
                scope,
                child_first_run,
            )
            seal_task = asyncio.create_task(competing_seal())
            await seal_started.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(seal_task), timeout=0.1)
            await child_session.execute(
                text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                {"run_id": child_first_run},
            )
            await child_transaction.commit()
            await seal_task

        seal_first_run = "closure-race-seal-first"
        await _insert_committed_unsealed_staging_run(
            factory,
            scope,
            seal_first_run,
            status="success",
        )
        child_started = asyncio.Event()

        async def competing_child() -> None:
            async with factory() as session, session.begin():
                await _set_run_assembly(session, seal_first_run)
                child_started.set()
                await _insert_agent_parent(
                    session,
                    scope,
                    seal_first_run,
                )

        async with factory() as seal_session:
            seal_transaction = await seal_session.begin()
            await seal_session.execute(
                text("UPDATE runs SET asset_closure_sealed=true WHERE run_id=:run_id"),
                {"run_id": seal_first_run},
            )
            child_task = asyncio.create_task(competing_child())
            await child_started.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(child_task), timeout=0.1)
            await seal_transaction.commit()
            with pytest.raises(DBAPIError):
                await child_task

        async with factory() as session:
            states = (
                await session.execute(
                    text(
                        """SELECT run_id, asset_closure_sealed,
                                  (SELECT count(*) FROM run_asset_versions asset
                                   WHERE asset.run_id = run.run_id) AS asset_count
                           FROM runs run
                           WHERE run_id IN (:child_first, :seal_first)
                           ORDER BY run_id"""
                    ),
                    {
                        "child_first": child_first_run,
                        "seal_first": seal_first_run,
                    },
                )
            ).all()
            assert {(row.run_id, row.asset_closure_sealed, row.asset_count) for row in states} == {
                (child_first_run, True, 1),
                (seal_first_run, True, 0),
            }
    finally:
        await engine.dispose()
