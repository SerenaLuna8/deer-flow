"""Real-PostgreSQL gates for Skill Builder revision sessions."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError
from support.private_thread_seed import PrivateThreadSeed, seed_private_thread_database
from support.system_model_seed import seed_system_model_config

from app.private_work.run_repository import PrivateRunRepository
from app.projects.context import ProjectContext
from app.shared_assets.bootstrap import service as bootstrap_service
from app.shared_assets.errors import (
    AssetConflict,
    AssetNotFound,
    SkillDesignNoChanges,
    SkillDesignTargetDeleted,
    SkillDesignTargetSessionExists,
    SkillDesignTargetUnsupported,
)
from app.shared_assets.skill_builder_contract import SkillBuilderCandidateFileList
from app.shared_assets.skill_builder_run_admission import SkillBuilderRunAdmissionService
from app.shared_assets.skill_design_activity import SkillDesignActivityKind
from app.shared_assets.skill_design_repository import SkillDesignRepository
from app.shared_assets.skill_design_service import (
    CommitSkillDesignSession,
    CreateSkillDesignRevisionSession,
    CreateSkillDesignSession,
    SkillDesignDraftUpdateTurn,
    SkillDesignMessageTurn,
    SkillDesignService,
    SkillDesignStatus,
    SubmitSkillDesignTurn,
    ValidateSkillDesignSession,
)
from app.shared_assets.skill_repository import SkillRepository
from app.shared_assets.skill_service import (
    SkillArchiveFile,
    SkillFileChange,
    SkillService,
)
from app.system_runtime_settings import SystemRuntimePolicyService
from app.system_settings import SystemModelCatalogService
from app.system_settings.bootstrap import DEEPSEEK_V4_FLASH_VISION_EXP_MODEL_ID
from deerflow.persistence.jobs.model import WorkerNodeRow
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.shared_assets import (
    SkillDesignDraftFileRow,
    SkillDesignOperationRow,
    SkillDesignSessionRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked


class _SkillQuota:
    def __init__(self) -> None:
        self.reserved: list[uuid.UUID] = []
        self.released: list[uuid.UUID] = []

    async def reserve_skill_version(self, _session, *, project_id, version_id, size) -> None:
        assert project_id and size >= 0
        self.reserved.append(version_id)

    async def release_skill_version_if_reserved(
        self,
        _session,
        project_id,
        *,
        version_id,
        size,
    ) -> bool:
        assert project_id and size >= 0
        self.released.append(version_id)
        return True

    async def reconcile_project_storage(self, _session, project_id) -> None:
        assert project_id


class _SimulatedCommitCrash(BaseException):
    pass


class _RecordingRunQuota:
    async def reserve_concurrent_run(self, _session, _context, _run) -> None:
        return None

    async def release_concurrent_run(self, _session, _scope, *, run_id: str, request_id: str) -> None:
        assert run_id and request_id


class _RecordingRunAudit:
    async def run_admitted(self, _session, _context, _run, _job) -> None:
        return None

    async def run_cancel_requested(self, _session, _context, *, run_id: str, job_id: uuid.UUID) -> None:
        assert run_id and isinstance(job_id, uuid.UUID)

    async def run_terminal(
        self,
        _session,
        _scope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None:
        assert run_id and job_type == "private_run" and request_id
        assert isinstance(job_id, uuid.UUID)
        assert public_error_code is None or public_error_code.isupper()


def _project_context(seed: PrivateThreadSeed, *, request_id: str) -> ProjectContext:
    source = seed.owner_a
    return ProjectContext(
        user_id=source.user_id,
        project_id=source.project_id,
        membership_id=source.membership_id,
        role=source.role,
        capabilities=source.capabilities,
        membership_version=source.membership_version,
        request_id=request_id,
    )


def _skill_md(slug: str, body: str) -> SkillArchiveFile:
    content = (f"---\nname: {slug}\ndescription: Describe when and how to use this skill.\n---\n\n# {slug}\n\n{body}").encode()
    return SkillArchiveFile("SKILL.md", content, "text/markdown")


def _template_body() -> str:
    return "Add instructions for this skill here.\n"


async def _environment(database_url: str, *, with_model: bool = False):
    seed = await seed_private_thread_database(database_url)
    await bootstrap_service.bootstrap_system_assets(seed.factory)
    if with_model:
        await _seed_default_model(seed)
    context = _project_context(seed, request_id="a" * 32)
    quota = _SkillQuota()
    run_quota = _RecordingRunQuota()
    run_audit = _RecordingRunAudit()
    skills = SkillService(seed.factory, quota=quota)
    admission = SkillBuilderRunAdmissionService(
        seed.factory,
        model_catalog=SystemModelCatalogService(seed.factory),
        runtime_policy=SystemRuntimePolicyService,
        quota=run_quota,
        audit=run_audit,
    )
    design = SkillDesignService(
        seed.factory,
        skill_service=skills,
        run_admission=admission,
        quota=run_quota,
        audit=run_audit,
    )
    return seed, context, skills, design, quota


async def _seed_default_model(seed: PrivateThreadSeed) -> None:
    model_id = uuid.uuid4()
    provider_model = f"builder-rev-{model_id.hex}"
    async with seed.engine.begin() as connection:
        await seed_system_model_config(
            connection,
            model_id=model_id,
            owner_user_id=str(seed.owner_a.user_id),
            display_name="Builder revision model",
            provider_model=provider_model,
        )
        await seed_system_model_config(
            connection,
            model_id=DEEPSEEK_V4_FLASH_VISION_EXP_MODEL_ID,
            owner_user_id=str(seed.owner_a.user_id),
            display_name="Builder revision vision model",
            provider_model="builder-revision-vision",
            supports_vision=True,
        )
        await connection.execute(
            sa.text(
                """UPDATE system_model_catalog_state
                   SET default_model_config_id=:model, revision=revision+1,
                       updated_by_user_id=:owner
                 WHERE id=1"""
            ),
            {"model": model_id, "owner": str(seed.owner_a.user_id)},
        )


async def _create_current_template(skills: SkillService, context: ProjectContext, slug: str):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            "SKILL.md",
            _skill_md(slug, _template_body()).content,
        )
    created = await skills.create_project_from_archive_upload(
        context,
        archive.getvalue(),
        filename=f"{slug}.zip",
    )
    current = await skills.activate_version(
        context,
        created.asset.id,
        created.version.id,
        expected_asset_version=created.asset.revision,
        expected_payload_checksum=created.version.payload_checksum,
        expected_secret_revision=0,
    )
    return await skills.get(context, created.asset.id), current


async def _open_revision(design: SkillDesignService, context: ProjectContext, skill_id: uuid.UUID, key: str):
    return await design.create_revision(
        context,
        CreateSkillDesignRevisionSession(skill_id=skill_id, idempotency_key=key),
    )


def _replace_skill_md(content: str, *, media_type: str | None = "text/markdown") -> SkillFileChange:
    return SkillFileChange("replace", "SKILL.md", content, media_type)


async def _edit_draft(
    design: SkillDesignService,
    context: ProjectContext,
    session,
    content: str,
    *,
    key: str,
    media_type: str | None = "text/markdown",
):
    assert session.draft_checksum is not None
    return await design.submit_turn(
        context,
        session.id,
        SubmitSkillDesignTurn(
            input=SkillDesignDraftUpdateTurn(
                kind="draft_update",
                expected_draft_checksum=session.draft_checksum,
                changes=(_replace_skill_md(content, media_type=media_type),),
            ),
            expected_revision=session.revision,
            idempotency_key=key,
        ),
    )


async def _validate_session(design: SkillDesignService, context: ProjectContext, session, key: str):
    assert session.draft_checksum is not None
    return await design.validate(
        context,
        session.id,
        ValidateSkillDesignSession(
            expected_revision=session.revision,
            expected_draft_checksum=session.draft_checksum,
            idempotency_key=key,
        ),
    )


async def _seed_create_draft(
    seed: PrivateThreadSeed,
    context: ProjectContext,
    skills: SkillService,
    design: SkillDesignService,
    session_id: uuid.UUID,
    files: tuple[SkillArchiveFile, ...],
):
    preview = await skills.preview_archive(context, files)
    async with seed.factory() as session, session.begin():
        repository = SkillDesignRepository(session)
        row = await repository.get(context, session_id, for_update=True)
        assert row.session_kind == "create"
        assert row.status == SkillDesignStatus.INTERVIEWING.value
        await repository.replace_draft_files(context, row.id, files)
        row.status = SkillDesignStatus.DRAFT_READY.value
        row.draft_checksum = preview.checksum
        row.progress_json = [
            {"id": "interview", "label": "确认需求", "status": "completed"},
            {"id": "package", "label": "生成候选文件", "status": "completed"},
            {"id": "validate", "label": "检查 Skill", "status": "pending"},
        ]
        row.revision += 1
    return await design.get(context, session_id), preview


async def _commit_session(
    design: SkillDesignService,
    context: ProjectContext,
    session,
    key: str,
):
    assert session.draft_checksum is not None
    return await design.commit(
        context,
        session.id,
        CommitSkillDesignSession(
            expected_revision=session.revision,
            expected_draft_checksum=session.draft_checksum,
            idempotency_key=key,
        ),
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_validate_records_package_and_terminal_stages(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, design, _quota = await _environment(
        migrated_postgres_database_url,
    )
    try:
        opened = await design.create(
            context,
            CreateSkillDesignSession(
                slug="validation-activity",
                display_name="Validation Activity",
                idempotency_key="create-validation-activity",
            ),
        )
        drafted, _preview = await _seed_create_draft(
            seed,
            context,
            skills,
            design,
            opened.id,
            (_skill_md("validation-activity", "Validate every stage."),),
        )
        validated = await _validate_session(
            design,
            context,
            drafted,
            "validate-stage-activity",
        )

        assert validated.status is SkillDesignStatus.VALIDATED
        activities = await design.list_activities(context, opened.id)
        assert [(item.kind, item.payload) for item in activities] == [
            (SkillDesignActivityKind.REQUEST_ACCEPTED, {}),
            (
                SkillDesignActivityKind.VALIDATION_STARTED,
                {"stage": "package_files"},
            ),
            (SkillDesignActivityKind.VALIDATION_PASSED, {}),
            (
                SkillDesignActivityKind.RUN_TERMINAL,
                {"status": "completed"},
            ),
        ]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_validation_unexpected_failure_has_durable_terminal(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, context, skills, design, _quota = await _environment(
        migrated_postgres_database_url,
    )
    try:
        opened = await design.create(
            context,
            CreateSkillDesignSession(
                slug="validation-unexpected-failure",
                display_name="Validation Unexpected Failure",
                idempotency_key="create-validation-unexpected-failure",
            ),
        )
        drafted, _preview = await _seed_create_draft(
            seed,
            context,
            skills,
            design,
            opened.id,
            (
                _skill_md(
                    "validation-unexpected-failure",
                    "Record a safe validation terminal.",
                ),
            ),
        )

        async def fail_validation(*_args, **_kwargs):
            raise RuntimeError("unexpected validation failure")

        monkeypatch.setattr(skills, "preview_archive", fail_validation)
        with pytest.raises(RuntimeError, match="unexpected validation failure"):
            await _validate_session(
                design,
                context,
                drafted,
                "validate-unexpected-failure",
            )

        current = await design.get(context, opened.id)
        assert current.status is SkillDesignStatus.DRAFT_READY
        activities = await design.list_activities(context, opened.id)
        assert [(item.kind, item.payload) for item in activities] == [
            (SkillDesignActivityKind.REQUEST_ACCEPTED, {}),
            (
                SkillDesignActivityKind.VALIDATION_STARTED,
                {"stage": "package_files"},
            ),
            (SkillDesignActivityKind.VALIDATION_FAILED, {}),
            (
                SkillDesignActivityKind.RUN_TERMINAL,
                {
                    "status": "failed",
                    "code": "asset_storage_unavailable",
                },
            ),
        ]
        async with seed.factory() as session:
            operation = (
                await session.execute(
                    sa.select(SkillDesignOperationRow).where(
                        SkillDesignOperationRow.session_id == opened.id,
                        SkillDesignOperationRow.operation_kind == "validate",
                    )
                )
            ).scalar_one()
            assert operation.status == "failed"
            assert operation.public_error_code == "asset_storage_unavailable"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("crash_stage", ["validation", "persistence"])
async def test_stale_commit_is_recovered_with_failed_terminal(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    crash_stage: str,
) -> None:
    seed, context, skills, design, _quota = await _environment(
        migrated_postgres_database_url,
    )
    try:
        slug = f"stale-commit-{crash_stage}"
        opened = await design.create(
            context,
            CreateSkillDesignSession(
                slug=slug,
                display_name=f"Stale Commit {crash_stage}",
                idempotency_key=f"create-{slug}",
            ),
        )
        drafted, _preview = await _seed_create_draft(
            seed,
            context,
            skills,
            design,
            opened.id,
            (_skill_md(slug, "Recover an interrupted Commit."),),
        )
        validated = await _validate_session(
            design,
            context,
            drafted,
            f"validate-{slug}",
        )

        async def crash(*_args, **_kwargs):
            raise _SimulatedCommitCrash

        if crash_stage == "validation":
            monkeypatch.setattr(skills, "preview_archive", crash)
        else:
            monkeypatch.setattr(
                skills,
                "create_project_from_preview_in_session",
                crash,
            )
        with pytest.raises(_SimulatedCommitCrash):
            await _commit_session(
                design,
                context,
                validated,
                f"commit-{slug}",
            )

        recovery_design = SkillDesignService(
            seed.factory,
            skill_service=skills,
            stale_generating_seconds=0.001,
        )
        await asyncio.sleep(0.01)
        if crash_stage == "validation":
            summaries = await recovery_design.list_incomplete(context)
            assert len(summaries) == 1
            assert summaries[0].status is SkillDesignStatus.VALIDATED
        recovered = await recovery_design.get(context, opened.id)
        assert recovered.status is SkillDesignStatus.VALIDATED
        activities = await recovery_design.list_activities(context, opened.id)
        commit_activities = [item for item in activities if item.kind.value.startswith("commit_")]
        expected = [
            SkillDesignActivityKind.COMMIT_ACCEPTED,
            SkillDesignActivityKind.COMMIT_VALIDATION_STARTED,
        ]
        if crash_stage == "persistence":
            expected.extend(
                [
                    SkillDesignActivityKind.COMMIT_VALIDATION_PASSED,
                    SkillDesignActivityKind.COMMIT_PERSISTENCE_STARTED,
                ]
            )
        expected.append(SkillDesignActivityKind.COMMIT_TERMINAL)
        assert [item.kind for item in commit_activities] == expected
        assert commit_activities[-1].payload == {
            "status": "failed",
            "code": "SKILL_DESIGN_COMMIT_INTERRUPTED",
        }
        async with seed.factory() as session:
            operation = (
                await session.execute(
                    sa.select(SkillDesignOperationRow).where(
                        SkillDesignOperationRow.session_id == opened.id,
                        SkillDesignOperationRow.operation_kind == "commit",
                    )
                )
            ).scalar_one()
            assert operation.status == "failed"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_commit_stages_are_visible_while_work_is_in_progress(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, context, skills, design, _quota = await _environment(
        migrated_postgres_database_url,
    )
    preview_entered = asyncio.Event()
    preview_release = asyncio.Event()
    persistence_entered = asyncio.Event()
    persistence_release = asyncio.Event()
    try:
        opened = await design.create(
            context,
            CreateSkillDesignSession(
                slug="live-commit-activity",
                display_name="Live Commit Activity",
                idempotency_key="create-live-commit-activity",
            ),
        )
        drafted, _preview = await _seed_create_draft(
            seed,
            context,
            skills,
            design,
            opened.id,
            (_skill_md("live-commit-activity", "Expose real Commit stages."),),
        )
        validated = await _validate_session(
            design,
            context,
            drafted,
            "validate-live-commit-activity",
        )
        original_preview = skills.preview_archive
        original_create = skills.create_project_from_preview_in_session

        async def blocked_preview(*args, **kwargs):
            preview_entered.set()
            await preview_release.wait()
            return await original_preview(*args, **kwargs)

        async def blocked_create(*args, **kwargs):
            persistence_entered.set()
            await persistence_release.wait()
            return await original_create(*args, **kwargs)

        monkeypatch.setattr(skills, "preview_archive", blocked_preview)
        monkeypatch.setattr(
            skills,
            "create_project_from_preview_in_session",
            blocked_create,
        )
        commit_task = asyncio.create_task(
            _commit_session(
                design,
                context,
                validated,
                "commit-live-activity",
            )
        )
        await asyncio.wait_for(preview_entered.wait(), timeout=5)
        during_validation = await design.list_activities(context, opened.id)
        assert [item.kind for item in during_validation if item.kind.value.startswith("commit_")] == [
            SkillDesignActivityKind.COMMIT_ACCEPTED,
            SkillDesignActivityKind.COMMIT_VALIDATION_STARTED,
        ]

        preview_release.set()
        await asyncio.wait_for(persistence_entered.wait(), timeout=5)
        during_persistence = await design.list_activities(context, opened.id)
        assert [item.kind for item in during_persistence if item.kind.value.startswith("commit_")] == [
            SkillDesignActivityKind.COMMIT_ACCEPTED,
            SkillDesignActivityKind.COMMIT_VALIDATION_STARTED,
            SkillDesignActivityKind.COMMIT_VALIDATION_PASSED,
            SkillDesignActivityKind.COMMIT_PERSISTENCE_STARTED,
        ]

        persistence_release.set()
        result = await asyncio.wait_for(commit_task, timeout=5)
        assert result.session.status is SkillDesignStatus.COMPLETED
    finally:
        preview_release.set()
        persistence_release.set()
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_commit_revalidation_failure_has_durable_terminal(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, design, _quota = await _environment(
        migrated_postgres_database_url,
    )
    try:
        opened = await design.create(
            context,
            CreateSkillDesignSession(
                slug="commit-revalidation-failure",
                display_name="Commit Revalidation Failure",
                idempotency_key="create-commit-revalidation-failure",
            ),
        )
        drafted, _preview = await _seed_create_draft(
            seed,
            context,
            skills,
            design,
            opened.id,
            (
                _skill_md(
                    "commit-revalidation-failure",
                    "Reject a stale Commit after admission.",
                ),
            ),
        )
        validated = await _validate_session(
            design,
            context,
            drafted,
            "validate-before-stale-commit",
        )
        assert validated.draft_checksum is not None

        with pytest.raises(AssetConflict):
            await design.commit(
                context,
                opened.id,
                CommitSkillDesignSession(
                    expected_revision=validated.revision - 1,
                    expected_draft_checksum=validated.draft_checksum,
                    idempotency_key="stale-commit-with-terminal",
                ),
            )

        activities = await design.list_activities(context, opened.id)
        commit_activities = [item for item in activities if item.kind.value.startswith("commit_")]
        assert [item.kind for item in commit_activities] == [
            SkillDesignActivityKind.COMMIT_ACCEPTED,
            SkillDesignActivityKind.COMMIT_VALIDATION_STARTED,
            SkillDesignActivityKind.COMMIT_TERMINAL,
        ]
        assert commit_activities[-1].payload == {
            "status": "failed",
            "code": "asset_conflict",
        }
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_commit_failure_keeps_failed_operation_and_terminal_activity(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, context, skills, design, _quota = await _environment(
        migrated_postgres_database_url,
    )
    try:
        opened = await design.create(
            context,
            CreateSkillDesignSession(
                slug="commit-failure-activity",
                display_name="Commit Failure Activity",
                idempotency_key="create-commit-failure-activity",
            ),
        )
        drafted, _preview = await _seed_create_draft(
            seed,
            context,
            skills,
            design,
            opened.id,
            (
                _skill_md(
                    "commit-failure-activity",
                    "Fail only after deterministic validation.",
                ),
            ),
        )
        validated = await _validate_session(
            design,
            context,
            drafted,
            "validate-before-commit-failure",
        )

        async def fail_persistence(*_args, **_kwargs):
            raise RuntimeError("unexpected persistence failure")

        monkeypatch.setattr(
            skills,
            "create_project_from_preview_in_session",
            fail_persistence,
        )
        with pytest.raises(RuntimeError, match="unexpected persistence failure"):
            await _commit_session(
                design,
                context,
                validated,
                "commit-persistence-failure",
            )

        current = await design.get(context, opened.id)
        assert current.status is SkillDesignStatus.VALIDATED
        activities = await design.list_activities(context, opened.id)
        commit_activities = [item for item in activities if item.kind.value.startswith("commit_")]
        assert [item.kind for item in commit_activities] == [
            SkillDesignActivityKind.COMMIT_ACCEPTED,
            SkillDesignActivityKind.COMMIT_VALIDATION_STARTED,
            SkillDesignActivityKind.COMMIT_VALIDATION_PASSED,
            SkillDesignActivityKind.COMMIT_PERSISTENCE_STARTED,
            SkillDesignActivityKind.COMMIT_TERMINAL,
        ]
        assert commit_activities[-1].payload == {
            "status": "failed",
            "code": "asset_storage_unavailable",
        }
        async with seed.factory() as session:
            operation = (
                await session.execute(
                    sa.select(SkillDesignOperationRow).where(
                        SkillDesignOperationRow.session_id == opened.id,
                        SkillDesignOperationRow.operation_kind == "commit",
                    )
                )
            ).scalar_one()
            assert operation.status == "failed"
            assert operation.public_error_code == "asset_storage_unavailable"
            assert (
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(SkillRow)
                    .where(
                        SkillRow.project_id == context.project_id,
                        SkillRow.slug == "commit-failure-activity",
                    )
                )
                == 0
            )
    finally:
        await seed.engine.dispose()


async def _insert_current_skill(
    seed: PrivateThreadSeed,
    context: ProjectContext,
    *,
    slug: str,
    files: tuple[SkillArchiveFile, ...],
) -> uuid.UUID:
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    checksum = hashlib.sha256(
        json.dumps(
            [
                {
                    "path": item.path,
                    "sha256": hashlib.sha256(item.content).hexdigest(),
                    "size_bytes": len(item.content),
                }
                for item in sorted(files, key=lambda value: value.path)
            ],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    async with seed.factory() as session, session.begin():
        session.add(
            SkillRow(
                id=skill_id,
                scope="project",
                project_id=context.project_id,
                slug=slug,
                display_name=slug,
                status="active",
                current_version_id=None,
                revision=2,
                created_by_user_id=str(context.user_id),
            )
        )
        await session.flush()
        session.add(
            SkillVersionRow(
                id=version_id,
                skill_id=skill_id,
                version_number=1,
                description="Describe when and how to use this skill.",
                frontmatter={"name": slug, "description": "Describe when and how to use this skill."},
                compatibility=None,
                secret_requirements=[],
                scan_decision="allow",
                scan_summary={},
                payload_checksum=checksum,
                file_count=len(files),
                content_size_bytes=sum(len(item.content) for item in files),
                files_sealed=False,
                created_by_user_id=str(context.user_id),
            )
        )
        await session.flush()
        await session.execute(
            sa.text(
                """SELECT set_config(
                    'deerflow.asset_version_assembly',
                    :version_id,
                    true
                )"""
            ),
            {"version_id": str(version_id)},
        )
        for item in files:
            session.add(
                SkillVersionFileRow(
                    skill_version_id=version_id,
                    path=item.path,
                    media_type=item.media_type,
                    size_bytes=len(item.content),
                    sha256=hashlib.sha256(item.content).hexdigest(),
                    content=item.content,
                )
            )
        await session.flush()
        version = await session.get(SkillVersionRow, version_id)
        asset = await session.get(SkillRow, skill_id)
        assert version is not None and asset is not None
        version.files_sealed = True
        await session.flush()
        asset.current_version_id = version_id
    return skill_id


async def _claim_and_begin(seed: PrivateThreadSeed, *, now: datetime):
    worker_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="skill-builder-revision-pg",
                capabilities_json=["private_run"],
                max_concurrent_jobs=1,
            )
        )
    async with seed.factory() as session, session.begin():
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"private_run"}),
            lease_seconds=60,
            now=now,
        )
        assert claim is not None
        assert await jobs.mark_running(claim.job_id, lease_token=claim.lease_token, now=now)
        await PrivateRunRepository(session).begin_execution(
            scope=seed.owner_a.resource_scope,
            run_id=claim.run_id or "",
            job_id=claim.job_id,
            lease_token=claim.lease_token,
            origin_trace_id=claim.origin_trace_id,
            now=now,
        )
    return claim


@pytest.mark.postgres
@pytest.mark.anyio
async def test_create_session_validate_and_commit_saves_candidate_v1(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, design, quota = await _environment(migrated_postgres_database_url)
    slug = "builder-create-commit"
    files = (
        _skill_md(
            slug,
            "Use this Skill to turn a short request into a checked project summary.\n",
        ),
    )
    try:
        opened = await design.create(
            context,
            CreateSkillDesignSession(
                slug=slug,
                display_name="Builder Create Commit",
                idempotency_key="builder-create-open",
            ),
        )
        assert opened.session_kind == "create"
        assert opened.status is SkillDesignStatus.INTERVIEWING
        assert opened.created_skill_id is None

        ready, preview = await _seed_create_draft(
            seed,
            context,
            skills,
            design,
            opened.id,
            files,
        )
        assert ready.status is SkillDesignStatus.DRAFT_READY
        assert ready.draft_checksum == preview.checksum

        validated = await _validate_session(
            design,
            context,
            ready,
            "builder-create-validate",
        )
        assert validated.status is SkillDesignStatus.VALIDATED
        assert validated.validation is not None
        assert validated.validation.draft_checksum == preview.checksum

        committed = await _commit_session(
            design,
            context,
            validated,
            "builder-create-commit",
        )
        assert committed.session.status is SkillDesignStatus.COMPLETED
        assert committed.session.session_kind == "create"
        assert committed.session.created_skill_id == committed.skill.id
        assert committed.session.files == ()
        assert committed.skill.slug == slug
        assert committed.skill.status == "suspended"
        assert committed.skill.current_version_id is None
        assert committed.version is not None
        assert committed.session.created_skill_version_id == committed.version.id
        assert committed.version.relation.value == "candidate"
        refreshed = await design.get(context, opened.id)
        assert refreshed.created_skill_version_id == committed.version.id

        async with seed.factory() as session:
            design_row = await session.get(SkillDesignSessionRow, opened.id)
            assert design_row is not None
            assert design_row.created_skill_version_id is not None
            asset = await session.get(SkillRow, committed.skill.id)
            version = await session.get(
                SkillVersionRow,
                design_row.created_skill_version_id,
            )
            version_files = (await session.execute(sa.select(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == design_row.created_skill_version_id))).scalars().all()
            draft_file_count = await session.scalar(sa.select(sa.func.count()).select_from(SkillDesignDraftFileRow).where(SkillDesignDraftFileRow.session_id == opened.id))

        assert asset is not None
        assert version is not None
        assert asset.status == "suspended"
        assert asset.current_version_id is None
        assert version.version_number == 1
        assert version.supersedes_version_id is None
        assert version.payload_checksum == preview.checksum
        assert len(version_files) == 1
        assert version_files[0].path == "SKILL.md"
        assert version_files[0].content == files[0].content
        assert draft_file_count == 0
        assert quota.reserved == [version.id]

        activated = await skills.activate_version(
            context,
            committed.skill.id,
            committed.version.id,
            expected_asset_version=committed.skill.revision,
            expected_payload_checksum=committed.version.payload_checksum,
            expected_secret_revision=0,
        )
        assert activated.id == committed.version.id
        next_asset = await skills.get(context, committed.skill.id)
        next_candidate = await skills.create_version_from_archive(
            context,
            committed.skill.id,
            (_skill_md(slug, "A later current revision.\n"),),
            expected_asset_version=next_asset.revision,
        )
        next_asset = await skills.get(context, committed.skill.id)
        next_current = await skills.activate_version(
            context,
            committed.skill.id,
            next_candidate.id,
            expected_asset_version=next_asset.revision,
            expected_payload_checksum=next_candidate.payload_checksum,
            expected_secret_revision=0,
        )
        assert next_current.id != committed.version.id

        replayed = await _commit_session(
            design,
            context,
            validated,
            "builder-create-commit",
        )
        assert replayed.version is not None
        assert replayed.version.id == committed.version.id
        assert replayed.session.created_skill_version_id == committed.version.id
        assert replayed.skill.current_version_id == next_current.id
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revision_seed_matches_published_bytes_and_allows_manual_validate(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, design, _quota = await _environment(migrated_postgres_database_url)
    try:
        asset, current = await _create_current_template(skills, context, "seed-auditor")
        opened = await _open_revision(design, context, asset.id, "seed-open")
        assert opened.status is SkillDesignStatus.DRAFT_READY
        assert opened.session_kind == "revise"
        assert opened.target_skill_id == asset.id
        assert opened.base_version_id == current.id
        assert opened.base_version_number == 1
        assert opened.draft_checksum == current.payload_checksum == opened.base_payload_checksum
        assert [item.path for item in opened.files] == ["SKILL.md"]
        assert opened.files[0].content.encode() == _skill_md("seed-auditor", _template_body()).content
        assert opened.base_files[0].sha256 == opened.files[0].sha256
        assert opened.base_files[0].media_type == opened.files[0].media_type

        edited = await _edit_draft(
            design,
            context,
            opened,
            opened.files[0].content.replace("Add instructions", "Use reviewed inputs"),
            key="seed-edit",
        )
        assert edited.status is SkillDesignStatus.DRAFT_READY
        assert edited.draft_checksum != opened.draft_checksum
        assert edited.base_files == opened.base_files

        validated = await _validate_session(design, context, edited, "seed-validate")
        assert validated.status is SkillDesignStatus.VALIDATED
        assert validated.validation is not None
        assert validated.base_files == opened.base_files
        activities = await design.list_activities(context, opened.id)
        assert [item.kind.value for item in activities] == [
            "request_accepted",
            "validation_started",
            "validation_passed",
            "run_terminal",
        ]
        assert activities[1].payload == {"stage": "package_files"}
        assert activities[-1].payload == {"status": "completed"}
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revision_seed_dry_run_rejects_unsupported_published_shapes(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, _skills, design, _quota = await _environment(migrated_postgres_database_url)
    try:
        too_many = (
            _skill_md("too-many-files", _template_body()),
            *(SkillArchiveFile(f"notes/file-{index}.md", b"note\n", "text/markdown") for index in range(128)),
        )
        oversized = (
            _skill_md("oversized-file", _template_body()),
            SkillArchiveFile("notes/big.md", (b"x" * (512 * 1024 + 1)), "text/markdown"),
        )
        dotted = (
            _skill_md("dotted-path", _template_body()),
            SkillArchiveFile(".gitignore", b"*.pyc\n", "text/plain"),
        )
        secret = (_skill_md("secret-material", 'api_key = "abcdefghijklmnop"\n'),)
        binary = (
            _skill_md("binary-note", _template_body()),
            SkillArchiveFile("notes/raw.txt", b"\xff\xfe not utf-8", "text/plain"),
        )
        cases = (
            ("too-many-files", too_many),
            ("oversized-file", oversized),
            ("dotted-path", dotted),
            ("secret-material", secret),
            ("binary-note", binary),
        )
        for slug, files in cases:
            skill_id = await _insert_current_skill(seed, context, slug=slug, files=files)
            with pytest.raises(SkillDesignTargetUnsupported):
                await _open_revision(design, context, skill_id, f"reject-{slug}")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revision_session_uniqueness_and_target_404(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, design, _quota = await _environment(migrated_postgres_database_url)
    try:
        asset, _current = await _create_current_template(skills, context, "unique-auditor")
        first = await _open_revision(design, context, asset.id, "unique-open")
        replay = await _open_revision(design, context, asset.id, "unique-open")
        assert replay.id == first.id

        with pytest.raises(SkillDesignTargetSessionExists):
            await _open_revision(design, context, asset.id, "unique-other")

        async with seed.factory() as session, session.begin():
            row = await session.get(SkillDesignSessionRow, first.id)
            assert row is not None
            row.status = "failed"
            row.error_code = "SKILL_DESIGN_TARGET_SESSION_EXISTS"
            row.error_message = "A live revision session already occupies this target."
        with pytest.raises(SkillDesignTargetSessionExists):
            await _open_revision(design, context, asset.id, "unique-after-failed")

        async with seed.factory() as session, session.begin():
            with pytest.raises(IntegrityError) as exc_info:
                await session.execute(
                    sa.text(
                        """INSERT INTO skill_design_sessions
                        (id,project_id,owner_user_id,thread_id,slug,display_name,status,
                         revision,messages_json,progress_json,draft_checksum,
                         skill_creator_skill_id,skill_creator_version_id,
                         skill_creator_payload_checksum,error_code,error_message,
                         session_kind,target_skill_id,base_version_id,base_version_number,
                         base_payload_checksum,target_skill_deleted,
                         create_idempotency_key_hash,create_request_checksum)
                        SELECT :id,project_id,owner_user_id,:thread,slug,display_name,status,
                               revision,messages_json,progress_json,draft_checksum,
                               skill_creator_skill_id,skill_creator_version_id,
                               skill_creator_payload_checksum,error_code,error_message,
                               session_kind,target_skill_id,base_version_id,
                               base_version_number,base_payload_checksum,
                               target_skill_deleted,:hash,create_request_checksum
                          FROM skill_design_sessions WHERE id=:existing"""
                    ),
                    {
                        "id": uuid.uuid4(),
                        "thread": uuid.uuid4(),
                        "hash": "c" * 64,
                        "existing": first.id,
                    },
                )
        assert "uq_skill_design_sessions_live_revise_target" in str(exc_info.value)

        second_asset, _second_published = await _create_current_template(skills, context, "race-auditor")
        results = await asyncio.gather(
            _open_revision(design, context, second_asset.id, "race-a"),
            _open_revision(design, context, second_asset.id, "race-b"),
            return_exceptions=True,
        )
        opened = [item for item in results if not isinstance(item, Exception)]
        conflicts = [item for item in results if isinstance(item, SkillDesignTargetSessionExists)]
        assert len(opened) == 1
        assert len(conflicts) == 1

        async with seed.factory() as session:
            system_skill_id = await session.scalar(sa.select(SkillRow.id).where(SkillRow.scope == "system").limit(1))
        assert system_skill_id is not None
        with pytest.raises(AssetNotFound):
            await _open_revision(design, context, system_skill_id, "missing-system")
        with pytest.raises(AssetNotFound):
            await _open_revision(design, context, uuid.uuid4(), "missing-id")

        other_project_id = uuid.uuid4()
        other_skill_id = uuid.uuid4()
        async with seed.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    """INSERT INTO projects (id,slug,display_name,created_by_user_id)
                       VALUES (:id,:slug,'Other',:owner)"""
                ),
                {
                    "id": other_project_id,
                    "slug": f"other-{other_project_id.hex[:12]}",
                    "owner": str(context.user_id),
                },
            )
            await connection.execute(
                sa.text(
                    """INSERT INTO skills
                        (id,scope,project_id,slug,display_name,status,revision,created_by_user_id)
                    VALUES (:id,'project',:project,'foreign-skill','foreign-skill',
                            'suspended',1,:owner)"""
                ),
                {
                    "id": other_skill_id,
                    "project": other_project_id,
                    "owner": str(context.user_id),
                },
            )
        with pytest.raises(AssetNotFound):
            await _open_revision(design, context, other_skill_id, "missing-foreign")
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revision_turn_authoring_payload_is_isolated(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, design, _quota = await _environment(
        migrated_postgres_database_url,
        with_model=True,
    )
    try:
        asset, current = await _create_current_template(skills, context, "authoring-auditor")
        opened = await _open_revision(design, context, asset.id, "authoring-open")
        admitted = await design.submit_turn(
            context,
            opened.id,
            SubmitSkillDesignTurn(
                input=SkillDesignMessageTurn(kind="message", message="收紧已发布说明"),
                expected_revision=opened.revision,
                idempotency_key="authoring-turn",
            ),
        )
        assert admitted.run_id
        async with seed.factory() as session:
            run = (await session.execute(sa.select(RunRow).where(RunRow.run_id == admitted.run_id))).scalar_one()
        payload = json.loads(run.kwargs_json["input"]["messages"][0]["content"])
        assert payload["authoring"] == {
            "kind": "revise",
            "target_slug": "authoring-auditor",
            "base_version_number": current.version_number,
        }
        assert payload["conversation"]["mode"] == "initial"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revision_commit_creates_candidate_without_moving_current(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, design, quota = await _environment(migrated_postgres_database_url)
    try:
        asset, current_version = await _create_current_template(skills, context, "commit-auditor")
        opened = await _open_revision(design, context, asset.id, "commit-open")
        edited = await _edit_draft(
            design,
            context,
            opened,
            opened.files[0].content.replace("Add instructions", "Use reviewed inputs"),
            key="commit-edit",
        )
        validated = await _validate_session(design, context, edited, "commit-validate")
        reserved_before = set(quota.reserved)
        committed = await _commit_session(design, context, validated, "commit-save")
        assert committed.session.status is SkillDesignStatus.COMPLETED
        assert committed.session.created_skill_id == asset.id
        assert committed.version is not None
        assert committed.session.created_skill_version_id == committed.version.id
        assert committed.version.version_number == 2
        assert committed.version.relation.value == "candidate"
        assert committed.version.supersedes_version_id == current_version.id
        assert committed.version.id in quota.reserved
        assert committed.version.id not in reserved_before
        refreshed = await design.get(context, opened.id)
        assert refreshed.created_skill_version_id == committed.version.id
        current_asset = await skills.get(context, asset.id)
        assert current_asset.current_version_id == current_version.id
        replayed = await _commit_session(design, context, validated, "commit-save")
        assert replayed.version is not None
        assert replayed.version.id == committed.version.id
        assert replayed.session.created_skill_version_id == committed.version.id
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revision_commit_rejects_noop_and_stale_base(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, design, _quota = await _environment(migrated_postgres_database_url)
    try:
        asset, current = await _create_current_template(skills, context, "noop-auditor")
        opened = await _open_revision(design, context, asset.id, "noop-open")
        validated = await _validate_session(design, context, opened, "noop-validate")
        with pytest.raises(SkillDesignNoChanges):
            await _commit_session(design, context, validated, "noop-commit")

        media_only = await _edit_draft(
            design,
            context,
            validated,
            validated.files[0].content,
            key="media-edit",
            media_type="text/plain",
        )
        assert media_only.draft_checksum == opened.draft_checksum
        assert media_only.files[0].media_type == "text/plain"
        media_validated = await _validate_session(design, context, media_only, "media-validate")
        media_committed = await _commit_session(design, context, media_validated, "media-commit")
        assert media_committed.version is not None
        assert media_committed.version.supersedes_version_id == current.id

        stale_asset, stale_base = await _create_current_template(skills, context, "stale-auditor")
        stale_session = await _open_revision(design, context, stale_asset.id, "stale-open")
        live_candidate = await skills.create_version_from_archive(
            context,
            stale_asset.id,
            (_skill_md("stale-auditor", "Newer live successor.\n"),),
            expected_asset_version=stale_asset.revision,
        )
        stale_asset = await skills.get(context, stale_asset.id)
        live = await skills.activate_version(
            context,
            stale_asset.id,
            live_candidate.id,
            expected_asset_version=stale_asset.revision,
            expected_payload_checksum=live_candidate.payload_checksum,
            expected_secret_revision=0,
        )
        edited = await _edit_draft(
            design,
            context,
            stale_session,
            stale_session.files[0].content.replace("Add instructions", "Branch from the old base"),
            key="stale-edit",
        )
        stale_validated = await _validate_session(design, context, edited, "stale-validate")
        with pytest.raises(AssetConflict):
            await _commit_session(design, context, stale_validated, "stale-commit")
        current_asset = await skills.get(context, stale_asset.id)
        assert current_asset.current_version_id == live.id
        assert stale_base.id != live.id
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revision_delete_and_commit_converge_without_deadlock(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, design, _quota = await _environment(migrated_postgres_database_url)
    try:
        asset, _current = await _create_current_template(skills, context, "race-delete")
        opened = await _open_revision(design, context, asset.id, "race-delete-open")
        edited = await _edit_draft(
            design,
            context,
            opened,
            opened.files[0].content.replace("Add instructions", "Race against delete"),
            key="race-delete-edit",
        )
        validated = await _validate_session(design, context, edited, "race-delete-validate")
        current = await skills.get(context, asset.id)

        results = await asyncio.wait_for(
            asyncio.gather(
                skills.delete(
                    context,
                    asset.id,
                    expected_asset_version=current.revision,
                ),
                _commit_session(design, context, validated, "race-delete-commit"),
                return_exceptions=True,
            ),
            timeout=20,
        )
        for result in results:
            assert not isinstance(result, OperationalError)
        delete_result, commit_result = results
        delete_ok = delete_result is None
        commit_ok = not isinstance(commit_result, Exception)
        assert delete_ok != commit_ok, (delete_result, commit_result)
        if delete_ok:
            assert isinstance(commit_result, (SkillDesignTargetDeleted, AssetConflict, AssetNotFound))
            closed = await design.get(context, validated.id)
            assert closed.target_skill_deleted is True
            assert closed.status is SkillDesignStatus.FAILED
            with pytest.raises(AssetNotFound):
                await skills.get(context, asset.id)
        else:
            assert isinstance(delete_result, AssetConflict)
            assert commit_ok
            assert commit_result.version is not None
            persisted = await skills.get(context, asset.id)
            assert persisted.current_version_id == current.current_version_id
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revision_delete_fails_open_sessions_and_revokes_in_flight_tools(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, design, _quota = await _environment(
        migrated_postgres_database_url,
        with_model=True,
    )
    try:
        asset, _current = await _create_current_template(skills, context, "delete-auditor")
        opened = await _open_revision(design, context, asset.id, "delete-open")
        admitted = await design.submit_turn(
            context,
            opened.id,
            SubmitSkillDesignTurn(
                input=SkillDesignMessageTurn(kind="message", message="继续修订"),
                expected_revision=opened.revision,
                idempotency_key="delete-turn",
            ),
        )
        claim = await _claim_and_begin(seed, now=datetime.now(UTC))
        current = await skills.get(context, asset.id)
        await skills.delete(context, asset.id, expected_asset_version=current.revision)

        closed = await design.get(context, opened.id)
        assert closed.status is SkillDesignStatus.FAILED
        assert closed.error_code == "SKILL_DESIGN_TARGET_DELETED"
        assert closed.target_skill_deleted is True
        assert closed.target_skill_id is None
        async with seed.factory() as session:
            operation = (
                await session.execute(
                    sa.select(SkillDesignOperationRow).where(
                        SkillDesignOperationRow.run_id == admitted.run_id,
                    )
                )
            ).scalar_one()
        assert operation.status == "failed"
        assert operation.public_error_code == "SKILL_DESIGN_TARGET_DELETED"

        sink = design.terminal_sink(seed.owner_a, claim)
        with pytest.raises(AuthorizationRevoked):
            await sink.list_candidate_files(SkillBuilderCandidateFileList())

        with pytest.raises(SkillDesignTargetDeleted):
            await design.submit_turn(
                context,
                opened.id,
                SubmitSkillDesignTurn(
                    input=SkillDesignMessageTurn(kind="message", message="删除后不应继续"),
                    expected_revision=closed.revision,
                    idempotency_key="delete-turn-after",
                ),
            )
        with pytest.raises(SkillDesignTargetDeleted):
            await design.validate(
                context,
                opened.id,
                ValidateSkillDesignSession(
                    expected_revision=closed.revision,
                    expected_draft_checksum=closed.draft_checksum or ("d" * 64),
                    idempotency_key="delete-validate",
                ),
            )
        with pytest.raises(SkillDesignTargetDeleted):
            await design.commit(
                context,
                opened.id,
                CommitSkillDesignSession(
                    expected_revision=closed.revision,
                    expected_draft_checksum=closed.draft_checksum or ("d" * 64),
                    idempotency_key="delete-commit",
                ),
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_revision_delete_project_gate_prevents_builder_lock_cycle(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, context, skills, design, _quota = await _environment(
        migrated_postgres_database_url,
        with_model=True,
    )
    builder_operation_locked = asyncio.Event()
    delete_reached_project_gate = asyncio.Event()
    release_builder = asyncio.Event()
    retry_task: asyncio.Task | None = None
    delete_task: asyncio.Task | None = None
    try:
        asset, _current = await _create_current_template(
            skills,
            context,
            "delete-builder-lock-order",
        )
        opened = await _open_revision(
            design,
            context,
            asset.id,
            "delete-builder-lock-open",
        )
        turn = SubmitSkillDesignTurn(
            input=SkillDesignMessageTurn(kind="message", message="继续修订"),
            expected_revision=opened.revision,
            idempotency_key="delete-builder-lock-turn",
        )
        admitted = await design.submit_turn(
            context,
            opened.id,
            turn,
        )
        current = await skills.get(context, asset.id)

        original_get_operation = SkillDesignRepository.get_operation
        original_delete_gate = SkillRepository.lock_project_delete_scope

        async def hold_builder_operation(
            self,
            actor,
            *,
            operation_kind,
            idempotency_key_hash,
            for_update=False,
        ):
            operation = await original_get_operation(
                self,
                actor,
                operation_kind=operation_kind,
                idempotency_key_hash=idempotency_key_hash,
                for_update=for_update,
            )
            if for_update and operation is not None:
                builder_operation_locked.set()
                await asyncio.wait_for(release_builder.wait(), timeout=5)
            return operation

        async def observe_delete_project_gate(self, actor):
            delete_reached_project_gate.set()
            return await original_delete_gate(self, actor)

        monkeypatch.setattr(
            SkillDesignRepository,
            "get_operation",
            hold_builder_operation,
        )
        monkeypatch.setattr(
            SkillRepository,
            "lock_project_delete_scope",
            observe_delete_project_gate,
        )

        retry_task = asyncio.create_task(
            design.submit_turn(context, opened.id, turn),
        )
        await asyncio.wait_for(builder_operation_locked.wait(), timeout=5)
        delete_task = asyncio.create_task(
            skills.delete(
                context,
                asset.id,
                expected_asset_version=current.revision,
            )
        )
        await asyncio.wait_for(delete_reached_project_gate.wait(), timeout=5)
        release_builder.set()
        retry_result, delete_result = await asyncio.wait_for(
            asyncio.gather(
                retry_task,
                delete_task,
                return_exceptions=True,
            ),
            timeout=20,
        )

        assert not isinstance(retry_result, Exception), retry_result
        assert retry_result.run_id == admitted.run_id
        assert delete_result is None
        closed = await design.get(context, opened.id)
        assert closed.status is SkillDesignStatus.FAILED
        assert closed.target_skill_deleted is True
        async with seed.factory() as session:
            operation = (
                await session.execute(
                    sa.select(SkillDesignOperationRow).where(
                        SkillDesignOperationRow.run_id == admitted.run_id,
                    )
                )
            ).scalar_one()
        assert operation.status == "failed"
        assert operation.public_error_code == "SKILL_DESIGN_TARGET_DELETED"
    finally:
        release_builder.set()
        pending = tuple(task for task in (retry_task, delete_task) if task is not None and not task.done())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_forward_activation_lineage_guard_on_postgres(
    migrated_postgres_database_url: str,
) -> None:
    seed, context, skills, _design, _quota = await _environment(migrated_postgres_database_url)
    try:
        asset, first = await _create_current_template(skills, context, "lineage-auditor")
        assert first.supersedes_version_id is None
        current = await skills.get(context, asset.id)
        assert current.current_version_id == first.id

        second_candidate = await skills.create_version_from_archive(
            context,
            asset.id,
            (_skill_md("lineage-auditor", "Newer live successor.\n"),),
            expected_asset_version=current.revision,
        )
        current = await skills.get(context, asset.id)
        second = await skills.activate_version(
            context,
            asset.id,
            second_candidate.id,
            expected_asset_version=current.revision,
            expected_payload_checksum=second_candidate.payload_checksum,
            expected_secret_revision=0,
        )
        latest = await skills.get(context, asset.id)
        with pytest.raises(AssetConflict):
            await skills.fork_version(
                context,
                asset.id,
                first.id,
                (
                    SkillFileChange(
                        "replace",
                        "SKILL.md",
                        _skill_md(
                            "lineage-auditor",
                            "Attempted history branch.\n",
                        ).content.decode(),
                        "text/markdown",
                    ),
                ),
                expected_asset_version=latest.revision,
                expected_source_payload_checksum=first.payload_checksum,
            )

        third_candidate = await skills.create_version_from_archive(
            context,
            asset.id,
            (_skill_md("lineage-auditor", "Forward successor three.\n"),),
            expected_asset_version=latest.revision,
        )
        latest = await skills.get(context, asset.id)
        with pytest.raises(AssetConflict):
            await skills.activate_version(
                context,
                asset.id,
                third_candidate.id,
                expected_asset_version=latest.revision - 1,
                expected_payload_checksum=third_candidate.payload_checksum,
                expected_secret_revision=0,
            )
        third = await skills.activate_version(
            context,
            asset.id,
            third_candidate.id,
            expected_asset_version=latest.revision,
            expected_payload_checksum=third_candidate.payload_checksum,
            expected_secret_revision=0,
        )
        assert third.id == third_candidate.id
        assert third.supersedes_version_id == second.id
        after = await skills.get(context, asset.id)
        assert after.current_version_id == third.id
    finally:
        await seed.engine.dispose()
