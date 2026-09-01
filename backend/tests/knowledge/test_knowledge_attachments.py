"""Registration, quota and exact-claim I/O fences on real PostgreSQL."""

import asyncio
import hashlib
import uuid
from dataclasses import replace

import pytest
from actweave_knowledge.contracts import KNOWLEDGE_CONFLICT, KNOWLEDGE_NOT_FOUND, KNOWLEDGE_QUOTA_EXCEEDED, KnowledgeError
from actweave_knowledge.persistence.models import KnowledgeAttachmentRow, KnowledgeDocumentRow, KnowledgeSegmentAttachmentRow, KnowledgeSegmentRow
from actweave_knowledge.persistence.tasks import settle_task_success
from extraction_test_helpers import ToggleKnowledgeAuthority, extraction_harness, make_extraction_result, write_test_asset
from parsing_test_helpers import make_parse_profile
from sqlalchemy import func, select, text


async def begin(h):
    return await h.store.begin(h.claim, source_sha256=(await h.read_rows())["documents"][0].source_sha256, profile=make_parse_profile(".pdf"))


@pytest.mark.asyncio
async def test_real_published_attachment_binding_and_toggle_authority(postgres_database_url, tmp_path):
    async with extraction_harness(postgres_database_url) as h:
        asset = write_test_asset(tmp_path)
        profile = make_parse_profile(".pdf")
        source_sha256 = (await h.read_rows())["documents"][0].source_sha256
        reservation = await h.store.begin(h.claim, source_sha256=source_sha256, profile=profile)
        await h.store.persist_attachment(reservation, asset, work_dir=tmp_path)
        stored = await h.store.complete(
            reservation,
            make_extraction_result(profile, source_sha256=source_sha256, attachments=(asset,)),
        )
        async with h.session_factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, h.document_id)
            document.published_extraction_id = stored.extraction_id
            document.published_version = document.version
            document.status = "ready"
            assert await settle_task_success(session, h.claim.id, h.claim.claim_token)

        segment_id, attachment_id, digest = await h.bind_test_attachment(stored, asset)
        async with h.session_factory() as session, session.begin():
            segment = await session.get(KnowledgeSegmentRow, segment_id)
            attachment = await session.get(KnowledgeAttachmentRow, attachment_id)
            binding = await session.get(KnowledgeSegmentAttachmentRow, (segment_id, 1))
            document = await session.get(KnowledgeDocumentRow, h.document_id)
            assert segment.extraction_id == document.published_extraction_id == stored.extraction_id
            assert segment.document_version == document.published_version
            assert f"knowledge-attachment:{asset.attachment.ref}" in segment.content
            assert digest == hashlib.sha256(segment.content.encode()).hexdigest()
            assert attachment.state == "ready" and attachment.upload_state == "stored"
            assert binding.attachment_id == attachment_id
            session.add(
                KnowledgeSegmentAttachmentRow(
                    project_id=h.project_id,
                    knowledge_base_id=h.base_id,
                    knowledge_document_id=h.document_id,
                    extraction_id=stored.extraction_id,
                    segment_id=segment_id,
                    attachment_id=attachment_id,
                    position=2,
                )
            )
        async with h.session_factory() as session:
            positions = await session.scalar(
                select(func.count())
                .select_from(KnowledgeSegmentAttachmentRow)
                .where(
                    KnowledgeSegmentAttachmentRow.segment_id == segment_id,
                )
            )
            authority = ToggleKnowledgeAuthority(h.project_id, uuid.uuid4())
            await authority.revalidate(session)
            authority.revoked = True
            with pytest.raises(KnowledgeError) as error:
                await authority.revalidate(session)
            assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert positions == 2


@pytest.mark.asyncio
async def test_put_has_committed_registration_and_reservation(postgres_database_url, tmp_path):
    async with extraction_harness(postgres_database_url) as h:
        asset = write_test_asset(tmp_path)
        reservation = await begin(h)
        assert await begin(h) == reservation
        gate = h.object_store.pause("put")
        pending = asyncio.create_task(h.store.persist_attachment(reservation, asset, work_dir=tmp_path))
        async with asyncio.timeout(10):
            await gate.entered.wait()
            rows = await h.read_rows()
            try:
                assert len(rows["extractions"]) == 1
                assert rows["tasks"][0].extraction_id == reservation.extraction_id
                assert len(rows["attachments"]) == 1
                assert rows["attachments"][0].quota_state == "reserved"
                assert rows["attachments"][0].upload_state == "pending"
            finally:
                gate.released.set()
            await pending
        await h.store.persist_attachment(reservation, asset, work_dir=tmp_path)
        rows = await h.read_rows()
        assert rows["attachments"][0].state == "ready"
        assert rows["attachments"][0].quota_state == "committed"
        assert len([call for call in h.object_store.calls if call[0] == "put"]) == 1


