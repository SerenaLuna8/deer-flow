from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.private_work import asset_runtime as asset_runtime_module
from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkAssetStale, PrivateWorkUnavailable
from app.private_work.execution_approval import _asset_closure
from app.private_work.run_admission import (
    AdmittedPrivateRun,
    PersistedRunSnapshot,
    PrivateRunAdmissionService,
)
from app.private_work.run_repository import PrivateRunRecord, PrivateRunRepository
from app.private_work.run_skill_tree_materializer import (
    LegacyInlineRunSkillPlan,
    LegacyInlineRunSkillSourceAdapter,
    MaterializationAttemptIdentity,
    MaterializationAuthorityReadback,
    PinnedSkillVersionPlan,
    PinnedSkillVersionSourceAdapter,
    RunSkillTreeMaterializationPlan,
    RunSkillTreeMaterializationStale,
    RunSkillTreeMaterializer,
)
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import (
    ProjectContext,
    resolve_project_context_in_transaction,
)
from app.projects.models import ProjectRole
from app.reliability.jobs import AdmittedJobRecord, PrivateRunJobRepository
from app.reliability.run_execution.handler import PrivateRunJobHandler
from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedRunAssetFact,
    ResolvedSkillSnapshot,
    SkillArchiveFile,
    SkillAssetRef,
    SkillSecretRequirementSnapshot,
)
from app.shared_assets.run_snapshot_codec import encode_run_asset_snapshot
from app.shared_assets.skill_version_facts import skill_version_archive_facts
from deerflow.config.worker_config import (
    LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES,
    WorkerConfig,
)
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.jobs.model import WorkerNodeRow
from deerflow.persistence.jobs.sql import JobRepository, JobScope
from deerflow.persistence.user.private_lifecycle import AccountPrivateGeneration
from deerflow.sandbox import NotAcquired
from deerflow.skills.types import SkillCategory


@dataclass(frozen=True, slots=True)
class _File:
    path: str
    media_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _Version:
    scope: AssetScope
    skill_id: uuid.UUID
    version_id: uuid.UUID
    checksum: str
    files: tuple[_File, ...]
    secret_requirements: tuple[SkillSecretRequirementSnapshot, ...]

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def content_size_bytes(self) -> int:
        return sum(len(value.content) for value in self.files)


@dataclass(frozen=True, slots=True)
class _LegacyVersion:
    snapshot: ResolvedSkillSnapshot
    schema_version: int
    encoded: dict[str, object]

    @property
    def scope(self) -> AssetScope:
        return self.snapshot.scope

    @property
    def skill_id(self) -> uuid.UUID:
        return self.snapshot.asset_id

    @property
    def version_id(self) -> uuid.UUID:
        return self.snapshot.version_id

    @property
    def checksum(self) -> str:
        return self.snapshot.checksum

    @property
    def files(self) -> tuple[_File, ...]:
        return tuple(_File(value.path, value.media_type, value.content) for value in self.snapshot.files)

    @property
    def secret_requirements(
        self,
    ) -> tuple[SkillSecretRequirementSnapshot, ...]:
        return self.snapshot.secret_requirements


@dataclass(frozen=True, slots=True)
class _Scope:
    user_id: uuid.UUID
    project_id: uuid.UUID
    membership_id: uuid.UUID
    agent_id: uuid.UUID
    thread_id: str


class _Authority:
    def __init__(
        self,
        readback: MaterializationAuthorityReadback,
        *,
        drift_at: tuple[str, int | None] | None = None,
    ) -> None:
        self.readback = readback
        self.drift_at = drift_at
        self.boundaries: list[tuple[str, int | None]] = []

    async def read_materialization_authority(
        self,
        *,
        boundary: str,
        dependency_order: int | None,
    ) -> MaterializationAuthorityReadback:
        self.boundaries.append((boundary, dependency_order))
        if (boundary, dependency_order) == self.drift_at:
            return replace(self.readback, plan_fingerprint="f" * 64)
        return self.readback


class _ExecutionBoundary:
    def __init__(self) -> None:
        self.execution_job_id = uuid.uuid4()
        self.attempt_id = uuid.uuid4()
        self.expected_worker_id = uuid.uuid4()
        self.snapshot_checks = 0
        self.materialization_checks = 0

    async def before_checkpoint_read(self) -> None:
        self.snapshot_checks += 1

    async def lock_and_assert_materialization_active_in_session(
        self,
        _session: object,
        _locked_context: ProjectContext,
    ) -> MaterializationAttemptIdentity:
        self.materialization_checks += 1
        return MaterializationAttemptIdentity(
            job_id=uuid.UUID(str(self.execution_job_id)),
            attempt_id=uuid.UUID(str(self.attempt_id)),
            worker_id=uuid.UUID(str(self.expected_worker_id)),
        )


@pytest.mark.asyncio
async def test_materialization_authority_uses_one_governance_execution_fingerprint_transaction() -> None:
    events: list[tuple[str, object]] = []
    identity = MaterializationAttemptIdentity(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        worker_id=uuid.uuid4(),
    )
    project_context = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="atomic-materialization-authority",
    )
    private_context = PrivateWorkContext.from_project(project_context)

    class Transaction:
        async def __aenter__(self) -> None:
            events.append(("transaction-enter", session))

        async def __aexit__(self, *_args: object) -> None:
            events.append(("transaction-exit", session))

    class SessionContext:
        async def __aenter__(self) -> object:
            events.append(("session-enter", self))
            return self

        async def __aexit__(self, *_args: object) -> None:
            events.append(("session-exit", self))

        def begin(self) -> Transaction:
            return Transaction()

    session = SessionContext()

    class Factory:
        def __call__(self) -> SessionContext:
            events.append(("factory", session))
            return session

    class Revalidator:
        async def require(
            self,
            observed_session: object,
            observed_context: PrivateWorkContext,
            *observed_capabilities: Capability,
            lock_mode: str,
        ) -> ProjectContext:
            assert observed_session is session
            assert observed_context is private_context
            assert observed_capabilities == (Capability.PRIVATE_WORK_CREATE,)
            assert lock_mode == "share"
            events.append(("governance", observed_session))
            return project_context

    class Boundary:
        async def lock_and_assert_materialization_active_in_session(
            self,
            observed_session: object,
            locked_context: ProjectContext,
        ) -> MaterializationAttemptIdentity:
            assert observed_session is session
            assert locked_context is project_context
            events.append(("execution", observed_session))
            return identity

    async def fingerprint_reader(
        observed_session: object,
        locked_context: ProjectContext,
    ) -> str:
        assert observed_session is session
        assert locked_context is project_context
        events.append(("fingerprint", observed_session))
        return "f" * 64

    authority = asset_runtime_module._AssetRuntimeMaterializationAuthority(
        Boundary(),
        session_factory=Factory(),  # type: ignore[arg-type]
        revalidator=Revalidator(),  # type: ignore[arg-type]
        context=private_context,
        capabilities=(Capability.PRIVATE_WORK_CREATE,),
        fingerprint_reader=fingerprint_reader,  # type: ignore[arg-type]
    )

    readback = await authority.read_materialization_authority(
        boundary="version",
        dependency_order=3,
    )

    assert readback == MaterializationAuthorityReadback(
        attempt_identity=identity,
        plan_fingerprint="f" * 64,
    )
    assert [name for name, _value in events] == [
        "factory",
        "session-enter",
        "transaction-enter",
        "governance",
        "execution",
        "fingerprint",
        "transaction-exit",
        "session-exit",
    ]
    assert all(value is session for _name, value in events)


