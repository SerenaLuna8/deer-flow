"""Durable derived-object registration; network I/O never owns a transaction."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import stat
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from PIL import Image
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..asyncio_utils import run_sync_to_completion
from ..contracts import KNOWLEDGE_CONFLICT, KNOWLEDGE_INVALID_REQUEST, KNOWLEDGE_QUOTA_EXCEEDED, KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError
from ..extraction import Attachment, ExtractionLimits, ExtractionResult, LocalAttachment, ParseProfile, canonical_parse_fingerprint
from ..extraction.contracts import ExtractionError
from ..extraction.manifest import decode_manifest, encode_manifest
from ..persistence.models import KnowledgeAttachmentRow, KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeExtractionRow, KnowledgeTaskRow
from ..persistence.tasks import TASK_OPEN_STATUSES, lock_extraction_claim
from ..tasks.worker import KnowledgeProjectInactive, KnowledgeTaskClaim, ProjectActiveCheck
from .extraction_keys import attachment_storage_key, manifest_storage_key
from .minio_store import MinioObjectStore, ObjectMissingError
from .quota import KnowledgeStorageQuotaPort

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractionReservation:
    extraction_id: UUID
    document_id: UUID
    project_id: UUID
    base_id: UUID
    task_id: UUID
    attempt: int


@dataclass(frozen=True, slots=True)
class StoredExtraction:
    extraction_id: UUID
    document_id: UUID
    result: ExtractionResult


def _conflict() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_CONFLICT, "提取结果或任务身份已变更")


class _CorruptCache(KnowledgeError):
    """Verified invalid persisted bytes/closure, never a transport failure."""


def _corrupt() -> _CorruptCache:
    return _CorruptCache(KNOWLEDGE_STORAGE_UNAVAILABLE, "提取缓存不完整或校验失败")


async def _live_deadline(session: AsyncSession, task: KnowledgeTaskRow) -> None:
    # Every row/quota lock can wait; authority must still be live at commit.
    if task.lease_until is None or task.lease_until <= await session.scalar(select(func.clock_timestamp())):
        from ..contracts import KNOWLEDGE_TASK_FAILED

        raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务租约已失效")


def _claim(task: KnowledgeTaskRow, extraction: KnowledgeExtractionRow) -> KnowledgeTaskClaim:
    return KnowledgeTaskClaim(
        id=extraction.created_task_id,
        project_id=extraction.project_id,
        resource_id=extraction.knowledge_document_id,
        kind=task.kind,
        target_version=extraction.target_document_version,
        claim_token=extraction.created_claim_token,
        attempt_count=extraction.created_attempt,
        max_attempts=task.max_attempts,
    )


def _reservation(row: KnowledgeExtractionRow) -> ExtractionReservation:
    return ExtractionReservation(row.id, row.knowledge_document_id, row.project_id, row.knowledge_base_id, row.created_task_id, row.created_attempt)


def _validated_asset(asset: LocalAttachment, work_dir: Path) -> Path:
    """Verify parent-received bytes; child IPC metadata grants no trust."""
    try:
        relative = Path(asset.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe path")
        root = work_dir.resolve(strict=True)
        path = root
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("symlink")
        if not path.resolve(strict=True).is_relative_to(root) or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("not a regular file")
        limits = ExtractionLimits()
        info = asset.attachment
        if path.stat().st_size != info.size_bytes or info.size_bytes > limits.max_image_bytes:
            raise ValueError("size")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(65536):
                digest.update(block)
        if digest.hexdigest() != info.ref:
            raise ValueError("digest")
        with Image.open(path) as image:
            if Image.MIME.get(image.format) != info.media_type or image.size != (info.width, info.height) or info.width * info.height > limits.max_image_pixels or getattr(image, "n_frames", 1) != 1:
                raise ValueError("image metadata")
            image.verify()
        return path
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise KnowledgeError(KNOWLEDGE_INVALID_REQUEST, "附件字节或元数据校验失败") from exc


async def _drain(operation):
    """Await an already-started async adapter despite repeated cancellation.

    Return its physical outcome as well as cancellation so the owner can save
    successful PUT evidence before propagating cancellation.
    """
    worker = asyncio.create_task(operation)
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(worker)
            return result, cancelled
        except asyncio.CancelledError as exc:
            if worker.cancelled():
                raise
            cancelled = cancelled or exc


class ExtractionStore:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], object_store: MinioObjectStore, quota: KnowledgeStorageQuotaPort, project_active_check: ProjectActiveCheck, cache_enabled: bool):
        self._sessions = session_factory
        self._objects = object_store
        self._quota = quota
        self._project_active_check = project_active_check
        self._cache_enabled = cache_enabled
        # MinIO already allows one PUT. Serialize callbacks through settlement
        # too, so repeated occurrences cannot race one another's pending row.
        self._attachment_slot = asyncio.Lock()

    async def _guard(self, session: AsyncSession, claim: KnowledgeTaskClaim):
        if not await self._project_active_check(session, claim.project_id):
            raise KnowledgeProjectInactive()
        return await lock_extraction_claim(session, claim)

    async def begin(self, claim: KnowledgeTaskClaim, *, source_sha256: str, profile: ParseProfile) -> ExtractionReservation:
        fingerprint = canonical_parse_fingerprint(profile)
        async with self._sessions() as session, session.begin():
            task, document = await self._guard(session, claim)
            if document.source_sha256 != source_sha256:
                raise _conflict()
            row = await session.scalar(
                select(KnowledgeExtractionRow)
                .where(
                    KnowledgeExtractionRow.knowledge_document_id == document.id,
                    KnowledgeExtractionRow.created_task_id == task.id,
                    KnowledgeExtractionRow.created_attempt == claim.attempt_count,
                    KnowledgeExtractionRow.created_claim_token == claim.claim_token,
                )
                .with_for_update()
            )
            if row is None:
                row = KnowledgeExtractionRow(
                    id=uuid4(),
                    project_id=document.project_id,
                    knowledge_base_id=document.knowledge_base_id,
                    knowledge_document_id=document.id,
                    source_sha256=source_sha256,
                    parser_fingerprint=fingerprint,
                    normalization_version=profile.normalization_version,
                    state="staging",
                    created_task_id=task.id,
                    created_attempt=claim.attempt_count,
                    created_claim_token=claim.claim_token,
                    target_document_version=claim.target_version,
                )
                session.add(row)
                await session.flush()
            elif row.state != "staging" or row.created_claim_token != claim.claim_token or row.source_sha256 != source_sha256 or row.parser_fingerprint != fingerprint or row.target_document_version != document.version:
                raise _conflict()
            if task.extraction_id not in (None, row.id):
                raise _conflict()
            task.extraction_id = row.id
            return _reservation(row)

    async def abort(
        self,
        claim: KnowledgeTaskClaim,
        reservation: ExtractionReservation,
    ) -> None:
        """Atomically transfer one live incomplete generation to durable cleanup.

        The parser owner calls this only before ``complete``.  The exact
        creation claim must still own the Task pin; a stale attempt can never
        unpin or enqueue cleanup for a newer attempt or a published
        Extraction.
        """

        async with self._sessions() as session, session.begin():
            task, document = await self._guard(session, claim)
            row = await session.scalar(select(KnowledgeExtractionRow).where(KnowledgeExtractionRow.id == reservation.extraction_id).with_for_update().execution_options(populate_existing=True))
            if (
                row is None
                or _reservation(row) != reservation
                or row.created_claim_token != claim.claim_token
                or row.created_attempt != claim.attempt_count
                or row.target_document_version != claim.target_version
                or row.knowledge_document_id != claim.resource_id
                or row.source_sha256 != document.source_sha256
                or document.published_extraction_id == row.id
            ):
                raise _conflict()
            if row.state == "deleting":
                if task.extraction_id is not None:
                    raise _conflict()
                await self._admit_cleanup(session, row)
                return
            if row.state != "staging" or task.extraction_id != row.id:
                raise _conflict()
            task.extraction_id = None
            await session.flush()
            if await self._is_pinned(session, row.id):
                raise _conflict()
            await self._admit_cleanup(session, row)

    async def _lock_reservation(self, session: AsyncSession, reservation: ExtractionReservation):
        # These first reads discover immutable creation evidence only. The
        # same rows are re-read under locks after the Project/claim fence.
        row = await session.get(KnowledgeExtractionRow, reservation.extraction_id)
        task = await session.get(KnowledgeTaskRow, reservation.task_id)
        if row is None or task is None or _reservation(row) != reservation:
            raise _conflict()
        task, document = await self._guard(session, _claim(task, row))
        row = await session.scalar(select(KnowledgeExtractionRow).where(KnowledgeExtractionRow.id == reservation.extraction_id).with_for_update().execution_options(populate_existing=True))
        if row is None or _reservation(row) != reservation or row.state != "staging" or task.extraction_id != row.id or row.source_sha256 != document.source_sha256:
            raise _conflict()
        # Extraction lock waits also consume the lease.
        if task.lease_until <= await session.scalar(select(func.clock_timestamp())):
            from ..contracts import KNOWLEDGE_TASK_FAILED

            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务租约已失效")
        return task, document

    async def _register_attachment(self, session: AsyncSession, reservation: ExtractionReservation, info: Attachment):
        row = await session.scalar(select(KnowledgeAttachmentRow).where(KnowledgeAttachmentRow.extraction_id == reservation.extraction_id, KnowledgeAttachmentRow.sha256 == info.ref).with_for_update())
        key = attachment_storage_key(reservation.project_id, reservation.base_id, reservation.document_id, reservation.extraction_id, info.ref, info.media_type)
        if row is not None:
            if (row.storage_key, row.size_bytes, row.media_type, row.width, row.height) != (key, info.size_bytes, info.media_type, info.width, info.height) or row.state != "ready" or row.upload_state != "stored":
                # A persisted pending row is an uncertain earlier PUT. Never
                # overwrite it; recovery owns those facts.
                raise _conflict()
            return row
        count, total = (await session.execute(select(func.count(), func.coalesce(func.sum(KnowledgeAttachmentRow.size_bytes), 0)).where(KnowledgeAttachmentRow.extraction_id == reservation.extraction_id))).one()
        limits = ExtractionLimits()
        if count >= limits.max_images or total + info.size_bytes > limits.max_total_image_bytes:
            raise KnowledgeError(KNOWLEDGE_INVALID_REQUEST, "附件资源超过限制")
        row = KnowledgeAttachmentRow(
            id=uuid4(),
            extraction_id=reservation.extraction_id,
            project_id=reservation.project_id,
            knowledge_base_id=reservation.base_id,
            knowledge_document_id=reservation.document_id,
            sha256=info.ref,
            media_type=info.media_type,
            size_bytes=info.size_bytes,
            width=info.width,
            height=info.height,
            storage_key=key,
            state="staging",
        )
        session.add(row)
        await session.flush()
        return row

    async def persist_attachment(self, reservation: ExtractionReservation, asset: LocalAttachment, *, work_dir: Path) -> None:
        path = await run_sync_to_completion(_validated_asset, asset, work_dir)
        async with self._attachment_slot:
            async with self._sessions() as session, session.begin():
                await self._lock_reservation(session, reservation)
                row = await self._register_attachment(session, reservation, asset.attachment)
                if row.upload_state == "stored":
                    return
                await self._quota.reserve(session, project_id=row.project_id, object_id=row.id, size_bytes=row.size_bytes)
                key, object_id = row.storage_key, row.id
            stored = False
            try:
                _, cancelled = await _drain(self._objects.upload_from(key, path, media_type=asset.attachment.media_type))
                stored = True
                if cancelled is not None:
                    raise cancelled
                async with self._sessions() as session, session.begin():
                    await self._lock_reservation(session, reservation)
                    row = await session.get(KnowledgeAttachmentRow, object_id, with_for_update=True)
                    row.upload_state = "stored"
                    await self._quota.commit(session, object_id=object_id)
                    row.state = "ready"
            except BaseException:
                try:
                    await _drain(self._compensate(reservation, object_id, stored=stored))
                except (KnowledgeError, SQLAlchemyError):
                    # The committed pending row + reservation survive a DB
                    # outage and are the recovery authority. Never release.
                    logger.warning("knowledge attachment settlement deferred")
                raise

    async def _compensate(self, reservation: ExtractionReservation, object_id: UUID, *, stored: bool) -> None:
        async with self._sessions() as session, session.begin():
            # Inactive Projects still allow only retention of existing facts.
            await self._project_active_check(session, reservation.project_id)
            task = await session.get(KnowledgeTaskRow, reservation.task_id, with_for_update=True)
            await session.get(KnowledgeBaseRow, reservation.base_id, with_for_update=True)
            document = await session.get(KnowledgeDocumentRow, reservation.document_id, with_for_update=True)
            extraction = await session.get(KnowledgeExtractionRow, reservation.extraction_id, with_for_update=True)
            if document is None or extraction is None or _reservation(extraction) != reservation:
                return
            row = await session.get(KnowledgeAttachmentRow, object_id, with_for_update=True)
            if row is None or row.extraction_id != extraction.id:
                return
            row.state = "deleting"
            if stored:
                row.upload_state = "stored"
                await self._quota.commit(session, object_id=object_id)
            row.upload_state = "delete_pending"
            if task is not None and task.claim_token == extraction.created_claim_token and task.attempt_count == extraction.created_attempt and task.extraction_id == extraction.id:
                task.extraction_id = None
                await session.flush()
            if not await self._is_pinned(session, extraction.id) and document.published_extraction_id != extraction.id:
                await self._admit_cleanup(session, extraction)

    async def _inventory(self, session: AsyncSession, extraction: KnowledgeExtractionRow, result: ExtractionResult) -> list[KnowledgeAttachmentRow]:
        rows = list((await session.scalars(select(KnowledgeAttachmentRow).where(KnowledgeAttachmentRow.extraction_id == extraction.id).with_for_update())).all())
        expected = {asset.ref: asset for asset in result.attachments}
        if len(expected) != len(result.attachments) or len(rows) != len(expected):
            raise _corrupt()
        for row in rows:
            asset = expected.get(row.sha256)
            if asset is None or row.state != "ready" or row.upload_state != "stored" or row.quota_state != "committed":
                raise _corrupt()
            if (row.project_id, row.knowledge_base_id, row.knowledge_document_id) != (extraction.project_id, extraction.knowledge_base_id, extraction.knowledge_document_id):
                raise _corrupt()
            key = attachment_storage_key(row.project_id, row.knowledge_base_id, row.knowledge_document_id, extraction.id, asset.ref, asset.media_type)
            if (row.storage_key, row.media_type, row.size_bytes, row.width, row.height) != (key, asset.media_type, asset.size_bytes, asset.width, asset.height):
                raise _corrupt()
        return rows

    async def _check_candidate(self, claim: KnowledgeTaskClaim, extraction_id: UUID, *, state: str, result: ExtractionResult | None = None):
        async with self._sessions() as session, session.begin():
            task, document = await self._guard(session, claim)
            row = await session.get(KnowledgeExtractionRow, extraction_id, with_for_update=True)
            if (
                row is None
                or row.state != state
                or task.extraction_id != row.id
                or (row.project_id, row.knowledge_base_id, row.knowledge_document_id, row.source_sha256) != (document.project_id, document.knowledge_base_id, document.id, document.source_sha256)
            ):
                raise _conflict()
            if task.lease_until <= await session.scalar(select(func.clock_timestamp())):
                from ..contracts import KNOWLEDGE_TASK_FAILED

                raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务租约已失效")
            if result is not None:
                if (result.source_sha256, result.parse_fingerprint) != (row.source_sha256, row.parser_fingerprint):
                    raise _corrupt()
                attachments = await self._inventory(session, row, result)
                await _live_deadline(session, task)
                return attachments
            return row

    async def _read_manifest_bounded(self, candidate: KnowledgeExtractionRow, limits: ExtractionLimits) -> bytes:
        cap = min(limits.max_manifest_bytes, limits.max_work_dir_bytes, ExtractionLimits().max_manifest_bytes)
        if candidate.manifest_size_bytes > cap:
            raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "解析资源超限")
        with TemporaryDirectory(prefix="knowledge-manifest-") as directory:
            path = Path(directory) / "manifest.json"
            await self._objects.download_to(candidate.manifest_storage_key, path, max_bytes=cap)
            payload = await run_sync_to_completion(path.read_bytes)
        if len(payload) != candidate.manifest_size_bytes or hashlib.sha256(payload).hexdigest() != candidate.manifest_sha256:
            raise _corrupt()
        return payload

    async def _verified_result(self, claim: KnowledgeTaskClaim, candidate: KnowledgeExtractionRow, *, state: str, limits: ExtractionLimits) -> ExtractionResult:
        hard_limits = ExtractionLimits().model_dump()
        limits = ExtractionLimits(**{name: min(value, hard_limits[name]) for name, value in limits.model_dump().items()})
        await self._check_candidate(claim, candidate.id, state=state)
        payload = await self._read_manifest_bounded(candidate, limits)
        await self._check_candidate(claim, candidate.id, state=state)
        try:
            result = decode_manifest(payload, limits)
        except ExtractionError as exc:
            if exc.reason_code == "INVALID_MANIFEST":
                raise _corrupt() from exc
            raise
        attachments = await self._check_candidate(claim, candidate.id, state=state, result=result)
        for attachment in attachments:
            await self._check_candidate(claim, candidate.id, state=state)
            info = await self._objects.stat_object(attachment.storage_key)
            await self._check_candidate(claim, candidate.id, state=state)
            if info.size_bytes > min(limits.max_image_bytes, ExtractionLimits().max_image_bytes):
                raise KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "解析资源超限")
            if info.size_bytes != attachment.size_bytes:
                raise _corrupt()
            if not isinstance(info.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", info.sha256) is None:
                with TemporaryDirectory(prefix="knowledge-asset-check-") as directory:
                    path = Path(directory) / "asset"
                    await self._check_candidate(claim, candidate.id, state=state)
                    await self._objects.download_to(attachment.storage_key, path, max_bytes=min(limits.max_image_bytes, limits.max_work_dir_bytes, ExtractionLimits().max_image_bytes))
                    await self._check_candidate(claim, candidate.id, state=state)
                    payload = await run_sync_to_completion(path.read_bytes)
                if len(payload) != attachment.size_bytes or hashlib.sha256(payload).hexdigest() != attachment.sha256:
                    raise _corrupt()
            elif info.sha256 != attachment.sha256:
                raise _corrupt()
        await self._check_candidate(claim, candidate.id, state=state, result=result)
        return result

    async def complete(self, reservation: ExtractionReservation, result: ExtractionResult) -> StoredExtraction:
        # The parser workspace and its asset callbacks are already drained by
        # the owner. Only this private <=50 MiB manifest directory remains.
        payload = await run_sync_to_completion(encode_manifest, result)
        async with self._attachment_slot:
            async with self._sessions() as session, session.begin():
                task, _document = await self._lock_reservation(session, reservation)
                row = await session.get(KnowledgeExtractionRow, reservation.extraction_id)
                if (result.source_sha256, result.parse_fingerprint) != (row.source_sha256, row.parser_fingerprint) or row.manifest_storage_key is not None:
                    raise _conflict()
                await self._inventory(session, row, result)
                claim = _claim(task, row)
                row.manifest_storage_key = manifest_storage_key(row.project_id, row.knowledge_base_id, row.knowledge_document_id, row.id)
                row.manifest_sha256 = hashlib.sha256(payload).hexdigest()
                row.manifest_size_bytes = len(payload)
                await session.flush()
                await self._quota.reserve(session, project_id=row.project_id, object_id=row.id, size_bytes=len(payload))
                key = row.manifest_storage_key
                await _live_deadline(session, task)
            stored = False
            try:
                with TemporaryDirectory(prefix="knowledge-manifest-") as directory:
                    path = Path(directory) / "manifest.json"
                    await run_sync_to_completion(path.write_bytes, payload)
                    await self._check_candidate(claim, row.id, state="staging")
                    _, cancelled = await _drain(self._objects.upload_from(key, path, media_type="application/json"))
                    stored = True
                    if cancelled is not None:
                        raise cancelled
                verified = await self._verified_result(claim, row, state="staging", limits=ExtractionLimits())
                async with self._sessions() as session, session.begin():
                    _task, document = await self._lock_reservation(session, reservation)
                    row = await session.get(KnowledgeExtractionRow, reservation.extraction_id)
                    await self._inventory(session, row, verified)
                    row.manifest_upload_state = "stored"
                    await self._quota.commit(session, object_id=row.id)
                    row.state = "ready"
                    row.completed_at = await session.scalar(select(func.clock_timestamp()))
                    row.unpublished_expires_at = row.completed_at + timedelta(hours=24)
                    await session.flush()
                    # Pending/running pins may temporarily exceed capacity;
                    # the GC owner retries once those references settle.
                    older = list(
                        (
                            await session.scalars(
                                select(KnowledgeExtractionRow)
                                .where(KnowledgeExtractionRow.knowledge_document_id == document.id, KnowledgeExtractionRow.state == "ready", KnowledgeExtractionRow.id != row.id)
                                .order_by(KnowledgeExtractionRow.completed_at.desc(), KnowledgeExtractionRow.id.desc())
                                .with_for_update()
                            )
                        ).all()
                    )
                    for old in older:
                        if document.published_extraction_id != old.id and not await self._is_pinned(session, old.id):
                            await self._admit_cleanup(session, old)
                    await _live_deadline(session, _task)
                return StoredExtraction(row.id, reservation.document_id, verified)
            except BaseException:
                try:
                    await _drain(self._compensate_manifest(reservation, stored=stored))
                except (KnowledgeError, SQLAlchemyError):
                    logger.warning("knowledge manifest settlement deferred")
                raise

    async def _compensate_manifest(self, reservation: ExtractionReservation, *, stored: bool) -> None:
        async with self._sessions() as session, session.begin():
            await self._project_active_check(session, reservation.project_id)
            task = await session.get(KnowledgeTaskRow, reservation.task_id, with_for_update=True)
            await session.get(KnowledgeBaseRow, reservation.base_id, with_for_update=True)
            document = await session.get(KnowledgeDocumentRow, reservation.document_id, with_for_update=True)
            row = await session.get(KnowledgeExtractionRow, reservation.extraction_id, with_for_update=True)
            if document is None or row is None or _reservation(row) != reservation or row.state not in {"staging", "deleting"}:
                return
            if stored:
                row.manifest_upload_state = "stored"
                await self._quota.commit(session, object_id=row.id)
            row.manifest_upload_state = "delete_pending"
            if task is not None and task.claim_token == row.created_claim_token and task.attempt_count == row.created_attempt and task.extraction_id == row.id:
                task.extraction_id = None
                await session.flush()
            if not await self._is_pinned(session, row.id) and document.published_extraction_id != row.id:
                await self._admit_cleanup(session, row)

    async def find_ready(self, claim: KnowledgeTaskClaim, *, source_sha256: str, profile: ParseProfile, limits: ExtractionLimits) -> StoredExtraction | None:
        if not self._cache_enabled:
            return None
        fingerprint = canonical_parse_fingerprint(profile)
        async with self._sessions() as session, session.begin():
            task, document = await self._guard(session, claim)
            if document.source_sha256 != source_sha256:
                return None
            now = await session.scalar(select(func.clock_timestamp()))
            candidate = await session.scalar(
                select(KnowledgeExtractionRow)
                .where(
                    KnowledgeExtractionRow.project_id == document.project_id,
                    KnowledgeExtractionRow.knowledge_base_id == document.knowledge_base_id,
                    KnowledgeExtractionRow.knowledge_document_id == document.id,
                    KnowledgeExtractionRow.source_sha256 == source_sha256,
                    KnowledgeExtractionRow.parser_fingerprint == fingerprint,
                    KnowledgeExtractionRow.normalization_version == profile.normalization_version,
                    KnowledgeExtractionRow.state == "ready",
                    or_(KnowledgeExtractionRow.id == document.published_extraction_id, KnowledgeExtractionRow.unpublished_expires_at > now),
                )
                .order_by((KnowledgeExtractionRow.id == document.published_extraction_id).desc(), KnowledgeExtractionRow.completed_at.desc(), KnowledgeExtractionRow.id.desc())
                .limit(1)
                .with_for_update()
            )
            if candidate is None:
                return None
            if task.extraction_id not in (None, candidate.id):
                raise _conflict()
            if task.lease_until <= await session.scalar(select(func.clock_timestamp())):
                from ..contracts import KNOWLEDGE_TASK_FAILED

                raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "Knowledge 任务租约已失效")
            task.extraction_id = candidate.id
        try:
            result = await self._verified_result(claim, candidate, state="ready", limits=limits)
        except (ObjectMissingError, _CorruptCache):
            # Only verified byte/closure failures are a cache miss. Re-check
            # authority even on this branch; never hide revoked/stale work.
            async with self._sessions() as session, session.begin():
                task, document = await self._guard(session, claim)
                row = await session.get(KnowledgeExtractionRow, candidate.id, with_for_update=True)
                if row is None or row.state != "ready" or task.extraction_id != row.id:
                    raise _conflict()
                task.extraction_id = None
                await session.flush()
                if document.published_extraction_id != row.id and not await self._is_pinned(session, row.id):
                    await self._admit_cleanup(session, row)
                await _live_deadline(session, task)
            return None
        return StoredExtraction(candidate.id, candidate.knowledge_document_id, result)

    async def _is_pinned(self, session: AsyncSession, extraction_id: UUID) -> bool:
        return await session.scalar(select(KnowledgeTaskRow.id).where(KnowledgeTaskRow.extraction_id == extraction_id, KnowledgeTaskRow.status.in_(TASK_OPEN_STATUSES)).limit(1)) is not None

    async def _admit_cleanup(self, session: AsyncSession, extraction: KnowledgeExtractionRow) -> None:
        extraction.state = "deleting"
        await session.execute(
            pg_insert(KnowledgeTaskRow)
            .values(
                id=uuid4(),
                project_id=extraction.project_id,
                resource_id=extraction.id,
                kind="delete_extraction",
                target_version=None,
                storage_key=None,
                status="queued",
            )
            .on_conflict_do_nothing(
                index_elements=[KnowledgeTaskRow.resource_id],
                index_where=text("kind = 'delete_extraction' AND status IN ('queued', 'running', 'retry_wait')"),
            )
        )

    async def enqueue_cleanup(self, extraction_id: UUID, *, project_id: UUID) -> None:
        async with self._sessions() as session, session.begin():
            await self._project_active_check(session, project_id)
            row = await session.get(KnowledgeExtractionRow, extraction_id)
            if row is None or row.project_id != project_id:
                raise _conflict()
            await session.get(KnowledgeBaseRow, row.knowledge_base_id, with_for_update=True)
            document = await session.get(KnowledgeDocumentRow, row.knowledge_document_id, with_for_update=True)
            row = await session.scalar(select(KnowledgeExtractionRow).where(KnowledgeExtractionRow.id == extraction_id).with_for_update().execution_options(populate_existing=True))
            if row is None or document is None or document.published_extraction_id == extraction_id or await self._is_pinned(session, extraction_id):
                raise _conflict()
            await self._admit_cleanup(session, row)