@pytest.mark.asyncio
async def test_quota_failure_never_puts(postgres_database_url, tmp_path):
    async with extraction_harness(postgres_database_url, quota_bytes=8) as h:
        reservation = await begin(h)
        with pytest.raises(KnowledgeError) as error:
            await h.store.persist_attachment(reservation, write_test_asset(tmp_path), work_dir=tmp_path)
        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert not h.object_store.calls
        assert not (await h.read_rows())["attachments"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["lease", "version", "put", "cancel", "project"])
async def test_failed_or_cancelled_put_keeps_facts_and_admits_cleanup(postgres_database_url, tmp_path, fault):
    async with extraction_harness(postgres_database_url) as h:
        reservation = await begin(h)
        gate = h.object_store.pause("put")
        if fault == "put":
            h.object_store.fail_next("put")
        pending = asyncio.create_task(h.store.persist_attachment(reservation, write_test_asset(tmp_path), work_dir=tmp_path))
        async with asyncio.timeout(10):
            await gate.entered.wait()
            if fault == "lease":
                async with h.session_factory() as session, session.begin():
                    await session.execute(text("UPDATE knowledge_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": h.claim.id})
            elif fault == "version":
                async with h.session_factory() as session, session.begin():
                    await session.execute(text("UPDATE knowledge_documents SET version=version+1 WHERE id=:id"), {"id": h.document_id})
            elif fault == "project":
                async with h.session_factory() as session, session.begin():
                    await session.execute(text("UPDATE projects SET status='pending_deletion' WHERE id=:id"), {"id": h.project_id})
            elif fault == "cancel":
                pending.cancel()
                await asyncio.get_running_loop().run_in_executor(None, lambda: None)
                assert not pending.done()
            gate.released.set()
            with pytest.raises((KnowledgeError, asyncio.CancelledError)):
                await pending
        rows = await h.read_rows()
        attachment = rows["attachments"][0]
        assert attachment.state == "deleting"
        assert attachment.quota_state == ("reserved" if fault == "put" else "committed")
        assert rows["extractions"][0].state == "deleting"
        cleanup = [t for t in rows["tasks"] if t.kind == "delete_extraction"]
        assert len(cleanup) == 1 and cleanup[0].storage_key is None
        assert cleanup[0].resource_id == reservation.extraction_id
        await h.store.enqueue_cleanup(reservation.extraction_id, project_id=h.project_id)
        assert len([t for t in (await h.read_rows())["tasks"] if t.kind == "delete_extraction"]) == 1


@pytest.mark.asyncio
async def test_untrusted_asset_and_scope_never_put(postgres_database_url, tmp_path):
    async with extraction_harness(postgres_database_url) as h:
        reservation = await begin(h)
        asset = write_test_asset(tmp_path)
        variants = [
            asset.model_copy(update={"relative_path": "../asset.png"}),
            asset.model_copy(update={"attachment": asset.attachment.model_copy(update={"width": 2})}),
            asset.model_copy(update={"attachment": asset.attachment.model_copy(update={"ref": "f" * 64})}),
        ]
        for invalid in variants:
            with pytest.raises(KnowledgeError):
                await h.store.persist_attachment(reservation, invalid, work_dir=tmp_path)
        with pytest.raises(KnowledgeError):
            await h.store.persist_attachment(replace(reservation, project_id=uuid.uuid4()), asset, work_dir=tmp_path)
        assert not h.object_store.calls


