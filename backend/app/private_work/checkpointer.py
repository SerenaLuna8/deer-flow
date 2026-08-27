from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, Protocol, TypeVar, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automations.reconciliation import AutomationReconciler
from app.private_work.checkpoint_delete_recovery import (
    checkpoint_delete_candidate_from_record,
    recover_checkpoint_delete_candidate,
)
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
    strip_private_client_fields,
)
from app.private_work.context_projection import (
    ContextProjectionTransaction,
    context_evidence_record_to_core,
)
from app.private_work.context_replacement import (
    idle_checkpoint_projection_source,
    idle_compaction_projection_source,
    source_from_checkpoint_snapshot,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.execution_approval_audit import (
    HostExecutionApprovalAuditPort,
)
from app.private_work.execution_approval_lifecycle import (
    ApprovalJobDependency,
    ApprovalRunDependency,
    ExecutionApprovalPrivateLifecycleConflict,
    lock_execution_approval_private_rows,
)
from app.private_work.output_delivery_obligation import (
    OutputDeliveryObligationConflict,
    transition_output_delivery_obligation_for_approval_terminal,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.thread_repository import (
    PrivateThreadRecord,
    PrivateThreadRepository,
)
from app.projects.capabilities import Capability
from deerflow.agents.memory.snip import (
    MEMORY_ARCHIVE_RECEIPT_KEY,
    MEMORY_ARCHIVE_RECEIPT_VERSION,
    SNIP_ARCHIVE_PROMPT_VERSION,
)
from deerflow.agents.provider_request_contract import (
    CONTEXT_COMPACTION_RECEIPT_STATE_KEY,
    CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
    provider_response_digest,
)
from deerflow.config.model_execution import SystemModelExecutionProvenance
from deerflow.error_codes import ContextProviderCallAmbiguousError
from deerflow.persistence.context_evidence import (
    ContextEvidenceRepository,
    ContextEvidenceScope,
    ContextSubjectRef,
)
from deerflow.persistence.execution_approvals import (
    EXECUTION_APPROVAL_ACTIVE_STATUSES,
    ExecutionApprovalRequestRow,
    ExecutionApprovalResultReceiptRow,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryHistoryActivation,
)
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
    PrivateFileRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.context_evidence import (
    CheckpointLinkedV1,
    CompactionCommittedV1,
    ContextCheckpointProjectionSnapshot,
    ContextCompactionCheckpointReceipt,
    ContextProjectionHead,
    ContextSubject,
    ProjectionPhase,
    ProviderCallDisposition,
    RequestPreparedV1,
    resolve_provider_call,
)
from deerflow.runtime.events.models import STREAM_TERMINAL_ERROR_CODES
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope

PRIVATE_SCOPE_MARKER = "deerflow_private_scope"
_T = TypeVar("_T")
_MEMORY_ARCHIVE_RECEIPT_FIELDS = frozenset(
    {
        "version",
        "project_id",
        "owner_user_id",
        "namespace",
        "thread_id",
        "source_checkpoint_id",
        "source_digest",
        "tagged_text",
        "content_digest",
        "preference_version",
        "snip_prompt_version",
        "summary_model_config_id",
        "summary_model_payload_checksum",
        "summary_model_secret_generation_id",
        "summary_model_secret_envelope_digest",
    }
)


class PrivateCheckpointQuotaPort(Protocol):
    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None: ...

    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        request_id: str,
    ) -> None: ...


class PrivateCheckpointAuditPort(HostExecutionApprovalAuditPort, Protocol):
    async def run_cancel_requested(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        run_id: str,
        job_id: uuid.UUID,
    ) -> None: ...

    async def run_terminal(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None: ...


class _NoopPrivateCheckpointQuota:
    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None:
        del session, scope, file_id, size, request_id

    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        del session, scope, run_id, request_id


def _drop_marker(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _drop_marker(item) for key, item in value.items() if isinstance(key, str) and key != PRIVATE_SCOPE_MARKER}
    if isinstance(value, list):
        return [_drop_marker(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_drop_marker(item) for item in value)
    return value


class ProjectScopedCheckpointer:
    """Factory that binds raw checkpoint persistence to trusted project context."""

    def __init__(
        self,
        raw_saver: BaseCheckpointSaver,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        quota: PrivateCheckpointQuotaPort | None = None,
        approval_audit: PrivateCheckpointAuditPort | None = None,
        run_event_store: DbRunEventStore | None = None,
    ) -> None:
        self._raw = raw_saver
        self._session_factory = session_factory
        self._quota = quota or _NoopPrivateCheckpointQuota()
        self._approval_audit = approval_audit
        self._run_event_store = run_event_store or DbRunEventStore(session_factory)
        try:
            self._owner_loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("ProjectScopedCheckpointer must be created on its owning event loop") from exc

    def for_context(
        self,
        context: PrivateWorkContext,
        *,
        thread_kind: Literal["chat", "skill_builder"] = "chat",
    ) -> _ScopedCheckpointSaver:
        return _ScopedCheckpointSaver(
            self._raw,
            self._session_factory,
            require_issued_private_work_context(context),
            self._owner_loop,
            self._quota,
            self._approval_audit,
            self._run_event_store,
            thread_kind=thread_kind,
        )


class _ScopedCheckpointSaver(BaseCheckpointSaver):
    def __init__(
        self,
        raw_saver: BaseCheckpointSaver,
        session_factory: async_sessionmaker[AsyncSession],
        context: PrivateWorkContext,
        owner_loop: asyncio.AbstractEventLoop,
        quota: PrivateCheckpointQuotaPort,
        approval_audit: PrivateCheckpointAuditPort | None,
        run_event_store: DbRunEventStore,
        *,
        thread_kind: Literal["chat", "skill_builder"],
    ) -> None:
        super().__init__(serde=raw_saver.serde)
        self._raw = raw_saver
        self._session_factory = session_factory
        self._context = context
        self._owner_loop = owner_loop
        self._quota = quota
        self._approval_audit = approval_audit
        self._run_event_store = run_event_store
        self._automation_reconciler = AutomationReconciler(session_factory)
        self._thread_kind = thread_kind
        self._revalidator = PrivateWorkRevalidator()
        self._authorization_boundary: object | None = None
        self._context_evidence_observer: object | None = None

    def set_authorization_boundary(self, boundary: object) -> None:
        self._authorization_boundary = boundary

    def set_context_evidence_observer(self, observer: object) -> None:
        self._context_evidence_observer = observer

    def already_authorized(
        self,
        session: AsyncSession,
    ) -> _AlreadyAuthorizedCheckpointSaver:
        """Bind graph checkpoint operations to one caller-owned transaction."""
        return _AlreadyAuthorizedCheckpointSaver(self, session)

    @property
    def config_specs(self):
        return self._raw.config_specs

    def get_next_version(self, current, channel):
        return self._raw.get_next_version(current, channel)

    @property
    def _scope_marker(self) -> dict[str, str]:
        context = require_issued_private_work_context(self._context)
        return {
            "project_id": str(context.project_id),
            "owner_user_id": str(context.user_id),
        }

    @staticmethod
    def _thread_id(config: RunnableConfig | None) -> str:
        if not isinstance(config, Mapping):
            raise PrivateWorkNotFound("unknown")
        configurable = config.get("configurable")
        if not isinstance(configurable, Mapping):
            raise PrivateWorkNotFound("unknown")
        thread_id = configurable.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise PrivateWorkNotFound("unknown")
        return thread_id

    def _sanitize_config(
        self,
        config: RunnableConfig,
        *,
        thread_id: str | None = None,
    ) -> RunnableConfig:
        clean = cast(
            dict[str, object],
            _drop_marker(strip_private_client_fields(cast(Mapping[str, object], config))),
        )
        configurable = clean.get("configurable")
        clean_configurable = dict(configurable) if isinstance(configurable, Mapping) else {}
        clean_configurable["thread_id"] = thread_id or self._thread_id(config)
        clean["configurable"] = clean_configurable
        return cast(RunnableConfig, clean)

    def _sanitize_metadata(
        self,
        metadata: CheckpointMetadata,
    ) -> CheckpointMetadata:
        clean = cast(
            dict[str, Any],
            _drop_marker(strip_private_client_fields(cast(Mapping[str, object], metadata))),
        )
        clean[PRIVATE_SCOPE_MARKER] = self._scope_marker
        return cast(CheckpointMetadata, clean)

    def _validate_marker(
        self,
        item: CheckpointTuple,
        *,
        thread_id: str,
    ) -> None:
        if self._thread_id(item.config) != thread_id:
            raise PrivateWorkNotFound(self._context.request_id)
        marker = item.metadata.get(PRIVATE_SCOPE_MARKER)
        if marker != self._scope_marker:
            raise PrivateWorkNotFound(self._context.request_id)

    @staticmethod
    def _checkpoint_id(config: RunnableConfig | None) -> str | None:
        if not isinstance(config, Mapping):
            return None
        configurable = config.get("configurable")
        if not isinstance(configurable, Mapping):
            return None
        checkpoint_id = configurable.get("checkpoint_id")
        return checkpoint_id if isinstance(checkpoint_id, str) and checkpoint_id else None

    def _memory_history_activation(
        self,
        item: CheckpointTuple,
        *,
        thread_id: str,
    ) -> MemoryHistoryActivation | None:
        checkpoint = item.checkpoint
        if not isinstance(checkpoint, Mapping):
            raise ValueError("Checkpoint payload is invalid")
        channel_values = checkpoint.get("channel_values")
        if not isinstance(channel_values, Mapping):
            return None
        receipt = channel_values.get(MEMORY_ARCHIVE_RECEIPT_KEY)
        if receipt is None:
            return None
        if not isinstance(receipt, Mapping) or set(receipt) != _MEMORY_ARCHIVE_RECEIPT_FIELDS or receipt.get("version") != MEMORY_ARCHIVE_RECEIPT_VERSION or receipt.get("snip_prompt_version") != SNIP_ARCHIVE_PROMPT_VERSION:
            raise ValueError("Checkpoint Memory receipt is invalid")

        context = require_issued_private_work_context(self._context)
        if receipt.get("project_id") != str(context.project_id) or receipt.get("owner_user_id") != str(context.user_id) or receipt.get("thread_id") != thread_id:
            raise ValueError("Checkpoint Memory receipt scope is invalid")
        # Dual-segment SNIP: the checkpoint's summary_text carries the prose
        # continuity segment while the receipt carries only the tagged segment,
        # so the two texts are intentionally different. A receipt may still only
        # ride a checkpoint that also carries a non-empty summary.
        tagged_text = receipt.get("tagged_text")
        summary_text = channel_values.get("summary_text")
        if not isinstance(tagged_text, str) or not isinstance(summary_text, str) or not summary_text.strip():
            raise ValueError("Checkpoint Memory receipt summary is invalid")
        committed_checkpoint_id = self._checkpoint_id(item.config)
        if committed_checkpoint_id is None:
            raise ValueError("Checkpoint Memory receipt commit is invalid")
        try:
            secret_generation_raw = receipt["summary_model_secret_generation_id"]
            summary_model = SystemModelExecutionProvenance(
                model_config_id=uuid.UUID(str(receipt["summary_model_config_id"])),
                payload_checksum=str(receipt["summary_model_payload_checksum"]),
                secret_generation_id=(uuid.UUID(str(secret_generation_raw)) if secret_generation_raw is not None else None),
                secret_envelope_digest=(str(receipt["summary_model_secret_envelope_digest"]) if receipt["summary_model_secret_envelope_digest"] is not None else None),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("Checkpoint Memory model is invalid") from None
        return MemoryHistoryActivation(
            scope=MemoryDocumentScope(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                namespace=receipt.get("namespace"),
            ),
            thread_id=thread_id,
            source_checkpoint_id=receipt.get("source_checkpoint_id"),
            committed_checkpoint_id=committed_checkpoint_id,
            source_digest=receipt.get("source_digest"),
            tagged_text=tagged_text,
            content_digest=receipt.get("content_digest"),
            preference_version=receipt.get("preference_version"),
            snip_prompt_version=receipt.get("snip_prompt_version"),
            summary_model=summary_model,
        )

    async def _repair_memory_archive_receipt(
        self,
        session: AsyncSession,
        item: CheckpointTuple,
        *,
        thread_id: str,
    ) -> None:
        activation = self._memory_history_activation(
            item,
            thread_id=thread_id,
        )
        if activation is None:
            return
        await MemoryDocumentRepository(session).activate_history(activation)

    def _context_compaction_activation(
        self,
        item: CheckpointTuple,
        *,
        thread_id: str,
    ) -> tuple[str, ContextCompactionCheckpointReceipt] | None:
        checkpoint = item.checkpoint
        if not isinstance(checkpoint, Mapping):
            raise ValueError("Checkpoint payload is invalid")
        channel_values = checkpoint.get("channel_values")
        if not isinstance(channel_values, Mapping):
            return None
        raw_receipt = channel_values.get(CONTEXT_COMPACTION_RECEIPT_STATE_KEY)
        if raw_receipt is None:
            return None
        raw_projection = channel_values.get(CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY)
        if not isinstance(raw_receipt, Mapping) or not isinstance(
            raw_projection,
            Mapping,
        ):
            raise ValueError("Checkpoint Context compaction receipt is invalid")
        receipt = ContextCompactionCheckpointReceipt.from_safe_mapping(
            raw_receipt,
        )
        projection = ContextCheckpointProjectionSnapshot.from_safe_mapping(
            raw_projection,
        )
        if receipt.projection_snapshot != projection:
            # A Provider response clears this receipt. Treat any retained older
            # receipt beside a newer safe snapshot as stale derived state.
            return None
        committed_checkpoint_id = self._checkpoint_id(item.config)
        if committed_checkpoint_id is None:
            raise ValueError("Checkpoint Context compaction commit is invalid")
        if thread_id != self._thread_id(item.config):
            raise ValueError("Checkpoint Context compaction Thread is invalid")
        return committed_checkpoint_id, receipt

    async def _repair_context_compaction_receipt(
        self,
        session: AsyncSession,
        item: CheckpointTuple,
        *,
        thread_id: str,
    ) -> None:
        activation = self._context_compaction_activation(
            item,
            thread_id=thread_id,
        )
        if activation is None:
            return
        checkpoint_id, receipt = activation
        context = require_issued_private_work_context(self._context)
        scope = ContextEvidenceScope.from_resource(
            context.resource_scope,
            thread_id,
        )
        repository = ContextEvidenceRepository(session)
        subject = ContextSubjectRef.lead_thread(thread_id)
        current = await repository.read_head(scope, subject, lock=True)
        if current is not None and current.checkpoint_id == checkpoint_id and str(current.context_window_generation) == receipt.result_generation.generation_id:
            try:
                ContextProjectionHead.from_safe_mapping(current.projection)
            except (TypeError, ValueError):
                await repository.delete_head(scope, subject)
            else:
                return
        source = idle_compaction_projection_source(
            receipt,
            thread_id=thread_id,
            result_checkpoint_id=checkpoint_id,
        )
        await ContextProjectionTransaction(repository).append_and_project(
            scope=scope,
            source=source,
            payloads=(
                CompactionCommittedV1(
                    receipt_id=receipt.receipt_id,
                    source_checkpoint_id=receipt.source_checkpoint_id,
                    result_checkpoint_id=checkpoint_id,
                    source_generation=receipt.source_generation,
                    result_generation=receipt.result_generation,
                    source_tokens=receipt.source_tokens,
                    result_tokens=receipt.result_tokens,
                    summary_digest=receipt.summary_digest,
                ),
            ),
            origin_run_id=receipt.origin_run_id,
            active_run_id=(receipt.origin_run_id if receipt.phase.value == "active" else None),
        )

    def _context_provider_checkpoint_activation(
        self,
        item: CheckpointTuple,
        *,
        thread_id: str,
    ) -> tuple[str, ContextCheckpointProjectionSnapshot] | None:
        checkpoint = item.checkpoint
        if not isinstance(checkpoint, Mapping):
            raise ValueError("Checkpoint payload is invalid")
        channel_values = checkpoint.get("channel_values")
        if not isinstance(channel_values, Mapping):
            return None
        raw_projection = channel_values.get(
            CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY,
        )
        if not isinstance(raw_projection, Mapping):
            return None
        provider_authority_fields = (
            "provider_call_id",
            "provider_subject",
            "origin_run_id",
            "provider_response_message_start",
            "provider_response_message_count",
            "provider_response_digest",
        )
        if not any(raw_projection.get(field) is not None for field in provider_authority_fields):
            return None
        try:
            snapshot = ContextCheckpointProjectionSnapshot.from_safe_mapping(
                raw_projection,
            )
        except (TypeError, ValueError):
            raise ContextProviderCallAmbiguousError(
                "Checkpoint Provider response authority is invalid",
            ) from None
        if snapshot.provider_call_id is None or snapshot.provider_subject is None or snapshot.provider_subject != ContextSubject.lead_thread(thread_id=thread_id):
            raise ContextProviderCallAmbiguousError(
                "Checkpoint Provider response subject is invalid",
            )
        checkpoint_id = self._checkpoint_id(item.config)
        if checkpoint_id is None or thread_id != self._thread_id(item.config):
            raise ContextProviderCallAmbiguousError(
                "Checkpoint Provider response identity is invalid",
            )
        return checkpoint_id, snapshot

    @staticmethod
    def _validate_context_provider_checkpoint_response(
        item: CheckpointTuple,
        snapshot: ContextCheckpointProjectionSnapshot,
    ) -> None:
        checkpoint = item.checkpoint
        if not isinstance(checkpoint, Mapping):
            raise ValueError("Checkpoint payload is invalid")
        channel_values = checkpoint.get("channel_values")
        if not isinstance(channel_values, Mapping):
            raise ContextProviderCallAmbiguousError(
                "Checkpoint does not prove the Provider response",
            )
        messages = channel_values.get("messages")
        start = snapshot.provider_response_message_start
        count = snapshot.provider_response_message_count
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)) or start is None or count is None or start + count > len(messages):
            raise ContextProviderCallAmbiguousError(
                "Checkpoint does not prove the Provider response",
            )
        try:
            digest = provider_response_digest(
                tuple(messages[start : start + count]),
            )
        except (TypeError, ValueError):
            raise ContextProviderCallAmbiguousError(
                "Checkpoint Provider response proof is invalid",
            ) from None
        if digest != snapshot.provider_response_digest:
            raise ContextProviderCallAmbiguousError(
                "Checkpoint does not contain the bound Provider response",
            )

    async def _repair_context_provider_checkpoint(
        self,
        session: AsyncSession,
        item: CheckpointTuple,
        *,
        thread_id: str,
    ) -> str | None:
        """Converge one observed response to Checkpoint Evidence and its Head."""

        activation = self._context_provider_checkpoint_activation(
            item,
            thread_id=thread_id,
        )
        if activation is None:
            return None
        checkpoint_id, snapshot = activation
        context = require_issued_private_work_context(self._context)
        scope = ContextEvidenceScope.from_resource(
            context.resource_scope,
            thread_id,
        )
        repository = ContextEvidenceRepository(session)
        provider_call_id = snapshot.provider_call_id
        if provider_call_id is None:  # pragma: no cover - activation invariant
            return None
        records = []
        cursor = 0
        while True:
            page = await repository.page_provider_call_evidence(
                scope,
                ContextSubjectRef.lead_thread(thread_id),
                provider_call_id,
                after_seq=cursor,
                limit=1000,
            )
            records.extend(page)
            if len(page) < 1000:
                break
            cursor = page[-1].evidence_seq
        evidence = tuple(context_evidence_record_to_core(record) for record in records)
        try:
            plan = resolve_provider_call(evidence, provider_call_id)
        except (TypeError, ValueError):
            raise ContextProviderCallAmbiguousError(
                "Provider lifecycle Evidence is invalid",
            ) from None
        prepared = next(
            (item for item in reversed(evidence) if isinstance(item.payload, RequestPreparedV1) and item.payload.provider_call.provider_call_id == provider_call_id),
            None,
        )
        if prepared is None or not isinstance(
            prepared.payload,
            RequestPreparedV1,
        ):
            raise ContextProviderCallAmbiguousError(
                "Checkpoint Provider response has no prepared Evidence",
            )
        provider_call = prepared.payload.provider_call
        if (
            provider_call.subject != snapshot.provider_subject
            or provider_call.generation != snapshot.generation
            or provider_call.request_fingerprint != snapshot.measurement.request_fingerprint
            or prepared.payload.measurement != snapshot.measurement
            or prepared.origin_run_id != snapshot.origin_run_id
        ):
            raise ContextProviderCallAmbiguousError(
                "Checkpoint Provider response disagrees with Evidence",
            )
        if plan.disposition is ProviderCallDisposition.REUSE_RESULT:
            return provider_call_id
        phase = ProjectionPhase.IDLE
        active_run_id = None
        observer_run_id = getattr(
            getattr(self, "_context_evidence_observer", None),
            "run_id",
            None,
        )
        if observer_run_id == prepared.origin_run_id:
            phase = ProjectionPhase.ACTIVE
            active_run_id = prepared.origin_run_id
        if phase is ProjectionPhase.IDLE:
            source = idle_checkpoint_projection_source(
                snapshot,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
            )
        else:
            source = source_from_checkpoint_snapshot(
                snapshot,
                subject=provider_call.subject,
                checkpoint_id=checkpoint_id,
                phase=phase,
            )
        source = source.model_copy(
            update={"current_provider_call_id": provider_call_id},
        )
        if plan.disposition is not ProviderCallDisposition.REPAIR_CHECKPOINT_LINK:
            raise ContextProviderCallAmbiguousError(
                "Provider call cannot be replayed safely",
            )
        self._validate_context_provider_checkpoint_response(
            item,
            snapshot,
        )
        await ContextProjectionTransaction(repository).append_and_project(
            scope=scope,
            source=source,
            payloads=(
                CheckpointLinkedV1(
                    provider_call_id=provider_call_id,
                    checkpoint_id=checkpoint_id,
                ),
            ),
            origin_run_id=prepared.origin_run_id,
            active_run_id=active_run_id,
        )
        return provider_call_id

    @asynccontextmanager
    async def _locked_active(
        self,
        thread_id: str,
        capability: Capability,
        authorization_operation: str,
    ) -> AsyncIterator[AsyncSession]:
        try:
            if self._authorization_boundary is not None:
                await getattr(self._authorization_boundary, authorization_operation)()
            async with self._session_factory() as session:
                async with session.begin():
                    await self._revalidator.require(
                        session,
                        self._context,
                        capability,
                        lock=True,
                    )
                    record = await PrivateThreadRepository(session).get(
                        scope=self._context.resource_scope,
                        thread_id=thread_id,
                        lock=True,
                        thread_kind=self._thread_kind,
                    )
                    if record is None:
                        raise PrivateWorkNotFound(self._context.request_id)
                    yield session
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        async with self._locked_active(
            thread_id,
            Capability.PRIVATE_WORK_READ_OWN,
            "before_checkpoint_read",
        ) as session:
            try:
                item = await self._raw.aget_tuple(clean_config)
                if item is not None:
                    self._validate_marker(item, thread_id=thread_id)
                    await self._repair_memory_archive_receipt(
                        session,
                        item,
                        thread_id=thread_id,
                    )
                    await self._repair_context_compaction_receipt(
                        session,
                        item,
                        thread_id=thread_id,
                    )
                    await self._repair_context_provider_checkpoint(
                        session,
                        item,
                        thread_id=thread_id,
                    )
                return item
            except PrivateWorkError:
                raise
            except ContextProviderCallAmbiguousError:
                raise
            except Exception:
                raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aget_tuple_already_authorized(
        self,
        config: RunnableConfig,
        *,
        session: AsyncSession,
    ) -> CheckpointTuple | None:
        """Read through the raw saver while the caller holds the scoped DB locks."""

        if not session.in_transaction():
            raise PrivateWorkUnavailable(self._context.request_id)
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        try:
            item = await self._raw.aget_tuple(clean_config)
            if item is not None:
                self._validate_marker(item, thread_id=thread_id)
                await self._repair_memory_archive_receipt(
                    session,
                    item,
                    thread_id=thread_id,
                )
                await self._repair_context_compaction_receipt(
                    session,
                    item,
                    thread_id=thread_id,
                )
                await self._repair_context_provider_checkpoint(
                    session,
                    item,
                    thread_id=thread_id,
                )
            return item
        except PrivateWorkError:
            raise
        except ContextProviderCallAmbiguousError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aget(self, config: RunnableConfig) -> Checkpoint | None:
        item = await self.aget_tuple(config)
        return None if item is None else item.checkpoint

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        clean_before = None if before is None else self._sanitize_config(before, thread_id=thread_id)
        clean_filter = (
            None
            if filter is None
            else cast(
                dict[str, Any],
                _drop_marker(strip_private_client_fields(filter)),
            )
        )
        async with self._locked_active(
            thread_id,
            Capability.PRIVATE_WORK_READ_OWN,
            "before_checkpoint_read",
        ):
            try:
                async for item in self._raw.alist(
                    clean_config,
                    filter=clean_filter,
                    before=clean_before,
                    limit=limit,
                ):
                    self._validate_marker(item, thread_id=thread_id)
                    yield item
            except PrivateWorkError:
                raise
            except Exception:
                raise PrivateWorkUnavailable(self._context.request_id) from None

    async def alist_already_authorized(
        self,
        config: RunnableConfig | None,
        *,
        session: AsyncSession,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """List raw tuples while the caller holds the scoped Thread lock."""
        if not session.in_transaction():
            raise PrivateWorkUnavailable(self._context.request_id)
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        clean_before = None if before is None else self._sanitize_config(before, thread_id=thread_id)
        clean_filter = (
            None
            if filter is None
            else cast(
                dict[str, Any],
                _drop_marker(strip_private_client_fields(filter)),
            )
        )
        try:
            async for item in self._raw.alist(
                clean_config,
                filter=clean_filter,
                before=clean_before,
                limit=limit,
            ):
                self._validate_marker(item, thread_id=thread_id)
                yield item
        except PrivateWorkError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = self._thread_id(config)
        written_config: RunnableConfig | None = None
        repaired_provider_call_id: str | None = None
        async with self._locked_active(
            thread_id,
            Capability.PRIVATE_WORK_CREATE,
            "before_checkpoint_write",
        ) as session:
            try:
                clean_config = self._sanitize_config(
                    config,
                    thread_id=thread_id,
                )
                source = await self._raw.aget_tuple(clean_config)
                if source is not None:
                    self._validate_marker(source, thread_id=thread_id)
                    await self._repair_memory_archive_receipt(
                        session,
                        source,
                        thread_id=thread_id,
                    )
                    await self._repair_context_compaction_receipt(
                        session,
                        source,
                        thread_id=thread_id,
                    )
                    repaired_provider_call_id = await self._repair_context_provider_checkpoint(
                        session,
                        source,
                        thread_id=thread_id,
                    )
                written_config = await self._raw.aput(
                    clean_config,
                    checkpoint,
                    self._sanitize_metadata(metadata),
                    new_versions,
                )
                item = await self._raw.aget_tuple(written_config)
                if item is None:
                    raise PrivateWorkNotFound(self._context.request_id)
                self._validate_marker(item, thread_id=thread_id)
                await self._repair_memory_archive_receipt(
                    session,
                    item,
                    thread_id=thread_id,
                )
                await self._repair_context_compaction_receipt(
                    session,
                    item,
                    thread_id=thread_id,
                )
                repaired_provider_call_id = (
                    await self._repair_context_provider_checkpoint(
                        session,
                        item,
                        thread_id=thread_id,
                    )
                    or repaired_provider_call_id
                )
            except PrivateWorkError:
                raise
            except ContextProviderCallAmbiguousError:
                raise
            except Exception:
                raise PrivateWorkUnavailable(self._context.request_id) from None
        if written_config is None:
            raise PrivateWorkUnavailable(self._context.request_id)
        checkpoint_id = self._checkpoint_id(written_config)
        accept_checkpoint = getattr(
            self._context_evidence_observer,
            "accept_checkpoint_linked",
            None,
        )
        if checkpoint_id is not None and repaired_provider_call_id is not None and callable(accept_checkpoint):
            accept_checkpoint(
                provider_call_id=repaired_provider_call_id,
                checkpoint_id=checkpoint_id,
            )
        return written_config

    async def aput_already_authorized(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
        *,
        session: AsyncSession,
    ) -> RunnableConfig:
        """Write while the caller holds the exact scoped Thread authority lock.

        This narrow adapter exists for compare-and-swap flows that must keep the
        final head check and checkpoint write inside one database lock cycle.
        The caller must already hold the scoped Thread lock; this method repeats
        the exact authority/row check in the same transaction as defense in
        depth before touching the raw saver.
        """

        if not session.in_transaction():
            raise PrivateWorkUnavailable(self._context.request_id)
        thread_id = self._thread_id(config)
        try:
            await self._revalidator.require(
                session,
                self._context,
                Capability.PRIVATE_WORK_CREATE,
                lock=True,
            )
            record = await PrivateThreadRepository(session).get(
                scope=self._context.resource_scope,
                thread_id=thread_id,
                lock=True,
                thread_kind=self._thread_kind,
            )
            if record is None:
                raise PrivateWorkNotFound(self._context.request_id)
            clean_config = self._sanitize_config(
                config,
                thread_id=thread_id,
            )
            source = await self._raw.aget_tuple(clean_config)
            if source is not None:
                self._validate_marker(source, thread_id=thread_id)
                await self._repair_memory_archive_receipt(
                    session,
                    source,
                    thread_id=thread_id,
                )
                await self._repair_context_compaction_receipt(
                    session,
                    source,
                    thread_id=thread_id,
                )
                await self._repair_context_provider_checkpoint(
                    session,
                    source,
                    thread_id=thread_id,
                )
            written_config = await self._raw.aput(
                clean_config,
                checkpoint,
                self._sanitize_metadata(metadata),
                new_versions,
            )
            item = await self._raw.aget_tuple(written_config)
            if item is None:
                raise PrivateWorkNotFound(self._context.request_id)
            self._validate_marker(item, thread_id=thread_id)
            await self._repair_memory_archive_receipt(
                session,
                item,
                thread_id=thread_id,
            )
            await self._repair_context_compaction_receipt(
                session,
                item,
                thread_id=thread_id,
            )
            await self._repair_context_provider_checkpoint(
                session,
                item,
                thread_id=thread_id,
            )
            return written_config
        except PrivateWorkError:
            raise
        except ContextProviderCallAmbiguousError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        async with self._locked_active(
            thread_id,
            Capability.PRIVATE_WORK_CREATE,
            "before_checkpoint_write",
        ):
            try:
                item = await self._raw.aget_tuple(clean_config)
                if item is not None:
                    self._validate_marker(item, thread_id=thread_id)
                # Pending writes are a first-class saver operation: LangGraph
                # may emit them before the matching checkpoint row exists.
                await self._raw.aput_writes(
                    clean_config,
                    writes,
                    task_id,
                    task_path,
                )
            except PrivateWorkError:
                raise
            except Exception:
                raise PrivateWorkUnavailable(self._context.request_id) from None

    async def aput_writes_already_authorized(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
        *,
        session: AsyncSession,
    ) -> None:
        """Write pending channel values inside a caller-owned transaction."""
        if not session.in_transaction():
            raise PrivateWorkUnavailable(self._context.request_id)
        thread_id = self._thread_id(config)
        clean_config = self._sanitize_config(config, thread_id=thread_id)
        try:
            await self._revalidator.require(
                session,
                self._context,
                Capability.PRIVATE_WORK_CREATE,
                lock=True,
            )
            record = await PrivateThreadRepository(session).get(
                scope=self._context.resource_scope,
                thread_id=thread_id,
                lock=True,
                thread_kind=self._thread_kind,
            )
            if record is None:
                raise PrivateWorkNotFound(self._context.request_id)
            item = await self._raw.aget_tuple(clean_config)
            if item is not None:
                self._validate_marker(item, thread_id=thread_id)
            await self._raw.aput_writes(
                clean_config,
                writes,
                task_id,
                task_path,
            )
        except PrivateWorkError:
            raise
        except Exception:
            raise PrivateWorkUnavailable(self._context.request_id) from None

    async def _cancel_thread_execution_approval(
        self,
        session: AsyncSession,
        row: ExecutionApprovalRequestRow,
        *,
        now: datetime,
    ) -> None:
        audit = self._approval_audit
        if audit is None:
            raise PrivateWorkUnavailable(self._context.request_id)
        if row.status not in EXECUTION_APPROVAL_ACTIVE_STATUSES:
            return
        terminal_status = "unknown" if row.status == "claimed" and row.spawn_authorized_at is not None else "cancelled"
        try:
            await transition_output_delivery_obligation_for_approval_terminal(
                session,
                approval=row,
                approval_status=terminal_status,
                now=now,
            )
        except OutputDeliveryObligationConflict:
            raise PrivateWorkConflict(self._context.request_id) from None
        row.status = terminal_status
        row.version += 1
        row.terminal_at = now
        row.updated_at = now
        await audit.host_execution_approval_terminal(
            session,
            project_id=row.project_id,
            source_run_id=row.source_run_id,
            status=terminal_status,
            request_id=self._context.request_id,
            occurred_at=now,
        )

    async def _prepare_thread_execution_approval_deletion(
        self,
        session: AsyncSession,
        thread_id: str,
    ) -> tuple[bool, set[str]]:
        """Close approvals after Job -> Run -> active attempt -> approval locks."""

        context = self._context
        discovered_runs = (
            await session.execute(
                select(RunRow.run_id, RunRow.job_id)
                .where(
                    RunRow.project_id == context.project_id,
                    RunRow.owner_user_id == str(context.user_id),
                    RunRow.thread_id == thread_id,
                )
                .order_by(RunRow.run_id)
            )
        ).all()
        discovered_run_ids = tuple(run_id for run_id, _job_id in discovered_runs)
        discovered_jobs = ()
        if discovered_run_ids:
            discovered_jobs = tuple(
                (
                    await session.execute(
                        select(JobRow.id, JobRow.run_id)
                        .where(
                            JobRow.project_id == context.project_id,
                            JobRow.owner_user_id == str(context.user_id),
                            JobRow.run_id.in_(discovered_run_ids),
                        )
                        .order_by(JobRow.id)
                    )
                ).all()
            )
        try:
            locked = await lock_execution_approval_private_rows(
                session,
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                extra_run_dependencies=tuple(
                    ApprovalRunDependency(
                        owner_user_id=str(context.user_id),
                        thread_id=thread_id,
                        run_id=run_id,
                        job_id=job_id,
                    )
                    for run_id, job_id in discovered_runs
                ),
                extra_job_dependencies=tuple(
                    ApprovalJobDependency(
                        owner_user_id=str(context.user_id),
                        run_id=run_id,
                        job_id=job_id,
                    )
                    for job_id, run_id in discovered_jobs
                ),
                lock_active_attempts=True,
            )
        except ExecutionApprovalPrivateLifecycleConflict:
            raise PrivateWorkConflict(context.request_id) from None

        # ``Run.job_id`` is nullable while the authoritative Job has its own
        # reverse ``run_id`` FK. Recheck the complete reverse set after the Run
        # locks so a concurrent admission cannot escape the deletion boundary.
        locked_reverse_job_ids: set[uuid.UUID] = set()
        if discovered_run_ids:
            locked_reverse_job_ids = set(
                (
                    await session.execute(
                        select(JobRow.id).where(
                            JobRow.project_id == context.project_id,
                            JobRow.owner_user_id == str(context.user_id),
                            JobRow.run_id.in_(discovered_run_ids),
                        )
                    )
                ).scalars()
            )
        if locked_reverse_job_ids != {job_id for job_id, _run_id in discovered_jobs}:
            raise PrivateWorkConflict(context.request_id)

        now = await session.scalar(select(func.clock_timestamp()))
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise PrivateWorkUnavailable(context.request_id)
        now = now.astimezone(UTC)
        rows = locked.rows
        audit = self._approval_audit
        synchronously_cancelled_runs: set[str] = set()
        jobs_by_run: dict[str, list[JobRow]] = {run_id: [] for run_id in locked.runs}
        for job in locked.jobs.values():
            if job.project_id != context.project_id or job.owner_user_id != str(context.user_id) or job.run_id not in jobs_by_run or job.job_type not in {"private_run", "automation_run"}:
                raise PrivateWorkConflict(context.request_id)
            assert job.run_id is not None
            jobs_by_run[job.run_id].append(job)

        for run_id in sorted(locked.runs):
            run = locked.runs[run_id]
            if run.project_id != context.project_id or run.owner_user_id != str(context.user_id) or run.thread_id != thread_id:
                raise PrivateWorkConflict(context.request_id)
            reverse_jobs = sorted(jobs_by_run[run_id], key=lambda item: item.id)
            jobs_by_id = {job.id: job for job in reverse_jobs}
            if run.job_id is not None and run.job_id not in jobs_by_id:
                raise PrivateWorkConflict(context.request_id)
            active_jobs: list[JobRow] = []
            unresolved_attempts = False
            for job in reverse_jobs:
                attempts = locked.active_attempts.get(job.id, ())
                if attempts:
                    unresolved_attempts = True
                    for attempt in attempts:
                        attempt.heartbeat_at = now
                        attempt.finished_at = now
                        attempt.outcome = "cancelled"
                        attempt.public_error_code = None
                if job.status not in {"queued", "leased", "running", "retry_wait"}:
                    continue
                active_jobs.append(job)
                job.cancel_requested_at = job.cancel_requested_at or now
                job.cancel_reason = job.cancel_reason or "thread_deleted"
                job.status = "cancelled"
                job.public_error_code = None
                job.lease_owner_id = None
                job.lease_token_hash = None
                job.lease_expires_at = None
                job.heartbeat_at = None
                job.completed_at = now
                job.updated_at = now

            run_active = run.status in {"pending", "running"}
            finalization_active = run.finalization_status == "finalizing"
            if not active_jobs and not unresolved_attempts and not run_active and not finalization_active:
                continue

            reason = "thread_deleted"
            if active_jobs or unresolved_attempts or run_active or finalization_active:
                run.cancel_requested_at = run.cancel_requested_at or now
                run.cancel_reason = run.cancel_reason or reason
                run.authorization_cancel_requested_at = run.authorization_cancel_requested_at or now
                run.authorization_cancel_reason = run.authorization_cancel_reason or reason
                run.execution_lease_token_hash = None
                run.execution_lease_expires_at = None
                run.execution_heartbeat_at = None
                run.updated_at = now
            if finalization_active:
                run.finalization_status = "failed"
            if run_active:
                projected_job = jobs_by_id.get(run.job_id) if run.job_id is not None else None
                terminal_authority = projected_job
                if terminal_authority is None and len(reverse_jobs) == 1:
                    terminal_authority = reverse_jobs[0]

                # A terminal projected Job is already the durable outcome
                # authority. Do not rewrite it backwards merely because its
                # Run projection was left active by an earlier partial
                # settlement. Any additional live reverse Jobs were revoked
                # above, while this Run/stream converges to the projected Job.
                if terminal_authority is not None and terminal_authority.status == "succeeded":
                    run.status = "success"
                    run.error = None
                    stream_status = "completed"
                    stream_error_code = None
                elif terminal_authority is not None and terminal_authority.status in {
                    "failed",
                    "dead",
                }:
                    run.status = "error"
                    run.error = terminal_authority.public_error_code
                    stream_status = "error"
                    stream_error_code = terminal_authority.public_error_code if terminal_authority.public_error_code in STREAM_TERMINAL_ERROR_CODES else None
                else:
                    run.status = "interrupted"
                    run.error = (None if terminal_authority is None else terminal_authority.cancel_reason) or run.authorization_cancel_reason or run.cancel_reason
                    stream_status = "interrupted"
                    stream_error_code = None
                try:
                    await self._run_event_store.ensure_settled_stream_terminal(
                        session,
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        run_id=run_id,
                        status=stream_status,
                        error_code=stream_error_code,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise PrivateWorkUnavailable(context.request_id) from None
                await self._quota.release_concurrent_run(
                    session,
                    context.resource_scope,
                    run_id=run_id,
                    request_id=context.request_id,
                )
                audit_job = terminal_authority or (active_jobs[0] if active_jobs else (reverse_jobs[0] if reverse_jobs else None))
                if run.status == "interrupted" and audit_job is not None:
                    if audit is None:
                        raise PrivateWorkUnavailable(context.request_id)
                    await audit.run_terminal(
                        session,
                        context.resource_scope,
                        run_id=run_id,
                        job_id=audit_job.id,
                        job_type=audit_job.job_type,
                        status="interrupted",
                        public_error_code=None,
                        request_id=context.request_id,
                    )
                synchronously_cancelled_runs.add(run_id)

        for row in rows:
            if row.status in EXECUTION_APPROVAL_ACTIVE_STATUSES:
                if audit is None:
                    raise PrivateWorkUnavailable(context.request_id)
                await self._cancel_thread_execution_approval(
                    session,
                    row,
                    now=now,
                )

        if rows:
            approval_ids = tuple(row.id for row in rows)
            await session.flush()
            await session.execute(
                delete(ExecutionApprovalResultReceiptRow).where(
                    ExecutionApprovalResultReceiptRow.project_id == context.project_id,
                    ExecutionApprovalResultReceiptRow.owner_user_id == str(context.user_id),
                    ExecutionApprovalResultReceiptRow.thread_id == thread_id,
                    ExecutionApprovalResultReceiptRow.approval_id.in_(
                        approval_ids,
                    ),
                )
            )
            await session.execute(
                delete(ExecutionApprovalRequestRow).where(
                    ExecutionApprovalRequestRow.project_id == context.project_id,
                    ExecutionApprovalRequestRow.owner_user_id == str(context.user_id),
                    ExecutionApprovalRequestRow.thread_id == thread_id,
                    ExecutionApprovalRequestRow.id.in_(approval_ids),
                )
            )
        return False, synchronously_cancelled_runs

    async def adelete_thread(
        self,
        thread_id: str,
        *,
        expected_version: int | None = None,
    ) -> None:
        """Tombstone a business Thread while retaining its raw checkpoint."""

        await self._atombstone_thread(
            thread_id,
            expected_version=expected_version,
            expected_created_at=None,
        )

    async def atombstone_compensated_create(
        self,
        thread_id: str,
        *,
        expected_version: int,
        expected_created_at: datetime,
    ) -> PrivateThreadRecord:
        """Hide one exact failed-create generation before destructive cleanup."""

        return await self._atombstone_thread(
            thread_id,
            expected_version=expected_version,
            expected_created_at=expected_created_at,
        )

    async def _atombstone_thread(
        self,
        thread_id: str,
        *,
        expected_version: int | None,
        expected_created_at: datetime | None,
    ) -> PrivateThreadRecord:
        context = require_issued_private_work_context(self._context)
        conflict_after_commit = False
        synchronously_cancelled_runs: set[str] = set()
        tombstone: PrivateThreadRecord | None = None
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._revalidator.require(
                        session,
                        context,
                        Capability.PRIVATE_WORK_CREATE,
                        lock=True,
                    )
                    repository = PrivateThreadRepository(session)
                    record = await repository.get(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        lock=True,
                        thread_kind=self._thread_kind,
                    )
                    if record is None:
                        tombstone = await repository.get_deleted(
                            scope=context.resource_scope,
                            thread_id=thread_id,
                            lock=True,
                            thread_kind=self._thread_kind,
                        )
                        if tombstone is None:
                            raise PrivateWorkNotFound(context.request_id)
                        if expected_created_at is not None and tombstone.created_at != expected_created_at:
                            raise PrivateWorkConflict(context.request_id)
                    else:
                        if expected_version is not None and record.version != expected_version:
                            raise PrivateWorkConflict(context.request_id)
                        if expected_created_at is not None and record.created_at != expected_created_at:
                            raise PrivateWorkConflict(context.request_id)
                        (
                            conflict_after_commit,
                            synchronously_cancelled_runs,
                        ) = await self._prepare_thread_execution_approval_deletion(
                            session,
                            thread_id,
                        )
                        if not conflict_after_commit:
                            tombstone = await repository.mark_deleted(
                                scope=context.resource_scope,
                                thread_id=thread_id,
                                expected_version=record.version,
                                thread_kind=self._thread_kind,
                            )
            for run_id in synchronously_cancelled_runs:
                try:
                    await self._automation_reconciler.handle_run_completion(
                        SimpleNamespace(run_id=run_id),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The authoritative Run/Job/Thread transaction is already
                    # committed. Automation recovery is independently durable.
                    pass
            if conflict_after_commit:
                raise PrivateWorkConflict(context.request_id)
            if tombstone is None or tombstone.deleted_at is None:
                raise PrivateWorkUnavailable(context.request_id)
            return tombstone
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

    async def acleanup_compensated_create(
        self,
        thread_id: str,
        *,
        expected_created_at: datetime,
        expected_deleted_at: datetime,
    ) -> bool:
        """Physically clean one already-hidden failed-create generation."""

        context = require_issued_private_work_context(self._context)
        candidate = None
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._revalidator.require(
                        session,
                        context,
                        Capability.PRIVATE_WORK_CREATE,
                        lock=True,
                    )
                    repository = PrivateThreadRepository(session)
                    tombstone = await repository.get_deleted(
                        scope=context.resource_scope,
                        thread_id=thread_id,
                        lock=True,
                        thread_kind=self._thread_kind,
                    )
                    if tombstone is None:
                        raise PrivateWorkNotFound(context.request_id)
                    if tombstone.created_at != expected_created_at:
                        raise PrivateWorkConflict(context.request_id)
                    if tombstone.deleted_at != expected_deleted_at:
                        raise PrivateWorkConflict(context.request_id)

                    if tombstone.checkpoint_delete_status == "not_requested":
                        await self._cleanup_compensated_thread_files(
                            session,
                            context,
                            thread_id,
                            deleted_at=tombstone.deleted_at,
                        )
                        tombstone = await repository.request_checkpoint_delete_for_compensation(
                            scope=context.resource_scope,
                            thread_id=thread_id,
                            expected_created_at=expected_created_at,
                            expected_deleted_at=expected_deleted_at,
                            thread_kind=self._thread_kind,
                        )
                    elif tombstone.checkpoint_delete_status == "complete":
                        return True
                    candidate = checkpoint_delete_candidate_from_record(
                        tombstone,
                    )
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None

        if candidate is None:
            raise PrivateWorkUnavailable(context.request_id)
        return await recover_checkpoint_delete_candidate(
            self._raw,
            self._session_factory,
            candidate,
        )

    async def _cleanup_compensated_thread_files(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        deleted_at: datetime | None,
    ) -> None:
        """Release and hide files only for a failed create/branch cleanup."""

        if deleted_at is None:
            raise PrivateWorkUnavailable(context.request_id)
        ready_files = (
            (
                await session.execute(
                    select(PrivateFileRow)
                    .where(
                        PrivateFileRow.project_id == context.project_id,
                        PrivateFileRow.owner_user_id == str(context.user_id),
                        PrivateFileRow.thread_id == thread_id,
                        PrivateFileRow.status == "ready",
                    )
                    .with_for_update(of=PrivateFileRow)
                )
            )
            .scalars()
            .all()
        )
        for file_row in ready_files:
            await self._quota.release_file(
                session,
                context.resource_scope,
                file_id=file_row.id,
                size=file_row.size,
                request_id=context.request_id,
            )
        await session.execute(
            update(PrivateFileRow)
            .where(
                PrivateFileRow.project_id == context.project_id,
                PrivateFileRow.owner_user_id == str(context.user_id),
                PrivateFileRow.thread_id == thread_id,
                PrivateFileRow.status != "deleted",
            )
            .values(
                status="deleted",
                deleted_at=deleted_at,
                updated_at=deleted_at,
            )
        )
        await session.execute(
            update(PrivateArtifactRow)
            .where(
                PrivateArtifactRow.project_id == context.project_id,
                PrivateArtifactRow.owner_user_id == str(context.user_id),
                PrivateArtifactRow.thread_id == thread_id,
                PrivateArtifactRow.deleted_at.is_(None),
            )
            .values(deleted_at=deleted_at)
        )

    def _run_sync(self, coroutine_factory: Callable[[], Awaitable[_T]]) -> _T:
        if not self._owner_loop.is_running():
            raise PrivateWorkUnavailable(self._context.request_id)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self._owner_loop:
            raise PrivateWorkUnavailable(self._context.request_id)
        return cast(
            _T,
            asyncio.run_coroutine_threadsafe(
                coroutine_factory(),
                self._owner_loop,
            ).result(),
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self._run_sync(lambda: self.aget_tuple(config))

    def get(self, config: RunnableConfig) -> Checkpoint | None:
        return self._run_sync(lambda: self.aget(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        async def collect() -> list[CheckpointTuple]:
            return [
                item
                async for item in self.alist(
                    config,
                    filter=filter,
                    before=before,
                    limit=limit,
                )
            ]

        return iter(self._run_sync(collect))

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self._run_sync(lambda: self.aput(config, checkpoint, metadata, new_versions))

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._run_sync(lambda: self.aput_writes(config, writes, task_id, task_path))

    def delete_thread(
        self,
        thread_id: str,
        *,
        expected_version: int | None = None,
    ) -> None:
        self._run_sync(
            lambda: self.adelete_thread(
                thread_id,
                expected_version=expected_version,
            )
        )


class _AlreadyAuthorizedCheckpointSaver(BaseCheckpointSaver):
    """Async saver facade pinned to one existing SQL transaction."""

    def __init__(
        self,
        saver: _ScopedCheckpointSaver,
        session: AsyncSession,
    ) -> None:
        super().__init__(serde=saver.serde)
        self._saver = saver
        self._session = session

    @property
    def config_specs(self):
        return self._saver.config_specs

    def get_next_version(self, current, channel):
        return self._saver.get_next_version(current, channel)

    async def aget_tuple(
        self,
        config: RunnableConfig,
    ) -> CheckpointTuple | None:
        return await self._saver.aget_tuple_already_authorized(
            config,
            session=self._session,
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async for item in self._saver.alist_already_authorized(
            config,
            session=self._session,
            filter=filter,
            before=before,
            limit=limit,
        ):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await self._saver.aput_already_authorized(
            config,
            checkpoint,
            metadata,
            new_versions,
            session=self._session,
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self._saver.aput_writes_already_authorized(
            config,
            writes,
            task_id,
            task_path,
            session=self._session,
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise PrivateWorkUnavailable(self._saver._context.request_id)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        del config, filter, before, limit
        raise PrivateWorkUnavailable(self._saver._context.request_id)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del config, checkpoint, metadata, new_versions
        raise PrivateWorkUnavailable(self._saver._context.request_id)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        del config, writes, task_id, task_path
        raise PrivateWorkUnavailable(self._saver._context.request_id)
