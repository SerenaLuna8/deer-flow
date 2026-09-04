"""Real publication/binding authorization with only object bytes doubled."""

import asyncio
import hashlib
from uuid import uuid4

import pytest
from actweave_knowledge import KNOWLEDGE_CONFLICT, KNOWLEDGE_NOT_FOUND, KNOWLEDGE_STORAGE_UNAVAILABLE, KnowledgeError
from actweave_knowledge.persistence.models import KnowledgeAttachmentRow, KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeExtractionRow, KnowledgeSegmentAttachmentRow, KnowledgeSegmentRow
from extraction_test_helpers import extraction_harness
from sqlalchemy import delete, select, text


def read_call(service, h, kind, seeded, output, **overrides):
    segment_id, attachment_id, digest, authority = seeded
    expected = {"expected_document_version": 1, "expected_content_digest": digest, "authority": authority} | overrides
    if kind == "citation":
        return service.download_citation(h.project_id, h.base_id, h.document_id, segment_id, attachment_id, output, **expected)
    return service.download_managed(h.project_id, h.document_id, segment_id, attachment_id, output, **expected)


def read_service(h):
    from actweave_knowledge.storage.attachment_reads import KnowledgeAttachmentReadService

    return KnowledgeAttachmentReadService(session_factory=h.session_factory, object_store=h.object_store)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["managed", "citation"])
async def test_bound_attachment_reads_actual_bytes_and_allows_repeated_occurrence(postgres_database_url, tmp_path, kind):
    async with extraction_harness(postgres_database_url) as h:
        seeded = await h.seed_attachment_read(tmp_path)
        async with h.session_factory() as session, session.begin():
            binding = await session.get(KnowledgeSegmentAttachmentRow, (seeded[0], 1))
            segment = await session.get(KnowledgeSegmentRow, seeded[0])
            segment.content += "\n\n" + segment.content
            session.add(
                KnowledgeSegmentAttachmentRow(project_id=h.project_id, knowledge_base_id=h.base_id, knowledge_document_id=h.document_id, extraction_id=binding.extraction_id, segment_id=seeded[0], attachment_id=seeded[1], position=2)
            )
            seeded = seeded[:2] + (hashlib.sha256(segment.content.encode()).hexdigest(), seeded[3])
        output = tmp_path / "download.png"
        metadata = await read_call(read_service(h), h, kind, seeded, output)
        assert output.read_bytes() == (tmp_path / "asset.png").read_bytes()
        assert metadata.media_type == "image/png" and metadata.size_bytes == len(output.read_bytes())
        assert not hasattr(metadata, "storage_key")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["managed", "citation"])