@pytest.mark.asyncio
async def test_cleanup_refuses_live_pin(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        reservation = await begin(h)
        with pytest.raises(KnowledgeError) as error:
            await h.store.enqueue_cleanup(reservation.extraction_id, project_id=h.project_id)
        assert error.value.code == KNOWLEDGE_CONFLICT
        assert (await h.read_rows())["extractions"][0].state == "staging"


@pytest.mark.asyncio
async def test_settlement_releases_only_owned_task_pin(postgres_database_url):
    from actweave_knowledge.persistence.tasks import settle_task_failure

    async with extraction_harness(postgres_database_url) as h:
        await begin(h)
        async with h.session_factory() as session, session.begin():
            await settle_task_failure(session, h.claim.id, h.claim.claim_token, error_message="retry", retry_delay_seconds=0)
        assert (await h.read_rows())["tasks"][0].extraction_id is None


@pytest.mark.asyncio
async def test_post_put_database_outage_keeps_pending_quota(postgres_database_url, tmp_path):
    from sqlalchemy.exc import SQLAlchemyError

    async with extraction_harness(postgres_database_url) as h:
        reservation = await begin(h)
        gate = h.object_store.pause("put")
        pending = asyncio.create_task(h.store.persist_attachment(reservation, write_test_asset(tmp_path), work_dir=tmp_path))
        async with asyncio.timeout(10):
            await gate.entered.wait()

            def unavailable():
                raise SQLAlchemyError("test database unavailable")

            original = h.store._sessions
            h.store._sessions = unavailable
            gate.released.set()
            try:
                with pytest.raises(SQLAlchemyError):
                    await pending
            finally:
                h.store._sessions = original
        row = (await h.read_rows())["attachments"][0]
        assert row.upload_state == "pending" and row.quota_state == "reserved"
        assert row.storage_key in h.object_store.objects


def test_derived_keys_require_every_scope_and_never_pass_original_grammar():
    from actweave_knowledge.storage.extraction_keys import attachment_storage_key, is_extraction_storage_key, manifest_storage_key
    from actweave_knowledge.storage.minio_store import is_document_storage_key

    p, b, d, e = [uuid.uuid4() for _ in range(4)]
    scope = dict(project_id=p, base_id=b, document_id=d, extraction_id=e)
    for key in (attachment_storage_key(p, b, d, e, "a" * 64, "image/png"), manifest_storage_key(p, b, d, e)):
        assert is_extraction_storage_key(key, **scope)
        assert not is_document_storage_key(key, project_id=p, document_id=d)
        for field in scope:
            assert not is_extraction_storage_key(key, **(scope | {field: uuid.uuid4()}))
        assert not is_extraction_storage_key(key + "/../manifest.json", **scope)


@pytest.mark.asyncio
async def test_begin_rejects_mismatched_creation_identity(postgres_database_url):
    async with extraction_harness(postgres_database_url) as h:
        source = (await h.read_rows())["documents"][0].source_sha256
        profile = make_parse_profile(".pdf")
        for claim in (replace(h.claim, claim_token=uuid.uuid4()), replace(h.claim, attempt_count=2), replace(h.claim, target_version=2), replace(h.claim, resource_id=uuid.uuid4()), replace(h.claim, project_id=uuid.uuid4())):
            with pytest.raises(KnowledgeError):
                await h.store.begin(claim, source_sha256=source, profile=profile)
        with pytest.raises(KnowledgeError):
            await h.store.begin(h.claim, source_sha256="f" * 64, profile=profile)
        await begin(h)
        with pytest.raises(KnowledgeError):
            await h.store.begin(h.claim, source_sha256=source, profile=profile.model_copy(update={"normalization_version": "other"}))
        assert len((await h.read_rows())["extractions"]) == 1


@pytest.mark.asyncio
async def test_asset_symlink_rejected_before_registration(postgres_database_url, tmp_path):
    async with extraction_harness(postgres_database_url) as h:
        reservation = await begin(h)
        asset = write_test_asset(tmp_path)
        (tmp_path / "linked.png").symlink_to(tmp_path / "asset.png")
        with pytest.raises(KnowledgeError):
            await h.store.persist_attachment(reservation, asset.model_copy(update={"relative_path": "linked.png"}), work_dir=tmp_path)
        assert not (await h.read_rows())["attachments"]
        assert not h.object_store.calls


@pytest.mark.asyncio
async def test_cleanup_does_not_clear_new_claim_pin(postgres_database_url, tmp_path):
    from actweave_knowledge.persistence.models import KnowledgeTaskRow

    async with extraction_harness(postgres_database_url) as h:
        reservation = await begin(h)
        gate = h.object_store.pause("put")
        pending = asyncio.create_task(h.store.persist_attachment(reservation, write_test_asset(tmp_path), work_dir=tmp_path))
        async with asyncio.timeout(10):
            await gate.entered.wait()
            token = uuid.uuid4()
            async with h.session_factory() as session, session.begin():
                task = await session.get(KnowledgeTaskRow, h.claim.id, with_for_update=True)
                task.claim_token = token
                task.attempt_count = 2
            gate.released.set()
            with pytest.raises(KnowledgeError):
                await pending
        rows = await h.read_rows()
        assert rows["tasks"][0].claim_token == token
        assert rows["tasks"][0].extraction_id == reservation.extraction_id
        assert not [t for t in rows["tasks"] if t.kind == "delete_extraction"]
        assert rows["extractions"][0].state == "staging"
        assert rows["attachments"][0].quota_state == "committed"


@pytest.mark.asyncio
async def test_inactive_project_still_takes_real_project_fence(postgres_database_url):
    from sqlalchemy.exc import DBAPIError

    from app.knowledge.composition import is_knowledge_project_active

    async with extraction_harness(postgres_database_url) as h:
        async with h.session_factory() as session, session.begin():
            await session.execute(text("UPDATE projects SET status='pending_deletion' WHERE id=:id"), {"id": h.project_id})
        async with h.session_factory() as fenced, fenced.begin():
            assert await is_knowledge_project_active(fenced, h.project_id) is False
            async with h.session_factory() as competing:
                with pytest.raises(DBAPIError):
                    await competing.execute(text("SELECT id FROM projects WHERE id=:id FOR UPDATE NOWAIT"), {"id": h.project_id})
        with pytest.raises(KnowledgeError):
            await begin(h)
        assert not h.object_store.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [1, 3])
