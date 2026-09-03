"""Checkpoint receipt repair: Memory, Context compaction, and Provider response convergence."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
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
from app.private_work.errors import PrivateWorkNotFound
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
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryHistoryActivation,
)
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


def thread_id_from_config(config: RunnableConfig | None) -> str:
    if not isinstance(config, Mapping):
        raise PrivateWorkNotFound("unknown")
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        raise PrivateWorkNotFound("unknown")
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise PrivateWorkNotFound("unknown")
    return thread_id


def checkpoint_id_from_config(config: RunnableConfig | None) -> str | None:
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    checkpoint_id = configurable.get("checkpoint_id")
    return checkpoint_id if isinstance(checkpoint_id, str) and checkpoint_id else None


def memory_history_activation(
    context: PrivateWorkContext,
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

    context = require_issued_private_work_context(context)
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
    committed_checkpoint_id = checkpoint_id_from_config(item.config)
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


async def repair_memory_archive_receipt(
    session: AsyncSession,
    context: PrivateWorkContext,
    item: CheckpointTuple,
    *,
    thread_id: str,
) -> None:
    activation = memory_history_activation(
        context,
        item,
        thread_id=thread_id,
    )
    if activation is None:
        return
    await MemoryDocumentRepository(session).activate_history(activation)


def context_compaction_activation(
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
    committed_checkpoint_id = checkpoint_id_from_config(item.config)
    if committed_checkpoint_id is None:
        raise ValueError("Checkpoint Context compaction commit is invalid")
    if thread_id != thread_id_from_config(item.config):
        raise ValueError("Checkpoint Context compaction Thread is invalid")
    return committed_checkpoint_id, receipt


async def repair_context_compaction_receipt(
    session: AsyncSession,
    context: PrivateWorkContext,
    item: CheckpointTuple,
    *,
    thread_id: str,
) -> None:
    activation = context_compaction_activation(
        item,
        thread_id=thread_id,
    )
    if activation is None:
        return
    checkpoint_id, receipt = activation
    context = require_issued_private_work_context(context)
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


def context_provider_checkpoint_activation(
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
    checkpoint_id = checkpoint_id_from_config(item.config)
    if checkpoint_id is None or thread_id != thread_id_from_config(item.config):
        raise ContextProviderCallAmbiguousError(
            "Checkpoint Provider response identity is invalid",
        )
    return checkpoint_id, snapshot


def validate_context_provider_checkpoint_response(
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


async def repair_context_provider_checkpoint(
    session: AsyncSession,
    context: PrivateWorkContext,
    item: CheckpointTuple,
    *,
    thread_id: str,
    observer_run_id: str | None,
) -> str | None:
    """Converge one observed response to Checkpoint Evidence and its Head."""

    activation = context_provider_checkpoint_activation(
        item,
        thread_id=thread_id,
    )
    if activation is None:
        return None
    checkpoint_id, snapshot = activation
    context = require_issued_private_work_context(context)
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
    validate_context_provider_checkpoint_response(
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
