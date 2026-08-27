from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from support.private_thread_seed import (
    PrivateThreadSeed,
    seed_private_thread_database,
)
from support.run_closure import add_sealed_test_run

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.errors import PrivateWorkNotFound, PrivateWorkUnavailable
from app.private_work.file_service import PrivateFileService
from app.private_work.sandbox_files import (
    RUN_CURRENT_UPLOAD_SNAPSHOT_KWARG,
    CurrentUploadSnapshotEntry,
    CurrentUploadSnapshotInvalid,
    admit_current_upload_snapshot,
    persisted_current_upload_snapshot,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.models import QuotaSourceRef
from app.quotas.service import QuotaService
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.private_work.model import (
    PrivateArtifactRow,
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


def _quota_enforcer(seed: PrivateThreadSeed) -> ProjectQuotaEnforcer:
    quota_service = QuotaService(
        seed.factory,
        QuotaConfig(),
        source_ref_hasher=_quota_source_ref,
    )
    return ProjectQuotaEnforcer(quota_service)


def _file_service(seed: PrivateThreadSeed) -> PrivateFileService:
    return PrivateFileService(
        seed.factory,
        quota=_quota_enforcer(seed),
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
            await add_sealed_test_run(
                session,
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
                ),
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
async def test_thread_delete_retains_upload_artifact_and_storage_quota(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    service = _file_service(seed)
    thread_id = str(uuid.uuid4())
    content = b"retained thread upload bytes"
    try:
        await _add_thread(seed, thread_id)
        upload = await _upload(
            seed,
            service,
            thread_id=thread_id,
            content=content,
            name="retained.txt",
        )
        run_id = str(uuid.uuid4())
        async with seed.factory() as session, session.begin():
            await add_sealed_test_run(
                session,
                RunRow(
                    run_id=run_id,
                    thread_id=thread_id,
                    assistant_id=str(seed.project_agent_id),
                    owner_user_id=str(seed.owner_a.user_id),
                    status="success",
                    metadata_json={},
                    kwargs_json={},
                    project_id=seed.owner_a.project_id,
                ),
            )
            artifact = PrivateArtifactRow(
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                thread_id=thread_id,
                run_id=run_id,
                file_id=upload.id,
                display_name="retained.txt",
                media_type="text/plain",
                artifact_metadata={"logical_path": upload.logical_path},
            )
            session.add(artifact)
            await session.flush()
            artifact_id = artifact.id

        before = await _stored_state(seed, file_id=upload.id)
        assert before.file is not None
        assert before.chunks[0][2] == content

        await (
            ProjectScopedCheckpointer(
                InMemorySaver(),
                seed.factory,
                quota=_quota_enforcer(seed),
            )
            .for_context(seed.owner_a)
            .adelete_thread(
                thread_id,
                expected_version=1,
            )
        )

        assert await _stored_state(seed, file_id=upload.id) == before
        assert (
            await service.get_ready(
                seed.owner_a,
                thread_id=thread_id,
                file_id=upload.id,
            )
            is None
        )
        with pytest.raises(PrivateWorkNotFound):
            await service.list_ready(
                seed.owner_a,
                thread_id=thread_id,
            )
        async with seed.factory() as session:
            retained_artifact = await session.get(
                PrivateArtifactRow,
                artifact_id,
            )
            tombstone = await session.get(ThreadMetaRow, thread_id)
            assert retained_artifact is not None
            assert retained_artifact.deleted_at is None
            assert tombstone is not None
            assert tombstone.deleted_at is not None
            assert tombstone.checkpoint_delete_status == "not_requested"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_failed_create_compensation_releases_concurrent_upload_quota(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    service = _file_service(seed)
    thread_id = str(uuid.uuid4())
    content = b"failed create concurrent upload"
    try:
        await _add_thread(seed, thread_id)
        upload = await _upload(
            seed,
            service,
            thread_id=thread_id,
            content=content,
            name="compensated.txt",
        )
        before = await _stored_state(seed, file_id=upload.id)
        assert before.counter is not None and before.counter[:2] == (
            0,
            len(content),
        )
        async with seed.factory() as session:
            active_thread = await session.get(ThreadMetaRow, thread_id)
            assert active_thread is not None
            expected_created_at = active_thread.created_at

        compensator = ProjectScopedCheckpointer(
            InMemorySaver(),
            seed.factory,
            quota=_quota_enforcer(seed),
        ).for_context(seed.owner_a)
        tombstone = await compensator.atombstone_compensated_create(
            thread_id,
            expected_version=1,
            expected_created_at=expected_created_at,
        )
        assert tombstone.deleted_at is not None
        cleaned = await compensator.acleanup_compensated_create(
            thread_id,
            expected_created_at=expected_created_at,
            expected_deleted_at=tombstone.deleted_at,
        )
        assert cleaned
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).purge_compensated_create(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_created_at=expected_created_at,
                expected_deleted_at=tombstone.deleted_at,
            )

        after = await _stored_state(seed, file_id=upload.id)
        assert after.file is None
        assert after.chunks == ()
        assert after.counter is not None and after.counter[:2] == (0, 0)
        assert sorted((row[1], row[2]) for row in after.ledger) == [
            (-len(content), "release"),
            (len(content), "reserve"),
        ]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stale_branch_rollback_cannot_delete_recreated_thread_upload(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    service = _file_service(seed)
    thread_id = str(uuid.uuid4())
    content = b"replacement generation upload"
    try:
        await _add_thread(seed, thread_id)
        async with seed.factory() as session, session.begin():
            repository = PrivateThreadRepository(session)
            tombstone = await repository.mark_deleted(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_version=1,
            )
            assert tombstone.deleted_at is not None
            await repository.request_checkpoint_delete_for_compensation(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_created_at=tombstone.created_at,
                expected_deleted_at=tombstone.deleted_at,
            )
            assert await repository.set_checkpoint_delete_status(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                status="complete",
            )
            await repository.purge_compensated_create(
                scope=seed.owner_a.resource_scope,
                thread_id=thread_id,
                expected_created_at=tombstone.created_at,
                expected_deleted_at=tombstone.deleted_at,
            )

        await _add_thread(seed, thread_id)
        upload = await _upload(
            seed,
            service,
            thread_id=thread_id,
            content=content,
            name="replacement.txt",
        )
        before = await _stored_state(seed, file_id=upload.id)

        with pytest.raises(PrivateWorkUnavailable):
            await service.rollback_branch_authority(
                seed.owner_a.resource_scope,
                "source-thread",
                thread_id,
                expected_target_created_at=tombstone.created_at,
                expected_target_deleted_at=tombstone.deleted_at,
            )

        assert await _stored_state(seed, file_id=upload.id) == before
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
