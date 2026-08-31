"""M3 gates: Knowledge Base CRUD rules over the installed Schema V1 snapshot.

Covers project scoping, the per-project name and quota rules, the registry
model bindings (deferred embedding for empty bases, optional reranker), ordering, and the
``document_count`` / ``delete_error`` view derivations.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from actweave_knowledge import (
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeError,
    KnowledgeSettings,
)
from actweave_knowledge.bases import KnowledgeBaseService
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeTaskRow,
)
from registry_helpers import (
    registry_model_port,
    seed_embedding_model,
    seed_provider,
    seed_registry_models,
    seed_rerank_model,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.model_registry import ModelProviderModelRow


class _Harness:
    def __init__(self, engine, factory, service: KnowledgeBaseService) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.service = service


class _RevokedAuthority:
    def __init__(self, project_id: uuid.UUID) -> None:
        self.project_id = project_id
        self.actor_user_id = uuid.uuid4()

    async def revalidate(self, session: AsyncSession) -> None:
        del session
        raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")


async def _harness(postgres_database_url: str, **settings_overrides: object) -> _Harness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    settings = KnowledgeSettings.model_validate({"enabled": False, **settings_overrides})
    service = KnowledgeBaseService(
        session_factory=factory,
        settings=settings,
        model_port=registry_model_port(),
    )
    return _Harness(engine, factory, service)


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
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"m3_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"m3-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


async def _prepared(postgres_database_url: str, **settings_overrides: object) -> tuple[_Harness, uuid.UUID, uuid.UUID]:
    harness = await _harness(postgres_database_url, **settings_overrides)
    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
    embedding_model_id, _ = await seed_registry_models(harness.factory)
    return harness, project_id, embedding_model_id


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_unconfigured_base_without_any_registered_models(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])

        created = await harness.service.create_knowledge_base(
            project_id,
            KnowledgeBaseCreate(name="待配置知识库", description="首次导入时配置"),
        )

        assert created.embedding_model_id is None
        assert created.reranker_model_id is None
        assert created.retrieval_mode == "semantic"
        assert created.document_count == 0
        assert created.status == "active"
        assert await harness.service.get_knowledge_base(project_id, created.id) == created
        listed, total = await harness.service.list_knowledge_bases(project_id)
        assert total == 1
        assert listed == [created]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_then_get_round_trips_the_view(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(
            project_id,
            KnowledgeBaseCreate(name="产品手册", embedding_model_id=embedding_model_id, description="说明"),
        )

        assert created.project_id == project_id
        assert created.name == "产品手册"
        assert created.description == "说明"
        assert created.embedding_model_id == embedding_model_id
        assert created.reranker_model_id is None
        assert created.status == "active"
        assert created.document_count == 0
        # K3: new bases carry the schema's retrieval defaults.
        assert created.default_top_k == 4
        assert created.default_score_threshold == 0.2
        assert created.retrieval_mode == "semantic"
        assert created.delete_error is None

        fetched = await harness.service.get_knowledge_base(project_id, created.id)
        assert fetched == created
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_unconfigured_base_rejects_reranker_without_embedding(postgres_database_url: str) -> None:
    harness, project_id, _ = await _prepared(postgres_database_url)
    try:
        provider_id = await seed_provider(harness.factory)
        reranker_id = await seed_rerank_model(harness.factory, provider_id)
        with pytest.raises(KnowledgeError) as create_error:
            await harness.service.create_knowledge_base(
                project_id,
                KnowledgeBaseCreate(name="不可仅配置重排序", reranker_model_id=reranker_id),
            )
        assert create_error.value.code == KNOWLEDGE_INVALID_REQUEST

        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="待配置"))
        with pytest.raises(KnowledgeError) as update_error:
            await harness.service.update_knowledge_base(
                project_id,
                created.id,
                KnowledgeBaseUpdate(reranker_model_id=reranker_id, retrieval_mode="hybrid"),
            )
        assert update_error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert await harness.service.get_knowledge_base(project_id, created.id) == created
        _, total = await harness.service.list_knowledge_bases(project_id)
        assert total == 1
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_retrieval_mode_round_trips_and_validates(postgres_database_url: str) -> None:
    """T8: hybrid is an explicit per-base choice on create or update."""

    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(
            project_id,
            KnowledgeBaseCreate(name="混合库", embedding_model_id=embedding_model_id, retrieval_mode="hybrid"),
        )
        assert created.retrieval_mode == "hybrid"

        updated = await harness.service.update_knowledge_base(
            project_id,
            created.id,
            KnowledgeBaseUpdate(retrieval_mode="semantic"),
        )
        assert updated.retrieval_mode == "semantic"

        with pytest.raises(KnowledgeError) as bad_create:
            await harness.service.create_knowledge_base(
                project_id,
                KnowledgeBaseCreate(name="非法模式", embedding_model_id=embedding_model_id, retrieval_mode="fancy"),  # type: ignore[arg-type]
            )
        assert bad_create.value.code == KNOWLEDGE_INVALID_REQUEST

        with pytest.raises(KnowledgeError) as bad_update:
            await harness.service.update_knowledge_base(
                project_id,
                created.id,
                KnowledgeBaseUpdate(retrieval_mode="fancy"),  # type: ignore[arg-type]
            )
        assert bad_update.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_binds_the_optional_reranker_and_checks_its_type(postgres_database_url: str) -> None:
    """Reranking is opt-in per base; the two binding slots are type-checked."""

    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        provider_id = await seed_provider(harness.factory)
        rerank_model_id = await seed_rerank_model(harness.factory, provider_id)

        created = await harness.service.create_knowledge_base(
            project_id,
            KnowledgeBaseCreate(
                name="带重排序",
                embedding_model_id=embedding_model_id,
                reranker_model_id=rerank_model_id,
            ),
        )
        assert created.embedding_model_id == embedding_model_id
        assert created.reranker_model_id == rerank_model_id

        # A rerank model cannot fill the embedding slot, nor vice versa.
        with pytest.raises(KnowledgeError) as swapped_embedding:
            await harness.service.create_knowledge_base(
                project_id,
                KnowledgeBaseCreate(name="类型错位A", embedding_model_id=rerank_model_id),
            )
        assert swapped_embedding.value.code == KNOWLEDGE_MODEL_UNAVAILABLE

        with pytest.raises(KnowledgeError) as swapped_rerank:
            await harness.service.create_knowledge_base(
                project_id,
                KnowledgeBaseCreate(
                    name="类型错位B",
                    embedding_model_id=embedding_model_id,
                    reranker_model_id=embedding_model_id,
                ),
            )
        assert swapped_rerank.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_rejects_case_insensitive_duplicate_names_per_project(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="Handbook", embedding_model_id=embedding_model_id))
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="handbook", embedding_model_id=embedding_model_id))
        assert error.value.code == KNOWLEDGE_NAME_CONFLICT

        # The same name in another project is allowed.
        async with harness.factory() as session, session.begin():
            other_project = await _seed_project(session, "other")
        other = await harness.service.create_knowledge_base(other_project, KnowledgeBaseCreate(name="Handbook", embedding_model_id=embedding_model_id))
        assert other.name == "Handbook"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_enforces_the_per_project_quota(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url, max_knowledge_bases_per_project=1)
    try:
        await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="first", embedding_model_id=embedding_model_id))
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="second", embedding_model_id=embedding_model_id))
        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_create_requires_an_active_embedding_model(postgres_database_url: str) -> None:
    harness, project_id, _ = await _prepared(postgres_database_url)
    disabled_provider = await seed_provider(harness.factory)
    disabled_id = await seed_embedding_model(harness.factory, disabled_provider, status="disabled")
    try:
        for embedding_model_id in (uuid.uuid4(), disabled_id):
            with pytest.raises(KnowledgeError) as error:
                await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="kb", embedding_model_id=embedding_model_id))
            assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "description"),
    [
        ("", ""),
        ("   ", ""),
        ("x" * 121, ""),
        ("ok", "d" * 501),
    ],
    ids=["empty-name", "blank-name", "long-name", "long-description"],
)
async def test_create_validates_name_and_description(postgres_database_url: str, name: str, description: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_knowledge_base(
                project_id,
                KnowledgeBaseCreate(name=name, embedding_model_id=embedding_model_id, description=description),
            )
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# List and get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_orders_by_updated_at_desc_and_paginates(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        base_time = datetime(2036, 3, 1, 12, 0, tzinfo=UTC)
        ids: list[uuid.UUID] = []
        async with harness.factory() as session, session.begin():
            for position in range(3):
                base_id = uuid.uuid4()
                ids.append(base_id)
                session.add(
                    KnowledgeBaseRow(
                        id=base_id,
                        project_id=project_id,
                        name=f"kb-{position}",
                        description="",
                        embedding_model_id=embedding_model_id,
                        status="active",
                        created_at=base_time,
                        updated_at=base_time.replace(minute=position),
                    )
                )

        first_page, total = await harness.service.list_knowledge_bases(project_id, page=1, page_size=2)
        second_page, _ = await harness.service.list_knowledge_bases(project_id, page=2, page_size=2)

        assert total == 3
        assert [view.id for view in first_page] == [ids[2], ids[1]]
        assert [view.id for view in second_page] == [ids[0]]

        with pytest.raises(KnowledgeError):
            await harness.service.list_knowledge_bases(project_id, page=0)
        with pytest.raises(KnowledgeError):
            await harness.service.list_knowledge_bases(project_id, page_size=101)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_get_is_scoped_to_the_requesting_project(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="mine", embedding_model_id=embedding_model_id))
        async with harness.factory() as session, session.begin():
            other_project = await _seed_project(session, "outsider")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.get_knowledge_base(other_project, created.id)
        assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_view_derives_document_count_and_delete_error(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="counted", embedding_model_id=embedding_model_id))
        async with harness.factory() as session, session.begin():
            document_id = uuid.uuid4()
            session.add(
                KnowledgeDocumentRow(
                    id=document_id,
                    project_id=project_id,
                    knowledge_base_id=created.id,
                    name="doc",
                    original_name="doc.txt",
                    storage_key=f"projects/{project_id}/knowledge/{created.id}/{document_id}.txt",
                    size_bytes=10,
                    status="queued",
                    version=1,
                    chunk_size=1000,
                    chunk_overlap=100,
                )
            )
            session.add(
                KnowledgeTaskRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    resource_id=created.id,
                    kind="delete_knowledge_base",
                    target_version=None,
                    status="failed",
                    attempt_count=3,
                    error_message="对象存储清理失败",
                    finished_at=datetime(2036, 3, 1, tzinfo=UTC),
                )
            )

        stuck = await harness.service.get_knowledge_base(project_id, created.id)
        assert stuck.document_count == 1
        assert stuck.delete_error == "对象存储清理失败"

        # An open retry hides the stale error while the deletion is in progress.
        async with harness.factory() as session, session.begin():
            session.add(
                KnowledgeTaskRow(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    resource_id=created.id,
                    kind="delete_knowledge_base",
                    target_version=None,
                    status="queued",
                )
            )
        retrying = await harness.service.get_knowledge_base(project_id, created.id)
        assert retrying.delete_error is None
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_configuration_rejects_a_base_with_documents(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="待配置"))
        # Even a never-published failed document prevents initial binding.
        async with harness.factory() as session, session.begin():
            session.add(_seed_document(project_id, created.id, name="failed", status="failed", published_version=None))
        before = await harness.service.get_knowledge_base(project_id, created.id)
        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_knowledge_base(
                project_id,
                created.id,
                KnowledgeBaseUpdate(embedding_model_id=embedding_model_id, retrieval_mode="hybrid"),
            )
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert await harness.service.get_knowledge_base(project_id, created.id) == before
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_initial_configuration_commits_models_and_retrieval_settings_together(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        provider_id = await seed_provider(harness.factory)
        reranker_id = await seed_rerank_model(harness.factory, provider_id)
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="待配置"))

        with pytest.raises(KnowledgeError) as invalid_reranker:
            await harness.service.update_knowledge_base(
                project_id,
                created.id,
                KnowledgeBaseUpdate(embedding_model_id=embedding_model_id, reranker_model_id=embedding_model_id, retrieval_mode="hybrid", name="不应保存"),
            )
        assert invalid_reranker.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
        assert await harness.service.get_knowledge_base(project_id, created.id) == created

        configured = await harness.service.update_knowledge_base(
            project_id,
            created.id,
            KnowledgeBaseUpdate(embedding_model_id=embedding_model_id, reranker_model_id=reranker_id, retrieval_mode="hybrid"),
        )
        assert configured.embedding_model_id == embedding_model_id
        assert configured.reranker_model_id == reranker_id
        assert configured.retrieval_mode == "hybrid"
        assert configured.document_count == 0
        assert await harness.service.get_knowledge_base(project_id, created.id) == configured

        with pytest.raises(KnowledgeError) as rebound:
            await harness.service.update_knowledge_base(project_id, created.id, KnowledgeBaseUpdate(embedding_model_id=uuid.uuid4()))
        assert rebound.value.code == KNOWLEDGE_INVALID_REQUEST
        unchanged = await harness.service.update_knowledge_base(project_id, created.id, KnowledgeBaseUpdate(embedding_model_id=None))
        assert unchanged == configured
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_changes_allowed_fields_and_bumps_updated_at(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="before", embedding_model_id=embedding_model_id))

        updated = await harness.service.update_knowledge_base(
            project_id,
            created.id,
            KnowledgeBaseUpdate(name="after", description="新的描述", status="disabled"),
        )

        assert updated.name == "after"
        assert updated.description == "新的描述"
        assert updated.status == "disabled"
        assert updated.embedding_model_id == embedding_model_id
        assert updated.updated_at > created.updated_at

        # A no-op update keeps updated_at untouched.
        unchanged = await harness.service.update_knowledge_base(project_id, created.id, KnowledgeBaseUpdate(name="after"))
        assert unchanged.updated_at == updated.updated_at
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_rebinding_and_clearing_the_reranker(postgres_database_url: str) -> None:
    """Rerank rebinding is tri-state and never touches documents or versions."""

    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        provider_id = await seed_provider(harness.factory)
        first_rerank = await seed_rerank_model(harness.factory, provider_id)
        second_rerank = await seed_rerank_model(harness.factory, provider_id)
        created = await harness.service.create_knowledge_base(
            project_id,
            KnowledgeBaseCreate(name="重排序管理", embedding_model_id=embedding_model_id),
        )
        async with harness.factory() as session, session.begin():
            session.add(_seed_document(project_id, created.id, name="doc", status="ready", version=2))

        bound = await harness.service.update_knowledge_base(
            project_id,
            created.id,
            KnowledgeBaseUpdate(reranker_model_id=first_rerank),
        )
        assert bound.reranker_model_id == first_rerank

        rebound = await harness.service.update_knowledge_base(
            project_id,
            created.id,
            KnowledgeBaseUpdate(reranker_model_id=second_rerank),
        )
        assert rebound.reranker_model_id == second_rerank

        cleared = await harness.service.update_knowledge_base(
            project_id,
            created.id,
            KnowledgeBaseUpdate(clear_reranker_model=True),
        )
        assert cleared.reranker_model_id is None

        # Neither binding nor clearing queued any re-ingestion.
        async with harness.factory() as session:
            document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == created.id))
            assert document is not None
            assert document.status == "ready"
            assert document.version == 2
            tasks = (await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.kind == "ingest_document"))).all()
        assert tasks == []

        # Binding an embedding model into the rerank slot is a type error.
        with pytest.raises(KnowledgeError) as type_error:
            await harness.service.update_knowledge_base(
                project_id,
                created.id,
                KnowledgeBaseUpdate(reranker_model_id=embedding_model_id),
            )
        assert type_error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE

        # Setting both tri-state controls at once is invalid.
        with pytest.raises(KnowledgeError) as both:
            await harness.service.update_knowledge_base(
                project_id,
                created.id,
                KnowledgeBaseUpdate(reranker_model_id=first_rerank, clear_reranker_model=True),
            )
        assert both.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_revalidates_authority_before_locking_or_writing_base(
    postgres_database_url: str,
) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(
            project_id,
            KnowledgeBaseCreate(name="before", embedding_model_id=embedding_model_id),
        )

        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_knowledge_base(
                project_id,
                created.id,
                KnowledgeBaseUpdate(name="after"),
                authority=_RevokedAuthority(project_id),
            )

        assert error.value.code == KNOWLEDGE_NOT_FOUND
        assert (await harness.service.get_knowledge_base(project_id, created.id)).name == "before"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_retrieval_defaults_round_trip_and_validate(postgres_database_url: str) -> None:
    """K3: per-base default_top_k / default_score_threshold are editable and bounded."""

    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="调参库", embedding_model_id=embedding_model_id))

        updated = await harness.service.update_knowledge_base(
            project_id,
            created.id,
            KnowledgeBaseUpdate(default_top_k=9, default_score_threshold=0.65),
        )
        assert updated.default_top_k == 9
        assert updated.default_score_threshold == 0.65

        # Boundary values are allowed: 1..20 and 0..1 (0 disables the filter).
        edges = await harness.service.update_knowledge_base(
            project_id,
            created.id,
            KnowledgeBaseUpdate(default_top_k=20, default_score_threshold=0.0),
        )
        assert edges.default_top_k == 20
        assert edges.default_score_threshold == 0.0

        for update in (
            KnowledgeBaseUpdate(default_top_k=0),
            KnowledgeBaseUpdate(default_top_k=21),
            KnowledgeBaseUpdate(default_top_k=True),  # type: ignore[arg-type]
            KnowledgeBaseUpdate(default_score_threshold=-0.1),
            KnowledgeBaseUpdate(default_score_threshold=1.1),
        ):
            with pytest.raises(KnowledgeError) as error:
                await harness.service.update_knowledge_base(project_id, created.id, update)
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST

        # Failed updates never partially persist.
        fetched = await harness.service.get_knowledge_base(project_id, created.id)
        assert fetched.default_top_k == 20
        assert fetched.default_score_threshold == 0.0
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_rejects_name_conflicts_missing_bases_and_bad_status(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="taken", embedding_model_id=embedding_model_id))
        mine = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="mine", embedding_model_id=embedding_model_id))

        with pytest.raises(KnowledgeError) as conflict:
            await harness.service.update_knowledge_base(project_id, mine.id, KnowledgeBaseUpdate(name="TAKEN"))
        assert conflict.value.code == KNOWLEDGE_NAME_CONFLICT

        with pytest.raises(KnowledgeError) as missing:
            await harness.service.update_knowledge_base(project_id, uuid.uuid4(), KnowledgeBaseUpdate(name="x"))
        assert missing.value.code == KNOWLEDGE_NOT_FOUND

        with pytest.raises(KnowledgeError) as bad_status:
            await harness.service.update_knowledge_base(
                project_id,
                mine.id,
                KnowledgeBaseUpdate(status="deleting"),  # type: ignore[arg-type]
            )
        assert bad_status.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_update_rejects_a_base_that_is_being_deleted(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="dying", embedding_model_id=embedding_model_id))
        async with harness.factory() as session, session.begin():
            row = await session.get(KnowledgeBaseRow, created.id)
            assert row is not None
            row.status = "deleting"

        with pytest.raises(KnowledgeError) as error:
            await harness.service.update_knowledge_base(project_id, created.id, KnowledgeBaseUpdate(name="renamed"))
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# K4: rebuild (embedding model rebind + per-document re-embedding)
# ---------------------------------------------------------------------------


def _seed_document(
    project_id: uuid.UUID,
    base_id: uuid.UUID,
    *,
    name: str,
    status: str,
    version: int = 1,
    published_version: int | None = 1,
) -> KnowledgeDocumentRow:
    document_id = uuid.uuid4()
    return KnowledgeDocumentRow(
        id=document_id,
        project_id=project_id,
        knowledge_base_id=base_id,
        name=name,
        original_name=f"{name}.txt",
        storage_key=f"projects/{project_id}/knowledge/{base_id}/{document_id}.txt",
        size_bytes=10,
        status=status,
        version=version,
        published_version=published_version,
        chunk_size=1000,
        chunk_overlap=100,
        segment_count=7,
        word_count=1200,
        error_message="旧的失败原因" if status == "failed" else None,
    )


@pytest.mark.asyncio
async def test_rebuild_rebinds_and_queues_reembed_for_initialized_documents(postgres_database_url: str) -> None:
    """Initialized ready/failed documents queue ``reembed_document`` with
    their rows and counters intact; a never-published failed document is
    skipped and listed in the result instead of being silently re-parsed."""

    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="重建库", embedding_model_id=embedding_model_id))
        new_embedding_model_id, _ = await seed_registry_models(harness.factory)
        async with harness.factory() as session, session.begin():
            ready = _seed_document(project_id, created.id, name="ready", status="ready", version=2, published_version=2)
            failed = _seed_document(project_id, created.id, name="failed", status="failed", version=4, published_version=3)
            never_published = _seed_document(project_id, created.id, name="never", status="failed", published_version=None)
            session.add_all([ready, failed, never_published])

        result = await harness.service.rebuild_knowledge_base(project_id, created.id, embedding_model_id=new_embedding_model_id)

        assert result.base.embedding_model_id == new_embedding_model_id
        assert result.accepted_document_count == 2
        assert result.skipped_document_ids == (never_published.id,)
        async with harness.factory() as session:
            documents = {row.name: row for row in (await session.scalars(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == created.id))).all()}
            tasks = (await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.kind == "reembed_document", KnowledgeTaskRow.project_id == project_id))).all()

        for name, old_version in (("ready", 2), ("failed", 4)):
            document = documents[name]
            assert document.status == "queued", name
            assert document.version == old_version + 1, name
            # Content rows survive a re-embed; the counters keep describing them.
            assert document.segment_count == 7
            assert document.word_count == 1200
            assert document.error_message is None
        assert documents["never"].status == "failed"
        assert documents["never"].version == 1
        assert documents["never"].error_message == "旧的失败原因"

        queued = {(task.resource_id, task.target_version) for task in tasks}
        assert queued == {
            (documents["ready"].id, 3),
            (documents["failed"].id, 5),
        }
        assert all(task.status == "queued" for task in tasks)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_rebuild_rejects_in_flight_documents(postgres_database_url: str) -> None:
    """Uploading/queued/processing/deleting documents reject the rebuild:
    changing the vector space must not race an upload or another build."""

    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        for blocking_status in ("uploading", "queued", "processing", "deleting"):
            created = await harness.service.create_knowledge_base(
                project_id,
                KnowledgeBaseCreate(name=f"拒绝-{blocking_status}", embedding_model_id=embedding_model_id),
            )
            async with harness.factory() as session, session.begin():
                session.add(_seed_document(project_id, created.id, name="ready", status="ready"))
                session.add(
                    _seed_document(
                        project_id,
                        created.id,
                        name=blocking_status,
                        status=blocking_status,
                        published_version=None,
                    )
                )
            with pytest.raises(KnowledgeError) as error:
                await harness.service.rebuild_knowledge_base(project_id, created.id, embedding_model_id=embedding_model_id)
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST, blocking_status
        async with harness.factory() as session:
            tasks = (await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.kind.in_(("ingest_document", "reembed_document"))))).all()
        assert tasks == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_rebuild_with_the_same_model_is_a_plain_re_embed(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="原地重建", embedding_model_id=embedding_model_id))
        async with harness.factory() as session, session.begin():
            session.add(_seed_document(project_id, created.id, name="doc", status="ready"))

        result = await harness.service.rebuild_knowledge_base(project_id, created.id, embedding_model_id=embedding_model_id)

        assert result.base.embedding_model_id == embedding_model_id
        assert result.accepted_document_count == 1
        assert result.skipped_document_ids == ()
        async with harness.factory() as session:
            document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == created.id))
            assert document is not None
            assert document.status == "queued"
            assert document.version == 2
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_rebuild_rejects_missing_bases_bad_models_and_deleting_bases(postgres_database_url: str) -> None:
    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="校验", embedding_model_id=embedding_model_id))
        inactive_provider = await seed_provider(harness.factory)
        inactive_model_id = await seed_embedding_model(harness.factory, inactive_provider, status="disabled")

        with pytest.raises(KnowledgeError) as missing:
            await harness.service.rebuild_knowledge_base(project_id, uuid.uuid4(), embedding_model_id=embedding_model_id)
        assert missing.value.code == KNOWLEDGE_NOT_FOUND

        with pytest.raises(KnowledgeError) as other_project:
            await harness.service.rebuild_knowledge_base(uuid.uuid4(), created.id, embedding_model_id=embedding_model_id)
        assert other_project.value.code == KNOWLEDGE_NOT_FOUND

        with pytest.raises(KnowledgeError) as unknown_model:
            await harness.service.rebuild_knowledge_base(project_id, created.id, embedding_model_id=uuid.uuid4())
        assert unknown_model.value.code == KNOWLEDGE_MODEL_UNAVAILABLE

        with pytest.raises(KnowledgeError) as inactive:
            await harness.service.rebuild_knowledge_base(project_id, created.id, embedding_model_id=inactive_model_id)
        assert inactive.value.code == KNOWLEDGE_MODEL_UNAVAILABLE

        with pytest.raises(KnowledgeError) as bad_input:
            await harness.service.rebuild_knowledge_base(project_id, created.id, embedding_model_id="not-a-uuid")  # type: ignore[arg-type]
        assert bad_input.value.code == KNOWLEDGE_INVALID_REQUEST

        async with harness.factory() as session, session.begin():
            row = await session.get(KnowledgeBaseRow, created.id)
            assert row is not None
            row.status = "deleting"
        with pytest.raises(KnowledgeError) as dying:
            await harness.service.rebuild_knowledge_base(project_id, created.id, embedding_model_id=embedding_model_id)
        assert dying.value.code == KNOWLEDGE_INVALID_REQUEST

        # No rejected call left a task behind.
        async with harness.factory() as session:
            tasks = (await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.kind.in_(("ingest_document", "reembed_document"))))).all()
        assert tasks == []
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Concurrency: registry disable must serialize with binding reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_serializes_against_concurrent_model_disable(postgres_database_url: str) -> None:
    """A disable holding FOR UPDATE wins: the create re-reads and rejects.

    The binding port takes FOR SHARE on the model row, so it cannot pass the
    active check on a stale snapshot while a registry transaction is
    mid-disable; without the lock the base would commit referencing a
    disabled model.
    """

    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        async with harness.factory() as admin_session, admin_session.begin():
            row = await admin_session.scalar(select(ModelProviderModelRow).where(ModelProviderModelRow.id == embedding_model_id).with_for_update())
            assert row is not None
            row.status = "disabled"
            await admin_session.flush()
            create_task = asyncio.create_task(
                harness.service.create_knowledge_base(
                    project_id,
                    KnowledgeBaseCreate(name="并发建库", embedding_model_id=embedding_model_id),
                )
            )
            # The create must block on the model row lock instead of
            # completing against the stale committed "active" snapshot.
            done, _ = await asyncio.wait({create_task}, timeout=0.5)
            assert not done
        # The registry transaction committed; the create resumes, re-reads the
        # now-disabled row, and must reject instead of binding to it.
        with pytest.raises(KnowledgeError) as error:
            await create_task
        assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
        async with harness.factory() as session:
            bases = (await session.scalars(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))).all()
        assert bases == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_rebuild_serializes_against_concurrent_model_disable(postgres_database_url: str) -> None:
    """Rebinding onto a model mid-disable rejects and changes nothing."""

    harness, project_id, embedding_model_id = await _prepared(postgres_database_url)
    try:
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="并发重建", embedding_model_id=embedding_model_id))
        target_model_id, _ = await seed_registry_models(harness.factory)
        async with harness.factory() as session, session.begin():
            session.add(_seed_document(project_id, created.id, name="doc", status="ready"))

        async with harness.factory() as admin_session, admin_session.begin():
            row = await admin_session.scalar(select(ModelProviderModelRow).where(ModelProviderModelRow.id == target_model_id).with_for_update())
            assert row is not None
            row.status = "disabled"
            await admin_session.flush()
            rebuild_task = asyncio.create_task(
                harness.service.rebuild_knowledge_base(
                    project_id,
                    created.id,
                    embedding_model_id=target_model_id,
                )
            )
            done, _ = await asyncio.wait({rebuild_task}, timeout=0.5)
            assert not done
        with pytest.raises(KnowledgeError) as error:
            await rebuild_task
        assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE

        async with harness.factory() as session:
            base_row = await session.get(KnowledgeBaseRow, created.id)
            assert base_row is not None
            assert base_row.embedding_model_id == embedding_model_id
            document = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == created.id))
            assert document is not None
            assert document.status == "ready"
            assert document.version == 1
            tasks = (await session.scalars(select(KnowledgeTaskRow).where(KnowledgeTaskRow.kind == "ingest_document"))).all()
        assert tasks == []
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_initial_configurations_commit_one_complete_configuration(postgres_database_url: str) -> None:
    """Only the first configuration owns every field; its competitor cannot overwrite any."""

    harness = await _harness(postgres_database_url)
    configuration_tasks = []
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        first_embedding_id, first_reranker_id = await seed_registry_models(harness.factory)
        second_embedding_id, second_reranker_id = await seed_registry_models(harness.factory)
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="待配置"))
        first_update = KnowledgeBaseUpdate(
            name="第一套配置",
            description="第一套描述",
            embedding_model_id=first_embedding_id,
            reranker_model_id=first_reranker_id,
            retrieval_mode="hybrid",
            default_top_k=8,
            default_score_threshold=0.6,
        )
        second_update = KnowledgeBaseUpdate(
            name="第二套配置",
            description="第二套描述",
            embedding_model_id=second_embedding_id,
            reranker_model_id=second_reranker_id,
            retrieval_mode="semantic",
            default_top_k=2,
            default_score_threshold=0.1,
        )

        async with harness.factory() as blocker, blocker.begin():
            model = await blocker.scalar(select(ModelProviderModelRow).where(ModelProviderModelRow.id == first_embedding_id).with_for_update())
            assert model is not None
            first_task = asyncio.create_task(harness.service.update_knowledge_base(project_id, created.id, first_update))
            configuration_tasks.append(first_task)
            # The first call owns the Base lock while waiting for this model.
            done, _ = await asyncio.wait({first_task}, timeout=0.5)
            assert not done
            second_task = asyncio.create_task(harness.service.update_knowledge_base(project_id, created.id, second_update))
            configuration_tasks.append(second_task)
            # Its different models are unlocked; only the shared Base can
            # prevent the competitor from racing past the first configuration.
            done, _ = await asyncio.wait(set(configuration_tasks), timeout=0.5)
            assert not done

        first_result, second_result = await asyncio.wait_for(asyncio.gather(*configuration_tasks, return_exceptions=True), timeout=5)
        assert not isinstance(first_result, BaseException)
        assert isinstance(second_result, KnowledgeError)
        assert second_result.code == KNOWLEDGE_INVALID_REQUEST
        stored = await harness.service.get_knowledge_base(project_id, created.id)
        assert stored == first_result
        assert stored.name == first_update.name
        assert stored.description == first_update.description
        assert stored.embedding_model_id == first_embedding_id
        assert stored.reranker_model_id == first_reranker_id
        assert stored.retrieval_mode == first_update.retrieval_mode
        assert stored.default_top_k == first_update.default_top_k
        assert stored.default_score_threshold == first_update.default_score_threshold
        assert stored.document_count == 0
    finally:
        for task in configuration_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*configuration_tasks, return_exceptions=True)
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_initial_configuration_rolls_back_after_concurrent_reranker_disable(postgres_database_url: str) -> None:
    """A model disabled while initial binding waits leaves the entire Base unchanged."""

    harness = await _harness(postgres_database_url)
    configuration_task = None
    try:
        async with harness.factory() as session, session.begin():
            project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        embedding_id, reranker_id = await seed_registry_models(harness.factory)
        created = await harness.service.create_knowledge_base(project_id, KnowledgeBaseCreate(name="等待模型配置", description="尚未配置"))
        async with harness.factory() as admin_session, admin_session.begin():
            reranker = await admin_session.scalar(select(ModelProviderModelRow).where(ModelProviderModelRow.id == reranker_id).with_for_update())
            assert reranker is not None
            reranker.status = "disabled"
            await admin_session.flush()
            configuration_task = asyncio.create_task(
                harness.service.update_knowledge_base(
                    project_id,
                    created.id,
                    KnowledgeBaseUpdate(
                        name="不应保存的名称",
                        description="不应保存的描述",
                        embedding_model_id=embedding_id,
                        reranker_model_id=reranker_id,
                        retrieval_mode="hybrid",
                        default_top_k=9,
                        default_score_threshold=0.7,
                    ),
                )
            )
            # Embedding validation has no conflicting lock. The second model
            # must wait and re-read active status after this disable commits.
            done, _ = await asyncio.wait({configuration_task}, timeout=0.5)
            assert not done

        with pytest.raises(KnowledgeError) as error:
            await asyncio.wait_for(configuration_task, timeout=5)
        assert error.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
        # Includes models, defaults, mode, text fields, and updated_at.
        assert await harness.service.get_knowledge_base(project_id, created.id) == created
    finally:
        if configuration_task is not None:
            if not configuration_task.done():
                configuration_task.cancel()
            await asyncio.gather(configuration_task, return_exceptions=True)
        await harness.engine.dispose()
