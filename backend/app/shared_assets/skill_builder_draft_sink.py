"""Run-bound Skill Builder draft and terminal operations under the live Job lease."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.run_repository import (
    PrivateRunExecutionLeaseLost,
    PrivateRunRepository,
)
from app.projects.capabilities import Capability
from app.projects.context import (
    ProjectContext,
    resolve_project_context_in_transaction,
)
from app.projects.errors import ProjectForbidden, ProjectNotFound
from app.shared_assets.errors import (
    AssetConflict,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.project_authoring_catalog import (
    ProjectAuthoringCatalogRepository,
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
    SkillBuilderTerminalReceipt,
)
from app.shared_assets.skill_design_activity import (
    SkillDesignActivityKind,
    SkillDesignActivityRepository,
)
from app.shared_assets.skill_design_codec import (
    _clarification_json,
    _clarification_request,
    _message_json,
    _progress_json,
    _request_checksum,
)
from app.shared_assets.skill_design_contracts import SkillDesignStatus
from app.shared_assets.skill_design_generation import (
    NeedsClarificationResult,
    SkillBuilderDependencySnapshot,
    contains_secret_like_material,
)
from app.shared_assets.skill_design_repository import SkillDesignRepository
from app.shared_assets.skill_design_validation import (
    _require_message_capacity,
    _require_preview_name,
    _validate_builder_files,
    _validate_partial_builder_files,
)
from app.shared_assets.skill_service import SkillService
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.shared_assets import (
    SkillDesignOperationRow,
    SkillDesignSessionRow,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked


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


def _draft_snapshot(
    context: ProjectContext,
    files: tuple[SkillArchiveFile, ...],
) -> _SkillBuilderDraftState:
    files = _validate_partial_builder_files(
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
    checksum = _draft_checksum_from_metadata(metadata)
    return _SkillBuilderDraftState(
        draft_checksum=checksum,
        files=metadata,
        total_size_bytes=sum(item.size_bytes for item in metadata),
    )


def _append_row_message(
    context: ProjectContext,
    row: SkillDesignSessionRow,
    role: str,
    content: str,
    *,
    operation_id: uuid.UUID | None = None,
) -> None:
    if contains_secret_like_material(content):
        raise AssetValidationFailed(context.request_id)
    _require_message_capacity(
        context,
        row,
        additional=1,
    )
    row.messages_json = [
        *row.messages_json,
        _message_json(
            role,
            content,
            now=datetime.now(UTC),
            operation_id=operation_id,
        ),
    ]


class SkillDesignDraftSink:
    """Revalidate authority and the Job lease for every model-facing Builder tool."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        context: PrivateWorkContext,
        claim: JobClaim,
        *,
        skill_service: SkillService | None = None,
        repository_factory: Callable[[AsyncSession], SkillDesignRepository] = SkillDesignRepository,
    ) -> None:
        self._session_factory = session_factory
        self._context = context
        self._claim = claim
        self._skill_service = skill_service or SkillService(session_factory)
        self._repository_factory = repository_factory

    @asynccontextmanager
    async def _builder_tool_transaction(self) -> AsyncIterator[_BuilderToolTransaction]:
        """Revalidate one model-facing Builder tool under the live Job lease."""

        context = require_issued_private_work_context(self._context)
        claim = self._claim
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
    def _require_persisted_draft_snapshot(
        context: ProjectContext,
        design: SkillDesignSessionRow,
        files: tuple[SkillArchiveFile, ...],
    ) -> _SkillBuilderDraftState:
        snapshot = _draft_snapshot(context, files)
        if design.draft_checksum != snapshot.draft_checksum:
            raise AssetConflict(context.request_id)
        return snapshot

    async def list_candidate_files(
        self,
        request: SkillBuilderCandidateFileList,
    ) -> SkillBuilderDraftFilePage:
        if not isinstance(request, SkillBuilderCandidateFileList):
            raise AssetValidationFailed(self._context.request_id)
        async with self._builder_tool_transaction() as transaction:
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

    async def read_candidate_file(
        self,
        request: SkillBuilderCandidateFileRead,
    ) -> SkillBuilderCandidateFileChunk:
        if not isinstance(request, SkillBuilderCandidateFileRead):
            raise AssetValidationFailed(self._context.request_id)
        async with self._builder_tool_transaction() as transaction:
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

    async def upsert_candidate_file(
        self,
        request: SkillBuilderCandidateFileUpsert,
    ) -> SkillBuilderDraftMutationReceipt:
        if not isinstance(request, SkillBuilderCandidateFileUpsert):
            raise AssetValidationFailed(self._context.request_id)
        if contains_secret_like_material(request.content):
            raise AssetValidationFailed(self._context.request_id)
        async with self._builder_tool_transaction() as transaction:
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
            updated = _validate_builder_files(
                transaction.context,
                updated,
                require_skill_md=False,
            )
            await transaction.repository.replace_draft_files(
                transaction.context,
                transaction.design.id,
                updated,
            )
            result = _draft_snapshot(transaction.context, updated)
            transaction.design.draft_checksum = result.draft_checksum
            transaction.design.validation_json = None
            transaction.design.validated_draft_checksum = None
            transaction.design.authoring_dependencies_json = None
            return self._draft_mutation_receipt(
                "upsert",
                result,
                path=request.path,
            )

    async def delete_candidate_file(
        self,
        request: SkillBuilderCandidateFileDelete,
    ) -> SkillBuilderDraftMutationReceipt:
        if not isinstance(request, SkillBuilderCandidateFileDelete):
            raise AssetValidationFailed(self._context.request_id)
        async with self._builder_tool_transaction() as transaction:
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
            updated = _validate_builder_files(
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
            result = _draft_snapshot(transaction.context, updated)
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
        return _draft_checksum_from_metadata(tuple(metadata))

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

    async def request_clarification(
        self,
        result: NeedsClarificationResult,
    ) -> SkillBuilderTerminalReceipt:
        if not isinstance(result, NeedsClarificationResult):
            raise AssetValidationFailed(self._context.request_id)
        terminal_checksum = _request_checksum(
            {
                "terminal": "clarification",
                "result": result.model_dump(mode="json"),
            }
        )
        async with self._builder_tool_transaction() as transaction:
            operation = transaction.operation
            row = transaction.design
            if operation.status == "completed":
                if operation.terminal_kind != "clarification" or operation.terminal_request_checksum != terminal_checksum or row.status != SkillDesignStatus.AWAITING_CLARIFICATION.value:
                    raise AuthorizationRevoked
                return SkillBuilderTerminalReceipt(terminal="clarification")
            self._require_builder_tool_in_progress(transaction)
            clarification = _clarification_request(result.questions[0])
            row.status = SkillDesignStatus.AWAITING_CLARIFICATION.value
            row.active_clarification_json = _clarification_json(clarification)
            row.progress_json = _progress_json(
                SkillDesignStatus.AWAITING_CLARIFICATION,
            )
            _append_row_message(
                transaction.context,
                row,
                "assistant",
                clarification.question,
                operation_id=operation.id,
            )
            row.error_code = None
            row.error_message = None
            row.revision += 1
            operation.status = "completed"
            operation.result_revision = row.revision
            operation.public_error_code = None
            operation.terminal_kind = "clarification"
            operation.terminal_request_checksum = terminal_checksum
            activity_repository = SkillDesignActivityRepository(
                transaction.repository.session,
            )
            await activity_repository.append(
                transaction.context,
                session_id=row.id,
                operation_id=operation.id,
                run_id=operation.run_id,
                kind=SkillDesignActivityKind.RUN_TERMINAL,
                payload={"status": "completed"},
                source_event_id="run-terminal",
            )
            await transaction.repository.clear_operation_baseline(
                transaction.context,
                session_id=row.id,
                operation_id=operation.id,
            )
            return SkillBuilderTerminalReceipt(terminal="clarification")

    async def finalize_candidate(
        self,
        request: SkillBuilderCandidateFinalize,
        dependencies: SkillBuilderDependencySnapshot,
    ) -> SkillBuilderTerminalReceipt:
        if not isinstance(request, SkillBuilderCandidateFinalize) or not isinstance(dependencies, SkillBuilderDependencySnapshot) or dependencies.draft_checksum != request.expected_draft_checksum:
            raise AssetValidationFailed(self._context.request_id)
        async with self._builder_tool_transaction() as transaction:
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
            terminal_checksum = _request_checksum(
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
            _require_preview_name(transaction.context, preview, row.slug)
            if preview.checksum != request.expected_draft_checksum:
                raise AssetConflict(transaction.context.request_id)
            normalized_files = _validate_builder_files(
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
            row.progress_json = _progress_json(SkillDesignStatus.DRAFT_READY)
            row.error_code = None
            row.error_message = None
            _append_row_message(
                transaction.context,
                row,
                "assistant",
                request.summary,
                operation_id=operation.id,
            )
            row.revision += 1
            operation.status = "completed"
            operation.result_revision = row.revision
            operation.public_error_code = None
            operation.terminal_kind = "candidate"
            operation.terminal_request_checksum = terminal_checksum
            activity_repository = SkillDesignActivityRepository(
                transaction.repository.session,
            )
            await activity_repository.append(
                transaction.context,
                session_id=row.id,
                operation_id=operation.id,
                run_id=operation.run_id,
                kind=SkillDesignActivityKind.CANDIDATE_GENERATED,
                source_event_id="candidate-generated",
            )
            await activity_repository.append(
                transaction.context,
                session_id=row.id,
                operation_id=operation.id,
                run_id=operation.run_id,
                kind=SkillDesignActivityKind.VALIDATION_STARTED,
                source_event_id="candidate-validation-started",
            )
            await activity_repository.append(
                transaction.context,
                session_id=row.id,
                operation_id=operation.id,
                run_id=operation.run_id,
                kind=SkillDesignActivityKind.VALIDATION_PASSED,
                source_event_id="candidate-validation-passed",
            )
            await activity_repository.append(
                transaction.context,
                session_id=row.id,
                operation_id=operation.id,
                run_id=operation.run_id,
                kind=SkillDesignActivityKind.RUN_TERMINAL,
                payload={"status": "completed"},
                source_event_id="run-terminal",
            )
            await transaction.repository.clear_operation_baseline(
                transaction.context,
                session_id=row.id,
                operation_id=operation.id,
            )
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


__all__ = [
    "SkillDesignDraftSink",
]
