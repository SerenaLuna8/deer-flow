from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from support.private_thread_seed import (
    PrivateThreadSeed,
    seed_private_thread_database,
)

from app.private_work.file_service import PrivateFileService
from app.private_work.sandbox_files import (
    RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG,
    CurrentUploadSnapshotEntry,
    CurrentUploadSnapshotInvalid,
    admit_current_upload_snapshot,
    persisted_current_upload_snapshot,
)
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.models import QuotaSourceRef
from app.quotas.service import QuotaService
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.private_work.model import (
    PrivateFileChunkRow,
    PrivateFileRow,
)
from deerflow.persistence.quotas.model import (
    ProjectUsageCounterRow,
    ProjectUsageLedgerRow,
)
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow


@dataclass(frozen=True)
class _StoredUploadState:
    file: tuple[object, ...] | None
    chunks: tuple[tuple[object, ...], ...]
    counter: tuple[int, int, int] | None
    ledger: tuple[tuple[object, ...], ...]


def _quota_source_ref(payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(
        key_id="upload-discard-test-v1",
        hmac_hex=hashlib.sha256(payload).hexdigest(),
    )


def _file_service(seed: PrivateThreadSeed) -> PrivateFileService:
    quota_service = QuotaService(
        seed.factory,
        QuotaConfig(),
        source_ref_hasher=_quota_source_ref,
    )
    return PrivateFileService(
        seed.factory,
        quota=ProjectQuotaEnforcer(quota_service),
    )


async def _chunks(content: bytes) -> AsyncIterator[bytes]:
    yield content


async def _add_thread(seed: PrivateThreadSeed, thread_id: str) -> None:
    async with seed.factory() as session, session.begin():
        session.add(
            ThreadMetaRow(
                thread_id=thread_id,
                assistant_id=str(seed.project_agent_id),
                owner_user_id=str(seed.owner_a.user_id),
                display_name="Conditional upload discard",
                status="idle",
                metadata_json={},
                project_id=seed.owner_a.project_id,
                agent_asset_id=seed.project_agent_id,
                agent_scope="project",
            )
        )


async def _upload(
    seed: PrivateThreadSeed,
    service: PrivateFileService,
    *,
    thread_id: str,
    content: bytes,
    name: str,
):
    return await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path=f"uploads/{name}",
        media_type="text/plain",
        chunks=_chunks(content),
    )


async def _stored_state(
    seed: PrivateThreadSeed,
    *,
    file_id: uuid.UUID,
) -> _StoredUploadState:
    async with seed.factory() as session:
        file = (
            await session.execute(
                select(
                    PrivateFileRow.id,
                    PrivateFileRow.status,
                    PrivateFileRow.kind,
                    PrivateFileRow.logical_path,
                    PrivateFileRow.size,
                    PrivateFileRow.sha256,
                    PrivateFileRow.version,
                ).where(PrivateFileRow.id == file_id)
            )
        ).one_or_none()
        chunks = tuple(
            (
                await session.execute(
                    select(
                        PrivateFileChunkRow.file_id,
                        PrivateFileChunkRow.chunk_index,
                        PrivateFileChunkRow.content,
                        PrivateFileChunkRow.size,
                        PrivateFileChunkRow.sha256,
                    )
                    .where(PrivateFileChunkRow.file_id == file_id)
                    .order_by(PrivateFileChunkRow.chunk_index)
                )
            ).all()
        )
        counter = (
            await session.execute(
                select(
                    ProjectUsageCounterRow.used,
                    ProjectUsageCounterRow.reserved,
                    ProjectUsageCounterRow.version,
                ).where(
                    ProjectUsageCounterRow.project_id == seed.owner_a.project_id,
                    ProjectUsageCounterRow.dimension == "storage_bytes",
                    ProjectUsageCounterRow.bucket == "lifetime",
                )
            )
        ).one_or_none()
        ledger = tuple(
            (
                await session.execute(
                    select(
                        ProjectUsageLedgerRow.id,
                        ProjectUsageLedgerRow.delta,
                        ProjectUsageLedgerRow.source_kind,
                        ProjectUsageLedgerRow.source_ref_key_id,
                        ProjectUsageLedgerRow.source_ref_hmac,
                        ProjectUsageLedgerRow.idempotency_key,
                    )
                    .where(
                        ProjectUsageLedgerRow.project_id == seed.owner_a.project_id,
                        ProjectUsageLedgerRow.dimension == "storage_bytes",
                    )
                    .order_by(ProjectUsageLedgerRow.occurred_at, ProjectUsageLedgerRow.id)
                )
            ).all()
        )
    return _StoredUploadState(
        file=None if file is None else tuple(file),
        chunks=tuple(tuple(row) for row in chunks),
        counter=None if counter is None else tuple(counter),
        ledger=tuple(tuple(row) for row in ledger),
    )