@pytest.mark.parametrize("disabled", ["base", "document", "segment", "failed"])
async def test_retained_publication_visible_only_to_management(postgres_database_url, tmp_path, kind, disabled):
    async with extraction_harness(postgres_database_url) as h:
        seeded = await h.seed_attachment_read(tmp_path)
        async with h.session_factory() as session, session.begin():
            if disabled == "base":
                (await session.get(KnowledgeBaseRow, h.base_id)).status = "disabled"
            elif disabled == "segment":
                (await session.get(KnowledgeSegmentRow, seeded[0])).enabled = False
            else:
                document = await session.get(KnowledgeDocumentRow, h.document_id)
                if disabled == "document":
                    document.enabled = False
                else:
                    document.status, document.version = disabled, 2
                    document.error_message = "解析失败" if disabled == "failed" else None
        output = tmp_path / "download.png"
        if kind == "managed":
            await read_call(read_service(h), h, kind, seeded, output)
            assert output.read_bytes() == (tmp_path / "asset.png").read_bytes()
        else:
            with pytest.raises(KnowledgeError) as error:
                await read_call(read_service(h), h, kind, seeded, output)
            assert error.value.code == KNOWLEDGE_CONFLICT
            assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,code",
    [
        ("authority", KNOWLEDGE_NOT_FOUND),
        ("project", KNOWLEDGE_NOT_FOUND),
        ("base", KNOWLEDGE_NOT_FOUND),
        ("document", KNOWLEDGE_NOT_FOUND),
        ("binding", KNOWLEDGE_NOT_FOUND),
        ("segment", KNOWLEDGE_CONFLICT),
        ("generation", KNOWLEDGE_CONFLICT),
        ("extraction", KNOWLEDGE_NOT_FOUND),
        ("attachment", KNOWLEDGE_NOT_FOUND),
        ("sha", KNOWLEDGE_CONFLICT),
    ],
)
async def test_copy_revalidates_full_binding_after_io(postgres_database_url, tmp_path, change, code):
    async with extraction_harness(postgres_database_url) as h:
        seeded = await h.seed_attachment_read(tmp_path)
        gate = h.object_store.pause("get")
        output = tmp_path / "download.png"
        pending = asyncio.create_task(read_call(read_service(h), h, "citation", seeded, output))
        await asyncio.wait_for(gate.entered.wait(), timeout=5)
        try:
            # This independent transaction must complete while GET is blocked.
            # It also proves no read transaction holds resource locks over I/O.
            async with h.session_factory() as session, session.begin():
                if change == "authority":
                    seeded[3].revoked = True
                elif change == "project":
                    await session.execute(text("UPDATE projects SET status='pending_deletion' WHERE id=:id"), {"id": h.project_id})
                elif change == "base":
                    (await session.get(KnowledgeBaseRow, h.base_id)).status = "deleting"
                elif change == "document":
                    (await session.get(KnowledgeDocumentRow, h.document_id)).status = "deleting"
                elif change == "binding":
                    await session.execute(delete(KnowledgeSegmentAttachmentRow).where(KnowledgeSegmentAttachmentRow.segment_id == seeded[0]))
                elif change == "segment":
                    (await session.get(KnowledgeSegmentRow, seeded[0])).content = "已修改"
                elif change == "generation":
                    document = await session.get(KnowledgeDocumentRow, h.document_id)
                    document.version = document.published_version = 2
                    (await session.get(KnowledgeSegmentRow, seeded[0])).document_version = 2
                elif change == "extraction":
                    attachment = await session.get(KnowledgeAttachmentRow, seeded[1])
                    (await session.get(KnowledgeExtractionRow, attachment.extraction_id)).state = "deleting"
                else:
                    attachment = await session.get(KnowledgeAttachmentRow, seeded[1])
                    if change == "attachment":
                        attachment.state = "deleting"
                    else:
                        attachment.sha256 = "b" * 64
        finally:
            gate.released.set()
        with pytest.raises(KnowledgeError) as error:
            await pending
        assert error.value.code == code
        assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("attachment_scope", ["wrong", "unbound", "deleting"])
