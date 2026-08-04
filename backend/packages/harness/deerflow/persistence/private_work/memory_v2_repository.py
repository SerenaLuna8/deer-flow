"""Session-bound persistence for Memory v2 source admission."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import JobAttemptRow, JobRow
from deerflow.persistence.jobs.sql import (
    EnqueueJob,
    JobRepository,
    JobScope,
)
from deerflow.persistence.private_work.memory_v2_model import (
    MemoryCandidateRow,
    MemoryExtractionGenerationRow,
    MemorySourceBatchRow,
    MemorySourceItemRow,
)
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


class MemoryV2AdmissionConflict(RuntimeError):
    """An existing idempotent admission does not match its immutable input."""


class MemoryV2ExtractionConflict(RuntimeError):
    """Extraction work no longer matches its immutable generation contract."""


class MemoryV2ExtractionLeaseLost(MemoryV2ExtractionConflict):
    """The extraction Job lease cannot authorize the final transaction."""


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


__all__ = [
    "MemoryCandidateCommitRecord",
    "MemoryCandidateDraft",
    "MemoryCandidateWrite",
    "MemoryExtractionModelSnapshot",
    "MemoryExtractionSourceItemRecord",
    "MemoryExtractionWork",
    "MemorySourceAdmissionRecord",
    "MemorySourceAdmissionWrite",
    "MemorySourceItemWrite",
    "MemoryV2AdmissionConflict",
    "MemoryV2ExtractionConflict",
    "MemoryV2ExtractionLeaseLost",
    "MemoryV2Repository",
    "prepare_memory_candidate_writes",
]
