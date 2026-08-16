"""Project-scoped conversational Skill Builder orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.run_repository import (
    PrivateRunExecutionLeaseLost,
    PrivateRunRepository,
)
from app.private_work.run_service import PrivateRunAuditPort, PrivateRunQuotaPort
from app.projects.capabilities import Capability
from app.projects.context import (
    ProjectContext,
    resolve_project_context_in_transaction,
)
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageQuotaExceeded,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
    SkillDesignBaseStale,
    SkillDesignNoChanges,
    SkillDesignTargetDeleted,
    SkillDesignTargetSessionExists,
    SkillDesignTargetUnsupported,
)
from app.shared_assets.model_refs import exact_model_ref
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.project_authoring_catalog import (
    ProjectAuthoringCatalogRepository,
)
from app.shared_assets.skill_builder_admission_contract import (
    SkillBuilderRunAdmission,
    SkillBuilderRunAdmissionPort,
)
from app.shared_assets.skill_builder_contract import (
    SkillBuilderCandidateFileChunk,
    SkillBuilderCandidateFileDelete,
    SkillBuilderCandidateFileList,
    SkillBuilderCandidateFileRead,
    SkillBuilderCandidateFileUpsert,
    SkillBuilderCandidateFinalize,
    SkillBuilderDraftFileMetadata,
    SkillBuilderDraftFilePage,
    SkillBuilderDraftMutationReceipt,
    SkillBuilderDraftSink,
    SkillBuilderTerminalReceipt,
    _canonical_candidate_path,
)
from app.shared_assets.skill_design_generation import (
    DEFAULT_SKILL_DESIGN_TIMEOUT_SECONDS,
    MAX_SKILL_DESIGN_ATTACHMENTS,
    MAX_SKILL_DESIGN_ATTACHMENTS_TOTAL_BYTES,
    MAX_SKILL_DESIGN_BRIEF_CHARS,
    SKILL_DESIGN_REASONING_EFFORTS,
    CandidateResult,
    ClarificationQuestion,
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
from app.shared_assets.skill_repository import SkillRepository, SkillVersionRecord
from app.shared_assets.skill_service import (
    CreateSkill,
    ProjectSkillArchiveCreateResult,
    SkillArchivePreview,
    SkillAssetView,
    SkillFileChange,
    SkillService,
    SkillVersionView,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.shared_assets import (
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked

_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_CHECKSUM_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PUBLIC_ERROR_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_MAX_IDEMPOTENCY_KEY_CHARS = 255
_MAX_MESSAGE_CHARS = 8_000
_MAX_SESSION_MESSAGES = 128
_MAX_DISPLAY_NAME_CHARS = 120
_MAX_BUILDER_FILES = 128
_MAX_BUILDER_FILE_BYTES = 512 * 1024
_MAX_BUILDER_TOTAL_BYTES = 2 * 1024 * 1024
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


class SkillDesignStatus(StrEnum):
    INTERVIEWING = "interviewing"
    GENERATING = "generating"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    DRAFT_READY = "draft_ready"
    VALIDATED = "validated"
    COMMITTING = "committing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SkillDesignProgressStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SkillDesignServiceErrorCode(StrEnum):
    GENERATION_INTERRUPTED = "SKILL_DESIGN_GENERATION_INTERRUPTED"
    GENERATION_UNAVAILABLE = "SKILL_DESIGN_GENERATION_UNAVAILABLE"
    INVALID_MODEL_OUTPUT = "SKILL_DESIGN_INVALID_MODEL_OUTPUT"


@dataclass(frozen=True, slots=True)
class CreateSkillDesignSession:
    slug: str
    display_name: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CreateSkillDesignRevisionSession:
    """Open a Builder session seeded from an existing Skill's published base."""

    skill_id: uuid.UUID
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SkillDesignMessage:
    id: str
    role: str
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SkillDesignProgressItem:
    id: str
    label: str
    status: SkillDesignProgressStatus


@dataclass(frozen=True, slots=True)
class SkillDesignClarificationOption:
    id: str
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class SkillDesignClarificationRequest:
    version: int
    kind: str
    source: str
    request_id: str
    clarification_type: str
    title: str
    question: str
    context: str
    input_mode: str
    options: tuple[SkillDesignClarificationOption, ...]


@dataclass(frozen=True, slots=True)
class SkillDesignClarificationResponse:
    version: int
    kind: str
    source: str
    request_id: str
    response_kind: str
    value: str
    option_id: str | None = None


@dataclass(frozen=True, slots=True)
class SkillDesignTurnAttachment:
    """User-uploaded UTF-8 reference file scoped to one message turn."""

    name: str
    content: str


