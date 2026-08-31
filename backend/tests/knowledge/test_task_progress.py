"""M10 T4 — real pipeline stages, verified batch progress, and attempt state.

The ingest and re-embed handlers must report what they are actually doing:
stage transitions happen in short claim-guarded transactions, ``completed_units``
counts only provider batches whose responses validated, a new attempt starts
from zero, and a lost lease stops undispatched provider batches — including
the client's internal retry. Document views project the open indexing task of
the current generation without leaking execution material.

Provider traffic goes through the real ``KnowledgeModelClient`` over an async
mock transport, so batch boundaries and the in-client retry are the real ones.
Everything else runs against the installed Schema V1 snapshot in a disposable
PostgreSQL database.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KNOWLEDGE_TASK_FAILED,
    KnowledgeError,
    KnowledgeSettings,
)
from actweave_knowledge.documents import KnowledgeDocumentService
from actweave_knowledge.ingestion.pipeline import KnowledgeIngestionHandler
from actweave_knowledge.ingestion.reembed import KnowledgeReembedHandler
from actweave_knowledge.models import KnowledgeModelClient
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentRow,
    KnowledgeTaskRow,
)
from actweave_knowledge.persistence.tasks import claim_next_task, settle_task_failure
from actweave_knowledge.tasks import KnowledgeTaskClaim
from registry_helpers import registry_model_port, seed_embedding_model, seed_provider
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from deerflow.persistence.bootstrap import _install_full_schema

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_DIMENSION = 4


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def download_to(self, key: str, target_path: Path) -> None:
        data = self.objects.get(key)
        if data is None:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "文档文件在对象存储中缺失")
        await asyncio.to_thread(target_path.write_bytes, data)


class _Provider:
    """Async mock transport that can inspect the task row mid-flight."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.requests: list[list[str]] = []
        # (stage, completed_units, total_units) observed while each provider
        # request was being answered.
        self.observed: list[tuple[str, int, int | None]] = []
        self.fail_from_request: int | None = None
        self.on_request = None
        self.task_id: uuid.UUID | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)["input"]
        self.requests.append(list(batch))
        if self.task_id is not None:
            async with self._factory() as session:
                row = await session.get(KnowledgeTaskRow, self.task_id)
                assert row is not None
                self.observed.append((row.stage, row.completed_units, row.total_units))
        if self.on_request is not None:
            await self.on_request(len(self.requests))
        if self.fail_from_request is not None and len(self.requests) >= self.fail_from_request:
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={"data": [{"index": index, "embedding": [0.1, 0.2, 0.3, 0.4]} for index in range(len(batch))]},
        )


