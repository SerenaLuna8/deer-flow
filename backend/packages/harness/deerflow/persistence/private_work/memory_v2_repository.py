"""Session-bound persistence for Memory v2 source admission."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import DeadJobRow, JobAttemptRow, JobRow
from deerflow.persistence.jobs.sql import (
    EnqueueJob,
    JobRepository,
    JobScope,
)
from deerflow.persistence.private_work.memory_v2_model import (
    MemoryCandidateRow,
    MemoryConsolidationGenerationRow,
    MemoryExtractionGenerationRow,
    MemoryFactEvidenceRow,
    MemoryFactRevisionRow,
    MemoryFactRow,
    MemorySourceBatchRow,
    MemorySourceItemRow,
    MemorySuppressionRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.system_runtime_settings import (
    RunRuntimePolicySnapshotRow,
    SystemRuntimePolicyVersionRow,
)
from deerflow.persistence.system_settings import RunModelConfigSnapshotRow

_SHA256 = re.compile(r"[0-9a-f]{64}")
_MEMORY_CANDIDATE_ID_NAMESPACE = uuid.UUID("4395227b-ff34-5f48-b14b-009662f24ad0")
_CANDIDATE_TYPES = frozenset(
    {
        "preference",
        "constraint",
        "correction",
        "context",
        "knowledge",
        "behavior",
        "goal",
    }
)
_RETENTION_CLASSES = frozenset({"permanent", "durable", "ephemeral"})
_SENSITIVITIES = frozenset({"normal", "sensitive", "restricted"})
_AUTO_RECOVERABLE_CONSOLIDATION_ERRORS = frozenset(
    {
        "ATTEMPTS_EXHAUSTED",
        "MEMORY_CONSOLIDATE_COMMIT_CONFLICT",
        "MEMORY_CONSOLIDATE_MODEL_UNAVAILABLE",
        "MEMORY_CONSOLIDATE_POLICY_UNAVAILABLE",
        "MEMORY_CONSOLIDATE_SCOPE_UNAVAILABLE",
        "MEMORY_CONSOLIDATE_TIMEOUT",
        "MEMORY_CONSOLIDATE_UNAVAILABLE",
        "MEMORY_CONSOLIDATE_WORK_UNAVAILABLE",
    }
)


def _create_fact_signature(
    candidate_type: str,
    content: str | None,
    category: str | None,
) -> tuple[str, str, str]:
    """Collapse model-normalized duplicate creates inside one generation."""

    return (
        candidate_type,
        " ".join((content or "").split()).casefold(),
        " ".join((category or "").split()).casefold(),
    )


class MemoryV2AdmissionConflict(RuntimeError):
    """An existing idempotent admission does not match its immutable input."""


class MemoryV2ExtractionConflict(RuntimeError):
    """Extraction work no longer matches its immutable generation contract."""


class MemoryV2ExtractionLeaseLost(MemoryV2ExtractionConflict):
    """The extraction Job lease cannot authorize the final transaction."""


class MemoryV2ConsolidationConflict(RuntimeError):
    """Consolidation work no longer matches its immutable generation contract."""


class MemoryV2ConsolidationLeaseLost(MemoryV2ConsolidationConflict):
    """The consolidation Job lease cannot authorize the final transaction."""


class MemoryV2RetentionConflict(RuntimeError):
    """Candidate retention work no longer has valid Job authority."""


class MemoryV2RetentionLeaseLost(MemoryV2RetentionConflict):
    """The retention Job lease cannot authorize the final transaction."""


@dataclass(frozen=True, slots=True)
class MemorySourceItemWrite:
    ordinal: int
    source_message_id: str
    content: str
    content_hmac: str


@dataclass(frozen=True, slots=True)
class MemorySourceAdmissionWrite:
    source_batch_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str
    thread_id: str
    run_id: str
    source_job_id: uuid.UUID
    source_attempt_id: uuid.UUID
    pipeline_mode: str
    policy_section: str
    policy_version_id: uuid.UUID
    policy_schema_version: int
    policy_checksum: str
    policy_revision: int
    source_identity_digest: str
    source_hmac_key_version: str
    items: tuple[MemorySourceItemWrite, ...]
    extract_job_idempotency_key: str
    contract_digest: str
    model_config_id: uuid.UUID
    model_config_version_id: uuid.UUID
    model_config_checksum: str
    prompt_version: str
    extractor_version: str
    output_schema_version: str
    extract_job_max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class MemorySourceAdmissionRecord:
    source_batch_id: uuid.UUID
    extraction_generation_id: uuid.UUID
    extract_job_id: uuid.UUID
    created: bool


@dataclass(frozen=True, slots=True)
class MemoryExtractionSourceItemRecord:
    id: uuid.UUID
    ordinal: int
    source_message_id: str
    content: str | None
    content_hmac: str


@dataclass(frozen=True, slots=True)
class MemoryExtractionModelSnapshot:
    purpose: Literal["lead", "memory"]
    model_config_id: uuid.UUID
    model_config_version_id: uuid.UUID
    model_config_checksum: str


@dataclass(frozen=True, slots=True)
class MemoryExtractionWork:
    generation_id: uuid.UUID
    source_batch_id: uuid.UUID
    job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str
    thread_id: str
    run_id: str
    pipeline_mode: str
    contract_digest: str
    policy_revision: int
    model_config_id: uuid.UUID
    model_config_version_id: uuid.UUID
    model_config_checksum: str
    prompt_version: str
    extractor_version: str
    output_schema_version: str
    source_items: tuple[MemoryExtractionSourceItemRecord, ...]
    model_snapshots: tuple[MemoryExtractionModelSnapshot, ...]
    suppressed: bool
    cancel_requested: bool
    candidate_committed: bool

    def has_exact_model_snapshot(self, purpose: Literal["lead", "memory"]) -> bool:
        return any(
            snapshot.purpose == purpose and snapshot.model_config_id == self.model_config_id and snapshot.model_config_version_id == self.model_config_version_id and snapshot.model_config_checksum == self.model_config_checksum
            for snapshot in self.model_snapshots
        )


@dataclass(frozen=True, slots=True)
class MemoryCandidateDraft:
    source_ordinal: int
    candidate_type: str
    content: str
    confidence: float
    retention_class: str
    sensitivity: str


@dataclass(frozen=True, slots=True)
class MemoryCandidateWrite:
    id: uuid.UUID
    ordinal: int
    source_ordinal: int
    candidate_type: str
    content: str
    content_digest: str
    confidence: float
    retention_class: str
    sensitivity: str


@dataclass(frozen=True, slots=True)
class MemoryCandidateCommitRecord:
    generation_id: uuid.UUID
    candidate_ids: tuple[uuid.UUID, ...]
    status: Literal["succeeded", "cancelled", "replayed"]


@dataclass(frozen=True, slots=True)
class MemoryConsolidationAdmissionContract:
    interval_minutes: int
    policy_revision: int
    model_config_id: uuid.UUID
    model_config_version_id: uuid.UUID
    model_config_checksum: str
    prompt_version: str
    consolidator_version: str
    output_schema_version: str
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class MemoryConsolidationAdmissionRecord:
    generation_id: uuid.UUID
    job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str
    candidate_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class MemoryRetentionAdmissionRecord:
    job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str


@dataclass(frozen=True, slots=True)
class MemoryConsolidationCandidateRecord:
    id: uuid.UUID
    candidate_type: str
    content: str
    content_digest: str
    confidence: float
    retention_class: str
    sensitivity: str
    source_item_id: uuid.UUID
    source_identity_hmac: str
    thread_id: str
    run_id: str
    run_event_sequence: int | None


@dataclass(frozen=True, slots=True)
class MemoryConsolidationFactRecord:
    id: uuid.UUID
    revision_id: uuid.UUID
    revision_number: int
    fact_kind: str
    version: int
    content: str
    content_digest: str
    category: str
    confidence: float
    last_confirmed_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryConsolidationWork:
    generation_id: uuid.UUID
    job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str
    candidate_input_digest: str
    contract_digest: str
    policy_revision: int
    model_config_id: uuid.UUID
    model_config_version_id: uuid.UUID
    model_config_checksum: str
    prompt_version: str
    consolidator_version: str
    output_schema_version: str
    candidates: tuple[MemoryConsolidationCandidateRecord, ...]
    facts: tuple[MemoryConsolidationFactRecord, ...]
    active_fact_count: int
    suppressed: bool
    cancel_requested: bool
    fact_committed: bool


@dataclass(frozen=True, slots=True)
class MemoryConsolidationDecisionWrite:
    candidate_id: uuid.UUID
    action: Literal["create", "confirm", "revise", "pending", "reject"]
    target_fact_id: uuid.UUID | None
    expected_revision_id: uuid.UUID | None
    content: str | None
    category: str | None
    confidence: float | None
    change_reason: Literal["new_fact", "supplement", "correction"] | None
    decision_reason: (
        Literal[
            "same_fact",
            "insufficient_evidence",
            "possible_conflict",
            "unsupported_governance_change",
            "sensitive_content",
        ]
        | None
    )


@dataclass(frozen=True, slots=True)
class MemoryFactCommitRecord:
    generation_id: uuid.UUID
    candidate_ids: tuple[uuid.UUID, ...]
    fact_ids: tuple[uuid.UUID, ...]
    revision_ids: tuple[uuid.UUID, ...]
    status: Literal["succeeded", "cancelled", "replayed"]


@dataclass(frozen=True, slots=True)
class MemoryRetentionCommitRecord:
    job_id: uuid.UUID
    erased_count: int
    status: Literal["succeeded", "cancelled"]


def prepare_memory_candidate_writes(
    generation_id: uuid.UUID,
    drafts: Iterable[MemoryCandidateDraft],
) -> tuple[MemoryCandidateWrite, ...]:
    """Normalize and deterministically identify one complete model output."""

    if not isinstance(generation_id, uuid.UUID):
        raise ValueError("Memory Candidate generation is invalid")
    values = tuple(drafts)
    if len(values) > 64 or any(type(value) is not MemoryCandidateDraft for value in values):
        raise ValueError("Memory Candidate batch is invalid")
    by_digest: dict[str, MemoryCandidateDraft] = {}
    for value in values:
        content = value.content.replace("\r\n", "\n").replace("\r", "\n").strip() if isinstance(value.content, str) else ""
        normalized = MemoryCandidateDraft(
            source_ordinal=value.source_ordinal,
            candidate_type=value.candidate_type,
            content=content,
            confidence=value.confidence,
            retention_class=value.retention_class,
            sensitivity=value.sensitivity,
        )
        if (
            type(normalized.source_ordinal) is not int
            or normalized.source_ordinal < 0
            or normalized.candidate_type not in _CANDIDATE_TYPES
            or not normalized.content
            or len(normalized.content) > 16_000
            or isinstance(normalized.confidence, bool)
            or not isinstance(normalized.confidence, int | float)
            or not math.isfinite(float(normalized.confidence))
            or not 0 <= float(normalized.confidence) <= 1
            or normalized.retention_class not in _RETENTION_CLASSES
            or normalized.sensitivity not in _SENSITIVITIES
        ):
            raise ValueError("Memory Candidate is invalid")
        digest = hashlib.sha256(normalized.content.encode("utf-8")).hexdigest()
        existing = by_digest.get(digest)
        if existing is not None and existing != normalized:
            raise ValueError("Memory Candidate digest conflict")
        by_digest[digest] = normalized

    writes: list[MemoryCandidateWrite] = []
    for ordinal, (digest, value) in enumerate(
        sorted(
            by_digest.items(),
            key=lambda item: (item[1].source_ordinal, item[0]),
        )
    ):
        writes.append(
            MemoryCandidateWrite(
                id=uuid.uuid5(
                    _MEMORY_CANDIDATE_ID_NAMESPACE,
                    f"{generation_id}\x00{digest}",
                ),
                ordinal=ordinal,
                source_ordinal=value.source_ordinal,
                candidate_type=value.candidate_type,
                content=value.content,
                content_digest=digest,
                confidence=float(value.confidence),
                retention_class=value.retention_class,
                sensitivity=value.sensitivity,
            )
        )
    return tuple(writes)


def _canonical_digest(value: object) -> str:
    import json

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _candidate_input_digest(
    rows: Iterable[MemoryCandidateRow],
) -> str:
    return _canonical_digest(
        [
            {
                "candidate_id": str(row.id),
                "content_digest": row.content_digest,
            }
            for row in rows
        ]
    )


def _consolidation_contract_digest(
    contract: MemoryConsolidationAdmissionContract,
) -> str:
    return _canonical_digest(
        {
            "consolidator_version": contract.consolidator_version,
            "model_config_checksum": contract.model_config_checksum,
            "model_config_id": str(contract.model_config_id),
            "model_config_version_id": str(contract.model_config_version_id),
            "output_schema_version": contract.output_schema_version,
            "policy_revision": contract.policy_revision,
            "prompt_version": contract.prompt_version,
        }
    )


class MemoryV2Repository:
    """Persist one complete source Batch and its first Extraction Generation."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        jobs: JobRepository | None = None,
    ) -> None:
        self.session = session
        self.jobs = jobs or JobRepository(session)

    async def source_suppressed(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        hmac_key_version: str,
        identity_hmacs: tuple[str, ...],
    ) -> bool:
        """Return whether hard-forget blocks any exact prepared source item."""

        if (
            not isinstance(project_id, uuid.UUID)
            or not isinstance(owner_user_id, str)
            or not owner_user_id
            or not isinstance(namespace, str)
            or not namespace
            or not isinstance(hmac_key_version, str)
            or not hmac_key_version
            or not identity_hmacs
            or any(_SHA256.fullmatch(value) is None for value in identity_hmacs)
        ):
            raise ValueError("Memory source suppression lookup is invalid")
        return (
            await self.session.scalar(
                select(
                    exists(
                        select(MemorySuppressionRow.id).where(
                            MemorySuppressionRow.project_id == project_id,
                            MemorySuppressionRow.owner_user_id == owner_user_id,
                            MemorySuppressionRow.namespace == namespace,
                            MemorySuppressionRow.suppression_kind == "source",
                            MemorySuppressionRow.hmac_key_version == hmac_key_version,
                            MemorySuppressionRow.identity_hmac.in_(identity_hmacs),
                        )
                    )
                )
            )
            is True
        )

    @staticmethod
    def _validate(request: MemorySourceAdmissionWrite) -> None:
        if type(request) is not MemorySourceAdmissionWrite:
            raise TypeError("MemorySourceAdmissionWrite is required")
        if not request.items or any(
            type(item) is not MemorySourceItemWrite
            or item.ordinal != index
            or not item.source_message_id
            or len(item.source_message_id) > 128
            or not item.content
            or len(item.content) > 64_000
            or _SHA256.fullmatch(item.content_hmac) is None
            for index, item in enumerate(request.items)
        ):
            raise ValueError("Memory source items are invalid")
        if len({item.source_message_id for item in request.items}) != len(request.items):
            raise ValueError("Memory source message identities must be unique")
        if (
            type(request.source_batch_id) is not uuid.UUID
            or request.pipeline_mode not in {"shadow", "consolidate", "v2"}
            or request.policy_section != "agent_runtime"
            or request.policy_schema_version < 1
            or request.policy_revision < 1
            or not request.source_hmac_key_version
            or len(request.source_hmac_key_version) > 64
            or request.extract_job_max_attempts < 1
            or request.extract_job_max_attempts > 20
            or any(
                _SHA256.fullmatch(value) is None
                for value in (
                    request.policy_checksum,
                    request.source_identity_digest,
                    request.extract_job_idempotency_key,
                    request.contract_digest,
                    request.model_config_checksum,
                )
            )
        ):
            raise ValueError("Memory source admission contract is invalid")

    @staticmethod
    def _batch_matches(
        row: MemorySourceBatchRow,
        request: MemorySourceAdmissionWrite,
    ) -> bool:
        return (
            row.id == request.source_batch_id
            and row.project_id == request.project_id
            and row.owner_user_id == request.owner_user_id
            and row.namespace == request.namespace
            and row.thread_id == request.thread_id
            and row.run_id == request.run_id
            and row.source_job_id == request.source_job_id
            and row.source_attempt_id == request.source_attempt_id
            and row.pipeline_mode == request.pipeline_mode
            and row.policy_section == request.policy_section
            and row.policy_version_id == request.policy_version_id
            and row.policy_schema_version == request.policy_schema_version
            and row.policy_checksum == request.policy_checksum
            and row.source_identity_digest == request.source_identity_digest
            and row.source_hmac_key_version == request.source_hmac_key_version
            and row.source_item_count == len(request.items)
        )

    async def _require_items_match(
        self,
        batch_id: uuid.UUID,
        request: MemorySourceAdmissionWrite,
    ) -> None:
        rows = tuple(
            (
                await self.session.execute(
                    select(MemorySourceItemRow)
                    .where(
                        MemorySourceItemRow.project_id == request.project_id,
                        MemorySourceItemRow.owner_user_id == request.owner_user_id,
                        MemorySourceItemRow.namespace == request.namespace,
                        MemorySourceItemRow.source_batch_id == batch_id,
                    )
                    .order_by(MemorySourceItemRow.ordinal)
                    .with_for_update(read=True, of=MemorySourceItemRow)
                )
            ).scalars()
        )
        if len(rows) != len(request.items) or any(
            row.ordinal != item.ordinal
            or row.source_message_id != item.source_message_id
            or row.role != "user"
            or row.content_hmac != item.content_hmac
            or row.run_event_sequence is not None
            or ((row.source_erased_at is None and row.content != item.content) or (row.source_erased_at is not None and row.content is not None))
            for row, item in zip(rows, request.items, strict=True)
        ):
            raise MemoryV2AdmissionConflict(
                "Memory source item replay does not match",
            )

    @staticmethod
    def _generation_matches(
        row: MemoryExtractionGenerationRow,
        request: MemorySourceAdmissionWrite,
        *,
        batch_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> bool:
        return (
            row.project_id == request.project_id
            and row.owner_user_id == request.owner_user_id
            and row.namespace == request.namespace
            and row.source_batch_id == batch_id
            and row.job_id == job_id
            and row.contract_digest == request.contract_digest
            and row.policy_revision == request.policy_revision
            and row.model_config_id == request.model_config_id
            and row.model_config_version_id == request.model_config_version_id
            and row.model_config_checksum == request.model_config_checksum
            and row.prompt_version == request.prompt_version
            and row.extractor_version == request.extractor_version
            and row.output_schema_version == request.output_schema_version
        )

    async def admit_source(
        self,
        request: MemorySourceAdmissionWrite,
    ) -> MemorySourceAdmissionRecord:
        self._validate(request)
        batch_id = request.source_batch_id
        inserted_batch_id = await self.session.scalar(
            insert(MemorySourceBatchRow)
            .values(
                id=batch_id,
                project_id=request.project_id,
                owner_user_id=request.owner_user_id,
                namespace=request.namespace,
                thread_id=request.thread_id,
                run_id=request.run_id,
                source_job_id=request.source_job_id,
                source_attempt_id=request.source_attempt_id,
                pipeline_mode=request.pipeline_mode,
                policy_section=request.policy_section,
                policy_version_id=request.policy_version_id,
                policy_schema_version=request.policy_schema_version,
                policy_checksum=request.policy_checksum,
                source_identity_digest=request.source_identity_digest,
                source_hmac_key_version=request.source_hmac_key_version,
                source_item_count=len(request.items),
            )
            .on_conflict_do_nothing(
                constraint="uq_memory_source_batches_identity",
            )
            .returning(MemorySourceBatchRow.id)
        )
        created = inserted_batch_id is not None
        if inserted_batch_id is not None:
            batch_id = inserted_batch_id
            self.session.add_all(
                [
                    MemorySourceItemRow(
                        project_id=request.project_id,
                        owner_user_id=request.owner_user_id,
                        namespace=request.namespace,
                        source_batch_id=batch_id,
                        ordinal=item.ordinal,
                        source_message_id=item.source_message_id,
                        run_event_sequence=None,
                        role="user",
                        content=item.content,
                        content_hmac=item.content_hmac,
                    )
                    for item in request.items
                ]
            )
            await self.session.flush()
        else:
            batch = (
                await self.session.execute(
                    select(MemorySourceBatchRow)
                    .where(
                        MemorySourceBatchRow.project_id == request.project_id,
                        MemorySourceBatchRow.owner_user_id == request.owner_user_id,
                        MemorySourceBatchRow.namespace == request.namespace,
                        MemorySourceBatchRow.run_id == request.run_id,
                        MemorySourceBatchRow.source_attempt_id == request.source_attempt_id,
                        MemorySourceBatchRow.source_identity_digest == request.source_identity_digest,
                    )
                    .with_for_update(of=MemorySourceBatchRow)
                )
            ).scalar_one()
            if not self._batch_matches(batch, request):
                raise MemoryV2AdmissionConflict(
                    "Memory source batch replay does not match",
                )
            batch_id = batch.id
            await self._require_items_match(batch_id, request)

        extract_job_id = await self.jobs.enqueue(
            EnqueueJob(
                job_type="memory_extract",
                scope=JobScope(
                    request.project_id,
                    request.owner_user_id,
                ),
                idempotency_key=request.extract_job_idempotency_key,
                run_id=None,
                occurrence_id=None,
                max_attempts=request.extract_job_max_attempts,
                namespace=request.namespace,
                retry_safety="safe",
            )
        )
        generation_id = uuid.uuid4()
        inserted_generation_id = await self.session.scalar(
            insert(MemoryExtractionGenerationRow)
            .values(
                id=generation_id,
                project_id=request.project_id,
                owner_user_id=request.owner_user_id,
                namespace=request.namespace,
                source_batch_id=batch_id,
                job_id=extract_job_id,
                contract_digest=request.contract_digest,
                policy_revision=request.policy_revision,
                model_config_id=request.model_config_id,
                model_config_version_id=request.model_config_version_id,
                model_config_checksum=request.model_config_checksum,
                prompt_version=request.prompt_version,
                extractor_version=request.extractor_version,
                output_schema_version=request.output_schema_version,
            )
            .on_conflict_do_nothing(
                constraint="uq_memory_extraction_generations_contract",
            )
            .returning(MemoryExtractionGenerationRow.id)
        )
        if inserted_generation_id is None:
            generation = (
                await self.session.execute(
                    select(MemoryExtractionGenerationRow)
                    .where(
                        MemoryExtractionGenerationRow.project_id == request.project_id,
                        MemoryExtractionGenerationRow.owner_user_id == request.owner_user_id,
                        MemoryExtractionGenerationRow.namespace == request.namespace,
                        MemoryExtractionGenerationRow.source_batch_id == batch_id,
                        MemoryExtractionGenerationRow.contract_digest == request.contract_digest,
                    )
                    .with_for_update(of=MemoryExtractionGenerationRow)
                )
            ).scalar_one()
            if not self._generation_matches(
                generation,
                request,
                batch_id=batch_id,
                job_id=extract_job_id,
            ):
                raise MemoryV2AdmissionConflict(
                    "Memory extraction generation replay does not match",
                )
            generation_id = generation.id
        else:
            generation_id = inserted_generation_id
        await self.session.flush()
        return MemorySourceAdmissionRecord(
            source_batch_id=batch_id,
            extraction_generation_id=generation_id,
            extract_job_id=extract_job_id,
            created=created,
        )

    async def load_extraction_work(
        self,
        *,
        job_id: uuid.UUID,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        for_update: bool = False,
    ) -> MemoryExtractionWork | None:
        """Load only the immutable source closure owned by one extract Job."""

        if not isinstance(job_id, uuid.UUID) or not isinstance(project_id, uuid.UUID) or not isinstance(owner_user_id, str) or not isinstance(namespace, str) or not namespace or type(for_update) is not bool:
            raise ValueError("Memory extraction coordinates are invalid")

        generation_statement = select(MemoryExtractionGenerationRow).where(
            MemoryExtractionGenerationRow.job_id == job_id,
            MemoryExtractionGenerationRow.project_id == project_id,
            MemoryExtractionGenerationRow.owner_user_id == owner_user_id,
            MemoryExtractionGenerationRow.namespace == namespace,
        )
        if for_update:
            generation_statement = generation_statement.with_for_update(
                of=MemoryExtractionGenerationRow,
            )
        generation = (await self.session.execute(generation_statement)).scalar_one_or_none()
        if generation is None:
            return None

        job_statement = select(JobRow).where(
            JobRow.id == job_id,
            JobRow.job_type == "memory_extract",
            JobRow.project_id == project_id,
            JobRow.owner_user_id == owner_user_id,
            JobRow.namespace == namespace,
            JobRow.run_id.is_(None),
            JobRow.automation_occurrence_id.is_(None),
            JobRow.status.in_(("leased", "running")),
        )
        if for_update:
            job_statement = job_statement.with_for_update(of=JobRow)
        job = (await self.session.execute(job_statement)).scalar_one_or_none()
        if job is None:
            raise MemoryV2ExtractionConflict("Memory extraction Job authority is unavailable")

        batch_statement = select(MemorySourceBatchRow).where(
            MemorySourceBatchRow.id == generation.source_batch_id,
            MemorySourceBatchRow.project_id == project_id,
            MemorySourceBatchRow.owner_user_id == owner_user_id,
            MemorySourceBatchRow.namespace == namespace,
        )
        if for_update:
            batch_statement = batch_statement.with_for_update(
                of=MemorySourceBatchRow,
            )
        batch = (await self.session.execute(batch_statement)).scalar_one_or_none()
        if batch is None:
            raise MemoryV2ExtractionConflict("Memory source Batch is unavailable")

        source_run = await self.session.scalar(
            select(RunRow.run_id).where(
                RunRow.project_id == project_id,
                RunRow.owner_user_id == owner_user_id,
                RunRow.thread_id == batch.thread_id,
                RunRow.run_id == batch.run_id,
                RunRow.job_id == batch.source_job_id,
                RunRow.status == "success",
            )
        )
        source_attempt = await self.session.scalar(
            select(JobAttemptRow.id).where(
                JobAttemptRow.id == batch.source_attempt_id,
                JobAttemptRow.job_id == batch.source_job_id,
                JobAttemptRow.outcome == "succeeded",
            )
        )
        if source_run is None or source_attempt is None:
            raise MemoryV2ExtractionConflict("Successful Memory source authority is unavailable")

        policy_material = (
            await self.session.execute(
                select(
                    RunRuntimePolicySnapshotRow,
                    SystemRuntimePolicyVersionRow,
                )
                .join(
                    SystemRuntimePolicyVersionRow,
                    (SystemRuntimePolicyVersionRow.id == RunRuntimePolicySnapshotRow.policy_version_id)
                    & (SystemRuntimePolicyVersionRow.section == RunRuntimePolicySnapshotRow.section)
                    & (SystemRuntimePolicyVersionRow.schema_version == RunRuntimePolicySnapshotRow.schema_version)
                    & (SystemRuntimePolicyVersionRow.payload_checksum == RunRuntimePolicySnapshotRow.payload_checksum),
                )
                .where(
                    RunRuntimePolicySnapshotRow.project_id == project_id,
                    RunRuntimePolicySnapshotRow.owner_user_id == owner_user_id,
                    RunRuntimePolicySnapshotRow.run_id == batch.run_id,
                    RunRuntimePolicySnapshotRow.section == batch.policy_section,
                    RunRuntimePolicySnapshotRow.policy_version_id == batch.policy_version_id,
                    RunRuntimePolicySnapshotRow.schema_version == batch.policy_schema_version,
                    RunRuntimePolicySnapshotRow.payload_checksum == batch.policy_checksum,
                )
            )
        ).one_or_none()
        if policy_material is None or int(policy_material[1].version_number) != int(generation.policy_revision):
            raise MemoryV2ExtractionConflict("Memory extraction policy snapshot is invalid")
        policy_value = policy_material[1].value
        memory_value = policy_value.get("memory") if isinstance(policy_value, Mapping) else None
        if not isinstance(memory_value, Mapping) or memory_value.get("enabled") is not True or memory_value.get("pipeline_mode") != batch.pipeline_mode or batch.pipeline_mode == "off":
            raise MemoryV2ExtractionConflict("Memory extraction Pipeline snapshot is invalid")

        item_statement = (
            select(MemorySourceItemRow)
            .where(
                MemorySourceItemRow.project_id == project_id,
                MemorySourceItemRow.owner_user_id == owner_user_id,
                MemorySourceItemRow.namespace == namespace,
                MemorySourceItemRow.source_batch_id == batch.id,
            )
            .order_by(MemorySourceItemRow.ordinal)
        )
        if for_update:
            item_statement = item_statement.with_for_update(
                of=MemorySourceItemRow,
            )
        item_rows = tuple((await self.session.execute(item_statement)).scalars())
        if len(item_rows) != batch.source_item_count or not item_rows or any(row.ordinal != index for index, row in enumerate(item_rows)):
            raise MemoryV2ExtractionConflict("Memory source Items are incomplete")

        snapshot_rows = tuple(
            (
                await self.session.execute(
                    select(RunModelConfigSnapshotRow)
                    .where(
                        RunModelConfigSnapshotRow.project_id == project_id,
                        RunModelConfigSnapshotRow.owner_user_id == owner_user_id,
                        RunModelConfigSnapshotRow.run_id == batch.run_id,
                        RunModelConfigSnapshotRow.purpose.in_(("lead", "memory")),
                        RunModelConfigSnapshotRow.model_config_id == generation.model_config_id,
                        RunModelConfigSnapshotRow.model_config_version_id == generation.model_config_version_id,
                        RunModelConfigSnapshotRow.payload_checksum == generation.model_config_checksum,
                    )
                    .order_by(RunModelConfigSnapshotRow.purpose)
                )
            ).scalars()
        )
        if not snapshot_rows:
            raise MemoryV2ExtractionConflict("Memory extraction model snapshot is invalid")

        source_items = tuple(
            MemoryExtractionSourceItemRecord(
                id=row.id,
                ordinal=row.ordinal,
                source_message_id=row.source_message_id,
                content=row.content,
                content_hmac=row.content_hmac,
            )
            for row in item_rows
        )
        return MemoryExtractionWork(
            generation_id=generation.id,
            source_batch_id=batch.id,
            job_id=job_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            thread_id=batch.thread_id,
            run_id=batch.run_id,
            pipeline_mode=batch.pipeline_mode,
            contract_digest=generation.contract_digest,
            policy_revision=int(generation.policy_revision),
            model_config_id=generation.model_config_id,
            model_config_version_id=generation.model_config_version_id,
            model_config_checksum=generation.model_config_checksum,
            prompt_version=generation.prompt_version,
            extractor_version=generation.extractor_version,
            output_schema_version=generation.output_schema_version,
            source_items=source_items,
            model_snapshots=tuple(
                MemoryExtractionModelSnapshot(
                    purpose=row.purpose,
                    model_config_id=row.model_config_id,
                    model_config_version_id=row.model_config_version_id,
                    model_config_checksum=row.payload_checksum,
                )
                for row in snapshot_rows
            ),
            suppressed=(batch.suppressed_at is not None or any(row.source_erased_at is not None or row.content is None for row in item_rows)),
            cancel_requested=job.cancel_requested_at is not None,
            candidate_committed=(generation.candidate_committed_at is not None),
        )

    async def finalize_extraction(
        self,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        generation_id: uuid.UUID,
        source_batch_id: uuid.UUID,
        contract_digest: str,
        pipeline_mode: str,
        candidates: tuple[MemoryCandidateWrite, ...],
        cancel: bool,
    ) -> MemoryCandidateCommitRecord:
        """Atomically commit one Candidate set and acknowledge its Job."""

        if (
            not isinstance(lease_token, str)
            or not lease_token
            or not isinstance(generation_id, uuid.UUID)
            or not isinstance(source_batch_id, uuid.UUID)
            or _SHA256.fullmatch(contract_digest) is None
            or pipeline_mode not in {"shadow", "consolidate", "v2"}
            or not isinstance(candidates, tuple)
            or type(cancel) is not bool
        ):
            raise ValueError("Memory extraction settlement is invalid")

        try:
            work = await self.load_extraction_work(
                job_id=job_id,
                project_id=project_id,
                owner_user_id=owner_user_id,
                namespace=namespace,
                for_update=True,
            )
        except MemoryV2ExtractionConflict:
            settled_at = datetime.now(UTC)
            changed = await self.jobs.settle_success(
                job_id,
                lease_token=lease_token,
                now=settled_at,
            )
            if not changed:
                raise MemoryV2ExtractionLeaseLost from None
            raise
        if work is None or work.generation_id != generation_id or work.source_batch_id != source_batch_id or work.contract_digest != contract_digest or work.pipeline_mode != pipeline_mode:
            raise MemoryV2ExtractionConflict("Memory extraction settlement does not match")

        if cancel or work.suppressed or work.cancel_requested:
            settled_at = datetime.now(UTC)
            changed = await self.jobs.settle_cancelled(
                job_id,
                lease_token=lease_token,
                now=settled_at,
            )
            if not changed:
                raise MemoryV2ExtractionLeaseLost
            return MemoryCandidateCommitRecord(
                generation_id=generation_id,
                candidate_ids=(),
                status="cancelled",
            )

        if work.candidate_committed:
            existing_ids = tuple(
                (
                    await self.session.execute(
                        select(MemoryCandidateRow.id)
                        .where(
                            MemoryCandidateRow.project_id == project_id,
                            MemoryCandidateRow.owner_user_id == owner_user_id,
                            MemoryCandidateRow.namespace == namespace,
                            MemoryCandidateRow.extraction_generation_id == generation_id,
                        )
                        .order_by(MemoryCandidateRow.ordinal)
                    )
                ).scalars()
            )
            settled_at = datetime.now(UTC)
            changed = await self.jobs.settle_success(
                job_id,
                lease_token=lease_token,
                now=settled_at,
            )
            if not changed:
                raise MemoryV2ExtractionLeaseLost
            return MemoryCandidateCommitRecord(
                generation_id=generation_id,
                candidate_ids=existing_ids,
                status="replayed",
            )

        if len(candidates) > 64 or any(type(candidate) is not MemoryCandidateWrite or candidate.ordinal != ordinal for ordinal, candidate in enumerate(candidates)):
            raise MemoryV2ExtractionConflict("Memory Candidate batch is invalid")
        source_by_ordinal = {item.ordinal: item for item in work.source_items}
        for candidate in candidates:
            source = source_by_ordinal.get(candidate.source_ordinal)
            expected_digest = hashlib.sha256(candidate.content.encode("utf-8")).hexdigest()
            expected_id = uuid.uuid5(
                _MEMORY_CANDIDATE_ID_NAMESPACE,
                f"{generation_id}\x00{expected_digest}",
            )
            if (
                source is None
                or source.content is None
                or candidate.id != expected_id
                or candidate.content_digest != expected_digest
                or candidate.candidate_type not in _CANDIDATE_TYPES
                or candidate.retention_class not in _RETENTION_CLASSES
                or candidate.sensitivity not in _SENSITIVITIES
                or not candidate.content
                or len(candidate.content) > 16_000
                or not 0 <= candidate.confidence <= 1
            ):
                raise MemoryV2ExtractionConflict("Memory Candidate does not match its source")

        existing_candidate = await self.session.scalar(
            select(MemoryCandidateRow.id)
            .where(
                MemoryCandidateRow.project_id == project_id,
                MemoryCandidateRow.owner_user_id == owner_user_id,
                MemoryCandidateRow.namespace == namespace,
                MemoryCandidateRow.extraction_generation_id == generation_id,
            )
            .with_for_update(of=MemoryCandidateRow)
            .limit(1)
        )
        if existing_candidate is not None:
            raise MemoryV2ExtractionConflict("Uncommitted Memory Candidates already exist")

        settled_at = datetime.now(UTC)
        changed = await self.jobs.settle_success(
            job_id,
            lease_token=lease_token,
            now=settled_at,
        )
        if not changed:
            raise MemoryV2ExtractionLeaseLost

        self.session.add_all(
            [
                MemoryCandidateRow(
                    id=candidate.id,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    namespace=namespace,
                    source_batch_id=source_batch_id,
                    extraction_generation_id=generation_id,
                    source_item_id=source_by_ordinal[candidate.source_ordinal].id,
                    consolidation_generation_id=None,
                    ordinal=candidate.ordinal,
                    candidate_type=candidate.candidate_type,
                    content=candidate.content,
                    content_digest=candidate.content_digest,
                    confidence=candidate.confidence,
                    retention_class=candidate.retention_class,
                    sensitivity=candidate.sensitivity,
                    status="pending",
                    decision_reason=None,
                    decided_at=None,
                    content_erased_at=None,
                    created_at=settled_at,
                    updated_at=settled_at,
                )
                for candidate in candidates
            ]
        )
        generation = await self.session.get(
            MemoryExtractionGenerationRow,
            generation_id,
        )
        if generation is None or generation.candidate_committed_at is not None:
            raise MemoryV2ExtractionConflict("Memory extraction Generation changed during settlement")
        generation.candidate_committed_at = settled_at
        await self.session.flush()
        return MemoryCandidateCommitRecord(
            generation_id=generation_id,
            candidate_ids=tuple(candidate.id for candidate in candidates),
            status="succeeded",
        )

    @staticmethod
    def _validate_consolidation_contract(
        contract: MemoryConsolidationAdmissionContract,
    ) -> None:
        if (
            type(contract) is not MemoryConsolidationAdmissionContract
            or type(contract.interval_minutes) is not int
            or not 15 <= contract.interval_minutes <= 1_440
            or type(contract.policy_revision) is not int
            or contract.policy_revision < 1
            or not isinstance(contract.model_config_id, uuid.UUID)
            or not isinstance(contract.model_config_version_id, uuid.UUID)
            or _SHA256.fullmatch(contract.model_config_checksum) is None
            or not contract.prompt_version
            or len(contract.prompt_version) > 64
            or not contract.consolidator_version
            or len(contract.consolidator_version) > 64
            or not contract.output_schema_version
            or len(contract.output_schema_version) > 64
            or type(contract.max_attempts) is not int
            or not 1 <= contract.max_attempts <= 20
        ):
            raise ValueError("Memory consolidation admission contract is invalid")

    async def _lock_live_scope(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        allow_viewer: bool = False,
    ) -> bool:
        project = (
            await self.session.execute(
                select(ProjectRow)
                .where(
                    ProjectRow.id == project_id,
                    ProjectRow.status == "active",
                    ProjectRow.is_suspended.is_(False),
                )
                .with_for_update(of=ProjectRow)
            )
        ).scalar_one_or_none()
        if project is None:
            return False
        allowed_roles = (
            "admin",
            "editor",
            "runner",
            "channel_guest",
            *(("viewer",) if allow_viewer else ()),
        )
        membership = (
            await self.session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.user_id == owner_user_id,
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.role.in_(allowed_roles),
                )
                .with_for_update(of=ProjectMembershipRow)
            )
        ).scalar_one_or_none()
        return membership is not None

    @staticmethod
    def _active_memory_job_exists(
        *,
        job_type: Literal["memory_consolidate", "memory_retention_purge"],
        project_id,
        owner_user_id,
        namespace,
    ):
        return exists(
            select(JobRow.id).where(
                JobRow.job_type == job_type,
                JobRow.project_id == project_id,
                JobRow.owner_user_id == owner_user_id,
                JobRow.namespace == namespace,
                JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
            )
        )

    async def _recover_dead_consolidation(
        self,
    ) -> MemoryConsolidationAdmissionRecord | None:
        """Attach one dead generation to a safe successor without changing its contract."""

        pending_candidate = exists(
            select(MemoryCandidateRow.id).where(
                MemoryCandidateRow.project_id == MemoryConsolidationGenerationRow.project_id,
                MemoryCandidateRow.owner_user_id == MemoryConsolidationGenerationRow.owner_user_id,
                MemoryCandidateRow.namespace == MemoryConsolidationGenerationRow.namespace,
                MemoryCandidateRow.consolidation_generation_id == MemoryConsolidationGenerationRow.id,
                MemoryCandidateRow.status == "pending",
                MemoryCandidateRow.content.is_not(None),
                MemoryCandidateRow.content_erased_at.is_(None),
            )
        )
        coordinates = (
            await self.session.execute(
                select(
                    MemoryConsolidationGenerationRow.id,
                    MemoryConsolidationGenerationRow.project_id,
                    MemoryConsolidationGenerationRow.owner_user_id,
                    MemoryConsolidationGenerationRow.namespace,
                    JobRow.id.label("job_id"),
                )
                .join(
                    JobRow,
                    JobRow.id == MemoryConsolidationGenerationRow.job_id,
                )
                .join(DeadJobRow, DeadJobRow.job_id == JobRow.id)
                .join(
                    ProjectRow,
                    ProjectRow.id == MemoryConsolidationGenerationRow.project_id,
                )
                .join(
                    ProjectMembershipRow,
                    and_(
                        ProjectMembershipRow.project_id == MemoryConsolidationGenerationRow.project_id,
                        ProjectMembershipRow.user_id == MemoryConsolidationGenerationRow.owner_user_id,
                    ),
                )
                .where(
                    MemoryConsolidationGenerationRow.fact_committed_at.is_(None),
                    JobRow.job_type == "memory_consolidate",
                    JobRow.status == "dead",
                    JobRow.predecessor_dead_job_id.is_(None),
                    DeadJobRow.retry_safety == "safe",
                    DeadJobRow.public_error_code.in_(
                        _AUTO_RECOVERABLE_CONSOLIDATION_ERRORS,
                    ),
                    ProjectRow.status == "active",
                    ProjectRow.is_suspended.is_(False),
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.role.in_(
                        ("admin", "editor", "runner", "channel_guest"),
                    ),
                    pending_candidate,
                )
                .order_by(
                    MemoryConsolidationGenerationRow.created_at,
                    MemoryConsolidationGenerationRow.id,
                )
                .limit(1)
            )
        ).one_or_none()
        if coordinates is None:
            return None
        if not await self._lock_live_scope(
            project_id=coordinates.project_id,
            owner_user_id=coordinates.owner_user_id,
        ):
            return None
        active_job = await self.session.scalar(
            select(JobRow.id)
            .where(
                JobRow.job_type == "memory_consolidate",
                JobRow.project_id == coordinates.project_id,
                JobRow.owner_user_id == coordinates.owner_user_id,
                JobRow.namespace == coordinates.namespace,
                JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
            )
            .with_for_update(of=JobRow)
            .limit(1)
        )
        if active_job is not None:
            return None
        pair = (
            await self.session.execute(
                select(
                    MemoryConsolidationGenerationRow,
                    JobRow,
                    DeadJobRow,
                )
                .join(
                    JobRow,
                    JobRow.id == MemoryConsolidationGenerationRow.job_id,
                )
                .join(DeadJobRow, DeadJobRow.job_id == JobRow.id)
                .where(
                    MemoryConsolidationGenerationRow.id == coordinates.id,
                    MemoryConsolidationGenerationRow.fact_committed_at.is_(None),
                    JobRow.id == coordinates.job_id,
                    JobRow.job_type == "memory_consolidate",
                    JobRow.status == "dead",
                    JobRow.predecessor_dead_job_id.is_(None),
                    JobRow.retry_safety == "safe",
                    DeadJobRow.retry_safety == "safe",
                    DeadJobRow.public_error_code.in_(
                        _AUTO_RECOVERABLE_CONSOLIDATION_ERRORS,
                    ),
                )
                .with_for_update(
                    of=(MemoryConsolidationGenerationRow, JobRow),
                )
            )
        ).one_or_none()
        if pair is None:
            return None
        generation, predecessor, _dead = pair
        candidate_rows = tuple(
            (
                await self.session.execute(
                    select(MemoryCandidateRow)
                    .where(
                        MemoryCandidateRow.project_id == generation.project_id,
                        MemoryCandidateRow.owner_user_id == generation.owner_user_id,
                        MemoryCandidateRow.namespace == generation.namespace,
                        MemoryCandidateRow.consolidation_generation_id == generation.id,
                        MemoryCandidateRow.status == "pending",
                        MemoryCandidateRow.content.is_not(None),
                        MemoryCandidateRow.content_erased_at.is_(None),
                    )
                    .order_by(MemoryCandidateRow.created_at, MemoryCandidateRow.id)
                    .with_for_update(of=MemoryCandidateRow)
                )
            ).scalars()
        )
        if not candidate_rows or len(candidate_rows) != int(generation.candidate_count) or _candidate_input_digest(candidate_rows) != generation.candidate_input_digest:
            return None
        candidate_ids = tuple(candidate.id for candidate in candidate_rows)
        successor_id = await self.jobs.enqueue(
            EnqueueJob(
                job_type="memory_consolidate",
                scope=JobScope(generation.project_id, generation.owner_user_id),
                idempotency_key=_canonical_digest(
                    {
                        "generation_id": str(generation.id),
                        "job_type": "memory_consolidate_recovery",
                        "predecessor_job_id": str(predecessor.id),
                    }
                ),
                run_id=None,
                occurrence_id=None,
                max_attempts=int(predecessor.max_attempts),
                namespace=generation.namespace,
                retry_safety="safe",
                priority=int(predecessor.priority),
                predecessor_dead_job_id=predecessor.id,
            )
        )
        generation.job_id = successor_id
        await self.session.flush()
        return MemoryConsolidationAdmissionRecord(
            generation_id=generation.id,
            job_id=successor_id,
            project_id=generation.project_id,
            owner_user_id=generation.owner_user_id,
            namespace=generation.namespace,
            candidate_ids=candidate_ids,
        )

    async def admit_next_consolidation(
        self,
        *,
        contract: MemoryConsolidationAdmissionContract,
        now: datetime,
    ) -> MemoryConsolidationAdmissionRecord | None:
        """Bind the oldest due Candidate scope to one idempotent Job."""

        self._validate_consolidation_contract(contract)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Memory consolidation admission time is invalid")
        recovered = await self._recover_dead_consolidation()
        if recovered is not None:
            return recovered
        due_before = now - timedelta(minutes=contract.interval_minutes)
        active_job = self._active_memory_job_exists(
            job_type="memory_consolidate",
            project_id=MemoryCandidateRow.project_id,
            owner_user_id=MemoryCandidateRow.owner_user_id,
            namespace=MemoryCandidateRow.namespace,
        )
        scope = (
            await self.session.execute(
                select(
                    MemoryCandidateRow.project_id,
                    MemoryCandidateRow.owner_user_id,
                    MemoryCandidateRow.namespace,
                )
                .join(
                    MemorySourceBatchRow,
                    and_(
                        MemorySourceBatchRow.id == MemoryCandidateRow.source_batch_id,
                        MemorySourceBatchRow.project_id == MemoryCandidateRow.project_id,
                        MemorySourceBatchRow.owner_user_id == MemoryCandidateRow.owner_user_id,
                        MemorySourceBatchRow.namespace == MemoryCandidateRow.namespace,
                    ),
                )
                .join(
                    MemorySourceItemRow,
                    and_(
                        MemorySourceItemRow.id == MemoryCandidateRow.source_item_id,
                        MemorySourceItemRow.project_id == MemoryCandidateRow.project_id,
                        MemorySourceItemRow.owner_user_id == MemoryCandidateRow.owner_user_id,
                        MemorySourceItemRow.namespace == MemoryCandidateRow.namespace,
                    ),
                )
                .join(
                    ProjectRow,
                    ProjectRow.id == MemoryCandidateRow.project_id,
                )
                .join(
                    ProjectMembershipRow,
                    and_(
                        ProjectMembershipRow.project_id == MemoryCandidateRow.project_id,
                        ProjectMembershipRow.user_id == MemoryCandidateRow.owner_user_id,
                    ),
                )
                .where(
                    MemoryCandidateRow.status == "pending",
                    MemoryCandidateRow.consolidation_generation_id.is_(None),
                    MemoryCandidateRow.content.is_not(None),
                    MemoryCandidateRow.content_erased_at.is_(None),
                    MemoryCandidateRow.created_at <= due_before,
                    MemorySourceBatchRow.pipeline_mode.in_(("consolidate", "v2")),
                    MemorySourceBatchRow.suppressed_at.is_(None),
                    MemorySourceItemRow.source_erased_at.is_(None),
                    MemorySourceItemRow.content.is_not(None),
                    ProjectRow.status == "active",
                    ProjectRow.is_suspended.is_(False),
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.role.in_(
                        (
                            "admin",
                            "editor",
                            "runner",
                            "channel_guest",
                        ),
                    ),
                    ~active_job,
                )
                .order_by(MemoryCandidateRow.created_at, MemoryCandidateRow.id)
                .limit(1)
            )
        ).one_or_none()
        if scope is None:
            return None
        project_id, owner_user_id, namespace = scope
        if not await self._lock_live_scope(
            project_id=project_id,
            owner_user_id=owner_user_id,
        ):
            return None
        existing_active_job = await self.session.scalar(
            select(JobRow.id)
            .where(
                JobRow.job_type == "memory_consolidate",
                JobRow.project_id == project_id,
                JobRow.owner_user_id == owner_user_id,
                JobRow.namespace == namespace,
                JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
            )
            .with_for_update(of=JobRow)
            .limit(1)
        )
        if existing_active_job is not None:
            return None
        candidate_rows = tuple(
            (
                await self.session.execute(
                    select(MemoryCandidateRow)
                    .join(
                        MemorySourceBatchRow,
                        and_(
                            MemorySourceBatchRow.id == MemoryCandidateRow.source_batch_id,
                            MemorySourceBatchRow.project_id == MemoryCandidateRow.project_id,
                            MemorySourceBatchRow.owner_user_id == MemoryCandidateRow.owner_user_id,
                            MemorySourceBatchRow.namespace == MemoryCandidateRow.namespace,
                        ),
                    )
                    .join(
                        MemorySourceItemRow,
                        and_(
                            MemorySourceItemRow.id == MemoryCandidateRow.source_item_id,
                            MemorySourceItemRow.project_id == MemoryCandidateRow.project_id,
                            MemorySourceItemRow.owner_user_id == MemoryCandidateRow.owner_user_id,
                            MemorySourceItemRow.namespace == MemoryCandidateRow.namespace,
                        ),
                    )
                    .where(
                        MemoryCandidateRow.project_id == project_id,
                        MemoryCandidateRow.owner_user_id == owner_user_id,
                        MemoryCandidateRow.namespace == namespace,
                        MemoryCandidateRow.status == "pending",
                        MemoryCandidateRow.consolidation_generation_id.is_(None),
                        MemoryCandidateRow.content.is_not(None),
                        MemoryCandidateRow.content_erased_at.is_(None),
                        MemoryCandidateRow.created_at <= due_before,
                        MemorySourceBatchRow.pipeline_mode.in_(("consolidate", "v2")),
                        MemorySourceBatchRow.suppressed_at.is_(None),
                        MemorySourceItemRow.source_erased_at.is_(None),
                        MemorySourceItemRow.content.is_not(None),
                    )
                    .order_by(MemoryCandidateRow.created_at, MemoryCandidateRow.id)
                    .limit(20)
                    .with_for_update(of=MemoryCandidateRow, skip_locked=True)
                )
            ).scalars()
        )
        if not candidate_rows:
            return None
        input_digest = _candidate_input_digest(candidate_rows)
        contract_digest = _consolidation_contract_digest(contract)
        idempotency_key = _canonical_digest(
            {
                "candidate_input_digest": input_digest,
                "contract_digest": contract_digest,
                "job_type": "memory_consolidate",
                "namespace": namespace,
                "owner_user_id": owner_user_id,
                "project_id": str(project_id),
            }
        )
        job_id = await self.jobs.enqueue(
            EnqueueJob(
                job_type="memory_consolidate",
                scope=JobScope(project_id, owner_user_id),
                idempotency_key=idempotency_key,
                run_id=None,
                occurrence_id=None,
                max_attempts=contract.max_attempts,
                namespace=namespace,
                retry_safety="safe",
            )
        )
        generation_id = uuid.uuid4()
        inserted_generation_id = await self.session.scalar(
            insert(MemoryConsolidationGenerationRow)
            .values(
                id=generation_id,
                project_id=project_id,
                owner_user_id=owner_user_id,
                namespace=namespace,
                job_id=job_id,
                candidate_input_digest=input_digest,
                candidate_count=len(candidate_rows),
                contract_digest=contract_digest,
                policy_revision=contract.policy_revision,
                model_config_id=contract.model_config_id,
                model_config_version_id=contract.model_config_version_id,
                model_config_checksum=contract.model_config_checksum,
                prompt_version=contract.prompt_version,
                consolidator_version=contract.consolidator_version,
                output_schema_version=contract.output_schema_version,
            )
            .on_conflict_do_nothing(
                constraint="uq_memory_consolidation_generations_contract",
            )
            .returning(MemoryConsolidationGenerationRow.id)
        )
        if inserted_generation_id is None:
            generation = (
                await self.session.execute(
                    select(MemoryConsolidationGenerationRow)
                    .where(
                        MemoryConsolidationGenerationRow.project_id == project_id,
                        MemoryConsolidationGenerationRow.owner_user_id == owner_user_id,
                        MemoryConsolidationGenerationRow.namespace == namespace,
                        MemoryConsolidationGenerationRow.candidate_input_digest == input_digest,
                        MemoryConsolidationGenerationRow.contract_digest == contract_digest,
                    )
                    .with_for_update(of=MemoryConsolidationGenerationRow)
                )
            ).scalar_one()
            if (
                generation.job_id != job_id
                or generation.candidate_count != len(candidate_rows)
                or generation.policy_revision != contract.policy_revision
                or generation.model_config_id != contract.model_config_id
                or generation.model_config_version_id != contract.model_config_version_id
                or generation.model_config_checksum != contract.model_config_checksum
                or generation.prompt_version != contract.prompt_version
                or generation.consolidator_version != contract.consolidator_version
                or generation.output_schema_version != contract.output_schema_version
            ):
                raise MemoryV2AdmissionConflict(
                    "Memory consolidation generation replay does not match",
                )
            generation_id = generation.id
        else:
            generation_id = inserted_generation_id
        for candidate in candidate_rows:
            if candidate.consolidation_generation_id is not None:
                raise MemoryV2AdmissionConflict(
                    "Memory Candidate was bound concurrently",
                )
            candidate.consolidation_generation_id = generation_id
        await self.session.flush()
        return MemoryConsolidationAdmissionRecord(
            generation_id=generation_id,
            job_id=job_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            candidate_ids=tuple(candidate.id for candidate in candidate_rows),
        )

    async def load_consolidation_work(
        self,
        *,
        job_id: uuid.UUID,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        for_update: bool = False,
    ) -> MemoryConsolidationWork | None:
        """Load the frozen Candidate closure and active Fact revisions for a Job."""

        if not isinstance(job_id, uuid.UUID) or not isinstance(project_id, uuid.UUID) or not isinstance(owner_user_id, str) or not owner_user_id or not isinstance(namespace, str) or not namespace or type(for_update) is not bool:
            raise ValueError("Memory consolidation coordinates are invalid")
        generation_statement = select(MemoryConsolidationGenerationRow).where(
            MemoryConsolidationGenerationRow.job_id == job_id,
            MemoryConsolidationGenerationRow.project_id == project_id,
            MemoryConsolidationGenerationRow.owner_user_id == owner_user_id,
            MemoryConsolidationGenerationRow.namespace == namespace,
        )
        if for_update:
            generation_statement = generation_statement.with_for_update(
                of=MemoryConsolidationGenerationRow,
            )
        job_statement = select(JobRow).where(
            JobRow.id == job_id,
            JobRow.job_type == "memory_consolidate",
            JobRow.project_id == project_id,
            JobRow.owner_user_id == owner_user_id,
            JobRow.namespace == namespace,
            JobRow.run_id.is_(None),
            JobRow.automation_occurrence_id.is_(None),
            JobRow.status.in_(("leased", "running")),
        )
        if for_update:
            job_statement = job_statement.with_for_update(of=JobRow)
        job = (await self.session.execute(job_statement)).scalar_one_or_none()
        if job is None:
            raise MemoryV2ConsolidationConflict(
                "Memory consolidation Job authority is unavailable",
            )
        generation = (await self.session.execute(generation_statement)).scalar_one_or_none()
        if generation is None:
            return None
        if generation.fact_committed_at is not None:
            return MemoryConsolidationWork(
                generation_id=generation.id,
                job_id=job_id,
                project_id=project_id,
                owner_user_id=owner_user_id,
                namespace=namespace,
                candidate_input_digest=generation.candidate_input_digest,
                contract_digest=generation.contract_digest,
                policy_revision=int(generation.policy_revision),
                model_config_id=generation.model_config_id,
                model_config_version_id=generation.model_config_version_id,
                model_config_checksum=generation.model_config_checksum,
                prompt_version=generation.prompt_version,
                consolidator_version=generation.consolidator_version,
                output_schema_version=generation.output_schema_version,
                candidates=(),
                facts=(),
                active_fact_count=0,
                suppressed=False,
                cancel_requested=job.cancel_requested_at is not None,
                fact_committed=True,
            )

        candidate_statement = (
            select(
                MemoryCandidateRow,
                MemorySourceItemRow,
                MemorySourceBatchRow,
            )
            .join(
                MemorySourceItemRow,
                and_(
                    MemorySourceItemRow.id == MemoryCandidateRow.source_item_id,
                    MemorySourceItemRow.project_id == MemoryCandidateRow.project_id,
                    MemorySourceItemRow.owner_user_id == MemoryCandidateRow.owner_user_id,
                    MemorySourceItemRow.namespace == MemoryCandidateRow.namespace,
                ),
            )
            .join(
                MemorySourceBatchRow,
                and_(
                    MemorySourceBatchRow.id == MemoryCandidateRow.source_batch_id,
                    MemorySourceBatchRow.project_id == MemoryCandidateRow.project_id,
                    MemorySourceBatchRow.owner_user_id == MemoryCandidateRow.owner_user_id,
                    MemorySourceBatchRow.namespace == MemoryCandidateRow.namespace,
                ),
            )
            .where(
                MemoryCandidateRow.project_id == project_id,
                MemoryCandidateRow.owner_user_id == owner_user_id,
                MemoryCandidateRow.namespace == namespace,
                MemoryCandidateRow.consolidation_generation_id == generation.id,
            )
            .order_by(MemoryCandidateRow.created_at, MemoryCandidateRow.id)
        )
        if for_update:
            candidate_statement = candidate_statement.with_for_update(
                of=(
                    MemoryCandidateRow,
                    MemorySourceItemRow,
                    MemorySourceBatchRow,
                ),
            )
        candidate_material = tuple((await self.session.execute(candidate_statement)).all())
        candidate_rows = tuple(value[0] for value in candidate_material)
        if len(candidate_rows) != generation.candidate_count or not candidate_rows or _candidate_input_digest(candidate_rows) != generation.candidate_input_digest:
            raise MemoryV2ConsolidationConflict(
                "Memory consolidation Candidate input changed",
            )
        suppressed = False
        candidates: list[MemoryConsolidationCandidateRecord] = []
        for candidate, source_item, source_batch in candidate_material:
            valid_content = candidate.status == "pending" and candidate.content is not None and candidate.content_erased_at is None and hashlib.sha256(candidate.content.encode("utf-8")).hexdigest() == candidate.content_digest
            valid_source = source_item.source_erased_at is None and source_item.content is not None and source_batch.pipeline_mode in {"consolidate", "v2"} and source_batch.suppressed_at is None
            if not valid_content:
                raise MemoryV2ConsolidationConflict(
                    "Memory consolidation Candidate is unavailable",
                )
            if not valid_source:
                suppressed = True
            candidates.append(
                MemoryConsolidationCandidateRecord(
                    id=candidate.id,
                    candidate_type=candidate.candidate_type,
                    content=candidate.content,
                    content_digest=candidate.content_digest,
                    confidence=float(candidate.confidence),
                    retention_class=candidate.retention_class,
                    sensitivity=candidate.sensitivity,
                    source_item_id=source_item.id,
                    source_identity_hmac=source_item.content_hmac,
                    thread_id=source_batch.thread_id,
                    run_id=source_batch.run_id,
                    run_event_sequence=source_item.run_event_sequence,
                )
            )

        active_fact_count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(MemoryFactRow)
                .where(
                    MemoryFactRow.project_id == project_id,
                    MemoryFactRow.owner_user_id == owner_user_id,
                    MemoryFactRow.namespace == namespace,
                    MemoryFactRow.status == "active",
                )
            )
            or 0
        )
        fact_statement = (
            select(MemoryFactRow, MemoryFactRevisionRow)
            .join(
                MemoryFactRevisionRow,
                and_(
                    MemoryFactRevisionRow.id == MemoryFactRow.current_revision_id,
                    MemoryFactRevisionRow.fact_id == MemoryFactRow.id,
                    MemoryFactRevisionRow.project_id == MemoryFactRow.project_id,
                    MemoryFactRevisionRow.owner_user_id == MemoryFactRow.owner_user_id,
                    MemoryFactRevisionRow.namespace == MemoryFactRow.namespace,
                ),
            )
            .where(
                MemoryFactRow.project_id == project_id,
                MemoryFactRow.owner_user_id == owner_user_id,
                MemoryFactRow.namespace == namespace,
                MemoryFactRow.status == "active",
            )
            .order_by(MemoryFactRow.updated_at.desc(), MemoryFactRow.id)
            .limit(500)
        )
        if for_update:
            fact_statement = fact_statement.with_for_update(
                of=(MemoryFactRow, MemoryFactRevisionRow),
            )
        fact_material = tuple((await self.session.execute(fact_statement)).all())
        facts: list[MemoryConsolidationFactRecord] = []
        for fact, revision in fact_material:
            if revision.content is None or revision.content_erased_at is not None or hashlib.sha256(revision.content.encode("utf-8")).hexdigest() != revision.content_digest:
                raise MemoryV2ConsolidationConflict(
                    "Memory Fact current revision is unavailable",
                )
            facts.append(
                MemoryConsolidationFactRecord(
                    id=fact.id,
                    revision_id=revision.id,
                    revision_number=int(revision.revision_number),
                    fact_kind=fact.fact_kind,
                    version=int(fact.version),
                    content=revision.content,
                    content_digest=revision.content_digest,
                    category=revision.category,
                    confidence=float(revision.confidence),
                    last_confirmed_at=revision.last_confirmed_at,
                )
            )
        return MemoryConsolidationWork(
            generation_id=generation.id,
            job_id=job_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            candidate_input_digest=generation.candidate_input_digest,
            contract_digest=generation.contract_digest,
            policy_revision=int(generation.policy_revision),
            model_config_id=generation.model_config_id,
            model_config_version_id=generation.model_config_version_id,
            model_config_checksum=generation.model_config_checksum,
            prompt_version=generation.prompt_version,
            consolidator_version=generation.consolidator_version,
            output_schema_version=generation.output_schema_version,
            candidates=tuple(candidates),
            facts=tuple(facts),
            active_fact_count=active_fact_count,
            suppressed=suppressed,
            cancel_requested=job.cancel_requested_at is not None,
            fact_committed=False,
        )

    @staticmethod
    def _validate_consolidation_decision(
        decision: MemoryConsolidationDecisionWrite,
    ) -> None:
        if type(decision) is not MemoryConsolidationDecisionWrite:
            raise MemoryV2ConsolidationConflict(
                "Memory consolidation decision is invalid",
            )
        has_fact_value = (
            isinstance(decision.content, str)
            and bool(decision.content)
            and len(decision.content) <= 16_000
            and isinstance(decision.category, str)
            and bool(decision.category)
            and len(decision.category) <= 32
            and not isinstance(decision.confidence, bool)
            and isinstance(decision.confidence, int | float)
            and math.isfinite(float(decision.confidence))
            and 0 <= float(decision.confidence) <= 1
        )
        empty_fact_value = decision.content is None and decision.category is None and decision.confidence is None
        if decision.action == "create":
            valid = decision.target_fact_id is None and decision.expected_revision_id is None and has_fact_value and decision.change_reason == "new_fact" and decision.decision_reason is None
        elif decision.action == "confirm":
            valid = isinstance(decision.target_fact_id, uuid.UUID) and isinstance(decision.expected_revision_id, uuid.UUID) and empty_fact_value and decision.change_reason is None and decision.decision_reason == "same_fact"
        elif decision.action == "revise":
            valid = isinstance(decision.target_fact_id, uuid.UUID) and isinstance(decision.expected_revision_id, uuid.UUID) and has_fact_value and decision.change_reason in {"supplement", "correction"} and decision.decision_reason is None
        elif decision.action == "pending":
            valid = decision.target_fact_id is None and decision.expected_revision_id is None and empty_fact_value and decision.change_reason is None and decision.decision_reason in {"insufficient_evidence", "possible_conflict"}
        elif decision.action == "reject":
            valid = decision.target_fact_id is None and decision.expected_revision_id is None and empty_fact_value and decision.change_reason is None and decision.decision_reason in {"unsupported_governance_change", "sensitive_content"}
        else:
            valid = False
        if not valid:
            raise MemoryV2ConsolidationConflict(
                "Memory consolidation decision is invalid",
            )

    async def finalize_consolidation(
        self,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        generation_id: uuid.UUID,
        candidate_input_digest: str,
        contract_digest: str,
        decisions: tuple[MemoryConsolidationDecisionWrite, ...],
        max_facts: int,
        fact_confidence_threshold: float,
        cancel: bool,
        release_candidates_on_cancel: bool = False,
        now: datetime | None = None,
    ) -> MemoryFactCommitRecord:
        """Commit Candidate decisions and Fact changes in the lease transaction."""

        if (
            not isinstance(lease_token, str)
            or not lease_token
            or not isinstance(generation_id, uuid.UUID)
            or _SHA256.fullmatch(candidate_input_digest) is None
            or _SHA256.fullmatch(contract_digest) is None
            or not isinstance(decisions, tuple)
            or type(max_facts) is not int
            or not 10 <= max_facts <= 500
            or isinstance(fact_confidence_threshold, bool)
            or not isinstance(fact_confidence_threshold, int | float)
            or not math.isfinite(float(fact_confidence_threshold))
            or not 0 <= float(fact_confidence_threshold) <= 1
            or type(cancel) is not bool
            or type(release_candidates_on_cancel) is not bool
            or (release_candidates_on_cancel and not cancel)
            or (now is not None and (not isinstance(now, datetime) or now.tzinfo is None))
        ):
            raise ValueError("Memory consolidation settlement is invalid")
        settled_at = now or datetime.now(UTC)
        work = await self.load_consolidation_work(
            job_id=job_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            for_update=True,
        )
        if work is None or work.generation_id != generation_id or work.candidate_input_digest != candidate_input_digest or work.contract_digest != contract_digest:
            raise MemoryV2ConsolidationConflict(
                "Memory consolidation settlement does not match",
            )
        if cancel or work.suppressed or work.cancel_requested:
            if release_candidates_on_cancel and not work.suppressed:
                await self.session.execute(
                    update(MemoryCandidateRow)
                    .where(
                        MemoryCandidateRow.project_id == project_id,
                        MemoryCandidateRow.owner_user_id == owner_user_id,
                        MemoryCandidateRow.namespace == namespace,
                        MemoryCandidateRow.consolidation_generation_id == generation_id,
                        MemoryCandidateRow.status == "pending",
                    )
                    .values(consolidation_generation_id=None)
                )
            changed = await self.jobs.settle_cancelled(
                job_id,
                lease_token=lease_token,
                now=settled_at,
            )
            if not changed:
                raise MemoryV2ConsolidationLeaseLost
            return MemoryFactCommitRecord(
                generation_id=generation_id,
                candidate_ids=(),
                fact_ids=(),
                revision_ids=(),
                status="cancelled",
            )
        if work.fact_committed:
            changed = await self.jobs.settle_success(
                job_id,
                lease_token=lease_token,
                now=settled_at,
            )
            if not changed:
                raise MemoryV2ConsolidationLeaseLost
            return MemoryFactCommitRecord(
                generation_id=generation_id,
                candidate_ids=(),
                fact_ids=(),
                revision_ids=(),
                status="replayed",
            )
        if len(decisions) != len(work.candidates):
            raise MemoryV2ConsolidationConflict(
                "Memory consolidation decisions are incomplete",
            )
        for decision in decisions:
            self._validate_consolidation_decision(decision)
        decisions_by_candidate = {decision.candidate_id: decision for decision in decisions}
        candidates_by_id = {candidate.id: candidate for candidate in work.candidates}
        if len(decisions_by_candidate) != len(decisions) or set(decisions_by_candidate) != set(candidates_by_id):
            raise MemoryV2ConsolidationConflict(
                "Memory consolidation decisions are not traceable",
            )
        facts_by_id = {fact.id: fact for fact in work.facts}
        revised_fact_ids = [decision.target_fact_id for decision in decisions if decision.action == "revise"]
        if len(revised_fact_ids) != len(set(revised_fact_ids)):
            raise MemoryV2ConsolidationConflict(
                "A Memory Fact cannot be revised twice in one generation",
            )
        policy_pending_candidate_ids: set[uuid.UUID] = set()
        available_fact_slots = max(0, max_facts - work.active_fact_count)
        admitted_create_signatures: set[tuple[str, str, str]] = set()
        for decision in decisions:
            candidate = candidates_by_id[decision.candidate_id]
            if candidate.sensitivity != "normal" and not (decision.action == "reject" and decision.decision_reason == "sensitive_content"):
                raise MemoryV2ConsolidationConflict(
                    "Sensitive Memory Candidate must be rejected",
                )
            if decision.target_fact_id is not None:
                target = facts_by_id.get(decision.target_fact_id)
                if target is None:
                    raise MemoryV2ConsolidationConflict(
                        "Memory consolidation target Fact is unavailable",
                    )
            if decision.action in {"create", "revise"} and float(decision.confidence or 0) < float(fact_confidence_threshold):
                policy_pending_candidate_ids.add(decision.candidate_id)
            if decision.action == "create" and decision.candidate_id not in policy_pending_candidate_ids:
                create_signature = _create_fact_signature(
                    candidate.candidate_type,
                    decision.content,
                    decision.category,
                )
                if create_signature in admitted_create_signatures:
                    continue
                if available_fact_slots == 0:
                    policy_pending_candidate_ids.add(decision.candidate_id)
                else:
                    available_fact_slots -= 1
                    admitted_create_signatures.add(create_signature)

        changed = await self.jobs.settle_success(
            job_id,
            lease_token=lease_token,
            now=settled_at,
        )
        if not changed:
            raise MemoryV2ConsolidationLeaseLost

        revision_sequence = int(
            await self.session.scalar(
                select(func.coalesce(func.max(MemoryFactRevisionRow.revision_sequence), 0)).where(
                    MemoryFactRevisionRow.project_id == project_id,
                    MemoryFactRevisionRow.owner_user_id == owner_user_id,
                    MemoryFactRevisionRow.namespace == namespace,
                )
            )
            or 0
        )
        fact_rows = {
            row.id: row
            for row in (
                await self.session.execute(
                    select(MemoryFactRow).where(
                        MemoryFactRow.project_id == project_id,
                        MemoryFactRow.owner_user_id == owner_user_id,
                        MemoryFactRow.namespace == namespace,
                        MemoryFactRow.id.in_(tuple(facts_by_id) or (uuid.uuid4(),)),
                    )
                )
            ).scalars()
        }
        revision_rows = {
            row.id: row
            for row in (
                await self.session.execute(
                    select(MemoryFactRevisionRow).where(
                        MemoryFactRevisionRow.project_id == project_id,
                        MemoryFactRevisionRow.owner_user_id == owner_user_id,
                        MemoryFactRevisionRow.namespace == namespace,
                        MemoryFactRevisionRow.id.in_(tuple(fact.revision_id for fact in work.facts) or (uuid.uuid4(),)),
                    )
                )
            ).scalars()
        }
        created_fact_ids: list[uuid.UUID] = []
        created_revision_ids: list[uuid.UUID] = []
        evidence_values: list[dict[str, object]] = []
        accepted_candidate_ids: list[uuid.UUID] = []
        created_by_signature: dict[
            tuple[str, str, str],
            tuple[uuid.UUID, uuid.UUID],
        ] = {}
        for decision in decisions:
            if decision.target_fact_id is None:
                continue
            target = facts_by_id[decision.target_fact_id]
            fact_row = fact_rows.get(target.id)
            if fact_row is None or fact_row.current_revision_id != target.revision_id:
                raise MemoryV2ConsolidationConflict(
                    "Memory Fact revision changed during consolidation",
                )
        for candidate in work.candidates:
            decision = decisions_by_candidate[candidate.id]
            candidate_row = await self.session.get(MemoryCandidateRow, candidate.id)
            if candidate_row is None or candidate_row.status != "pending":
                raise MemoryV2ConsolidationConflict(
                    "Memory Candidate changed during consolidation",
                )
            if decision.action == "pending" or candidate.id in policy_pending_candidate_ids:
                continue
            if decision.action == "reject":
                candidate_row.status = "rejected"
                candidate_row.decision_reason = decision.decision_reason
                candidate_row.decided_at = settled_at
                continue
            evidence_fact_id: uuid.UUID
            evidence_revision_id: uuid.UUID
            if decision.action == "create":
                create_signature = _create_fact_signature(
                    candidate.candidate_type,
                    decision.content,
                    decision.category,
                )
                existing_created = created_by_signature.get(create_signature)
                if existing_created is not None:
                    evidence_fact_id, evidence_revision_id = existing_created
                    candidate_row.status = "accepted"
                    candidate_row.decision_reason = "same_fact"
                    candidate_row.decided_at = settled_at
                    accepted_candidate_ids.append(candidate.id)
                    evidence_values.append(
                        {
                            "id": uuid.uuid4(),
                            "project_id": project_id,
                            "owner_user_id": owner_user_id,
                            "namespace": namespace,
                            "fact_id": evidence_fact_id,
                            "revision_id": evidence_revision_id,
                            "source_candidate_id": candidate.id,
                            "source_item_id": candidate.source_item_id,
                            "thread_id": candidate.thread_id,
                            "run_id": candidate.run_id,
                            "run_event_sequence": candidate.run_event_sequence,
                            "source_identity_hmac": candidate.source_identity_hmac,
                            "evidence_excerpt": candidate.content[:4_000],
                            "trust_class": "direct",
                            "source_erased_at": None,
                            "created_at": settled_at,
                        }
                    )
                    continue
                fact_id = uuid.uuid4()
                revision_id = uuid.uuid4()
                revision_sequence += 1
                fact_row = MemoryFactRow(
                    id=fact_id,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    namespace=namespace,
                    fact_kind=candidate.candidate_type,
                    status="active",
                    current_revision_id=revision_id,
                    version=1,
                    disabled_at=None,
                    superseded_at=None,
                    deleted_at=None,
                    created_at=settled_at,
                    updated_at=settled_at,
                )
                revision_row = MemoryFactRevisionRow(
                    id=revision_id,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    namespace=namespace,
                    fact_id=fact_id,
                    revision_number=1,
                    revision_sequence=revision_sequence,
                    content=decision.content,
                    content_digest=hashlib.sha256((decision.content or "").encode("utf-8")).hexdigest(),
                    category=decision.category,
                    confidence=float(decision.confidence or 0),
                    valid_from=settled_at,
                    valid_to=None,
                    last_confirmed_at=settled_at,
                    changed_by="consolidator",
                    source_candidate_id=candidate.id,
                    supersedes_revision_id=None,
                    change_reason="new_fact",
                    content_erased_at=None,
                    created_at=settled_at,
                )
                self.session.add_all((fact_row, revision_row))
                fact_rows[fact_id] = fact_row
                revision_rows[revision_id] = revision_row
                created_fact_ids.append(fact_id)
                created_revision_ids.append(revision_id)
                created_by_signature[create_signature] = (
                    fact_id,
                    revision_id,
                )
                evidence_fact_id = fact_id
                evidence_revision_id = revision_id
            else:
                target = facts_by_id[decision.target_fact_id]
                fact_row = fact_rows.get(target.id)
                current_revision = revision_rows.get(target.revision_id)
                if fact_row is None or current_revision is None or decision.expected_revision_id != target.revision_id:
                    raise MemoryV2ConsolidationConflict(
                        "Memory Fact revision changed during consolidation",
                    )
                if decision.action == "confirm":
                    current_revision.last_confirmed_at = settled_at
                    evidence_fact_id = fact_row.id
                    evidence_revision_id = current_revision.id
                else:
                    if fact_row.current_revision_id != target.revision_id:
                        raise MemoryV2ConsolidationConflict(
                            "Memory Fact revision changed during consolidation",
                        )
                    revision_id = uuid.uuid4()
                    revision_sequence += 1
                    revision_row = MemoryFactRevisionRow(
                        id=revision_id,
                        project_id=project_id,
                        owner_user_id=owner_user_id,
                        namespace=namespace,
                        fact_id=fact_row.id,
                        revision_number=int(current_revision.revision_number) + 1,
                        revision_sequence=revision_sequence,
                        content=decision.content,
                        content_digest=hashlib.sha256((decision.content or "").encode("utf-8")).hexdigest(),
                        category=decision.category,
                        confidence=float(decision.confidence or 0),
                        valid_from=settled_at,
                        valid_to=None,
                        last_confirmed_at=settled_at,
                        changed_by="consolidator",
                        source_candidate_id=candidate.id,
                        supersedes_revision_id=current_revision.id,
                        change_reason=decision.change_reason,
                        content_erased_at=None,
                        created_at=settled_at,
                    )
                    self.session.add(revision_row)
                    current_revision.valid_to = settled_at
                    fact_row.current_revision_id = revision_id
                    fact_row.version = int(fact_row.version) + 1
                    fact_row.updated_at = settled_at
                    revision_rows[revision_id] = revision_row
                    created_revision_ids.append(revision_id)
                    evidence_fact_id = fact_row.id
                    evidence_revision_id = revision_id
            candidate_row.status = "accepted"
            candidate_row.decision_reason = "same_fact" if decision.action == "confirm" else decision.change_reason
            candidate_row.decided_at = settled_at
            accepted_candidate_ids.append(candidate.id)
            evidence_values.append(
                {
                    "id": uuid.uuid4(),
                    "project_id": project_id,
                    "owner_user_id": owner_user_id,
                    "namespace": namespace,
                    "fact_id": evidence_fact_id,
                    "revision_id": evidence_revision_id,
                    "source_candidate_id": candidate.id,
                    "source_item_id": candidate.source_item_id,
                    "thread_id": candidate.thread_id,
                    "run_id": candidate.run_id,
                    "run_event_sequence": candidate.run_event_sequence,
                    "source_identity_hmac": candidate.source_identity_hmac,
                    "evidence_excerpt": candidate.content[:4_000],
                    "trust_class": "direct",
                    "source_erased_at": None,
                    "created_at": settled_at,
                }
            )
        await self.session.flush()
        for values in evidence_values:
            await self.session.execute(
                insert(MemoryFactEvidenceRow)
                .values(**values)
                .on_conflict_do_nothing(
                    constraint="uq_memory_fact_evidence_identity",
                )
            )
        generation = await self.session.get(
            MemoryConsolidationGenerationRow,
            generation_id,
        )
        if generation is None or generation.fact_committed_at is not None:
            raise MemoryV2ConsolidationConflict(
                "Memory consolidation Generation changed during settlement",
            )
        generation.fact_committed_at = settled_at
        await self.session.flush()
        return MemoryFactCommitRecord(
            generation_id=generation_id,
            candidate_ids=tuple(accepted_candidate_ids),
            fact_ids=tuple(created_fact_ids),
            revision_ids=tuple(created_revision_ids),
            status="succeeded",
        )

    async def admit_next_retention(
        self,
        *,
        retention_days: int,
        policy_revision: int,
        now: datetime,
    ) -> MemoryRetentionAdmissionRecord | None:
        """Admit one scope whose terminal Candidate bodies are due for erasure."""

        if type(retention_days) is not int or not 1 <= retention_days <= 365 or type(policy_revision) is not int or policy_revision < 1 or not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Memory retention admission contract is invalid")
        due_before = now - timedelta(days=retention_days)
        active_job = self._active_memory_job_exists(
            job_type="memory_retention_purge",
            project_id=MemoryCandidateRow.project_id,
            owner_user_id=MemoryCandidateRow.owner_user_id,
            namespace=MemoryCandidateRow.namespace,
        )
        scope = (
            await self.session.execute(
                select(
                    MemoryCandidateRow.project_id,
                    MemoryCandidateRow.owner_user_id,
                    MemoryCandidateRow.namespace,
                )
                .where(
                    MemoryCandidateRow.status.in_(
                        ("accepted", "rejected", "superseded"),
                    ),
                    MemoryCandidateRow.decided_at <= due_before,
                    MemoryCandidateRow.content.is_not(None),
                    MemoryCandidateRow.content_erased_at.is_(None),
                    ~active_job,
                )
                .join(
                    ProjectRow,
                    ProjectRow.id == MemoryCandidateRow.project_id,
                )
                .join(
                    ProjectMembershipRow,
                    and_(
                        ProjectMembershipRow.project_id == MemoryCandidateRow.project_id,
                        ProjectMembershipRow.user_id == MemoryCandidateRow.owner_user_id,
                    ),
                )
                .where(
                    ProjectRow.status == "active",
                    ProjectRow.is_suspended.is_(False),
                    ProjectMembershipRow.status == "active",
                    ProjectMembershipRow.role.in_(
                        (
                            "admin",
                            "editor",
                            "runner",
                            "viewer",
                            "channel_guest",
                        ),
                    ),
                )
                .order_by(MemoryCandidateRow.decided_at, MemoryCandidateRow.id)
                .limit(1)
            )
        ).one_or_none()
        if scope is None:
            return None
        project_id, owner_user_id, namespace = scope
        if not await self._lock_live_scope(
            project_id=project_id,
            owner_user_id=owner_user_id,
            allow_viewer=True,
        ):
            return None
        existing_active_job = await self.session.scalar(
            select(JobRow.id)
            .where(
                JobRow.job_type == "memory_retention_purge",
                JobRow.project_id == project_id,
                JobRow.owner_user_id == owner_user_id,
                JobRow.namespace == namespace,
                JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
            )
            .with_for_update(of=JobRow)
            .limit(1)
        )
        if existing_active_job is not None:
            return None
        oldest_candidate_id = await self.session.scalar(
            select(MemoryCandidateRow.id)
            .where(
                MemoryCandidateRow.project_id == project_id,
                MemoryCandidateRow.owner_user_id == owner_user_id,
                MemoryCandidateRow.namespace == namespace,
                MemoryCandidateRow.status.in_(
                    ("accepted", "rejected", "superseded"),
                ),
                MemoryCandidateRow.decided_at <= due_before,
                MemoryCandidateRow.content.is_not(None),
                MemoryCandidateRow.content_erased_at.is_(None),
            )
            .order_by(MemoryCandidateRow.decided_at, MemoryCandidateRow.id)
            .with_for_update(of=MemoryCandidateRow, skip_locked=True)
            .limit(1)
        )
        if oldest_candidate_id is None:
            return None
        idempotency_key = _canonical_digest(
            {
                "job_type": "memory_retention_purge",
                "namespace": namespace,
                "oldest_candidate_id": str(oldest_candidate_id),
                "owner_user_id": owner_user_id,
                "policy_revision": policy_revision,
                "project_id": str(project_id),
                "retention_cutoff_at": due_before.isoformat(),
            }
        )
        job_id = await self.jobs.enqueue(
            EnqueueJob(
                job_type="memory_retention_purge",
                scope=JobScope(project_id, owner_user_id),
                idempotency_key=idempotency_key,
                run_id=None,
                occurrence_id=None,
                max_attempts=3,
                namespace=namespace,
                retry_safety="safe",
                memory_retention_cutoff_at=due_before,
            )
        )
        job_status = await self.session.scalar(
            select(JobRow.status).where(JobRow.id == job_id),
        )
        if job_status not in {"queued", "leased", "running", "retry_wait"}:
            return None
        return MemoryRetentionAdmissionRecord(
            job_id=job_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
        )

    async def finalize_retention(
        self,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        cancel: bool,
        now: datetime | None = None,
    ) -> MemoryRetentionCommitRecord:
        """Erase due terminal Candidate bodies after lease validation."""

        if (
            not isinstance(job_id, uuid.UUID)
            or not isinstance(lease_token, str)
            or not lease_token
            or not isinstance(project_id, uuid.UUID)
            or not isinstance(owner_user_id, str)
            or not owner_user_id
            or not isinstance(namespace, str)
            or not namespace
            or type(cancel) is not bool
            or (now is not None and (not isinstance(now, datetime) or now.tzinfo is None))
        ):
            raise ValueError("Memory retention settlement is invalid")
        settled_at = now or datetime.now(UTC)
        job = (
            await self.session.execute(
                select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.job_type == "memory_retention_purge",
                    JobRow.project_id == project_id,
                    JobRow.owner_user_id == owner_user_id,
                    JobRow.namespace == namespace,
                    JobRow.run_id.is_(None),
                    JobRow.automation_occurrence_id.is_(None),
                    JobRow.status.in_(("leased", "running")),
                )
                .with_for_update(of=JobRow)
            )
        ).scalar_one_or_none()
        if job is None:
            raise MemoryV2RetentionConflict(
                "Memory retention Job authority is unavailable",
            )
        retention_cutoff_at = job.memory_retention_cutoff_at
        if retention_cutoff_at is None or retention_cutoff_at.tzinfo is None:
            raise MemoryV2RetentionConflict(
                "Memory retention cutoff is unavailable",
            )
        if cancel or job.cancel_requested_at is not None:
            changed = await self.jobs.settle_cancelled(
                job_id,
                lease_token=lease_token,
                now=settled_at,
            )
            if not changed:
                raise MemoryV2RetentionLeaseLost
            return MemoryRetentionCommitRecord(
                job_id=job_id,
                erased_count=0,
                status="cancelled",
            )
        changed = await self.jobs.settle_success(
            job_id,
            lease_token=lease_token,
            now=settled_at,
        )
        if not changed:
            raise MemoryV2RetentionLeaseLost
        result = await self.session.execute(
            update(MemoryCandidateRow)
            .where(
                MemoryCandidateRow.project_id == project_id,
                MemoryCandidateRow.owner_user_id == owner_user_id,
                MemoryCandidateRow.namespace == namespace,
                MemoryCandidateRow.status.in_(
                    ("accepted", "rejected", "superseded"),
                ),
                MemoryCandidateRow.decided_at <= retention_cutoff_at,
                MemoryCandidateRow.content.is_not(None),
                MemoryCandidateRow.content_erased_at.is_(None),
            )
            .values(
                content=None,
                content_erased_at=settled_at,
                updated_at=settled_at,
            )
        )
        await self.session.flush()
        return MemoryRetentionCommitRecord(
            job_id=job_id,
            erased_count=int(result.rowcount or 0),
            status="succeeded",
        )


__all__ = [
    "MemoryCandidateCommitRecord",
    "MemoryCandidateDraft",
    "MemoryCandidateWrite",
    "MemoryConsolidationAdmissionContract",
    "MemoryConsolidationAdmissionRecord",
    "MemoryConsolidationCandidateRecord",
    "MemoryConsolidationDecisionWrite",
    "MemoryConsolidationFactRecord",
    "MemoryConsolidationWork",
    "MemoryExtractionModelSnapshot",
    "MemoryExtractionSourceItemRecord",
    "MemoryExtractionWork",
    "MemoryFactCommitRecord",
    "MemoryRetentionAdmissionRecord",
    "MemoryRetentionCommitRecord",
    "MemorySourceAdmissionRecord",
    "MemorySourceAdmissionWrite",
    "MemorySourceItemWrite",
    "MemoryV2AdmissionConflict",
    "MemoryV2ConsolidationConflict",
    "MemoryV2ConsolidationLeaseLost",
    "MemoryV2ExtractionConflict",
    "MemoryV2ExtractionLeaseLost",
    "MemoryV2RetentionConflict",
    "MemoryV2RetentionLeaseLost",
    "MemoryV2Repository",
    "prepare_memory_candidate_writes",
]