def _private_context(scope: _Scope) -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=scope.user_id,
            project_id=scope.project_id,
            membership_id=scope.membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="asset-runtime-v4",
        )
    )


def _admitted(
    scope: _Scope,
    boundary: _ExecutionBoundary,
    *,
    run_id: str,
) -> AdmittedPrivateRun:
    now = datetime.now(UTC)
    run = PrivateRunRecord(
        run_id=run_id,
        thread_id=scope.thread_id,
        project_id=scope.project_id,
        owner_user_id=str(scope.user_id),
        assistant_id=str(scope.agent_id),
        status="pending",
        multitask_strategy="reject",
        metadata={},
        kwargs={},
        origin_trace_id=f"trace-{run_id}",
        error=None,
        model_name="test-model",
        created_at=now,
        updated_at=now,
    )
    return AdmittedPrivateRun(
        run=run,
        job=AdmittedJobRecord(
            job_id=boundary.execution_job_id,
            job_type="private_run",
            project_id=scope.project_id,
            owner_user_id=str(scope.user_id),
            run_id=run_id,
            idempotency_key="i" * 64,
            status="queued",
            origin_trace_id=run.origin_trace_id,
        ),
        snapshot=PersistedRunSnapshot(
            assets=(),
            mcp_secrets=(),
            catalog_generation=7,
        ),
        opaque_runtime_scope=_private_context(scope).resource_scope,
    )


def _files(
    runtime_name: str,
    marker: str,
    *,
    count: int,
    include_secret: bool = True,
) -> tuple[_File, ...]:
    secret_frontmatter = "required-secrets:\n  - name: API_TOKEN\n    target_env: PINNED_API_TOKEN\n    optional: false\n" if include_secret else ""
    manifest = (f"---\nname: {runtime_name}\ndescription: {marker} pinned Skill.\n{secret_frontmatter}---\n").encode()
    files = [
        _File(
            path="SKILL.md",
            media_type="text/markdown",
            content=manifest,
        ),
        _File(
            path="资料/说明-🦌.txt",
            media_type="text/plain",
            content=f"{marker}-unicode".encode(),
        ),
    ]
    files.extend(
        _File(
            path=f"data/{index:03d}.txt",
            media_type="text/plain",
            content=(f"{marker}-{index}" * (index % 3 + 1)).encode(),
        )
        for index in range(count)
    )
    return tuple(sorted(files, key=lambda value: value.path))


def _legacy_version(
    scope: AssetScope,
    runtime_name: str,
    marker: str,
    *,
    schema_version: int,
) -> _LegacyVersion:
    source_files = _files(
        runtime_name,
        marker,
        count=1,
        include_secret=False,
    )
    files = tuple(SkillArchiveFile(value.path, value.content, value.media_type) for value in source_files)
    facts = skill_version_archive_facts(
        tuple(
            (
                value.path,
                hashlib.sha256(value.content).hexdigest(),
                len(value.content),
            )
            for value in files
        )
    )
    snapshot = ResolvedSkillSnapshot(
        kind=AssetKind.SKILL,
        scope=scope,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum=facts.payload_checksum,
        catalog_generation=7,
        dependency_version_ids=(),
        files=files,
        secret_requirements=(),
    )
    if schema_version == 3:
        encoded = encode_run_asset_snapshot(snapshot)
    elif schema_version == 2:
        encoded = {
            "schema_version": 2,
            "kind": "skill",
            "scope": scope.value,
            "asset_id": str(snapshot.asset_id),
            "version_id": str(snapshot.version_id),
            "checksum": snapshot.checksum,
            "catalog_generation": snapshot.catalog_generation,
            "dependency_version_ids": [],
            "skill": {
                "files": [
                    {
                        "path": value.path,
                        "media_type": value.media_type,
                        "content_base64": base64.b64encode(
                            value.content,
                        ).decode("ascii"),
                    }
                    for value in files
                ],
                "secret_requirements": [],
            },
        }
    else:
        raise ValueError("unsupported legacy fixture schema")
    return _LegacyVersion(
        snapshot=snapshot,
        schema_version=schema_version,
        encoded=encoded,
    )


def _large_legacy_v2_version() -> _LegacyVersion:
    content = (bytes(range(251)) * 8_400)[: 2 * 1024 * 1024]
    files = (
        SkillArchiveFile(
            "SKILL.md",
            b"---\nname: large-legacy\ndescription: Large legacy fixture.\n---\n",
            "text/markdown",
        ),
        SkillArchiveFile("data/payload.bin", content, "application/octet-stream"),
    )
    facts = skill_version_archive_facts(
        tuple(
            (
                value.path,
                hashlib.sha256(value.content).hexdigest(),
                len(value.content),
            )
            for value in files
        )
    )
    snapshot = ResolvedSkillSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum=facts.payload_checksum,
        catalog_generation=7,
        dependency_version_ids=(),
        files=files,
        secret_requirements=(),
    )
    return _LegacyVersion(
        snapshot=snapshot,
        schema_version=2,
        encoded={
            "schema_version": 2,
            "kind": "skill",
            "scope": "project",
            "asset_id": str(snapshot.asset_id),
            "version_id": str(snapshot.version_id),
            "checksum": snapshot.checksum,
            "catalog_generation": 7,
            "dependency_version_ids": [],
            "skill": {
                "files": [
                    {
                        "path": value.path,
                        "media_type": value.media_type,
                        "content_base64": base64.b64encode(value.content).decode(
                            "ascii",
                        ),
                    }
                    for value in files
                ],
                "secret_requirements": [],
            },
        },
    )


