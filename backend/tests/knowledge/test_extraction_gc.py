"""Durable Extraction cleanup and database-clock GC integration gates."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from actweave_knowledge import (
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeError,
    KnowledgeSettings,
)
from actweave_knowledge.persistence.models import KnowledgeDocumentRow, KnowledgeExtractionRow, KnowledgeTaskRow
from actweave_knowledge.persistence.tasks import claim_next_task, settle_task_success
from actweave_knowledge.tasks.worker import KnowledgeTaskClaim
from extraction_test_helpers import extraction_harness, make_extraction_result, write_test_asset
from parsing_test_helpers import make_parse_profile
from sqlalchemy import func, select, text

from app.knowledge.composition import is_knowledge_project_active
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.quotas.model import ProjectUsageCounterRow, ProjectUsageLedgerRow


async def _claim_ingest(harness) -> KnowledgeTaskClaim:
    async with harness.session_factory() as session, session.begin():
        session.add(
            KnowledgeTaskRow(
                id=uuid.uuid4(),
                project_id=harness.project_id,
                resource_id=harness.document_id,
                kind="ingest_document",
                target_version=1,
            )
        )
        await session.flush()
        task = await claim_next_task(session, lease_seconds=600)
        assert task is not None and task.claim_token is not None
        return KnowledgeTaskClaim(
            id=task.id,
            project_id=task.project_id,
            resource_id=task.resource_id,
            kind=task.kind,
            target_version=task.target_version,
            claim_token=task.claim_token,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["bucket", "delete", "get"])
async def test_delete_failure_keeps_manifest_row_and_charge(
    postgres_database_url: str,
    failure: str,
) -> None:
    from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        stored = await harness.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
        claim = await harness.claim_cleanup(stored.extraction_id)
        harness.object_store.fail_next(failure)
        handler = KnowledgeExtractionDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        with pytest.raises(KnowledgeError):
            await handler(claim)

        row = (await harness.read_rows())["extractions"][0]
        assert row.state == "deleting"
        assert row.manifest_quota_state == "committed"
        assert row.delete_error
        assert "/" not in row.delete_error


@pytest.mark.asyncio
async def test_delete_removes_registered_objects_before_releasing_rows_and_quota(
    postgres_database_url: str,
    tmp_path,
) -> None:
    from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler

    async with extraction_harness(postgres_database_url) as harness:
        asset = write_test_asset(tmp_path)
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        await harness.store.persist_attachment(reservation, asset, work_dir=tmp_path)
        stored = await harness.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source, attachments=(asset,)),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
        claim = await harness.claim_cleanup(stored.extraction_id)
        handler = KnowledgeExtractionDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        await handler(claim)

        rows = await harness.read_rows()
        assert rows["extractions"] == []
        assert rows["attachments"] == []
        assert set(harness.object_store.objects) == {rows["documents"][0].storage_key}
        async with harness.session_factory() as session:
            counter = await session.scalar(
                select(ProjectUsageCounterRow).where(
                    ProjectUsageCounterRow.project_id == harness.project_id,
                    ProjectUsageCounterRow.dimension == "storage_bytes",
                )
            )
            assert counter is not None
            assert (counter.used, counter.reserved) == (8, 0)


@pytest.mark.asyncio
async def test_attachment_delete_failure_records_safe_retry_fact(
    postgres_database_url: str,
    tmp_path,
) -> None:
    from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler

    async with extraction_harness(postgres_database_url) as harness:
        asset = write_test_asset(tmp_path)
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        await harness.store.persist_attachment(reservation, asset, work_dir=tmp_path)
        stored = await harness.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source, attachments=(asset,)),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
        claim = await harness.claim_cleanup(stored.extraction_id)
        harness.object_store.fail_next("get")
        handler = KnowledgeExtractionDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        with pytest.raises(KnowledgeError):
            await handler(claim)

        rows = await harness.read_rows()
        assert rows["extractions"][0].delete_error
        attachment = rows["attachments"][0]
        assert attachment.upload_state == "delete_pending"
        assert attachment.quota_state == "committed"
        assert attachment.delete_error
        assert "/" not in attachment.delete_error


@pytest.mark.asyncio
async def test_gc_uses_database_expiry_and_never_selects_published_or_live_pinned(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.storage.extraction_gc import enqueue_extraction_gc

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        stored = await harness.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source),
        )
        async with harness.session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_extractions SET unpublished_expires_at = clock_timestamp() - interval '1 second' WHERE id = :id"),
                {"id": stored.extraction_id},
            )
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 0
            )

        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
            document = await session.get(KnowledgeDocumentRow, harness.document_id, with_for_update=True)
            assert document is not None
            document.published_extraction_id = stored.extraction_id
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 0
            )

        async with harness.session_factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, harness.document_id, with_for_update=True)
            assert document is not None
            document.published_extraction_id = None
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 1
            )

        rows = await harness.read_rows()
        assert rows["extractions"][0].state == "deleting"
        cleanup = [task for task in rows["tasks"] if task.kind == "delete_extraction"]
        assert len(cleanup) == 1
        assert cleanup[0].resource_id == stored.extraction_id


@pytest.mark.asyncio
async def test_gc_waits_settlement_grace_then_recovers_lost_staging_creator(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.storage.extraction_gc import enqueue_extraction_gc

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        async with harness.session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_tasks SET extraction_id = NULL WHERE id = :task_id"),
                {"task_id": harness.claim.id},
            )
            await session.execute(
                text("UPDATE knowledge_extractions SET created_at = clock_timestamp() - interval '1 day 1 second' WHERE id = :id"),
                {"id": reservation.extraction_id},
            )
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 0
            )
        async with harness.session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_tasks SET lease_until = clock_timestamp() - interval '1 second' WHERE id = :task_id"),
                {"task_id": harness.claim.id},
            )
            await session.execute(
                text("UPDATE knowledge_extractions SET created_at = clock_timestamp() - interval '23 hours' WHERE id = :id"),
                {"id": reservation.extraction_id},
            )
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 0
            )
        async with harness.session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_extractions SET created_at = clock_timestamp() - interval '1 day 1 second' WHERE id = :id"),
                {"id": reservation.extraction_id},
            )
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 1
            )
        rows = await harness.read_rows()
        assert rows["extractions"][0].state == "deleting"
        assert len([task for task in rows["tasks"] if task.kind == "delete_extraction"]) == 1

        async with harness.session_factory() as session, session.begin():
            creator = await session.get(KnowledgeTaskRow, harness.claim.id, with_for_update=True)
            assert creator is not None and creator.status == "retry_wait"
            creator.available_at = await session.scalar(select(func.clock_timestamp())) + timedelta(hours=1)
            ledger_before = await session.scalar(select(func.count()).select_from(ProjectUsageLedgerRow).where(ProjectUsageLedgerRow.project_id == harness.project_id))
        claim = await harness.claim_cleanup(reservation.extraction_id)
        calls_before = list(harness.object_store.calls)
        from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler

        handler = KnowledgeExtractionDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        await handler(claim)
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, claim.id, claim.claim_token)
            ledger_after = await session.scalar(select(func.count()).select_from(ProjectUsageLedgerRow).where(ProjectUsageLedgerRow.project_id == harness.project_id))

        rows = await harness.read_rows()
        assert rows["extractions"] == []
        cleanup = next(task for task in rows["tasks"] if task.id == claim.id)
        assert cleanup.status == "succeeded"
        assert harness.object_store.calls == calls_before
        assert ledger_after == ledger_before


@pytest.mark.asyncio
async def test_late_manifest_put_restores_deleting_fact_and_cleanup(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.storage.extraction_gc import enqueue_extraction_gc

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        barrier = harness.object_store.pause("put")
        completing = asyncio.create_task(
            harness.store.complete(
                reservation,
                make_extraction_result(profile, source_sha256=source),
            )
        )
        await barrier.entered.wait()
        async with harness.session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_tasks SET lease_until = clock_timestamp() - interval '1 second' WHERE id = :task_id"),
                {"task_id": harness.claim.id},
            )
            await session.execute(
                text("UPDATE knowledge_extractions SET created_at = clock_timestamp() - interval '1 day 1 second' WHERE id = :id"),
                {"id": reservation.extraction_id},
            )
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 1
            )
        barrier.released.set()
        with pytest.raises(KnowledgeError):
            await completing

        rows = await harness.read_rows()
        extraction = rows["extractions"][0]
        assert extraction.state == "deleting"
        assert extraction.manifest_upload_state == "delete_pending"
        assert extraction.manifest_quota_state == "committed"
        assert extraction.manifest_storage_key in harness.object_store.objects
        assert len([task for task in rows["tasks"] if task.kind == "delete_extraction"]) == 1


@pytest.mark.asyncio
async def test_registered_manifest_without_quota_fact_fails_closed_before_object_delete(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.storage.extraction_gc import enqueue_extraction_gc
    from actweave_knowledge.storage.extraction_keys import manifest_storage_key
    from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        key = manifest_storage_key(
            harness.project_id,
            harness.base_id,
            harness.document_id,
            reservation.extraction_id,
        )
        payload = b"x"
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
            row = await session.get(KnowledgeExtractionRow, reservation.extraction_id, with_for_update=True)
            assert row is not None
            row.manifest_storage_key = key
            row.manifest_sha256 = hashlib.sha256(payload).hexdigest()
            row.manifest_size_bytes = len(payload)
            row.manifest_upload_state = "pending"
            row.manifest_quota_state = "unreserved"
            row.created_at = await session.scalar(select(func.clock_timestamp())) - timedelta(days=1, seconds=1)
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 1
            )
        harness.object_store.objects[key] = payload
        claim = await harness.claim_cleanup(reservation.extraction_id)
        calls_before = list(harness.object_store.calls)
        handler = KnowledgeExtractionDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        with pytest.raises(KnowledgeError):
            await handler(claim)

        rows = await harness.read_rows()
        assert len(rows["extractions"]) == 1
        assert rows["extractions"][0].manifest_quota_state == "unreserved"
        assert rows["extractions"][0].delete_error
        assert harness.object_store.objects[key] == payload
        assert harness.object_store.calls == calls_before


@pytest.mark.asyncio
async def test_late_attachment_put_preserves_stored_fact_for_durable_cleanup(
    postgres_database_url: str,
    tmp_path,
) -> None:
    from actweave_knowledge.storage.extraction_gc import enqueue_extraction_gc

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        asset = write_test_asset(tmp_path)
        barrier = harness.object_store.pause("put")
        persisting = asyncio.create_task(harness.store.persist_attachment(reservation, asset, work_dir=tmp_path))
        await barrier.entered.wait()
        async with harness.session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_tasks SET lease_until = clock_timestamp() - interval '1 second' WHERE id = :task_id"),
                {"task_id": harness.claim.id},
            )
            await session.execute(
                text("UPDATE knowledge_extractions SET created_at = clock_timestamp() - interval '1 day 1 second' WHERE id = :id"),
                {"id": reservation.extraction_id},
            )
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 1
            )
        barrier.released.set()
        with pytest.raises(KnowledgeError):
            await persisting

        rows = await harness.read_rows()
        attachment = rows["attachments"][0]
        assert attachment.state == "deleting"
        assert attachment.upload_state == "delete_pending"
        assert attachment.quota_state == "committed"
        assert attachment.storage_key in harness.object_store.objects
        assert len([task for task in rows["tasks"] if task.kind == "delete_extraction"]) == 1


@pytest.mark.asyncio
async def test_gc_reclaims_older_complete_cache_and_keeps_latest_unexpired(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.storage.extraction_gc import enqueue_extraction_gc

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        first_reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        first = await harness.store.complete(
            first_reservation,
            make_extraction_result(profile, source_sha256=source),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)

        second_claim = await _claim_ingest(harness)
        second_reservation = await harness.store.begin(second_claim, source_sha256=source, profile=profile)
        second = await harness.store.complete(
            second_reservation,
            make_extraction_result(profile, source_sha256=source),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, second_claim.id, second_claim.claim_token)
            await session.execute(
                text("DELETE FROM knowledge_tasks WHERE kind = 'delete_extraction' AND resource_id = :id"),
                {"id": first.extraction_id},
            )
            await session.execute(
                text("UPDATE knowledge_extractions SET state = 'ready', unpublished_expires_at = clock_timestamp() + interval '24 hours' WHERE id IN (:first_id, :second_id)"),
                {"first_id": first.extraction_id, "second_id": second.extraction_id},
            )
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 1
            )
        rows = await harness.read_rows()
        states = {row.id: row.state for row in rows["extractions"]}
        assert states == {first.extraction_id: "deleting", second.extraction_id: "ready"}


@pytest.mark.asyncio
async def test_delete_confirmation_uncertainty_retries_without_double_release(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        stored = await harness.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
        claim = await harness.claim_cleanup(stored.extraction_id)
        handler = KnowledgeExtractionDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )
        harness.object_store.fail_next("get")

        with pytest.raises(KnowledgeError):
            await handler(claim)
        failed = (await harness.read_rows())["extractions"][0]
        assert failed.manifest_quota_state == "committed"
        assert failed.delete_error

        await handler(claim)
        rows = await harness.read_rows()
        assert rows["extractions"] == []
        async with harness.session_factory() as session:
            counter = await session.scalar(
                select(ProjectUsageCounterRow).where(
                    ProjectUsageCounterRow.project_id == harness.project_id,
                    ProjectUsageCounterRow.dimension == "storage_bytes",
                )
            )
            assert counter is not None
            assert (counter.used, counter.reserved) == (8, 0)


@pytest.mark.asyncio
async def test_release_transaction_failure_retries_without_double_debit(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        stored = await harness.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
        claim = await harness.claim_cleanup(stored.extraction_id)

        class FailAfterFirstRelease:
            def __init__(self) -> None:
                self.failed = False

            async def release(self, session, *, object_id):
                await harness.quota.release(session, object_id=object_id)
                if not self.failed:
                    self.failed = True
                    raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "quota settlement fixture")

        quota = FailAfterFirstRelease()
        handler = KnowledgeExtractionDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=quota,
            project_active_check=is_knowledge_project_active,
        )
        with pytest.raises(KnowledgeError):
            await handler(claim)
        failed = (await harness.read_rows())["extractions"][0]
        assert failed.manifest_quota_state == "committed"

        await handler(claim)
        async with harness.session_factory() as session:
            counter = await session.scalar(
                select(ProjectUsageCounterRow).where(
                    ProjectUsageCounterRow.project_id == harness.project_id,
                    ProjectUsageCounterRow.dimension == "storage_bytes",
                )
            )
            assert counter is not None
            assert (counter.used, counter.reserved) == (8, 0)
        assert (await harness.read_rows())["extractions"] == []


@pytest.mark.asyncio
async def test_internal_published_delete_requires_pointer_withdrawal(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.tasks.extraction_deletion import delete_registered_extraction

    async with extraction_harness(postgres_database_url) as harness:
        stored = await harness.published_result()
        manifest_key = (await harness.read_rows())["extractions"][0].manifest_storage_key
        async with harness.session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_extractions SET state = 'deleting' WHERE id = :id"),
                {"id": stored.extraction_id},
            )

        with pytest.raises(KnowledgeError):
            await delete_registered_extraction(
                session_factory=harness.session_factory,
                object_store=harness.object_store,
                quota=harness.quota,
                project_active_check=is_knowledge_project_active,
                project_id=harness.project_id,
                extraction_id=stored.extraction_id,
                allow_published=True,
            )

        rows = await harness.read_rows()
        assert rows["documents"][0].published_extraction_id == stored.extraction_id
        assert len(rows["extractions"]) == 1
        assert manifest_key in harness.object_store.objects


@pytest.mark.asyncio
async def test_concurrent_gc_admits_one_open_task_and_inactive_project_defers(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.storage.extraction_gc import enqueue_extraction_gc

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        stored = await harness.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
            await session.execute(
                text("UPDATE knowledge_extractions SET unpublished_expires_at = clock_timestamp() - interval '1 second' WHERE id = :id"),
                {"id": stored.extraction_id},
            )
            project = await session.get(ProjectRow, harness.project_id, with_for_update=True)
            assert project is not None
            project.status = "pending_deletion"
            assert (
                await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )
                == 0
            )
            project.status = "active"

        async def sweep() -> int:
            async with harness.session_factory() as session, session.begin():
                return await enqueue_extraction_gc(
                    session,
                    project_active_check=is_knowledge_project_active,
                    project_id=harness.project_id,
                )

        results = await asyncio.gather(sweep(), sweep())
        assert sum(results) == 1
        rows = await harness.read_rows()
        assert len([task for task in rows["tasks"] if task.kind == "delete_extraction" and task.status in {"queued", "running", "retry_wait"}]) == 1


@pytest.mark.asyncio
async def test_failed_delete_task_can_be_readmitted_with_full_attempt_budget(
    postgres_database_url: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        stored = await harness.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
        claim = await harness.claim_cleanup(stored.extraction_id)
        async with harness.session_factory() as session, session.begin():
            task = await session.get(KnowledgeTaskRow, claim.id, with_for_update=True)
            assert task is not None
            task.status = "failed"
            task.claim_token = None
            task.lease_until = None
            task.finished_at = await session.scalar(select(func.clock_timestamp()))
            task.error_message = "safe failure"

        await harness.store.enqueue_cleanup(stored.extraction_id, project_id=harness.project_id)

        delete_tasks = [task for task in (await harness.read_rows())["tasks"] if task.kind == "delete_extraction"]
        assert len(delete_tasks) == 2
        replacement = next(task for task in delete_tasks if task.status == "queued")
        assert (replacement.attempt_count, replacement.max_attempts, replacement.storage_key) == (0, 3, None)


@pytest.mark.asyncio
async def test_worker_runs_bounded_gc_before_claiming_cleanup(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler
    from actweave_knowledge.tasks.worker import KnowledgeTaskWorker

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        stored = await harness.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
            await session.execute(
                text("UPDATE knowledge_extractions SET unpublished_expires_at = clock_timestamp() - interval '1 second' WHERE id = :id"),
                {"id": stored.extraction_id},
            )
        handler = KnowledgeExtractionDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )
        worker = KnowledgeTaskWorker(
            session_factory=harness.session_factory,
            handlers={"delete_extraction": handler},
            project_active_check=is_knowledge_project_active,
            concurrency=1,
            task_timeout_seconds=30,
            retry_delay_seconds=0,
        )

        assert await worker._run_once()

        rows = await harness.read_rows()
        assert rows["extractions"] == []
        cleanup = [task for task in rows["tasks"] if task.kind == "delete_extraction"]
        assert len(cleanup) == 1 and cleanup[0].status == "succeeded"


@pytest.mark.asyncio
async def test_delete_handler_accepts_only_claimed_resource_and_missing_is_idempotent(
    postgres_database_url: str,
) -> None:
    from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler

    async with extraction_harness(postgres_database_url) as harness:
        profile = make_parse_profile(".pdf")
        source = (await harness.read_rows())["documents"][0].source_sha256
        reservation = await harness.store.begin(harness.claim, source_sha256=source, profile=profile)
        stored = await harness.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source),
        )
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(session, harness.claim.id, harness.claim.claim_token)
        claim = await harness.claim_cleanup(stored.extraction_id)
        handler = KnowledgeExtractionDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )
        before = dict(harness.object_store.objects)

        with pytest.raises(KnowledgeError):
            await handler(replace(claim, resource_id=uuid.uuid4()))

        assert harness.object_store.objects == before
        async with harness.session_factory() as session, session.begin():
            task = await session.get(KnowledgeTaskRow, claim.id, with_for_update=True)
            assert task is not None
            task.resource_id = uuid.uuid4()
            missing_id = task.resource_id
        await handler(replace(claim, resource_id=missing_id))
        assert harness.object_store.objects == before


@pytest.mark.asyncio
async def test_module_worker_registers_durable_extraction_deletion(monkeypatch) -> None:
    from actweave_knowledge import module as module_under_test
    from actweave_knowledge.tasks.extraction_deletion import KnowledgeExtractionDeletionHandler

    captured = {}

    class CapturingWorker:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def run(self, stop_event: asyncio.Event) -> None:
            assert stop_event.is_set()

    monkeypatch.setattr(module_under_test, "KnowledgeTaskWorker", CapturingWorker)
    module = object.__new__(module_under_test.KnowledgeModule)
    module._object_store = object()
    module._project_active_check = is_knowledge_project_active
    module._session_factory = object()
    module._quota = object()
    module._settings = KnowledgeSettings()
    module._model_client = object()
    module._model_port = object()
    stop_event = asyncio.Event()
    stop_event.set()

    await module.run_worker(stop_event)

    handler = captured["handlers"]["delete_extraction"]
    assert isinstance(handler, KnowledgeExtractionDeletionHandler)
    assert handler._session_factory is module._session_factory
    assert handler._object_store is module._object_store
    assert handler._quota is module._quota
    assert handler._project_active_check is is_knowledge_project_active
