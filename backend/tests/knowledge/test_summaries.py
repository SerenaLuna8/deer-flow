"""Derived-summary lifecycle against Schema V1 and real embedding batching."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from actweave_knowledge import KNOWLEDGE_MODEL_UNAVAILABLE, KNOWLEDGE_TASK_FAILED, KnowledgeError
from actweave_knowledge.extraction.contracts import ProcessingProfile
from actweave_knowledge.ingestion.summarize import KNOWLEDGE_SUMMARY_PROMPT_V1, SUMMARY_PUBLISH_BATCH, KnowledgeSummarizeHandler, source_content_digest
from actweave_knowledge.models import KnowledgeModelClient
from actweave_knowledge.persistence.models import KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeSegmentRow, KnowledgeSegmentSummaryRow, KnowledgeTaskRow
from actweave_knowledge.persistence.tasks import claim_next_task, recover_expired_tasks, settle_task_failure
from actweave_knowledge.tasks import KnowledgeTaskClaim
from extraction_test_helpers import (
    ExtractionObjectStore,
    make_test_file_capability_provider,
    make_test_quota_port,
)
from parsing_test_helpers import make_chunk_profile, make_parse_profile
from registry_helpers import registry_model_port, seed_embedding_model, seed_provider
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.knowledge.composition import is_knowledge_project_active


class _SummaryPort:
    def __init__(self, harness) -> None:
        self.harness = harness
        self.model_ref = "summary-model-v1"
        self.configured = True
        self.active = True
        self.calls: list[tuple[str, str]] = []
        self.observed: list[tuple[str, int, int | None]] = []
        self.on_call = None
        self.output = "这是生成的检索摘要。"

    async def resolve_summary_model(self, session):
        if not self.active:
            raise KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "摘要模型已停用")
        return self.model_ref if self.configured else None

    async def embedding_material(self, session, model_id):
        return await registry_model_port().embedding_material(session, model_id)

    async def lock_model_for_binding(self, session, model_id, model_type):
        return await registry_model_port().lock_model_for_binding(session, model_id, model_type)

    async def generate_summary(self, *, model_ref, prompt):
        self.calls.append((model_ref, prompt))
        self.observed.append(await self.harness.progress())
        if self.on_call:
            await self.on_call(len(self.calls))
        return self.output


class _Harness:
    def __init__(self, engine, factory):
        self.engine, self.factory = engine, factory
        self.port = _SummaryPort(self)
        self.batches: list[list[str]] = []
        self.embedding_observed: list[tuple[str, int, int | None]] = []
        self.on_batch = None
        self.client = KnowledgeModelClient(http=httpx.AsyncClient(transport=httpx.MockTransport(self._embed)))
        self.task_id = None

    async def _embed(self, request):
        batch = json.loads(request.content)["input"]
        self.batches.append(batch)
        self.embedding_observed.append(await self.progress())
        if self.on_batch:
            await self.on_batch(len(self.batches))
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.1, 0.2, 0.3, 0.4]} for i in range(len(batch))]})

    async def progress(self):
        async with self.factory() as session:
            row = await session.get(KnowledgeTaskRow, self.task_id)
            return row.stage, row.completed_units, row.total_units

    def handler(self):
        return KnowledgeSummarizeHandler(session_factory=self.factory, model_client=self.client, model_port=self.port)

    async def seed(
        self,
        contents,
        *,
        enabled=True,
        index_texts=None,
        parsing_profile=None,
    ):
        provider_id = await seed_provider(self.factory)
        model_id = await seed_embedding_model(self.factory, provider_id, dimension=4, max_batch=1)
        self.project_id, self.base_id, self.document_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.segment_ids = [uuid.uuid4() for _ in contents]
        async with self.factory() as session, session.begin():
            user_id = str(uuid.uuid4())
            await session.execute(
                text("INSERT INTO users(id,email,username,system_role,created_at,needs_setup,token_version) VALUES (:id,:email,:username,'user',now(),false,1)"),
                {"id": user_id, "email": user_id + "@example.invalid", "username": "s" + user_id.replace("-", "")[:12]},
            )
            await session.execute(text("INSERT INTO projects(id,slug,display_name,created_by_user_id) VALUES (:id,:slug,'Summary test',:owner)"), {"id": self.project_id, "slug": str(self.project_id), "owner": user_id})
            session.add(KnowledgeBaseRow(id=self.base_id, project_id=self.project_id, name="Summary", embedding_model_id=model_id, summary_index_enabled=enabled))
            session.add(
                KnowledgeDocumentRow(
                    id=self.document_id,
                    project_id=self.project_id,
                    knowledge_base_id=self.base_id,
                    name="summary.md",
                    original_name="summary.md",
                    storage_key="test/document.md",
                    size_bytes=10,
                    status="ready",
                    version=1,
                    published_version=1,
                    parsing_profile=parsing_profile,
                    segment_count=len(contents),
                    word_count=sum(map(len, contents)),
                )
            )
            for index, (segment_id, content) in enumerate(zip(self.segment_ids, contents, strict=True), 1):
                session.add(
                    KnowledgeSegmentRow(
                        id=segment_id,
                        project_id=self.project_id,
                        knowledge_base_id=self.base_id,
                        knowledge_document_id=self.document_id,
                        document_version=1,
                        position=index,
                        content=content,
                        index_text=(index_texts[index - 1] if index_texts is not None else ""),
                        word_count=len(content),
                        embedding=[1.0] * 4,
                        enabled=index != 2,
                    )
                )
        return await self.claim()

    async def claim(self):
        async with self.factory() as session, session.begin():
            self.task_id = uuid.uuid4()
            session.add(KnowledgeTaskRow(id=self.task_id, project_id=self.project_id, resource_id=self.document_id, kind="summarize_document", target_version=1))
        return await self.claim_existing()

    async def claim_existing(self):
        async with self.factory() as session, session.begin():
            row = await claim_next_task(session, lease_seconds=60)
            assert row is not None
            self.task_id = row.id
            return KnowledgeTaskClaim(
                id=row.id,
                project_id=row.project_id,
                resource_id=row.resource_id,
                kind=row.kind,
                target_version=row.target_version,
                claim_token=row.claim_token,
                attempt_count=row.attempt_count,
                max_attempts=row.max_attempts,
                reparse_settings=row.reparse_settings,
            )

    async def summaries(self):
        async with self.factory() as session:
            return (await session.scalars(select(KnowledgeSegmentSummaryRow).order_by(KnowledgeSegmentSummaryRow.knowledge_segment_id))).all()


@pytest_asyncio.fixture
async def harness(postgres_database_url):
    engine = create_async_engine(postgres_database_url)
    h = _Harness(engine, async_sessionmaker(engine, expire_on_commit=False))
    try:
        yield h
    finally:
        await h.client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_summary_generation_batches_progress_and_exact_source_fields(harness):
    contents = ["甲" * 220, "乙" * 200, "短段"]
    claim = await harness.seed(contents)
    await harness.handler()(claim)
    rows = await harness.summaries()
    assert len(rows) == 2  # disabled sources are also covered
    for row in rows:
        content = contents[harness.segment_ids.index(row.knowledge_segment_id)]
        assert (row.project_id, row.knowledge_base_id, row.knowledge_document_id, row.document_version) == (harness.project_id, harness.base_id, harness.document_id, 1)
        assert row.source_content_digest == source_content_digest(content)
        assert row.content == harness.port.output
        assert list(row.embedding) == pytest.approx([0.1, 0.2, 0.3, 0.4])
        assert row.created_at.tzinfo is not None
    assert harness.port.observed == [("summarizing", 0, 2), ("summarizing", 1, 2)]
    assert harness.embedding_observed == [("embedding", 0, 2), ("embedding", 1, 2)]
    assert await harness.progress() == ("done", 2, 2)
    assert harness.port.calls == [("summary-model-v1", KNOWLEDGE_SUMMARY_PROMPT_V1.format(content=c)) for c in contents[:2]]


@pytest.mark.asyncio
async def test_summary_model_uses_index_text_while_digest_binds_display_content(
    harness,
):
    ref = "a" * 64
    content = "# 原始 Markdown\n\n" + "关键结论。" * 40 + f"\n\n![机架](knowledge-attachment:{ref})"
    index_text = "原始 Markdown\n" + "关键结论。" * 40 + "\n机架"
    profile = ProcessingProfile(
        parse=make_parse_profile(".md"),
        chunk=make_chunk_profile(),
    ).model_dump(mode="json")
    claim = await harness.seed(
        [content],
        index_texts=[index_text],
        parsing_profile=profile,
    )

    await harness.handler()(claim)

    assert harness.port.calls == [
        (
            harness.port.model_ref,
            KNOWLEDGE_SUMMARY_PROMPT_V1.format(content=index_text),
        )
    ]
    assert ref not in harness.port.calls[0][1]
    [summary] = await harness.summaries()
    assert summary.source_content_digest == source_content_digest(content)


@pytest.mark.asyncio
async def test_no_eligible_sources_settle_without_provider_calls(harness):
    claim = await harness.seed(["短段"])
    await harness.handler()(claim)
    assert harness.port.calls == harness.batches == []
    assert (await harness.progress())[0] == "done"


@pytest.mark.asyncio
async def test_matching_digest_skips_and_changed_digest_regenerates(harness):
    claim = await harness.seed(["甲" * 200])
    await harness.handler()(claim)
    original = (await harness.summaries())[0]
    await harness.handler()(await harness.claim())
    assert len(harness.port.calls) == 1
    assert (await harness.summaries())[0].id == original.id
    async with harness.factory() as session, session.begin():
        await session.execute(update(KnowledgeSegmentRow).values(content="乙" * 200))
    await harness.handler()(await harness.claim())
    assert len(harness.port.calls) == 2
    assert (await harness.summaries())[0].source_content_digest == source_content_digest("乙" * 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unconfigured", "inactive"])
async def test_unavailable_model_failure_preserves_ready_document(harness, state):
    claim = await harness.seed(["甲" * 200])
    harness.port.configured = state != "unconfigured"
    harness.port.active = state != "inactive"
    with pytest.raises(KnowledgeError) as caught:
        await harness.handler()(claim)
    assert caught.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
    async with harness.factory() as session, session.begin():
        await session.execute(update(KnowledgeTaskRow).values(attempt_count=3))
        assert await settle_task_failure(session, claim.id, claim.claim_token, error_message="摘要失败", retry_delay_seconds=0) == "failed"
    async with harness.factory() as session:
        assert (await session.get(KnowledgeDocumentRow, harness.document_id)).status == "ready"
    assert await harness.summaries() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["version", "switch", "binding"])
async def test_late_publish_discards_generated_vectors(harness, change):
    claim = await harness.seed(["甲" * 200])

    async def mutate(_count):
        async with harness.factory() as session, session.begin():
            if change == "version":
                await session.execute(update(KnowledgeDocumentRow).values(version=2))
            elif change == "switch":
                await session.execute(update(KnowledgeBaseRow).values(summary_index_enabled=False))
            else:
                await session.execute(update(KnowledgeBaseRow).values(embedding_model_id=None))

    harness.port.on_call = mutate
    await harness.handler()(claim)
    assert await harness.summaries() == []
    assert (await harness.progress())[0] == "done"


@pytest.mark.asyncio
async def test_edit_during_generation_skips_stale_summary_and_enqueues_followup(harness):
    claim = await harness.seed(["甲" * 200, "乙" * 200])

    async def mutate(count):
        if count == 1:
            async with harness.factory() as session, session.begin():
                await session.execute(update(KnowledgeSegmentRow).where(KnowledgeSegmentRow.id == harness.segment_ids[0]).values(content="改" * 200))

    harness.port.on_call = mutate
    await harness.handler()(claim)
    assert [r.knowledge_segment_id for r in await harness.summaries()] == [harness.segment_ids[1]]
    followup = await harness.claim_existing()
    assert followup.id != claim.id and followup.kind == "summarize_document"
    harness.port.on_call = None
    await harness.handler()(followup)
    assert len(await harness.summaries()) == 2


@pytest.mark.asyncio
async def test_new_source_outside_target_snapshot_gets_followup(harness):
    claim = await harness.seed(["甲" * 200])

    async def add_source(_count):
        async with harness.factory() as session, session.begin():
            session.add(
                KnowledgeSegmentRow(
                    id=uuid.uuid4(), project_id=harness.project_id, knowledge_base_id=harness.base_id, knowledge_document_id=harness.document_id, document_version=1, position=2, content="新增" * 100, word_count=200, embedding=[1.0] * 4
                )
            )

    harness.port.on_call = add_source
    await harness.handler()(claim)
    assert len(await harness.summaries()) == 1
    followup = await harness.claim_existing()
    harness.port.on_call = None
    await harness.handler()(followup)
    assert len(await harness.summaries()) == 2


@pytest.mark.asyncio
async def test_expired_lease_stops_remaining_llm_calls_and_recovers_without_document_failure(harness):
    claim = await harness.seed(["甲" * 200, "乙" * 200])

    async def expire(_count):
        async with harness.factory() as session, session.begin():
            await session.execute(update(KnowledgeTaskRow).values(lease_until=datetime.now(UTC) - timedelta(seconds=1), attempt_count=3))

    harness.port.on_call = expire
    with pytest.raises(KnowledgeError) as caught:
        await harness.handler()(claim)
    assert caught.value.code == KNOWLEDGE_TASK_FAILED
    assert len(harness.port.calls) == 1 and harness.batches == []
    async with harness.factory() as session, session.begin():
        assert await recover_expired_tasks(session) == 1
    async with harness.factory() as session:
        assert (await session.get(KnowledgeDocumentRow, harness.document_id)).status == "ready"


@pytest.mark.asyncio
async def test_summaries_publish_in_batches_so_a_late_failure_keeps_finished_work(harness):
    """A long document no longer holds every generated summary until the end:
    each ``SUMMARY_PUBLISH_BATCH`` block is embedded and published under the
    lease, so a Provider failure at summary 23 leaves the first 20 durable and
    the retry only generates the remaining 5."""

    assert SUMMARY_PUBLISH_BATCH == 20
    contents = [f"{index:02d}" + "段" * 210 for index in range(25)]
    claim = await harness.seed(contents)

    async def fail_late(count):
        if count == 23:
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "摘要生成失败")

    harness.port.on_call = fail_late
    with pytest.raises(KnowledgeError):
        await harness.handler()(claim)
    assert len(await harness.summaries()) == 20
    assert len(harness.port.calls) == 23
    # Progress showed the cumulative count across batches, never reset to 0.
    assert harness.port.observed[20] == ("summarizing", 20, 25)

    harness.port.on_call = None
    async with harness.factory() as session, session.begin():
        await session.execute(update(KnowledgeTaskRow).values(status="queued", claim_token=None, lease_until=None))
    await harness.handler()(await harness.claim_existing())
    assert len(await harness.summaries()) == 25
    assert len(harness.port.calls) == 28  # 23 + the 5 still missing
    assert (await harness.progress())[0] == "done"


@pytest.mark.asyncio
async def test_summary_output_is_hard_capped(harness):
    claim = await harness.seed(["甲" * 200])
    harness.port.output = "摘" * 1200
    await harness.handler()(claim)
    assert (await harness.summaries())[0].content == "摘" * 1000
    assert harness.batches == [["摘" * 1000]]


@pytest.mark.asyncio
async def test_partial_llm_failure_keeps_existing_rows_and_retry_restarts_progress(harness):
    await harness.handler()(await harness.seed(["甲" * 200, "乙" * 200]))
    original = [(row.id, row.content) for row in await harness.summaries()]
    async with harness.factory() as session, session.begin():
        await session.execute(update(KnowledgeSegmentRow).values(content="改" * 200))
    claim = await harness.claim()
    harness.port.calls.clear()
    harness.port.observed.clear()

    async def fail_second(count):
        if count == 2:
            raise KnowledgeError(KNOWLEDGE_TASK_FAILED, "摘要生成失败")

    harness.port.on_call = fail_second
    with pytest.raises(KnowledgeError):
        await harness.handler()(claim)
    assert [(row.id, row.content) for row in await harness.summaries()] == original
    async with harness.factory() as session, session.begin():
        assert await settle_task_failure(session, claim.id, claim.claim_token, error_message="摘要生成失败", retry_delay_seconds=0) == "retry_wait"
    retry = await harness.claim_existing()
    assert retry.attempt_count == 2
    harness.port.on_call = None
    harness.port.observed.clear()
    await harness.handler()(retry)
    assert harness.port.observed == [("summarizing", 0, 2), ("summarizing", 1, 2)]
    assert all(row.source_content_digest == source_content_digest("改" * 200) for row in await harness.summaries())


@pytest.mark.asyncio
async def test_project_becoming_inactive_stops_later_provider_dispatch(harness):
    from actweave_knowledge.tasks.worker import KnowledgeProjectInactive

    claim = await harness.seed(["甲" * 200, "乙" * 200])
    active = True

    async def project_check(session, project_id):
        assert project_id == harness.project_id
        return active

    async def deactivate(_count):
        nonlocal active
        active = False

    harness.port.on_call = deactivate
    handler = KnowledgeSummarizeHandler(session_factory=harness.factory, model_client=harness.client, model_port=harness.port, project_active_check=project_check)
    with pytest.raises(KnowledgeProjectInactive):
        await handler(claim)
    assert len(harness.port.calls) == 1 and harness.batches == []
    assert await harness.summaries() == []


@pytest.mark.asyncio
async def test_lease_loss_during_embedding_stops_undispatched_batches(harness):
    claim = await harness.seed(["甲" * 200, "乙" * 200])

    async def expire(_count):
        async with harness.factory() as session, session.begin():
            await session.execute(update(KnowledgeTaskRow).values(lease_until=datetime.now(UTC) - timedelta(seconds=1)))

    harness.on_batch = expire
    with pytest.raises(KnowledgeError) as caught:
        await harness.handler()(claim)
    assert caught.value.code == KNOWLEDGE_TASK_FAILED
    assert len(harness.port.calls) == 2 and len(harness.batches) == 1
    assert await harness.summaries() == []


@pytest.mark.asyncio
async def test_summary_toggle_backfill_reports_admission_and_preserves_rows_when_off(harness):
    from actweave_knowledge import KnowledgeBaseUpdate, KnowledgeSettings
    from actweave_knowledge.bases import KnowledgeBaseService

    await harness.handler()(await harness.seed(["甲" * 200]))
    original_id = (await harness.summaries())[0].id
    service = KnowledgeBaseService(session_factory=harness.factory, settings=KnowledgeSettings(enabled=False), model_port=harness.port)
    result = await service.update_knowledge_base(harness.project_id, harness.base_id, KnowledgeBaseUpdate(summary_index_enabled=False))
    assert not result.base.summary_index_enabled and result.summary_backfill is None
    assert (await harness.summaries())[0].id == original_id
    result = await service.update_knowledge_base(harness.project_id, harness.base_id, KnowledgeBaseUpdate(summary_index_enabled=True))
    assert result.base.summary_index_enabled
    assert result.summary_backfill.accepted_document_count == 1
    assert result.summary_backfill.skipped_document_ids == ()
    await service.update_knowledge_base(harness.project_id, harness.base_id, KnowledgeBaseUpdate(summary_index_enabled=False))
    result = await service.update_knowledge_base(harness.project_id, harness.base_id, KnowledgeBaseUpdate(summary_index_enabled=True))
    assert result.summary_backfill.accepted_document_count == 0
    assert result.summary_backfill.skipped_document_ids == (harness.document_id,)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_state", ["unconfigured", "inactive"])
async def test_summary_toggle_requires_usable_model(harness, model_state):
    from actweave_knowledge import KnowledgeBaseUpdate, KnowledgeSettings
    from actweave_knowledge.bases import KnowledgeBaseService

    await harness.handler()(await harness.seed([], enabled=False))
    harness.port.configured = model_state != "unconfigured"
    harness.port.active = model_state != "inactive"
    service = KnowledgeBaseService(session_factory=harness.factory, settings=KnowledgeSettings(enabled=False), model_port=harness.port)
    with pytest.raises(KnowledgeError) as caught:
        await service.update_knowledge_base(harness.project_id, harness.base_id, KnowledgeBaseUpdate(summary_index_enabled=True))
    assert caught.value.code == "KNOWLEDGE_INVALID_REQUEST"
    assert "系统设置" in caught.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("status,published_version", [("processing", 1), ("ready", None)])
async def test_backfill_skips_non_ready_or_unpublished_documents(harness, status, published_version):
    from actweave_knowledge import KnowledgeBaseUpdate, KnowledgeSettings
    from actweave_knowledge.bases import KnowledgeBaseService

    await harness.handler()(await harness.seed([], enabled=False))
    async with harness.factory() as session, session.begin():
        await session.execute(update(KnowledgeDocumentRow).values(status=status, published_version=published_version, error_message="处理失败" if status == "failed" else None))
    service = KnowledgeBaseService(session_factory=harness.factory, settings=KnowledgeSettings(enabled=False), model_port=harness.port)
    result = await service.update_knowledge_base(harness.project_id, harness.base_id, KnowledgeBaseUpdate(summary_index_enabled=True))
    assert result.summary_backfill.accepted_document_count == 0
    assert result.summary_backfill.skipped_document_ids == (harness.document_id,)


@pytest.mark.asyncio
async def test_segment_edits_invalidate_and_single_open_refresh_covers_addition(harness):
    from actweave_knowledge import KnowledgeSegmentCreate, KnowledgeSegmentUpdate, KnowledgeSettings
    from actweave_knowledge.segments import KnowledgeSegmentService

    await harness.handler()(await harness.seed(["甲" * 200]))
    service = KnowledgeSegmentService(session_factory=harness.factory, settings=KnowledgeSettings(enabled=False), client=harness.client, model_port=harness.port)
    await service.update_segment(harness.project_id, harness.segment_ids[0], KnowledgeSegmentUpdate(content="改" * 200))
    assert await harness.summaries() == []
    added = await service.create_segment(harness.project_id, harness.document_id, KnowledgeSegmentCreate(content="新" * 200))
    async with harness.factory() as session:
        open_tasks = (await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.status == "queued"))).all()
        assert len(open_tasks) == 1 and open_tasks[0].kind == "summarize_document"
    await harness.handler()(await harness.claim_existing())
    assert len(await harness.summaries()) == 2
    await service.delete_segment(harness.project_id, added.id)
    assert len(await harness.summaries()) == 1


@pytest.mark.asyncio
async def test_summary_retry_keeps_generation_and_success_clears_failed_progress(harness):
    from actweave_knowledge import KnowledgeSettings
    from actweave_knowledge.documents import KnowledgeDocumentService

    claim = await harness.seed(["甲" * 200])
    async with harness.factory() as session, session.begin():
        await session.execute(update(KnowledgeTaskRow).values(attempt_count=3))
        await settle_task_failure(session, claim.id, claim.claim_token, error_message="摘要失败", retry_delay_seconds=0)
    service = KnowledgeDocumentService(
        project_active_check=is_knowledge_project_active,
        quota=make_test_quota_port(harness.factory),
        session_factory=harness.factory,
        settings=KnowledgeSettings(enabled=False),
        file_capabilities=make_test_file_capability_provider(),
        object_store=None,
    )
    result = await service.retry_document(harness.project_id, harness.document_id)
    assert result.status == "ready" and result.version == 1 and result.segment_count == 1
    assert result.task_progress.kind == "summarize_document"
    await harness.handler()(await harness.claim_existing())
    document = await service.get_document(harness.project_id, harness.document_id)
    assert document.task_progress is None


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_child", [False, True])
async def test_reembed_keeps_summary_text_digest_and_created_at_without_llm(harness, parent_child):
    from actweave_knowledge import KnowledgeSettings
    from actweave_knowledge.bases import KnowledgeBaseService
    from actweave_knowledge.ingestion.reembed import KnowledgeReembedHandler
    from actweave_knowledge.persistence.models import KnowledgeSegmentChildRow

    await harness.handler()(await harness.seed(["甲" * 200]))
    original = (await harness.summaries())[0]
    if parent_child:
        async with harness.factory() as session, session.begin():
            await session.execute(update(KnowledgeDocumentRow).values(chunking_mode="parent_child"))
            await session.execute(update(KnowledgeSegmentRow).values(embedding=None))
            session.add(
                KnowledgeSegmentChildRow(
                    id=uuid.uuid4(),
                    project_id=harness.project_id,
                    knowledge_base_id=harness.base_id,
                    knowledge_document_id=harness.document_id,
                    knowledge_segment_id=harness.segment_ids[0],
                    document_version=1,
                    position=1,
                    content="子段",
                    word_count=2,
                    embedding=[1.0] * 4,
                )
            )
    provider_id = await seed_provider(harness.factory)
    new_model = await seed_embedding_model(harness.factory, provider_id, dimension=4)
    service = KnowledgeBaseService(session_factory=harness.factory, settings=KnowledgeSettings(enabled=False), model_port=harness.port)
    await service.rebuild_knowledge_base(harness.project_id, harness.base_id, embedding_model_id=new_model)
    claim = await harness.claim_existing()
    assert claim.kind == "reembed_document"
    harness.port.calls.clear()
    harness.batches.clear()
    handler = KnowledgeReembedHandler(session_factory=harness.factory, model_client=harness.client, model_port=harness.port)
    await handler(claim)
    current = (await harness.summaries())[0]
    assert (current.id, current.content, current.source_content_digest, current.created_at) == (original.id, original.content, original.source_content_digest, original.created_at)
    assert current.document_version == 2
    assert harness.port.calls == []
    assert harness.batches == [["子段" if parent_child else "甲" * 200, original.content]]


@pytest.mark.asyncio
async def test_open_summary_blocks_reparse_and_rebuild(harness):
    from actweave_knowledge import KnowledgeReparseRequest, KnowledgeSettings
    from actweave_knowledge.bases import KnowledgeBaseService
    from actweave_knowledge.documents import KnowledgeDocumentService

    await harness.seed(["甲" * 200])
    bases = KnowledgeBaseService(session_factory=harness.factory, settings=KnowledgeSettings(enabled=False), model_port=harness.port)
    docs = KnowledgeDocumentService(
        project_active_check=is_knowledge_project_active,
        quota=make_test_quota_port(harness.factory),
        session_factory=harness.factory,
        settings=KnowledgeSettings(enabled=False),
        file_capabilities=make_test_file_capability_provider(),
        object_store=None,
    )
    async with harness.factory() as session:
        model_id = (await session.get(KnowledgeBaseRow, harness.base_id)).embedding_model_id
    with pytest.raises(KnowledgeError) as caught:
        await bases.rebuild_knowledge_base(harness.project_id, harness.base_id, embedding_model_id=model_id)
    assert caught.value.code == "KNOWLEDGE_INVALID_REQUEST"
    with pytest.raises(KnowledgeError) as caught:
        await docs.reparse_document(harness.project_id, harness.document_id, KnowledgeReparseRequest(expected_version=1))
    assert caught.value.code == "KNOWLEDGE_INVALID_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["enabled", "disabled", "unconfigured", "reparse"])
async def test_ingest_publish_admits_summary_without_compromising_ready_document(harness, mode):
    from actweave_knowledge import KnowledgeReparseRequest, KnowledgeSettings
    from actweave_knowledge.documents import KnowledgeDocumentService
    from actweave_knowledge.ingestion.pipeline import KnowledgeIngestionHandler
    from actweave_knowledge.persistence.tasks import settle_task_success

    claim = await harness.seed(["旧" * 200] if mode == "reparse" else [], enabled=mode != "disabled")
    if mode == "reparse":
        await harness.handler()(claim)
        assert len(await harness.summaries()) == 1
    else:
        async with harness.factory() as session, session.begin():
            await settle_task_success(session, claim.id, claim.claim_token)
    harness.port.configured = mode != "unconfigured"
    harness.port.active = mode != "inactive"

    payload = ("短段" if mode == "short" else "文档内容" * 60).encode()
    profile = ProcessingProfile(
        parse=make_parse_profile(".md"),
        chunk=make_chunk_profile(),
    )
    store = ExtractionObjectStore()
    async with harness.factory() as session, session.begin():
        document = await session.get(KnowledgeDocumentRow, harness.document_id, with_for_update=True)
        assert document is not None
        document.source_sha256 = hashlib.sha256(payload).hexdigest()
        document.parsing_profile = profile.model_dump(mode="json")
        document.capability_revision = "a" * 64
        document.size_bytes = len(payload)
        document.chunk_size = profile.chunk.size
        document.chunk_overlap = profile.chunk.overlap
        document.chunk_separator = profile.chunk.separator
        document.chunking_mode = profile.chunk.mode
        document.child_chunk_size = profile.chunk.child_size
        document.child_chunk_separator = profile.chunk.child_separator
        document.remove_extra_spaces = profile.chunk.remove_extra_spaces
        document.remove_urls_emails = profile.chunk.remove_urls_emails
        store.objects[document.storage_key] = payload
    settings = KnowledgeSettings(enabled=False)
    documents = KnowledgeDocumentService(
        project_active_check=is_knowledge_project_active, quota=make_test_quota_port(harness.factory), session_factory=harness.factory, settings=settings, file_capabilities=make_test_file_capability_provider(settings), object_store=store
    )
    if mode == "reparse":
        await documents.reparse_document(harness.project_id, harness.document_id, KnowledgeReparseRequest(expected_version=1))
    else:
        async with harness.factory() as session, session.begin():
            await session.execute(update(KnowledgeDocumentRow).values(status="queued", published_version=None))
            session.add(KnowledgeTaskRow(id=uuid.uuid4(), project_id=harness.project_id, resource_id=harness.document_id, kind="ingest_document", target_version=1))
    ingest = await harness.claim_existing()
    handler = KnowledgeIngestionHandler(
        session_factory=harness.factory,
        settings=settings,
        object_store=store,
        quota=make_test_quota_port(harness.factory),
        model_client=harness.client,
        model_port=harness.port,
        project_active_check=is_knowledge_project_active,
    )
    await handler(ingest)
    async with harness.factory() as session:
        document = await session.get(KnowledgeDocumentRow, harness.document_id)
        expected_version = 2 if mode == "reparse" else 1
        assert document.status == "ready" and document.version == document.published_version == expected_version
        tasks = (await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.status == "queued"))).all()
        assert len(tasks) == (1 if mode in ("enabled", "inactive", "reparse") else 0)
        assert all(task.kind == "summarize_document" and task.target_version == expected_version for task in tasks)
    assert await harness.summaries() == []  # old segments and summaries cascade together