@dataclass(frozen=True, slots=True)
class SkillDesignMessageTurn:
    kind: str
    message: str
    model_name: str | None = None
    reasoning_effort: str | None = None
    attachments: tuple[SkillDesignTurnAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillDesignClarificationTurn:
    kind: str
    response: SkillDesignClarificationResponse
    model_name: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class SkillDesignDraftUpdateTurn:
    kind: str
    expected_draft_checksum: str
    changes: tuple[SkillFileChange, ...]


SkillDesignTurn = SkillDesignMessageTurn | SkillDesignClarificationTurn | SkillDesignDraftUpdateTurn


@dataclass(frozen=True, slots=True)
class SubmitSkillDesignTurn:
    input: SkillDesignTurn
    expected_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ValidateSkillDesignSession:
    expected_revision: int
    expected_draft_checksum: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CommitSkillDesignSession:
    expected_revision: int
    expected_draft_checksum: str
    acknowledge_warnings: bool
    idempotency_key: str
    acknowledge_base_stale: bool = False


@dataclass(frozen=True, slots=True)
class CancelSkillDesignSession:
    expected_revision: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SkillDesignFileView:
    path: str
    media_type: str
    size_bytes: int
    sha256: str
    encoding: str
    content: str


@dataclass(frozen=True, slots=True)
class SkillDesignBaseFile:
    """Pinned base-version file identity used for revision diff rendering."""

    path: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SkillDesignSecretRequirement:
    name: str
    optional: bool


@dataclass(frozen=True, slots=True)
class SkillDesignValidation:
    draft_checksum: str
    validated_at: datetime
    description: str
    frontmatter: Mapping[str, object]
    compatibility: str | None
    secret_requirements: tuple[SkillDesignSecretRequirement, ...]
    scan_decision: str
    scan_rule_ids: tuple[str, ...]
    scan_summary: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SkillDesignSessionView:
    id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: uuid.UUID
    slug: str
    display_name: str
    status: SkillDesignStatus
    revision: int
    messages: tuple[SkillDesignMessage, ...]
    active_clarification: SkillDesignClarificationRequest | None
    progress: tuple[SkillDesignProgressItem, ...]
    files: tuple[SkillDesignFileView, ...]
    draft_checksum: str | None
    validation: SkillDesignValidation | None
    error_code: str | None
    error_message: str | None
    created_skill_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    active_run: SkillBuilderRunAdmission | None = None
    authoring_dependencies: SkillBuilderDependencySnapshot | None = None
    session_kind: str = "create"
    target_skill_id: uuid.UUID | None = None
    base_version_id: uuid.UUID | None = None
    base_version_number: int | None = None
    base_payload_checksum: str | None = None
    target_skill_deleted: bool = False
    base_files: tuple[SkillDesignBaseFile, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillDesignSessionSummary:
    id: uuid.UUID
    slug: str
    display_name: str
    status: SkillDesignStatus
    revision: int
    updated_at: datetime
    session_kind: str = "create"


@dataclass(frozen=True, slots=True)
class SkillDesignCommitResult:
    session: SkillDesignSessionView
    skill: SkillAssetView
    version: SkillVersionView | None = None


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


class _SkillDesignTerminalSink:
    def __init__(
        self,
        service: SkillDesignService,
        context: PrivateWorkContext,
        claim: JobClaim,
    ) -> None:
        self._service = service
        self._context = context
        self._claim = claim

    async def request_clarification(
        self,
        result: NeedsClarificationResult,
    ) -> SkillBuilderTerminalReceipt:
        return await self._service.finish_agent_clarification(
            self._context,
            self._claim,
            result,
        )

    async def list_candidate_files(
        self,
        request: SkillBuilderCandidateFileList,
    ) -> SkillBuilderDraftFilePage:
        return await self._service.list_agent_draft(
            self._context,
            self._claim,
            request,
        )

    async def read_candidate_file(
        self,
        request: SkillBuilderCandidateFileRead,
    ) -> SkillBuilderCandidateFileChunk:
        return await self._service.read_agent_draft_file(
            self._context,
            self._claim,
            request,
        )

    async def upsert_candidate_file(
        self,
        request: SkillBuilderCandidateFileUpsert,
    ) -> SkillBuilderDraftMutationReceipt:
        return await self._service.upsert_agent_draft_file(
            self._context,
            self._claim,
            request,
        )

    async def delete_candidate_file(
        self,
        request: SkillBuilderCandidateFileDelete,
    ) -> SkillBuilderDraftMutationReceipt:
        return await self._service.delete_agent_draft_file(
            self._context,
            self._claim,
            request,
        )

    async def finalize_candidate(
        self,
        request: SkillBuilderCandidateFinalize,
        dependencies: SkillBuilderDependencySnapshot,
    ) -> SkillBuilderTerminalReceipt:
        return await self._service.finalize_agent_candidate(
            self._context,
            self._claim,
            request,
            dependencies,
        )


@dataclass(frozen=True, slots=True)
class _BuilderToolTransaction:
    context: ProjectContext
    repository: SkillDesignRepository
    operation: SkillDesignOperationRow
    design: SkillDesignSessionRow


@dataclass(frozen=True, slots=True)
class _SkillBuilderDraftState:
    draft_checksum: str | None
    files: tuple[SkillBuilderDraftFileMetadata, ...]
    total_size_bytes: int


class SkillDesignService:
    """Coordinate private Skill Builder sessions and atomic import."""

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
                    if target.status not in {"active", "suspended"} or target.current_published_version_id is None:
                        raise AssetConflict(context.request_id)
                    if await repository.live_revision_session_exists(
                        context,
                        target.id,
                    ):
                        raise SkillDesignTargetSessionExists(context.request_id)
                    record = await skill_repository.get_project_version(
                        context,
                        target.id,
                        target.current_published_version_id,
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
            files = SkillService._verified_archive_files(record, context.request_id)
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
        """Re-run frontmatter parsing and SkillScan under current rules."""

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
                    rows = await repository.list_incomplete(context, limit=limit)
                    now = self._now()
                    result: list[SkillDesignSessionSummary] = []
                    for listed in rows:
                        row = listed
                        if Capability.SHARED_ASSETS_EDIT in context.capabilities and self._is_stale_generating(row, now=now):
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

    @asynccontextmanager
    async def _builder_tool_transaction(
        self,
        context: PrivateWorkContext,
        claim: JobClaim,
    ) -> AsyncIterator[_BuilderToolTransaction]:
        """Revalidate one model-facing Builder tool under the live Job lease."""

        context = require_issued_private_work_context(context)
        if claim.run_id is None or claim.scope.project_id != context.project_id or claim.scope.owner_user_id != str(context.user_id):
            raise AuthorizationRevoked
        try:
            async with self._session_factory() as session, session.begin():
                current = await resolve_project_context_in_transaction(
                    session,
                    context.user_id,
                    context.project_id,
                    context.request_id,
                    lock=True,
                )
                current.require(Capability.SHARED_ASSETS_READ)
                current.require(Capability.SHARED_ASSETS_EDIT)
                repository = self._repository_factory(session)
                operation = await repository.operation_by_run(
                    current,
                    claim.run_id,
                    for_update=True,
                )
                if operation is None:
                    raise AuthorizationRevoked
                design = await repository.get(
                    current,
                    operation.session_id,
                    for_update=True,
                )
                if design.session_kind == "revise" and (design.target_skill_deleted or design.target_skill_id is None):
                    # The revise target was deleted mid-run; fail closed at
                    # the next tool boundary so the Run settles as failed.
                    raise AuthorizationRevoked
                cancel_requested = await PrivateRunRepository(
                    session,
                ).assert_execution_active(
                    scope=context.resource_scope,
                    run_id=claim.run_id,
                    job_id=claim.job_id,
                    lease_token=claim.lease_token,
                )
                if cancel_requested or operation.run_id != claim.run_id:
                    raise AuthorizationRevoked
                yield _BuilderToolTransaction(
                    context=current,
                    repository=repository,
                    operation=operation,
                    design=design,
                )
                await session.flush()
        except asyncio.CancelledError:
            raise
        except AuthorizationRevoked:
            raise
        except (
            ProjectNotFound,
            ProjectForbidden,
            PrivateRunExecutionLeaseLost,
        ):
            raise AuthorizationRevoked from None
        except SharedAssetError:
            raise
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

    @staticmethod
    def _require_builder_tool_in_progress(
        transaction: _BuilderToolTransaction,
    ) -> None:
        if transaction.operation.status != "in_progress" or transaction.design.status != SkillDesignStatus.GENERATING.value:
            raise AuthorizationRevoked

    @staticmethod
    def _draft_snapshot(
        context: ProjectContext,
        files: tuple[SkillArchiveFile, ...],
    ) -> _SkillBuilderDraftState:
        files = SkillDesignService._validate_partial_builder_files(
            context,
            files,
        )
        metadata = tuple(
            SkillBuilderDraftFileMetadata(
                path=item.path,
                media_type=item.media_type,
                size_bytes=len(item.content),
                sha256=hashlib.sha256(item.content).hexdigest(),
            )
            for item in files
        )
        checksum = SkillDesignService._draft_checksum_from_metadata(metadata)
        return _SkillBuilderDraftState(
            draft_checksum=checksum,
            files=metadata,
            total_size_bytes=sum(item.size_bytes for item in metadata),
        )

    @staticmethod
    def _draft_checksum_from_metadata(
        files: tuple[SkillBuilderDraftFileMetadata, ...],
    ) -> str | None:
        if not files:
            return None
        canonical = json.dumps(
            [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in sorted(files, key=lambda value: value.path)
            ],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _require_persisted_draft_snapshot(
        context: ProjectContext,
        design: SkillDesignSessionRow,
        files: tuple[SkillArchiveFile, ...],
    ) -> _SkillBuilderDraftState:
        snapshot = SkillDesignService._draft_snapshot(context, files)
        if design.draft_checksum != snapshot.draft_checksum:
            raise AssetConflict(context.request_id)
        return snapshot

    async def list_agent_draft(
        self,
        context: PrivateWorkContext,
        claim: JobClaim,
        request: SkillBuilderCandidateFileList,
    ) -> SkillBuilderDraftFilePage:
        if not isinstance(request, SkillBuilderCandidateFileList):
            raise AssetValidationFailed(context.request_id)
        async with self._builder_tool_transaction(context, claim) as transaction:
            self._require_builder_tool_in_progress(transaction)
            files = await transaction.repository.load_draft_files(
                transaction.context,
                transaction.design.id,
                for_update=True,
            )
            snapshot = self._require_persisted_draft_snapshot(
                transaction.context,
                transaction.design,
                files,
            )
            if request.expected_draft_checksum is not None and request.expected_draft_checksum != snapshot.draft_checksum:
                raise AssetConflict(transaction.context.request_id)
            if request.offset > len(snapshot.files):
                raise AssetValidationFailed(transaction.context.request_id)
            items = snapshot.files[request.offset : request.offset + request.limit]
            next_offset = request.offset + len(items) if request.offset + len(items) < len(snapshot.files) else None
            return SkillBuilderDraftFilePage(
                draft_checksum=snapshot.draft_checksum,
                items=items,
                offset=request.offset,
                next_offset=next_offset,
                total_file_count=len(snapshot.files),
                total_size_bytes=snapshot.total_size_bytes,
            )

    async def read_agent_draft_file(
        self,
        context: PrivateWorkContext,
        claim: JobClaim,
        request: SkillBuilderCandidateFileRead,
    ) -> SkillBuilderCandidateFileChunk:
        if not isinstance(request, SkillBuilderCandidateFileRead):
            raise AssetValidationFailed(context.request_id)
        async with self._builder_tool_transaction(context, claim) as transaction:
            self._require_builder_tool_in_progress(transaction)
            files = await transaction.repository.load_draft_files(
                transaction.context,
                transaction.design.id,
                for_update=True,
            )
            snapshot = self._require_persisted_draft_snapshot(
                transaction.context,
                transaction.design,
                files,
            )
            if snapshot.draft_checksum != request.expected_draft_checksum:
                raise AssetConflict(transaction.context.request_id)
            item = next((value for value in files if value.path == request.path), None)
            if item is None:
                raise AssetConflict(transaction.context.request_id)
            raw = item.content
            if request.offset_bytes > len(raw) or (raw and request.offset_bytes == len(raw)):
                raise AssetValidationFailed(transaction.context.request_id)
            try:
                raw[: request.offset_bytes].decode("utf-8")
            except UnicodeDecodeError:
                raise AssetValidationFailed(transaction.context.request_id) from None
            end = min(len(raw), request.offset_bytes + request.limit_bytes)
            while end > request.offset_bytes:
                try:
                    content = raw[request.offset_bytes : end].decode("utf-8")
                    break
                except UnicodeDecodeError:
                    end -= 1
            else:
                if raw:
                    raise AssetValidationFailed(transaction.context.request_id)
                content = ""
            return SkillBuilderCandidateFileChunk(
                path=item.path,
                media_type=item.media_type,
                draft_checksum=request.expected_draft_checksum,
                file_size_bytes=len(raw),
                file_sha256=hashlib.sha256(raw).hexdigest(),
                offset_bytes=request.offset_bytes,
                content=content,
                next_offset_bytes=end if end < len(raw) else None,
            )

    async def upsert_agent_draft_file(
        self,
        context: PrivateWorkContext,
        claim: JobClaim,
        request: SkillBuilderCandidateFileUpsert,
    ) -> SkillBuilderDraftMutationReceipt:
        if not isinstance(request, SkillBuilderCandidateFileUpsert):
            raise AssetValidationFailed(context.request_id)
        if contains_secret_like_material(request.content):
            raise AssetValidationFailed(context.request_id)
        async with self._builder_tool_transaction(context, claim) as transaction:
            self._require_builder_tool_in_progress(transaction)
            files = await transaction.repository.load_draft_files(
                transaction.context,
                transaction.design.id,
                for_update=True,
            )
            snapshot = self._require_persisted_draft_snapshot(
                transaction.context,
                transaction.design,
                files,
            )
            existing = next((value for value in files if value.path == request.path), None)
            if snapshot.draft_checksum != request.expected_draft_checksum:
                if self._upsert_was_applied(files, request):
                    return self._draft_mutation_receipt(
                        "upsert",
                        snapshot,
                        path=request.path,
                    )
                raise AssetConflict(transaction.context.request_id)
            self._require_expected_file_identity(
                transaction.context,
                existing,
                expected_size=request.expected_file_size_bytes,
                expected_sha256=request.expected_file_sha256,
            )
            chunk = request.content.encode("utf-8")
            if request.mode == "append":
                if existing is None or existing.media_type != request.media_type:
                    raise AssetConflict(transaction.context.request_id)
                content = existing.content + chunk
                media_type = existing.media_type
            else:
                content = chunk
                media_type = request.media_type
            replacement = SkillArchiveFile(
                path=request.path,
                media_type=media_type,
                content=content,
            )
            updated = tuple(value for value in files if value.path != request.path) + (replacement,)
            updated = self._validate_builder_files(
                transaction.context,
                updated,
                require_skill_md=False,
            )
            await transaction.repository.replace_draft_files(
                transaction.context,
                transaction.design.id,
                updated,
            )
            result = self._draft_snapshot(transaction.context, updated)
            transaction.design.draft_checksum = result.draft_checksum
            transaction.design.validation_json = None
            transaction.design.validated_draft_checksum = None
            transaction.design.authoring_dependencies_json = None
            return self._draft_mutation_receipt(
                "upsert",
                result,
                path=request.path,
            )

    async def delete_agent_draft_file(
        self,
        context: PrivateWorkContext,
        claim: JobClaim,
        request: SkillBuilderCandidateFileDelete,
    ) -> SkillBuilderDraftMutationReceipt:
        if not isinstance(request, SkillBuilderCandidateFileDelete):
            raise AssetValidationFailed(context.request_id)
        async with self._builder_tool_transaction(context, claim) as transaction:
            self._require_builder_tool_in_progress(transaction)
            files = await transaction.repository.load_draft_files(
                transaction.context,
                transaction.design.id,
                for_update=True,
            )
            snapshot = self._require_persisted_draft_snapshot(
                transaction.context,
                transaction.design,
                files,
            )
            existing = next((value for value in files if value.path == request.path), None)
            if snapshot.draft_checksum != request.expected_draft_checksum:
                if existing is None and self._delete_was_applied(files, request):
                    return self._draft_mutation_receipt("delete", snapshot)
                raise AssetConflict(transaction.context.request_id)
            self._require_expected_file_identity(
                transaction.context,
                existing,
                expected_size=request.expected_file_size_bytes,
                expected_sha256=request.expected_file_sha256,
            )
            if existing is None:
                raise AssetConflict(transaction.context.request_id)
            updated = tuple(value for value in files if value.path != request.path)
            updated = self._validate_builder_files(
                transaction.context,
                updated,
                allow_empty=True,
                require_skill_md=False,
            )
            if updated:
                await transaction.repository.replace_draft_files(
                    transaction.context,
                    transaction.design.id,
                    updated,
                )
            else:
                await transaction.repository.clear_draft_files(
                    transaction.context,
                    transaction.design.id,
                )
            result = self._draft_snapshot(transaction.context, updated)
            transaction.design.draft_checksum = result.draft_checksum
            transaction.design.validation_json = None
            transaction.design.validated_draft_checksum = None
            transaction.design.authoring_dependencies_json = None
            return self._draft_mutation_receipt("delete", result)

    @staticmethod
    def _draft_mutation_receipt(
        mutation: Literal["upsert", "delete"],
        snapshot: _SkillBuilderDraftState,
        *,
        path: str | None = None,
    ) -> SkillBuilderDraftMutationReceipt:
        file = next((item for item in snapshot.files if item.path == path), None) if path is not None else None
        return SkillBuilderDraftMutationReceipt(
            mutation=mutation,
            draft_checksum=snapshot.draft_checksum,
            file=file,
            total_file_count=len(snapshot.files),
            total_size_bytes=snapshot.total_size_bytes,
        )

    @staticmethod
    def _require_expected_file_identity(
        context: ProjectContext,
        existing: SkillArchiveFile | None,
        *,
        expected_size: int,
        expected_sha256: str | None,
    ) -> None:
        if existing is None:
            if expected_size != 0 or expected_sha256 is not None:
                raise AssetConflict(context.request_id)
            return
        if len(existing.content) != expected_size or hashlib.sha256(existing.content).hexdigest() != expected_sha256:
            raise AssetConflict(context.request_id)

    @staticmethod
    def _preimage_checksum(
        files: tuple[SkillArchiveFile, ...],
        *,
        path: str,
        size_bytes: int,
        sha256: str | None,
    ) -> str | None:
        metadata = [
            SkillBuilderDraftFileMetadata(
                path=item.path,
                media_type=item.media_type,
                size_bytes=len(item.content),
                sha256=hashlib.sha256(item.content).hexdigest(),
            )
            for item in files
            if item.path != path
        ]
        if sha256 is not None:
            metadata.append(
                SkillBuilderDraftFileMetadata(
                    path=path,
                    media_type="application/octet-stream",
                    size_bytes=size_bytes,
                    sha256=sha256,
                )
            )
        return SkillDesignService._draft_checksum_from_metadata(tuple(metadata))

    @classmethod
    def _upsert_was_applied(
        cls,
        files: tuple[SkillArchiveFile, ...],
        request: SkillBuilderCandidateFileUpsert,
    ) -> bool:
        current = next((value for value in files if value.path == request.path), None)
        if current is None or current.media_type != request.media_type:
            return False
        chunk = request.content.encode("utf-8")
        if request.mode == "replace":
            applied = current.content == chunk
        else:
            applied = (
                len(current.content) == request.expected_file_size_bytes + len(chunk)
                and hashlib.sha256(current.content[: request.expected_file_size_bytes]).hexdigest() == request.expected_file_sha256
                and current.content[request.expected_file_size_bytes :] == chunk
            )
        return (
            applied
            and cls._preimage_checksum(
                files,
                path=request.path,
                size_bytes=request.expected_file_size_bytes,
                sha256=request.expected_file_sha256,
            )
            == request.expected_draft_checksum
        )

    @classmethod
    def _delete_was_applied(
        cls,
        files: tuple[SkillArchiveFile, ...],
        request: SkillBuilderCandidateFileDelete,
    ) -> bool:
        return (
            cls._preimage_checksum(
                files,
                path=request.path,
                size_bytes=request.expected_file_size_bytes,
                sha256=request.expected_file_sha256,
            )
            == request.expected_draft_checksum
        )

    async def finish_agent_clarification(
        self,
        context: PrivateWorkContext,
        claim: JobClaim,
        result: NeedsClarificationResult,
    ) -> SkillBuilderTerminalReceipt:
        if not isinstance(result, NeedsClarificationResult):
            raise AssetValidationFailed(context.request_id)
        terminal_checksum = self._request_checksum(
            {
                "terminal": "clarification",
                "result": result.model_dump(mode="json"),
            }
        )
        async with self._builder_tool_transaction(context, claim) as transaction:
            operation = transaction.operation
            row = transaction.design
            if operation.status == "completed":
                if operation.terminal_kind != "clarification" or operation.terminal_request_checksum != terminal_checksum or row.status != SkillDesignStatus.AWAITING_CLARIFICATION.value:
                    raise AuthorizationRevoked
                return SkillBuilderTerminalReceipt(terminal="clarification")
            self._require_builder_tool_in_progress(transaction)
            clarification = self._clarification_request(result.questions[0])
            row.status = SkillDesignStatus.AWAITING_CLARIFICATION.value
            row.active_clarification_json = self._clarification_json(clarification)
            row.progress_json = self._progress_json(
                SkillDesignStatus.AWAITING_CLARIFICATION,
            )
            self._append_row_message(
                transaction.context,
                row,
                "assistant",
                clarification.question,
            )
            row.error_code = None
            row.error_message = None
            row.revision += 1
            operation.status = "completed"
            operation.result_revision = row.revision
            operation.public_error_code = None
            operation.terminal_kind = "clarification"
            operation.terminal_request_checksum = terminal_checksum
            return SkillBuilderTerminalReceipt(terminal="clarification")

    async def finalize_agent_candidate(
        self,
        context: PrivateWorkContext,
        claim: JobClaim,
        request: SkillBuilderCandidateFinalize,
        dependencies: SkillBuilderDependencySnapshot,
    ) -> SkillBuilderTerminalReceipt:
        if not isinstance(request, SkillBuilderCandidateFinalize) or not isinstance(dependencies, SkillBuilderDependencySnapshot) or dependencies.draft_checksum != request.expected_draft_checksum:
            raise AssetValidationFailed(context.request_id)
        async with self._builder_tool_transaction(context, claim) as transaction:
            operation = transaction.operation
            row = transaction.design
            normalized_dependencies = dependencies.model_copy(
                update={
                    "requirements": tuple(
                        sorted(
                            dependencies.requirements,
                            key=lambda item: item.reference,
                        )
                    )
                }
            )
            terminal_checksum = self._request_checksum(
                {
                    "terminal": "candidate",
                    "request": request.model_dump(mode="json"),
                    "dependencies": normalized_dependencies.model_dump(
                        mode="json",
                    ),
                }
            )
            if operation.status == "completed":
                try:
                    persisted_dependencies = SkillBuilderDependencySnapshot.model_validate(
                        row.authoring_dependencies_json,
                    )
                except ValidationError:
                    raise AuthorizationRevoked from None
                if (
                    operation.terminal_kind != "candidate"
                    or operation.terminal_request_checksum != terminal_checksum
                    or row.status != SkillDesignStatus.DRAFT_READY.value
                    or row.draft_checksum != request.expected_draft_checksum
                    or persisted_dependencies.draft_checksum != request.expected_draft_checksum
                    or self._dependency_identity(persisted_dependencies) != self._dependency_identity(normalized_dependencies)
                ):
                    raise AuthorizationRevoked
                return SkillBuilderTerminalReceipt(terminal="candidate")
            self._require_builder_tool_in_progress(transaction)
            files = await transaction.repository.load_draft_files(
                transaction.context,
                row.id,
                for_update=True,
            )
            snapshot = self._require_persisted_draft_snapshot(
                transaction.context,
                row,
                files,
            )
            if snapshot.draft_checksum != request.expected_draft_checksum:
                raise AssetConflict(transaction.context.request_id)
            revalidated_dependencies = await ProjectAuthoringCatalogRepository(
                transaction.repository.session,
            ).revalidate_dependency_snapshot(
                transaction.context,
                normalized_dependencies,
            )
            preview = await self._skill_service.preview_archive(
                transaction.context,
                files,
            )
            self._require_preview_name(transaction.context, preview, row.slug)
            if preview.checksum != request.expected_draft_checksum:
                raise AssetConflict(transaction.context.request_id)
            normalized_files = self._validate_builder_files(
                transaction.context,
                preview.files,
            )
            await transaction.repository.replace_draft_files(
                transaction.context,
                row.id,
                normalized_files,
            )
            row.draft_checksum = preview.checksum
            row.authoring_dependencies_json = revalidated_dependencies.model_dump(
                mode="json",
            )
            row.validation_json = None
            row.validated_draft_checksum = None
            row.status = SkillDesignStatus.DRAFT_READY.value
            row.active_clarification_json = None
            row.progress_json = self._progress_json(SkillDesignStatus.DRAFT_READY)
            row.error_code = None
            row.error_message = None
            self._append_row_message(
                transaction.context,
                row,
                "assistant",
                request.summary,
            )
            row.revision += 1
            operation.status = "completed"
            operation.result_revision = row.revision
            operation.public_error_code = None
            operation.terminal_kind = "candidate"
            operation.terminal_request_checksum = terminal_checksum
            return SkillBuilderTerminalReceipt(terminal="candidate")

    @staticmethod
    def _dependency_identity(
        snapshot: SkillBuilderDependencySnapshot,
    ) -> tuple[tuple[object, ...], ...]:
        """Return immutable authoring identities, excluding refreshed display state."""

        identities: list[tuple[object, ...]] = []
        for item in snapshot.requirements:
            if item.kind == "skill":
                identities.append(
                    (
                        item.kind,
                        item.reference,
                        item.scope,
                        item.skill_id,
                        item.version_id,
                        item.version_number,
                        item.payload_checksum,
                    )
                )
            else:
                identities.append(
                    (
                        item.kind,
                        item.reference,
                        item.scope,
                        item.mcp_server_id,
                        item.version_id,
                        item.version_number,
                        item.tool_name,
                        item.payload_checksum,
                    )
                )
        return tuple(sorted(identities, key=lambda value: str(value[1])))

    def terminal_sink(
        self,
        context: PrivateWorkContext,
        claim: JobClaim,
    ) -> SkillBuilderDraftSink:
        return _SkillDesignTerminalSink(self, context, claim)

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
                            return self._session_view(context, row, files)
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
                    if operation is not None:
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
                            return self._session_view(
                                context,
                                row,
                                current_files,
                            )
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
                    operation = self._new_operation(
                        context,
                        session_id,
                        kind="validate",
                        idempotency_hash=operation_hash,
                        request_checksum=request_checksum,
                    )
                    operation.status = "completed"
                    operation.result_revision = row.revision
                    await repository.create_operation(context, operation)
                    await session.flush()
                    current_files = await repository.load_draft_files(
                        context,
                        row.id,
                    )
                    return self._session_view(context, row, current_files)
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            self._raise_integrity(context, exc)
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

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
                "acknowledge_warnings": command.acknowledge_warnings,
                "acknowledge_base_stale": command.acknowledge_base_stale,
            }
        )
        repeated_session: SkillDesignSessionView | None = None
        created_result: ProjectSkillArchiveCreateResult | None = None
        created_version: SkillVersionView | None = None
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    repository = self._repository_factory(session)
                    operation = await repository.get_operation(
                        context,
                        operation_kind="commit",
                        idempotency_key_hash=operation_hash,
                        for_update=True,
                    )
                    # Lock order: Project → Membership → Skill →
                    # SkillDesignSession. Read the session without a lock to
                    # learn the revise target, lock the Skill row first, then
                    # lock the session row and re-validate what we read.
                    unlocked = await repository.get(context, session_id)
                    target_asset = None
                    if unlocked.session_kind == "revise" and not unlocked.target_skill_deleted and unlocked.target_skill_id is not None:
                        skill_repository = SkillRepository(session)
                        try:
                            target_asset = await skill_repository.get_project_asset(
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
                    if operation is not None:
                        self._require_matching_operation(
                            context,
                            operation,
                            session_id=session_id,
                            request_checksum=request_checksum,
                        )
                        if operation.status == "completed" and row.status == SkillDesignStatus.COMPLETED.value:
                            repeated_session = self._session_view(
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
                        repeated_session = self._session_view(
                            context,
                            row,
                            (),
                        )
                    else:
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
                        if validation.scan_decision == "warn" and not command.acknowledge_warnings:
                            raise AssetConflict(context.request_id)
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
                            if target_asset.current_published_version_id != row.base_version_id and not command.acknowledge_base_stale:
                                raise SkillDesignBaseStale(context.request_id)
                        operation = self._new_operation(
                            context,
                            session_id,
                            kind="commit",
                            idempotency_hash=operation_hash,
                            request_checksum=request_checksum,
                        )
                        await repository.create_operation(context, operation)
                        row.status = SkillDesignStatus.COMMITTING.value
                        await session.flush()
                        if row.session_kind == "revise":
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
                        # Completion CHECK forbids created_skill_* while status is
                        # still committing. Set the terminal status before any
                        # follow-up query that would autoflush the session row.
                        row.status = SkillDesignStatus.COMPLETED.value
                        row.revision += 1
                        row.progress_json = self._progress_json(SkillDesignStatus.COMPLETED)
                        operation.status = "completed"
                        operation.result_revision = row.revision
                        await repository.clear_draft_files(context, row.id)
                        await session.flush()
                        repeated_session = self._session_view(
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
            if repeated_session.created_skill_id is None or row.created_skill_deleted:
                raise AssetConflict(context.request_id)
            skill = await self._skill_service.get(
                context,
                repeated_session.created_skill_id,
            )
            replayed_version: SkillVersionView | None = None
            if repeated_session.session_kind == "revise" and row.created_skill_version_id is not None:
                replayed_version = await self._skill_service.get_project_version_view(
                    context,
                    repeated_session.created_skill_id,
                    row.created_skill_version_id,
                )
            return SkillDesignCommitResult(
                session=repeated_session,
                skill=skill,
                version=replayed_version,
            )
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            self._raise_integrity(context, exc)
        except DBAPIError:
            raise AssetStorageUnavailable(context.request_id) from None

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
                            return self._session_view(context, row, ())
                        raise AssetConflict(context.request_id)
                    if row.status == SkillDesignStatus.CANCELLED.value:
                        self._require_expected_revision(
                            context,
                            row,
                            command.expected_revision,
                        )
                        return self._session_view(context, row, ())
                    if row.status == SkillDesignStatus.COMPLETED.value:
                        raise AssetConflict(context.request_id)
                    self._require_expected_revision(
                        context,
                        row,
                        command.expected_revision,
                    )
                    operation = self._new_operation(
                        context,
                        session_id,
                        kind="cancel",
                        idempotency_hash=operation_hash,
                        request_checksum=request_checksum,
                    )
                    await repository.create_operation(context, operation)
                    linked_run = await repository.latest_linked_run(
                        context,
                        row.id,
                    )
                    if linked_run is not None and linked_run.status in {"pending", "running"} and linked_run.job_id is not None:
                        private_context = PrivateWorkContext.from_project(context)
                        cancel_result = await PrivateRunRepository(
                            session,
                        ).request_cancel(
                            scope=private_context.resource_scope,
                            thread_id=linked_run.thread_id,
                            run_id=linked_run.run_id,
                            job_id=linked_run.job_id,
                            reason="skill_builder_cancelled",
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
                    await repository.clear_draft_files(context, row.id)
                    row.status = SkillDesignStatus.CANCELLED.value
                    row.draft_checksum = None
                    row.validation_json = None
                    row.validated_draft_checksum = None
                    row.active_clarification_json = None
                    row.error_code = None
                    row.error_message = None
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
                            status="failed",
                            result_revision=row.revision,
                            public_error_code=(SkillDesignServiceErrorCode.GENERATION_INTERRUPTED.value),
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
                    return self._session_view(context, row, ())
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
                            return self._session_view(context, row, files)
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
                    return self._session_view(context, row, files)
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
                            return self._session_view(context, row, files)
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
                                return self._session_view(context, row, files)
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
                            return self._session_view(context, row, files)
                        if operation.status == "failed":
                            files = await repository.load_draft_files(
                                context,
                                row.id,
                            )
                            return self._session_view(context, row, files)
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
                        admission = await self._run_admission.admit_in_session(
                            session,
                            context,
                            row,
                            operation,
                            request,
                            turn_message=turn_message,
                            model_name=getattr(
                                command.input,
                                "model_name",
                                None,
                            ),
                            reasoning_effort=getattr(
                                command.input,
                                "reasoning_effort",
                                None,
                            ),
                        )
                        await session.flush()
                        return admission
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
                    await session.flush()
                    return self._session_view(context, row, files)
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
                        return self._session_view(context, row, files)
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
                    await session.flush()
                    files = await repository.load_draft_files(
                        context,
                        row.id,
                    )
                    return self._session_view(context, row, files)
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
        code = SkillDesignServiceErrorCode.GENERATION_INTERRUPTED.value
        row.status = SkillDesignStatus.FAILED.value
        row.validation_json = None
        row.validated_draft_checksum = None
        row.error_code = code
        row.error_message = "上一次生成已中断，请重新发送你的要求。"
        row.progress_json = self._progress_json(SkillDesignStatus.FAILED)
        row.active_clarification_json = None
        row.revision += 1
        await repository.session.execute(
            update(SkillDesignOperationRow)
            .where(
                SkillDesignOperationRow.project_id == context.project_id,
                SkillDesignOperationRow.owner_user_id == str(context.user_id),
                SkillDesignOperationRow.session_id == row.id,
                SkillDesignOperationRow.status == "in_progress",
            )
            .values(
                status="failed",
                result_revision=row.revision,
                public_error_code=code,
            )
        )
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
    def _validate_create(
        context: ProjectContext,
        command: CreateSkillDesignSession,
    ) -> CreateSkillDesignSession:
        SkillDesignService._require_context(context)
        if not isinstance(command, CreateSkillDesignSession):
            raise AssetValidationFailed(context.request_id)
        slug = command.slug.strip() if isinstance(command.slug, str) else ""
        display_name = command.display_name.strip() if isinstance(command.display_name, str) else ""
        if _SLUG_PATTERN.fullmatch(slug) is None or not display_name or len(display_name) > _MAX_DISPLAY_NAME_CHARS or contains_secret_like_material(display_name):
            raise AssetValidationFailed(context.request_id)
        idempotency_key = SkillDesignService._validate_idempotency_key(
            context,
            command.idempotency_key,
        )
        return CreateSkillDesignSession(
            slug=slug,
            display_name=display_name,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _validate_create_revision(
        context: ProjectContext,
        command: CreateSkillDesignRevisionSession,
    ) -> CreateSkillDesignRevisionSession:
        SkillDesignService._require_context(context)
        if not isinstance(command, CreateSkillDesignRevisionSession):
            raise AssetValidationFailed(context.request_id)
        return CreateSkillDesignRevisionSession(
            skill_id=SkillDesignService._validate_uuid(context, command.skill_id),
            idempotency_key=SkillDesignService._validate_idempotency_key(
                context,
                command.idempotency_key,
            ),
        )

    @staticmethod
    def _validate_turn(
        context: ProjectContext,
        command: SubmitSkillDesignTurn,
    ) -> SubmitSkillDesignTurn:
        SkillDesignService._require_context(context)
        if not isinstance(command, SubmitSkillDesignTurn) or not SkillDesignService._valid_revision(command.expected_revision):
            raise AssetValidationFailed(context.request_id)
        key = SkillDesignService._validate_idempotency_key(
            context,
            command.idempotency_key,
        )
        turn = command.input
        if isinstance(turn, SkillDesignMessageTurn):
            if turn.kind != "message":
                raise AssetValidationFailed(context.request_id)
            message = SkillDesignService._bounded_text(
                context,
                turn.message,
                max_chars=_MAX_MESSAGE_CHARS,
            )
            if contains_secret_like_material(message):
                raise AssetValidationFailed(context.request_id)
            normalized: SkillDesignTurn = SkillDesignMessageTurn(
                kind="message",
                message=message,
                model_name=SkillDesignService._validate_turn_model_name(
                    context,
                    turn.model_name,
                ),
                reasoning_effort=SkillDesignService._validate_turn_reasoning_effort(
                    context,
                    turn.reasoning_effort,
                ),
                attachments=SkillDesignService._validate_turn_attachments(
                    context,
                    turn.attachments,
                ),
            )
        elif isinstance(turn, SkillDesignClarificationTurn):
            response = turn.response
            if turn.kind != "clarification" or not isinstance(response, SkillDesignClarificationResponse):
                raise AssetValidationFailed(context.request_id)
            value = SkillDesignService._bounded_text(
                context,
                response.value,
                max_chars=_MAX_MESSAGE_CHARS,
            )
            if contains_secret_like_material(value):
                raise AssetValidationFailed(context.request_id)
            normalized = SkillDesignClarificationTurn(
                kind="clarification",
                response=SkillDesignClarificationResponse(
                    version=response.version,
                    kind=response.kind,
                    source=response.source,
                    request_id=response.request_id,
                    response_kind=response.response_kind,
                    value=value,
                    option_id=response.option_id,
                ),
                model_name=SkillDesignService._validate_turn_model_name(
                    context,
                    turn.model_name,
                ),
                reasoning_effort=SkillDesignService._validate_turn_reasoning_effort(
                    context,
                    turn.reasoning_effort,
                ),
            )
        elif isinstance(turn, SkillDesignDraftUpdateTurn):
            if turn.kind != "draft_update" or _CHECKSUM_PATTERN.fullmatch(turn.expected_draft_checksum) is None:
                raise AssetValidationFailed(context.request_id)
            try:
                changes = tuple(turn.changes)
            except TypeError:
                raise AssetValidationFailed(context.request_id) from None
            if not changes:
                raise AssetValidationFailed(context.request_id)
            if contains_secret_like_material(SkillDesignService._jsonable(changes)):
                raise AssetValidationFailed(context.request_id)
            normalized = SkillDesignDraftUpdateTurn(
                kind="draft_update",
                expected_draft_checksum=turn.expected_draft_checksum,
                changes=changes,
            )
        else:
            raise AssetValidationFailed(context.request_id)
        return SubmitSkillDesignTurn(
            input=normalized,
            expected_revision=command.expected_revision,
            idempotency_key=key,
        )

    @staticmethod
    def _validate_turn_model_name(
        context: ProjectContext,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        if exact_model_ref(value) is None:
            raise AssetValidationFailed(context.request_id)
        return value

    @staticmethod
    def _validate_turn_reasoning_effort(
        context: ProjectContext,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or value not in SKILL_DESIGN_REASONING_EFFORTS:
            raise AssetValidationFailed(context.request_id)
        return value

    @staticmethod
    def _validate_turn_attachments(
        context: ProjectContext,
        attachments: object,
    ) -> tuple[SkillDesignTurnAttachment, ...]:
        try:
            snapshot = tuple(attachments)  # type: ignore[call-overload]
        except TypeError:
            raise AssetValidationFailed(context.request_id) from None
        if not snapshot:
            return ()
        if len(snapshot) > MAX_SKILL_DESIGN_ATTACHMENTS:
            raise AssetValidationFailed(context.request_id)
        normalized: list[SkillDesignTurnAttachment] = []
        names: set[str] = set()
        total = 0
        for item in snapshot:
            if not isinstance(item, SkillDesignTurnAttachment):
                raise AssetValidationFailed(context.request_id)
            try:
                # The generation contract model owns name/content shape rules.
                checked = SkillDesignAttachment(
                    name=item.name,
                    content=item.content,
                )
            except ValidationError:
                raise AssetValidationFailed(context.request_id) from None
            if checked.name in names:
                raise AssetValidationFailed(context.request_id)
            names.add(checked.name)
            total += len(checked.content.encode("utf-8"))
            if total > MAX_SKILL_DESIGN_ATTACHMENTS_TOTAL_BYTES:
                raise AssetValidationFailed(context.request_id)
            if contains_secret_like_material(checked.name) or contains_secret_like_material(checked.content):
                raise AssetValidationFailed(context.request_id)
            normalized.append(
                SkillDesignTurnAttachment(
                    name=checked.name,
                    content=checked.content,
                )
            )
        return tuple(normalized)

    @staticmethod
    def _validate_validation(
        context: ProjectContext,
        command: ValidateSkillDesignSession,
    ) -> ValidateSkillDesignSession:
        if (
            not isinstance(command, ValidateSkillDesignSession)
            or not SkillDesignService._valid_revision(command.expected_revision)
            or not isinstance(command.expected_draft_checksum, str)
            or _CHECKSUM_PATTERN.fullmatch(command.expected_draft_checksum) is None
        ):
            raise AssetValidationFailed(context.request_id)
        return ValidateSkillDesignSession(
            expected_revision=command.expected_revision,
            expected_draft_checksum=command.expected_draft_checksum,
            idempotency_key=SkillDesignService._validate_idempotency_key(
                context,
                command.idempotency_key,
            ),
        )

    @staticmethod
    def _validate_commit(
        context: ProjectContext,
        command: CommitSkillDesignSession,
    ) -> CommitSkillDesignSession:
        if (
            not isinstance(command, CommitSkillDesignSession)
            or not SkillDesignService._valid_revision(command.expected_revision)
            or not isinstance(command.expected_draft_checksum, str)
            or _CHECKSUM_PATTERN.fullmatch(command.expected_draft_checksum) is None
            or type(command.acknowledge_warnings) is not bool
            or type(command.acknowledge_base_stale) is not bool
        ):
            raise AssetValidationFailed(context.request_id)
        return CommitSkillDesignSession(
            expected_revision=command.expected_revision,
            expected_draft_checksum=command.expected_draft_checksum,
            acknowledge_warnings=command.acknowledge_warnings,
            idempotency_key=SkillDesignService._validate_idempotency_key(
                context,
                command.idempotency_key,
            ),
            acknowledge_base_stale=command.acknowledge_base_stale,
        )

    @staticmethod
    def _validate_cancel(
        context: ProjectContext,
        command: CancelSkillDesignSession,
    ) -> CancelSkillDesignSession:
        if not isinstance(command, CancelSkillDesignSession) or not SkillDesignService._valid_revision(command.expected_revision):
            raise AssetValidationFailed(context.request_id)
        return CancelSkillDesignSession(
            expected_revision=command.expected_revision,
            idempotency_key=SkillDesignService._validate_idempotency_key(
                context,
                command.idempotency_key,
            ),
        )

    @staticmethod
    def _require_context(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))

    @staticmethod
    def _require_capability(
        context: ProjectContext,
        capability: Capability,
    ) -> None:
        SkillDesignService._require_context(context)
        if capability not in context.capabilities:
            raise AssetForbidden(context.request_id)

    @staticmethod
    def _require_nonterminal(
        context: ProjectContext,
        row: SkillDesignSessionRow,
    ) -> None:
        if row.status in {
            SkillDesignStatus.COMPLETED.value,
            SkillDesignStatus.CANCELLED.value,
            SkillDesignStatus.COMMITTING.value,
        }:
            raise AssetConflict(context.request_id)

    @staticmethod
    def _require_revise_target_live(
        context: ProjectContext,
        row: SkillDesignSessionRow,
    ) -> None:
        """A revise session whose target Skill was deleted is terminally dead."""

        if row.session_kind == "revise" and (row.target_skill_deleted or row.target_skill_id is None):
            raise SkillDesignTargetDeleted(context.request_id)

    @staticmethod
    def _require_expected_revision(
        context: ProjectContext,
        row: SkillDesignSessionRow,
        expected: int,
    ) -> None:
        if row.revision != expected:
            raise AssetConflict(context.request_id)

    @staticmethod
    def _require_matching_operation(
        context: ProjectContext,
        operation: SkillDesignOperationRow,
        *,
        session_id: uuid.UUID,
        request_checksum: str,
    ) -> None:
        if operation.session_id != session_id or operation.request_checksum != request_checksum:
            raise AssetConflict(context.request_id)

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
        )
        row.active_clarification_json = None
        return content

    @staticmethod
    def _require_message_capacity(
        context: ProjectContext,
        row: SkillDesignSessionRow,
        *,
        additional: int,
    ) -> None:
        messages = row.messages_json
        if not isinstance(messages, list) or not isinstance(additional, int) or isinstance(additional, bool) or additional < 1 or len(messages) + additional > _MAX_SESSION_MESSAGES:
            raise AssetValidationFailed(context.request_id)

    @staticmethod
    def _append_row_message(
        context: ProjectContext,
        row: SkillDesignSessionRow,
        role: str,
        content: str,
    ) -> None:
        if contains_secret_like_material(content):
            raise AssetValidationFailed(context.request_id)
        SkillDesignService._require_message_capacity(
            context,
            row,
            additional=1,
        )
        row.messages_json = [
            *row.messages_json,
            SkillDesignService._message_json(
                role,
                content,
                now=SkillDesignService._now(),
            ),
        ]

    @staticmethod
    def _require_matching_clarification_response(
        context: ProjectContext,
        row: SkillDesignSessionRow,
        response: SkillDesignClarificationResponse,
    ) -> None:
        if row.status != SkillDesignStatus.AWAITING_CLARIFICATION.value or row.active_clarification_json is None:
            raise AssetConflict(context.request_id)
        request = SkillDesignService._clarification_from_json(
            context,
            row.active_clarification_json,
        )
        if response.version != request.version or response.kind != "human_input_response" or response.source != request.source or response.request_id != request.request_id or response.response_kind not in {"option", "text"}:
            raise AssetConflict(context.request_id)
        if response.response_kind == "option":
            selected = next(
                (item for item in request.options if item.id == response.option_id),
                None,
            )
            if selected is None or selected.value != response.value:
                raise AssetConflict(context.request_id)
        elif response.option_id is not None:
            raise AssetConflict(context.request_id)

    @staticmethod
    def _conversation_brief(
        context: ProjectContext,
        messages: object,
    ) -> str:
        """Build a bounded, role-preserving transcript, newest turns first."""

        if not isinstance(messages, list) or not messages:
            raise AssetValidationFailed(context.request_id)
        remaining = MAX_SKILL_DESIGN_BRIEF_CHARS
        newest_first: list[str] = []
        for raw in reversed(messages):
            if not isinstance(raw, Mapping):
                raise AssetValidationFailed(context.request_id)
            role = raw.get("role")
            content = raw.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise AssetValidationFailed(context.request_id)
            content = content.strip()
            if not content:
                raise AssetValidationFailed(context.request_id)
            prefix = f"{role}: "
            separator_size = 1 if newest_first else 0
            available = remaining - separator_size
            if available <= len(prefix):
                break
            if len(prefix) + len(content) <= available:
                segment = f"{prefix}{content}"
            else:
                content_budget = available - len(prefix)
                if newest_first and content_budget > 1:
                    segment = f"{prefix}…{content[-(content_budget - 1) :]}"
                else:
                    segment = f"{prefix}{content[:content_budget]}"
            newest_first.append(segment)
            remaining -= len(segment) + separator_size
            if remaining <= len("assistant: "):
                break
        if not newest_first:
            raise AssetValidationFailed(context.request_id)
        return "\n".join(reversed(newest_first))

    @staticmethod
    def _candidate_files(
        context: ProjectContext,
        result: CandidateResult,
    ) -> tuple[SkillArchiveFile, ...]:
        if contains_secret_like_material(result.model_dump(mode="json")):
            raise AssetValidationFailed(context.request_id)
        files = tuple(
            SkillArchiveFile(
                path=item.path,
                content=item.content.encode("utf-8"),
                media_type=item.media_type,
            )
            for item in result.files
        )
        return SkillDesignService._validate_builder_files(context, files)

    @staticmethod
    def _validate_builder_files(
        context: ProjectContext,
        files: tuple[SkillArchiveFile, ...],
        *,
        allow_empty: bool = False,
        require_skill_md: bool = True,
    ) -> tuple[SkillArchiveFile, ...]:
        try:
            snapshot = tuple(files)
        except TypeError:
            raise AssetValidationFailed(context.request_id) from None
        if not snapshot:
            if allow_empty:
                return ()
            raise AssetValidationFailed(context.request_id)
        if len(snapshot) > _MAX_BUILDER_FILES:
            raise AssetValidationFailed(context.request_id)
        total = 0
        paths: set[str] = set()
        for item in snapshot:
            if not isinstance(item, SkillArchiveFile):
                raise AssetValidationFailed(context.request_id)
            if item.path in paths or len(item.content) > _MAX_BUILDER_FILE_BYTES:
                raise AssetValidationFailed(context.request_id)
            paths.add(item.path)
            total += len(item.content)
            if total > _MAX_BUILDER_TOTAL_BYTES:
                raise AssetValidationFailed(context.request_id)
            try:
                decoded = item.content.decode("utf-8")
            except UnicodeDecodeError:
                raise AssetValidationFailed(context.request_id) from None
            if "\x00" in decoded or contains_secret_like_material(decoded):
                raise AssetValidationFailed(context.request_id)
        if require_skill_md and "SKILL.md" not in paths:
            raise AssetValidationFailed(context.request_id)
        return tuple(sorted(snapshot, key=lambda item: item.path))

    @staticmethod
    def _validate_partial_builder_files(
        context: ProjectContext,
        files: tuple[SkillArchiveFile, ...],
    ) -> tuple[SkillArchiveFile, ...]:
        """Validate a persisted in-progress draft without requiring completion."""

        return SkillDesignService._validate_builder_files(
            context,
            files,
            allow_empty=True,
            require_skill_md=False,
        )

    @staticmethod
    def _require_preview_name(
        context: ProjectContext,
        preview: SkillArchivePreview,
        expected_slug: str,
    ) -> None:
        name = preview.frontmatter.get("name")
        if not isinstance(name, str) or name != expected_slug:
            raise AssetValidationFailed(context.request_id)

    @staticmethod
    def _validation_from_preview(
        preview: SkillArchivePreview,
        *,
        validated_at: datetime,
    ) -> SkillDesignValidation:
        return SkillDesignValidation(
            draft_checksum=preview.checksum,
            validated_at=validated_at,
            description=preview.description,
            frontmatter=dict(preview.frontmatter),
            compatibility=preview.compatibility,
            secret_requirements=tuple(
                SkillDesignSecretRequirement(
                    name=item.name,
                    optional=item.optional,
                )
                for item in preview.secret_requirements
            ),
            scan_decision=preview.scan_decision,
            scan_rule_ids=preview.scan_rule_ids,
            scan_summary=dict(preview.scan_summary),
        )

    @staticmethod
    def _validation_matches_preview(
        validation: SkillDesignValidation,
        preview: SkillArchivePreview,
    ) -> bool:
        expected = SkillDesignService._validation_from_preview(
            preview,
            validated_at=validation.validated_at,
        )
        return validation == expected

    @staticmethod
    def _validation_json(
        validation: SkillDesignValidation,
    ) -> dict[str, object]:
        return {
            "draft_checksum": validation.draft_checksum,
            "validated_at": validation.validated_at.isoformat(),
            "description": validation.description,
            "frontmatter": dict(validation.frontmatter),
            "compatibility": validation.compatibility,
            "secret_requirements": [{"name": item.name, "optional": item.optional} for item in validation.secret_requirements],
            "scan_decision": validation.scan_decision,
            "scan_rule_ids": list(validation.scan_rule_ids),
            "scan_summary": dict(validation.scan_summary),
        }

    @staticmethod
    def _validation_from_json(
        context: ProjectContext,
        value: object,
    ) -> SkillDesignValidation:
        if not isinstance(value, dict):
            raise AssetValidationFailed(context.request_id)
        try:
            checksum = value["draft_checksum"]
            validated_at = datetime.fromisoformat(value["validated_at"])
            description = value["description"]
            frontmatter = value["frontmatter"]
            compatibility = value["compatibility"]
            requirements = value["secret_requirements"]
            decision = value["scan_decision"]
            rule_ids = value["scan_rule_ids"]
            summary = value["scan_summary"]
        except (KeyError, TypeError, ValueError):
            raise AssetValidationFailed(context.request_id) from None
        if (
            not isinstance(checksum, str)
            or _CHECKSUM_PATTERN.fullmatch(checksum) is None
            or not isinstance(description, str)
            or not isinstance(frontmatter, dict)
            or compatibility is not None
            and not isinstance(compatibility, str)
            or decision not in {"allow", "warn"}
            or not isinstance(requirements, list)
            or not isinstance(rule_ids, list)
            or not all(isinstance(item, str) for item in rule_ids)
            or not isinstance(summary, dict)
        ):
            raise AssetValidationFailed(context.request_id)
        parsed_requirements: list[SkillDesignSecretRequirement] = []
        for item in requirements:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or type(item.get("optional")) is not bool:
                raise AssetValidationFailed(context.request_id)
            parsed_requirements.append(
                SkillDesignSecretRequirement(
                    name=item["name"],
                    optional=item["optional"],
                )
            )
        return SkillDesignValidation(
            draft_checksum=checksum,
            validated_at=validated_at,
            description=description,
            frontmatter=frontmatter,
            compatibility=compatibility,
            secret_requirements=tuple(parsed_requirements),
            scan_decision=decision,
            scan_rule_ids=tuple(rule_ids),
            scan_summary=summary,
        )

    @staticmethod
    def _message_json(
        role: str,
        content: str,
        *,
        now: datetime,
    ) -> dict[str, object]:
        return {
            "id": uuid.uuid4().hex,
            "role": role,
            "content": content,
            "created_at": now.isoformat(),
        }

    @staticmethod
    def _progress_json(
        status: SkillDesignStatus,
    ) -> list[dict[str, object]]:
        if status is SkillDesignStatus.GENERATING:
            values = ("completed", "running", "pending")
        elif status is SkillDesignStatus.DRAFT_READY:
            values = ("completed", "completed", "pending")
        elif status in {
            SkillDesignStatus.VALIDATED,
            SkillDesignStatus.COMMITTING,
            SkillDesignStatus.COMPLETED,
        }:
            values = ("completed", "completed", "completed")
        elif status is SkillDesignStatus.FAILED:
            values = ("completed", "failed", "pending")
        elif status is SkillDesignStatus.AWAITING_CLARIFICATION:
            values = ("running", "pending", "pending")
        else:
            values = ("pending", "pending", "pending")
        return [
            {
                "id": "interview",
                "label": "确认需求",
                "status": values[0],
            },
            {
                "id": "package",
                "label": "生成候选文件",
                "status": values[1],
            },
            {
                "id": "validate",
                "label": "检查 Skill",
                "status": values[2],
            },
        ]

    @staticmethod
    def _clarification_request(
        question: ClarificationQuestion,
    ) -> SkillDesignClarificationRequest:
        request_id = uuid.uuid4().hex
        options = tuple(
            SkillDesignClarificationOption(
                id=f"{question.id}-{index}",
                label=value,
                value=value,
            )
            for index, value in enumerate(question.options, start=1)
        )
        return SkillDesignClarificationRequest(
            version=1,
            kind="human_input_request",
            source="skill-builder",
            request_id=request_id,
            clarification_type="skill_design",
            title="补充 Skill 信息",
            question=question.prompt,
            context=question.reason,
            input_mode=("single_choice" if question.kind == "single_select" else "free_text"),
            options=options,
        )

    @staticmethod
    def _clarification_json(
        request: SkillDesignClarificationRequest,
    ) -> dict[str, object]:
        return {
            "version": request.version,
            "kind": request.kind,
            "source": request.source,
            "request_id": request.request_id,
            "clarification_type": request.clarification_type,
            "title": request.title,
            "question": request.question,
            "context": request.context,
            "input_mode": request.input_mode,
            "options": [{"id": item.id, "label": item.label, "value": item.value} for item in request.options],
        }

    @staticmethod
    def _clarification_from_json(
        context: ProjectContext,
        value: object,
    ) -> SkillDesignClarificationRequest:
        if not isinstance(value, dict):
            raise AssetValidationFailed(context.request_id)
        try:
            options = tuple(
                SkillDesignClarificationOption(
                    id=item["id"],
                    label=item["label"],
                    value=item["value"],
                )
                for item in value.get("options", [])
                if isinstance(item, dict)
            )
            request = SkillDesignClarificationRequest(
                version=value["version"],
                kind=value["kind"],
                source=value["source"],
                request_id=value["request_id"],
                clarification_type=value["clarification_type"],
                title=value["title"],
                question=value["question"],
                context=value["context"],
                input_mode=value["input_mode"],
                options=options,
            )
        except (KeyError, TypeError, ValueError):
            raise AssetValidationFailed(context.request_id) from None
        return request

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
    def _session_summary(
        row: SkillDesignSessionRow,
    ) -> SkillDesignSessionSummary:
        return SkillDesignSessionSummary(
            id=row.id,
            slug=row.slug,
            display_name=row.display_name,
            status=SkillDesignStatus(row.status),
            revision=row.revision,
            updated_at=row.updated_at,
            session_kind=row.session_kind,
        )

    @staticmethod
    def _valid_revision(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1

    @staticmethod
    def _validate_uuid(
        context: ProjectContext,
        value: object,
    ) -> uuid.UUID:
        if not isinstance(value, uuid.UUID):
            raise AssetValidationFailed(context.request_id)
        return value

    @staticmethod
    def _validate_idempotency_key(
        context: ProjectContext,
        value: object,
    ) -> str:
        if not isinstance(value, str):
            raise AssetValidationFailed(context.request_id)
        normalized = value.strip()
        if not normalized or len(normalized) > _MAX_IDEMPOTENCY_KEY_CHARS or "\x00" in normalized:
            raise AssetValidationFailed(context.request_id)
        return normalized

    @staticmethod
    def _bounded_text(
        context: ProjectContext,
        value: object,
        *,
        max_chars: int,
    ) -> str:
        if not isinstance(value, str):
            raise AssetValidationFailed(context.request_id)
        normalized = value.strip()
        if not normalized or len(normalized) > max_chars or "\x00" in normalized:
            raise AssetValidationFailed(context.request_id)
        return normalized

    @staticmethod
    def _idempotency_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _request_checksum(value: object) -> str:
        canonical = json.dumps(
            SkillDesignService._jsonable(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _jsonable(value: object) -> object:
        if is_dataclass(value) and not isinstance(value, type):
            return SkillDesignService._jsonable(asdict(value))
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, Mapping):
            return {str(key): SkillDesignService._jsonable(item) for key, item in value.items()}
        if isinstance(value, tuple | list):
            return [SkillDesignService._jsonable(item) for item in value]
        return value

    @staticmethod
    def _stable_generation_error_message(code: str) -> str:
        if code == "MODEL_OUTPUT_LIMIT":
            return "本轮达到模型输出上限。已保存成功写入的候选草稿；请发送“基于现有草稿继续完成”让 Builder 续作。"
        if code == SkillDesignServiceErrorCode.INVALID_MODEL_OUTPUT.value:
            return "生成结果不是有效的 Skill 文件包，请调整描述后重试。"
        if code == SkillDesignServiceErrorCode.GENERATION_INTERRUPTED.value:
            return "上一次生成已中断，请重新发送你的要求。"
        return "Skill 生成暂时不可用，请稍后重试。"

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
