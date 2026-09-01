"""Frozen upload/reparse identities with real PostgreSQL and a local object store."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from actweave_knowledge.contracts import KnowledgeError, KnowledgeReparseRequest
from actweave_knowledge.extraction.registry import default_registry
from actweave_knowledge.ingestion.profiles import ProcessingParameters, build_file_capabilities, preview_fingerprint, resolve_processing_profile
from actweave_knowledge.persistence.models import KnowledgeDocumentRow, KnowledgeTaskRow
from sqlalchemy import select
from test_upload import _harness, _seed_base, _table_counts, _upload


@pytest.mark.asyncio
async def test_file_b_cannot_reuse_file_a_preview_before_any_row_or_object(postgres_database_url, tmp_path):
    harness = await _harness(postgres_database_url)
    try:
        project, base = await _seed_base(harness)
        upload = _upload(tmp_path)
        registry = default_registry()
        profile = resolve_processing_profile(harness.service._settings, ProcessingParameters(), registry, extension=".txt")
        revision = build_file_capabilities(harness.service._settings, registry).capability_revision
        fingerprint = preview_fingerprint(source_sha256=hashlib.sha256(b"not-the-upload").hexdigest(), extension=".txt", profile=profile, capability_revision=revision)
        upload = replace(upload, expected_preview_fingerprint=fingerprint)
        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project, base, upload)
        assert error.value.code == "KNOWLEDGE_CONFLICT"
        assert await _table_counts(harness) == (0, 0)
        assert harness.store.objects == {}
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_headless_upload_and_retry_keep_original_profile_after_etl_changes(postgres_database_url, tmp_path):
    harness = await _harness(postgres_database_url)
    try:
        project, base = await _seed_base(harness)
        uploaded = await harness.service.upload_document(project, base, _upload(tmp_path))
        assert uploaded.parsing_profile.parse.etl_type == "builtin"
        async with harness.factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, uploaded.id)
            original = document.parsing_profile
            document.status = "failed"
            document.error_message = "fixture failure"
            task = await session.scalar(select(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == uploaded.id))
            assert task.reparse_settings is None
            task.status = "failed"
            task.finished_at = datetime.now(UTC)
        harness.service._settings.etl_type = "unstructured_local"
        retried = await harness.service.retry_document(project, uploaded.id)
        assert retried.parsing_profile.parse.etl_type == "builtin"
        async with harness.factory() as session:
            document = await session.get(KnowledgeDocumentRow, uploaded.id)
            assert document.parsing_profile == original
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reparse_freezes_new_profile_on_task_without_changing_published_profile(postgres_database_url, tmp_path):
    harness = await _harness(postgres_database_url)
    try:
        project, base = await _seed_base(harness)
        uploaded = await harness.service.upload_document(project, base, _upload(tmp_path))
        async with harness.factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, uploaded.id)
            original = document.parsing_profile
            document.status = "failed"
            document.error_message = "fixture failure"
            task = await session.scalar(select(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == uploaded.id))
            task.status = "failed"
            task.finished_at = datetime.now(UTC)
        request = KnowledgeReparseRequest(expected_version=1, processing_profile=ProcessingParameters(size=800))
        await harness.service.reparse_document(project, uploaded.id, request)
        async with harness.factory() as session:
            document = await session.get(KnowledgeDocumentRow, uploaded.id)
            assert document.parsing_profile == original
            task = await session.scalar(select(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == uploaded.id, KnowledgeTaskRow.status == "queued"))
            assert task.reparse_settings["processing_profile"]["chunk"]["size"] == 800
            assert task.reparse_settings["chunk_size"] == 800
            assert len(task.reparse_settings["capability_revision"]) == 64
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_matching_preview_upload_freezes_exact_effective_profile(postgres_database_url, tmp_path):
    harness = await _harness(postgres_database_url)
    try:
        project, base = await _seed_base(harness)
        parameters = ProcessingParameters(size=800)
        upload = replace(_upload(tmp_path), processing_profile=parameters)
        registry = default_registry()
        profile = resolve_processing_profile(harness.service._settings, parameters, registry, extension=".txt")
        revision = build_file_capabilities(harness.service._settings, registry).capability_revision
        expected = preview_fingerprint(source_sha256=hashlib.sha256(upload.source_path.read_bytes()).hexdigest(), extension=".txt", profile=profile, capability_revision=revision)
        view = await harness.service.upload_document(project, base, replace(upload, expected_preview_fingerprint=expected))
        assert view.parsing_profile == profile and view.chunk_size == 800
        assert await _table_counts(harness) == (1, 1)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_retry_historical_missing_profile_requires_explicit_reparse_without_mutation(postgres_database_url, tmp_path):
    harness = await _harness(postgres_database_url)
    try:
        project, base = await _seed_base(harness)
        view = await harness.service.upload_document(project, base, _upload(tmp_path))
        async with harness.factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, view.id)
            document.parsing_profile = None
            document.status = "failed"
            document.error_message = "historical failure"
            task = await session.scalar(select(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == view.id))
            task.status = "failed"
            task.finished_at = datetime.now(UTC)
        with pytest.raises(KnowledgeError) as error:
            await harness.service.retry_document(project, view.id)
        assert error.value.reason_code == "PROCESSING_PROFILE_UNAVAILABLE"
        async with harness.factory() as session:
            document = await session.get(KnowledgeDocumentRow, view.id)
            assert document.version == 1 and document.status == "failed"
        assert await _table_counts(harness) == (1, 1)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reparse", "retry"])
async def test_processing_resource_checks_do_not_hold_database_transactions(postgres_database_url, tmp_path, monkeypatch, operation):
    from actweave_knowledge.ingestion import profiles

    harness = await _harness(postgres_database_url)
    try:
        project, base = await _seed_base(harness)
        view = await harness.service.upload_document(project, base, _upload(tmp_path))
        async with harness.factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, view.id)
            document.status = "failed"
            document.error_message = "failure"
            task = await session.scalar(select(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == view.id))
            task.status = "failed"
            task.finished_at = datetime.now(UTC)
        original = profiles.resolve_processing_profile

        def outside_transaction(*args, **kwargs):
            assert harness.engine.pool.checkedout() == 0, "resource hashing held a database transaction"
            return original(*args, **kwargs)

        monkeypatch.setattr(profiles, "resolve_processing_profile", outside_transaction)
        if operation == "retry":
            await harness.service.retry_document(project, view.id)
        else:
            await harness.service.reparse_document(project, view.id, KnowledgeReparseRequest(expected_version=1))
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", [".markdown", ".mdx", ".xls", ".eml", ".msg", ".xml"])
async def test_registered_formats_can_pass_upload_and_storage_key_admission(postgres_database_url, tmp_path, extension):
    from actweave_knowledge.storage.minio_store import is_document_storage_key

    harness = await _harness(postgres_database_url, etl_type="unstructured_local")
    try:
        project, base = await _seed_base(harness)
        view = await harness.service.upload_document(project, base, _upload(tmp_path, original_name="source" + extension))
        key = next(iter(harness.store.objects))
        assert is_document_storage_key(key, project_id=project, document_id=view.id)
        assert view.parsing_profile.parse.etl_type == "unstructured_local"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_reparse_preview_preserves_header_parameters_for_shared_preview_pipeline(postgres_database_url, tmp_path, monkeypatch):
    from actweave_knowledge.contracts import KnowledgeChunkPreview
    from actweave_knowledge.ingestion import preview

    harness = await _harness(postgres_database_url)
    try:
        project, base = await _seed_base(harness)
        view = await harness.service.upload_document(project, base, _upload(tmp_path, original_name="source.csv"))
        async with harness.factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, view.id)
            document.status = "failed"
            document.error_message = "fixture"
        parameters = ProcessingParameters(header_rules=({"sheet": None, "mode": "none"},))
        received = []

        async def capture(request, settings, *, capability_revision, parser_slots, guard):
            del settings, parser_slots, guard
            assert len(capability_revision) == 64
            received.append(request.processing_profile)
            return KnowledgeChunkPreview(total=0, chunks=())

        monkeypatch.setattr(preview, "preview_document_chunks", capture)
        await harness.service.preview_reparse(project, view.id, KnowledgeReparseRequest(expected_version=1, processing_profile=parameters))
        assert received == [parameters]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_uses_installed_capability_revision_without_rebuilding(postgres_database_url, tmp_path, monkeypatch):
    from actweave_knowledge.ingestion import profiles

    harness = await _harness(postgres_database_url)
    try:
        project, base = await _seed_base(harness)
        parameters = ProcessingParameters()
        profile = resolve_processing_profile(harness.service._settings, parameters, default_registry(), extension=".txt")
        served = build_file_capabilities(harness.service._settings, default_registry())
        upload = replace(_upload(tmp_path), processing_profile=parameters)
        expected = preview_fingerprint(source_sha256=hashlib.sha256(upload.source_path.read_bytes()).hexdigest(), extension=".txt", profile=profile, capability_revision=served.capability_revision)
        monkeypatch.setattr(profiles, "build_file_capabilities", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must use installed snapshot")))
        view = await harness.service.upload_document(project, base, replace(upload, expected_preview_fingerprint=expected))
        assert view.parsing_profile == profile
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_parser_failed_snapshot_blocks_only_ingest_retry_after_task_kind_is_known(postgres_database_url, tmp_path):
    from actweave_knowledge.extraction.contracts import ExtractionError

    harness = await _harness(postgres_database_url)
    try:
        project, base = await _seed_base(harness)
        view = await harness.service.upload_document(project, base, _upload(tmp_path))
        async with harness.factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, view.id)
            document.status = "failed"
            document.error_message = "failure"
            task = await session.scalar(select(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == view.id))
            task.status = "failed"
            task.finished_at = datetime.now(UTC)
        checks = []
        unavailable_snapshot = build_file_capabilities(harness.service._settings, default_registry(), runtime_reason="PARSER_SANDBOX_UNAVAILABLE")

        def unavailable():
            checks.append("checked")
            return unavailable_snapshot

        harness.service._file_capabilities = unavailable
        with pytest.raises(ExtractionError) as error:
            await harness.service.retry_document(project, view.id)
        assert error.value.reason_code == "PARSER_SANDBOX_UNAVAILABLE"
        async with harness.factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, view.id)
            assert document.version == 1 and document.status == "failed"
            task = await session.scalar(select(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == view.id))
            task.kind = "reembed_document"
        retried = await harness.service.retry_document(project, view.id)
        assert retried.version == 2 and retried.status == "queued"
        assert checks == ["checked"]
    finally:
        await harness.engine.dispose()
