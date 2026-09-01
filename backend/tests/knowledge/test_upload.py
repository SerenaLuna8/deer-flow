"""M3 gates: document upload pipeline, query/download rules, and HTTP contract.

Package tests run against the installed Schema V1 snapshot with a fake object
store so every cleanup branch is observable; the HTTP tests exercise the
multipart upload and download temp-file lifecycle over ASGI with a stub module.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_DISABLED,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_PARSE_FAILED,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeChunkPreview,
    KnowledgeChunkPreviewAttachment,
    KnowledgeChunkPreviewChunk,
    KnowledgeChunkPreviewRequest,
    KnowledgeDocumentUpload,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeHealth,
    KnowledgePreviewAttachment,
    KnowledgePreviewTableSource,
    KnowledgeSettings,
)
from actweave_knowledge.documents import ALLOWED_DOCUMENT_EXTENSIONS, KnowledgeDocumentService
from actweave_knowledge.extraction.contracts import ParseWarning, ProcessingProfile, SourceSpan
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeTaskRow,
)
from actweave_knowledge.persistence.tasks import (
    claim_next_task,
    settle_task_failure,
)
from actweave_knowledge.tasks import (
    KnowledgeDocumentDeletionHandler,
    KnowledgeDocumentObjectDeletionHandler,
    KnowledgeTaskClaim,
)
from extraction_test_helpers import make_test_file_capability_provider, make_test_quota_port
from fastapi import FastAPI
from parsing_test_helpers import make_chunk_profile, make_parse_profile
from registry_helpers import seed_embedding_model, seed_provider
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge import gateway
from app.knowledge.composition import is_knowledge_project_active
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.bootstrap import _install_full_schema

# ---------------------------------------------------------------------------
# Package fixtures
# ---------------------------------------------------------------------------


class _FakeObjectStore:
    """In-memory MinioObjectStore double recording every call."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[tuple[str, str | None]] = []
        self.deletes: list[str] = []
        self.fail_upload: KnowledgeError | None = None
        self.fail_delete = False

    async def upload_from(self, key: str, source_path: Path, *, media_type: str | None = None) -> None:
        if self.fail_upload is not None:
            raise self.fail_upload
        self.objects[key] = Path(source_path).read_bytes()
        self.uploads.append((key, media_type))

    async def download_to(self, key: str, target_path: Path) -> None:
        if key not in self.objects:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "文档文件在对象存储中缺失")
        Path(target_path).write_bytes(self.objects[key])

    async def delete(self, key: str) -> None:
        if self.fail_delete:
            raise KnowledgeError(
                KNOWLEDGE_STORAGE_UNAVAILABLE,
                "对象存储暂时不可用",
            )
        self.deletes.append(key)
        self.objects.pop(key, None)

    async def require_absent(self, key: str) -> None:
        if key in self.objects:
            raise KnowledgeError(
                KNOWLEDGE_STORAGE_UNAVAILABLE,
                "对象存储删除结果无法确认",
            )


class _UploadHarness:
    def __init__(self, engine, factory, store: _FakeObjectStore, service: KnowledgeDocumentService) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.store = store
        self.service = service


class _RevokedAfterFirstTransaction:
    """Authority that is revoked after the upload-reservation transaction."""

    def __init__(self, project_id: uuid.UUID) -> None:
        self.project_id = project_id
        self.actor_user_id = uuid.uuid4()
        self.calls = 0

    async def revalidate(self, session: AsyncSession) -> None:
        del session
        self.calls += 1
        if self.calls > 1:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")


async def _harness(postgres_database_url: str, **settings_overrides: object) -> _UploadHarness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    settings = KnowledgeSettings.model_validate({"enabled": False, **settings_overrides})
    store = _FakeObjectStore()
    service = KnowledgeDocumentService(
        project_active_check=is_knowledge_project_active,
        quota=make_test_quota_port(factory),
        session_factory=factory,
        settings=settings,
        file_capabilities=make_test_file_capability_provider(settings),
        object_store=store,  # type: ignore[arg-type]
    )
    return _UploadHarness(engine, factory, store, service)


