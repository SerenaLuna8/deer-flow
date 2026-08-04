"""Session-bound persistence for Memory v2 source admission."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.sql import (
    EnqueueJob,
    JobRepository,
    JobScope,
)
from deerflow.persistence.private_work.memory_v2_model import (
    MemoryExtractionGenerationRow,
    MemorySourceBatchRow,
    MemorySourceItemRow,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")


class MemoryV2AdmissionConflict(RuntimeError):
    """An existing idempotent admission does not match its immutable input."""


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


__all__ = [
    "MemorySourceAdmissionRecord",
    "MemorySourceAdmissionWrite",
    "MemorySourceItemWrite",
    "MemoryV2AdmissionConflict",
    "MemoryV2Repository",
]