def _legacy_from_version(
    version: _Version,
    *,
    schema_version: int,
) -> _LegacyVersion:
    snapshot = ResolvedSkillSnapshot(
        kind=AssetKind.SKILL,
        scope=version.scope,
        asset_id=version.skill_id,
        version_id=version.version_id,
        checksum=version.checksum,
        catalog_generation=7,
        dependency_version_ids=(),
        files=tuple(SkillArchiveFile(value.path, value.content, value.media_type) for value in version.files),
        secret_requirements=version.secret_requirements,
    )
    if schema_version == 3:
        encoded = encode_run_asset_snapshot(snapshot)
    elif schema_version == 2:
        encoded = {
            "schema_version": 2,
            "kind": "skill",
            "scope": version.scope.value,
            "asset_id": str(version.skill_id),
            "version_id": str(version.version_id),
            "checksum": version.checksum,
            "catalog_generation": 7,
            "dependency_version_ids": [],
            "skill": {
                "files": [
                    {
                        "path": value.path,
                        "media_type": value.media_type,
                        "content_base64": base64.b64encode(
                            value.content,
                        ).decode("ascii"),
                    }
                    for value in version.files
                ],
                "secret_requirements": [
                    {
                        "name": value.name,
                        "target_env": value.target_env,
                        "optional": value.optional,
                    }
                    for value in version.secret_requirements
                ],
            },
        }
    else:
        raise ValueError("unsupported legacy fixture schema")
    return _LegacyVersion(
        snapshot=snapshot,
        schema_version=schema_version,
        encoded=encoded,
    )


async def _seed_scope(session: AsyncSession) -> _Scope:
    scope = _Scope(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        thread_id="pinned-materializer-thread",
    )
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (
                   :user_id, :email, 'materializer_admin',
                   'system_admin', now(), false, 1
               )"""
        ),
        {
            "user_id": str(scope.user_id),
            "email": f"{scope.user_id.hex}@example.invalid",
        },
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (
                   :project_id, :slug, 'Materializer', :user_id
               )"""
        ),
        {
            "project_id": scope.project_id,
            "slug": f"materializer-{scope.project_id.hex[:12]}",
            "user_id": str(scope.user_id),
        },
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
            "id": scope.membership_id,
            "project_id": scope.project_id,
            "user_id": str(scope.user_id),
        },
    )
    await session.execute(
        text(
            """INSERT INTO agents (
                   id, scope, project_id, slug, display_name,
                   status, created_by_user_id
               ) VALUES (
                   :agent_id, 'project', :project_id, 'materializer-agent',
                   'Materializer Agent', 'active', :user_id
               )"""
        ),
        {
            "agent_id": scope.agent_id,
            "project_id": scope.project_id,
            "user_id": str(scope.user_id),
        },
    )
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
            "thread_id": scope.thread_id,
            "user_id": str(scope.user_id),
            "project_id": scope.project_id,
            "agent_id": scope.agent_id,
        },
    )
    return scope


async def _seed_version(
    session: AsyncSession,
    scope: _Scope,
    *,
    asset_scope: AssetScope,
    skill_id: uuid.UUID,
    version_number: int,
    files: tuple[_File, ...],
    create_skill: bool,
    slug: str,
    secret_requirements: (tuple[SkillSecretRequirementSnapshot, ...] | None) = None,
) -> _Version:
    version_id = uuid.uuid4()
    rows = tuple(
        (
            value.path,
            hashlib.sha256(value.content).hexdigest(),
            len(value.content),
        )
        for value in files
    )
    facts = skill_version_archive_facts(rows)
    secrets = (
        (
            SkillSecretRequirementSnapshot(
                name="API_TOKEN",
                target_env="PINNED_API_TOKEN",
                optional=False,
            ),
        )
        if secret_requirements is None
        else secret_requirements
    )
    if create_skill:
        await session.execute(
            text(
                """INSERT INTO skills (
                       id, scope, project_id, slug, display_name,
                       status, created_by_user_id
                   ) VALUES (
                       :skill_id, :scope, :project_id, :slug, :display_name,
                       'active', :user_id
                   )"""
            ),
            {
                "skill_id": skill_id,
                "scope": asset_scope.value,
                "project_id": (scope.project_id if asset_scope is AssetScope.PROJECT else None),
                "slug": slug,
                "display_name": f"{slug} display",
                "user_id": str(scope.user_id),
            },
        )
    await session.execute(
        text(
            """INSERT INTO skill_versions (
                   id, skill_id, version_number, secret_requirements,
                   scan_decision, payload_checksum, file_count,
                   content_size_bytes, files_sealed, created_by_user_id
               ) VALUES (
                   :version_id, :skill_id, :version_number,
                   CAST(:secret_requirements AS jsonb), 'allow', :checksum,
                   :file_count, :content_size, false, :user_id
               )"""
        ),
        {
            "version_id": version_id,
            "skill_id": skill_id,
            "version_number": version_number,
            "secret_requirements": json.dumps(
                [
                    {
                        "name": value.name,
                        "target_env": value.target_env,
                        "optional": value.optional,
                    }
                    for value in secrets
                ]
            ),
            "checksum": facts.payload_checksum,
            "file_count": facts.file_count,
            "content_size": facts.content_size_bytes,
            "user_id": str(scope.user_id),
        },
    )
    await session.execute(
        text("SELECT set_config('deerflow.asset_version_assembly', :version_id, true)"),
        {"version_id": str(version_id)},
    )
    await session.execute(
        text(
            """INSERT INTO skill_version_files (
                   skill_version_id, path, media_type, size_bytes,
                   sha256, content
               ) VALUES (
                   :version_id, :path, :media_type, :size_bytes,
                   :sha256, :content
               )"""
        ),
        [
            {
                "version_id": version_id,
                "path": value.path,
                "media_type": value.media_type,
                "size_bytes": len(value.content),
                "sha256": hashlib.sha256(value.content).hexdigest(),
                "content": value.content,
            }
            for value in files
        ],
    )
    await session.execute(
        text("UPDATE skill_versions SET files_sealed=true WHERE id=:version_id"),
        {"version_id": version_id},
    )
    return _Version(
        scope=asset_scope,
        skill_id=skill_id,
        version_id=version_id,
        checksum=facts.payload_checksum,
        files=files,
        secret_requirements=secrets,
    )