async def _seed_project(session: AsyncSession, label: str) -> uuid.UUID:
    user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (
                   :user_id, :email, :username, 'user', now(), false, 1
               )"""
        ),
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"m3u_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"m3u-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


async def _seed_base(harness: _UploadHarness, *, status: str = "active") -> tuple[uuid.UUID, uuid.UUID]:
    """Create project + registry embedding model + base; returns (project_id, base_id)."""

    provider_id = await seed_provider(harness.factory)
    embedding_model_id = await seed_embedding_model(harness.factory, provider_id)
    base_id = uuid.uuid4()
    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        session.add(
            KnowledgeBaseRow(
                id=base_id,
                project_id=project_id,
                name=f"kb-{base_id.hex[:8]}",
                description="",
                embedding_model_id=embedding_model_id,
                status=status,
            )
        )
    return project_id, base_id


def _upload(tmp_path: Path, content: bytes = b"knowledge upload bytes", **overrides: object) -> KnowledgeDocumentUpload:
    source = tmp_path / f"staged-{uuid.uuid4().hex[:8]}.bin"
    source.write_bytes(content)
    values: dict[str, object] = {
        "name": "季度报告",
        "original_name": "report.txt",
        "source_path": source,
        "size_bytes": len(content),
        "media_type": "text/plain",
        "chunk_size": 1000,
        "chunk_overlap": 100,
    }
    values.update(overrides)
    return KnowledgeDocumentUpload(**values)  # type: ignore[arg-type]


async def _table_counts(harness: _UploadHarness) -> tuple[int, int]:
    async with harness.factory() as session:
        documents = await session.scalar(select(func.count()).select_from(KnowledgeDocumentRow))
        tasks = await session.scalar(select(func.count()).select_from(KnowledgeTaskRow))
    return int(documents or 0), int(tasks or 0)


# ---------------------------------------------------------------------------
# Upload pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfigured_base_rejects_upload_without_reserving_or_storing_a_document(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        base_id = uuid.uuid4()
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
            session.add(KnowledgeBaseRow(id=base_id, project_id=project_id, name="待配置"))

        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path))

        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert await harness.service.list_documents(project_id, base_id) == ([], 0)
        assert await _table_counts(harness) == (0, 0)
        assert harness.store.objects == {}
        assert harness.store.uploads == []
        assert harness.store.deletes == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_creates_queued_document_and_ingest_task(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        content = b"the quick brown fox" * 100

        view = await harness.service.upload_document(project_id, base_id, _upload(tmp_path, content))

        assert view.status == "queued"
        assert view.version == 1
        assert view.name == "季度报告"
        assert view.original_name == "report.txt"
        assert view.size_bytes == len(content)
        assert view.segment_count == 0
        assert view.error_message is None
        assert view.delete_error is None

        expected_key = f"projects/{project_id}/knowledge/{base_id}/{view.id}.txt"
        assert harness.store.objects[expected_key] == content
        assert harness.store.uploads == [(expected_key, "text/plain")]

        async with harness.factory() as session:
            task = (await session.scalars(select(KnowledgeTaskRow))).one()
            document = (await session.scalars(select(KnowledgeDocumentRow))).one()
        assert document.status == "queued"
        assert document.storage_key == expected_key
        assert task.kind == "ingest_document"
        assert task.status == "queued"
        assert task.resource_id == view.id
        assert task.target_version == 1
        assert task.project_id == project_id

        # K2 parameters default to the escaped double-newline and rules off.
        assert view.chunk_separator == "\\n\\n"
        assert view.remove_extra_spaces is False
        assert view.remove_urls_emails is False
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_revocation_after_object_put_does_not_publish_document(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        authority = _RevokedAfterFirstTransaction(project_id)

        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(
                project_id,
                base_id,
                _upload(tmp_path),
                authority=authority,
            )

        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert authority.calls == 2
        assert harness.store.objects == {}
        assert await _table_counts(harness) == (0, 0)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_freezes_separator_and_rules_on_the_document_row(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)

        view = await harness.service.upload_document(
            project_id,
            base_id,
            _upload(tmp_path, chunk_separator="。", remove_extra_spaces=True, remove_urls_emails=True),
        )

        assert view.chunk_separator == "。"
        assert view.remove_extra_spaces is True
        assert view.remove_urls_emails is True
        async with harness.factory() as session:
            row = (await session.scalars(select(KnowledgeDocumentRow))).one()
        assert row.chunk_separator == "。"
        assert row.remove_extra_spaces is True
        assert row.remove_urls_emails is True
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_freezes_parent_child_mode_and_normalizes_general_child_params(postgres_database_url: str, tmp_path: Path) -> None:
    """K3: parent_child child params freeze on the row; general resets stray values."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)

        nested = await harness.service.upload_document(
            project_id,
            base_id,
            _upload(tmp_path, chunking_mode="parent_child", child_chunk_size=300, child_chunk_separator="。"),
        )
        assert nested.chunking_mode == "parent_child"
        assert nested.child_chunk_size == 300
        assert nested.child_chunk_separator == "。"
        assert nested.hit_count == 0

        # General mode ignores whatever child values the client sent.
        plain = await harness.service.upload_document(
            project_id,
            base_id,
            _upload(tmp_path, name="普通文档", chunking_mode="general", child_chunk_size=1999, child_chunk_separator="；"),
        )
        assert plain.chunking_mode == "general"
        assert plain.child_chunk_size == 500
        assert plain.child_chunk_separator == "\\n"

        async with harness.factory() as session:
            rows = {row.id: row for row in (await session.scalars(select(KnowledgeDocumentRow))).all()}
        assert rows[nested.id].chunking_mode == "parent_child"
        assert rows[nested.id].child_chunk_size == 300
        assert rows[nested.id].child_chunk_separator == "。"
        assert rows[plain.id].chunking_mode == "general"
        assert rows[plain.id].child_chunk_size == 500
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", sorted(ALLOWED_DOCUMENT_EXTENSIONS))
async def test_upload_accepts_every_frozen_extension(postgres_database_url: str, tmp_path: Path, extension: str) -> None:
    harness = await _harness(postgres_database_url, etl_type="unstructured_local")
    try:
        project_id, base_id = await _seed_base(harness)
        original_name = f"文件{extension.upper()}"  # extension matching is case-insensitive

        view = await harness.service.upload_document(project_id, base_id, _upload(tmp_path, original_name=original_name))

        assert view.original_name == original_name
        assert view.status == "queued"
        key = next(iter(harness.store.objects))
        assert key.endswith(extension)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_extensions(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        for original_name in ("run.exe", "archive.tar.gz", "noextension"):
            with pytest.raises(KnowledgeError) as error:
                await harness.service.upload_document(project_id, base_id, _upload(tmp_path, original_name=original_name))
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST

        assert await _table_counts(harness) == (0, 0)
        assert harness.store.uploads == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_rejects_empty_and_oversized_files(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url, upload_max_bytes=16)
    try:
        project_id, base_id = await _seed_base(harness)

        with pytest.raises(KnowledgeError) as empty:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path, b""))
        assert empty.value.code == KNOWLEDGE_INVALID_REQUEST

        with pytest.raises(KnowledgeError) as oversized:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path, b"x" * 17))
        assert oversized.value.code == KNOWLEDGE_INVALID_REQUEST

        boundary = await harness.service.upload_document(project_id, base_id, _upload(tmp_path, b"x" * 16))
        assert boundary.size_bytes == 16
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "   "},
        {"chunk_size": 199},
        {"chunk_size": 4001},
        {"chunk_overlap": 501},
        {"chunk_size": 300, "chunk_overlap": 300},
        {"chunk_separator": ""},
        {"chunk_separator": "#" * 65},
        {"remove_extra_spaces": "yes"},
        {"remove_urls_emails": 1},
        {"chunking_mode": "hierarchical"},
        {"chunking_mode": "parent_child", "child_chunk_size": 99},
        {"chunking_mode": "parent_child", "child_chunk_size": 2001},
        {"chunking_mode": "parent_child", "chunk_size": 400, "child_chunk_size": 400},
        {"chunking_mode": "parent_child", "child_chunk_separator": ""},
        {"chunking_mode": "parent_child", "child_chunk_separator": "#" * 65},
    ],
    ids=[
        "blank-name",
        "chunk-too-small",
        "chunk-too-large",
        "overlap-too-large",
        "overlap-not-below-chunk",
        "separator-empty",
        "separator-too-long",
        "rule-not-bool",
        "rule-int-not-bool",
        "mode-unknown",
        "child-too-small",
        "child-too-large",
        "child-not-below-chunk",
        "child-separator-empty",
        "child-separator-too-long",
    ],
)
async def test_upload_validates_name_and_chunk_parameters(postgres_database_url: str, tmp_path: Path, overrides: dict[str, object]) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path, **overrides))
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert harness.store.uploads == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_requires_an_active_base_in_the_same_project(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, disabled_base = await _seed_base(harness, status="disabled")
        _, deleting_base = await _seed_base(harness, status="deleting")
        other_project, other_base = await _seed_base(harness)

        for base_id, code in (
            (disabled_base, KNOWLEDGE_INVALID_REQUEST),
            (deleting_base, KNOWLEDGE_NOT_FOUND),  # other project's deleting base is invisible
            (uuid.uuid4(), KNOWLEDGE_NOT_FOUND),
            (other_base, KNOWLEDGE_NOT_FOUND),  # exists, but belongs to another project
        ):
            with pytest.raises(KnowledgeError) as error:
                await harness.service.upload_document(project_id, base_id, _upload(tmp_path))
            assert error.value.code == code

        assert await _table_counts(harness) == (0, 0)
        assert harness.store.uploads == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_enforces_the_per_base_document_quota(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url, max_documents_per_knowledge_base=1)
    try:
        project_id, base_id = await _seed_base(harness)
        await harness.service.upload_document(project_id, base_id, _upload(tmp_path))

        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path))
        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
        assert (await _table_counts(harness))[0] == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_failed_object_write_leaves_no_document_behind(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        harness.store.fail_upload = KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "写入失败")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path))

        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE
        assert await _table_counts(harness) == (0, 0)
        assert harness.store.objects == {}
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_failed_task_creation_deletes_the_written_object_and_row(postgres_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)

        async def _broken_publish(*args: object, **kwargs: object) -> KnowledgeDocumentView:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "任务创建失败")

        monkeypatch.setattr(harness.service, "_publish_queued_document", _broken_publish)

        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path))

        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE
        assert await _table_counts(harness) == (0, 0)
        assert harness.store.objects == {}
        assert len(harness.store.deletes) == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_wins_while_document_upload_is_in_flight(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleting Document cannot be revived when its object upload finishes."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        upload_started = asyncio.Event()
        allow_upload_to_finish = asyncio.Event()
        original_upload = harness.store.upload_from

        async def _paused_upload(key: str, source_path: Path, *, media_type: str | None = None) -> None:
            upload_started.set()
            await allow_upload_to_finish.wait()
            await original_upload(key, source_path, media_type=media_type)

        monkeypatch.setattr(harness.store, "upload_from", _paused_upload)
        upload_task = asyncio.create_task(harness.service.upload_document(project_id, base_id, _upload(tmp_path)))
        await asyncio.wait_for(upload_started.wait(), timeout=5)

        uploading, total = await harness.service.list_documents(project_id, base_id)
        assert total == 1
        assert uploading[0].status == "uploading"
        deleted = await harness.service.delete_document(project_id, uploading[0].id)
        assert deleted.status == "deleting"

        allow_upload_to_finish.set()
        with pytest.raises(KnowledgeError) as error:
            await upload_task
        assert error.value.code == KNOWLEDGE_CONFLICT

        remaining, total = await harness.service.list_documents(project_id, base_id)
        assert remaining == []
        assert total == 0
        assert harness.store.objects == {}
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_cleanup_persists_orphan_delete_after_worker_removed_the_row(
    postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late put survives no gap even while the original delete is running.

    The orphan cleanup has its own task kind and exact storage key, so its
    permanent failure is visible on the recreated tombstone and a user can
    retry through the ordinary Document-delete interface.
    """

    harness = await _harness(postgres_database_url)
    allow_upload_to_finish = asyncio.Event()
    try:
        project_id, base_id = await _seed_base(harness)
        upload_started = asyncio.Event()
        original_upload = harness.store.upload_from

        async def _paused_upload(
            key: str,
            source_path: Path,
            *,
            media_type: str | None = None,
        ) -> None:
            upload_started.set()
            await allow_upload_to_finish.wait()
            await original_upload(key, source_path, media_type=media_type)

        monkeypatch.setattr(harness.store, "upload_from", _paused_upload)
        upload_task = asyncio.create_task(
            harness.service.upload_document(
                project_id,
                base_id,
                _upload(tmp_path),
            )
        )
        await asyncio.wait_for(upload_started.wait(), timeout=5)
        uploading, _ = await harness.service.list_documents(project_id, base_id)
        document_id = uploading[0].id
        await harness.service.delete_document(project_id, document_id)

        async with harness.factory() as session, session.begin():
            delete_task = await claim_next_task(session, lease_seconds=60)
            assert delete_task is not None
            assert delete_task.kind == "delete_document"
            delete_task.attempt_count = 3
        handler = KnowledgeDocumentDeletionHandler(
            session_factory=harness.factory,
            object_store=harness.store,  # type: ignore[arg-type]
            quota=make_test_quota_port(harness.factory),
            project_active_check=is_knowledge_project_active,
        )
        delete_claim = KnowledgeTaskClaim(
            id=delete_task.id,
            project_id=project_id,
            resource_id=document_id,
            kind="delete_document",
            target_version=None,
            storage_key=None,
            claim_token=delete_task.claim_token,  # type: ignore[arg-type]
            attempt_count=3,
            max_attempts=3,
        )
        with pytest.raises(KnowledgeError) as pending_error:
            await handler(delete_claim)
        assert pending_error.value.code == "KNOWLEDGE_TASK_FAILED"
        async with harness.factory() as session, session.begin():
            outcome = await settle_task_failure(
                session,
                delete_task.id,
                delete_task.claim_token,  # type: ignore[arg-type]
                error_message=pending_error.value.message,
                retry_delay_seconds=0,
            )
        assert outcome == "failed"
        async with harness.factory() as session:
            pending = await session.get(KnowledgeDocumentRow, document_id)
            original_task = await session.get(KnowledgeTaskRow, delete_task.id)
        assert pending is not None
        assert (pending.upload_state, pending.quota_state) == ("pending", "reserved")
        assert original_task is not None and original_task.status == "failed"
        assert harness.store.objects == {}
        assert harness.store.deletes == []

        harness.store.fail_delete = True
        allow_upload_to_finish.set()
        with pytest.raises(KnowledgeError) as error:
            await upload_task
        assert error.value.code == KNOWLEDGE_CONFLICT
        assert len(harness.store.objects) == 1

        async with harness.factory() as session:
            cleanup_task = await session.scalar(
                select(KnowledgeTaskRow).where(
                    KnowledgeTaskRow.resource_id == document_id,
                    KnowledgeTaskRow.kind == "delete_document_object",
                    KnowledgeTaskRow.status == "queued",
                )
            )
            original_task = await session.get(KnowledgeTaskRow, delete_task.id)
            tombstone = await session.get(KnowledgeDocumentRow, document_id)
        assert cleanup_task is not None
        assert cleanup_task.storage_key in harness.store.objects
        assert original_task is not None and original_task.status == "failed"
        assert tombstone is not None
        assert tombstone.status == "deleting"
        assert tombstone.storage_key == cleanup_task.storage_key

        # Exhaust the orphan cleanup and expose its error on the tombstone.
        async with harness.factory() as session, session.begin():
            cleanup_claim = await claim_next_task(session, lease_seconds=60)
            assert cleanup_claim is not None
            assert cleanup_claim.id == cleanup_task.id
            cleanup_claim.attempt_count = 3
        object_handler = KnowledgeDocumentObjectDeletionHandler(
            session_factory=harness.factory,
            object_store=harness.store,  # type: ignore[arg-type]
            quota=make_test_quota_port(harness.factory),
            project_active_check=is_knowledge_project_active,
        )
        object_claim = KnowledgeTaskClaim(
            id=cleanup_claim.id,
            project_id=project_id,
            resource_id=document_id,
            kind="delete_document_object",
            target_version=None,
            storage_key=cleanup_claim.storage_key,
            claim_token=cleanup_claim.claim_token,  # type: ignore[arg-type]
            attempt_count=3,
            max_attempts=3,
        )
        with pytest.raises(KnowledgeError) as cleanup_error:
            await object_handler(object_claim)
        async with harness.factory() as session, session.begin():
            outcome = await settle_task_failure(
                session,
                cleanup_claim.id,
                cleanup_claim.claim_token,  # type: ignore[arg-type]
                error_message=cleanup_error.value.message,
                retry_delay_seconds=0,
            )
        assert outcome == "failed"
        stuck = await harness.service.get_document(project_id, document_id)
        assert stuck.status == "deleting"
        assert stuck.delete_error == cleanup_error.value.message

        # The first delete work has finished. A normal user retry opens the
        # ordinary task kind and remains recoverable through existing UI/API.
        retried = await harness.service.delete_document(project_id, document_id)
        assert retried.status == "deleting"
        assert retried.delete_error is None
        async with harness.factory() as session, session.begin():
            retry_task = await session.scalar(
                select(KnowledgeTaskRow).where(
                    KnowledgeTaskRow.resource_id == document_id,
                    KnowledgeTaskRow.kind == "delete_document",
                    KnowledgeTaskRow.status == "queued",
                )
            )
            claimed_retry = await claim_next_task(session, lease_seconds=60)
        assert retry_task is not None
        assert claimed_retry is not None and claimed_retry.id == retry_task.id

        harness.store.fail_delete = False
        await handler(
            KnowledgeTaskClaim(
                id=claimed_retry.id,
                project_id=project_id,
                resource_id=document_id,
                kind="delete_document",
                target_version=None,
                storage_key=None,
                claim_token=claimed_retry.claim_token,  # type: ignore[arg-type]
                attempt_count=claimed_retry.attempt_count,
                max_attempts=claimed_retry.max_attempts,
            )
        )
        assert harness.store.objects == {}
        async with harness.factory() as session:
            assert await session.get(KnowledgeDocumentRow, document_id) is None
    finally:
        allow_upload_to_finish.set()
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# List, get, download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_documents_orders_newest_first_and_paginates(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        base_time = datetime(2036, 3, 1, 12, 0, tzinfo=UTC)
        ids: list[uuid.UUID] = []
        async with harness.factory() as session, session.begin():
            for position in range(3):
                document_id = uuid.uuid4()
                ids.append(document_id)
                session.add(
                    KnowledgeDocumentRow(
                        id=document_id,
                        project_id=project_id,
                        knowledge_base_id=base_id,
                        name=f"doc-{position}",
                        original_name=f"doc-{position}.txt",
                        storage_key=f"projects/{project_id}/knowledge/{base_id}/{document_id}.txt",
                        size_bytes=10,
                        status="queued",
                        version=1,
                        chunk_size=1000,
                        chunk_overlap=100,
                        created_at=base_time.replace(minute=position),
                        updated_at=base_time,
                    )
                )

        first_page, total = await harness.service.list_documents(project_id, base_id, page=1, page_size=2)
        second_page, _ = await harness.service.list_documents(project_id, base_id, page=2, page_size=2)

        assert total == 3
        assert [view.id for view in first_page] == [ids[2], ids[1]]
        assert [view.id for view in second_page] == [ids[0]]

        with pytest.raises(KnowledgeError) as missing:
            await harness.service.list_documents(project_id, uuid.uuid4())
        assert missing.value.code == KNOWLEDGE_NOT_FOUND

        other_project, _ = await _seed_base(harness)
        with pytest.raises(KnowledgeError) as foreign:
            await harness.service.list_documents(other_project, base_id)
        assert foreign.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_get_document_is_project_scoped_and_derives_delete_error(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        uploaded = await harness.service.upload_document(project_id, base_id, _upload(tmp_path))

        fetched = await harness.service.get_document(project_id, uploaded.id)
        assert fetched.id == uploaded.id
        assert fetched.delete_error is None

        other_project, _ = await _seed_base(harness)
        with pytest.raises(KnowledgeError) as foreign:
            await harness.service.get_document(other_project, uploaded.id)
        assert foreign.value.code == KNOWLEDGE_NOT_FOUND

        async with harness.factory() as session, session.begin():
            session.add(
                KnowledgeTaskRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    resource_id=uploaded.id,
                    kind="delete_document",
                    target_version=None,
                    status="failed",
                    attempt_count=3,
                    error_message="删除对象失败",
                    finished_at=datetime(2036, 3, 2, tzinfo=UTC),
                )
            )
        stuck = await harness.service.get_document(project_id, uploaded.id)
        assert stuck.delete_error == "删除对象失败"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_download_round_trips_bytes_and_gates_statuses(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        content = b"downloadable original bytes"
        uploaded = await harness.service.upload_document(project_id, base_id, _upload(tmp_path, content))

        target = tmp_path / "roundtrip.txt"
        view = await harness.service.download_document(project_id, uploaded.id, target)
        assert target.read_bytes() == content
        assert view.original_name == "report.txt"
        assert view.media_type == "text/plain"

        async def _set_status(status: str, error_message: str | None = None) -> None:
            async with harness.factory() as session, session.begin():
                row = await session.get(KnowledgeDocumentRow, uploaded.id)
                assert row is not None
                row.status = status
                row.error_message = error_message

        # Every terminal/processing status stays downloadable.
        for status in ("processing", "ready"):
            await _set_status(status)
            await harness.service.download_document(project_id, uploaded.id, tmp_path / f"{status}.txt")
        await _set_status("failed", "摄取失败")
        await harness.service.download_document(project_id, uploaded.id, tmp_path / "failed.txt")

        for status in ("uploading", "deleting"):
            await _set_status(status)
            with pytest.raises(KnowledgeError) as blocked:
                await harness.service.download_document(project_id, uploaded.id, tmp_path / "blocked.txt")
            assert blocked.value.code == KNOWLEDGE_INVALID_REQUEST

        await _set_status("queued")
        harness.store.objects.clear()
        with pytest.raises(KnowledgeError) as missing_object:
            await harness.service.download_document(project_id, uploaded.id, tmp_path / "missing.txt")
        assert missing_object.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE

        with pytest.raises(KnowledgeError) as missing_document:
            await harness.service.download_document(project_id, uuid.uuid4(), tmp_path / "none.txt")
        assert missing_document.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_download_revalidates_authority_after_object_io_before_returning(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    """Revocation while MinIO copies bytes suppresses the document response."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        content = b"copied before final authority rejection"
        uploaded = await harness.service.upload_document(
            project_id,
            base_id,
            _upload(tmp_path, content),
        )
        authority = _RevokedAfterFirstTransaction(project_id)
        target = tmp_path / "revoked-download.txt"

        with pytest.raises(KnowledgeError) as error:
            await harness.service.download_document(
                project_id,
                uploaded.id,
                target,
                authority=authority,
            )

        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert authority.calls == 2
        assert target.read_bytes() == content
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_download_final_guard_database_failure_maps_to_storage_unavailable(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    """A DB outage after MinIO copy never escapes as a raw SQLAlchemy error."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        uploaded = await harness.service.upload_document(
            project_id,
            base_id,
            _upload(tmp_path, b"copied before database outage"),
        )

        class _DiesOnFinalGuard:
            def __init__(self, inner) -> None:  # noqa: ANN001
                self._inner = inner
                self._calls = 0

            def __call__(self):  # noqa: ANN204
                self._calls += 1
                if self._calls > 1:
                    raise SQLAlchemyError("pool failed after object copy")
                return self._inner()

        service = KnowledgeDocumentService(
            project_active_check=is_knowledge_project_active,
            quota=make_test_quota_port(harness.factory),
            session_factory=_DiesOnFinalGuard(harness.factory),  # type: ignore[arg-type]
            settings=KnowledgeSettings(),
            file_capabilities=make_test_file_capability_provider(),
            object_store=harness.store,  # type: ignore[arg-type]
        )
        target = tmp_path / "db-failed-download.txt"

        with pytest.raises(KnowledgeError) as error:
            await service.download_document(project_id, uploaded.id, target)

        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE
        assert target.read_bytes() == b"copied before database outage"
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# HTTP contract: bases, upload, download, health
# ---------------------------------------------------------------------------

_REQUEST_ID = "knowledge-m3-contract"
_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_BASE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_DOCUMENT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_NOW = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)


def _document_view(**overrides: object) -> KnowledgeDocumentView:
    values: dict[str, object] = {
        "id": _DOCUMENT_ID,
        "project_id": _PROJECT_ID,
        "knowledge_base_id": _BASE_ID,
        "name": "季度报告",
        "original_name": "report.pdf",
        "media_type": "application/pdf",
        "size_bytes": 11,
        "status": "queued",
        "enabled": True,
        "version": 1,
        "chunk_size": 1000,
        "chunk_overlap": 100,
        "chunk_separator": "\\n\\n",
        "remove_extra_spaces": False,
        "remove_urls_emails": False,
        "chunking_mode": "general",
        "child_chunk_size": 500,
        "child_chunk_separator": "\\n",
        "segment_count": 0,
        "word_count": 0,
        "hit_count": 0,
        "doc_metadata": {},
        "error_message": None,
        "delete_error": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return KnowledgeDocumentView(**values)  # type: ignore[arg-type]


class _FakeModule:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(upload_max_bytes=64)
        self.calls: list[tuple[str, object]] = []
        self.upload_error: KnowledgeError | None = None
        self.download_error: KnowledgeError | None = None
        self.preview_error: KnowledgeError | None = None
        self.staged_content: bytes | None = None
        self.download_payload = b"original file bytes"

    async def upload_document(self, project_id: uuid.UUID, base_id: uuid.UUID, upload: KnowledgeDocumentUpload, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self.calls.append(("upload", (project_id, base_id, upload)))
        # The staged temp file must exist while the module runs.
        self.staged_content = Path(upload.source_path).read_bytes()
        if self.upload_error is not None:
            raise self.upload_error
        return _document_view(
            name=upload.name,
            original_name=upload.original_name,
            media_type=upload.media_type,
            size_bytes=upload.size_bytes,
            chunk_size=upload.chunk_size,
            chunk_overlap=upload.chunk_overlap,
            chunk_separator=upload.chunk_separator,
            remove_extra_spaces=upload.remove_extra_spaces,
            remove_urls_emails=upload.remove_urls_emails,
        )

    async def preview_document_chunks(self, request: KnowledgeChunkPreviewRequest, *, authority):  # noqa: ANN001
        assert authority.project_id == _PROJECT_ID
        self.calls.append(("preview", request))
        self.staged_content = Path(request.source_path).read_bytes()
        if self.preview_error is not None:
            raise self.preview_error
        span = SourceSpan(block_id="preview:1", start=0, end=7, location={"paragraph": 1})
        ref = "a" * 64
        return KnowledgeChunkPreview(
            total=12,
            chunks=tuple(
                KnowledgeChunkPreviewChunk(
                    position=index,
                    content=f"chunk-{index}",
                    word_count=7,
                    child_contents=(("child-a", "child-b") if request.chunking_mode == "parent_child" else ()),
                    token_count=3,
                    source_spans=(span,),
                    attachments=(KnowledgeChunkPreviewAttachment(ref=ref, alt_text="拓扑图"),),
                )
                for index in range(1, 3)
            ),
            preview_fingerprint="b" * 64,
            source_sha256="c" * 64,
            effective_profile=ProcessingProfile(
                parse=make_parse_profile(".md"),
                chunk=make_chunk_profile(),
            ),
            warnings=(ParseWarning(code="HEADER_INFERRED", message="已自动识别表头，请确认", source_position={"row": 1}),),
            preview_attachments=(KnowledgePreviewAttachment(ref=ref, media_type="image/png", data_base64="aGVsbG8="),),
            omitted_preview_attachment_count=2,
            table_sources=(KnowledgePreviewTableSource(sheet=None, header_mode="auto", header_row=1, header_cells=("设备", "端口")),),
        )

    async def download_document(self, project_id: uuid.UUID, document_id: uuid.UUID, target_path: Path, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self.calls.append(("download", (project_id, document_id)))
        if self.download_error is not None:
            raise self.download_error
        await asyncio.to_thread(target_path.write_bytes, self.download_payload)
        return _document_view(original_name="下载报告.pdf")

    async def health(self, *, authority):  # noqa: ANN001
        assert authority.project_id == _PROJECT_ID
        self.calls.append(("health", None))
        return KnowledgeHealth(enabled=True, database_ok=True, storage_ok=False, message="对象存储 bucket 不可访问")


def _app(module: _FakeModule) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway.project_router)
    context = ProjectContext(
        user_id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=1,
        request_id=_REQUEST_ID,
    )
    app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: context
    app.dependency_overrides[gateway.require_project_knowledge_edit] = lambda: context
    app.state.knowledge_module = module
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture()
def temp_path_tracker(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every request temp file the gateway creates."""

    created: list[Path] = []
    original = gateway._new_request_temp_path

    async def _tracking(suffix: str = "") -> Path:
        path = await original(suffix)
        created.append(path)
        return path

    monkeypatch.setattr(gateway, "_new_request_temp_path", _tracking)
    return created


@pytest.mark.asyncio
async def test_http_upload_stages_multipart_body_and_cleans_temp_file(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/documents",
            files={"file": ("季度报告.pdf", b"pdf-bytes\x00\x01", "application/pdf")},
            data={
                "name": "展示名",
                "chunk_size": "800",
                "chunk_overlap": "80",
                "chunk_separator": "。",
                "remove_extra_spaces": "true",
                "remove_urls_emails": "false",
                "chunking_mode": "parent_child",
                "child_chunk_size": "300",
                "child_chunk_separator": "；",
            },
        )

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["name"] == "展示名"
    assert item["original_name"] == "季度报告.pdf"
    assert item["chunk_separator"] == "。"
    assert item["remove_extra_spaces"] is True
    assert item["remove_urls_emails"] is False

    verb, (project_id, base_id, upload) = module.calls[0]
    assert verb == "upload"
    assert project_id == _PROJECT_ID
    assert base_id == _BASE_ID
    assert upload.size_bytes == len(b"pdf-bytes\x00\x01")
    assert upload.chunk_size == 800
    assert upload.chunk_overlap == 80
    assert upload.chunk_separator == "。"
    assert upload.remove_extra_spaces is True
    assert upload.remove_urls_emails is False
    assert upload.chunking_mode == "parent_child"
    assert upload.child_chunk_size == 300
    assert upload.child_chunk_separator == "；"
    assert module.staged_content == b"pdf-bytes\x00\x01"

    assert temp_path_tracker, "the upload must stage through a temp file"
    assert all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_http_upload_defaults_display_name_to_the_filename(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/documents",
            files={"file": ("notes.md", b"# notes", "text/markdown")},
        )

    assert response.status_code == 200
    _, (_, _, upload) = module.calls[0]
    assert upload.name == "notes.md"
    assert upload.chunk_size == 1000
    assert upload.chunk_overlap == 100
    assert upload.chunk_separator == "\\n\\n"
    assert upload.remove_extra_spaces is False
    assert upload.remove_urls_emails is False
    assert upload.chunking_mode == "general"
    assert upload.child_chunk_size == 500
    assert upload.child_chunk_separator == "\\n"
    assert all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_http_chunk_preview_round_trips_and_cleans_temp_file(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/chunk-preview",
            files={"file": ("说明.md", "# 标题\n\n正文".encode(), "text/markdown")},
            data={
                "chunk_size": "500",
                "chunk_overlap": "50",
                "chunk_separator": "\\n\\n",
                "remove_extra_spaces": "true",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 12
    assert [item["content"] for item in payload["items"]] == ["chunk-1", "chunk-2"]
    assert payload["items"][0]["word_count"] == 7
    assert payload["items"][0]["token_count"] == 3
    assert payload["items"][0]["source_spans"] == [
        {
            "block_id": "preview:1",
            "start": 0,
            "end": 7,
            "location": {"paragraph": 1},
            "role": "source",
        }
    ]
    assert payload["items"][0]["attachments"] == [{"ref": "a" * 64, "alt_text": "拓扑图"}]
    assert payload["preview_fingerprint"] == "b" * 64
    assert payload["source_sha256"] == "c" * 64
    assert payload["effective_profile"]["parse"]["extractor_id"] == "dify.markdown"
    assert payload["warnings"] == [
        {
            "code": "HEADER_INFERRED",
            "message": "已自动识别表头，请确认",
            "source_position": {"row": 1},
        }
    ]
    assert payload["preview_attachments"] == [{"ref": "a" * 64, "media_type": "image/png", "data_base64": "aGVsbG8="}]
    assert payload["omitted_preview_attachment_count"] == 2
    assert payload["table_sources"] == [
        {
            "sheet": None,
            "header_mode": "auto",
            "header_row": 1,
            "header_cells": ["设备", "端口"],
        }
    ]
    assert "index_text" not in str(payload)
    assert "relative_path" not in str(payload)
    assert payload["request_id"] == _REQUEST_ID

    verb, request = module.calls[0]
    assert verb == "preview"
    assert request.original_name == "说明.md"
    assert request.chunk_size == 500
    assert request.chunk_overlap == 50
    assert request.chunk_separator == "\\n\\n"
    assert request.remove_extra_spaces is True
    assert request.remove_urls_emails is False
    assert request.chunking_mode == "general"
    assert [item["child_contents"] for item in payload["items"]] == [[], []]
    assert module.staged_content == "# 标题\n\n正文".encode()

    assert temp_path_tracker, "the preview must stage through a temp file"
    assert all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_http_chunk_preview_forwards_parent_child_mode_and_returns_nested_children(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/chunk-preview",
            files={"file": ("说明.md", "# 标题\n\n正文".encode(), "text/markdown")},
            data={
                "chunking_mode": "parent_child",
                "child_chunk_size": "250",
                "child_chunk_separator": "。",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert [item["child_contents"] for item in payload["items"]] == [["child-a", "child-b"], ["child-a", "child-b"]]

    _, request = module.calls[0]
    assert request.chunking_mode == "parent_child"
    assert request.child_chunk_size == 250
    assert request.child_chunk_separator == "。"
    assert all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_http_chunk_preview_maps_errors_and_still_cleans_temp(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    module.preview_error = KnowledgeError(KNOWLEDGE_PARSE_FAILED, "文件没有可提取的文本")
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/chunk-preview",
            files={"file": ("bad.pdf", b"%PDF-broken", "application/pdf")},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == KNOWLEDGE_PARSE_FAILED
    assert all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_http_chunk_preview_rejects_declared_oversized_bodies_before_staging(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()  # upload_max_bytes=64; allowance is 1 MiB
    oversized = b"x" * (2 * 1024 * 1024)
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/chunk-preview",
            files={"file": ("big.txt", oversized, "text/plain")},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == KNOWLEDGE_INVALID_REQUEST
    assert module.calls == []
    assert temp_path_tracker == []


@pytest.mark.asyncio
async def test_http_upload_rejects_oversized_bodies_while_streaming(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    module.settings.upload_max_bytes = 8
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/documents",
            files={"file": ("big.txt", b"x" * 64, "text/plain")},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == KNOWLEDGE_INVALID_REQUEST
    assert module.calls == []
    assert temp_path_tracker and all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_http_upload_cleans_temp_file_when_the_module_fails(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    module.upload_error = KnowledgeError(KNOWLEDGE_QUOTA_EXCEEDED, "配额已满")
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/documents",
            files={"file": ("doc.txt", b"payload", "text/plain")},
        )

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == KNOWLEDGE_QUOTA_EXCEEDED
    assert temp_path_tracker and all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_staging_cleans_the_temp_file_when_the_request_is_cancelled(
    temp_path_tracker: list[Path],
) -> None:
    class _CancelledUpload:
        async def read(self, size: int) -> bytes:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await gateway._stage_upload_to_temp(_CancelledUpload(), 100, _REQUEST_ID)  # type: ignore[arg-type]

    assert temp_path_tracker and all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_http_download_streams_original_file_then_cleans_temp(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    async with _client(_app(module)) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/download")

    assert response.status_code == 200
    assert response.content == b"original file bytes"
    assert response.headers["content-type"].startswith("application/pdf")
    assert "%E4%B8%8B%E8%BD%BD%E6%8A%A5%E5%91%8A.pdf" in response.headers["content-disposition"]
    assert module.calls == [("download", (_PROJECT_ID, _DOCUMENT_ID))]
    assert temp_path_tracker and all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_http_download_cleans_temp_when_the_module_fails(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    module.download_error = KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象缺失")
    async with _client(_app(module)) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/download")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == KNOWLEDGE_STORAGE_UNAVAILABLE
    assert temp_path_tracker and all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_http_health_reports_module_probes(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    async with _client(_app(module)) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/health")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "database_ok": True,
        "storage_ok": False,
        "message": "对象存储 bucket 不可访问",
        "request_id": _REQUEST_ID,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("create_binding", [{}, {"embedding_model_id": None}])
async def test_http_unconfigured_base_create_and_first_configuration(create_binding: dict[str, None]) -> None:
    from dataclasses import replace

    from actweave_knowledge import KnowledgeBaseUpdateResult, KnowledgeBaseView

    base_view = KnowledgeBaseView(
        id=_BASE_ID,
        project_id=_PROJECT_ID,
        name="待配置",
        description="",
        embedding_model_id=None,
        reranker_model_id=None,
        retrieval_mode="semantic",
        summary_index_enabled=False,
        status="active",
        document_count=0,
        default_top_k=4,
        default_score_threshold=0.2,
        delete_error=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    embedding_id, reranker_id = uuid.uuid4(), uuid.uuid4()

    class _BaseModule(_FakeModule):
        async def create_knowledge_base(self, project_id, create, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            assert create.embedding_model_id is None
            assert create.reranker_model_id is None
            assert create.retrieval_mode == "semantic"
            return base_view

        async def update_knowledge_base(self, project_id, base_id, update, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            assert base_id == _BASE_ID
            assert update.embedding_model_id == embedding_id
            assert update.reranker_model_id == reranker_id
            assert update.retrieval_mode == "hybrid"
            return KnowledgeBaseUpdateResult(base=replace(base_view, embedding_model_id=embedding_id, reranker_model_id=reranker_id, retrieval_mode="hybrid"))

    async with _client(_app(_BaseModule())) as client:
        created = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/bases", json={"name": "待配置", **create_binding})
        assert created.status_code == 200
        assert created.json()["item"]["embedding_model_id"] is None
        assert created.json()["item"]["document_count"] == 0
        configured = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}",
            json={"embedding_model_id": str(embedding_id), "retrieval_mode": "hybrid", "reranker_model_id": str(reranker_id)},
        )
        assert configured.status_code == 200
        assert configured.json()["item"]["embedding_model_id"] == str(embedding_id)
        assert configured.json()["item"]["reranker_model_id"] == str(reranker_id)
        assert configured.json()["item"]["retrieval_mode"] == "hybrid"


@pytest.mark.asyncio
async def test_http_base_routes_round_trip_the_module_views() -> None:
    from actweave_knowledge import KnowledgeBaseUpdateResult, KnowledgeBaseView, KnowledgeRebuildResult

    base_view = KnowledgeBaseView(
        id=_BASE_ID,
        project_id=_PROJECT_ID,
        name="产品手册",
        description="",
        embedding_model_id=uuid.uuid4(),
        reranker_model_id=None,
        retrieval_mode="semantic",
        summary_index_enabled=False,
        status="active",
        document_count=2,
        default_top_k=4,
        default_score_threshold=0.2,
        delete_error=None,
        created_at=_NOW,
        updated_at=_NOW,
    )

    class _BaseModule(_FakeModule):
        async def create_knowledge_base(self, project_id, create, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("create_base", (project_id, create)))
            return base_view

        async def list_knowledge_bases(self, project_id, *, page=1, page_size=20, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("list_bases", (project_id, page, page_size)))
            return [base_view], 1

        async def get_knowledge_base(self, project_id, base_id, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("get_base", (project_id, base_id)))
            return base_view

        async def update_knowledge_base(self, project_id, base_id, update, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("update_base", (project_id, base_id, update)))
            return KnowledgeBaseUpdateResult(base=base_view)

        async def rebuild_knowledge_base(self, project_id, base_id, *, embedding_model_id, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("rebuild_base", (project_id, base_id, embedding_model_id)))
            return KnowledgeRebuildResult(
                base=base_view,
                accepted_document_count=2,
                skipped_document_ids=(uuid.UUID(int=7),),
            )

    module = _BaseModule()
    rebuild_embedding_model_id = uuid.uuid4()
    async with _client(_app(module)) as client:
        created = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases",
            json={"name": "产品手册", "embedding_model_id": str(base_view.embedding_model_id)},
        )
        listed = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/bases", params={"page_size": 5})
        fetched = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}")
        patched = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}",
            json={"status": "disabled", "default_top_k": 8, "default_score_threshold": 0.35, "retrieval_mode": "hybrid"},
        )
        bad_mode = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}",
            json={"retrieval_mode": "fancy"},
        )
        rebuilt = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/rebuild",
            json={"embedding_model_id": str(rebuild_embedding_model_id)},
        )
        rebuild_missing_body = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/rebuild",
            json={},
        )

    assert created.status_code == 200
    assert created.json()["item"]["document_count"] == 2
    assert created.json()["item"]["default_top_k"] == 4
    assert created.json()["item"]["default_score_threshold"] == 0.2
    assert created.json()["item"]["retrieval_mode"] == "semantic"
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert fetched.status_code == 200
    assert patched.status_code == 200
    assert bad_mode.status_code == 422
    assert rebuilt.status_code == 200
    assert rebuilt.json()["item"]["id"] == str(_BASE_ID)
    assert rebuilt.json()["accepted_document_count"] == 2
    assert rebuilt.json()["skipped_document_ids"] == [str(uuid.UUID(int=7))]
    assert rebuild_missing_body.status_code == 422

    verbs = [verb for verb, _ in module.calls]
    assert verbs == ["create_base", "list_bases", "get_base", "update_base", "rebuild_base"]
    _, (create_project, create_dto) = module.calls[0]
    assert create_project == _PROJECT_ID
    assert create_dto.name == "产品手册"
    assert create_dto.description == ""
    assert create_dto.retrieval_mode == "semantic"  # the omitted default
    _, (_, _, update_dto) = module.calls[3]
    assert update_dto.status == "disabled"
    assert update_dto.name is None
    assert update_dto.default_top_k == 8
    assert update_dto.default_score_threshold == 0.35
    assert update_dto.retrieval_mode == "hybrid"
    _, (_, rebuild_base_id, rebuild_dto) = module.calls[4]
    assert rebuild_base_id == _BASE_ID
    assert rebuild_dto == rebuild_embedding_model_id
    assert isinstance(rebuild_dto, uuid.UUID)


@pytest.mark.asyncio
async def test_http_m4_routes_round_trip_delete_retry_and_segments() -> None:
    from actweave_knowledge import KnowledgeBaseView, KnowledgeSegmentView

    base_view = KnowledgeBaseView(
        id=_BASE_ID,
        project_id=_PROJECT_ID,
        name="产品手册",
        description="",
        embedding_model_id=uuid.uuid4(),
        reranker_model_id=None,
        retrieval_mode="semantic",
        summary_index_enabled=False,
        status="deleting",
        document_count=1,
        default_top_k=4,
        default_score_threshold=0.2,
        delete_error=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    segment_view = KnowledgeSegmentView(
        id=uuid.uuid4(),
        document_version=2,
        position=0,
        content="第一段内容",
        word_count=5,
        enabled=True,
        hit_count=0,
        source_position={"page": 1},
        created_at=_NOW,
    )

    class _M4Module(_FakeModule):
        async def delete_knowledge_base(self, project_id, base_id, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("delete_base", (project_id, base_id)))
            return base_view

        async def delete_document(self, project_id, document_id, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("delete_document", (project_id, document_id)))
            return _document_view(status="deleting", version=2)

        async def retry_document(self, project_id, document_id, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("retry", (project_id, document_id)))
            return _document_view(status="queued", version=3)

        async def list_document_segments(self, project_id, document_id, *, page=1, page_size=20, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("segments", (project_id, document_id, page, page_size)))
            return [segment_view], 1

    module = _M4Module()
    async with _client(_app(module)) as client:
        base_deleted = await client.delete(f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}")
        document_deleted = await client.delete(f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}")
        retried = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/retry")
        segments = await client.get(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/segments",
            params={"page": 2, "page_size": 5},
        )

    assert base_deleted.status_code == 200
    assert base_deleted.json()["item"]["status"] == "deleting"
    assert document_deleted.status_code == 200
    assert document_deleted.json()["item"]["status"] == "deleting"
    assert retried.status_code == 200
    assert retried.json()["item"]["status"] == "queued"
    assert retried.json()["item"]["version"] == 3
    assert segments.status_code == 200
    body = segments.json()
    assert body["total"] == 1
    assert body["page"] == 2
    assert body["items"][0]["content"] == "第一段内容"
    assert body["items"][0]["source_position"] == {"page": 1}

    assert [verb for verb, _ in module.calls] == ["delete_base", "delete_document", "retry", "segments"]
    assert module.calls[3][1] == (_PROJECT_ID, _DOCUMENT_ID, 2, 5)


@pytest.mark.asyncio
async def test_http_reparse_routes_round_trip_the_module_views() -> None:
    from actweave_knowledge import KnowledgeReparsePreview

    preview = KnowledgeReparsePreview(
        document_version=2,
        preview=KnowledgeChunkPreview(
            total=3,
            chunks=(
                KnowledgeChunkPreviewChunk(
                    position=1,
                    content="第一段",
                    word_count=3,
                    child_contents=("子块",),
                    token_count=3,
                ),
            ),
            preview_fingerprint="d" * 64,
            source_sha256="e" * 64,
            effective_profile=ProcessingProfile(
                parse=make_parse_profile(".txt"),
                chunk=make_chunk_profile(),
            ),
        ),
    )

    class _ReparseModule(_FakeModule):
        async def preview_document_reparse(self, project_id, document_id, request, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("reparse_preview", (project_id, document_id, request)))
            return preview

        async def reparse_document(self, project_id, document_id, request, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("reparse", (project_id, document_id, request)))
            return _document_view(status="queued", version=3)

    module = _ReparseModule()
    async with _client(_app(module)) as client:
        previewed = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/reparse-preview",
            json={"expected_version": 2, "chunk_size": 300, "chunk_overlap": 0},
        )
        reparsed = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/reparse",
            json={
                "expected_version": 2,
                "chunk_size": 300,
                "chunk_overlap": 0,
                "chunking_mode": "parent_child",
                "child_chunk_size": 150,
                "child_chunk_separator": "。",
            },
        )
        missing_version = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/reparse",
            json={"chunk_size": 300},
        )
        unknown_field = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/reparse",
            json={"expected_version": 2, "embedding_model_id": str(uuid.uuid4())},
        )

    assert previewed.status_code == 200
    body = previewed.json()
    assert body["document_version"] == 2
    assert body["total"] == 3
    assert body["items"] == [
        {
            "position": 1,
            "content": "第一段",
            "word_count": 3,
            "child_contents": ["子块"],
            "token_count": 3,
            "source_spans": [],
            "attachments": [],
        }
    ]
    assert body["preview_fingerprint"] == "d" * 64
    assert body["source_sha256"] == "e" * 64

    assert reparsed.status_code == 200
    assert reparsed.json()["item"]["status"] == "queued"
    assert reparsed.json()["item"]["version"] == 3

    # expected_version is mandatory, and a model change is not even a field.
    assert missing_version.status_code == 422
    assert unknown_field.status_code == 422

    verbs = [verb for verb, _ in module.calls]
    assert verbs == ["reparse_preview", "reparse"]
    _, (_, _, preview_request) = module.calls[0]
    assert preview_request.expected_version == 2
    assert preview_request.chunk_size == 300
    _, (_, _, reparse_request) = module.calls[1]
    assert reparse_request.chunking_mode == "parent_child"
    assert reparse_request.child_chunk_size == 150
    assert reparse_request.child_chunk_separator == "。"


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "stage", "document_status"), [("reembed_document", "embedding", "processing"), ("summarize_document", "summarizing", "ready")])
async def test_http_document_views_project_task_progress(temp_path_tracker: list[Path], kind: str, stage: str, document_status: str) -> None:
    """List and detail responses carry the current-generation task progress —
    or an explicit null — and expose no claim or lease material."""

    from actweave_knowledge import KnowledgeTaskProgress

    progress = KnowledgeTaskProgress(
        kind=kind,
        status="retry_wait",
        stage=stage,
        completed_units=6,
        total_units=9,
        attempt_count=1,
        max_attempts=3,
        target_version=2,
        next_attempt_at=datetime(2026, 8, 30, 4, 0, tzinfo=UTC),
    )
    running_id = uuid.UUID(int=41)
    idle_id = uuid.UUID(int=42)

    class _ProgressModule(_FakeModule):
        async def list_documents(self, project_id, base_id, *, page=1, page_size=20, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("list_documents", (project_id, base_id, page, page_size)))
            return (
                [
                    _document_view(id=running_id, status=document_status, version=2, task_progress=progress),
                    _document_view(id=idle_id, status="ready"),
                ],
                2,
            )

        async def get_document(self, project_id, document_id, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            self.calls.append(("get_document", (project_id, document_id)))
            return _document_view(id=document_id, status=document_status, version=2, task_progress=progress)

    module = _ProgressModule()
    async with _client(_app(module)) as client:
        listed = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/documents")
        fetched = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/documents/{running_id}")

    assert listed.status_code == 200
    items = {item["id"]: item for item in listed.json()["items"]}
    running_progress = items[str(running_id)]["task_progress"]
    assert running_progress == {
        "kind": kind,
        "status": "retry_wait",
        "stage": stage,
        "completed_units": 6,
        "total_units": 9,
        "attempt_count": 1,
        "max_attempts": 3,
        "target_version": 2,
        "next_attempt_at": "2026-08-30T04:00:00Z",
    }
    assert items[str(idle_id)]["task_progress"] is None

    assert fetched.status_code == 200
    assert fetched.json()["item"]["task_progress"]["stage"] == stage


@pytest.mark.asyncio
async def test_http_m4_routes_map_knowledge_errors_to_status_codes() -> None:
    class _FailingModule(_FakeModule):
        async def retry_document(self, project_id, document_id, *, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            raise KnowledgeError(KNOWLEDGE_INVALID_REQUEST, "仅 failed 状态的文档支持重试")

        async def list_document_segments(self, project_id, document_id, *, page=1, page_size=20, authority):  # noqa: ANN001
            assert authority.project_id == project_id
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")

    module = _FailingModule()
    async with _client(_app(module)) as client:
        retried = await client.post(f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/retry")
        segments = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/segments")

    assert retried.status_code == 422
    assert retried.json()["detail"]["code"] == KNOWLEDGE_INVALID_REQUEST
    assert segments.status_code == 404
    assert segments.json()["detail"]["code"] == KNOWLEDGE_NOT_FOUND


# ---------------------------------------------------------------------------
# Review fixes: cancellation cleanup, deferred deletes, guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_upload_still_removes_the_row_and_object(postgres_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A client disconnect during the object put must roll back like any error."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        put_started = asyncio.Event()
        finish_put = asyncio.Event()

        async def _stalled_put(key: str, source_path: Path, *, media_type: str | None = None) -> None:
            put_started.set()
            await finish_put.wait()  # finite external I/O must drain before cleanup

        monkeypatch.setattr(harness.store, "upload_from", _stalled_put)

        upload_task = asyncio.create_task(harness.service.upload_document(project_id, base_id, _upload(tmp_path)))
        await asyncio.wait_for(put_started.wait(), timeout=5)
        upload_task.cancel()
        await asyncio.get_running_loop().run_in_executor(None, lambda: None)
        assert not upload_task.done()
        finish_put.set()
        with pytest.raises(asyncio.CancelledError):
            await upload_task

        assert await _table_counts(harness) == (0, 0)
        assert harness.store.deletes, "cleanup must still try to delete the object"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_cleanup_defers_to_the_delete_worker_when_the_object_delete_fails(postgres_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Object-store outage during rollback: keep the row and enqueue a delete task."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)

        async def _broken_publish(*args: object, **kwargs: object) -> KnowledgeDocumentView:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "任务创建失败")

        async def _broken_delete(key: str) -> None:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "对象存储不可用")

        monkeypatch.setattr(harness.service, "_publish_queued_document", _broken_publish)
        monkeypatch.setattr(harness.store, "delete", _broken_delete)

        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path))
        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE

        async with harness.factory() as session:
            document = await session.scalar(select(KnowledgeDocumentRow))
            assert document is not None
            assert document.status == "deleting"
            task = await session.scalar(select(KnowledgeTaskRow).where(KnowledgeTaskRow.kind == "delete_document_object"))
            assert task is not None
            assert task.status == "queued"
            assert task.resource_id == document.id
            assert task.storage_key == document.storage_key
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_upload_rejects_malformed_media_types(postgres_database_url: str, tmp_path: Path) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(
                project_id,
                base_id,
                _upload(tmp_path, media_type="not a mime type"),
            )
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert await _table_counts(harness) == (0, 0)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_download_temp_file_is_removed_when_the_client_aborts(tmp_path: Path) -> None:
    """A mid-body disconnect skips Starlette's background task; the response must clean up itself."""

    staged = tmp_path / "download-copy.pdf"
    staged.write_bytes(b"x" * 2048)
    response = gateway._TempFileResponse(path=staged, filename="报告.pdf", media_type="application/pdf")

    async def _receive() -> dict[str, object]:
        return {"type": "http.request"}

    async def _send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            raise ConnectionResetError("client went away")

    with pytest.raises(ConnectionResetError):
        await response({"type": "http", "method": "GET", "headers": []}, _receive, _send)

    assert not staged.exists()


@pytest.mark.asyncio
async def test_http_download_cleans_temp_even_for_range_requests(temp_path_tracker: list[Path]) -> None:
    module = _FakeModule()
    async with _client(_app(module)) as client:
        response = await client.get(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/download",
            headers={"range": "bytes=malformed"},
        )

    assert response.status_code in (200, 206, 400, 416)
    assert temp_path_tracker and all(not path.exists() for path in temp_path_tracker)


@pytest.mark.asyncio
async def test_http_upload_precheck_rejects_declared_oversized_bodies_before_staging(
    temp_path_tracker: list[Path],
) -> None:
    """A Content-Length far above the cap fails before any byte is copied to disk."""

    module = _FakeModule()  # upload_max_bytes=64; allowance is 1 MiB
    oversized = b"x" * (2 * 1024 * 1024)
    async with _client(_app(module)) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/documents",
            files={"file": ("big.bin", oversized, "application/octet-stream")},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == KNOWLEDGE_INVALID_REQUEST
    assert module.calls == []
    assert temp_path_tracker == [], "the pre-check must fire before a temp file is created"


@pytest.mark.asyncio
async def test_http_routes_answer_disabled_when_the_module_is_absent() -> None:
    app = FastAPI()
    app.include_router(gateway.project_router)
    context = ProjectContext(
        user_id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=1,
        request_id=_REQUEST_ID,
    )
    app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: context
    app.dependency_overrides[gateway.require_project_knowledge_edit] = lambda: context

    async with _client(app) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/bases")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == KNOWLEDGE_DISABLED


def test_project_routes_declare_exactly_the_documented_capability_guards() -> None:
    """Every project route must carry exactly one capability dependency, per the plan's table."""

    from fastapi.routing import APIRoute

    prefix = "/api/projects/{project_id}/knowledge"
    expected = {
        ("GET", f"{prefix}/model-options"): "read",
        ("GET", f"{prefix}/file-capabilities"): "read",
        ("GET", f"{prefix}/health"): "read",
        ("POST", f"{prefix}/bases"): "edit",
        ("GET", f"{prefix}/bases"): "read",
        ("GET", f"{prefix}/bases/{{base_id}}"): "read",
        ("PATCH", f"{prefix}/bases/{{base_id}}"): "edit",
        ("DELETE", f"{prefix}/bases/{{base_id}}"): "edit",
        ("POST", f"{prefix}/bases/{{base_id}}/documents"): "edit",
        ("POST", f"{prefix}/chunk-preview"): "edit",
        ("GET", f"{prefix}/bases/{{base_id}}/documents"): "read",
        ("GET", f"{prefix}/documents/{{document_id}}"): "read",
        ("GET", f"{prefix}/documents/{{document_id}}/attachments"): "edit",
        ("GET", f"{prefix}/documents/{{document_id}}/download"): "read",
        ("GET", f"{prefix}/documents/{{document_id}}/segments/{{segment_id}}/attachments/{{attachment_id}}"): "read",
        ("DELETE", f"{prefix}/documents/{{document_id}}"): "edit",
        ("POST", f"{prefix}/documents/{{document_id}}/retry"): "edit",
        ("POST", f"{prefix}/documents/{{document_id}}/reparse-preview"): "edit",
        ("POST", f"{prefix}/documents/{{document_id}}/reparse"): "edit",
        ("PATCH", f"{prefix}/documents/{{document_id}}"): "edit",
        ("POST", f"{prefix}/documents/batch-status"): "edit",
        ("POST", f"{prefix}/documents/batch-delete"): "edit",
        ("GET", f"{prefix}/documents/{{document_id}}/segments"): "read",
        ("GET", f"{prefix}/bases/{{base_id}}/documents/{{document_id}}/segments/{{segment_id}}"): "read",
        ("GET", f"{prefix}/bases/{{base_id}}/documents/{{document_id}}/segments/{{segment_id}}/attachments/{{attachment_id}}"): "read",
        ("POST", f"{prefix}/documents/{{document_id}}/segments"): "edit",
        ("PATCH", f"{prefix}/segments/{{segment_id}}"): "edit",
        ("DELETE", f"{prefix}/segments/{{segment_id}}"): "edit",
        ("POST", f"{prefix}/search"): "read",
        ("GET", f"{prefix}/bases/{{base_id}}/queries"): "read",
        ("POST", f"{prefix}/bases/{{base_id}}/rebuild"): "edit",
        ("GET", f"{prefix}/bases/{{base_id}}/metadata-fields"): "read",
        ("POST", f"{prefix}/bases/{{base_id}}/metadata-fields"): "edit",
        ("PATCH", f"{prefix}/metadata-fields/{{field_id}}"): "edit",
        ("DELETE", f"{prefix}/metadata-fields/{{field_id}}"): "edit",
        ("PATCH", f"{prefix}/documents/{{document_id}}/metadata"): "edit",
        ("GET", f"{prefix}/filter-fields"): "read",
        ("PATCH", f"{prefix}/bases/{{base_id}}/documents/metadata"): "edit",
    }
    guards = {
        gateway.require_project_knowledge_read: "read",
        gateway.require_project_knowledge_edit: "edit",
    }

    seen: dict[tuple[str, str], str] = {}
    for route in gateway.project_router.routes:
        assert isinstance(route, APIRoute)
        capabilities = [guards[dependency.call] for dependency in route.dependant.dependencies if dependency.call in guards]
        assert len(capabilities) == 1, f"{route.path} 必须恰好声明一个项目能力守卫"
        for method in route.methods - {"HEAD", "OPTIONS"}:
            seen[(method, route.path)] = capabilities[0]

    assert seen == expected


@pytest.mark.asyncio
async def test_original_put_observes_reserved_row_and_commits_bytes(postgres_database_url, tmp_path):
    import hashlib

    from extraction_test_helpers import ExtractionObjectStore

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        store = ExtractionObjectStore()
        harness.service._object_store = store
        gate = store.pause("put")
        pending = asyncio.create_task(harness.service.upload_document(project_id, base_id, _upload(tmp_path, b"original")))
        async with asyncio.timeout(10):
            await gate.entered.wait()
            try:
                async with harness.factory() as session:
                    row = await session.scalar(select(KnowledgeDocumentRow))
                    assert row.quota_state == "reserved"
                    assert row.upload_state == "pending"
                    assert row.source_sha256 == hashlib.sha256(b"original").hexdigest()
            finally:
                gate.released.set()
            await pending
        async with harness.factory() as session:
            row = await session.scalar(select(KnowledgeDocumentRow))
            assert row.quota_state == "committed"
            assert row.upload_state == "stored"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_confirmed_original_put_retains_committed_tombstone_if_cleanup_fails(postgres_database_url, tmp_path):
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        harness.store.fail_delete = True
        with pytest.raises(KnowledgeError):
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path), authority=_RevokedAfterFirstTransaction(project_id))
        async with harness.factory() as session:
            row = await session.scalar(select(KnowledgeDocumentRow))
            assert row.status == "deleting"
            assert row.quota_state == "committed"
            assert row.upload_state == "delete_pending"
            assert row.storage_key in harness.store.objects
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_original_cleanup_requires_confirmed_absence_before_release(
    postgres_database_url,
    tmp_path,
    monkeypatch,
):
    from deerflow.persistence.quotas.model import ProjectUsageCounterRow

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)

        async def acknowledged_noop_delete(key: str) -> None:
            harness.store.deletes.append(key)

        monkeypatch.setattr(harness.store, "delete", acknowledged_noop_delete)
        with pytest.raises(KnowledgeError):
            await harness.service.upload_document(
                project_id,
                base_id,
                _upload(tmp_path),
                authority=_RevokedAfterFirstTransaction(project_id),
            )

        async with harness.factory() as session:
            row = await session.scalar(select(KnowledgeDocumentRow))
            cleanup = await session.scalar(
                select(KnowledgeTaskRow).where(
                    KnowledgeTaskRow.kind == "delete_document_object",
                    KnowledgeTaskRow.status == "queued",
                )
            )
            counter = await session.scalar(
                select(ProjectUsageCounterRow).where(
                    ProjectUsageCounterRow.project_id == project_id,
                    ProjectUsageCounterRow.dimension == "storage_bytes",
                )
            )
            assert row is not None
            assert (row.status, row.upload_state, row.quota_state) == (
                "deleting",
                "delete_pending",
                "committed",
            )
            assert cleanup is not None and cleanup.storage_key == row.storage_key
            assert counter is not None and (counter.used, counter.reserved) == (
                row.size_bytes,
                0,
            )
            assert row.storage_key in harness.store.objects
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_late_original_delete_releases_same_reservation_after_row_was_removed(postgres_database_url, tmp_path):
    from extraction_test_helpers import ExtractionObjectStore
    from sqlalchemy import delete

    from deerflow.persistence.quotas.model import ProjectUsageCounterRow

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        store = ExtractionObjectStore()
        harness.service._object_store = store
        gate = store.pause("put")
        pending = asyncio.create_task(harness.service.upload_document(project_id, base_id, _upload(tmp_path)))
        async with asyncio.timeout(10):
            await gate.entered.wait()
            async with harness.factory() as session, session.begin():
                await session.execute(delete(KnowledgeDocumentRow))
            gate.released.set()
            with pytest.raises(KnowledgeError):
                await pending
        async with harness.factory() as session:
            counter = await session.scalar(select(ProjectUsageCounterRow).where(ProjectUsageCounterRow.dimension == "storage_bytes"))
            assert counter.reserved == 0 and counter.used == 0
        assert not store.objects
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_joins_started_original_cleanup(postgres_database_url, tmp_path):
    from extraction_test_helpers import ExtractionObjectStore

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        store = ExtractionObjectStore()
        harness.service._object_store = store
        gate = store.pause("delete")
        pending = asyncio.create_task(harness.service.upload_document(project_id, base_id, _upload(tmp_path), authority=_RevokedAfterFirstTransaction(project_id)))
        async with asyncio.timeout(10):
            await gate.entered.wait()
            pending.cancel()
            await asyncio.get_running_loop().run_in_executor(None, lambda: None)
            try:
                assert not pending.done()
            finally:
                gate.released.set()
            with pytest.raises((KnowledgeError, asyncio.CancelledError)):
                await pending
        assert not store.objects
        assert await _table_counts(harness) == (0, 0)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_joins_started_original_absence_confirmation(
    postgres_database_url,
    tmp_path,
):
    from extraction_test_helpers import ExtractionObjectStore

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        store = ExtractionObjectStore()
        harness.service._object_store = store
        gate = store.pause("get")
        pending = asyncio.create_task(
            harness.service.upload_document(
                project_id,
                base_id,
                _upload(tmp_path),
                authority=_RevokedAfterFirstTransaction(project_id),
            )
        )
        async with asyncio.timeout(10):
            await gate.entered.wait()
            pending.cancel()
            await asyncio.get_running_loop().run_in_executor(None, lambda: None)
            try:
                assert not pending.done()
            finally:
                gate.released.set()
            with pytest.raises((KnowledgeError, asyncio.CancelledError)):
                await pending
        assert not store.objects
        assert await _table_counts(harness) == (0, 0)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_inactive_project_does_not_bypass_resource_hiding_authority(postgres_database_url, tmp_path):
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        async with harness.factory() as session, session.begin():
            await session.execute(text("UPDATE projects SET status='pending_deletion' WHERE id=:id"), {"id": project_id})
        authority = _RevokedAfterFirstTransaction(project_id)
        authority.calls = 1
        with pytest.raises(KnowledgeError) as error:
            await harness.service.upload_document(project_id, base_id, _upload(tmp_path), authority=authority)
        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert not harness.store.objects
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_real_adapter_put_commits_confirmed_bytes_when_delete_fails(postgres_database_url, tmp_path, monkeypatch):
    import threading

    from actweave_knowledge.contracts import KnowledgeMinioSettings
    from actweave_knowledge.storage import MinioObjectStore
    from actweave_knowledge.storage import minio_store as minio_store_module

    from deerflow.persistence.quotas.model import ProjectUsageCounterRow

    harness = await _harness(postgres_database_url)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    physical = {}

    class BarrierMinio:
        def __init__(self, *args, **kwargs):
            pass

        def bucket_exists(self, bucket):
            return True

        def get_bucket_versioning(self, bucket):
            return SimpleNamespace(status=None)

        def fput_object(self, bucket, key, source_path, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=10):
                raise AssertionError("PUT barrier was not released")
            physical[key] = Path(source_path).read_bytes()

        def remove_object(self, bucket, key):
            raise RuntimeError("injected delete failure")

    monkeypatch.setattr(minio_store_module, "Minio", BarrierMinio)
    harness.service._object_store = MinioObjectStore(KnowledgeMinioSettings(endpoint="minio.invalid:9000", bucket="test-cancel", access_key="test", secret_key="test", secure=False))
    pending = None
    try:
        project_id, base_id = await _seed_base(harness)
        payload = b"confirmed original PUT"
        pending = asyncio.create_task(harness.service.upload_document(project_id, base_id, _upload(tmp_path, payload)))
        async with asyncio.timeout(10):
            await entered.wait()
            for _ in range(2):
                pending.cancel()
                await asyncio.get_running_loop().run_in_executor(None, lambda: None)
                assert not pending.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        async with harness.factory() as session:
            document = await session.scalar(select(KnowledgeDocumentRow))
            counter = await session.scalar(select(ProjectUsageCounterRow).where(ProjectUsageCounterRow.project_id == project_id, ProjectUsageCounterRow.dimension == "storage_bytes"))
            assert document.storage_key in physical
            assert document.status == "deleting" and document.upload_state == "delete_pending"
            assert document.quota_state == "committed"
            assert counter.used == len(payload) and counter.reserved == 0
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await harness.engine.dispose()
