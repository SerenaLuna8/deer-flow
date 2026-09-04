"""P2-T6 durable Document/Base/Project extraction retention gates."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from actweave_knowledge.contracts import (
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KNOWLEDGE_TASK_FAILED,
    KnowledgeError,
    KnowledgeSettings,
)
from actweave_knowledge.persistence.models import (
    KnowledgeAttachmentRow,
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeExtractionRow,
    KnowledgeSegmentAttachmentRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from actweave_knowledge.persistence.tasks import claim_next_task, settle_task_success
from actweave_knowledge.project_retention import create_knowledge_project_purger
from actweave_knowledge.tasks import (
    KnowledgeBaseDeletionHandler,
    KnowledgeDocumentDeletionHandler,
    KnowledgeDocumentObjectDeletionHandler,
    KnowledgeTaskClaim,
    purge_project_knowledge,
)
from extraction_test_helpers import (
    ExtractionObjectStore,
    extraction_harness,
    installed_knowledge_sessions,
    make_test_quota_port,
    seed_scope,
)
from sqlalchemy import func, select, text

from app.knowledge.composition import is_knowledge_project_active
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.quotas.model import (
    ProjectUsageCounterRow,
    ProjectUsageLedgerRow,
)


async def _claim_delete_document(harness) -> KnowledgeTaskClaim:  # noqa: ANN001
    async with harness.session_factory() as session, session.begin():
        document = await session.get(
            KnowledgeDocumentRow,
            harness.document_id,
            with_for_update=True,
        )
        assert document is not None
        document.status = "deleting"
        document.error_message = None
        session.add(
            KnowledgeTaskRow(
                id=uuid.uuid4(),
                project_id=harness.project_id,
                resource_id=harness.document_id,
                kind="delete_document",
                target_version=None,
                status="queued",
            )
        )
        await session.flush()
        task = await claim_next_task(session, lease_seconds=600)
        assert task is not None and task.claim_token is not None
        assert task.kind == "delete_document"
        return KnowledgeTaskClaim(
            id=task.id,
            project_id=task.project_id,
            resource_id=task.resource_id,
            kind=task.kind,
            target_version=task.target_version,
            claim_token=task.claim_token,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
            storage_key=task.storage_key,
            reparse_settings=task.reparse_settings,
        )


async def _claim_delete_base(harness) -> KnowledgeTaskClaim:  # noqa: ANN001
    async with harness.session_factory() as session, session.begin():
        base = await session.get(KnowledgeBaseRow, harness.base_id, with_for_update=True)
        assert base is not None
        base.status = "deleting"
        session.add(
            KnowledgeTaskRow(
                id=uuid.uuid4(),
                project_id=harness.project_id,
                resource_id=harness.base_id,
                kind="delete_knowledge_base",
                target_version=None,
                status="queued",
            )
        )
        await session.flush()
        task = await claim_next_task(session, lease_seconds=600)
        assert task is not None and task.claim_token is not None
        assert task.kind == "delete_knowledge_base"
        return KnowledgeTaskClaim(
            id=task.id,
            project_id=task.project_id,
            resource_id=task.resource_id,
            kind=task.kind,
            target_version=task.target_version,
            claim_token=task.claim_token,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
            storage_key=task.storage_key,
            reparse_settings=task.reparse_settings,
        )


async def _claim_delete_object(harness, storage_key: str) -> KnowledgeTaskClaim:  # noqa: ANN001
    async with harness.session_factory() as session, session.begin():
        assert await settle_task_success(
            session,
            harness.claim.id,
            harness.claim.claim_token,
        )
        document = await session.get(
            KnowledgeDocumentRow,
            harness.document_id,
            with_for_update=True,
        )
        assert document is not None
        document.status = "deleting"
        session.add(
            KnowledgeTaskRow(
                id=uuid.uuid4(),
                project_id=harness.project_id,
                resource_id=harness.document_id,
                kind="delete_document_object",
                target_version=None,
                storage_key=storage_key,
                status="queued",
            )
        )
        await session.flush()
        task = await claim_next_task(session, lease_seconds=600)
        assert task is not None and task.claim_token is not None
        assert task.kind == "delete_document_object"
        return KnowledgeTaskClaim(
            id=task.id,
            project_id=task.project_id,
            resource_id=task.resource_id,
            kind=task.kind,
            target_version=task.target_version,
            claim_token=task.claim_token,
            attempt_count=task.attempt_count,
            max_attempts=task.max_attempts,
            storage_key=task.storage_key,
            reparse_settings=task.reparse_settings,
        )


async def _storage_usage(harness) -> tuple[int, int]:  # noqa: ANN001
    async with harness.session_factory() as session:
        counter = await session.scalar(
            select(ProjectUsageCounterRow).where(
                ProjectUsageCounterRow.project_id == harness.project_id,
                ProjectUsageCounterRow.dimension == "storage_bytes",
            )
        )
        assert counter is not None
        return counter.used, counter.reserved


async def _install_valid_source_key(harness) -> str:  # noqa: ANN001
    valid_source_key = f"projects/{harness.project_id}/knowledge/{harness.base_id}/{harness.document_id}.pdf"
    async with harness.session_factory() as session, session.begin():
        document = await session.get(KnowledgeDocumentRow, harness.document_id)
        assert document is not None
        old_source_key = document.storage_key
        document.storage_key = valid_source_key
    harness.object_store.objects[valid_source_key] = harness.object_store.objects.pop(old_source_key)
    return valid_source_key


async def _is_pending_deletion_project(session, project_id: uuid.UUID) -> bool:  # noqa: ANN001
    status = await session.scalar(select(ProjectRow.status).where(ProjectRow.id == project_id).with_for_update(read=True, of=ProjectRow))
    return status == "pending_deletion"


@pytest.mark.asyncio
async def test_document_delete_withdraws_publication_then_deletes_exact_registered_closure(
    postgres_database_url: str,
    tmp_path,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        await _install_valid_source_key(harness)
        await harness.seed_attachment_read(tmp_path)
        registered_keys = set(harness.object_store.objects)
        assert len(registered_keys) == 3  # original + attachment + manifest
        assert await _storage_usage(harness) > (0, 0)
        claim = await _claim_delete_document(harness)
        handler = KnowledgeDocumentDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        await handler(claim)
        async with harness.session_factory() as session:
            ledger_after_first = set((await session.scalars(select(ProjectUsageLedgerRow.id).where(ProjectUsageLedgerRow.project_id == harness.project_id))).all())
        await handler(claim)

        assert not (registered_keys & harness.object_store.objects.keys())
        assert await _storage_usage(harness) == (0, 0)
        async with harness.session_factory() as session:
            assert await session.get(KnowledgeDocumentRow, harness.document_id) is None
            for model in (
                KnowledgeExtractionRow,
                KnowledgeAttachmentRow,
                KnowledgeSegmentRow,
                KnowledgeSegmentAttachmentRow,
            ):
                remaining = await session.scalar(select(func.count()).select_from(model).where(model.project_id == harness.project_id))
                assert int(remaining or 0) == 0, model.__name__
            ledger_after_retry = set((await session.scalars(select(ProjectUsageLedgerRow.id).where(ProjectUsageLedgerRow.project_id == harness.project_id))).all())
            assert ledger_after_retry == ledger_after_first


@pytest.mark.parametrize("failure_stage", ("attachment", "manifest", "source"))
@pytest.mark.asyncio
async def test_document_delete_failure_keeps_durable_scope_and_charge_until_retry(
    postgres_database_url: str,
    tmp_path,
    failure_stage: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        source_key = await _install_valid_source_key(harness)
        await harness.seed_attachment_read(tmp_path)
        async with harness.session_factory() as session:
            attachment_key = await session.scalar(select(KnowledgeAttachmentRow.storage_key).where(KnowledgeAttachmentRow.project_id == harness.project_id))
            manifest_key = await session.scalar(select(KnowledgeExtractionRow.manifest_storage_key).where(KnowledgeExtractionRow.project_id == harness.project_id))
        assert attachment_key is not None and manifest_key is not None
        target_key = {
            "attachment": attachment_key,
            "manifest": manifest_key,
            "source": source_key,
        }[failure_stage]
        claim = await _claim_delete_document(harness)
        original_before = harness.object_store._before
        injected = False

        async def fail_exact_delete(operation: str, key: str) -> None:
            nonlocal injected
            if operation == "delete" and key == target_key and not injected:
                injected = True
                raise KnowledgeError(
                    KNOWLEDGE_STORAGE_UNAVAILABLE,
                    "对象存储暂时不可用",
                )
            await original_before(operation, key)

        harness.object_store._before = fail_exact_delete
        handler = KnowledgeDocumentDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        with pytest.raises(KnowledgeError):
            await handler(claim)

        assert target_key in harness.object_store.objects
        used_after_failure, reserved_after_failure = await _storage_usage(harness)
        assert used_after_failure > 0
        assert reserved_after_failure == 0
        async with harness.session_factory() as session:
            document = await session.get(KnowledgeDocumentRow, harness.document_id)
            assert document is not None and document.status == "deleting"
            if failure_stage != "source":
                remaining_extractions = await session.scalar(select(func.count()).select_from(KnowledgeExtractionRow).where(KnowledgeExtractionRow.project_id == harness.project_id))
                assert int(remaining_extractions or 0) == 1

        await handler(claim)

        assert await _storage_usage(harness) == (0, 0)
        assert harness.object_store.objects == {}
        async with harness.session_factory() as session:
            assert await session.get(KnowledgeDocumentRow, harness.document_id) is None


@pytest.mark.asyncio
async def test_document_delete_rejects_unregistered_source_key_without_object_io(
    postgres_database_url: str,
    tmp_path,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        await harness.seed_attachment_read(tmp_path)
        original_objects = dict(harness.object_store.objects)
        calls_before_delete = list(harness.object_store.calls)
        claim = await _claim_delete_document(harness)
        handler = KnowledgeDocumentDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        with pytest.raises(KnowledgeError) as error:
            await handler(claim)

        assert error.value.code == KNOWLEDGE_TASK_FAILED
        assert harness.object_store.objects == original_objects
        assert harness.object_store.calls == calls_before_delete
        async with harness.session_factory() as session:
            assert await session.get(KnowledgeDocumentRow, harness.document_id) is not None


@pytest.mark.asyncio
async def test_source_absence_confirmation_failure_keeps_document_charge_for_retry(
    postgres_database_url: str,
    tmp_path,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        source_key = await _install_valid_source_key(harness)
        await harness.seed_attachment_read(tmp_path)
        claim = await _claim_delete_document(harness)
        original_before = harness.object_store._before
        injected = False

        async def fail_source_confirmation(operation: str, key: str) -> None:
            nonlocal injected
            if operation == "get" and key == source_key and not injected:
                injected = True
                raise KnowledgeError(
                    KNOWLEDGE_STORAGE_UNAVAILABLE,
                    "对象存储暂时不可用",
                )
            await original_before(operation, key)

        harness.object_store._before = fail_source_confirmation
        handler = KnowledgeDocumentDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        with pytest.raises(KnowledgeError):
            await handler(claim)

        assert source_key not in harness.object_store.objects
        assert await _storage_usage(harness) == (8, 0)
        async with harness.session_factory() as session:
            document = await session.get(KnowledgeDocumentRow, harness.document_id)
            assert document is not None
            assert document.upload_state == "delete_pending"
            assert document.quota_state == "committed"

        await handler(claim)
        assert await _storage_usage(harness) == (0, 0)


@pytest.mark.asyncio
async def test_late_put_object_handler_confirms_absence_and_releases_exact_charge(
    postgres_database_url: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        source_key = await _install_valid_source_key(harness)
        claim = await _claim_delete_object(harness, source_key)
        handler = KnowledgeDocumentObjectDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        await handler(claim)
        await handler(claim)

        assert source_key not in harness.object_store.objects
        assert await _storage_usage(harness) == (0, 0)
        async with harness.session_factory() as session:
            assert await session.get(KnowledgeDocumentRow, harness.document_id) is None


@pytest.mark.asyncio
async def test_late_put_confirmation_failure_keeps_tombstone_and_charge_for_retry(
    postgres_database_url: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        source_key = await _install_valid_source_key(harness)
        claim = await _claim_delete_object(harness, source_key)
        original_before = harness.object_store._before
        injected = False

        async def fail_confirmation(operation: str, key: str) -> None:
            nonlocal injected
            if operation == "get" and key == source_key and not injected:
                injected = True
                raise KnowledgeError(
                    KNOWLEDGE_STORAGE_UNAVAILABLE,
                    "对象存储暂时不可用",
                )
            await original_before(operation, key)

        harness.object_store._before = fail_confirmation
        handler = KnowledgeDocumentObjectDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        with pytest.raises(KnowledgeError):
            await handler(claim)

        assert await _storage_usage(harness) == (8, 0)
        async with harness.session_factory() as session:
            document = await session.get(KnowledgeDocumentRow, harness.document_id)
            assert document is not None and document.status == "deleting"

        await handler(claim)
        assert await _storage_usage(harness) == (0, 0)


@pytest.mark.asyncio
async def test_late_put_handler_rejects_forged_claim_before_object_io(
    postgres_database_url: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        source_key = await _install_valid_source_key(harness)
        claim = await _claim_delete_object(harness, source_key)
        forged_key = f"projects/{harness.project_id}/knowledge/{uuid.uuid4()}/{harness.document_id}.pdf"
        calls_before = list(harness.object_store.calls)
        handler = KnowledgeDocumentObjectDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        with pytest.raises(KnowledgeError) as error:
            await handler(replace(claim, storage_key=forged_key))

        assert error.value.code == KNOWLEDGE_TASK_FAILED
        assert harness.object_store.calls == calls_before
        assert source_key in harness.object_store.objects


@pytest.mark.asyncio
async def test_late_put_handler_revalidates_lease_after_object_io(
    postgres_database_url: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        source_key = await _install_valid_source_key(harness)
        claim = await _claim_delete_object(harness, source_key)
        confirmation = harness.object_store.pause("get")
        handler = KnowledgeDocumentObjectDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )
        running = asyncio.create_task(handler(claim))
        await confirmation.entered.wait()
        async with harness.session_factory() as session, session.begin():
            task = await session.get(KnowledgeTaskRow, claim.id, with_for_update=True)
            assert task is not None
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        confirmation.released.set()

        with pytest.raises(KnowledgeError) as error:
            await running

        assert error.value.code == KNOWLEDGE_TASK_FAILED
        assert source_key not in harness.object_store.objects
        assert await _storage_usage(harness) == (8, 0)
        async with harness.session_factory() as session:
            assert await session.get(KnowledgeDocumentRow, harness.document_id) is not None


@pytest.mark.parametrize("parent_kind", ("document", "base"))
@pytest.mark.asyncio
async def test_parent_delete_lease_loss_during_attachment_confirmation_keeps_child_authority(
    postgres_database_url: str,
    tmp_path,
    parent_kind: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        await _install_valid_source_key(harness)
        await harness.seed_attachment_read(tmp_path)
        if parent_kind == "document":
            claim = await _claim_delete_document(harness)
            handler = KnowledgeDocumentDeletionHandler(
                session_factory=harness.session_factory,
                object_store=harness.object_store,
                quota=harness.quota,
                project_active_check=is_knowledge_project_active,
            )
        else:
            claim = await _claim_delete_base(harness)
            handler = KnowledgeBaseDeletionHandler(
                session_factory=harness.session_factory,
                object_store=harness.object_store,
                quota=harness.quota,
                project_active_check=is_knowledge_project_active,
            )
        confirmation = harness.object_store.pause("get")
        running = asyncio.create_task(handler(claim))
        await confirmation.entered.wait()
        async with harness.session_factory() as session, session.begin():
            task = await session.get(KnowledgeTaskRow, claim.id, with_for_update=True)
            assert task is not None
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        confirmation.released.set()

        with pytest.raises(KnowledgeError):
            await running

        async with harness.session_factory() as session:
            attachment = await session.scalar(select(KnowledgeAttachmentRow).where(KnowledgeAttachmentRow.project_id == harness.project_id))
            extraction = await session.scalar(select(KnowledgeExtractionRow).where(KnowledgeExtractionRow.project_id == harness.project_id))
            document = await session.get(KnowledgeDocumentRow, harness.document_id)
            assert attachment is not None
            assert attachment.upload_state == "delete_pending"
            assert attachment.quota_state == "committed"
            assert extraction is not None and document is not None
        used, reserved = await _storage_usage(harness)
        assert used > 8 and reserved == 0

        async with harness.session_factory() as session, session.begin():
            retried = await claim_next_task(session, lease_seconds=600)
            assert retried is not None and retried.id == claim.id
            assert retried.claim_token is not None
            retry_claim = KnowledgeTaskClaim(
                id=retried.id,
                project_id=retried.project_id,
                resource_id=retried.resource_id,
                kind=retried.kind,
                target_version=retried.target_version,
                claim_token=retried.claim_token,
                attempt_count=retried.attempt_count,
                max_attempts=retried.max_attempts,
                storage_key=retried.storage_key,
                reparse_settings=retried.reparse_settings,
            )

        await handler(retry_claim)

        assert harness.object_store.objects == {}
        assert await _storage_usage(harness) == (0, 0)
        async with harness.session_factory() as session:
            assert await session.get(KnowledgeDocumentRow, harness.document_id) is None
            if parent_kind == "base":
                assert await session.get(KnowledgeBaseRow, harness.base_id) is None


@pytest.mark.asyncio
async def test_late_put_handler_rejects_post_io_tombstone_key_change(
    postgres_database_url: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        source_key = await _install_valid_source_key(harness)
        claim = await _claim_delete_object(harness, source_key)
        changed_key = f"projects/{harness.project_id}/knowledge/{uuid.uuid4()}/{harness.document_id}.pdf"
        harness.object_store.objects[changed_key] = b"unrelated"
        confirmation = harness.object_store.pause("get")
        handler = KnowledgeDocumentObjectDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )
        running = asyncio.create_task(handler(claim))
        await confirmation.entered.wait()
        async with harness.session_factory() as session, session.begin():
            document = await session.get(
                KnowledgeDocumentRow,
                harness.document_id,
                with_for_update=True,
            )
            assert document is not None
            document.storage_key = changed_key
        confirmation.released.set()

        with pytest.raises(KnowledgeError) as error:
            await running

        assert error.value.code == KNOWLEDGE_TASK_FAILED
        assert source_key not in harness.object_store.objects
        assert harness.object_store.objects[changed_key] == b"unrelated"
        assert await _storage_usage(harness) == (8, 0)


@pytest.mark.asyncio
async def test_late_put_handler_cancellation_keeps_charge_until_retry(
    postgres_database_url: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        source_key = await _install_valid_source_key(harness)
        claim = await _claim_delete_object(harness, source_key)
        confirmation = harness.object_store.pause("get")
        handler = KnowledgeDocumentObjectDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )
        running = asyncio.create_task(handler(claim))
        await confirmation.entered.wait()
        running.cancel()
        confirmation.released.set()

        with pytest.raises(asyncio.CancelledError):
            await running

        assert source_key not in harness.object_store.objects
        assert await _storage_usage(harness) == (8, 0)
        async with harness.session_factory() as session:
            assert await session.get(KnowledgeDocumentRow, harness.document_id) is not None

        await handler(claim)
        assert await _storage_usage(harness) == (0, 0)


@pytest.mark.asyncio
async def test_base_delete_quiesces_and_drains_registered_document_family(
    postgres_database_url: str,
    tmp_path,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        await _install_valid_source_key(harness)
        await harness.seed_attachment_read(tmp_path)
        claim = await _claim_delete_base(harness)
        handler = KnowledgeBaseDeletionHandler(
            session_factory=harness.session_factory,
            object_store=harness.object_store,
            quota=harness.quota,
            project_active_check=is_knowledge_project_active,
        )

        await handler(claim)

        assert harness.object_store.objects == {}
        assert await _storage_usage(harness) == (0, 0)
        async with harness.session_factory() as session:
            assert await session.get(KnowledgeBaseRow, harness.base_id) is None
            assert await session.get(KnowledgeDocumentRow, harness.document_id) is None


@pytest.mark.asyncio
async def test_project_purge_requires_pending_deletion_and_removes_registered_family(
    postgres_database_url: str,
    tmp_path,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        await _install_valid_source_key(harness)
        await harness.seed_attachment_read(tmp_path)
        async with harness.session_factory() as session, session.begin():
            project = await session.get(ProjectRow, harness.project_id, with_for_update=True)
            assert project is not None
            project.status = "pending_deletion"

        completed = await purge_project_knowledge(
            harness.session_factory,
            harness.object_store,
            quota=harness.quota,
            project_cleanup_check=_is_pending_deletion_project,
            project_id=harness.project_id,
        )

        assert completed is True
        assert harness.object_store.objects == {}
        assert await _storage_usage(harness) == (0, 0)
        async with harness.session_factory() as session:
            for model in (
                KnowledgeBaseRow,
                KnowledgeDocumentRow,
                KnowledgeExtractionRow,
                KnowledgeAttachmentRow,
                KnowledgeTaskRow,
            ):
                remaining = await session.scalar(select(func.count()).select_from(model).where(model.project_id == harness.project_id))
                assert int(remaining or 0) == 0, model.__name__


@pytest.mark.asyncio
async def test_project_purge_rejects_active_project_before_object_io(
    postgres_database_url: str,
    tmp_path,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        await _install_valid_source_key(harness)
        await harness.seed_attachment_read(tmp_path)
        original_objects = dict(harness.object_store.objects)
        calls_before = list(harness.object_store.calls)

        completed = await purge_project_knowledge(
            harness.session_factory,
            harness.object_store,
            quota=harness.quota,
            project_cleanup_check=_is_pending_deletion_project,
            project_id=harness.project_id,
        )

        assert completed is False
        assert harness.object_store.objects == original_objects
        assert harness.object_store.calls == calls_before


@pytest.mark.asyncio
async def test_project_purge_honors_upload_grace_then_removes_late_put(
    postgres_database_url: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        source_key = await _install_valid_source_key(harness)
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(
                session,
                harness.claim.id,
                harness.claim.claim_token,
            )
            document = await session.get(
                KnowledgeDocumentRow,
                harness.document_id,
                with_for_update=True,
            )
            project = await session.get(ProjectRow, harness.project_id, with_for_update=True)
            assert document is not None and project is not None
            document.status = "uploading"
            document.updated_at = func.clock_timestamp()  # type: ignore[assignment]
            project.status = "pending_deletion"

        assert not await purge_project_knowledge(
            harness.session_factory,
            harness.object_store,
            quota=harness.quota,
            project_cleanup_check=_is_pending_deletion_project,
            project_id=harness.project_id,
        )
        assert source_key in harness.object_store.objects

        async with harness.session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_documents SET updated_at = clock_timestamp() - interval '2 days' WHERE id = :document_id"),
                {"document_id": harness.document_id},
            )
        # A stale upload first becomes a durable exact-key tombstone. The PUT
        # may settle after that transaction, so this attempt never deletes it.
        assert not await purge_project_knowledge(
            harness.session_factory,
            harness.object_store,
            quota=harness.quota,
            project_cleanup_check=_is_pending_deletion_project,
            project_id=harness.project_id,
        )
        assert source_key in harness.object_store.objects
        async with harness.session_factory() as session:
            document = await session.get(KnowledgeDocumentRow, harness.document_id)
            assert document is not None and document.status == "deleting"

        assert await purge_project_knowledge(
            harness.session_factory,
            harness.object_store,
            quota=harness.quota,
            project_cleanup_check=_is_pending_deletion_project,
            project_id=harness.project_id,
        )
        assert source_key not in harness.object_store.objects
        assert await _storage_usage(harness) == (0, 0)


@pytest.mark.asyncio
async def test_project_purge_preserves_young_pending_cleanup_then_recovers_stale_late_put(
    postgres_database_url: str,
) -> None:
    async with installed_knowledge_sessions(postgres_database_url) as sessions:
        project_id, base_id, document_id = await seed_scope(sessions)
        quota = make_test_quota_port(sessions)
        store = ExtractionObjectStore()
        source_key = f"projects/{project_id}/knowledge/{base_id}/{document_id}.pdf"
        cleanup_task_id = uuid.uuid4()
        async with sessions() as session, session.begin():
            project = await session.get(ProjectRow, project_id, with_for_update=True)
            document = await session.get(
                KnowledgeDocumentRow,
                document_id,
                with_for_update=True,
            )
            assert project is not None and document is not None
            document.storage_key = source_key
            document.size_bytes = 8
            document.status = "deleting"
            document.upload_state = "pending"
            document.quota_state = "unreserved"
            document.updated_at = func.clock_timestamp()  # type: ignore[assignment]
            await session.flush()
            await quota.reserve(
                session,
                project_id=project_id,
                object_id=document_id,
                size_bytes=8,
            )
            project.status = "pending_deletion"
            session.add(
                KnowledgeTaskRow(
                    id=cleanup_task_id,
                    project_id=project_id,
                    resource_id=document_id,
                    kind="delete_document_object",
                    target_version=None,
                    storage_key=source_key,
                    status="queued",
                )
            )
        store.objects[source_key] = b"late put"

        for _ in range(2):
            assert not await purge_project_knowledge(
                sessions,
                store,
                quota=quota,
                project_cleanup_check=_is_pending_deletion_project,
                project_id=project_id,
            )
            async with sessions() as session:
                document = await session.get(KnowledgeDocumentRow, document_id)
                cleanup = await session.get(KnowledgeTaskRow, cleanup_task_id)
                counter = await session.scalar(
                    select(ProjectUsageCounterRow).where(
                        ProjectUsageCounterRow.project_id == project_id,
                        ProjectUsageCounterRow.dimension == "storage_bytes",
                    )
                )
                assert document is not None
                assert (document.upload_state, document.quota_state) == (
                    "pending",
                    "reserved",
                )
                assert cleanup is not None and cleanup.status == "queued"
                assert counter is not None and (counter.used, counter.reserved) == (0, 8)
                assert source_key in store.objects

        async with sessions() as session, session.begin():
            await session.execute(
                text("UPDATE knowledge_documents SET updated_at = clock_timestamp() - interval '2 days' WHERE id = :document_id"),
                {"document_id": document_id},
            )

        assert not await purge_project_knowledge(
            sessions,
            store,
            quota=quota,
            project_cleanup_check=_is_pending_deletion_project,
            project_id=project_id,
        )
        async with sessions() as session:
            document = await session.get(KnowledgeDocumentRow, document_id)
            assert document is not None
            assert (document.status, document.upload_state, document.quota_state) == (
                "deleting",
                "delete_pending",
                "reserved",
            )
            assert (
                await session.scalar(
                    select(KnowledgeTaskRow.id).where(
                        KnowledgeTaskRow.project_id == project_id,
                        KnowledgeTaskRow.resource_id == document_id,
                        KnowledgeTaskRow.kind == "delete_document_object",
                        KnowledgeTaskRow.status.in_(("queued", "retry_wait")),
                    )
                )
                is not None
            )
        assert source_key in store.objects

        assert await purge_project_knowledge(
            sessions,
            store,
            quota=quota,
            project_cleanup_check=_is_pending_deletion_project,
            project_id=project_id,
        )
        assert source_key not in store.objects
        async with sessions() as session:
            assert await session.get(KnowledgeDocumentRow, document_id) is None
            counter = await session.scalar(
                select(ProjectUsageCounterRow).where(
                    ProjectUsageCounterRow.project_id == project_id,
                    ProjectUsageCounterRow.dimension == "storage_bytes",
                )
            )
            assert counter is not None and (counter.used, counter.reserved) == (0, 0)


@pytest.mark.asyncio
async def test_disabled_retention_without_storage_fails_closed_for_extraction_cleanup(
    postgres_database_url: str,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        async with harness.session_factory() as session, session.begin():
            assert await settle_task_success(
                session,
                harness.claim.id,
                harness.claim.claim_token,
            )
            project = await session.get(ProjectRow, harness.project_id, with_for_update=True)
            assert project is not None
            project.status = "pending_deletion"
            session.add(
                KnowledgeTaskRow(
                    id=uuid.uuid4(),
                    project_id=harness.project_id,
                    resource_id=uuid.uuid4(),
                    kind="delete_extraction",
                    target_version=None,
                    status="queued",
                )
            )
        purger = create_knowledge_project_purger(
            settings=KnowledgeSettings(),
            session_factory=harness.session_factory,
            quota=harness.quota,
            project_cleanup_check=_is_pending_deletion_project,
        )

        assert await purger.purge_project(harness.project_id) is False
        async with harness.session_factory() as session:
            pending_cleanup = await session.scalar(
                select(KnowledgeTaskRow.id).where(
                    KnowledgeTaskRow.project_id == harness.project_id,
                    KnowledgeTaskRow.kind == "delete_extraction",
                    KnowledgeTaskRow.status != "succeeded",
                )
            )
            assert pending_cleanup is not None
