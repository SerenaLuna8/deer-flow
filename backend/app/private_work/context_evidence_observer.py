"""Lease-authorized Worker observer for durable Context Evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.context_projection import (
    ContextProjectionTransaction,
    context_evidence_record_to_core,
)
from app.private_work.context_replacement import compaction_checkpoint_receipt
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from deerflow.error_codes import ContextProviderCallAmbiguousError
from deerflow.persistence.context_evidence import (
    ContextEvidenceRecord,
    ContextEvidenceRepository,
    ContextEvidenceScope,
    ContextEvidenceType,
    ContextSubjectRef,
)
from deerflow.runtime.context_evidence import (
    CheckpointLinkedV1,
    CompactionAuthority,
    CompactionCommittedV1,
    CompactionProjection,
    ContextCheckpointEstimator,
    ContextCheckpointProjectionSnapshot,
    ContextCompactionCheckpointReceipt,
    ContextContribution,
    ContextLane,
    ContextModelProjection,
    ContextProjectionSource,
    ContextRebaseReason,
    ContextSubject,
    ContextWindowGeneration,
    FinalRequestMeasurement,
    ProjectionFreshness,
    ProjectionPhase,
    ProviderAmbiguityReason,
    ProviderAmbiguousV1,
    ProviderCallDisposition,
    ProviderCallIdentity,
    ProviderFailedV1,
    ProviderObservedV1,
    ProviderRetrySafety,
    ProviderUsageUnreportedV1,
    RequestDispatchedV1,
    RequestPreparedV1,
    TokenEstimate,
    WindowOpenedV1,
    WindowRebasedV1,
    resolve_provider_call,
)


@dataclass(frozen=True, slots=True)
class _PreparedCall:
    provider_call: ProviderCallIdentity
    source: ContextProjectionSource


class ContextProviderCallAmbiguous(ContextProviderCallAmbiguousError):
    """A previously dispatched Provider call cannot be repeated safely."""

    def __init__(self, provider_call_id: str) -> None:
        self.provider_call_id = provider_call_id
        super().__init__("CONTEXT_PROVIDER_CALL_AMBIGUOUS")


class PrivateRunContextEvidenceObserver:
    """Persist one Lead or Sub-Agent Context Subject under the active Job lease."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        context: PrivateWorkContext,
        boundary: object,
        thread_id: str,
        run_id: str,
        subject: ContextSubject,
        model_identity_digest: str,
        context_window_tokens: int | None,
        compaction_enabled: bool,
        compaction_threshold_tokens: int | None,
        source_checkpoint_id: str | None = None,
        rebase_reason: ContextRebaseReason | None = None,
        subagent_model_context: Mapping[
            str,
            tuple[str, int, int | None],
        ]
        | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._context = require_issued_private_work_context(context)
        self._boundary = boundary
        self._thread_id = thread_id
        self._run_id = run_id
        if subject.thread_id != thread_id:
            raise ValueError("Context Subject does not belong to the Thread")
        self._subject = subject
        self._subject_ref = ContextSubjectRef.lead_thread(thread_id) if subject.execution_id is None else ContextSubjectRef.subagent_task(subject.execution_id)
        self._model = ContextModelProjection(
            identity_digest=model_identity_digest,
            context_window_tokens=context_window_tokens,
        )
        self._compaction_enabled = compaction_enabled
        self._compaction_threshold_tokens = compaction_threshold_tokens
        self._source_checkpoint_id = source_checkpoint_id
        if rebase_reason is not None and not isinstance(
            rebase_reason,
            ContextRebaseReason,
        ):
            raise TypeError("Context rebase reason is invalid")
        if rebase_reason is not None and subject.execution_id is not None:
            raise ValueError("Sub-Agent Task cannot use Thread rebase authority")
        self._rebase_reason = rebase_reason
        self._subagent_model_context = dict(subagent_model_context or {})
        self._revalidator = PrivateWorkRevalidator()
        self._lock = asyncio.Lock()
        self._prepared: dict[str, _PreparedCall] = {}
        self._last_result_call_id: str | None = None
        self._last_source: ContextProjectionSource | None = None
        self._settled = False

    @property
    def subject(self) -> ContextSubject:
        return self._subject

    @property
    def run_id(self) -> str:
        return self._run_id

    def accept_checkpoint_linked(
        self,
        *,
        provider_call_id: str,
        checkpoint_id: str,
    ) -> None:
        """Advance process-local state after the app transaction has linked."""

        prepared = self._prepared.get(provider_call_id)
        if prepared is not None:
            self._prepared[provider_call_id] = _PreparedCall(
                provider_call=prepared.provider_call,
                source=prepared.source.model_copy(
                    update={"checkpoint_id": checkpoint_id},
                ),
            )
            self._last_source = self._prepared[provider_call_id].source
        if self._last_result_call_id == provider_call_id:
            self._last_result_call_id = None
        self._source_checkpoint_id = checkpoint_id

    def create_subagent_observer(
        self,
        execution_id: uuid.UUID,
        model_name: str,
    ) -> PrivateRunContextEvidenceObserver:
        """Create one Task-private observer from the frozen parent Run models."""

        if not isinstance(execution_id, uuid.UUID):
            raise TypeError("Sub-Agent execution identity must be a UUID")
        model_context = self._subagent_model_context.get(model_name)
        if model_context is None:
            raise RuntimeError("Sub-Agent model has no frozen Context authority")
        model_identity_digest, context_window_tokens, threshold_tokens = model_context
        return PrivateRunContextEvidenceObserver(
            self._session_factory,
            context=self._context,
            boundary=self._boundary,
            thread_id=self._thread_id,
            run_id=self._run_id,
            subject=ContextSubject.subagent_task(
                thread_id=self._thread_id,
                execution_id=execution_id,
            ),
            model_identity_digest=model_identity_digest,
            context_window_tokens=context_window_tokens,
            compaction_enabled=self._compaction_enabled,
            compaction_threshold_tokens=threshold_tokens,
            source_checkpoint_id=f"task-state:{execution_id}",
        )

    def checkpoint_projection_snapshot(
        self,
        *,
        estimator: ContextCheckpointEstimator,
        provider_call: ProviderCallIdentity,
        origin_run_id: str,
        provider_response_message_start: int,
        provider_response_message_count: int,
        provider_response_digest: str,
    ) -> ContextCheckpointProjectionSnapshot:
        """Return safe rebuild input for the checkpoint written after a call."""

        source = self._last_source
        if source is None:
            raise RuntimeError("Context Projection source is unavailable")
        if (
            self._last_result_call_id != provider_call.provider_call_id
            or origin_run_id != self._run_id
            or provider_call.subject != self._subject
            or provider_call.generation != source.generation
            or provider_call.request_fingerprint != source.measurement.request_fingerprint
        ):
            raise RuntimeError("Context checkpoint Provider authority is invalid")
        return ContextCheckpointProjectionSnapshot(
            generation=source.generation,
            model=source.model,
            measurement=source.measurement,
            compaction=source.compaction,
            estimator=estimator,
            provider_call_id=provider_call.provider_call_id,
            provider_subject=provider_call.subject,
            origin_run_id=origin_run_id,
            provider_response_message_start=provider_response_message_start,
            provider_response_message_count=provider_response_message_count,
            provider_response_digest=provider_response_digest,
        )

    def prepare_compaction_checkpoint_receipt(
        self,
        *,
        source_checkpoint_id: str,
        source_snapshot: Mapping[str, object] | None,
        estimator: ContextCheckpointEstimator,
        source_state_digest: str,
        source_tokens: int,
        result_values: Mapping[str, object],
    ) -> ContextCompactionCheckpointReceipt:
        """Prepare safe repair authority before a Lead checkpoint commit."""

        if self._subject.execution_id is not None:
            raise RuntimeError("Sub-Agent compaction has no checkpoint receipt")
        if source_snapshot is None:
            source_identity = hashlib.sha256(f"lead-pre-provider-compaction-v1:{source_state_digest}".encode()).hexdigest()
            contribution = ContextContribution(
                contribution_id=hashlib.sha256(f"lead-pre-provider-compaction-v1:{source_identity}".encode()).hexdigest(),
                source_identity_digest=source_identity,
                lane=ContextLane.CONVERSATION,
                model_visible_bytes=0,
                token_estimate=TokenEstimate.bounded(
                    projected_tokens=source_tokens,
                    lower_bound_tokens=0,
                    safety_upper_bound_tokens=source_tokens,
                ),
            )
            measurement = FinalRequestMeasurement(
                request_fingerprint=source_state_digest,
                adapter_revision="lead-pre-provider-compaction-v1",
                contributions=(contribution,),
            )
            snapshot = ContextCheckpointProjectionSnapshot(
                generation=ContextWindowGeneration(
                    generation_id=uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"actweave-lead-state-v1:{self._thread_id}:{self._run_id}:{source_checkpoint_id}:{source_state_digest}",
                    ),
                ),
                model=self._model,
                measurement=measurement,
                compaction=self._compaction(measurement),
                estimator=estimator,
            )
        else:
            snapshot = ContextCheckpointProjectionSnapshot.from_safe_mapping(
                source_snapshot,
            )
        return compaction_checkpoint_receipt(
            snapshot,
            source_checkpoint_id=source_checkpoint_id,
            checkpoint_values=result_values,
            result_generation=ContextWindowGeneration(
                generation_id=uuid.uuid4(),
            ),
            phase=ProjectionPhase.ACTIVE,
            origin_run_id=self._run_id,
        )

    async def record_ephemeral_compaction_committed(
        self,
        *,
        source_state_digest: str,
        result_state_digest: str,
        source_tokens: int,
        result_tokens: int,
        summary_tokens: int,
        summary_digest: str,
    ) -> None:
        """Commit one checkpoint-free Sub-Agent history replacement."""

        if self._subject.execution_id is None:
            raise RuntimeError("Lead compaction requires a checkpoint receipt")
        if result_tokens > source_tokens or summary_tokens > result_tokens:
            raise ValueError("Sub-Agent compaction Token sizes are invalid")
        for digest in (
            source_state_digest,
            result_state_digest,
            summary_digest,
        ):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("Sub-Agent compaction digest is invalid")
        receipt_id = hashlib.sha256(
            json.dumps(
                {
                    "execution_id": self._subject.execution_id,
                    "result_state_digest": result_state_digest,
                    "source_state_digest": source_state_digest,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        result_generation = ContextWindowGeneration(
            generation_id=uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"actweave-subagent-compaction-v1:{self._subject.execution_id}:{receipt_id}",
            ),
        )
        async with self._lock, self._authorized_session() as session:
            repository = ContextEvidenceRepository(session)
            committed = await self._subject_event_records(
                repository,
                "compaction.committed.v1",
            )
            if any(record.payload.get("receipt_id") == receipt_id for record in committed):
                return
            head = await repository.read_head(
                self._scope,
                self._subject_ref,
                lock=True,
            )
            if head is not None:
                source_generation = ContextWindowGeneration(
                    generation_id=head.context_window_generation,
                )
            else:
                latest = await repository.read_latest_subject_evidence(
                    self._scope,
                    self._subject_ref,
                )
                if latest is not None:
                    source_generation = ContextWindowGeneration(
                        generation_id=latest.context_window_generation,
                    )
                else:
                    source_generation = ContextWindowGeneration(
                        generation_id=uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"actweave-subagent-state-v1:{self._subject.execution_id}:{source_state_digest}",
                        ),
                    )
            contributions = []
            if self._last_source is not None:
                contributions.extend(
                    item
                    for item in self._last_source.measurement.contributions
                    if item.lane
                    not in {
                        ContextLane.SUMMARIZED_CONVERSATION,
                        ContextLane.CONVERSATION,
                        ContextLane.VISUAL_MEDIA,
                    }
                )

            def contribution(
                lane: ContextLane,
                tokens: int,
                identity: str,
            ) -> ContextContribution | None:
                if tokens <= 0:
                    return None
                source_identity = hashlib.sha256(f"{lane.value}:{identity}".encode()).hexdigest()
                return ContextContribution(
                    contribution_id=hashlib.sha256(f"subagent-compaction-v1:{lane.value}:{source_identity}".encode()).hexdigest(),
                    source_identity_digest=source_identity,
                    lane=lane,
                    model_visible_bytes=0,
                    token_estimate=TokenEstimate.bounded(
                        projected_tokens=tokens,
                        lower_bound_tokens=0,
                        safety_upper_bound_tokens=(tokens + max(1, (tokens + 3) // 4)),
                    ),
                )

            conversation = contribution(
                ContextLane.CONVERSATION,
                result_tokens - summary_tokens,
                result_state_digest,
            )
            summary = contribution(
                ContextLane.SUMMARIZED_CONVERSATION,
                summary_tokens,
                summary_digest,
            )
            if conversation is not None:
                contributions.append(conversation)
            if summary is not None:
                contributions.append(summary)
            contributions.sort(
                key=lambda item: (
                    tuple(ContextLane).index(item.lane),
                    item.contribution_id,
                )
            )
            measurement = FinalRequestMeasurement(
                request_fingerprint=result_state_digest,
                adapter_revision="subagent-compaction-v1",
                contributions=tuple(contributions),
            )
            source = ContextProjectionSource(
                subject=self._subject,
                phase=ProjectionPhase.ACTIVE,
                generation=result_generation,
                checkpoint_id=None,
                model=self._model,
                measurement=measurement,
                current_provider_call_id=None,
                compaction=self._compaction(measurement),
                freshness=ProjectionFreshness.STALE,
            )
            await ContextProjectionTransaction(repository).append_and_project(
                scope=self._scope,
                source=source,
                payloads=(
                    self._window_opened(),
                    CompactionCommittedV1(
                        receipt_id=receipt_id,
                        source_state_digest=source_state_digest,
                        result_state_digest=result_state_digest,
                        source_generation=source_generation,
                        result_generation=result_generation,
                        source_tokens=source_tokens,
                        result_tokens=result_tokens,
                        summary_tokens=summary_tokens,
                        summary_digest=summary_digest,
                    ),
                ),
                origin_run_id=self._run_id,
                active_run_id=self._run_id,
            )
            self._last_source = source

    async def record_window_rebased(
        self,
        *,
        reason: ContextRebaseReason,
        source_checkpoint_id: str,
        result_checkpoint_id: str,
    ) -> None:
        """Commit one same-Thread checkpoint history replacement."""

        if self._subject.execution_id is not None:
            raise RuntimeError("Sub-Agent Task has no checkpoint history")
        if reason not in {
            ContextRebaseReason.ROLLBACK,
            ContextRebaseReason.HISTORY_REPLACEMENT,
        }:
            raise ValueError("Worker Context rebase reason is invalid")
        generation = ContextWindowGeneration(
            generation_id=uuid.uuid5(
                uuid.NAMESPACE_URL,
                ":".join(
                    (
                        "actweave-context-rebase-v1",
                        self._thread_id,
                        self._run_id,
                        reason.value,
                        source_checkpoint_id,
                        result_checkpoint_id,
                    )
                ),
            ),
        )
        digest = hashlib.sha256(
            json.dumps(
                {
                    "reason": reason.value,
                    "result_checkpoint_id": result_checkpoint_id,
                    "source_checkpoint_id": source_checkpoint_id,
                    "thread_id": self._thread_id,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        async with (
            self._lock,
            self._authorized_session(
                allow_cancel_requested=True,
            ) as session,
        ):
            repository = ContextEvidenceRepository(session)
            rebases = await self._subject_event_records(
                repository,
                "context.window.rebased.v1",
                origin_run_id=self._run_id,
                checkpoint_id=result_checkpoint_id,
            )
            existing = next(
                (record for record in reversed(rebases) if record.payload.get("reason") == reason.value and record.payload.get("result_checkpoint_id") == result_checkpoint_id),
                None,
            )
            if existing is not None:
                return
            links = await self._subject_event_records(
                repository,
                "checkpoint.linked.v1",
                checkpoint_id=result_checkpoint_id,
            )
            linked = next(
                (
                    CheckpointLinkedV1.model_validate_json(
                        json.dumps(record.payload, separators=(",", ":")),
                    )
                    for record in reversed(links)
                ),
                None,
            )
            call_records = (
                await self._provider_call_records(
                    repository,
                    linked.provider_call_id,
                )
                if linked is not None
                else ()
            )
            prepared = next(
                (
                    RequestPreparedV1.model_validate_json(
                        json.dumps(
                            record.payload,
                            separators=(",", ":"),
                        ),
                    )
                    for record in reversed(call_records)
                    if record.event_type == "request.prepared.v1"
                ),
                None,
            )
            windows = (
                await self._subject_event_records(
                    repository,
                    "context.window.opened.v1",
                    generation_id=uuid.UUID(
                        prepared.provider_call.generation.generation_id,
                    ),
                )
                if prepared is not None
                else ()
            )
            opened = (
                next(
                    (
                        WindowOpenedV1.model_validate_json(
                            json.dumps(
                                record.payload,
                                separators=(",", ":"),
                            ),
                        )
                        for record in reversed(windows)
                    ),
                    None,
                )
                if prepared is not None
                else None
            )
            if prepared is not None and opened is not None:
                measurement = prepared.measurement
                model = ContextModelProjection(
                    identity_digest=opened.model_identity_digest,
                    context_window_tokens=opened.context_window_tokens,
                )
                compaction = CompactionProjection(
                    enabled=opened.compaction_enabled,
                    threshold_tokens=opened.compaction_threshold_tokens,
                    reached=bool(opened.compaction_enabled and opened.compaction_threshold_tokens is not None and measurement.projected_tokens >= opened.compaction_threshold_tokens),
                    authority=(CompactionAuthority(opened.compaction_authority) if opened.compaction_authority is not None else None),
                )
            else:
                measurement = FinalRequestMeasurement(
                    request_fingerprint=digest,
                    adapter_revision="context-rebase-empty-v1",
                    contributions=(),
                )
                model = self._model
                compaction = self._compaction(measurement)
            source = ContextProjectionSource(
                subject=self._subject,
                phase=ProjectionPhase.ACTIVE,
                generation=generation,
                checkpoint_id=result_checkpoint_id,
                model=model,
                measurement=measurement,
                current_provider_call_id=None,
                compaction=compaction,
                freshness=ProjectionFreshness.CURRENT,
            )
            await ContextProjectionTransaction(repository).append_and_project(
                scope=self._scope,
                source=source,
                payloads=(
                    WindowRebasedV1(
                        reason=reason,
                        source_checkpoint_id=source_checkpoint_id,
                        result_checkpoint_id=result_checkpoint_id,
                        result_generation=generation,
                        history_digest=digest,
                    ),
                ),
                origin_run_id=self._run_id,
                active_run_id=self._run_id,
            )
            self._last_source = source

    @asynccontextmanager
    async def _authorized_session(
        self,
        *,
        allow_cancel_requested: bool = False,
    ) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session, session.begin():
            locked_context = await self._revalidator.require(
                session,
                self._context,
                Capability.PRIVATE_WORK_CREATE,
                Capability.SHARED_ASSETS_EXECUTE,
                lock_mode="share",
            )
            boundary_method = "lock_and_assert_context_evidence_settlement_in_session" if allow_cancel_requested else "lock_and_assert_materialization_active_in_session"
            assert_active = getattr(self._boundary, boundary_method, None)
            if not callable(assert_active):
                raise RuntimeError("Context Evidence requires an execution lease boundary")
            await assert_active(session, locked_context)
            thread = await PrivateThreadRepository(session).get(
                scope=self._context.resource_scope,
                thread_id=self._thread_id,
                lock=True,
            )
            if thread is None:
                raise RuntimeError("Context Evidence Thread is unavailable")
            yield session

    @property
    def _scope(self) -> ContextEvidenceScope:
        return ContextEvidenceScope.from_resource(
            self._context.resource_scope,
            self._thread_id,
        )

    async def _subject_event_records(
        self,
        repository: ContextEvidenceRepository,
        event_type: ContextEvidenceType,
        *,
        origin_run_id: str | None = None,
        generation_id: uuid.UUID | None = None,
        checkpoint_id: str | None = None,
    ) -> tuple[ContextEvidenceRecord, ...]:
        records: list[ContextEvidenceRecord] = []
        cursor = 0
        while True:
            page = await repository.page_subject_event_evidence(
                self._scope,
                self._subject_ref,
                event_type,
                origin_run_id=origin_run_id,
                generation_id=generation_id,
                checkpoint_id=checkpoint_id,
                after_seq=cursor,
                limit=1000,
            )
            records.extend(page)
            if len(page) < 1000:
                return tuple(records)
            cursor = page[-1].evidence_seq

    async def _provider_call_records(
        self,
        repository: ContextEvidenceRepository,
        provider_call_id: str,
    ) -> tuple[ContextEvidenceRecord, ...]:
        records: list[ContextEvidenceRecord] = []
        cursor = 0
        while True:
            page = await repository.page_provider_call_evidence(
                self._scope,
                self._subject_ref,
                provider_call_id,
                after_seq=cursor,
                limit=1000,
            )
            records.extend(page)
            if len(page) < 1000:
                return tuple(records)
            cursor = page[-1].evidence_seq

    def _compaction(self, measurement: FinalRequestMeasurement) -> CompactionProjection:
        threshold = self._compaction_threshold_tokens
        return CompactionProjection(
            enabled=self._compaction_enabled,
            threshold_tokens=threshold,
            reached=bool(self._compaction_enabled and threshold is not None and measurement.projected_tokens >= threshold),
            authority=(CompactionAuthority.FROZEN_RUN if self._compaction_enabled else None),
        )

    def _window_opened(self) -> WindowOpenedV1:
        return WindowOpenedV1(
            model_identity_digest=self._model.identity_digest,
            context_window_tokens=self._model.context_window_tokens,
            compaction_enabled=self._compaction_enabled,
            compaction_threshold_tokens=(self._compaction_threshold_tokens if self._compaction_enabled else None),
            compaction_authority=(CompactionAuthority.FROZEN_RUN.value if self._compaction_enabled else None),
        )

    async def _prepare_source(
        self,
        repository: ContextEvidenceRepository,
        measurement: FinalRequestMeasurement,
    ) -> tuple[
        ContextProjectionSource,
        ProviderCallIdentity,
        bool,
        bool,
        ProviderAmbiguousV1 | None,
    ]:
        head = await repository.read_head(
            self._scope,
            self._subject_ref,
            lock=True,
        )
        if self._rebase_reason is not None and self._source_checkpoint_id is not None and head is not None and head.checkpoint_id is not None and head.checkpoint_id != self._source_checkpoint_id:
            generation = ContextWindowGeneration(
                generation_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    ":".join(
                        (
                            "actweave-context-rebase-v1",
                            self._thread_id,
                            self._run_id,
                            self._rebase_reason.value,
                            head.checkpoint_id,
                            self._source_checkpoint_id,
                        )
                    ),
                ),
            )
            replacement_source = ContextProjectionSource(
                subject=self._subject,
                phase=ProjectionPhase.ACTIVE,
                generation=generation,
                checkpoint_id=self._source_checkpoint_id,
                model=self._model,
                measurement=measurement,
                current_provider_call_id=None,
                compaction=self._compaction(measurement),
                freshness=ProjectionFreshness.CURRENT,
            )
            replacement_digest = hashlib.sha256(
                json.dumps(
                    {
                        "reason": self._rebase_reason.value,
                        "request_fingerprint": measurement.request_fingerprint,
                        "result_checkpoint_id": self._source_checkpoint_id,
                        "source_checkpoint_id": head.checkpoint_id,
                        "thread_id": self._thread_id,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            await ContextProjectionTransaction(repository).append_and_project(
                scope=self._scope,
                source=replacement_source,
                payloads=(
                    WindowRebasedV1(
                        reason=self._rebase_reason,
                        source_checkpoint_id=head.checkpoint_id,
                        result_checkpoint_id=self._source_checkpoint_id,
                        result_generation=generation,
                        history_digest=replacement_digest,
                    ),
                ),
                origin_run_id=self._run_id,
                active_run_id=self._run_id,
            )
            checkpoint_id = self._source_checkpoint_id
            self._last_source = replacement_source
        elif head is not None:
            generation = ContextWindowGeneration(
                generation_id=head.context_window_generation,
            )
            checkpoint_id = head.checkpoint_id
        elif (
            latest := await repository.read_latest_subject_evidence(
                self._scope,
                self._subject_ref,
            )
        ) is not None:
            generation = ContextWindowGeneration(
                generation_id=latest.context_window_generation,
            )
            checkpoint_id = None
        else:
            generation = ContextWindowGeneration(generation_id=uuid.uuid4())
            checkpoint_id = None
        checkpoint_identity = checkpoint_id or self._source_checkpoint_id or f"run:{self._run_id}"
        latest_prepared: tuple[object, RequestPreparedV1] | None = None
        run_prepared = await self._subject_event_records(
            repository,
            "request.prepared.v1",
            origin_run_id=self._run_id,
            generation_id=uuid.UUID(generation.generation_id),
        )
        for record in reversed(run_prepared):
            try:
                prepared_payload = RequestPreparedV1.model_validate_json(
                    json.dumps(record.payload, separators=(",", ":")),
                )
            except (TypeError, ValueError):
                raise RuntimeError("Stored Context Evidence is invalid") from None
            if prepared_payload.measurement.request_fingerprint == measurement.request_fingerprint:
                latest_prepared = (record, prepared_payload)
                break
        if latest_prepared is not None:
            _prepared_record, prepared_payload = latest_prepared
            existing_call = prepared_payload.provider_call
            try:
                call_records = await self._provider_call_records(
                    repository,
                    existing_call.provider_call_id,
                )
                plan = resolve_provider_call(
                    tuple(context_evidence_record_to_core(record) for record in call_records),
                    existing_call.provider_call_id,
                )
            except (TypeError, ValueError):
                raise RuntimeError("Stored Provider lifecycle Evidence is invalid") from None
            if plan.disposition is not ProviderCallDisposition.RETRY_PROVEN_SAFE_FAILURE:
                existing_source = ContextProjectionSource(
                    subject=self._subject,
                    phase=ProjectionPhase.ACTIVE,
                    generation=existing_call.generation,
                    checkpoint_id=checkpoint_id,
                    model=self._model,
                    measurement=prepared_payload.measurement,
                    current_provider_call_id=existing_call.provider_call_id,
                    compaction=self._compaction(prepared_payload.measurement),
                    freshness=ProjectionFreshness.CURRENT,
                )
                if plan.disposition is ProviderCallDisposition.DISPATCH:
                    return (
                        existing_source,
                        existing_call,
                        False,
                        True,
                        None,
                    )
                if plan.disposition is ProviderCallDisposition.MARK_AMBIGUOUS:
                    return (
                        existing_source,
                        existing_call,
                        False,
                        True,
                        ProviderAmbiguousV1(
                            provider_call_id=existing_call.provider_call_id,
                            reason=(ProviderAmbiguityReason.DISPATCH_OUTCOME_UNKNOWN),
                        ),
                    )
                self._last_source = existing_source
                raise ContextProviderCallAmbiguous(
                    existing_call.provider_call_id,
                )
        ordinal = await repository.count_subject_run_prepared_requests(
            self._scope,
            self._subject_ref,
            self._run_id,
        )
        provider_call = ProviderCallIdentity.derive(
            subject=self._subject,
            generation=generation,
            source_checkpoint_id=checkpoint_identity,
            graph_step="model",
            model_call_ordinal=ordinal,
            request_fingerprint=measurement.request_fingerprint,
        )
        source = ContextProjectionSource(
            subject=self._subject,
            phase=ProjectionPhase.ACTIVE,
            generation=generation,
            checkpoint_id=checkpoint_id,
            model=self._model,
            measurement=measurement,
            current_provider_call_id=provider_call.provider_call_id,
            compaction=self._compaction(measurement),
            freshness=ProjectionFreshness.CURRENT,
        )
        expected_window = self._window_opened()
        opened = True
        windows = await self._subject_event_records(
            repository,
            "context.window.opened.v1",
            generation_id=uuid.UUID(generation.generation_id),
        )
        for record in reversed(windows):
            try:
                stored_window = WindowOpenedV1.model_validate_json(
                    json.dumps(record.payload, separators=(",", ":")),
                )
            except (TypeError, ValueError):
                raise RuntimeError("Stored Context Window Evidence is invalid") from None
            if stored_window == expected_window:
                opened = False
                break
        return source, provider_call, opened, False, None

    async def record_request_prepared(
        self,
        measurement: FinalRequestMeasurement,
        /,
    ) -> ProviderCallIdentity:
        async with self._lock, self._authorized_session() as session:
            repository = ContextEvidenceRepository(session)
            (
                source,
                provider_call,
                opened,
                reused_prepared,
                recovery_ambiguity,
            ) = await self._prepare_source(
                repository,
                measurement,
            )
            if recovery_ambiguity is not None:
                await ContextProjectionTransaction(repository).append_and_project(
                    scope=self._scope,
                    source=source,
                    payloads=(recovery_ambiguity,),
                    origin_run_id=self._run_id,
                    active_run_id=self._run_id,
                )
                self._last_source = source
                raise ContextProviderCallAmbiguous(
                    provider_call.provider_call_id,
                )
            payloads = []
            if opened:
                payloads.append(self._window_opened())
            if not reused_prepared:
                payloads.append(
                    RequestPreparedV1(
                        provider_call=provider_call,
                        measurement=measurement,
                    )
                )
            if payloads:
                await ContextProjectionTransaction(repository).append_only(
                    scope=self._scope,
                    source=source,
                    payloads=tuple(payloads),
                    origin_run_id=self._run_id,
                )
            self._prepared[provider_call.provider_call_id] = _PreparedCall(
                provider_call=provider_call,
                source=source,
            )
            self._last_source = source
            return provider_call

    def _call(self, provider_call: ProviderCallIdentity) -> _PreparedCall:
        prepared = self._prepared.get(provider_call.provider_call_id)
        if prepared is None or prepared.provider_call != provider_call:
            raise RuntimeError("Provider call has no process-owned prepared state")
        return prepared

    async def record_request_dispatched(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None:
        async with self._lock, self._authorized_session() as session:
            prepared = self._call(provider_call)
            await ContextProjectionTransaction(ContextEvidenceRepository(session)).append_only(
                scope=self._scope,
                source=prepared.source,
                payloads=(
                    RequestDispatchedV1(
                        provider_call_id=provider_call.provider_call_id,
                    ),
                ),
                origin_run_id=self._run_id,
            )

    async def _record_terminal(
        self,
        provider_call: ProviderCallIdentity,
        payload: BaseModel,
    ) -> None:
        async with (
            self._lock,
            self._authorized_session(
                allow_cancel_requested=True,
            ) as session,
        ):
            prepared = self._call(provider_call)
            await ContextProjectionTransaction(ContextEvidenceRepository(session)).append_and_project(
                scope=self._scope,
                source=prepared.source,
                payloads=(payload,),
                origin_run_id=self._run_id,
                active_run_id=self._run_id,
            )
            self._last_result_call_id = provider_call.provider_call_id
            self._last_source = prepared.source

    async def record_provider_observed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        input_tokens: int,
    ) -> None:
        await self._record_terminal(
            provider_call,
            ProviderObservedV1(
                provider_call_id=provider_call.provider_call_id,
                input_tokens=input_tokens,
            ),
        )

    async def record_provider_usage_unreported(
        self,
        provider_call: ProviderCallIdentity,
        /,
    ) -> None:
        await self._record_terminal(
            provider_call,
            ProviderUsageUnreportedV1(
                provider_call_id=provider_call.provider_call_id,
            ),
        )

    async def record_provider_failed(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        failure_code: str,
        retry_safety: ProviderRetrySafety,
    ) -> None:
        await self._record_terminal(
            provider_call,
            ProviderFailedV1(
                provider_call_id=provider_call.provider_call_id,
                failure_code=failure_code,
                retry_safety=retry_safety,
            ),
        )

    async def record_provider_ambiguous(
        self,
        provider_call: ProviderCallIdentity,
        /,
        *,
        reason: ProviderAmbiguityReason,
    ) -> None:
        await self._record_terminal(
            provider_call,
            ProviderAmbiguousV1(
                provider_call_id=provider_call.provider_call_id,
                reason=reason,
            ),
        )

    async def record_settled(self) -> None:
        """Publish the final Head, including a Task with no Provider request."""

        async with self._lock:
            if self._settled:
                return
            source = self._last_source
            async with self._authorized_session(
                allow_cancel_requested=True,
            ) as session:
                repository = ContextEvidenceRepository(session)
                payloads: tuple[BaseModel, ...] = ()
                if source is None:
                    # A Task may fail or be cancelled before its first model
                    # call. Keep a settled, empty read model for that execution
                    # without fabricating request.prepared. If authority already
                    # exists (notably a Lead Thread's idle Head), preserve it.
                    existing = await repository.read_head(
                        self._scope,
                        self._subject_ref,
                        lock=True,
                    )
                    if existing is not None:
                        self._settled = True
                        return
                    generation = ContextWindowGeneration(generation_id=uuid.uuid4())
                    fingerprint = hashlib.sha256(
                        json.dumps(
                            {
                                "generation": generation.generation_id,
                                "source_checkpoint_id": self._source_checkpoint_id,
                                "subject": self._subject.to_safe_mapping(),
                            },
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8"),
                    ).hexdigest()
                    empty_measurement = FinalRequestMeasurement(
                        request_fingerprint=fingerprint,
                        adapter_revision="empty-context-v1",
                        contributions=(),
                    )
                    source = ContextProjectionSource(
                        subject=self._subject,
                        phase=(ProjectionPhase.IDLE if self._subject.execution_id is None else ProjectionPhase.SETTLED),
                        generation=generation,
                        checkpoint_id=None,
                        model=self._model,
                        measurement=empty_measurement,
                        current_provider_call_id=None,
                        compaction=self._compaction(empty_measurement),
                        freshness=ProjectionFreshness.CURRENT,
                    )
                    payloads = (self._window_opened(),)
                settled = source.model_copy(
                    update={"phase": (ProjectionPhase.IDLE if self._subject.execution_id is None else ProjectionPhase.SETTLED)},
                )
                await ContextProjectionTransaction(repository).append_and_project(
                    scope=self._scope,
                    source=settled,
                    payloads=payloads,
                    origin_run_id=self._run_id,
                    active_run_id=None,
                )
                self._last_source = settled
                self._settled = True


__all__ = [
    "ContextProviderCallAmbiguous",
    "PrivateRunContextEvidenceObserver",
]