async def _seed_run(
    session: AsyncSession,
    scope: _Scope,
    *,
    run_id: str,
    versions: tuple[_Version | _LegacyVersion, ...],
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
                   :run_id, :thread_id, :owner_user_id, 'pending',
                   'reject', '{}'::json, '{}'::json, :trace_id,
                   0, 0, 0, 0, 0, 0, 0, 0, '{}'::json, now(), now(),
                   :project_id, 'pending', false
               )"""
        ),
        {
            "run_id": run_id,
            "thread_id": scope.thread_id,
            "owner_user_id": str(scope.user_id),
            "trace_id": f"trace-{run_id}",
            "project_id": scope.project_id,
        },
    )
    await session.execute(
        text("SELECT set_config('deerflow.run_asset_closure_assembly', :run_id, true)"),
        {"run_id": run_id},
    )
    agent_version_id = uuid.uuid4()
    payload = AgentPayload(
        description="Materializer Agent",
        soul="Materialize exact pinned Skills.",
        model_ref="test-model",
        tool_groups=(),
        skill_refs=tuple(
            SkillAssetRef(
                scope=version.scope,
                asset_id=version.skill_id,
            )
            for version in versions
        ),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
    agent_checksum = agent_payload_checksum(payload)
    agent_snapshot = ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=scope.agent_id,
        version_id=agent_version_id,
        checksum=agent_checksum,
        catalog_generation=7,
        dependency_version_ids=tuple(version.version_id for version in versions),
        payload=payload,
        skill_version_ids=tuple(version.version_id for version in versions),
        slug="materializer-agent",
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
            "project_id": scope.project_id,
            "owner_user_id": str(scope.user_id),
            "thread_id": scope.thread_id,
            "run_id": run_id,
            "asset_id": scope.agent_id,
            "version_id": agent_version_id,
            "checksum": agent_checksum,
            "snapshot": json.dumps(encode_run_asset_snapshot(agent_snapshot)),
        },
    )
    for dependency_order, version in enumerate(versions, start=1):
        if isinstance(version, _LegacyVersion):
            await session.execute(
                text(
                    """INSERT INTO run_asset_versions (
                           project_id, owner_user_id, thread_id, run_id,
                           asset_kind, dependency_order, asset_scope, asset_id,
                           version_id, payload_checksum, catalog_generation,
                           snapshot_schema_version, snapshot_json
                       ) VALUES (
                           :project_id, :owner_user_id, :thread_id, :run_id,
                           'skill', :dependency_order, :asset_scope, :asset_id,
                           :version_id, :checksum, 7, :schema_version,
                           CAST(:snapshot AS jsonb)
                       )"""
                ),
                {
                    "project_id": scope.project_id,
                    "owner_user_id": str(scope.user_id),
                    "thread_id": scope.thread_id,
                    "run_id": run_id,
                    "dependency_order": dependency_order,
                    "asset_scope": version.scope.value,
                    "asset_id": version.skill_id,
                    "version_id": version.version_id,
                    "checksum": version.checksum,
                    "schema_version": version.schema_version,
                    "snapshot": json.dumps(version.encoded),
                },
            )
            continue
        manifest = {
            "schema_version": 4,
            "kind": "skill",
            "scope": version.scope.value,
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
        await session.execute(
            text(
                """INSERT INTO run_asset_versions (
                       project_id, owner_user_id, thread_id, run_id,
                       asset_kind, dependency_order, asset_scope, asset_id,
                       version_id, payload_checksum, catalog_generation,
                       snapshot_schema_version, snapshot_json
                   ) VALUES (
                       :project_id, :owner_user_id, :thread_id, :run_id,
                       'skill', :dependency_order, :asset_scope, :asset_id,
                       :version_id, :checksum, 7, 4,
                       CAST(:snapshot AS jsonb)
                   )"""
            ),
            {
                "project_id": scope.project_id,
                "owner_user_id": str(scope.user_id),
                "thread_id": scope.thread_id,
                "run_id": run_id,
                "dependency_order": dependency_order,
                "asset_scope": version.scope.value,
                "asset_id": version.skill_id,
                "version_id": version.version_id,
                "checksum": version.checksum,
                "snapshot": json.dumps(manifest),
            },
        )
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
                       :skill_project_id, :skill_id, :version_id, :checksum,
                       :file_count, :content_size
                   )"""
            ),
            {
                "project_id": scope.project_id,
                "owner_user_id": str(scope.user_id),
                "thread_id": scope.thread_id,
                "run_id": run_id,
                "dependency_order": dependency_order,
                "asset_scope": version.scope.value,
                "skill_project_id": (scope.project_id if version.scope is AssetScope.PROJECT else None),
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


def _plan(
    scope: _Scope,
    *,
    run_id: str,
    identity: MaterializationAttemptIdentity,
    versions: tuple[_Version | _LegacyVersion, ...],
) -> RunSkillTreeMaterializationPlan:
    return RunSkillTreeMaterializationPlan(
        project_id=scope.project_id,
        owner_user_id=str(scope.user_id),
        thread_id=scope.thread_id,
        run_id=run_id,
        runtime_kind="chat",
        attempt_identity=identity,
        plan_fingerprint=hashlib.sha256(run_id.encode()).hexdigest(),
        skill_versions=tuple(
            LegacyInlineRunSkillPlan(
                dependency_order=dependency_order,
                scope=version.scope,
                asset_id=version.skill_id,
                version_id=version.version_id,
                payload_checksum=version.checksum,
                catalog_generation=7,
                snapshot_schema_version=version.schema_version,
                file_count=len(version.files),
                content_size_bytes=sum(len(value.content) for value in version.files),
                secret_requirements=version.secret_requirements,
            )
            if isinstance(version, _LegacyVersion)
            else PinnedSkillVersionPlan(
                dependency_order=dependency_order,
                scope=version.scope,
                asset_id=version.skill_id,
                version_id=version.version_id,
                payload_checksum=version.checksum,
                catalog_generation=7,
                dependency_version_ids=(),
                file_count=version.file_count,
                content_size_bytes=version.content_size_bytes,
                secret_requirements=version.secret_requirements,
            )
            for dependency_order, version in enumerate(versions, start=1)
        ),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_v2_reader_materializes_only_the_inline_run_snapshot(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
            legacy = _legacy_version(
                AssetScope.PROJECT,
                "legacy-v2-skill",
                "legacy-v2",
                schema_version=2,
            )
            await _seed_run(
                session,
                scope,
                run_id="materialize-legacy-v2",
                versions=(legacy,),
            )

        materializer = RunSkillTreeMaterializer(
            materialization_root=tmp_path / "legacy-v2-materializations",
            worker_config=WorkerConfig(),
            legacy_source_adapter=LegacyInlineRunSkillSourceAdapter(factory),
        )

        def capture_legacy_read(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)
            if "run_asset_versions.snapshot_json" in statement and "run_asset_versions.asset_kind" in statement:
                assert materializer._memory_budget.in_use_bytes == LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES
                assert materializer._memory_budget.v4_in_use_bytes == 0

        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            capture_legacy_read,
        )
        identity = MaterializationAttemptIdentity(
            job_id=uuid.uuid4(),
            attempt_id=uuid.uuid4(),
            worker_id=uuid.uuid4(),
        )
        plan = _plan(
            scope,
            run_id="materialize-legacy-v2",
            identity=identity,
            versions=(legacy,),
        )
        authority = _Authority(
            MaterializationAuthorityReadback(
                attempt_identity=identity,
                plan_fingerprint=plan.plan_fingerprint,
            )
        )

        pending = await materializer.materialize(
            plan=plan,
            authority=authority,  # type: ignore[arg-type]
        )

        assert authority.boundaries == [
            ("initial", None),
            ("version", 1),
            ("final", None),
        ]
        assert [value.name for value in pending.skills] == ["legacy-v2-skill"]
        assert [value.relative_root for value in pending.manifests] == [legacy.skill_id.hex]
        tree = pending.source.worker_root
        assert (tree / "custom" / legacy.skill_id.hex / "资料/说明-🦌.txt").read_bytes() == b"legacy-v2-unicode"
        legacy_reads = [statement for statement in statements if "run_asset_versions.snapshot_json" in statement and "run_asset_versions.asset_kind" in statement]
        assert len(legacy_reads) == 1
        assert "skill_versions" not in legacy_reads[0]
        assert "skills.current_version_id" not in legacy_reads[0]
        assert not any(statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for statement in statements)
        await pending.aclose()
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_mixed_v2_v3_v4_reader_preserves_dependency_order_and_fails_closed(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
            legacy_v2 = _legacy_version(
                AssetScope.PROJECT,
                "mixed-v2",
                "mixed-v2",
                schema_version=2,
            )
            legacy_v3 = _legacy_version(
                AssetScope.PROJECT,
                "mixed-v3",
                "mixed-v3",
                schema_version=3,
            )
            pinned_v4 = await _seed_version(
                session,
                scope,
                asset_scope=AssetScope.PROJECT,
                skill_id=uuid.uuid4(),
                version_number=1,
                files=_files(
                    "mixed-v4",
                    "mixed-v4",
                    count=2,
                    include_secret=False,
                ),
                create_skill=True,
                slug="mixed-v4",
                secret_requirements=(),
            )
            await _seed_run(
                session,
                scope,
                run_id="materialize-mixed-v2-v3-v4",
                versions=(legacy_v2, legacy_v3, pinned_v4),
            )

            corrupt_encoded = json.loads(json.dumps(legacy_v2.encoded))
            corrupt_encoded["skill"]["files"][0]["content_base64"] = "!!!!"
            corrupt_v2 = replace(legacy_v2, encoded=corrupt_encoded)
            await _seed_run(
                session,
                scope,
                run_id="materialize-corrupt-v2",
                versions=(corrupt_v2,),
            )

        root = tmp_path / "mixed-materializations"
        materializer = RunSkillTreeMaterializer(
            materialization_root=root,
            worker_config=WorkerConfig(
                materialization_batch_max_bytes=128,
                materialization_batch_max_files=2,
            ),
            legacy_source_adapter=LegacyInlineRunSkillSourceAdapter(factory),
            pinned_source_adapter=PinnedSkillVersionSourceAdapter(factory),
        )

        def capture_mixed_read(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)
            if "run_asset_versions.snapshot_json" in statement and "NOT (EXISTS" in statement and "skill_versions" not in statement:
                assert materializer._memory_budget.in_use_bytes == LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES
                assert materializer._memory_budget.v4_in_use_bytes == 0

        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            capture_mixed_read,
        )
        identity = MaterializationAttemptIdentity(
            job_id=uuid.uuid4(),
            attempt_id=uuid.uuid4(),
            worker_id=uuid.uuid4(),
        )
        plan = _plan(
            scope,
            run_id="materialize-mixed-v2-v3-v4",
            identity=identity,
            versions=(legacy_v2, legacy_v3, pinned_v4),
        )
        authority = _Authority(
            MaterializationAuthorityReadback(
                attempt_identity=identity,
                plan_fingerprint=plan.plan_fingerprint,
            )
        )

        pending = await materializer.materialize(
            plan=plan,
            authority=authority,  # type: ignore[arg-type]
        )

        assert [value.name for value in pending.skills] == [
            "mixed-v2",
            "mixed-v3",
            "mixed-v4",
        ]
        assert [value.version_id for value in pending.manifests] == [
            legacy_v2.version_id,
            legacy_v3.version_id,
            pinned_v4.version_id,
        ]
        assert (pending.source.worker_root / "custom" / legacy_v3.skill_id.hex / "资料/说明-🦌.txt").read_bytes() == b"mixed-v3-unicode"
        legacy_reads = [statement for statement in statements if "run_asset_versions.snapshot_json" in statement and "run_asset_versions.asset_kind" in statement and "skill_versions" not in statement]
        assert len(legacy_reads) == 2
        assert all("current_version_id" not in statement for statement in statements)
        assert not any(statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for statement in statements)
        await pending.aclose()
        assert not any(root.iterdir())

        corrupt_plan = _plan(
            scope,
            run_id="materialize-corrupt-v2",
            identity=identity,
            versions=(corrupt_v2,),
        )
        corrupt_authority = _Authority(
            MaterializationAuthorityReadback(
                attempt_identity=identity,
                plan_fingerprint=corrupt_plan.plan_fingerprint,
            )
        )
        with pytest.raises(
            RunSkillTreeMaterializationStale,
            match="snapshot is invalid",
        ):
            await materializer.materialize(
                plan=corrupt_plan,
                authority=corrupt_authority,  # type: ignore[arg-type]
            )
        assert not any(root.iterdir())
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pinned_v4_reader_materializes_bounded_exact_versions_and_fails_closed(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []
    materializer_holder: dict[str, RunSkillTreeMaterializer] = {}

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)
        if "skill_version_files.content" in statement:
            assert materializer_holder["materializer"]._memory_budget.in_use_bytes > 0
            assert materializer_holder["materializer"]._memory_budget.v4_in_use_bytes > 0

    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
            project_skill_id = uuid.uuid4()
            first = await _seed_version(
                session,
                scope,
                asset_scope=AssetScope.PROJECT,
                skill_id=project_skill_id,
                version_number=1,
                files=_files("pinned-skill", "first", count=121),
                create_skill=True,
                slug="pinned-skill",
            )
            historical = await _seed_version(
                session,
                scope,
                asset_scope=AssetScope.PROJECT,
                skill_id=project_skill_id,
                version_number=2,
                files=_files("pinned-skill", "historical", count=2),
                create_skill=False,
                slug="pinned-skill",
            )
            public = await _seed_version(
                session,
                scope,
                asset_scope=AssetScope.SYSTEM,
                skill_id=uuid.uuid4(),
                version_number=1,
                files=_files("public-helper", "public", count=1),
                create_skill=True,
                slug="public-helper",
            )
            conflict = await _seed_version(
                session,
                scope,
                asset_scope=AssetScope.PROJECT,
                skill_id=uuid.uuid4(),
                version_number=1,
                files=_files("pinned-skill", "conflict", count=1),
                create_skill=True,
                slug="different-database-slug",
            )
            await _seed_run(
                session,
                scope,
                run_id="materialize-success",
                versions=(first, historical, public),
            )
            await _seed_run(
                session,
                scope,
                run_id="materialize-name-conflict",
                versions=(historical, conflict),
            )

        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            capture_statement,
        )
        root = tmp_path / "materializations"
        materializer = RunSkillTreeMaterializer(
            materialization_root=root,
            worker_config=WorkerConfig(
                materialization_batch_max_bytes=256,
                materialization_batch_max_files=7,
            ),
            pinned_source_adapter=PinnedSkillVersionSourceAdapter(factory),
        )
        materializer_holder["materializer"] = materializer
        identity = MaterializationAttemptIdentity(
            job_id=uuid.uuid4(),
            attempt_id=uuid.uuid4(),
            worker_id=uuid.uuid4(),
        )
        plan = _plan(
            scope,
            run_id="materialize-success",
            identity=identity,
            versions=(first, historical, public),
        )
        authority = _Authority(
            MaterializationAuthorityReadback(
                attempt_identity=identity,
                plan_fingerprint=plan.plan_fingerprint,
            )
        )

        pending = await materializer.materialize(
            plan=plan,
            authority=authority,  # type: ignore[arg-type]
        )

        assert authority.boundaries == [
            ("initial", None),
            ("version", 1),
            ("version", 2),
            ("version", 3),
            ("final", None),
        ]
        assert [value.relative_root for value in pending.manifests] == [
            project_skill_id.hex,
            f".versions/{project_skill_id.hex}/{historical.version_id.hex}",
            "public-helper",
        ]
        assert [value.category for value in pending.skills] == [
            SkillCategory.CUSTOM,
            SkillCategory.CUSTOM,
            SkillCategory.PUBLIC,
        ]
        tree = pending.source.worker_root
        assert (tree / "custom" / project_skill_id.hex / "资料/说明-🦌.txt").read_bytes() == b"first-unicode"
        assert (tree / "custom" / ".versions" / project_skill_id.hex / historical.version_id.hex / "SKILL.md").is_file()
        assert (tree / "public" / "public-helper" / "SKILL.md").is_file()
        assert all(value.skill_file.is_file() for value in pending.skills)

        content_queries = [statement for statement in statements if "skill_version_files.content" in statement]
        assert len(content_queries) > 3
        assert all('COLLATE "C"' in statement and " >= " in statement and " <= " in statement for statement in content_queries)
        metadata_queries = [statement for statement in statements if "skill_version_files.media_type" in statement]
        assert len(metadata_queries) == 3
        assert all("skill_version_files.content" not in statement for statement in metadata_queries)
        parent_queries = [statement for statement in statements if "FROM run_asset_versions" in statement]
        assert len(parent_queries) == 3
        assert all("skill_version_files.content" not in statement for statement in parent_queries)
        assert materializer._memory_budget.peak_v4_in_use_bytes <= materializer._memory_budget.v4_capacity_bytes
        await pending.aclose()
        assert not any(root.iterdir())

        drifting_authority = _Authority(
            MaterializationAuthorityReadback(
                attempt_identity=identity,
                plan_fingerprint=plan.plan_fingerprint,
            ),
            drift_at=("final", None),
        )
        with pytest.raises(
            RunSkillTreeMaterializationStale,
            match="authority changed",
        ):
            await materializer.materialize(
                plan=plan,
                authority=drifting_authority,  # type: ignore[arg-type]
            )
        assert drifting_authority.boundaries[-1] == ("final", None)
        assert not any(root.iterdir())

        conflict_plan = _plan(
            scope,
            run_id="materialize-name-conflict",
            identity=identity,
            versions=(historical, conflict),
        )
        conflict_authority = _Authority(
            MaterializationAuthorityReadback(
                attempt_identity=identity,
                plan_fingerprint=conflict_plan.plan_fingerprint,
            )
        )
        with pytest.raises(
            RunSkillTreeMaterializationStale,
            match="runtime name conflicts",
        ):
            await materializer.materialize(
                plan=conflict_plan,
                authority=conflict_authority,  # type: ignore[arg-type]
            )
        assert not any(root.iterdir())

        wrong_secrets = replace(
            plan,
            skill_versions=(
                replace(plan.skill_versions[0], secret_requirements=()),
                *plan.skill_versions[1:],
            ),
        )
        wrong_authority = _Authority(
            MaterializationAuthorityReadback(
                attempt_identity=identity,
                plan_fingerprint=wrong_secrets.plan_fingerprint,
            )
        )
        with pytest.raises(
            RunSkillTreeMaterializationStale,
            match="metadata changed",
        ):
            await materializer.materialize(
                plan=wrong_secrets,
                authority=wrong_authority,  # type: ignore[arg-type]
            )
        assert not any(root.iterdir())

        event.remove(
            engine.sync_engine,
            "before_cursor_execute",
            capture_statement,
        )
        async with factory() as session, session.begin():
            await session.execute(text("ALTER TABLE skill_version_files DISABLE TRIGGER trg_skill_version_files_immutable"))
            await session.execute(
                text(
                    """UPDATE skill_version_files
                       SET content=set_byte(content, 0, 33)
                       WHERE skill_version_id=:version_id
                         AND path='SKILL.md'"""
                ),
                {"version_id": first.version_id},
            )
            await session.execute(text("ALTER TABLE skill_version_files ENABLE TRIGGER trg_skill_version_files_immutable"))
        tampered_authority = _Authority(
            MaterializationAuthorityReadback(
                attempt_identity=identity,
                plan_fingerprint=plan.plan_fingerprint,
            )
        )
        with pytest.raises(
            RunSkillTreeMaterializationStale,
            match="content changed",
        ):
            await materializer.materialize(
                plan=plan,
                authority=tampered_authority,  # type: ignore[arg-type]
            )
        assert not any(root.iterdir())
    finally:
        if event.contains(
            engine.sync_engine,
            "before_cursor_execute",
            capture_statement,
        ):
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                capture_statement,
            )
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_private_asset_runtime_materializes_mixed_legacy_and_pinned_sources(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []
    root = tmp_path / "asset-runtime-mixed-materializations"
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
            version_v2 = await _seed_version(
                session,
                scope,
                asset_scope=AssetScope.PROJECT,
                skill_id=uuid.uuid4(),
                version_number=1,
                files=_files(
                    "runtime-v2",
                    "runtime-v2",
                    count=1,
                    include_secret=False,
                ),
                create_skill=True,
                slug="runtime-v2",
                secret_requirements=(),
            )
            version_v3 = await _seed_version(
                session,
                scope,
                asset_scope=AssetScope.PROJECT,
                skill_id=uuid.uuid4(),
                version_number=1,
                files=_files(
                    "runtime-v3",
                    "runtime-v3",
                    count=1,
                    include_secret=False,
                ),
                create_skill=True,
                slug="runtime-v3",
                secret_requirements=(),
            )
            version_v4 = await _seed_version(
                session,
                scope,
                asset_scope=AssetScope.PROJECT,
                skill_id=uuid.uuid4(),
                version_number=1,
                files=_files(
                    "runtime-v4",
                    "runtime-v4",
                    count=1,
                    include_secret=False,
                ),
                create_skill=True,
                slug="runtime-v4",
                secret_requirements=(),
            )
            legacy_v2 = _legacy_from_version(version_v2, schema_version=2)
            legacy_v3 = _legacy_from_version(version_v3, schema_version=3)
            await _seed_run(
                session,
                scope,
                run_id="asset-runtime-mixed",
                versions=(legacy_v2, legacy_v3, version_v4),
            )
            corrupt_encoded = json.loads(json.dumps(legacy_v3.encoded))
            corrupt_encoded["skill"]["archive_base64"] = "!!!!"
            corrupt_v3 = replace(legacy_v3, encoded=corrupt_encoded)
            await _seed_run(
                session,
                scope,
                run_id="asset-runtime-corrupt-v3",
                versions=(corrupt_v3,),
            )

        async with factory() as session, session.begin():
            persisted_project = await resolve_project_context_in_transaction(
                session,
                scope.user_id,
                scope.project_id,
                "asset-runtime-mixed",
            )
            persisted_attempt_ids = (
                await session.execute(
                    text(
                        """SELECT p.id AS job_id, m.id AS attempt_id
                           FROM projects p
                           JOIN project_memberships m ON m.project_id=p.id
                           WHERE p.id=:project_id
                             AND m.id=:membership_id"""
                    ),
                    {
                        "project_id": scope.project_id,
                        "membership_id": scope.membership_id,
                    },
                )
            ).one()

        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement),
        )
        materializer = RunSkillTreeMaterializer(
            materialization_root=root,
            worker_config=WorkerConfig(
                materialization_batch_max_bytes=128,
                materialization_batch_max_files=2,
            ),
            legacy_source_adapter=LegacyInlineRunSkillSourceAdapter(factory),
            pinned_source_adapter=PinnedSkillVersionSourceAdapter(factory),
        )
        asset_runtime = PrivateAssetRuntime(
            factory,
            skill_tree_materializer=materializer,
            run_session_reuse=False,
        )
        context = PrivateWorkContext.from_project(persisted_project)
        boundary = _ExecutionBoundary()
        boundary.execution_job_id = persisted_attempt_ids.job_id
        boundary.attempt_id = persisted_attempt_ids.attempt_id

        runtime = await asset_runtime.materialize(
            context,
            _admitted(scope, boundary, run_id="asset-runtime-mixed"),
            authorization_boundary=boundary,
        )

        assert [value.name for value in runtime.skills] == [
            "runtime-v2",
            "runtime-v3",
            "runtime-v4",
        ]
        assert boundary.snapshot_checks == 0
        assert boundary.materialization_checks == 6
        assert all("current_version_id" not in statement for statement in statements)
        legacy_reads = [statement for statement in statements if "run_asset_versions.snapshot_json" in statement and "NOT (EXISTS" in statement and "skill_versions" not in statement]
        assert len(legacy_reads) == 2
        legacy_control_reads = [
            statement for statement in statements if "skill_versions.secret_requirements" in statement and "NOT (EXISTS" in statement and "run_asset_versions.snapshot_json" not in statement and "skill_version_files.content" not in statement
        ]
        assert len(legacy_control_reads) == 2
        owner_root = runtime.skill_root.parent
        owner_id = uuid.UUID(hex=owner_root.name)
        await runtime.aclose(NotAcquired(owner_id=owner_id))
        assert not owner_root.exists()

        corrupt_boundary = _ExecutionBoundary()
        with pytest.raises(PrivateWorkAssetStale):
            await asset_runtime.materialize(
                context,
                _admitted(
                    scope,
                    corrupt_boundary,
                    run_id="asset-runtime-corrupt-v3",
                ),
                authorization_boundary=corrupt_boundary,
            )
        assert not any(root.iterdir())
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_private_asset_runtime_transfers_v4_tree_and_cleans_each_failure_owner(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "asset-runtime-materializations"
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
            version = await _seed_version(
                session,
                scope,
                asset_scope=AssetScope.PROJECT,
                skill_id=uuid.uuid4(),
                version_number=1,
                files=_files(
                    "runtime-owned-skill",
                    "runtime",
                    count=4,
                    include_secret=False,
                ),
                create_skill=True,
                slug="runtime-owned-skill",
                secret_requirements=(),
            )
            await _seed_run(
                session,
                scope,
                run_id="asset-runtime-v4",
                versions=(version,),
            )

        async with factory() as session, session.begin():
            persisted_project = await resolve_project_context_in_transaction(
                session,
                scope.user_id,
                scope.project_id,
                "asset-runtime-v4",
            )
            persisted_attempt_ids = (
                await session.execute(
                    text(
                        """SELECT p.id AS job_id, m.id AS attempt_id
                           FROM projects p
                           JOIN project_memberships m ON m.project_id=p.id
                           WHERE p.id=:project_id
                             AND m.id=:membership_id"""
                    ),
                    {
                        "project_id": scope.project_id,
                        "membership_id": scope.membership_id,
                    },
                )
            ).one()

        materializer = RunSkillTreeMaterializer(
            materialization_root=root,
            worker_config=WorkerConfig(
                materialization_batch_max_bytes=128,
                materialization_batch_max_files=2,
            ),
            pinned_source_adapter=PinnedSkillVersionSourceAdapter(factory),
        )
        asset_runtime = PrivateAssetRuntime(
            factory,
            skill_tree_materializer=materializer,
            run_session_reuse=False,
        )
        context = PrivateWorkContext.from_project(persisted_project)

        boundary = _ExecutionBoundary()
        # Exercise the exact asyncpg UUID objects that cross the Worker claim
        # and Project-context persistence boundaries in production.
        boundary.execution_job_id = persisted_attempt_ids.job_id
        boundary.attempt_id = persisted_attempt_ids.attempt_id
        runtime = await asset_runtime.materialize(
            context,
            _admitted(scope, boundary, run_id="asset-runtime-v4"),
            authorization_boundary=boundary,
        )

        owner_root = runtime.skill_root.parent
        owner_id = uuid.UUID(hex=owner_root.name)
        assert runtime.skill_root == owner_root / "tree"
        assert runtime.skill_mount_source.worker_root == runtime.skill_root
        assert runtime.skills[0].skill_file.is_file()
        assert boundary.snapshot_checks == 0
        assert boundary.materialization_checks == 4

        await runtime.aclose()
        await runtime.aclose()
        assert owner_root.exists()
        await runtime.aclose(NotAcquired(owner_id=owner_id))
        await runtime.aclose(NotAcquired(owner_id=owner_id))
        assert not owner_root.exists()

        original_runtime = asset_runtime_module.PrivateAgentRuntime

        def reject_construction(**_kwargs: object) -> object:
            raise RuntimeError("constructor failed")

        with monkeypatch.context() as patcher:
            patcher.setattr(
                asset_runtime_module,
                "PrivateAgentRuntime",
                reject_construction,
            )
            construction_boundary = _ExecutionBoundary()
            with pytest.raises(PrivateWorkUnavailable):
                await asset_runtime.materialize(
                    context,
                    _admitted(
                        scope,
                        construction_boundary,
                        run_id="asset-runtime-v4",
                    ),
                    authorization_boundary=construction_boundary,
                )
        assert not any(root.iterdir())

        async def reject_discovery(self: object) -> None:
            del self
            raise RuntimeError("discovery failed")

        with monkeypatch.context() as patcher:
            patcher.setattr(
                original_runtime,
                "discover_mcp_tools",
                reject_discovery,
            )
            failure_boundary = _ExecutionBoundary()
            with pytest.raises(PrivateWorkUnavailable):
                await asset_runtime.materialize(
                    context,
                    _admitted(
                        scope,
                        failure_boundary,
                        run_id="asset-runtime-v4",
                    ),
                    authorization_boundary=failure_boundary,
                )
        assert not any(root.iterdir())

        discovery_entered = asyncio.Event()

        async def block_discovery(self: object) -> None:
            del self
            discovery_entered.set()
            await asyncio.Future()

        with monkeypatch.context() as patcher:
            patcher.setattr(
                original_runtime,
                "discover_mcp_tools",
                block_discovery,
            )
            cancel_boundary = _ExecutionBoundary()
            task = asyncio.create_task(
                asset_runtime.materialize(
                    context,
                    _admitted(
                        scope,
                        cancel_boundary,
                        run_id="asset-runtime-v4",
                    ),
                    authorization_boundary=cancel_boundary,
                )
            )
            await discovery_entered.wait()
            assert any(root.iterdir())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not any(root.iterdir())
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admission_worker_begin_and_approval_metadata_reads_do_not_detoast_legacy_skill(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = "metadata-only-large-legacy"
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    def selects_run_asset_snapshot_json(statement: str) -> bool:
        normalized = " ".join(statement.lower().split())
        marker = " from run_asset_versions"
        marker_index = normalized.find(marker)
        return marker_index >= 0 and normalized.startswith("select ") and "snapshot_json" in normalized[:marker_index]

    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_scope(session)
            legacy = _large_legacy_v2_version()
            await _seed_run(
                session,
                scope,
                run_id=run_id,
                versions=(legacy,),
            )
            job = await PrivateRunJobRepository(session).enqueue(
                scope=JobScope(scope.project_id, str(scope.user_id)),
                run_id=run_id,
                origin_trace_id=f"trace-{run_id}",
                account_private_generation=AccountPrivateGeneration(
                    owner_user_id=str(scope.user_id),
                    generation=1,
                ),
            )
            await PrivateRunRepository(session).attach_job(
                scope=_private_context(scope).resource_scope,
                run_id=run_id,
                job_id=job.job_id,
            )
            worker_id = uuid.uuid4()
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="metadata-only-snapshot-test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                ),
            )

        async with factory() as session:
            stored_bytes, logical_bytes = (
                await session.execute(
                    text(
                        """SELECT pg_column_size(snapshot_json),
                                          octet_length(snapshot_json::text)
                                   FROM run_asset_versions
                                   WHERE run_id=:run_id
                                     AND asset_kind='skill'""",
                    ),
                    {"run_id": run_id},
                )
            ).one()
        assert logical_bytes > 2 * 1024 * 1024
        assert stored_bytes < logical_bytes

        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            capture_statement,
        )
        context = _private_context(scope)
        admission = PrivateRunAdmissionService(factory)
        async with factory() as session, session.begin():
            persisted = await admission._persisted_snapshot(
                session,
                context,
                run_id,
                thread_id=scope.thread_id,
            )
        admission_statements = tuple(statements)

        async with factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
            assert claim is not None
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )

        begin_start = len(statements)
        handler = PrivateRunJobHandler(
            factory,
            executor=object(),  # type: ignore[arg-type]
        )
        execution, _cancel_requested, recovered, _scope = await handler._begin(
            claim,
        )
        begin_statements = tuple(statements[begin_start:])
        assert execution is not None
        assert recovered is None

        approval_start = len(statements)
        async with factory() as session:
            closure = await _asset_closure(
                session,
                project_id=scope.project_id,
                owner_user_id=str(scope.user_id),
                run_id=run_id,
            )
        approval_statements = tuple(statements[approval_start:])

        assert closure[0]
        assert persisted.assets
        assert execution.snapshot.assets
        assert not any(selects_run_asset_snapshot_json(statement) for statement in admission_statements)
        assert not any(selects_run_asset_snapshot_json(statement) for statement in begin_statements)
        assert not any(selects_run_asset_snapshot_json(statement) for statement in approval_statements)
        assert all(type(asset) is ResolvedRunAssetFact for asset in persisted.assets)
        assert all(type(asset) is ResolvedRunAssetFact for asset in execution.snapshot.assets)
    finally:
        if event.contains(
            engine.sync_engine,
            "before_cursor_execute",
            capture_statement,
        ):
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                capture_statement,
            )
        await engine.dispose()