async def test_project_resume_same_attempt_creates_independent_claim_generation(postgres_database_url, tmp_path, attempt):
    from actweave_knowledge.persistence.models import KnowledgeTaskRow
    from actweave_knowledge.persistence.tasks import claim_next_task, defer_running_task_for_inactive_project
    from actweave_knowledge.tasks.worker import KnowledgeProjectInactive

    async with extraction_harness(postgres_database_url) as h:
        async with h.session_factory() as session, session.begin():
            task = await session.get(KnowledgeTaskRow, h.claim.id)
            task.attempt_count = attempt
        h.claim = replace(h.claim, attempt_count=attempt)
        original_claim = h.claim
        original = await begin(h)
        async with h.session_factory() as session, session.begin():
            await session.execute(text("UPDATE projects SET status='pending_deletion' WHERE id=:id"), {"id": h.project_id})
        with pytest.raises(KnowledgeProjectInactive):
            await begin(h)
        async with h.session_factory() as session, session.begin():
            assert await defer_running_task_for_inactive_project(session, h.claim.id, h.claim.claim_token)
        async with h.session_factory() as session, session.begin():
            await session.execute(text("UPDATE projects SET status='active' WHERE id=:id"), {"id": h.project_id})
            await session.execute(text("UPDATE knowledge_tasks SET available_at=clock_timestamp() WHERE id=:id"), {"id": h.claim.id})
            task = await claim_next_task(session, lease_seconds=600)
            assert task.attempt_count == attempt
            assert task.claim_token != original_claim.claim_token
            h.claim = replace(original_claim, claim_token=task.claim_token)
        resumed = await begin(h)
        assert await begin(h) == resumed
        assert resumed.extraction_id != original.extraction_id
        with pytest.raises(KnowledgeError):
            await h.store.persist_attachment(original, write_test_asset(tmp_path), work_dir=tmp_path)
        rows = await h.read_rows()
        assert len(rows["extractions"]) == 2
        old = next(row for row in rows["extractions"] if row.id == original.extraction_id)
        assert old.created_claim_token == original_claim.claim_token
        assert old.created_attempt == attempt and old.state == "staging"
        assert rows["tasks"][0].extraction_id == resumed.extraction_id
        assert rows["tasks"][0].attempt_count == attempt
        assert rows["documents"][0].status != "failed"
        assert not h.object_store.calls
