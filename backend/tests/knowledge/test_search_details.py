"""T5 — single-segment detail reads with paged children.

``get_segment_detail`` is the authoritative read behind "open a search hit"
and plain segment maintenance browsing. Tests run against the installed
Schema V1 snapshot: the full resource chain is validated, expectations
(document version + content digest) turn drift into ``KNOWLEDGE_CONFLICT``,
stale rows left by failed reprocessing stay readable only without
expectations, and children page at 50 with every page re-checking the
expectations. HTTP tests pin the detail route contract over ASGI.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_SEGMENT_DETAIL_CHILD_PAGE_SIZE,
    KnowledgeError,
    KnowledgeSegmentChildView,
    KnowledgeSegmentDetail,
    KnowledgeSegmentSummaryView,
    KnowledgeSegmentView,
    KnowledgeSettings,
)
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentChildRow,
    KnowledgeSegmentRow,
    KnowledgeSegmentSummaryRow,
)
from actweave_knowledge.segments import KnowledgeSegmentService
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge import gateway
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.bootstrap import _install_full_schema

_OWNER_USER_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")


@pytest.mark.asyncio
async def test_segment_detail_projects_an_optional_system_generated_summary(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_id, segment_id, content = await _seed_detail_fixture(harness)
        missing = await harness.service.get_segment_detail(project_id, base_id, document_id, segment_id)
        assert missing.summary is None
        async with harness.factory() as session, session.begin():
            session.add(
                KnowledgeSegmentSummaryRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    knowledge_document_id=document_id,
                    knowledge_segment_id=segment_id,
                    document_version=1,
                    content="系统生成的故障排查摘要",
                    source_content_digest=hashlib.sha256(content.encode()).hexdigest(),
                    embedding=[1.0, 0.0, 0.0],
                )
            )

        present = await harness.service.get_segment_detail(project_id, base_id, document_id, segment_id)

        assert present.segment.content == content
        assert present.summary is not None
        assert present.summary.content == "系统生成的故障排查摘要"
        assert present.summary.created_at.tzinfo is not None
    finally:
        await harness.engine.dispose()


class _DetailHarness:
    def __init__(self, engine, factory, service: KnowledgeSegmentService) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.service = service


async def _harness(postgres_database_url: str) -> _DetailHarness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    # Detail reads never touch the embedding client or the model port.
    service = KnowledgeSegmentService(
        session_factory=factory,
        settings=KnowledgeSettings(),
        client=None,  # type: ignore[arg-type]
        model_port=None,  # type: ignore[arg-type]
    )
    return _DetailHarness(engine, factory, service)


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
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"t5_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"t5-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


async def _seed_registry_model(session: AsyncSession) -> uuid.UUID:
    from registry_helpers import TEST_REGISTRY_API_KEY, registry_secret_key

    from app.model_registry.secrets import protect_provider_api_key
    from deerflow.persistence.model_registry import ModelProviderModelRow, ModelProviderRow

    provider_id = uuid.uuid4()
    envelope = protect_provider_api_key(
        provider_id=provider_id,
        base_url="https://provider.invalid/v1",
        api_key=TEST_REGISTRY_API_KEY,
        key=registry_secret_key(),
    )
    session.add(
        ModelProviderRow(
            id=provider_id,
            name=f"provider-{provider_id.hex[:12]}",
            base_url="https://provider.invalid/v1",
            request_timeout_seconds=30,
            api_key_nonce=envelope.nonce,
            api_key_ciphertext=envelope.ciphertext,
        )
    )
    await session.flush()
    model_id = uuid.uuid4()
    session.add(
        ModelProviderModelRow(
            id=model_id,
            provider_id=provider_id,
            model_type="embedding",
            model_name=f"embed-{model_id.hex[:12]}",
            embedding_dimension=3,
            max_batch=64,
            status="active",
        )
    )
    await session.flush()
    return model_id


async def _seed_detail_fixture(
    harness: _DetailHarness,
    *,
    child_count: int = 3,
    document_version: int = 1,
    segment_version: int | None = None,
    document_status: str = "ready",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, str]:
    """Project + base + parent_child document + one segment with children.

    Returns (project_id, base_id, document_id, segment_id, content).
    """

    content = "被检索命中的父块完整内容。" * 10
    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        embedding_id = await _seed_registry_model(session)
        base = KnowledgeBaseRow(
            id=uuid.uuid4(),
            project_id=project_id,
            name="详情库",
            embedding_model_id=embedding_id,
            status="active",
        )
        session.add(base)
        await session.flush()
        document_id = uuid.uuid4()
        document = KnowledgeDocumentRow(
            id=document_id,
            project_id=project_id,
            knowledge_base_id=base.id,
            name="详情文档",
            original_name="详情文档.md",
            storage_key=f"projects/{project_id}/knowledge/{base.id}/{document_id}.md",
            size_bytes=64,
            status=document_status,
            error_message="解析失败" if document_status == "failed" else None,
            version=document_version,
            chunk_size=1000,
            chunk_overlap=100,
            chunking_mode="parent_child",
        )
        session.add(document)
        await session.flush()
        segment = KnowledgeSegmentRow(
            id=uuid.uuid4(),
            project_id=project_id,
            knowledge_base_id=base.id,
            knowledge_document_id=document.id,
            document_version=segment_version if segment_version is not None else document_version,
            position=1,
            content=content,
            source_position={"page": 3},
            embedding=None,
        )
        session.add(segment)
        await session.flush()
        for position in range(1, child_count + 1):
            session.add(
                KnowledgeSegmentChildRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    knowledge_base_id=base.id,
                    knowledge_document_id=document.id,
                    knowledge_segment_id=segment.id,
                    document_version=segment.document_version,
                    position=position,
                    content=f"子块{position:02d}",
                    word_count=4,
                    embedding=[1.0, 0.0, 0.0],
                )
            )
    return project_id, base.id, document_id, segment.id, content


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_detail_returns_the_current_segment_with_paged_children(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        page_size = KNOWLEDGE_SEGMENT_DETAIL_CHILD_PAGE_SIZE
        project_id, base_id, document_id, segment_id, content = await _seed_detail_fixture(
            harness,
            child_count=page_size + 1,
        )

        detail = await harness.service.get_segment_detail(project_id, base_id, document_id, segment_id)

        assert detail.segment.id == segment_id
        assert detail.segment.content == content
        assert detail.segment.source_position == {"page": 3}
        assert detail.knowledge_base_id == base_id
        assert detail.document_id == document_id
        assert detail.document_name == "详情文档"
        assert detail.content_state == "current"
        assert detail.stored_content_version == 1
        assert detail.current_document_version == 1
        assert detail.children_total == page_size + 1
        assert detail.child_page == 1
        assert [child.position for child in detail.children] == list(range(1, page_size + 1))
        assert detail.children[0].content == "子块01"
        assert detail.children[0].word_count == 4

        second = await harness.service.get_segment_detail(
            project_id,
            base_id,
            document_id,
            segment_id,
            child_page=2,
        )
        assert [child.position for child in second.children] == [page_size + 1]
        assert second.child_page == 2
        assert second.children_total == page_size + 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_detail_validates_the_complete_resource_chain(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_id, segment_id, _ = await _seed_detail_fixture(harness)
        other_project_id, other_base_id, other_document_id, _, _ = await _seed_detail_fixture(harness)

        for wrong in (
            (other_project_id, base_id, document_id, segment_id),
            (project_id, other_base_id, document_id, segment_id),
            (project_id, base_id, other_document_id, segment_id),
            (project_id, base_id, document_id, uuid.uuid4()),
        ):
            with pytest.raises(KnowledgeError) as error:
                await harness.service.get_segment_detail(*wrong)
            assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_detail_child_page_must_be_a_positive_integer(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_id, segment_id, _ = await _seed_detail_fixture(harness)

        with pytest.raises(KnowledgeError) as error:
            await harness.service.get_segment_detail(
                project_id,
                base_id,
                document_id,
                segment_id,
                child_page=0,
            )
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_detail_expectations_pass_on_matching_version_and_digest(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_id, segment_id, content = await _seed_detail_fixture(harness)

        detail = await harness.service.get_segment_detail(
            project_id,
            base_id,
            document_id,
            segment_id,
            expected_document_version=1,
            expected_content_digest=_digest(content),
        )

        assert detail.content_state == "current"
        assert detail.segment.content == content
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["version", "digest", "not_ready"])
async def test_detail_expectations_conflict_on_any_drift(postgres_database_url: str, drift: str) -> None:
    """Old scores are never silently explained with new text."""

    harness = await _harness(postgres_database_url)
    try:
        if drift == "not_ready":
            project_id, base_id, document_id, segment_id, content = await _seed_detail_fixture(
                harness,
                document_status="failed",
            )
            expected_version, expected_digest = 1, _digest(content)
        elif drift == "version":
            project_id, base_id, document_id, segment_id, content = await _seed_detail_fixture(harness)
            expected_version, expected_digest = 2, _digest(content)
        else:
            project_id, base_id, document_id, segment_id, content = await _seed_detail_fixture(harness)
            expected_version, expected_digest = 1, _digest("检索时的旧内容")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.get_segment_detail(
                project_id,
                base_id,
                document_id,
                segment_id,
                expected_document_version=expected_version,
                expected_content_digest=expected_digest,
            )
        assert error.value.code == KNOWLEDGE_CONFLICT
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_stale_rows_read_only_without_expectations(postgres_database_url: str) -> None:
    """Failed-reprocessing leftovers browse as ``stale``; hit expectations conflict."""

    harness = await _harness(postgres_database_url)
    try:
        # The document moved to version 2 but this segment stayed on 1.
        project_id, base_id, document_id, segment_id, content = await _seed_detail_fixture(
            harness,
            document_version=2,
            segment_version=1,
        )

        detail = await harness.service.get_segment_detail(project_id, base_id, document_id, segment_id)
        assert detail.content_state == "stale"
        assert detail.stored_content_version == 1
        assert detail.current_document_version == 2
        assert detail.segment.content == content
        # Children stay readable alongside their stale parent.
        assert detail.children_total == 3

        # A search hit that expects the stored generation still conflicts:
        # the row is no longer the current content generation.
        with pytest.raises(KnowledgeError) as error:
            await harness.service.get_segment_detail(
                project_id,
                base_id,
                document_id,
                segment_id,
                expected_document_version=1,
                expected_content_digest=_digest(content),
            )
        assert error.value.code == KNOWLEDGE_CONFLICT

        # Every child page re-checks the expectations, not only page one.
        with pytest.raises(KnowledgeError) as second_page_error:
            await harness.service.get_segment_detail(
                project_id,
                base_id,
                document_id,
                segment_id,
                expected_document_version=1,
                expected_content_digest=_digest(content),
                child_page=2,
            )
        assert second_page_error.value.code == KNOWLEDGE_CONFLICT
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_detail_revalidates_project_authority(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_id, segment_id, _ = await _seed_detail_fixture(harness)

        class _RevokedAuthority:
            def __init__(self) -> None:
                self.project_id = project_id
                self.actor_user_id = _OWNER_USER_ID

            async def revalidate(self, session) -> None:  # noqa: ANN001
                del session
                raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.get_segment_detail(
                project_id,
                base_id,
                document_id,
                segment_id,
                authority=_RevokedAuthority(),  # type: ignore[arg-type]
            )
        assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_general_segments_report_zero_children(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_id, segment_id, _ = await _seed_detail_fixture(
            harness,
            child_count=0,
        )

        detail = await harness.service.get_segment_detail(project_id, base_id, document_id, segment_id)

        assert detail.children_total == 0
        assert detail.children == ()
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------

_REQUEST_ID = "knowledge-t5-detail"
_PROJECT_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")


class _FakeDetailModule:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.error: KnowledgeError | None = None
        self.detail = KnowledgeSegmentDetail(
            segment=KnowledgeSegmentView(
                id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
                document_version=2,
                position=3,
                content="被命中的完整父块内容。",
                word_count=11,
                enabled=True,
                hit_count=7,
                source_position={"page": 12},
                created_at=datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
            ),
            knowledge_base_id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
            document_id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
            document_name="安装指南.pdf",
            content_state="current",
            stored_content_version=2,
            current_document_version=2,
            children_total=1,
            child_page=1,
            children=(
                KnowledgeSegmentChildView(
                    id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
                    position=1,
                    content="子块内容",
                    word_count=4,
                ),
            ),
        )

    async def get_segment_detail(
        self,
        project_id: uuid.UUID,
        base_id: uuid.UUID,
        document_id: uuid.UUID,
        segment_id: uuid.UUID,
        *,
        expected_document_version: int | None = None,
        expected_content_digest: str | None = None,
        child_page: int = 1,
        authority,  # noqa: ANN001
    ) -> KnowledgeSegmentDetail:
        assert authority.project_id == _PROJECT_ID
        assert authority.actor_user_id == _OWNER_USER_ID
        self.calls.append((project_id, base_id, document_id, segment_id, expected_document_version, expected_content_digest, child_page))
        if self.error is not None:
            raise self.error
        return self.detail


def _app(module: _FakeDetailModule) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway.project_router)
    context = ProjectContext(
        user_id=_OWNER_USER_ID,
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=1,
        request_id=_REQUEST_ID,
    )
    app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: context
    app.dependency_overrides[gateway.require_project_knowledge_edit] = lambda: context
    app.state.knowledge_module = module
    return app


@pytest.mark.asyncio
async def test_http_segment_detail_round_trips_the_module_view() -> None:
    module = _FakeDetailModule()
    base_id = module.detail.knowledge_base_id
    document_id = module.detail.document_id
    segment_id = module.detail.segment.id
    digest = hashlib.sha256("被命中的完整父块内容。".encode()).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(module)), base_url="http://test") as client:
        response = await client.get(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{base_id}/documents/{document_id}/segments/{segment_id}",
            params={
                "expected_document_version": 2,
                "expected_content_digest": digest,
                "child_page": 1,
            },
        )

    assert response.status_code == 200
    assert response.json()["summary"] is None
    assert module.calls == [
        (_PROJECT_ID, base_id, document_id, segment_id, 2, digest, 1),
    ]
    assert response.json() == {
        "segment": {
            "id": str(segment_id),
            "document_version": 2,
            "position": 3,
            "content": "被命中的完整父块内容。",
            "word_count": 11,
            "enabled": True,
            "hit_count": 7,
            "source_position": {"page": 12},
            "created_at": "2026-08-30T10:00:00Z",
        },
        "knowledge_base_id": str(base_id),
        "document_id": str(document_id),
        "document_name": "安装指南.pdf",
        "content_state": "current",
        "stored_content_version": 2,
        "current_document_version": 2,
        "children_total": 1,
        "summary": None,
        "child_page": 1,
        "children": [
            {
                "id": "99999999-9999-4999-8999-999999999999",
                "position": 1,
                "content": "子块内容",
                "word_count": 4,
            }
        ],
        "request_id": _REQUEST_ID,
    }


@pytest.mark.asyncio
async def test_http_segment_detail_includes_only_summary_text_and_creation_time() -> None:
    module = _FakeDetailModule()
    module.detail = replace(module.detail, summary=KnowledgeSegmentSummaryView(content="生成的摘要", created_at=datetime(2026, 8, 31, tzinfo=UTC)))
    detail = module.detail
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(module)), base_url="http://test") as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/bases/{detail.knowledge_base_id}/documents/{detail.document_id}/segments/{detail.segment.id}")

    assert response.status_code == 200
    assert response.json()["summary"] == {"content": "生成的摘要", "created_at": "2026-08-31T00:00:00Z"}


@pytest.mark.asyncio
async def test_http_segment_detail_defaults_optional_expectations_and_maps_conflicts() -> None:
    module = _FakeDetailModule()
    base_id = module.detail.knowledge_base_id
    document_id = module.detail.document_id
    segment_id = module.detail.segment.id
    url = f"/api/projects/{_PROJECT_ID}/knowledge/bases/{base_id}/documents/{document_id}/segments/{segment_id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(module)), base_url="http://test") as client:
        plain = await client.get(url)

        module.error = KnowledgeError(KNOWLEDGE_CONFLICT, "文档内容已更新，请重新检索")
        conflicted = await client.get(url, params={"expected_document_version": 1})

        module.error = KnowledgeError(KNOWLEDGE_NOT_FOUND, "Segment 不存在")
        missing = await client.get(url)

    assert plain.status_code == 200
    assert module.calls[0][4:] == (None, None, 1)

    assert conflicted.status_code == 409
    assert conflicted.json()["detail"]["code"] == KNOWLEDGE_CONFLICT
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == KNOWLEDGE_NOT_FOUND