class _Harness:
    def __init__(self, engine, factory, store: _FakeStore, provider: _Provider, client: KnowledgeModelClient) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.store = store
        self.provider = provider
        self.client = client

    def ingest_handler(self) -> KnowledgeIngestionHandler:
        return KnowledgeIngestionHandler(
            session_factory=self.factory,
            settings=KnowledgeSettings.model_validate({"enabled": False}),
            object_store=self.store,  # type: ignore[arg-type]
            model_client=self.client,
            model_port=registry_model_port(),
        )

    def reembed_handler(self) -> KnowledgeReembedHandler:
        return KnowledgeReembedHandler(
            session_factory=self.factory,
            model_client=self.client,
            model_port=registry_model_port(),
        )

    def documents(self) -> KnowledgeDocumentService:
        return KnowledgeDocumentService(
            session_factory=self.factory,
            settings=KnowledgeSettings.model_validate({"enabled": False}),
            object_store=self.store,  # type: ignore[arg-type]
        )

    async def dispose(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()


async def _harness(postgres_database_url: str) -> _Harness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    store = _FakeStore()
    provider = _Provider(factory)
    client = KnowledgeModelClient(http=httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    return _Harness(engine, factory, store, provider, client)


async def _seed_project(session: AsyncSession, label: str) -> uuid.UUID:
    user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (:user_id, :email, :username, 'user', now(), false, 1)"""
        ),
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"m10p_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"m10p-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


async def _seed_base(harness: _Harness, *, max_batch: int = 2) -> tuple[uuid.UUID, uuid.UUID]:
    provider_id = await seed_provider(harness.factory)
    embedding_model_id = await seed_embedding_model(
        harness.factory,
        provider_id,
        dimension=_DIMENSION,
        max_batch=max_batch,
    )
    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base_id = uuid.uuid4()
        session.add(
            KnowledgeBaseRow(
                id=base_id,
                project_id=project_id,
                name=f"base-{base_id.hex[:8]}",
                embedding_model_id=embedding_model_id,
                status="active",
            )
        )
    return project_id, base_id


async def _seed_queued_document(
    harness: _Harness,
    project_id: uuid.UUID,
    base_id: uuid.UUID,
) -> uuid.UUID:
    """A queued document whose stored original splits into three segments.

    Each paragraph is 150 characters against ``chunk_size=200`` with zero
    overlap, so the splitter cannot merge neighbours: exactly three drafts.
    """

    content = "\n\n".join(("甲" * 150, "乙" * 150, "丙" * 150)).encode()
    document_id = uuid.uuid4()
    storage_key = f"projects/{project_id}/knowledge/{base_id}/{document_id}.md"
    async with harness.factory() as session, session.begin():
        session.add(
            KnowledgeDocumentRow(
                id=document_id,
                project_id=project_id,
                knowledge_base_id=base_id,
                name="note.md",
                original_name="note.md",
                storage_key=storage_key,
                size_bytes=len(content),
                status="queued",
                version=1,
                chunk_size=200,
                chunk_overlap=0,
            )
        )
        session.add(
            KnowledgeTaskRow(
                id=uuid.uuid4(),
                project_id=project_id,
                resource_id=document_id,
                kind="ingest_document",
                target_version=1,
                status="queued",
            )
        )
    harness.store.objects[storage_key] = content
    return document_id


async def _seed_published_document(
    harness: _Harness,
    project_id: uuid.UUID,
    base_id: uuid.UUID,
    *,
    contents: tuple[str, ...] = ("旧行甲", "旧行乙", "旧行丙"),
) -> uuid.UUID:
    """A ready document with published rows plus a queued re-embed task."""

    document_id = uuid.uuid4()
    async with harness.factory() as session, session.begin():
        session.add(
            KnowledgeDocumentRow(
                id=document_id,
                project_id=project_id,
                knowledge_base_id=base_id,
                name="note.md",
                original_name="note.md",
                storage_key=f"projects/{project_id}/knowledge/{base_id}/{document_id}.md",
                size_bytes=32,
                status="queued",
                version=2,
                published_version=1,
                segment_count=len(contents),
                word_count=sum(len(item) for item in contents),
            )
        )
        for position, item in enumerate(contents, start=1):
            session.add(
                KnowledgeSegmentRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    knowledge_document_id=document_id,
                    document_version=1,
                    position=position,
                    content=item,
                    word_count=len(item),
                    source_position={"page": position},
                    embedding=[0.25] * _DIMENSION,
                )
            )
        session.add(
            KnowledgeTaskRow(
                id=uuid.uuid4(),
                project_id=project_id,
                resource_id=document_id,
                kind="reembed_document",
                target_version=2,
                status="queued",
            )
        )
    return document_id


async def _claim(harness: _Harness) -> KnowledgeTaskClaim:
    async with harness.factory() as session, session.begin():
        row = await claim_next_task(session, lease_seconds=60)
        assert row is not None, "expected a claimable task"
        harness.provider.task_id = row.id
        return KnowledgeTaskClaim(
            id=row.id,
            project_id=row.project_id,
            resource_id=row.resource_id,
            kind=row.kind,
            target_version=row.target_version,
            claim_token=row.claim_token,  # type: ignore[arg-type]
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            storage_key=row.storage_key,
            reparse_settings=row.reparse_settings,
        )


async def _task_row(harness: _Harness, task_id: uuid.UUID) -> KnowledgeTaskRow:
    async with harness.factory() as session:
        row = await session.get(KnowledgeTaskRow, task_id)
        assert row is not None
        return row


async def _document_row(harness: _Harness, document_id: uuid.UUID) -> KnowledgeDocumentRow:
    async with harness.factory() as session:
        row = await session.get(KnowledgeDocumentRow, document_id)
        assert row is not None
        return row


async def _swap_claim_token(harness: _Harness, task_id: uuid.UUID) -> None:
    """Simulate another worker re-claiming the lease mid-execution."""

    async with harness.factory() as session, session.begin():
        await session.execute(update(KnowledgeTaskRow).where(KnowledgeTaskRow.id == task_id).values(claim_token=uuid.uuid4()))


# ---------------------------------------------------------------------------
# Ingest: stages and verified batch progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_reports_stages_and_verified_batch_progress(postgres_database_url: str) -> None:
    """Three segments over max_batch=2 are two provider batches. The task row
    must show ``embedding`` with a verifiable total while batches run, count
    only validated batches, and reach ``done`` exactly with the publish."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        document_id = await _seed_queued_document(harness, project_id, base_id)
        claim = await _claim(harness)

        claimed = await _task_row(harness, claim.id)
        assert (claimed.stage, claimed.completed_units, claimed.total_units) == ("queued", 0, None)

        await harness.ingest_handler()(claim)

        # Batch one saw the freshly initialized embedding stage; batch two saw
        # exactly the first batch's verified size — never a simulated total.
        assert harness.provider.observed == [("embedding", 0, 3), ("embedding", 2, 3)]

        settled = await _task_row(harness, claim.id)
        assert settled.status == "succeeded"
        assert settled.stage == "done"
        assert (settled.completed_units, settled.total_units) == (3, 3)
        assert settled.progress_updated_at is not None

        document = await _document_row(harness, document_id)
        assert document.status == "ready"
        assert document.segment_count == 3
    finally:
        await harness.dispose()


@pytest.mark.asyncio
async def test_second_batch_failure_keeps_verified_progress_and_failing_stage(postgres_database_url: str) -> None:
    """A second-batch provider failure keeps batch one's verified progress and
    the ``embedding`` stage on the settled row, never marks the document ready
    early, and the next claim starts the new attempt from zero."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        document_id = await _seed_queued_document(harness, project_id, base_id)
        harness.provider.fail_from_request = 2

        claim = await _claim(harness)
        with pytest.raises(KnowledgeError):
            await harness.ingest_handler()(claim)
        # Batch two dispatched once plus one in-client retry; batch one stayed
        # verified.
        assert len(harness.provider.requests) == 3

        async with harness.factory() as session, session.begin():
            outcome = await settle_task_failure(
                session,
                claim.id,
                claim.claim_token,
                error_message="Embedding 调用失败",
                retry_delay_seconds=0,
            )
        assert outcome == "retry_wait"

        settled = await _task_row(harness, claim.id)
        assert settled.stage == "embedding"
        assert (settled.completed_units, settled.total_units) == (2, 3)

        document = await _document_row(harness, document_id)
        assert document.status == "processing"
        async with harness.factory() as session:
            rows = (await session.execute(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_document_id == document_id))).scalars().all()
        assert rows == []

        # The retry claims a fresh attempt: counters reset before any batch.
        harness.provider.fail_from_request = None
        harness.provider.observed.clear()
        retry_claim = await _claim(harness)
        assert retry_claim.attempt_count == 2
        reset = await _task_row(harness, claim.id)
        assert (reset.stage, reset.completed_units, reset.total_units) == ("queued", 0, None)

        await harness.ingest_handler()(retry_claim)
        assert harness.provider.observed == [("embedding", 0, 3), ("embedding", 2, 3)]
        final = await _task_row(harness, claim.id)
        assert final.status == "succeeded" and final.stage == "done"
        assert (await _document_row(harness, document_id)).status == "ready"
    finally:
        await harness.dispose()


@pytest.mark.asyncio
async def test_lost_lease_stops_undispatched_batches(postgres_database_url: str) -> None:
    """When the lease is re-claimed while batch one is in flight, recording
    that batch's progress fails the claim guard and batch two is never sent."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        await _seed_queued_document(harness, project_id, base_id)
        claim = await _claim(harness)

        async def _steal_lease(request_number: int) -> None:
            if request_number == 1:
                await _swap_claim_token(harness, claim.id)

        harness.provider.on_request = _steal_lease

        with pytest.raises(KnowledgeError) as error:
            await harness.ingest_handler()(claim)

        assert error.value.code == KNOWLEDGE_TASK_FAILED
        assert len(harness.provider.requests) == 1
        # The stale attempt's late progress never landed on the stolen row.
        stolen = await _task_row(harness, claim.id)
        assert stolen.completed_units == 0
    finally:
        await harness.dispose()


@pytest.mark.asyncio
async def test_lease_loss_before_the_429_retry_prevents_the_second_attempt(postgres_database_url: str) -> None:
    """A 429 answer normally triggers the client's single internal retry; the
    per-attempt guard must stop that retry once the lease is gone."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        await _seed_queued_document(harness, project_id, base_id)
        claim = await _claim(harness)

        original_call = harness.provider.__call__

        async def _rate_limited(request: httpx.Request) -> httpx.Response:
            await original_call(request)
            await _swap_claim_token(harness, claim.id)
            return httpx.Response(429)

        harness.client._http = httpx.AsyncClient(transport=httpx.MockTransport(_rate_limited))  # noqa: SLF001

        with pytest.raises(KnowledgeError) as error:
            await harness.ingest_handler()(claim)

        assert error.value.code == KNOWLEDGE_TASK_FAILED
        assert len(harness.provider.requests) == 1
    finally:
        await harness.dispose()


# ---------------------------------------------------------------------------
# Re-embed: stages over existing rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reembed_reports_loading_then_embedding_and_publishes_done(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        document_id = await _seed_published_document(harness, project_id, base_id)
        claim = await _claim(harness)
        assert claim.kind == "reembed_document"

        await harness.reembed_handler()(claim)

        # Both provider batches ran inside the embedding stage with the row
        # count as the verifiable total.
        assert harness.provider.observed == [("embedding", 0, 3), ("embedding", 2, 3)]

        settled = await _task_row(harness, claim.id)
        assert settled.status == "succeeded"
        assert settled.stage == "done"
        assert (settled.completed_units, settled.total_units) == (3, 3)
        document = await _document_row(harness, document_id)
        assert document.status == "ready" and document.published_version == 2
    finally:
        await harness.dispose()


# ---------------------------------------------------------------------------
# Document views: current-generation task progress projection
# ---------------------------------------------------------------------------


async def _seed_projection_document(
    harness: _Harness,
    project_id: uuid.UUID,
    base_id: uuid.UUID,
    *,
    status: str,
    version: int = 1,
) -> uuid.UUID:
    document_id = uuid.uuid4()
    async with harness.factory() as session, session.begin():
        session.add(
            KnowledgeDocumentRow(
                id=document_id,
                project_id=project_id,
                knowledge_base_id=base_id,
                name="note.md",
                original_name="note.md",
                storage_key=f"projects/{project_id}/knowledge/{base_id}/{document_id}.md",
                size_bytes=16,
                status=status,
                version=version,
                error_message="失败原因" if status == "failed" else None,
            )
        )
    return document_id


async def _seed_progress_task(
    harness: _Harness,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    kind: str = "ingest_document",
    status: str,
    target_version: int = 1,
    stage: str = "queued",
    completed_units: int = 0,
    total_units: int | None = None,
    attempt_count: int = 0,
    available_at: datetime | None = None,
    claim_token: uuid.UUID | None = None,
    lease_until: datetime | None = None,
) -> uuid.UUID:
    task_id = uuid.uuid4()
    async with harness.factory() as session, session.begin():
        session.add(
            KnowledgeTaskRow(
                id=task_id,
                project_id=project_id,
                resource_id=document_id,
                kind=kind,
                target_version=target_version,
                status=status,
                stage=stage,
                completed_units=completed_units,
                total_units=total_units,
                attempt_count=attempt_count,
                available_at=available_at or datetime.now(UTC),
                claim_token=claim_token,
                lease_until=lease_until,
                finished_at=datetime.now(UTC) if status in ("succeeded", "failed") else None,
            )
        )
    return task_id


@pytest.mark.asyncio
async def test_document_views_carry_current_generation_task_progress(postgres_database_url: str) -> None:
    """List and detail views project the open indexing task bound to the
    document's current version — queued, retry_wait with its next attempt
    time, and failed keeping its failing stage — while succeeded tasks and
    other generations project nothing. No claim token, lease, or storage key
    fields exist on the projection."""

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        documents = harness.documents()
        next_attempt = datetime.now(UTC) + timedelta(seconds=90)

        queued_doc = await _seed_projection_document(harness, project_id, base_id, status="queued")
        await _seed_progress_task(harness, project_id, queued_doc, status="queued")

        waiting_doc = await _seed_projection_document(harness, project_id, base_id, status="processing")
        await _seed_progress_task(
            harness,
            project_id,
            waiting_doc,
            kind="reembed_document",
            status="retry_wait",
            stage="embedding",
            completed_units=2,
            total_units=5,
            attempt_count=1,
            available_at=next_attempt,
        )

        failed_doc = await _seed_projection_document(harness, project_id, base_id, status="failed")
        await _seed_progress_task(
            harness,
            project_id,
            failed_doc,
            status="failed",
            stage="embedding",
            completed_units=2,
            total_units=5,
            attempt_count=3,
        )

        done_doc = await _seed_projection_document(harness, project_id, base_id, status="ready")
        await _seed_progress_task(
            harness,
            project_id,
            done_doc,
            status="succeeded",
            stage="done",
            completed_units=5,
            total_units=5,
        )

        stale_doc = await _seed_projection_document(harness, project_id, base_id, status="ready", version=2)
        await _seed_progress_task(harness, project_id, stale_doc, status="failed", target_version=1, stage="embedding")

        views, total = await documents.list_documents(project_id, base_id, page=1, page_size=20)
        assert total == 5
        by_id = {view.id: view for view in views}

        queued_progress = by_id[queued_doc].task_progress
        assert queued_progress is not None
        assert (queued_progress.kind, queued_progress.status, queued_progress.stage) == ("ingest_document", "queued", "queued")
        assert (queued_progress.completed_units, queued_progress.total_units) == (0, None)
        assert queued_progress.target_version == 1
        assert queued_progress.next_attempt_at is None

        waiting_progress = by_id[waiting_doc].task_progress
        assert waiting_progress is not None
        assert (waiting_progress.kind, waiting_progress.status, waiting_progress.stage) == ("reembed_document", "retry_wait", "embedding")
        assert (waiting_progress.completed_units, waiting_progress.total_units) == (2, 5)
        assert waiting_progress.next_attempt_at == next_attempt
        assert (waiting_progress.attempt_count, waiting_progress.max_attempts) == (1, 3)

        failed_progress = by_id[failed_doc].task_progress
        assert failed_progress is not None
        assert failed_progress.status == "failed"
        assert failed_progress.stage == "embedding"
        assert failed_progress.next_attempt_at is None

        assert by_id[done_doc].task_progress is None
        assert by_id[stale_doc].task_progress is None

        # The detail view carries the same projection.
        detail = await documents.get_document(project_id, waiting_doc)
        assert detail.task_progress is not None
        assert detail.task_progress.stage == "embedding"
        assert detail.task_progress.next_attempt_at == next_attempt

        # The projection exposes no execution material.
        field_names = {field.name for field in dataclasses.fields(waiting_progress)}
        assert field_names == {
            "kind",
            "status",
            "stage",
            "completed_units",
            "total_units",
            "attempt_count",
            "max_attempts",
            "target_version",
            "next_attempt_at",
        }
    finally:
        await harness.dispose()
