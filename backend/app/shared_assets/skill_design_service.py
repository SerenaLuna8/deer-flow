"""Project-scoped conversational Skill Builder orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import (
    PrivateWorkContext,
)
from app.private_work.run_repository import (
    PrivateRunRepository,
)
from app.private_work.run_service import PrivateRunAuditPort, PrivateRunQuotaPort
from app.projects.capabilities import Capability
from app.projects.context import (
    ProjectContext,
)
from app.shared_assets.errors import (
    AssetConflict,
    AssetNotFound,
    AssetStorageQuotaExceeded,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
    SkillDesignNoChanges,
    SkillDesignTargetDeleted,
    SkillDesignTargetSessionExists,
    SkillDesignTargetUnsupported,
)
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.skill_builder_admission_contract import (
    SkillBuilderRunAdmission,
    SkillBuilderRunAdmissionPort,
)
from app.shared_assets.skill_builder_contract import (
    SkillBuilderDraftSink,
    _canonical_candidate_path,
)
from app.shared_assets.skill_builder_draft_sink import (
    SkillDesignDraftSink,
)
from app.shared_assets.skill_builder_draft_sink import (
    _append_row_message as _append_row_message_impl,
)
from app.shared_assets.skill_builder_draft_sink import (
    _draft_snapshot as _draft_snapshot_impl,
)
from app.shared_assets.skill_design_activity import (
    SkillDesignActivity,
    SkillDesignActivityKind,
    SkillDesignActivityRepository,
    activity_view,
)
from app.shared_assets.skill_design_codec import (
    _clarification_from_json as _clarification_from_json_impl,
)
from app.shared_assets.skill_design_codec import (
    _clarification_json as _clarification_json_impl,
)
from app.shared_assets.skill_design_codec import (
    _clarification_request as _clarification_request_impl,
)
from app.shared_assets.skill_design_codec import (
    _conversation_brief as _conversation_brief_impl,
)
from app.shared_assets.skill_design_codec import (
    _idempotency_hash as _idempotency_hash_impl,
)
from app.shared_assets.skill_design_codec import (
    _jsonable as _jsonable_impl,
)
from app.shared_assets.skill_design_codec import (
    _message_json as _message_json_impl,
)
from app.shared_assets.skill_design_codec import (
    _progress_json as _progress_json_impl,
)
from app.shared_assets.skill_design_codec import (
    _request_checksum as _request_checksum_impl,
)
from app.shared_assets.skill_design_codec import (
    _session_summary as _session_summary_impl,
)
from app.shared_assets.skill_design_codec import (
    _stable_generation_error_message as _stable_generation_error_message_impl,
)
from app.shared_assets.skill_design_codec import (
    _validation_from_json as _validation_from_json_impl,
)
from app.shared_assets.skill_design_codec import (
    _validation_from_preview as _validation_from_preview_impl,
)
from app.shared_assets.skill_design_codec import (
    _validation_json as _validation_json_impl,
)
from app.shared_assets.skill_design_codec import (
    _validation_matches_preview as _validation_matches_preview_impl,
)
from app.shared_assets.skill_design_contracts import (
    CancelSkillDesignSession,
    CommitSkillDesignSession,
    CreateSkillDesignRevisionSession,
    CreateSkillDesignSession,
    SetSkillDesignExecutionPreference,
    SkillDesignBaseFile,
    SkillDesignClarificationOption,  # noqa: F401
    SkillDesignClarificationRequest,  # noqa: F401
    SkillDesignClarificationResponse,
    SkillDesignClarificationTurn,
    SkillDesignCommitResult,
    SkillDesignDraftUpdateTurn,
    SkillDesignExecutionPreference,
    SkillDesignFileView,
    SkillDesignMessage,
    SkillDesignMessageTurn,
    SkillDesignProgressItem,
    SkillDesignProgressStatus,
    SkillDesignSecretRequirement,
    SkillDesignServiceErrorCode,
    SkillDesignSessionSummary,
    SkillDesignSessionView,
    SkillDesignStatus,
    SkillDesignTurn,
    SkillDesignTurnAttachment,
    SkillDesignValidation,
    SubmitSkillDesignTurn,
    ValidateSkillDesignSession,
)
from app.shared_assets.skill_design_generation import (
    DEFAULT_SKILL_DESIGN_TIMEOUT_SECONDS,
    CandidateResult,
    NeedsClarificationResult,
    SkillBuilderDependencySnapshot,
    SkillDesignAttachment,
    SkillDesignGeneratedFile,
    SkillDesignGenerationError,
    SkillDesignGenerationRequest,
    SkillDesignGenerationResult,
    SkillDesignGenerationService,
    contains_secret_like_material,
)
from app.shared_assets.skill_design_repository import SkillDesignRepository
from app.shared_assets.skill_design_validation import (
    _bounded_text as _bounded_text_impl,
)
from app.shared_assets.skill_design_validation import (
    _candidate_files as _candidate_files_impl,
)
from app.shared_assets.skill_design_validation import (
    _require_capability as _require_capability_impl,
)
from app.shared_assets.skill_design_validation import (
    _require_context as _require_context_impl,
)
from app.shared_assets.skill_design_validation import (
    _require_expected_revision as _require_expected_revision_impl,
)
from app.shared_assets.skill_design_validation import (
    _require_matching_clarification_response as _require_matching_clarification_response_impl,
)
from app.shared_assets.skill_design_validation import (
    _require_matching_operation as _require_matching_operation_impl,
)
from app.shared_assets.skill_design_validation import (
    _require_message_capacity as _require_message_capacity_impl,
)
from app.shared_assets.skill_design_validation import (
    _require_nonterminal as _require_nonterminal_impl,
)
from app.shared_assets.skill_design_validation import (
    _require_preview_name as _require_preview_name_impl,
)
from app.shared_assets.skill_design_validation import (
    _require_revise_target_live as _require_revise_target_live_impl,
)
from app.shared_assets.skill_design_validation import (
    _valid_revision as _valid_revision_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_builder_files as _validate_builder_files_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_cancel as _validate_cancel_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_commit as _validate_commit_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_create as _validate_create_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_create_revision as _validate_create_revision_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_execution_preference as _validate_execution_preference_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_idempotency_key as _validate_idempotency_key_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_partial_builder_files as _validate_partial_builder_files_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_turn as _validate_turn_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_turn_attachments as _validate_turn_attachments_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_turn_model_name as _validate_turn_model_name_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_turn_reasoning_effort as _validate_turn_reasoning_effort_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_uuid as _validate_uuid_impl,
)
from app.shared_assets.skill_design_validation import (
    _validate_validation as _validate_validation_impl,
)
from app.shared_assets.skill_package_integrity import verified_archive_files
from app.shared_assets.skill_repository import SkillRepository, SkillVersionRecord
from app.shared_assets.skill_service import (
    CreateSkill,
    ProjectSkillArchiveCreateResult,
    SkillArchivePreview,
    SkillService,
    SkillVersionView,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.shared_assets import (
    SkillDesignActivityRow,
    SkillDesignOperationBaselineFileRow,
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)

_PUBLIC_ERROR_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
MAX_INCOMPLETE_SKILL_DESIGN_SESSIONS_PER_OWNER_PROJECT = 8
_DEFAULT_STALE_GENERATING_SECONDS = DEFAULT_SKILL_DESIGN_TIMEOUT_SECONDS + 60.0
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "uq_skill_design_operations_idempotency",
        "uq_skill_design_sessions_create_idempotency",
        "uq_skill_design_sessions_live_revise_target",
        "uq_skills_project_display_name",
        "uq_skills_project_slug",
        "uq_skill_versions_asset_number",
    }
)


def _constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str):
            return name
        current = getattr(current, "orig", None) or getattr(
            current,
            "__cause__",
            None,
        )
    return None


class _RepositoryFactory(Protocol):
    def __call__(self, session: AsyncSession) -> SkillDesignRepository: ...


class _NoopPrivateRunQuota:
    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        del session, scope, run_id, request_id


class _NoopPrivateRunAudit:
    async def run_cancel_requested(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        run_id: str,
        job_id: uuid.UUID,
    ) -> None:
        del session, context, run_id, job_id

    async def run_terminal(
        self,
        session: AsyncSession,
        scope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None:
        del (
            session,
            scope,
            run_id,
            job_id,
            job_type,
            status,
            public_error_code,
            request_id,
        )


class SkillDesignService:
    """Coordinate private Skill Builder sessions and atomic import."""

    _conversation_brief = staticmethod(_conversation_brief_impl)
    _validation_from_preview = staticmethod(_validation_from_preview_impl)
    _validation_matches_preview = staticmethod(_validation_matches_preview_impl)
    _validation_json = staticmethod(_validation_json_impl)
    _validation_from_json = staticmethod(_validation_from_json_impl)
    _message_json = staticmethod(_message_json_impl)
    _progress_json = staticmethod(_progress_json_impl)
    _clarification_request = staticmethod(_clarification_request_impl)
    _clarification_json = staticmethod(_clarification_json_impl)
    _clarification_from_json = staticmethod(_clarification_from_json_impl)
    _session_summary = staticmethod(_session_summary_impl)
    _idempotency_hash = staticmethod(_idempotency_hash_impl)
    _request_checksum = staticmethod(_request_checksum_impl)
    _jsonable = staticmethod(_jsonable_impl)
    _stable_generation_error_message = staticmethod(_stable_generation_error_message_impl)
    _validate_create = staticmethod(_validate_create_impl)
    _validate_create_revision = staticmethod(_validate_create_revision_impl)
    _validate_turn = staticmethod(_validate_turn_impl)
    _validate_execution_preference = staticmethod(_validate_execution_preference_impl)
    _validate_turn_model_name = staticmethod(_validate_turn_model_name_impl)
    _validate_turn_reasoning_effort = staticmethod(_validate_turn_reasoning_effort_impl)
    _validate_turn_attachments = staticmethod(_validate_turn_attachments_impl)
    _validate_validation = staticmethod(_validate_validation_impl)
    _validate_commit = staticmethod(_validate_commit_impl)
    _validate_cancel = staticmethod(_validate_cancel_impl)
    _require_context = staticmethod(_require_context_impl)
    _require_capability = staticmethod(_require_capability_impl)
    _require_nonterminal = staticmethod(_require_nonterminal_impl)
    _require_revise_target_live = staticmethod(_require_revise_target_live_impl)
    _require_expected_revision = staticmethod(_require_expected_revision_impl)
    _require_matching_operation = staticmethod(_require_matching_operation_impl)
    _require_message_capacity = staticmethod(_require_message_capacity_impl)
    _require_matching_clarification_response = staticmethod(_require_matching_clarification_response_impl)
    _candidate_files = staticmethod(_candidate_files_impl)
    _validate_builder_files = staticmethod(_validate_builder_files_impl)
    _validate_partial_builder_files = staticmethod(_validate_partial_builder_files_impl)
    _require_preview_name = staticmethod(_require_preview_name_impl)
    _valid_revision = staticmethod(_valid_revision_impl)
    _validate_uuid = staticmethod(_validate_uuid_impl)
    _validate_idempotency_key = staticmethod(_validate_idempotency_key_impl)
    _bounded_text = staticmethod(_bounded_text_impl)
    _append_row_message = staticmethod(_append_row_message_impl)
    _draft_snapshot = staticmethod(_draft_snapshot_impl)

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        generator: SkillDesignGenerationService | None = None,
        skill_service: SkillService | None = None,
        repository_factory: _RepositoryFactory = SkillDesignRepository,
        run_admission: SkillBuilderRunAdmissionPort | None = None,
        quota: PrivateRunQuotaPort | None = None,
        audit: PrivateRunAuditPort | None = None,
        stale_generating_seconds: float = _DEFAULT_STALE_GENERATING_SECONDS,
    ) -> None:
        if not isinstance(stale_generating_seconds, int | float) or isinstance(stale_generating_seconds, bool) or stale_generating_seconds <= 0:
            raise ValueError("stale_generating_seconds must be positive")
        self._session_factory = session_factory
        # Legacy tests may inject the old in-process generator explicitly. The
        # production router always supplies durable Run admission; Gateway must
        # never create a model caller as an implicit fallback.
        self._generator = generator
        self._skill_service = skill_service or SkillService(session_factory)
        self._repository_factory = repository_factory
        self._run_admission = run_admission
        self._quota = quota or _NoopPrivateRunQuota()
        self._audit = audit or _NoopPrivateRunAudit()
        self._stale_after = timedelta(seconds=float(stale_generating_seconds))

    async def create(
        self,
        context: ProjectContext,
        command: CreateSkillDesignSession,
    ) -> SkillDesignSessionView:
        command = self._validate_create(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        idempotency_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "slug": command.slug,
                "display_name": command.display_name,
            }
        )
        now = self._now()
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    await repository.lock_session_create_scope(context)
                    existing = await repository.get_by_create_idempotency(
                        context,
                        idempotency_hash,
                        for_update=True,
                    )
                    if existing is not None:
                        if existing.create_request_checksum != request_checksum:
                            raise AssetConflict(context.request_id)
                        files = await repository.load_draft_files(
                            context,
                            existing.id,
                        )
                        return self._session_view(context, existing, files)
                    if await repository.count_incomplete(context) >= MAX_INCOMPLETE_SKILL_DESIGN_SESSIONS_PER_OWNER_PROJECT:
                        raise AssetStorageQuotaExceeded(context.request_id)
                    if await repository.project_skill_name_exists(
                        context,
                        slug=command.slug,
                        display_name=command.display_name,
                    ):
                        raise AssetConflict(context.request_id)
                    creator = await repository.resolve_current_skill_creator(context)
                    row = SkillDesignSessionRow(
                        id=uuid.uuid4(),
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=uuid.uuid4(),
                        slug=command.slug,
                        display_name=command.display_name,
                        status=SkillDesignStatus.INTERVIEWING.value,
                        revision=1,
                        messages_json=[
                            self._message_json(
                                "assistant",
                                "请描述这个 Skill 要解决的问题，并给出一两个典型使用示例。",
                                now=now,
                            )
                        ],
                        progress_json=self._progress_json(SkillDesignStatus.INTERVIEWING),
                        active_clarification_json=None,
                        draft_checksum=None,
                        validation_json=None,
                        validated_draft_checksum=None,
                        skill_creator_skill_id=creator.skill_id,
                        skill_creator_version_id=creator.version_id,
                        skill_creator_payload_checksum=creator.payload_checksum,
                        error_code=None,
                        error_message=None,
                        created_skill_id=None,
                        created_skill_version_id=None,
                        created_skill_deleted=False,
                        create_idempotency_key_hash=idempotency_hash,
                        create_request_checksum=request_checksum,
                        created_at=now,
                        updated_at=now,
                    )
                    await repository.create(context, row)
                    return self._session_view(context, row, ())
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            self._raise_integrity(context, exc)
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def _record_validation_failure(
        self,
        context: ProjectContext,
        *,
        session_id: uuid.UUID,
        operation_hash: str,
        request_checksum: str,
        public_error_code: str,
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                operation = await repository.get_operation(
                    context,
                    operation_kind="validate",
                    idempotency_key_hash=operation_hash,
                    for_update=True,
                )
                row = await repository.get(context, session_id, for_update=True)
                if operation is None or operation.status != "in_progress":
                    return
                self._require_matching_operation(
                    context,
                    operation,
                    session_id=session_id,
                    request_checksum=request_checksum,
                )
                operation.status = "failed"
                operation.result_revision = row.revision
                operation.public_error_code = public_error_code[:64]
                await SkillDesignActivityRepository(session).append(
                    context,
                    session_id=row.id,
                    operation_id=operation.id,
                    kind=SkillDesignActivityKind.VALIDATION_FAILED,
                    source_event_id="validation-failed",
                )
                await SkillDesignActivityRepository(session).append(
                    context,
                    session_id=row.id,
                    operation_id=operation.id,
                    kind=SkillDesignActivityKind.RUN_TERMINAL,
                    payload={
                        "status": "failed",
                        "code": public_error_code[:64],
                    },
                    source_event_id="validation-terminal",
                )
        except (SharedAssetError, DBAPIError):
            return

    async def _record_commit_failure(
        self,
        context: ProjectContext,
        *,
        session_id: uuid.UUID,
        operation_hash: str,
        request_checksum: str,
        public_error_code: str,
        validation_passed: bool,
        persistence_started: bool,
    ) -> None:
        """Persist the failed Commit projection after its business tx rolls back."""

        try:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                await repository.lock_context(context)
                row = await repository.get(context, session_id, for_update=True)
                operation = await repository.get_operation(
                    context,
                    operation_kind="commit",
                    idempotency_key_hash=operation_hash,
                    for_update=True,
                )
                if operation is None or operation.status != "in_progress":
                    return
                self._require_matching_operation(
                    context,
                    operation,
                    session_id=session_id,
                    request_checksum=request_checksum,
                )
                code = public_error_code[:64]
                operation.status = "failed"
                operation.result_revision = row.revision
                operation.public_error_code = code
                if row.status == SkillDesignStatus.COMMITTING.value:
                    row.status = SkillDesignStatus.VALIDATED.value
                    row.progress_json = self._progress_json(
                        SkillDesignStatus.VALIDATED,
                    )
                activity_repository = SkillDesignActivityRepository(session)
                stages: list[tuple[SkillDesignActivityKind, str]] = []
                if validation_passed:
                    stages.append(
                        (
                            SkillDesignActivityKind.COMMIT_VALIDATION_PASSED,
                            "commit-validation-passed",
                        )
                    )
                if persistence_started:
                    stages.append(
                        (
                            SkillDesignActivityKind.COMMIT_PERSISTENCE_STARTED,
                            "commit-persistence-started",
                        )
                    )
                for kind, source_event_id in stages:
                    await activity_repository.append(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                        kind=kind,
                        source_event_id=source_event_id,
                    )
                await activity_repository.append(
                    context,
                    session_id=row.id,
                    operation_id=operation.id,
                    kind=SkillDesignActivityKind.COMMIT_TERMINAL,
                    payload={"status": "failed", "code": code},
                    source_event_id="commit-terminal",
                )
        except (SharedAssetError, DBAPIError):
            return

    async def create_revision(
        self,
        context: ProjectContext,
        command: CreateSkillDesignRevisionSession,
    ) -> SkillDesignSessionView:
        command = self._validate_create_revision(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        idempotency_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "session_kind": "revise",
                "skill_id": str(command.skill_id),
            }
        )
        now = self._now()
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    await repository.lock_session_create_scope(context)
                    existing = await repository.get_by_create_idempotency(
                        context,
                        idempotency_hash,
                        for_update=True,
                    )
                    if existing is not None:
                        if existing.create_request_checksum != request_checksum:
                            raise AssetConflict(context.request_id)
                        files = await repository.load_draft_files(
                            context,
                            existing.id,
                        )
                        return self._session_view(
                            context,
                            existing,
                            files,
                            base_files=await self._base_files_for_row(
                                repository,
                                context,
                                existing,
                            ),
                        )
                    if await repository.count_incomplete(context) >= MAX_INCOMPLETE_SKILL_DESIGN_SESSIONS_PER_OWNER_PROJECT:
                        raise AssetStorageQuotaExceeded(context.request_id)
                    skill_repository = SkillRepository(session)
                    target = await skill_repository.get_project_asset(
                        context,
                        command.skill_id,
                        for_update=True,
                    )
                    if target.status not in {"active", "suspended"}:
                        raise AssetConflict(context.request_id)
                    if await repository.live_revision_session_exists(
                        context,
                        target.id,
                    ):
                        raise SkillDesignTargetSessionExists(context.request_id)
                    history = await skill_repository.get_project_version_history(
                        context,
                        target.id,
                    )
                    head = max(
                        history,
                        key=lambda item: item.row.version_number,
                        default=None,
                    )
                    if head is None:
                        raise AssetConflict(context.request_id)
                    record = await skill_repository.get_project_version(
                        context,
                        target.id,
                        head.row.id,
                        for_update=True,
                    )
                    seeded = self._seed_revision_files(context, record)
                    snapshot = self._draft_snapshot(context, seeded)
                    if snapshot.draft_checksum != record.row.payload_checksum:
                        raise SkillDesignTargetUnsupported(context.request_id)
                    await self._seed_revision_dry_run(context, seeded)
                    creator = await repository.resolve_current_skill_creator(context)
                    row = SkillDesignSessionRow(
                        id=uuid.uuid4(),
                        project_id=context.project_id,
                        owner_user_id=str(context.user_id),
                        thread_id=uuid.uuid4(),
                        slug=target.slug,
                        display_name=target.display_name,
                        status=SkillDesignStatus.DRAFT_READY.value,
                        revision=1,
                        messages_json=[
                            self._message_json(
                                "assistant",
                                (f"已加载 {target.slug} v{record.row.version_number} 的 {len(seeded)} 个文件，可直接编辑草稿，或描述要修改的内容。"),
                                now=now,
                            )
                        ],
                        progress_json=self._progress_json(SkillDesignStatus.DRAFT_READY),
                        active_clarification_json=None,
                        draft_checksum=snapshot.draft_checksum,
                        validation_json=None,
                        validated_draft_checksum=None,
                        skill_creator_skill_id=creator.skill_id,
                        skill_creator_version_id=creator.version_id,
                        skill_creator_payload_checksum=creator.payload_checksum,
                        error_code=None,
                        error_message=None,
                        created_skill_id=None,
                        created_skill_version_id=None,
                        created_skill_deleted=False,
                        session_kind="revise",
                        target_skill_id=target.id,
                        base_version_id=record.row.id,
                        base_version_number=record.row.version_number,
                        base_payload_checksum=record.row.payload_checksum,
                        target_skill_deleted=False,
                        create_idempotency_key_hash=idempotency_hash,
                        create_request_checksum=request_checksum,
                        created_at=now,
                        updated_at=now,
                    )
                    await repository.create(context, row)
                    await repository.replace_draft_files(context, row.id, seeded)
                    return self._session_view(
                        context,
                        row,
                        seeded,
                        base_files=tuple(
                            SkillDesignBaseFile(
                                path=item.path,
                                media_type=item.media_type,
                                size_bytes=len(item.content),
                                sha256=hashlib.sha256(item.content).hexdigest(),
                            )
                            for item in seeded
                        ),
                    )
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            self._raise_integrity(context, exc)
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    @staticmethod
    def _seed_revision_files(
        context: ProjectContext,
        record: SkillVersionRecord,
    ) -> tuple[SkillArchiveFile, ...]:
        """Copy byte-verified base files, rejecting shapes the Builder cannot hold."""

        try:
            files = verified_archive_files(record, context.request_id)
        except SharedAssetError:
            raise SkillDesignTargetUnsupported(context.request_id) from None
        for item in files:
            try:
                _canonical_candidate_path(item.path)
            except ValueError:
                raise SkillDesignTargetUnsupported(context.request_id) from None
        try:
            return SkillDesignService._validate_builder_files(
                context,
                files,
                allow_empty=False,
                require_skill_md=True,
            )
        except AssetValidationFailed:
            raise SkillDesignTargetUnsupported(context.request_id) from None

    async def _seed_revision_dry_run(
        self,
        context: ProjectContext,
        files: tuple[SkillArchiveFile, ...],
    ) -> None:
        """Re-run structural and frontmatter validation under current rules."""

        try:
            await self._skill_service.preview_archive(context, files)
        except SharedAssetError:
            raise SkillDesignTargetUnsupported(context.request_id) from None

    @staticmethod
    async def _base_files_for_row(
        repository: SkillDesignRepository,
        context: ProjectContext,
        row: SkillDesignSessionRow,
    ) -> tuple[SkillDesignBaseFile, ...]:
        """Pinned base-version identity for revise sessions, without content."""

        if row.session_kind != "revise" or row.base_version_id is None:
            return ()
        metadata = await repository.load_base_file_metadata(
            context,
            row.base_version_id,
        )
        return tuple(
            SkillDesignBaseFile(
                path=item.path,
                media_type=item.media_type,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
            )
            for item in metadata
        )

    @staticmethod
    async def _session_view_for_row(
        repository: SkillDesignRepository,
        context: ProjectContext,
        row: SkillDesignSessionRow,
        files: tuple[SkillArchiveFile, ...],
    ) -> SkillDesignSessionView:
        """Keep a revision session's immutable comparison baseline on mutations."""

        return SkillDesignService._session_view(
            context,
            row,
            files,
            base_files=await SkillDesignService._base_files_for_row(
                repository,
                context,
                row,
            ),
        )

    async def list_incomplete(
        self,
        context: ProjectContext,
        *,
        limit: int = 20,
    ) -> tuple[SkillDesignSessionSummary, ...]:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_READ)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise AssetValidationFailed(context.request_id)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    if Capability.SHARED_ASSETS_EDIT in context.capabilities:
                        await repository.lock_context(context)
                    rows = await repository.list_incomplete(context, limit=limit)
                    now = self._now()
                    result: list[SkillDesignSessionSummary] = []
                    for listed in rows:
                        row = listed
                        if Capability.SHARED_ASSETS_EDIT in context.capabilities and (
                            self._is_stale_generating(row, now=now)
                            or row.status
                            in {
                                SkillDesignStatus.VALIDATED.value,
                                SkillDesignStatus.COMMITTING.value,
                            }
                        ):
                            row = await repository.get(
                                context,
                                row.id,
                                for_update=True,
                            )
                            await self._recover_stale_generating(
                                repository,
                                context,
                                row,
                                now=now,
                            )
                            await self._recover_stale_commit(
                                repository,
                                context,
                                row,
                                now=now,
                            )
                        result.append(self._session_summary(row))
                    return tuple(result)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def get(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
    ) -> SkillDesignSessionView:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_READ)
        session_id = self._validate_uuid(context, session_id)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    if Capability.SHARED_ASSETS_EDIT in context.capabilities:
                        await repository.lock_context(context)
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=(Capability.SHARED_ASSETS_EDIT in context.capabilities),
                    )
                    if Capability.SHARED_ASSETS_EDIT in context.capabilities:
                        await self._recover_stale_generating(
                            repository,
                            context,
                            row,
                            now=self._now(),
                        )
                        await self._recover_stale_commit(
                            repository,
                            context,
                            row,
                            now=self._now(),
                        )
                    files = await repository.load_draft_files(
                        context,
                        row.id,
                    )
                    linked_run = await repository.latest_linked_run(
                        context,
                        row.id,
                    )
                    active_run = (
                        SkillBuilderRunAdmission(
                            run_id=linked_run.run_id,
                            status=linked_run.status,
                            thread_id=str(row.thread_id),
                        )
                        if linked_run is not None and linked_run.status in {"pending", "running"}
                        else None
                    )
                    return self._session_view(
                        context,
                        row,
                        files,
                        active_run=active_run,
                        base_files=await self._base_files_for_row(
                            repository,
                            context,
                            row,
                        ),
                    )
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def get_by_created_version(
        self,
        context: ProjectContext,
        version_id: uuid.UUID,
    ) -> SkillDesignSessionView:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_READ)
        version_id = self._validate_uuid(context, version_id)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    row = await repository.get_by_created_version(
                        context,
                        version_id,
                    )
                    files = await repository.load_draft_files(
                        context,
                        row.id,
                    )
                    return await self._session_view_for_row(
                        repository,
                        context,
                        row,
                        files,
                    )
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def submit_turn(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: SubmitSkillDesignTurn,
    ) -> SkillDesignSessionView | SkillBuilderRunAdmission:
        command = self._validate_turn(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        operation_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "session_id": session_id,
                "expected_revision": command.expected_revision,
                "input": command.input,
            }
        )
        if isinstance(command.input, SkillDesignDraftUpdateTurn):
            return await self._apply_draft_update(
                context,
                session_id,
                command,
                operation_hash=operation_hash,
                request_checksum=request_checksum,
            )

        prepared = await self._prepare_generation(
            context,
            session_id,
            command,
            operation_hash=operation_hash,
            request_checksum=request_checksum,
        )
        if isinstance(prepared, SkillDesignSessionView | SkillBuilderRunAdmission):
            return prepared
        generation_revision, request, creator_content = prepared
        if self._generator is None:
            raise AssetStorageUnavailable(context.request_id)
        try:
            result = await self._generator.generate(
                request,
                skill_creator_content=creator_content,
                model_name=getattr(command.input, "model_name", None),
                reasoning_effort=getattr(command.input, "reasoning_effort", None),
            )
            if contains_secret_like_material(result.model_dump(mode="json")):
                raise AssetValidationFailed(context.request_id)
            preview: SkillArchivePreview | None = None
            if isinstance(result, CandidateResult):
                candidate_files = self._candidate_files(
                    context,
                    result,
                )
                preview = await self._skill_service.preview_archive(
                    context,
                    candidate_files,
                )
                self._require_preview_name(
                    context,
                    preview,
                    request.skill_slug,
                )
            return await self._finish_generation_success(
                context,
                session_id,
                operation_hash=operation_hash,
                generation_revision=generation_revision,
                result=result,
                preview=preview,
            )
        except SkillDesignGenerationError as exc:
            code = exc.code if isinstance(exc.code, str) and _PUBLIC_ERROR_PATTERN.fullmatch(exc.code) else SkillDesignServiceErrorCode.GENERATION_UNAVAILABLE.value
        except AssetValidationFailed:
            code = SkillDesignServiceErrorCode.INVALID_MODEL_OUTPUT.value
        except Exception:
            code = SkillDesignServiceErrorCode.GENERATION_UNAVAILABLE.value
        return await self._finish_generation_failure(
            context,
            session_id,
            operation_hash=operation_hash,
            generation_revision=generation_revision,
            error_code=code,
            error_message=self._stable_generation_error_message(code),
        )

    async def set_execution_preference(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: SetSkillDesignExecutionPreference,
    ) -> SkillDesignSessionView:
        command = self._validate_execution_preference(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        try:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                row = await repository.get(context, session_id, for_update=True)
                self._require_revise_target_live(context, row)
                if row.status in {
                    SkillDesignStatus.GENERATING.value,
                    SkillDesignStatus.COMMITTING.value,
                    SkillDesignStatus.COMPLETED.value,
                    SkillDesignStatus.CANCELLED.value,
                }:
                    raise AssetConflict(context.request_id)
                row.execution_model_ref = command.model_name
                row.execution_mode = command.mode
                row.execution_thinking_enabled = command.thinking_enabled
                row.execution_reasoning_effort = command.reasoning_effort
                row.revision += 1
                await session.flush()
                files = await repository.load_draft_files(context, row.id)
                return await self._session_view_for_row(
                    repository,
                    context,
                    row,
                    files,
                )
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            self._raise_integrity(context, exc)
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def list_activities(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> tuple[SkillDesignActivity, ...]:
        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_READ)
        session_id = self._validate_uuid(context, session_id)
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0 or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 2_000:
            raise AssetValidationFailed(context.request_id)
        try:
            async with self._session_factory() as session, session.begin():
                rows = await SkillDesignActivityRepository(session).list_after(
                    context,
                    session_id=session_id,
                    after_seq=after_seq,
                    limit=limit,
                )
                return tuple(activity_view(row) for row in rows)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def stop_current_run(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
    ) -> SkillDesignSessionView:
        """Request cancellation for only the current Builder Run."""

        self._require_context(context)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        try:
            async with self._session_factory() as session, session.begin():
                repository = self._repository_factory(session)
                row = await repository.get(context, session_id, for_update=True)
                linked_run = await repository.latest_linked_run(
                    context,
                    row.id,
                    lock=True,
                )
                if linked_run is None:
                    files = await repository.load_draft_files(context, row.id)
                    return await self._session_view_for_row(
                        repository,
                        context,
                        row,
                        files,
                    )
                operation = await repository.operation_by_run(
                    context,
                    linked_run.run_id,
                    for_update=True,
                )
                if operation is None or operation.status != "in_progress" or row.status != SkillDesignStatus.GENERATING.value or linked_run.status not in {"pending", "running"} or linked_run.job_id is None:
                    files = await repository.load_draft_files(context, row.id)
                    return await self._session_view_for_row(
                        repository,
                        context,
                        row,
                        files,
                    )
                operation.stop_requested_at = operation.stop_requested_at or self._now()
                private_context = PrivateWorkContext.from_project(context)
                cancel_result = await PrivateRunRepository(session).request_cancel(
                    scope=private_context.resource_scope,
                    thread_id=linked_run.thread_id,
                    run_id=linked_run.run_id,
                    job_id=linked_run.job_id,
                    reason="skill_builder_turn_stopped",
                )
                if cancel_result != "terminal":
                    await self._audit.run_cancel_requested(
                        session,
                        private_context,
                        run_id=linked_run.run_id,
                        job_id=linked_run.job_id,
                    )
                if cancel_result in {"cancelled", "terminal"}:
                    await self._quota.release_concurrent_run(
                        session,
                        private_context.resource_scope,
                        run_id=linked_run.run_id,
                        request_id=context.request_id,
                    )
                if cancel_result == "cancelled":
                    job_type = await session.scalar(
                        select(JobRow.job_type).where(
                            JobRow.id == linked_run.job_id,
                            JobRow.project_id == context.project_id,
                            JobRow.owner_user_id == str(context.user_id),
                        )
                    )
                    if job_type != "private_run":
                        raise AssetConflict(context.request_id)
                    await self._audit.run_terminal(
                        session,
                        private_context.resource_scope,
                        run_id=linked_run.run_id,
                        job_id=linked_run.job_id,
                        job_type=job_type,
                        status="interrupted",
                        public_error_code=None,
                        request_id=context.request_id,
                    )
                    files = await repository.restore_operation_baseline(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                    )
                    snapshot = self._draft_snapshot(context, files)
                    row.draft_checksum = snapshot.draft_checksum
                    row.authoring_dependencies_json = None
                    row.validation_json = None
                    row.validated_draft_checksum = None
                    row.active_clarification_json = None
                    row.error_code = None
                    row.error_message = None
                    row.status = SkillDesignStatus.DRAFT_READY.value if snapshot.draft_checksum is not None else SkillDesignStatus.INTERVIEWING.value
                    row.progress_json = self._progress_json(SkillDesignStatus.DRAFT_READY if snapshot.draft_checksum is not None else SkillDesignStatus.INTERVIEWING)
                    row.revision += 1
                    operation.status = "stopped"
                    operation.result_revision = row.revision
                    operation.public_error_code = None
                    await SkillDesignActivityRepository(session).append(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                        run_id=operation.run_id,
                        kind=SkillDesignActivityKind.RUN_TERMINAL,
                        payload={"status": "stopped"},
                        source_event_id="run-terminal",
                    )
                    await repository.clear_operation_baseline(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                    )
                    await session.flush()
                    return await self._session_view_for_row(
                        repository,
                        context,
                        row,
                        files,
                    )
            for _ in range(40):
                await asyncio.sleep(0.25)
                view = await self.get(context, session_id)
                if view.status != SkillDesignStatus.GENERATING:
                    return view
            return await self.get(context, session_id)
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    def terminal_sink(
        self,
        context: PrivateWorkContext,
        claim: JobClaim,
    ) -> SkillBuilderDraftSink:
        return SkillDesignDraftSink(
            self._session_factory,
            context,
            claim,
            skill_service=self._skill_service,
            repository_factory=self._repository_factory,
        )

    async def validate(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: ValidateSkillDesignSession,
    ) -> SkillDesignSessionView:
        command = self._validate_validation(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        operation_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "session_id": session_id,
                "expected_revision": command.expected_revision,
                "expected_draft_checksum": command.expected_draft_checksum,
            }
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="validate",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is not None:
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        if operation.status == "completed":
                            files = await repository.load_draft_files(
                                context,
                                row.id,
                            )
                            return await self._session_view_for_row(
                                repository,
                                context,
                                row,
                                files,
                            )
                        raise AssetConflict(context.request_id)
                    self._require_revise_target_live(context, row)
                    self._require_expected_revision(
                        context,
                        row,
                        command.expected_revision,
                    )
                    self._require_nonterminal(context, row)
                    if (
                        row.status
                        not in {
                            SkillDesignStatus.DRAFT_READY.value,
                            SkillDesignStatus.VALIDATED.value,
                        }
                        or row.draft_checksum != command.expected_draft_checksum
                    ):
                        raise AssetConflict(context.request_id)
                    files = await repository.load_draft_files(
                        context,
                        row.id,
                    )
                    operation = self._new_operation(
                        context,
                        session_id,
                        kind="validate",
                        idempotency_hash=operation_hash,
                        request_checksum=request_checksum,
                    )
                    await repository.create_operation(context, operation)
                    activity_repository = SkillDesignActivityRepository(session)
                    await activity_repository.append(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                        kind=SkillDesignActivityKind.REQUEST_ACCEPTED,
                        source_event_id="validation-request-accepted",
                    )
                    await activity_repository.append(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                        kind=SkillDesignActivityKind.VALIDATION_STARTED,
                        payload={"stage": "package_files"},
                        source_event_id="validation-package-files-started",
                    )
            files = self._validate_builder_files(context, files)
            preview = await self._skill_service.preview_archive(context, files)
            self._require_preview_name(context, preview, row.slug)
            validation = self._validation_from_preview(
                preview,
                validated_at=self._now(),
            )

            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="validate",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is None:
                        raise AssetConflict(context.request_id)
                    self._require_matching_operation(
                        context,
                        operation,
                        session_id=session_id,
                        request_checksum=request_checksum,
                    )
                    if operation.status == "completed":
                        current_files = await repository.load_draft_files(
                            context,
                            row.id,
                        )
                        return await self._session_view_for_row(
                            repository,
                            context,
                            row,
                            current_files,
                        )
                    if operation.status != "in_progress":
                        raise AssetConflict(context.request_id)
                    self._require_revise_target_live(context, row)
                    self._require_expected_revision(
                        context,
                        row,
                        command.expected_revision,
                    )
                    if row.draft_checksum != command.expected_draft_checksum:
                        raise AssetConflict(context.request_id)
                    row.validation_json = self._validation_json(validation)
                    row.validated_draft_checksum = row.draft_checksum
                    row.status = SkillDesignStatus.VALIDATED.value
                    row.progress_json = self._progress_json(SkillDesignStatus.VALIDATED)
                    row.error_code = None
                    row.error_message = None
                    row.revision += 1
                    operation.status = "completed"
                    operation.result_revision = row.revision
                    await SkillDesignActivityRepository(session).append(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                        kind=SkillDesignActivityKind.VALIDATION_PASSED,
                        source_event_id="validation-passed",
                    )
                    await SkillDesignActivityRepository(session).append(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                        kind=SkillDesignActivityKind.RUN_TERMINAL,
                        payload={"status": "completed"},
                        source_event_id="validation-terminal",
                    )
                    await session.flush()
                    current_files = await repository.load_draft_files(
                        context,
                        row.id,
                    )
                    return await self._session_view_for_row(
                        repository,
                        context,
                        row,
                        current_files,
                    )
        except SharedAssetError as exc:
            await self._record_validation_failure(
                context,
                session_id=session_id,
                operation_hash=operation_hash,
                request_checksum=request_checksum,
                public_error_code=exc.code,
            )
            raise
        except IntegrityError as exc:
            try:
                self._raise_integrity(context, exc)
            except SharedAssetError as domain_error:
                await self._record_validation_failure(
                    context,
                    session_id=session_id,
                    operation_hash=operation_hash,
                    request_checksum=request_checksum,
                    public_error_code=domain_error.code,
                )
                raise
        except DBAPIError:
            await self._record_validation_failure(
                context,
                session_id=session_id,
                operation_hash=operation_hash,
                request_checksum=request_checksum,
                public_error_code=AssetStorageUnavailable.code,
            )
            raise AssetStorageUnavailable(context.request_id) from None
        except Exception:
            await self._record_validation_failure(
                context,
                session_id=session_id,
                operation_hash=operation_hash,
                request_checksum=request_checksum,
                public_error_code=AssetStorageUnavailable.code,
            )
            raise

    async def commit(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: CommitSkillDesignSession,
    ) -> SkillDesignCommitResult:
        command = self._validate_commit(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        operation_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "session_id": session_id,
                "expected_revision": command.expected_revision,
                "expected_draft_checksum": command.expected_draft_checksum,
            }
        )
        repeated_session: SkillDesignSessionView | None = None
        created_result: ProjectSkillArchiveCreateResult | None = None
        created_version: SkillVersionView | None = None
        commit_started = False
        commit_validation_passed = False
        commit_persistence_started = False
        try:
            # Admit the Commit first so request/revalidation progress is durable
            # and observable before deterministic work starts.
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    await repository.lock_context(context)
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    operation = await repository.get_operation(
                        context,
                        operation_kind="commit",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    if operation is not None:
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        if operation.status == "completed" and row.status == SkillDesignStatus.COMPLETED.value:
                            repeated_session = await self._session_view_for_row(
                                repository,
                                context,
                                row,
                                (),
                            )
                        else:
                            raise AssetConflict(context.request_id)
                    elif row.status == SkillDesignStatus.COMPLETED.value:
                        self._require_expected_revision(
                            context,
                            row,
                            command.expected_revision,
                        )
                        if row.draft_checksum != command.expected_draft_checksum:
                            raise AssetConflict(context.request_id)
                        repeated_session = await self._session_view_for_row(
                            repository,
                            context,
                            row,
                            (),
                        )
                    else:
                        operation = self._new_operation(
                            context,
                            session_id,
                            kind="commit",
                            idempotency_hash=operation_hash,
                            request_checksum=request_checksum,
                        )
                        await repository.create_operation(context, operation)
                        activity_repository = SkillDesignActivityRepository(
                            session,
                        )
                        for kind, source_event_id in (
                            (
                                SkillDesignActivityKind.COMMIT_ACCEPTED,
                                "commit-accepted",
                            ),
                            (
                                SkillDesignActivityKind.COMMIT_VALIDATION_STARTED,
                                "commit-validation-started",
                            ),
                        ):
                            await activity_repository.append(
                                context,
                                session_id=row.id,
                                operation_id=operation.id,
                                kind=kind,
                                source_event_id=source_event_id,
                            )
                        await session.flush()
                        commit_started = True

            if repeated_session is None:
                # Revalidate under locks, then make the draft immutable to all
                # other Builder mutations before announcing persistence.
                async with self._session_factory() as session:
                    async with session.begin():
                        repository = self._repository_factory(session)
                        await repository.lock_context(context)
                        # Lock order: Project → Membership → Skill →
                        # SkillDesignSession. Learn the target first, then lock
                        # its Skill before the Builder session.
                        unlocked = await repository.get(context, session_id)
                        target_asset = None
                        if unlocked.session_kind == "revise" and not unlocked.target_skill_deleted and unlocked.target_skill_id is not None:
                            try:
                                target_asset = await SkillRepository(
                                    session,
                                ).get_project_asset(
                                    context,
                                    unlocked.target_skill_id,
                                    for_update=True,
                                )
                            except AssetNotFound:
                                target_asset = None
                        row = await repository.get(
                            context,
                            session_id,
                            for_update=True,
                        )
                        operation = await repository.get_operation(
                            context,
                            operation_kind="commit",
                            idempotency_key_hash=operation_hash,
                            for_update=True,
                        )
                        if operation is None or operation.status != "in_progress":
                            raise AssetConflict(context.request_id)
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        self._require_revise_target_live(context, row)
                        self._require_expected_revision(
                            context,
                            row,
                            command.expected_revision,
                        )
                        if row.status != SkillDesignStatus.VALIDATED.value or row.draft_checksum != command.expected_draft_checksum or row.validated_draft_checksum != row.draft_checksum or row.validation_json is None:
                            raise AssetConflict(context.request_id)
                        validation = self._validation_from_json(
                            context,
                            row.validation_json,
                        )
                        files = await repository.load_draft_files(
                            context,
                            row.id,
                            for_update=True,
                        )
                        files = self._validate_builder_files(context, files)
                        preview = await self._skill_service.preview_archive(
                            context,
                            files,
                        )
                        self._require_preview_name(
                            context,
                            preview,
                            row.slug,
                        )
                        if not self._validation_matches_preview(
                            validation,
                            preview,
                        ):
                            raise AssetConflict(context.request_id)
                        if row.session_kind == "revise":
                            if row.target_skill_deleted or row.target_skill_id is None or row.base_version_id is None:
                                raise SkillDesignTargetDeleted(context.request_id)
                            if target_asset is None or target_asset.id != row.target_skill_id or target_asset.status not in {"active", "suspended"}:
                                raise AssetConflict(context.request_id)
                            base_metadata = await repository.load_base_file_metadata(
                                context,
                                row.base_version_id,
                            )
                            base_identity = {(item.path, item.sha256, item.size_bytes, item.media_type) for item in base_metadata}
                            draft_identity = {(item.path, item.sha256, item.size_bytes, item.media_type) for item in preview.file_views}
                            if base_identity == draft_identity:
                                raise SkillDesignNoChanges(context.request_id)
                        row.status = SkillDesignStatus.COMMITTING.value
                        operation.updated_at = self._now()
                        row.progress_json = self._progress_json(
                            SkillDesignStatus.COMMITTING,
                        )
                        activity_repository = SkillDesignActivityRepository(
                            session,
                        )
                        await activity_repository.append(
                            context,
                            session_id=row.id,
                            operation_id=operation.id,
                            kind=(SkillDesignActivityKind.COMMIT_VALIDATION_PASSED),
                            source_event_id="commit-validation-passed",
                        )
                        await activity_repository.append(
                            context,
                            session_id=row.id,
                            operation_id=operation.id,
                            kind=(SkillDesignActivityKind.COMMIT_PERSISTENCE_STARTED),
                            source_event_id="commit-persistence-started",
                        )
                        await session.flush()
                commit_validation_passed = True
                commit_persistence_started = True

                # Asset/version persistence and the successful terminal Activity
                # remain one atomic transaction.
                async with self._session_factory() as session:
                    async with session.begin():
                        repository = self._repository_factory(session)
                        await repository.lock_context(context)
                        unlocked = await repository.get(context, session_id)
                        target_asset = None
                        if unlocked.session_kind == "revise" and not unlocked.target_skill_deleted and unlocked.target_skill_id is not None:
                            try:
                                target_asset = await SkillRepository(
                                    session,
                                ).get_project_asset(
                                    context,
                                    unlocked.target_skill_id,
                                    for_update=True,
                                )
                            except AssetNotFound:
                                target_asset = None
                        row = await repository.get(
                            context,
                            session_id,
                            for_update=True,
                        )
                        operation = await repository.get_operation(
                            context,
                            operation_kind="commit",
                            idempotency_key_hash=operation_hash,
                            for_update=True,
                        )
                        if operation is None or operation.status != "in_progress":
                            raise AssetConflict(context.request_id)
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        self._require_revise_target_live(context, row)
                        self._require_expected_revision(
                            context,
                            row,
                            command.expected_revision,
                        )
                        if row.status != SkillDesignStatus.COMMITTING.value or row.draft_checksum != command.expected_draft_checksum:
                            raise AssetConflict(context.request_id)
                        assert preview is not None
                        if row.session_kind == "revise":
                            if row.target_skill_id is None or row.base_version_id is None or target_asset is None or target_asset.id != row.target_skill_id or target_asset.status not in {"active", "suspended"}:
                                raise AssetConflict(context.request_id)
                            created_version = await self._skill_service.create_project_version_from_preview_in_session(
                                session,
                                context,
                                row.target_skill_id,
                                preview,
                                supersedes_version_id=row.base_version_id,
                            )
                            row.created_skill_id = row.target_skill_id
                            row.created_skill_version_id = created_version.id
                        else:
                            created_result = await self._skill_service.create_project_from_preview_in_session(
                                session,
                                context,
                                CreateSkill(
                                    slug=row.slug,
                                    display_name=row.display_name,
                                ),
                                preview,
                            )
                            row.created_skill_id = created_result.asset.id
                            row.created_skill_version_id = created_result.version.id
                        row.status = SkillDesignStatus.COMPLETED.value
                        row.revision += 1
                        row.progress_json = self._progress_json(
                            SkillDesignStatus.COMPLETED,
                        )
                        operation.status = "completed"
                        operation.result_revision = row.revision
                        activity_repository = SkillDesignActivityRepository(
                            session,
                        )
                        await activity_repository.append(
                            context,
                            session_id=row.id,
                            operation_id=operation.id,
                            kind=(SkillDesignActivityKind.COMMIT_PERSISTENCE_COMPLETED),
                            source_event_id="commit-persistence-completed",
                        )
                        await activity_repository.append(
                            context,
                            session_id=row.id,
                            operation_id=operation.id,
                            kind=SkillDesignActivityKind.COMMIT_TERMINAL,
                            payload={"status": "completed"},
                            source_event_id="commit-terminal",
                        )
                        await repository.clear_draft_files(context, row.id)
                        await session.flush()
                        repeated_session = await self._session_view_for_row(
                            repository,
                            context,
                            row,
                            (),
                        )
            if repeated_session is None:
                raise AssetStorageUnavailable(context.request_id)
            if created_result is not None:
                return SkillDesignCommitResult(
                    session=repeated_session,
                    skill=created_result.asset,
                    version=created_result.version,
                )
            if created_version is not None:
                skill = await self._skill_service.get(
                    context,
                    created_version.skill_id,
                )
                return SkillDesignCommitResult(
                    session=repeated_session,
                    skill=skill,
                    version=created_version,
                )
            if repeated_session.created_skill_id is None:
                raise AssetConflict(context.request_id)
            skill = await self._skill_service.get(
                context,
                repeated_session.created_skill_id,
            )
            replayed_version: SkillVersionView | None = None
            if repeated_session.created_skill_version_id is not None:
                replayed_version = await self._skill_service.get_project_version_view(
                    context,
                    repeated_session.created_skill_id,
                    repeated_session.created_skill_version_id,
                )
            return SkillDesignCommitResult(
                session=repeated_session,
                skill=skill,
                version=replayed_version,
            )
        except SharedAssetError as exc:
            if commit_started:
                await self._record_commit_failure(
                    context,
                    session_id=session_id,
                    operation_hash=operation_hash,
                    request_checksum=request_checksum,
                    public_error_code=exc.code,
                    validation_passed=commit_validation_passed,
                    persistence_started=commit_persistence_started,
                )
            raise
        except IntegrityError as exc:
            try:
                self._raise_integrity(context, exc)
            except SharedAssetError as domain_error:
                if commit_started:
                    await self._record_commit_failure(
                        context,
                        session_id=session_id,
                        operation_hash=operation_hash,
                        request_checksum=request_checksum,
                        public_error_code=domain_error.code,
                        validation_passed=commit_validation_passed,
                        persistence_started=commit_persistence_started,
                    )
                raise
        except DBAPIError:
            if commit_started:
                await self._record_commit_failure(
                    context,
                    session_id=session_id,
                    operation_hash=operation_hash,
                    request_checksum=request_checksum,
                    public_error_code=AssetStorageUnavailable.code,
                    validation_passed=commit_validation_passed,
                    persistence_started=commit_persistence_started,
                )
            raise AssetStorageUnavailable(context.request_id) from None
        except Exception:
            if commit_started:
                await self._record_commit_failure(
                    context,
                    session_id=session_id,
                    operation_hash=operation_hash,
                    request_checksum=request_checksum,
                    public_error_code=AssetStorageUnavailable.code,
                    validation_passed=commit_validation_passed,
                    persistence_started=commit_persistence_started,
                )
            raise

    async def cancel(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: CancelSkillDesignSession,
    ) -> SkillDesignSessionView:
        command = self._validate_cancel(context, command)
        self._require_capability(context, Capability.SHARED_ASSETS_EDIT)
        session_id = self._validate_uuid(context, session_id)
        operation_hash = self._idempotency_hash(command.idempotency_key)
        request_checksum = self._request_checksum(
            {
                "session_id": session_id,
                "expected_revision": command.expected_revision,
            }
        )
        effective_expected_revision = command.expected_revision
        current = await self.get(context, session_id)
        if current.status is SkillDesignStatus.GENERATING:
            if current.revision != command.expected_revision:
                raise AssetConflict(context.request_id)
            stopped = await self.stop_current_run(context, session_id)
            if stopped.status is SkillDesignStatus.GENERATING:
                # The Worker still owns the Provider call. Keep the session and
                # its Activity intact so a retry can finish the protected flow.
                raise AssetConflict(context.request_id)
            effective_expected_revision = stopped.revision
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="cancel",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is not None:
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        if operation.status == "completed":
                            return await self._session_view_for_row(
                                repository,
                                context,
                                row,
                                (),
                            )
                        raise AssetConflict(context.request_id)
                    if row.status == SkillDesignStatus.CANCELLED.value:
                        self._require_expected_revision(
                            context,
                            row,
                            command.expected_revision,
                        )
                        return await self._session_view_for_row(
                            repository,
                            context,
                            row,
                            (),
                        )
                    if row.status == SkillDesignStatus.COMPLETED.value:
                        raise AssetConflict(context.request_id)
                    self._require_expected_revision(
                        context,
                        row,
                        effective_expected_revision,
                    )
                    operation = self._new_operation(
                        context,
                        session_id,
                        kind="cancel",
                        idempotency_hash=operation_hash,
                        request_checksum=request_checksum,
                    )
                    await repository.create_operation(context, operation)
                    await repository.clear_draft_files(context, row.id)
                    await SkillDesignActivityRepository(session).clear_session(
                        context,
                        session_id=row.id,
                    )
                    await session.execute(
                        delete(SkillDesignOperationBaselineFileRow).where(
                            SkillDesignOperationBaselineFileRow.project_id == context.project_id,
                            SkillDesignOperationBaselineFileRow.owner_user_id == str(context.user_id),
                            SkillDesignOperationBaselineFileRow.session_id == row.id,
                        )
                    )
                    row.status = SkillDesignStatus.CANCELLED.value
                    row.messages_json = []
                    row.draft_checksum = None
                    row.authoring_dependencies_json = None
                    row.validation_json = None
                    row.validated_draft_checksum = None
                    row.active_clarification_json = None
                    row.error_code = None
                    row.error_message = None
                    row.execution_model_ref = None
                    row.execution_mode = None
                    row.execution_thinking_enabled = None
                    row.execution_reasoning_effort = None
                    row.progress_json = self._progress_json(SkillDesignStatus.CANCELLED)
                    row.revision += 1
                    await session.execute(
                        update(SkillDesignOperationRow)
                        .where(
                            SkillDesignOperationRow.project_id == context.project_id,
                            SkillDesignOperationRow.owner_user_id == str(context.user_id),
                            SkillDesignOperationRow.session_id == row.id,
                            SkillDesignOperationRow.operation_kind == "turn",
                            SkillDesignOperationRow.status == "in_progress",
                        )
                        .values(
                            status="stopped",
                            result_revision=row.revision,
                            public_error_code=None,
                            stop_requested_at=self._now(),
                        )
                    )
                    operation.status = "completed"
                    operation.result_revision = row.revision
                    await session.flush()
                    # PostgreSQL's shared ``updated_at`` trigger is authoritative.
                    # Refresh before building the response so the first result and
                    # an idempotent replay expose the same committed timestamp.
                    refresh = getattr(session, "refresh", None)
                    if refresh is not None:
                        await refresh(row)
                    return await self._session_view_for_row(
                        repository,
                        context,
                        row,
                        (),
                    )
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            self._raise_integrity(context, exc)
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def _apply_draft_update(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: SubmitSkillDesignTurn,
        *,
        operation_hash: str,
        request_checksum: str,
    ) -> SkillDesignSessionView:
        turn = command.input
        if not isinstance(turn, SkillDesignDraftUpdateTurn):
            raise AssetValidationFailed(context.request_id)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="turn",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is not None:
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        if operation.status == "completed":
                            files = await repository.load_draft_files(
                                context,
                                row.id,
                            )
                            return await self._session_view_for_row(
                                repository,
                                context,
                                row,
                                files,
                            )
                        raise AssetConflict(context.request_id)
                    self._require_revise_target_live(context, row)
                    self._require_expected_revision(
                        context,
                        row,
                        command.expected_revision,
                    )
                    self._require_nonterminal(context, row)
                    if (
                        row.status
                        not in {
                            SkillDesignStatus.DRAFT_READY.value,
                            SkillDesignStatus.VALIDATED.value,
                        }
                        or row.draft_checksum != turn.expected_draft_checksum
                    ):
                        raise AssetConflict(context.request_id)
                    self._require_message_capacity(
                        context,
                        row,
                        additional=1,
                    )
                    current_files = await repository.load_draft_files(
                        context,
                        row.id,
                        for_update=True,
                    )
                    snapshot = await self._skill_service.apply_draft_changes(
                        context,
                        current_files,
                        turn.changes,
                        expected_draft_checksum=turn.expected_draft_checksum,
                    )
                    files = self._validate_builder_files(
                        context,
                        snapshot.files,
                    )
                    await repository.replace_draft_files(
                        context,
                        row.id,
                        files,
                    )
                    operation = self._new_operation(
                        context,
                        session_id,
                        kind="turn",
                        idempotency_hash=operation_hash,
                        request_checksum=request_checksum,
                    )
                    await repository.create_operation(context, operation)
                    row.draft_checksum = snapshot.checksum
                    row.authoring_dependencies_json = None
                    row.validation_json = None
                    row.validated_draft_checksum = None
                    row.status = SkillDesignStatus.DRAFT_READY.value
                    row.active_clarification_json = None
                    row.error_code = None
                    row.error_message = None
                    row.progress_json = self._progress_json(SkillDesignStatus.DRAFT_READY)
                    self._append_row_message(
                        context,
                        row,
                        "user",
                        f"已手动更新候选文件包（{len(turn.changes)} 项变更）。",
                    )
                    row.revision += 1
                    operation.status = "completed"
                    operation.result_revision = row.revision
                    await session.flush()
                    return await self._session_view_for_row(
                        repository,
                        context,
                        row,
                        files,
                    )
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            self._raise_integrity(context, exc)
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def _prepare_generation(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        command: SubmitSkillDesignTurn,
        *,
        operation_hash: str,
        request_checksum: str,
    ) -> SkillDesignSessionView | SkillBuilderRunAdmission | tuple[int, SkillDesignGenerationRequest, str]:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="turn",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is not None:
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        if operation.status == "completed":
                            files = await repository.load_draft_files(
                                context,
                                row.id,
                            )
                            return await self._session_view_for_row(
                                repository,
                                context,
                                row,
                                files,
                            )
                        if operation.status == "in_progress":
                            linked_run = await repository.linked_run_for_operation(
                                context,
                                operation,
                                lock=True,
                            )
                            if linked_run is not None and linked_run.status in {
                                "pending",
                                "running",
                            }:
                                return SkillBuilderRunAdmission(
                                    run_id=linked_run.run_id,
                                    status=linked_run.status,
                                    thread_id=linked_run.thread_id,
                                )
                            if linked_run is not None:
                                code = (
                                    linked_run.error
                                    if isinstance(linked_run.error, str)
                                    and _PUBLIC_ERROR_PATTERN.fullmatch(
                                        linked_run.error,
                                    )
                                    else SkillDesignServiceErrorCode.GENERATION_INTERRUPTED.value
                                )
                                row.status = SkillDesignStatus.FAILED.value
                                row.validation_json = None
                                row.validated_draft_checksum = None
                                row.active_clarification_json = None
                                row.error_code = code
                                row.error_message = self._stable_generation_error_message(
                                    code,
                                )
                                row.progress_json = self._progress_json(
                                    SkillDesignStatus.FAILED,
                                )
                                row.revision += 1
                                operation.status = "failed"
                                operation.result_revision = row.revision
                                operation.public_error_code = code
                                await session.flush()
                                files = await repository.load_draft_files(
                                    context,
                                    row.id,
                                )
                                return await self._session_view_for_row(
                                    repository,
                                    context,
                                    row,
                                    files,
                                )
                            if not self._is_stale_generating(
                                row,
                                now=self._now(),
                            ):
                                raise AssetConflict(context.request_id)
                            recovered = await self._recover_stale_generating(
                                repository,
                                context,
                                row,
                                now=self._now(),
                            )
                            if not recovered:
                                raise AssetConflict(context.request_id)
                            files = await repository.load_draft_files(
                                context,
                                row.id,
                            )
                            return await self._session_view_for_row(
                                repository,
                                context,
                                row,
                                files,
                            )
                        if operation.status == "failed":
                            files = await repository.load_draft_files(
                                context,
                                row.id,
                            )
                            return await self._session_view_for_row(
                                repository,
                                context,
                                row,
                                files,
                            )
                        raise AssetConflict(context.request_id)
                    else:
                        self._require_revise_target_live(context, row)
                        self._require_expected_revision(
                            context,
                            row,
                            command.expected_revision,
                        )
                        if row.status == SkillDesignStatus.GENERATING.value:
                            if not self._is_stale_generating(
                                row,
                                now=self._now(),
                            ):
                                raise AssetConflict(context.request_id)
                            await self._recover_stale_generating(
                                repository,
                                context,
                                row,
                                now=self._now(),
                            )
                        self._require_nonterminal(context, row)
                        self._require_message_capacity(
                            context,
                            row,
                            additional=2,
                        )
                        operation = self._new_operation(
                            context,
                            session_id,
                            kind="turn",
                            idempotency_hash=operation_hash,
                            request_checksum=request_checksum,
                        )
                        await repository.create_operation(context, operation)
                    turn_message = self._append_turn_input(
                        context,
                        row,
                        command.input,
                        operation_id=operation.id,
                    )
                    row.status = SkillDesignStatus.GENERATING.value
                    row.active_clarification_json = None
                    row.validation_json = None
                    row.validated_draft_checksum = None
                    row.error_code = None
                    row.error_message = None
                    row.progress_json = self._progress_json(SkillDesignStatus.GENERATING)
                    row.revision += 1
                    self._reset_operation(operation)
                    files = await repository.load_draft_files(
                        context,
                        row.id,
                    )
                    files = self._validate_partial_builder_files(
                        context,
                        files,
                    )
                    creator = await repository.load_pinned_skill_creator(
                        context,
                        row,
                    )
                    request = SkillDesignGenerationRequest(
                        skill_slug=row.slug,
                        skill_name=row.display_name,
                        brief=self._conversation_brief(
                            context,
                            row.messages_json,
                        ),
                        current_files=tuple(
                            SkillDesignGeneratedFile(
                                path=item.path,
                                media_type=item.media_type,
                                content=item.content.decode("utf-8"),
                            )
                            for item in files
                        ),
                        attachments=tuple(
                            SkillDesignAttachment(
                                name=item.name,
                                content=item.content,
                            )
                            for item in getattr(command.input, "attachments", ())
                        ),
                    )
                    if self._run_admission is not None:
                        await repository.capture_operation_baseline(
                            context,
                            session_id=row.id,
                            operation_id=operation.id,
                        )
                        explicit_model = getattr(command.input, "model_name", None)
                        explicit_thinking = getattr(
                            command.input,
                            "thinking_enabled",
                            None,
                        )
                        explicit_effort = getattr(
                            command.input,
                            "reasoning_effort",
                            None,
                        )
                        explicit_profile = any(
                            value is not None
                            for value in (
                                explicit_model,
                                getattr(command.input, "mode", None),
                                explicit_thinking,
                                explicit_effort,
                            )
                        )
                        admission = await self._run_admission.admit_in_session(
                            session,
                            context,
                            row,
                            operation,
                            request,
                            turn_message=turn_message,
                            model_name=(explicit_model or row.execution_model_ref),
                            thinking_enabled=(explicit_thinking if explicit_profile else row.execution_thinking_enabled),
                            reasoning_effort=(explicit_effort if explicit_profile else row.execution_reasoning_effort),
                        )
                        await SkillDesignActivityRepository(session).append(
                            context,
                            session_id=row.id,
                            operation_id=operation.id,
                            run_id=admission.run_id,
                            kind=SkillDesignActivityKind.REQUEST_ACCEPTED,
                            source_event_id="request-accepted",
                        )
                        await session.flush()
                        return admission
                    await SkillDesignActivityRepository(session).append(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                        kind=SkillDesignActivityKind.REQUEST_ACCEPTED,
                        source_event_id="request-accepted",
                    )
                    await session.flush()
                    return (
                        row.revision,
                        request,
                        creator.skill_md_content,
                    )
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            self._raise_integrity(context, exc)
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def _finish_generation_success(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        operation_hash: str,
        generation_revision: int,
        result: SkillDesignGenerationResult,
        preview: SkillArchivePreview | None,
    ) -> SkillDesignSessionView:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="turn",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is None or operation.status != "in_progress" or row.status != SkillDesignStatus.GENERATING.value or row.revision != generation_revision:
                        raise AssetConflict(context.request_id)
                    if isinstance(result, NeedsClarificationResult):
                        clarification = self._clarification_request(result.questions[0])
                        row.status = SkillDesignStatus.AWAITING_CLARIFICATION.value
                        row.active_clarification_json = self._clarification_json(clarification)
                        row.progress_json = self._progress_json(SkillDesignStatus.AWAITING_CLARIFICATION)
                        self._append_row_message(
                            context,
                            row,
                            "assistant",
                            clarification.question,
                        )
                        files = await repository.load_draft_files(
                            context,
                            row.id,
                        )
                    elif isinstance(result, CandidateResult):
                        if preview is None:
                            raise AssetValidationFailed(context.request_id)
                        files = self._validate_builder_files(
                            context,
                            preview.files,
                        )
                        await repository.replace_draft_files(
                            context,
                            row.id,
                            files,
                        )
                        row.draft_checksum = preview.checksum
                        row.validation_json = None
                        row.validated_draft_checksum = None
                        row.status = SkillDesignStatus.DRAFT_READY.value
                        row.active_clarification_json = None
                        row.progress_json = self._progress_json(SkillDesignStatus.DRAFT_READY)
                        self._append_row_message(
                            context,
                            row,
                            "assistant",
                            result.summary,
                        )
                    else:
                        raise AssetValidationFailed(context.request_id)
                    row.error_code = None
                    row.error_message = None
                    row.revision += 1
                    operation.status = "completed"
                    operation.result_revision = row.revision
                    operation.public_error_code = None
                    await SkillDesignActivityRepository(session).append(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                        kind=SkillDesignActivityKind.RUN_TERMINAL,
                        payload={"status": "completed"},
                        source_event_id="run-terminal",
                    )
                    await session.flush()
                    return await self._session_view_for_row(
                        repository,
                        context,
                        row,
                        files,
                    )
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def _finish_generation_failure(
        self,
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        operation_hash: str,
        generation_revision: int,
        error_code: str,
        error_message: str,
    ) -> SkillDesignSessionView:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="turn",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    row = await repository.get(
                        context,
                        session_id,
                        for_update=True,
                    )
                    if operation is None or operation.status != "in_progress" or row.status != SkillDesignStatus.GENERATING.value or row.revision != generation_revision:
                        files = await repository.load_draft_files(
                            context,
                            row.id,
                        )
                        return await self._session_view_for_row(
                            repository,
                            context,
                            row,
                            files,
                        )
                    row.status = SkillDesignStatus.FAILED.value
                    row.active_clarification_json = None
                    row.error_code = error_code
                    row.error_message = error_message
                    row.progress_json = self._progress_json(SkillDesignStatus.FAILED)
                    self._append_row_message(
                        context,
                        row,
                        "assistant",
                        error_message,
                    )
                    row.revision += 1
                    operation.status = "failed"
                    operation.result_revision = row.revision
                    operation.public_error_code = error_code
                    await SkillDesignActivityRepository(session).append(
                        context,
                        session_id=row.id,
                        operation_id=operation.id,
                        kind=SkillDesignActivityKind.RUN_TERMINAL,
                        payload={
                            "status": "failed",
                            "code": error_code,
                        },
                        source_event_id="run-terminal",
                    )
                    await session.flush()
                    files = await repository.load_draft_files(
                        context,
                        row.id,
                    )
                    return await self._session_view_for_row(
                        repository,
                        context,
                        row,
                        files,
                    )
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    async def _recover_stale_generating(
        self,
        repository: SkillDesignRepository,
        context: ProjectContext,
        row: SkillDesignSessionRow,
        *,
        now: datetime,
    ) -> bool:
        if not self._is_stale_generating(row, now=now):
            return False
        linked_run = await repository.latest_linked_run(
            context,
            row.id,
            lock=True,
        )
        if linked_run is not None and linked_run.status in {"pending", "running"}:
            return False
        operation = (
            await repository.session.execute(
                select(SkillDesignOperationRow)
                .where(
                    SkillDesignOperationRow.project_id == context.project_id,
                    SkillDesignOperationRow.owner_user_id == str(context.user_id),
                    SkillDesignOperationRow.session_id == row.id,
                    SkillDesignOperationRow.operation_kind == "turn",
                    SkillDesignOperationRow.status == "in_progress",
                )
                .order_by(SkillDesignOperationRow.created_at.desc())
                .limit(1)
                .with_for_update(of=SkillDesignOperationRow)
            )
        ).scalar_one_or_none()
        code = SkillDesignServiceErrorCode.GENERATION_INTERRUPTED.value
        if operation is not None:
            has_new_activity_contract = await repository.session.scalar(
                select(SkillDesignActivityRow.seq)
                .where(
                    SkillDesignActivityRow.operation_id == operation.id,
                    SkillDesignActivityRow.kind == SkillDesignActivityKind.REQUEST_ACCEPTED.value,
                )
                .limit(1)
            )
            if has_new_activity_contract is not None:
                files = await repository.restore_operation_baseline(
                    context,
                    session_id=row.id,
                    operation_id=operation.id,
                )
                row.draft_checksum = self._draft_snapshot(
                    context,
                    files,
                ).draft_checksum
                await SkillDesignActivityRepository(repository.session).append(
                    context,
                    session_id=row.id,
                    operation_id=operation.id,
                    run_id=operation.run_id,
                    kind=SkillDesignActivityKind.RUN_TERMINAL,
                    payload={"status": "failed", "code": code},
                    source_event_id="run-terminal",
                )
                await repository.clear_operation_baseline(
                    context,
                    session_id=row.id,
                    operation_id=operation.id,
                )
        row.status = SkillDesignStatus.FAILED.value
        row.validation_json = None
        row.validated_draft_checksum = None
        row.error_code = code
        row.error_message = "上一次生成已中断，请重新发送你的要求。"
        row.progress_json = self._progress_json(SkillDesignStatus.FAILED)
        row.active_clarification_json = None
        row.revision += 1
        if operation is not None:
            operation.status = "failed"
            operation.result_revision = row.revision
            operation.public_error_code = code
        await repository.session.flush()
        return True

    async def _recover_stale_commit(
        self,
        repository: SkillDesignRepository,
        context: ProjectContext,
        row: SkillDesignSessionRow,
        *,
        now: datetime,
    ) -> bool:
        if row.status not in {
            SkillDesignStatus.VALIDATED.value,
            SkillDesignStatus.COMMITTING.value,
        }:
            return False
        operations = tuple(
            (
                await repository.session.execute(
                    select(SkillDesignOperationRow)
                    .where(
                        SkillDesignOperationRow.project_id == context.project_id,
                        SkillDesignOperationRow.owner_user_id == str(context.user_id),
                        SkillDesignOperationRow.session_id == row.id,
                        SkillDesignOperationRow.operation_kind == "commit",
                        SkillDesignOperationRow.status == "in_progress",
                    )
                    .order_by(SkillDesignOperationRow.created_at.asc())
                    .with_for_update(of=SkillDesignOperationRow)
                )
            )
            .scalars()
            .all()
        )
        stale: list[SkillDesignOperationRow] = []
        has_fresh = False
        for operation in operations:
            updated_at = operation.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if now - updated_at >= self._stale_after:
                stale.append(operation)
            else:
                has_fresh = True
        if not stale:
            return False
        code = SkillDesignServiceErrorCode.COMMIT_INTERRUPTED.value
        activity_repository = SkillDesignActivityRepository(repository.session)
        for operation in stale:
            operation.status = "failed"
            operation.result_revision = row.revision
            operation.public_error_code = code
            await activity_repository.append(
                context,
                session_id=row.id,
                operation_id=operation.id,
                kind=SkillDesignActivityKind.COMMIT_TERMINAL,
                payload={"status": "failed", "code": code},
                source_event_id="commit-terminal",
            )
        if row.status == SkillDesignStatus.COMMITTING.value and not has_fresh:
            row.status = SkillDesignStatus.VALIDATED.value
            row.progress_json = self._progress_json(SkillDesignStatus.VALIDATED)
        await repository.session.flush()
        return True

    def _is_stale_generating(
        self,
        row: SkillDesignSessionRow,
        *,
        now: datetime,
    ) -> bool:
        if row.status != SkillDesignStatus.GENERATING.value:
            return False
        updated_at = row.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return now - updated_at >= self._stale_after

    @staticmethod
    def _new_operation(
        context: ProjectContext,
        session_id: uuid.UUID,
        *,
        kind: str,
        idempotency_hash: str,
        request_checksum: str,
    ) -> SkillDesignOperationRow:
        return SkillDesignOperationRow(
            id=uuid.uuid4(),
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            session_id=session_id,
            operation_kind=kind,
            idempotency_key_hash=idempotency_hash,
            request_checksum=request_checksum,
            status="in_progress",
            result_revision=None,
            public_error_code=None,
        )

    @staticmethod
    def _reset_operation(operation: SkillDesignOperationRow) -> None:
        operation.status = "in_progress"
        operation.result_revision = None
        operation.public_error_code = None

    @staticmethod
    def _append_turn_input(
        context: ProjectContext,
        row: SkillDesignSessionRow,
        turn: SkillDesignTurn,
        *,
        operation_id: uuid.UUID,
    ) -> str:
        SkillDesignService._require_message_capacity(
            context,
            row,
            additional=2,
        )
        if isinstance(turn, SkillDesignMessageTurn):
            content = turn.message
            if turn.attachments:
                names = "、".join(item.name for item in turn.attachments)
                content = f"{content}\n\n[附带 {len(turn.attachments)} 个参考文件：{names}]"
        elif isinstance(turn, SkillDesignClarificationTurn):
            SkillDesignService._require_matching_clarification_response(
                context,
                row,
                turn.response,
            )
            content = turn.response.value
        else:
            raise AssetValidationFailed(context.request_id)
        SkillDesignService._append_row_message(
            context,
            row,
            "user",
            content,
            operation_id=operation_id,
        )
        row.active_clarification_json = None
        return content

    @staticmethod
    def _session_view(
        context: ProjectContext,
        row: SkillDesignSessionRow,
        files: tuple[SkillArchiveFile, ...],
        *,
        active_run: SkillBuilderRunAdmission | None = None,
        base_files: tuple[SkillDesignBaseFile, ...] = (),
    ) -> SkillDesignSessionView:
        if contains_secret_like_material(row.messages_json) or (row.active_clarification_json is not None and contains_secret_like_material(row.active_clarification_json)):
            raise AssetValidationFailed(context.request_id)
        try:
            messages = tuple(
                SkillDesignMessage(
                    id=item["id"],
                    role=item["role"],
                    content=item["content"],
                    created_at=datetime.fromisoformat(item["created_at"]),
                    operation_id=(uuid.UUID(item["operation_id"]) if item.get("operation_id") is not None else None),
                )
                for item in row.messages_json
            )
            progress = tuple(
                SkillDesignProgressItem(
                    id=item["id"],
                    label=item["label"],
                    status=SkillDesignProgressStatus(item["status"]),
                )
                for item in row.progress_json
            )
            active = (
                SkillDesignService._clarification_from_json(
                    context,
                    row.active_clarification_json,
                )
                if row.active_clarification_json is not None
                else None
            )
            validation = (
                SkillDesignService._validation_from_json(
                    context,
                    row.validation_json,
                )
                if row.validation_json is not None
                else None
            )
            authoring_dependencies = (
                SkillBuilderDependencySnapshot.model_validate_json(
                    json.dumps(
                        row.authoring_dependencies_json,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    strict=True,
                )
                if row.authoring_dependencies_json is not None
                else None
            )
            file_views = SkillDesignService._file_views(context, files)
            return SkillDesignSessionView(
                id=row.id,
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                thread_id=row.thread_id,
                slug=row.slug,
                display_name=row.display_name,
                status=SkillDesignStatus(row.status),
                revision=row.revision,
                messages=messages,
                active_clarification=active,
                progress=progress,
                files=file_views,
                draft_checksum=row.draft_checksum,
                validation=validation,
                error_code=row.error_code,
                error_message=row.error_message,
                created_skill_id=row.created_skill_id,
                created_skill_version_id=row.created_skill_version_id,
                created_at=row.created_at,
                updated_at=row.updated_at,
                active_run=active_run,
                authoring_dependencies=authoring_dependencies,
                session_kind=row.session_kind,
                target_skill_id=row.target_skill_id,
                base_version_id=row.base_version_id,
                base_version_number=row.base_version_number,
                base_payload_checksum=row.base_payload_checksum,
                target_skill_deleted=row.target_skill_deleted,
                base_files=base_files,
                execution_preference=(
                    SkillDesignExecutionPreference(
                        model_name=row.execution_model_ref,
                        mode=row.execution_mode,
                        thinking_enabled=row.execution_thinking_enabled,
                        reasoning_effort=row.execution_reasoning_effort,
                    )
                    if row.execution_model_ref is not None and row.execution_mode is not None and row.execution_thinking_enabled is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise AssetValidationFailed(context.request_id) from None

    @staticmethod
    def _file_views(
        context: ProjectContext,
        files: tuple[SkillArchiveFile, ...],
    ) -> tuple[SkillDesignFileView, ...]:
        views: list[SkillDesignFileView] = []
        for item in SkillDesignService._validate_partial_builder_files(
            context,
            files,
        ):
            try:
                content = item.content.decode("utf-8")
            except UnicodeDecodeError:
                raise AssetValidationFailed(context.request_id) from None
            if contains_secret_like_material(content):
                raise AssetValidationFailed(context.request_id)
            views.append(
                SkillDesignFileView(
                    path=item.path,
                    media_type=item.media_type,
                    size_bytes=len(item.content),
                    sha256=hashlib.sha256(item.content).hexdigest(),
                    encoding="utf-8",
                    content=content,
                )
            )
        return tuple(views)

    @staticmethod
    def _raise_integrity(
        context: ProjectContext,
        exc: IntegrityError,
    ) -> None:
        constraint = _constraint_name(exc)
        if constraint == "uq_skill_design_sessions_live_revise_target":
            raise SkillDesignTargetSessionExists(context.request_id) from None
        if constraint in _CONFLICT_CONSTRAINTS:
            raise AssetConflict(context.request_id) from None
        raise AssetStorageUnavailable(context.request_id) from None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)


__all__ = [
    "CancelSkillDesignSession",
    "CommitSkillDesignSession",
    "CreateSkillDesignRevisionSession",
    "CreateSkillDesignSession",
    "MAX_INCOMPLETE_SKILL_DESIGN_SESSIONS_PER_OWNER_PROJECT",
    "SkillDesignBaseFile",
    "SkillDesignClarificationResponse",
    "SkillDesignClarificationTurn",
    "SkillDesignCommitResult",
    "SkillDesignDraftUpdateTurn",
    "SkillDesignFileView",
    "SkillDesignMessageTurn",
    "SkillDesignProgressItem",
    "SkillDesignSecretRequirement",
    "SkillDesignService",
    "SkillDesignSessionSummary",
    "SkillDesignSessionView",
    "SkillDesignStatus",
    "SkillDesignTurnAttachment",
    "SkillDesignValidation",
    "SubmitSkillDesignTurn",
    "ValidateSkillDesignSession",
]