def _snapshot_entry(file) -> CurrentUploadSnapshotEntry:
    return CurrentUploadSnapshotEntry(
        file_id=str(file.id),
        logical_path=file.logical_path,
        media_type=file.media_type,
        size=file.size,
        sha256=file.sha256,
        version=file.version,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conditional_discard_retains_run_snapshot_upload_and_quota(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    service = _file_service(seed)
    thread_id = str(uuid.uuid4())
    content = b"frozen upload bytes"
    try:
        await _add_thread(seed, thread_id)
        upload = await _upload(
            seed,
            service,
            thread_id=thread_id,
            content=content,
            name="frozen.txt",
        )
        async with seed.factory() as session, session.begin():
            session.add(
                RunRow(
                    run_id=str(uuid.uuid4()),
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=str(seed.owner_a.user_id),
                    status="success",
                    metadata_json={},
                    kwargs_json={
                        "input": {
                            "messages": [
                                {
                                    "type": "human",
                                    "additional_kwargs": {"files": [{"file_id": str(upload.id)}]},
                                }
                            ]
                        },
                        RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG: persisted_current_upload_snapshot((_snapshot_entry(upload),)),
                    },
                    project_id=seed.owner_a.project_id,
                )
            )

        before = await _stored_state(seed, file_id=upload.id)
        assert before.file is not None
        assert before.chunks[0][2] == content
        assert before.counter is not None and before.counter[:2] == (
            0,
            len(content),
        )
        assert [(row[1], row[2]) for row in before.ledger] == [(len(content), "reserve")]

        deleted = await service.delete_ready(
            seed.owner_a,
            thread_id=thread_id,
            file_id=upload.id,
            only_if_unreferenced=True,
        )

        assert deleted is None
        assert await _stored_state(seed, file_id=upload.id) == before
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conditional_discard_deletes_unreferenced_upload_and_releases_quota(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    service = _file_service(seed)
    thread_id = str(uuid.uuid4())
    content = b"unreferenced upload bytes"
    try:
        await _add_thread(seed, thread_id)
        upload = await _upload(
            seed,
            service,
            thread_id=thread_id,
            content=content,
            name="draft.txt",
        )

        deleted = await service.delete_ready(
            seed.owner_a,
            thread_id=thread_id,
            file_id=upload.id,
            only_if_unreferenced=True,
        )

        assert deleted is not None and deleted.id == upload.id
        after = await _stored_state(seed, file_id=upload.id)
        assert after.file is None
        assert after.chunks == ()
        assert after.counter is not None and after.counter[:2] == (0, 0)
        assert sorted((row[1], row[2]) for row in after.ledger) == [
            (-len(content), "release"),
            (len(content), "reserve"),
        ]
        assert sum(row[1] for row in after.ledger) == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_delete_first_makes_strict_upload_admission_reject_whole_request(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    service = _file_service(seed)
    thread_id = str(uuid.uuid4())
    try:
        await _add_thread(seed, thread_id)
        upload = await _upload(
            seed,
            service,
            thread_id=thread_id,
            content=b"delete wins",
            name="raced.txt",
        )
        assert (
            await service.delete_ready(
                seed.owner_a,
                thread_id=thread_id,
                file_id=upload.id,
                only_if_unreferenced=True,
            )
            is not None
        )

        run_kwargs = {
            "input": {
                "messages": [
                    {
                        "type": "human",
                        "additional_kwargs": {"files": [{"file_id": str(upload.id)}]},
                    }
                ]
            }
        }
        async with seed.factory() as session, session.begin():
            with pytest.raises(CurrentUploadSnapshotInvalid):
                await admit_current_upload_snapshot(
                    session,
                    scope=seed.owner_a_scope,
                    thread_id=thread_id,
                    run_kwargs=run_kwargs,
                )
    finally:
        await seed.engine.dispose()
