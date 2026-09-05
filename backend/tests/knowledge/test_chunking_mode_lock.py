"""Base-wide chunking mode: fixed by the first document, switched only by a
base-wide reparse.

Mixing general and parent_child documents in one base makes their native
recall scores incomparable, so the mode is a base invariant: the first live
document determines it, every later upload and per-document reparse must
match, and only ``reparse_knowledge_base`` — an all-or-nothing admission over
every document — changes it. Everything runs against the installed Schema V1
snapshot in a disposable PostgreSQL database with a fake object store.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_INVALID_REQUEST,
    KnowledgeBaseReparseRequest,
    KnowledgeError,
    KnowledgeReparseRequest,
    KnowledgeSettings,
)
from actweave_knowledge.bases import KnowledgeBaseService
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeTaskRow,
)
from registry_helpers import registry_model_port
from sqlalchemy import func, select, update
from test_upload import _harness, _seed_base, _table_counts, _upload


def _base_service(harness) -> KnowledgeBaseService:  # noqa: ANN001 - upload harness
    return KnowledgeBaseService(
        session_factory=harness.factory,
        settings=KnowledgeSettings.model_validate({"enabled": False}),
        model_port=registry_model_port(),
    )


async def _stored_base_mode(harness, base_id: uuid.UUID) -> str | None:  # noqa: ANN001
    async with harness.factory() as session:
        return await session.scalar(select(KnowledgeBaseRow.chunking_mode).where(KnowledgeBaseRow.id == base_id))


async def _settle_as_ready(harness, document_id: uuid.UUID) -> None:  # noqa: ANN001
    """Stand in for the Worker: publish the queued generation and close its task."""

    async with harness.factory() as session, session.begin():
        row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == document_id).with_for_update())
        assert row is not None
        row.status = "ready"
        row.published_version = row.version
        row.error_message = None
        await session.execute(update(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == document_id, KnowledgeTaskRow.status.in_(("queued", "running", "retry_wait"))).values(status="succeeded", finished_at=func.now()))


async def _open_tasks(harness, document_id: uuid.UUID) -> list[KnowledgeTaskRow]:  # noqa: ANN001
    async with harness.factory() as session:
        return list((await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == document_id, KnowledgeTaskRow.status == "queued").order_by(KnowledgeTaskRow.created_at))).all())


# ---------------------------------------------------------------------------
# Upload admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_upload_fixes_the_base_mode_and_later_uploads_must_match(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        bases = _base_service(harness)
        assert (await bases.get_knowledge_base(project_id, base_id)).chunking_mode is None
        assert await _stored_base_mode(harness, base_id) is None

        first = await harness.service.upload_document(
            project_id,
            base_id,
            _upload(tmp_path, chunking_mode="parent_child", child_chunk_size=300),
        )
        assert first.chunking_mode == "parent_child"
        assert await _stored_base_mode(harness, base_id) == "parent_child"
        assert (await bases.get_knowledge_base(project_id, base_id)).chunking_mode == "parent_child"

        # A different mode is refused before any row or object is created.
        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path, name="普通文档", chunking_mode="general"))
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert "分段模式已锁定" in error.value.message
        assert "父子分段" in error.value.message
        assert await _table_counts(harness) == (1, 1)
        assert len(harness.store.uploads) == 1

        # The same mode with its own parameters is still a per-document choice.
        second = await harness.service.upload_document(
            project_id,
            base_id,
            _upload(tmp_path, name="第二份", chunking_mode="parent_child", child_chunk_size=200, chunk_size=800),
        )
        assert second.chunking_mode == "parent_child"
        assert second.chunk_size == 800
        assert await _table_counts(harness) == (2, 2)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_an_emptied_base_is_determined_again_by_its_next_upload(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        bases = _base_service(harness)
        document = await harness.service.upload_document(project_id, base_id, _upload(tmp_path, chunking_mode="general"))
        assert await _stored_base_mode(harness, base_id) == "general"

        # A document being deleted no longer holds the mode: the view reports
        # the base as undetermined and the next upload may choose freely.
        await _settle_as_ready(harness, document.id)
        await harness.service.delete_document(project_id, document.id)
        assert (await bases.get_knowledge_base(project_id, base_id)).chunking_mode is None
        assert await _stored_base_mode(harness, base_id) == "general"

        replacement = await harness.service.upload_document(
            project_id,
            base_id,
            _upload(tmp_path, name="重新开始", chunking_mode="parent_child", child_chunk_size=300),
        )
        assert replacement.chunking_mode == "parent_child"
        assert await _stored_base_mode(harness, base_id) == "parent_child"
        assert (await bases.get_knowledge_base(project_id, base_id)).chunking_mode == "parent_child"
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Per-document reparse and retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_document_reparse_cannot_switch_the_base_mode(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        document = await harness.service.upload_document(project_id, base_id, _upload(tmp_path, chunking_mode="general"))
        await _settle_as_ready(harness, document.id)

        for attempt in (
            harness.service.preview_reparse(project_id, document.id, KnowledgeReparseRequest(expected_version=1, chunking_mode="parent_child", child_chunk_size=300)),
            harness.service.reparse_document(project_id, document.id, KnowledgeReparseRequest(expected_version=1, chunking_mode="parent_child", child_chunk_size=300)),
        ):
            with pytest.raises(KnowledgeError) as error:
                await attempt
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST
            assert "分段模式已锁定" in error.value.message
        assert await _open_tasks(harness, document.id) == []

        # Same mode, new parameters: the ordinary explicit reparse.
        queued = await harness.service.reparse_document(project_id, document.id, KnowledgeReparseRequest(expected_version=1, chunking_mode="general", chunk_size=600))
        assert queued.status == "queued"
        assert queued.version == 2
        [task] = await _open_tasks(harness, document.id)
        assert task.reparse_settings["chunking_mode"] == "general"
        assert task.reparse_settings["chunk_size"] == 600
        assert await _stored_base_mode(harness, base_id) == "general"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_failed_ingest_retry_under_a_stale_mode_points_at_chunk_settings(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        document = await harness.service.upload_document(project_id, base_id, _upload(tmp_path, chunking_mode="general"))
        async with harness.factory() as session, session.begin():
            row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == document.id).with_for_update())
            assert row is not None
            row.status = "failed"
            row.error_message = "解析失败"
            await session.execute(update(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == document.id).values(status="failed", finished_at=func.now()))
            # The base moved on to parent_child while this document was stuck.
            await session.execute(update(KnowledgeBaseRow).where(KnowledgeBaseRow.id == base_id).values(chunking_mode="parent_child"))

        with pytest.raises(KnowledgeError) as error:
            await harness.service.retry_document(project_id, document.id)
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert "分段设置" in error.value.message
        assert await _open_tasks(harness, document.id) == []
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Base-wide reparse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_wide_reparse_switches_the_mode_and_freezes_one_parameter_set_per_document(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        bases = _base_service(harness)
        text_document = await harness.service.upload_document(project_id, base_id, _upload(tmp_path, chunking_mode="general", chunk_separator="。"))
        markdown_document = await harness.service.upload_document(
            project_id,
            base_id,
            _upload(tmp_path, name="说明", original_name="guide.md", media_type="text/markdown", chunking_mode="general"),
        )
        for document in (text_document, markdown_document):
            await _settle_as_ready(harness, document.id)
        # A failed document with a stored original is re-parsed as well.
        async with harness.factory() as session, session.begin():
            row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == markdown_document.id).with_for_update())
            assert row is not None
            row.status = "failed"
            row.error_message = "上次解析失败"

        result = await harness.service.reparse_knowledge_base(
            project_id,
            base_id,
            KnowledgeBaseReparseRequest(chunking_mode="parent_child", chunk_size=800, chunk_overlap=50, child_chunk_size=200, child_chunk_separator="。", remove_extra_spaces=True),
        )
        assert result == 2
        assert await _stored_base_mode(harness, base_id) == "parent_child"
        assert (await bases.get_knowledge_base(project_id, base_id)).chunking_mode == "parent_child"

        async with harness.factory() as session:
            rows = {row.id: row for row in (await session.scalars(select(KnowledgeDocumentRow))).all()}
        for document in (text_document, markdown_document):
            row = rows[document.id]
            assert (row.status, row.version, row.error_message) == ("queued", 2, None)
            # Stored parameters describe the published rows until the new
            # generation publishes; only the task carries the new set.
            assert row.chunking_mode == "general"
            [task] = await _open_tasks(harness, document.id)
            assert (task.kind, task.target_version) == ("ingest_document", 2)
            assert task.reparse_settings["chunking_mode"] == "parent_child"
            assert task.reparse_settings["chunk_size"] == 800
            assert task.reparse_settings["chunk_overlap"] == 50
            assert task.reparse_settings["child_chunk_size"] == 200
            assert task.reparse_settings["child_chunk_separator"] == "。"
            assert task.reparse_settings["remove_extra_spaces"] is True
            assert task.reparse_settings["processing_profile"]["chunk"]["mode"] == "parent_child"
        # Parser identity follows each file's own extension.
        [text_task] = await _open_tasks(harness, text_document.id)
        [markdown_task] = await _open_tasks(harness, markdown_document.id)
        assert text_task.reparse_settings["processing_profile"]["parse"]["extractor_id"] != markdown_task.reparse_settings["processing_profile"]["parse"]["extractor_id"]

        # Later uploads follow the switched mode even before anything republishes.
        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path, name="旧模式", chunking_mode="general"))
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        # Nothing may run twice: the open tasks block a second switch.
        with pytest.raises(KnowledgeError) as blocked:
            await harness.service.reparse_knowledge_base(project_id, base_id, KnowledgeBaseReparseRequest(chunking_mode="general"))
        assert blocked.value.code == KNOWLEDGE_INVALID_REQUEST
        assert "状态的文档" in blocked.value.message
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_base_wide_reparse_is_all_or_nothing_and_needs_documents(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        with pytest.raises(KnowledgeError) as empty:
            await harness.service.reparse_knowledge_base(project_id, base_id, KnowledgeBaseReparseRequest(chunking_mode="parent_child", child_chunk_size=300))
        assert empty.value.code == KNOWLEDGE_INVALID_REQUEST
        assert "还没有文档" in empty.value.message
        assert await _stored_base_mode(harness, base_id) is None

        settled = await harness.service.upload_document(project_id, base_id, _upload(tmp_path, chunking_mode="general"))
        await _settle_as_ready(harness, settled.id)
        # The second upload is still queued with an open ingest task.
        pending = await harness.service.upload_document(project_id, base_id, _upload(tmp_path, name="处理中", chunking_mode="general"))

        with pytest.raises(KnowledgeError) as in_flight:
            await harness.service.reparse_knowledge_base(project_id, base_id, KnowledgeBaseReparseRequest(chunking_mode="parent_child", child_chunk_size=300))
        assert in_flight.value.code == KNOWLEDGE_INVALID_REQUEST
        assert "queued" in in_flight.value.message
        # Nothing moved: the settled document keeps its version and the base its mode.
        async with harness.factory() as session:
            rows = {row.id: row for row in (await session.scalars(select(KnowledgeDocumentRow))).all()}
        assert (rows[settled.id].status, rows[settled.id].version) == ("ready", 1)
        assert (rows[pending.id].status, rows[pending.id].version) == ("queued", 1)
        assert await _stored_base_mode(harness, base_id) == "general"

        # Invalid parameter sets are rejected before any lock is taken.
        with pytest.raises(KnowledgeError) as invalid:
            await harness.service.reparse_knowledge_base(project_id, base_id, KnowledgeBaseReparseRequest(chunking_mode="parent_child", chunk_size=300, child_chunk_size=300))
        assert invalid.value.code == KNOWLEDGE_INVALID_REQUEST

        # A document set that changes between resolving profiles and locking
        # conflicts instead of admitting a stale plan.
        await _settle_as_ready(harness, pending.id)
        original = harness.service._session_factory

        class _RacingFactory:
            """Deletes a document between the snapshot and the write transaction."""

            def __init__(self) -> None:
                self.calls = 0

            def __call__(self):  # noqa: ANN204
                self.calls += 1
                if self.calls == 2:
                    return _DeleteThenOpen()
                return original()

        class _DeleteThenOpen:
            async def __aenter__(self):  # noqa: ANN204
                async with original() as session, session.begin():
                    row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == pending.id).with_for_update())
                    assert row is not None
                    row.status = "deleting"
                    row.version = row.version + 1
                self._session = original()
                return await self._session.__aenter__()

            async def __aexit__(self, *exc_info):  # noqa: ANN002, ANN204
                return await self._session.__aexit__(*exc_info)

        harness.service._session_factory = _RacingFactory()  # type: ignore[assignment]
        try:
            with pytest.raises(KnowledgeError) as raced:
                await harness.service.reparse_knowledge_base(project_id, base_id, KnowledgeBaseReparseRequest(chunking_mode="parent_child", child_chunk_size=300))
        finally:
            harness.service._session_factory = original
        assert raced.value.code == KNOWLEDGE_CONFLICT
        assert await _stored_base_mode(harness, base_id) == "general"
        async with harness.factory() as session:
            assert await session.scalar(select(func.count()).select_from(KnowledgeTaskRow).where(KnowledgeTaskRow.status == "queued", KnowledgeTaskRow.kind == "ingest_document")) == 0
    finally:
        await harness.engine.dispose()