async def test_attachment_scope_is_hidden_before_stale_expectations(
    postgres_database_url,
    tmp_path,
    attachment_scope,
):
    async with extraction_harness(postgres_database_url) as h:
        seeded = await h.seed_attachment_read(tmp_path)
        if attachment_scope == "wrong":
            seeded = (seeded[0], uuid4(), seeded[2], seeded[3])
        else:
            async with h.session_factory() as session, session.begin():
                if attachment_scope == "unbound":
                    await session.execute(
                        delete(KnowledgeSegmentAttachmentRow).where(
                            KnowledgeSegmentAttachmentRow.segment_id == seeded[0],
                        )
                    )
                else:
                    (await session.get(KnowledgeAttachmentRow, seeded[1])).state = "deleting"
        overrides = {"expected_content_digest": "b" * 64}
        h.object_store.calls.clear()
        output = tmp_path / "hidden.png"
        with pytest.raises(KnowledgeError) as error:
            await read_call(read_service(h), h, "managed", seeded, output, **overrides)
        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert h.object_store.calls == []
        assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch,code", [("segment", KNOWLEDGE_NOT_FOUND), ("attachment", KNOWLEDGE_NOT_FOUND), ("authority", KNOWLEDGE_NOT_FOUND), ("version", KNOWLEDGE_CONFLICT), ("digest", KNOWLEDGE_CONFLICT), ("unbound", KNOWLEDGE_NOT_FOUND)]
)
async def test_unmatched_scope_and_expected_never_read_objects(postgres_database_url, tmp_path, mismatch, code):
    async with extraction_harness(postgres_database_url) as h:
        seeded = await h.seed_attachment_read(tmp_path)
        overrides = {}
        if mismatch == "segment":
            seeded = (uuid4(), *seeded[1:])
        elif mismatch == "attachment":
            seeded = (seeded[0], uuid4(), *seeded[2:])
        elif mismatch == "authority":
            seeded[3].project_id = uuid4()
        elif mismatch == "version":
            overrides["expected_document_version"] = 2
        elif mismatch == "digest":
            overrides["expected_content_digest"] = "b" * 64
        else:
            async with h.session_factory() as session, session.begin():
                await session.execute(delete(KnowledgeSegmentAttachmentRow).where(KnowledgeSegmentAttachmentRow.segment_id == seeded[0]))
        h.object_store.calls.clear()
        output = tmp_path / "download.png"
        output.touch()
        with pytest.raises(KnowledgeError) as error:
            await read_call(read_service(h), h, "citation", seeded, output, **overrides)
        assert error.value.code == code
        assert h.object_store.calls == []
        assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["corrupt", "oversize", "missing", "cancel"])
async def test_download_failure_or_cancel_removes_output(postgres_database_url, tmp_path, failure):
    async with extraction_harness(postgres_database_url) as h:
        seeded = await h.seed_attachment_read(tmp_path)
        attachment = (await h.read_rows())["attachments"][0]
        if failure == "corrupt":
            h.object_store.objects[attachment.storage_key] = b"invalid" + h.object_store.objects[attachment.storage_key][7:]
        elif failure == "oversize":
            h.object_store.objects[attachment.storage_key] = b"x" * (5 * 1024 * 1024 + 1)
        elif failure == "missing":
            del h.object_store.objects[attachment.storage_key]
        output = tmp_path / "download.png"
        output.touch()
        if failure == "cancel":
            gate = h.object_store.pause("get")
            pending = asyncio.create_task(read_call(read_service(h), h, "managed", seeded, output))
            await asyncio.wait_for(gate.entered.wait(), timeout=5)
            pending.cancel()
            gate.released.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            with pytest.raises(KnowledgeError) as error:
                await read_call(read_service(h), h, "managed", seeded, output)
            assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE
        assert not output.exists()


@pytest.mark.asyncio
async def test_cached_extraction_can_publish_a_later_document_generation(postgres_database_url, tmp_path):
    """Creation target is immutable task evidence, not publication generation."""
    async with extraction_harness(postgres_database_url) as h:
        seeded = await h.seed_attachment_read(tmp_path)
        async with h.session_factory() as session, session.begin():
            extraction = await session.scalar(select(KnowledgeExtractionRow).where(KnowledgeExtractionRow.id == (await session.get(KnowledgeSegmentRow, seeded[0])).extraction_id))
            assert extraction.target_document_version == 1
            document = await session.get(KnowledgeDocumentRow, h.document_id)
            document.version = document.published_version = 2
            (await session.get(KnowledgeSegmentRow, seeded[0])).document_version = 2
        seeded = seeded[:2] + (seeded[2], seeded[3])
        output = tmp_path / "cached-generation.png"
        metadata = await read_call(read_service(h), h, "managed", seeded, output, expected_document_version=2)
        assert metadata.media_type == "image/png"
        assert output.read_bytes() == (tmp_path / "asset.png").read_bytes()
