"""M1 gates: knowledge tables in Schema V1, ORM parity, queue and bootstrap.

Every test installs the real ``full_schema.sql`` snapshot into an isolated
``deerflow_test_*`` database, so the constraints exercised here are the ones
operators actually get from ``make setup-db``.
"""

from __future__ import annotations

import json
import uuid
from base64 import b64decode, b64encode
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeOrmBase,
    KnowledgeSegmentRow,
    KnowledgeSegmentSummaryRow,
    KnowledgeTaskRow,
)
from actweave_knowledge.persistence.tasks import claim_next_task
from registry_helpers import seed_registry_models
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.model_registry.bootstrap import (
    DEFAULT_EMBEDDING_MODEL_ID,
    DEFAULT_MODEL_PROVIDER_ID,
    DEFAULT_RERANK_MODEL_ID,
    ModelRegistryBootstrapConfigurationInvalid,
    ModelRegistryBootstrapConflict,
    ModelRegistryBootstrapSkipped,
    ModelRegistrySeed,
    bootstrap_default_model_registry,
    prepare_model_registry_bootstrap,
)
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.final_schema_contract import FINAL_APP_TABLES
from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
from deerflow.persistence.model_registry import ModelProviderModelRow, ModelProviderRow

KNOWLEDGE_TABLES = (
    "knowledge_bases",
    "knowledge_documents",
    "knowledge_metadata_fields",
    "knowledge_segments",
    "knowledge_segment_children",
    "knowledge_segment_summaries",
    "knowledge_queries",
    "knowledge_tasks",
)

# 宿主级模型注册表表（M9）：无 knowledge_ 前缀，knowledge_bases 通过 FK 绑定。
MODEL_REGISTRY_TABLES = (
    "model_providers",
    "model_provider_models",
)

# 宿主级知识系统设置单行表（M11）：注册在宿主 Base.metadata，不属于包 ORM。
HOST_KNOWLEDGE_SETTINGS_TABLES = ("knowledge_system_settings",)

_SECRET_KEY = b64encode(b"k" * 32).decode("ascii")


@pytest.fixture
def model_registry_bootstrap_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", _SECRET_KEY)
    monkeypatch.setenv(
        "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY",
        "unit-registry-plaintext-key",
    )
    monkeypatch.delenv("ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP", raising=False)


def _seed() -> ModelRegistrySeed:
    return ModelRegistrySeed(
        provider_id=uuid.uuid4(),
        provider_name="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        request_timeout_seconds=30,
        embedding_model_id=uuid.uuid4(),
        embedding_model_name="Qwen/Qwen3-VL-Embedding-8B",
        embedding_dimension=4096,
        embedding_max_batch=64,
        rerank_model_id=uuid.uuid4(),
        rerank_model_name="Qwen/Qwen3-VL-Reranker-8B",
        rerank_max_batch=32,
        api_key_nonce=b"n" * 12,
        api_key_ciphertext=b"c" * 24,
    )


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
        {
            "user_id": user_id,
            "email": f"{label}@example.invalid",
            "username": f"kb_{label}",
        },
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (
                   :project_id, :slug, :display_name, :user_id
               )"""
        ),
        {
            "project_id": project_id,
            "slug": f"kb-{label}",
            "display_name": label,
            "user_id": user_id,
        },
    )
    return project_id


async def _seed_base(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    embedding_model_id: uuid.UUID,
    name: str = "Handbook",
) -> KnowledgeBaseRow:
    base = KnowledgeBaseRow(
        id=uuid.uuid4(),
        project_id=project_id,
        name=name,
        embedding_model_id=embedding_model_id,
    )
    session.add(base)
    await session.flush()
    return base


def _document(
    base: KnowledgeBaseRow,
    *,
    status: str = "queued",
    error_message: str | None = None,
) -> KnowledgeDocumentRow:
    document_id = uuid.uuid4()
    return KnowledgeDocumentRow(
        id=document_id,
        project_id=base.project_id,
        knowledge_base_id=base.id,
        name="guide.md",
        original_name="guide.md",
        storage_key=f"{base.project_id}/{base.id}/{document_id}/v1",
        size_bytes=64,
        status=status,
        error_message=error_message,
    )


def _segment(
    document: KnowledgeDocumentRow,
    *,
    position: int,
    embedding: list[float],
) -> KnowledgeSegmentRow:
    return KnowledgeSegmentRow(
        id=uuid.uuid4(),
        project_id=document.project_id,
        knowledge_base_id=document.knowledge_base_id,
        knowledge_document_id=document.id,
        document_version=1,
        position=position,
        content=f"segment {position}",
        source_position={"page": position},
        embedding=embedding,
    )


def _summary(
    segment: KnowledgeSegmentRow,
    *,
    content: str = "系统生成的段摘要",
    document_version: int = 1,
    embedding: list[float] | None = None,
) -> KnowledgeSegmentSummaryRow:
    return KnowledgeSegmentSummaryRow(
        id=uuid.uuid4(),
        project_id=segment.project_id,
        knowledge_base_id=segment.knowledge_base_id,
        knowledge_document_id=segment.knowledge_document_id,
        knowledge_segment_id=segment.id,
        document_version=document_version,
        content=content,
        source_content_digest="a" * 64,
        embedding=embedding or [0.1, 0.2, 0.3],
    )


def _task(
    *,
    project_id: uuid.UUID,
    resource_id: uuid.UUID,
    kind: str = "ingest_document",
    target_version: int | None = 1,
    status: str = "queued",
    available_at: datetime | None = None,
    created_at: datetime | None = None,
    attempt_count: int = 0,
    claim_token: uuid.UUID | None = None,
    lease_until: datetime | None = None,
    finished_at: datetime | None = None,
    storage_key: str | None = None,
) -> KnowledgeTaskRow:
    now = datetime.now(UTC)
    return KnowledgeTaskRow(
        id=uuid.uuid4(),
        project_id=project_id,
        resource_id=resource_id,
        kind=kind,
        target_version=target_version,
        status=status,
        attempt_count=attempt_count,
        available_at=available_at or now,
        claim_token=claim_token,
        lease_until=lease_until,
        storage_key=storage_key,
        created_at=created_at or now,
        finished_at=finished_at,
    )


@pytest.mark.asyncio
async def test_orm_metadata_matches_installed_catalog(postgres_database_url: str) -> None:
    """ORM 列集合与 full_schema.sql 安装出的目录一致（Task 8 契约）。"""

    assert set(KNOWLEDGE_TABLES) <= FINAL_APP_TABLES
    assert set(MODEL_REGISTRY_TABLES) <= FINAL_APP_TABLES
    assert set(HOST_KNOWLEDGE_SETTINGS_TABLES) <= FINAL_APP_TABLES
    assert set(KnowledgeOrmBase.metadata.tables) == set(KNOWLEDGE_TABLES)

    from scripts.check_postgres import REQUIRED_TABLES

    assert set(KNOWLEDGE_TABLES) <= set(REQUIRED_TABLES)
    assert set(MODEL_REGISTRY_TABLES) <= set(REQUIRED_TABLES)
    assert set(HOST_KNOWLEDGE_SETTINGS_TABLES) <= set(REQUIRED_TABLES)

    engine = create_async_engine(postgres_database_url)
    try:
        await _install_full_schema(engine)
        # The registry and settings rows live on the host harness metadata,
        # not the package-isolated KnowledgeOrmBase.
        host_metadata = ModelProviderRow.metadata
        async with engine.connect() as connection:
            for table_name in (*KNOWLEDGE_TABLES, *MODEL_REGISTRY_TABLES, *HOST_KNOWLEDGE_SETTINGS_TABLES):
                rows = (
                    await connection.execute(
                        text(
                            """SELECT column_name, is_nullable
                               FROM information_schema.columns
                               WHERE table_schema = current_schema()
                                 AND table_name = :table_name"""
                        ),
                        {"table_name": table_name},
                    )
                ).all()
                catalog_columns = {name: nullable == "YES" for name, nullable in rows}
                orm_metadata = KnowledgeOrmBase.metadata if table_name in KNOWLEDGE_TABLES else host_metadata
                orm_table = orm_metadata.tables[table_name]
                orm_columns = {column.name: bool(column.nullable) for column in orm_table.columns}
                assert catalog_columns == orm_columns, table_name
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_default_registry_bootstrap_roundtrip_and_conflict(
    postgres_database_url: str,
    model_registry_bootstrap_environment: None,
) -> None:
    """Task 9：预检加密材料 → 安装唯一默认 Provider 与模型 → 重复安装不再写入。"""

    seed = prepare_model_registry_bootstrap()
    assert isinstance(seed, ModelRegistrySeed)
    assert seed.provider_id == DEFAULT_MODEL_PROVIDER_ID
    assert seed.embedding_model_id == DEFAULT_EMBEDDING_MODEL_ID
    assert seed.rerank_model_id == DEFAULT_RERANK_MODEL_ID
    assert len(seed.api_key_nonce) == 12
    assert len(seed.api_key_ciphertext) >= 16
    assert b"unit-registry-plaintext-key" not in seed.api_key_ciphertext
    assert "unit-registry-plaintext-key" not in repr(seed)

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        assert await bootstrap_default_model_registry(factory, seed) is True

        async with factory() as session:
            providers = (await session.execute(sa.select(ModelProviderRow))).scalars().all()
            assert len(providers) == 1
            provider = providers[0]
            assert provider.id == DEFAULT_MODEL_PROVIDER_ID
            assert provider.name == "SiliconFlow"
            assert provider.base_url == "https://api.siliconflow.cn/v1"
            assert provider.request_timeout_seconds == 30
            models = {row.model_type: row for row in (await session.execute(sa.select(ModelProviderModelRow))).scalars().all()}
            assert set(models) == {"embedding", "rerank"}
            embedding = models["embedding"]
            assert embedding.id == DEFAULT_EMBEDDING_MODEL_ID
            assert embedding.model_name == "Qwen/Qwen3-VL-Embedding-8B"
            assert embedding.embedding_dimension == 4096
            assert embedding.max_batch == 64
            assert embedding.status == "active"
            rerank = models["rerank"]
            assert rerank.id == DEFAULT_RERANK_MODEL_ID
            assert rerank.model_name == "Qwen/Qwen3-VL-Reranker-8B"
            assert rerank.embedding_dimension is None
            assert rerank.max_batch == 32
            assert rerank.status == "active"

        # A lost bootstrap race finds the winner's fixed identity and must not
        # write; installed-ness is the fixed provider UUID, not a row count.
        assert await bootstrap_default_model_registry(factory, prepare_model_registry_bootstrap()) is False
        # The default name held by a different identity is a loud conflict:
        # the seed never adopts or repairs another Provider's row.
        with pytest.raises(ModelRegistryBootstrapConflict):
            await bootstrap_default_model_registry(factory, _seed())
        async with factory() as session:
            provider_count = await session.scalar(sa.select(sa.func.count()).select_from(ModelProviderRow))
            model_count = await session.scalar(sa.select(sa.func.count()).select_from(ModelProviderModelRow))
            assert provider_count == 1
            assert model_count == 2

        async with factory() as session, session.begin():
            stored = await session.get(ModelProviderRow, DEFAULT_MODEL_PROVIDER_ID)
            assert stored is not None
            stored.name = "Renamed Provider"
            embedding_row = await session.get(ModelProviderModelRow, DEFAULT_EMBEDDING_MODEL_ID)
            assert embedding_row is not None
            embedding_row.status = "disabled"
        async with factory() as session:
            renamed = await session.get(ModelProviderRow, DEFAULT_MODEL_PROVIDER_ID)
            assert renamed is not None
            assert renamed.name == "Renamed Provider"
            disabled = await session.get(ModelProviderModelRow, DEFAULT_EMBEDDING_MODEL_ID)
            assert disabled is not None
            assert disabled.status == "disabled"
    finally:
        await engine.dispose()


def test_prepare_model_registry_bootstrap_fails_fast_without_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 9：缺 bootstrap Key 或主密钥时，在任何 DDL 前失败。"""

    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", _SECRET_KEY)
    monkeypatch.delenv("ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP", raising=False)
    with pytest.raises(ModelRegistryBootstrapConfigurationInvalid):
        prepare_model_registry_bootstrap()

    monkeypatch.setenv("ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY", "   ")
    with pytest.raises(ModelRegistryBootstrapConfigurationInvalid):
        prepare_model_registry_bootstrap()

    monkeypatch.setenv("ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY", "plain-key")
    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", "not-a-valid-key")
    with pytest.raises(ModelRegistryBootstrapConfigurationInvalid):
        prepare_model_registry_bootstrap()


def test_prepare_model_registry_bootstrap_honors_explicit_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP=1 skips the seed without any key material."""

    monkeypatch.delenv("ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("ACT_WEAVE_SECRET_KEY", raising=False)

    monkeypatch.setenv("ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP", "1")
    assert isinstance(prepare_model_registry_bootstrap(), ModelRegistryBootstrapSkipped)

    # Only the exact documented value skips; anything else keeps the preflight.
    monkeypatch.setenv("ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP", "yes")
    with pytest.raises(ModelRegistryBootstrapConfigurationInvalid):
        prepare_model_registry_bootstrap()


@pytest.mark.asyncio
async def test_empty_base_can_be_stored_before_embedding_configuration(
    postgres_database_url: str,
) -> None:
    """空知识库可以先保存，检索模式默认值和后续模型绑定仍由原有契约管理。"""

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, "unconfigured")
            base = KnowledgeBaseRow(
                id=uuid.uuid4(),
                project_id=project_id,
                name="Unconfigured knowledge",
            )
            session.add(base)
            await session.flush()
            await session.refresh(base)

            assert base.embedding_model_id is None
            assert base.reranker_model_id is None
            assert base.retrieval_mode == "semantic"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_base_names_unique_per_project_and_model_restrict(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        embedding_model_id, _ = await seed_registry_models(factory)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, "uniq")
            other_project_id = await _seed_project(session, "uniqother")
            await _seed_base(session, project_id=project_id, embedding_model_id=embedding_model_id, name="Docs")

            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    await _seed_base(
                        session,
                        project_id=project_id,
                        embedding_model_id=embedding_model_id,
                        name="dOCS",
                    )

            await _seed_base(
                session,
                project_id=other_project_id,
                embedding_model_id=embedding_model_id,
                name="Docs",
            )

            # An in-use model must not be deletable (RESTRICT).
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    await session.execute(sa.delete(ModelProviderModelRow).where(ModelProviderModelRow.id == embedding_model_id))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_delete_restricted_until_knowledge_rows_removed(
    postgres_database_url: str,
) -> None:
    """Project 挂着 Base 时数据库拒绝直接删除；清空后放行（purge 语义）。"""

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        embedding_model_id, _ = await seed_registry_models(factory)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, "purge")
            base = await _seed_base(session, project_id=project_id, embedding_model_id=embedding_model_id)

            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    await session.execute(
                        text("DELETE FROM projects WHERE id = :project_id"),
                        {"project_id": project_id},
                    )

            await session.execute(sa.delete(KnowledgeBaseRow).where(KnowledgeBaseRow.id == base.id))
            await session.execute(
                text("DELETE FROM projects WHERE id = :project_id"),
                {"project_id": project_id},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_document_status_error_and_storage_key_rules(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        embedding_model_id, _ = await seed_registry_models(factory)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, "docs")
            base = await _seed_base(session, project_id=project_id, embedding_model_id=embedding_model_id)

            document = _document(base)
            session.add(document)
            await session.flush()

            duplicate_key = _document(base)
            duplicate_key.storage_key = document.storage_key
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(duplicate_key)
                    await session.flush()

            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(_document(base, status="failed", error_message=None))
                    await session.flush()

            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(_document(base, status="exploded"))
                    await session.flush()

            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    bad_overlap = _document(base)
                    bad_overlap.chunk_size = 300
                    bad_overlap.chunk_overlap = 300
                    session.add(bad_overlap)
                    await session.flush()

            document.status = "failed"
            document.error_message = "boom"
            await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_segment_vector_roundtrip_and_cascade_delete(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        embedding_model_id, _ = await seed_registry_models(factory)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, "vec")
            base = await _seed_base(session, project_id=project_id, embedding_model_id=embedding_model_id)
            document = _document(base, status="ready")
            session.add(document)
            await session.flush()
            session.add_all(
                [
                    _segment(document, position=1, embedding=[0.5, -1.25, 3.0]),
                    _segment(document, position=2, embedding=[1.0, 2.0, 4.5]),
                ]
            )
            await session.flush()

            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(_segment(document, position=2, embedding=[0.0, 0.0, 1.0]))
                    await session.flush()

        async with factory() as session:
            stored = (await session.execute(sa.select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_document_id == document.id).order_by(KnowledgeSegmentRow.position))).scalars().all()
            assert [segment.position for segment in stored] == [1, 2]
            assert [round(float(value), 3) for value in stored[0].embedding] == [0.5, -1.25, 3.0]
            assert stored[0].source_position == {"page": 1}

        async with factory() as session, session.begin():
            await session.execute(sa.delete(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == document.id))
        async with factory() as session:
            remaining = await session.scalar(sa.select(sa.func.count()).select_from(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_document_id == document.id))
            assert remaining == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_segment_summary_unique_per_segment_and_cascades_with_segment(
    postgres_database_url: str,
) -> None:
    """M11 T1：摘要行每段唯一、约束探针、删段级联删摘要。"""

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        embedding_model_id, _ = await seed_registry_models(factory)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, "summary")
            base = await _seed_base(session, project_id=project_id, embedding_model_id=embedding_model_id)
            document = _document(base, status="ready")
            session.add(document)
            await session.flush()
            segment = _segment(document, position=1, embedding=[0.5, -1.25, 3.0])
            sibling = _segment(document, position=2, embedding=[1.0, 2.0, 4.5])
            session.add_all([segment, sibling])
            await session.flush()

            session.add(_summary(segment))
            await session.flush()

            # 每段至多一条摘要（uq_knowledge_segment_summaries_segment）。
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(_summary(segment, content="重复摘要"))
                    await session.flush()

            # document_version 必须 >= 1。
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(_summary(sibling, document_version=0))
                    await session.flush()

            # content 必须非空（length(content) > 0）。
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(_summary(sibling, content=""))
                    await session.flush()

            session.add(_summary(sibling))
            await session.flush()

        async with factory() as session:
            stored = (await session.execute(sa.select(KnowledgeSegmentSummaryRow).where(KnowledgeSegmentSummaryRow.knowledge_document_id == document.id).order_by(KnowledgeSegmentSummaryRow.created_at))).scalars().all()
            assert len(stored) == 2
            assert stored[0].source_content_digest == "a" * 64
            assert [round(float(value), 3) for value in stored[0].embedding] == [0.1, 0.2, 0.3]

        # 删除父段：摘要随 FK 级联删除，另一段的摘要保留。
        async with factory() as session, session.begin():
            await session.execute(sa.delete(KnowledgeSegmentRow).where(KnowledgeSegmentRow.id == segment.id))
        async with factory() as session:
            remaining = (await session.execute(sa.select(KnowledgeSegmentSummaryRow.knowledge_segment_id).where(KnowledgeSegmentSummaryRow.knowledge_document_id == document.id))).scalars().all()
            assert remaining == [sibling.id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_system_settings_singleton_and_minio_guards(
    postgres_database_url: str,
) -> None:
    """M11 T1：单行 CHECK、密文对约束、enabled-requires-minio、repr 不带密文。"""

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            session.add(KnowledgeSystemSettingsRow(id=1))
            await session.flush()

            # 单行表：id 只能是 1。
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(KnowledgeSystemSettingsRow(id=2))
                    await session.flush()

        async with factory() as session:
            stored = await session.get(KnowledgeSystemSettingsRow, 1)
            assert stored is not None
            assert stored.revision == 1
            assert stored.enabled is False
            assert stored.worker_concurrency == 2
            assert stored.task_timeout_seconds == 900
            assert stored.upload_max_bytes == 52428800
            assert stored.max_knowledge_bases_per_project == 20
            assert stored.max_documents_per_knowledge_base == 500
            assert stored.max_segments_per_document == 5000
            assert stored.minio_endpoint is None
            assert stored.minio_secure is False
            assert stored.summary_model_name is None
            assert stored.query_cache_enabled is True
            assert stored.query_cache_max_entries == 512
            assert stored.query_cache_ttl_seconds == 300

        async with factory() as session, session.begin():
            row = await session.get(KnowledgeSystemSettingsRow, 1)
            assert row is not None

            # 未配齐 MinIO 五要素时不得启用。
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    row.enabled = True
                    await session.flush()
            session.expire(row)
            row = await session.get(KnowledgeSystemSettingsRow, 1)
            assert row is not None

            # 密文对必须同空同非空。
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    row.minio_secret_nonce = b"n" * 12
                    await session.flush()
            session.expire(row)
            row = await session.get(KnowledgeSystemSettingsRow, 1)
            assert row is not None

            # 非空时 nonce 必须 12 字节、密文至少 16 字节。
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    row.minio_secret_nonce = b"n" * 11
                    row.minio_secret_ciphertext = b"c" * 24
                    await session.flush()
            session.expire(row)
            row = await session.get(KnowledgeSystemSettingsRow, 1)
            assert row is not None

            row.enabled = True
            row.minio_endpoint = "127.0.0.1:9000"
            row.minio_bucket = "actweave-knowledge"
            row.minio_access_key = "minio-access-value"
            row.minio_secret_nonce = b"n" * 12
            row.minio_secret_ciphertext = b"c" * 24
            await session.flush()

        async with factory() as session:
            enabled_row = await session.get(KnowledgeSystemSettingsRow, 1)
            assert enabled_row is not None
            assert enabled_row.enabled is True
            rendered = repr(enabled_row)
            assert "minio_secret_nonce" not in rendered
            assert "minio_secret_ciphertext" not in rendered
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_summarize_task_shares_the_open_indexing_slot(
    postgres_database_url: str,
) -> None:
    """M11 T1：summarize 与 ingest/reembed 同文档同版本互斥；约束按字面执行。"""

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, "summarize")
            resource_id = uuid.uuid4()

            session.add(_task(project_id=project_id, resource_id=resource_id, kind="summarize_document"))
            await session.flush()

            # 开放的 summarize 任务占用同一开放索引槽：ingest 与 reembed 均被拒。
            for other_kind in ("ingest_document", "reembed_document", "summarize_document"):
                with pytest.raises(sa.exc.IntegrityError):
                    async with session.begin_nested():
                        session.add(_task(project_id=project_id, resource_id=resource_id, kind=other_kind))
                        await session.flush()

            # summarize 属于"必须携带 target_version"的种类。
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(
                        _task(
                            project_id=project_id,
                            resource_id=uuid.uuid4(),
                            kind="summarize_document",
                            target_version=None,
                        )
                    )
                    await session.flush()

            # 冻结的 reparse 参数仍仅允许 ingest 携带。
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    reparse_carrier = _task(
                        project_id=project_id,
                        resource_id=uuid.uuid4(),
                        kind="summarize_document",
                    )
                    reparse_carrier.reparse_settings = {"chunk_size": 1000}
                    session.add(reparse_carrier)
                    await session.flush()

            # 新 stage 字面量 summarizing 被 CHECK 接受。
            running = _task(
                project_id=project_id,
                resource_id=uuid.uuid4(),
                kind="summarize_document",
                status="running",
                attempt_count=1,
                claim_token=uuid.uuid4(),
                lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
            running.stage = "summarizing"
            session.add(running)
            await session.flush()

            # 结清后释放开放槽。
            await session.execute(sa.update(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == resource_id).values(status="succeeded", finished_at=datetime.now(UTC)))
            session.add(_task(project_id=project_id, resource_id=resource_id, kind="ingest_document"))
            await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_open_task_partial_uniques_and_kind_rules(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, "tasks")
            resource_id = uuid.uuid4()

            session.add(_task(project_id=project_id, resource_id=resource_id))
            await session.flush()

            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(_task(project_id=project_id, resource_id=resource_id))
                    await session.flush()

            # A different indexing kind must not slip past the open slot: one
            # document/version admits one open indexing operation, period.
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(
                        _task(
                            project_id=project_id,
                            resource_id=resource_id,
                            kind="reembed_document",
                        )
                    )
                    await session.flush()

            # Same document, next version stays open in parallel.
            session.add(_task(project_id=project_id, resource_id=resource_id, target_version=2))
            await session.flush()

            # A settled task frees the open-task slot.
            settled_at = datetime.now(UTC)
            await session.execute(sa.update(KnowledgeTaskRow).where(KnowledgeTaskRow.resource_id == resource_id, KnowledgeTaskRow.target_version == 1).values(status="succeeded", finished_at=settled_at))
            session.add(_task(project_id=project_id, resource_id=resource_id))
            await session.flush()

            delete_resource = uuid.uuid4()
            session.add(
                _task(
                    project_id=project_id,
                    resource_id=delete_resource,
                    kind="delete_document",
                    target_version=None,
                )
            )
            await session.flush()
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(
                        _task(
                            project_id=project_id,
                            resource_id=delete_resource,
                            kind="delete_document",
                            target_version=None,
                        )
                    )
                    await session.flush()

            # Object-only cleanup is a separate durable work kind. It must be
            # able to coexist with the still-running ordinary delete that
            # removed the Document row before a late upload put completed.
            orphan_key = f"projects/{project_id}/knowledge/{uuid.uuid4()}/{delete_resource}.pdf"
            session.add(
                _task(
                    project_id=project_id,
                    resource_id=delete_resource,
                    kind="delete_document_object",
                    target_version=None,
                    storage_key=orphan_key,
                )
            )
            await session.flush()

            # Exact object authority is mandatory only for this task kind.
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(
                        _task(
                            project_id=project_id,
                            resource_id=uuid.uuid4(),
                            kind="delete_document_object",
                            target_version=None,
                        )
                    )
                    await session.flush()
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(
                        _task(
                            project_id=project_id,
                            resource_id=uuid.uuid4(),
                            kind="delete_document",
                            target_version=None,
                            storage_key=orphan_key,
                        )
                    )
                    await session.flush()

            # delete tasks must not carry target_version.
            with pytest.raises(sa.exc.IntegrityError):
                async with session.begin_nested():
                    session.add(
                        _task(
                            project_id=project_id,
                            resource_id=uuid.uuid4(),
                            kind="delete_knowledge_base",
                            target_version=3,
                        )
                    )
                    await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_next_task_orders_and_skips_locked_rows(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base_time = datetime(2036, 1, 2, 3, 0, tzinfo=UTC)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, "claim")
            first = _task(
                project_id=project_id,
                resource_id=uuid.uuid4(),
                available_at=base_time,
                created_at=base_time,
            )
            second = _task(
                project_id=project_id,
                resource_id=uuid.uuid4(),
                available_at=base_time + timedelta(seconds=1),
                created_at=base_time,
            )
            future = _task(
                project_id=project_id,
                resource_id=uuid.uuid4(),
                available_at=base_time + timedelta(days=365 * 20),
                created_at=base_time,
            )
            session.add_all([first, second, future])

        claim_at = base_time + timedelta(minutes=1)
        async with factory() as first_session, factory() as second_session:
            async with first_session.begin(), second_session.begin():
                first_claim = await claim_next_task(first_session, lease_seconds=60, now=claim_at)
                assert first_claim is not None
                assert first_claim.id == first.id
                assert first_claim.status == "running"
                assert first_claim.attempt_count == 1
                assert first_claim.claim_token is not None
                assert first_claim.lease_until == claim_at + timedelta(seconds=60)

                # A concurrent worker must skip the locked row and take the next one.
                second_claim = await claim_next_task(second_session, lease_seconds=60, now=claim_at)
                assert second_claim is not None
                assert second_claim.id == second.id

        async with factory() as session, session.begin():
            third_claim = await claim_next_task(session, lease_seconds=60, now=claim_at)
            assert third_claim is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_reclaims_expired_lease_only_while_attempts_remain(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    moment = datetime(2036, 2, 3, 4, 0, tzinfo=UTC)
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            project_id = await _seed_project(session, "lease")
            expired = _task(
                project_id=project_id,
                resource_id=uuid.uuid4(),
                status="running",
                attempt_count=1,
                claim_token=uuid.uuid4(),
                available_at=moment - timedelta(minutes=10),
                created_at=moment - timedelta(minutes=10),
                lease_until=moment - timedelta(minutes=1),
            )
            session.add(expired)

        async with factory() as session, session.begin():
            reclaimed = await claim_next_task(session, lease_seconds=30, now=moment)
            assert reclaimed is not None
            assert reclaimed.id == expired.id
            assert reclaimed.attempt_count == 2
            assert reclaimed.lease_until == moment + timedelta(seconds=30)

        async with factory() as session, session.begin():
            await session.execute(
                sa.update(KnowledgeTaskRow)
                .where(KnowledgeTaskRow.id == expired.id)
                .values(
                    attempt_count=3,
                    lease_until=moment - timedelta(seconds=5),
                    updated_at=moment,
                )
            )

        async with factory() as session, session.begin():
            # Exhausted expired leases are settled by the executor, not re-claimed.
            assert await claim_next_task(session, lease_seconds=30, now=moment) is None
        async with factory() as session:
            row = await session.get(KnowledgeTaskRow, expired.id)
            assert row is not None
            assert row.status == "running"
            assert row.attempt_count == 3

        # A queued row whose attempts are already spent (for example after an
        # erroneous manual requeue) must never be claimed into a fourth attempt.
        async with factory() as session, session.begin():
            exhausted_queued = _task(
                project_id=project_id,
                resource_id=uuid.uuid4(),
                status="queued",
                attempt_count=3,
                available_at=moment - timedelta(minutes=5),
                created_at=moment - timedelta(minutes=5),
            )
            session.add(exhausted_queued)
        async with factory() as session, session.begin():
            assert await claim_next_task(session, lease_seconds=30, now=moment) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_registry_seed_fails_bootstrap_without_marker(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 9：模型注册表预检材料缺失时 Schema V1 marker 不发布。"""

    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", _SECRET_KEY)
    monkeypatch.setenv("ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY", "unit-model-key")

    from app.system_settings.bootstrap import prepare_default_system_model_bootstrap
    from scripts import setup_postgres

    default_model_bootstrap = prepare_default_system_model_bootstrap()

    with pytest.raises(setup_postgres.PostgresSetupError, match="MODEL_REGISTRY_BOOTSTRAP_SEED_MISSING"):
        await setup_postgres._bootstrap_empty_schema_under_lock(
            postgres_database_url,
            default_model_bootstrap=default_model_bootstrap,
            model_registry_bootstrap=None,
        )

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            marker_count = await connection.scalar(text("SELECT count(*) FROM alembic_version"))
            provider_count = await connection.scalar(text("SELECT count(*) FROM model_providers"))
            retrieval_count = await connection.scalar(text("SELECT count(*) FROM model_provider_models"))
        # The Schema V1 marker is never published; the earlier DeepSeek stage
        # committed its Provider, but the SiliconFlow seed wrote nothing.
        assert marker_count == 0
        assert provider_count == 1
        assert retrieval_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_with_explicit_skip_installs_schema_without_seed(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit skip omits only the SiliconFlow seed; DeepSeek still lands."""

    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", _SECRET_KEY)
    monkeypatch.setenv("ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY", "unit-model-key")

    from app.system_settings.bootstrap import (
        DEEPSEEK_PROVIDER_ID,
        prepare_default_system_model_bootstrap,
    )
    from scripts import setup_postgres

    revision = await setup_postgres._bootstrap_empty_schema_under_lock(
        postgres_database_url,
        default_model_bootstrap=prepare_default_system_model_bootstrap(),
        model_registry_bootstrap=ModelRegistryBootstrapSkipped(),
    )
    assert revision == setup_postgres.CURRENT_SCHEMA_REVISION

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            marker_count = await connection.scalar(text("SELECT count(*) FROM alembic_version"))
            provider_ids = [row[0] for row in await connection.execute(text("SELECT id FROM model_providers"))]
            retrieval_count = await connection.scalar(text("SELECT count(*) FROM model_provider_models"))
            text_model_count = await connection.scalar(text("SELECT count(*) FROM system_model_configs WHERE provider_id = :provider_id"), {"provider_id": DEEPSEEK_PROVIDER_ID})
        assert marker_count == 1
        assert provider_ids == [DEEPSEEK_PROVIDER_ID]
        assert retrieval_count == 0
        assert text_model_count == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_default_full_install_seeds_both_providers_with_their_own_keys(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """联合验收：默认全新安装得到 2 供应商 / 3 文本模型 / 2 检索模型 / 3 代际。

    DeepSeek 先初始化不会让 SiliconFlow 漏建；两把不同的虚构 Key 分别保护各自
    供应商，DeepSeek 三个模型代际解密结果与其供应商 Key 一致。直接重复
    bootstrap 只读返回，不覆盖、不新增代际。
    """

    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", _SECRET_KEY)
    monkeypatch.setenv("ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY", "unit-deepseek-key")
    monkeypatch.setenv(
        "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY",
        "unit-siliconflow-key",
    )
    monkeypatch.delenv("ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP", raising=False)

    from app.model_registry.secrets import materialize_provider_api_key
    from app.system_settings.bootstrap import (
        DEEPSEEK_PROVIDER_ID,
        DEEPSEEK_V4_FLASH_MODEL_ID,
        DEEPSEEK_V4_FLASH_VISION_EXP_MODEL_ID,
        bootstrap_default_system_model,
        prepare_default_system_model_bootstrap,
    )
    from app.system_settings.secrets import model_secret_recipient
    from deerflow.secrets import SecretEnvelope, SecretKey
    from scripts import setup_postgres

    revision = await setup_postgres._bootstrap_empty_schema_under_lock(
        postgres_database_url,
        default_model_bootstrap=prepare_default_system_model_bootstrap(),
        model_registry_bootstrap=prepare_model_registry_bootstrap(),
    )
    assert revision == setup_postgres.CURRENT_SCHEMA_REVISION

    secret_key = SecretKey(b64decode(_SECRET_KEY))
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.connect() as connection:
            providers = {row.id: row for row in await connection.execute(text("SELECT id, name, base_url, api_key_nonce, api_key_ciphertext FROM model_providers"))}
            text_models = list(
                await connection.execute(
                    text("SELECT id, provider_id, provider_adapter, settings, status FROM system_model_configs ORDER BY id"),
                )
            )
            retrieval_models = list(
                await connection.execute(
                    text("SELECT id, provider_id, model_type FROM model_provider_models ORDER BY id"),
                )
            )
            generations = list(
                await connection.execute(
                    text("SELECT model_config_id, nonce, ciphertext FROM system_model_secret_generations"),
                )
            )
            default_model_id = await connection.scalar(text("SELECT default_model_config_id FROM system_model_catalog_state WHERE id = 1"))
            vision_flags = {row.id: row.supports_vision for row in await connection.execute(text("SELECT id, supports_vision FROM system_model_configs"))}

        assert set(providers) == {DEEPSEEK_PROVIDER_ID, DEFAULT_MODEL_PROVIDER_ID}
        assert providers[DEEPSEEK_PROVIDER_ID].name == "DeepSeek"
        assert providers[DEFAULT_MODEL_PROVIDER_ID].name == "SiliconFlow"

        # Three individually selectable text models, all bound to DeepSeek.
        assert len(text_models) == 3
        assert len({row.id for row in text_models}) == 3
        assert all(row.provider_id == DEEPSEEK_PROVIDER_ID for row in text_models)
        assert all(row.status == "active" for row in text_models)
        assert default_model_id == DEEPSEEK_V4_FLASH_MODEL_ID
        assert vision_flags[DEEPSEEK_V4_FLASH_VISION_EXP_MODEL_ID] is True

        # Both retrieval models bound to SiliconFlow; they never enter the
        # text-model catalog.
        assert {row.model_type for row in retrieval_models} == {"embedding", "rerank"}
        assert all(row.provider_id == DEFAULT_MODEL_PROVIDER_ID for row in retrieval_models)

        # Each Provider keeps its own Key: DeepSeek's envelope and all three
        # model generations open to the DeepSeek Key, SiliconFlow to its own.
        deepseek_row = providers[DEEPSEEK_PROVIDER_ID]
        assert (
            materialize_provider_api_key(
                provider_id=DEEPSEEK_PROVIDER_ID,
                base_url=deepseek_row.base_url,
                nonce=bytes(deepseek_row.api_key_nonce),
                ciphertext=bytes(deepseek_row.api_key_ciphertext),
                key=secret_key,
            )
            == "unit-deepseek-key"
        )
        siliconflow_row = providers[DEFAULT_MODEL_PROVIDER_ID]
        assert (
            materialize_provider_api_key(
                provider_id=DEFAULT_MODEL_PROVIDER_ID,
                base_url=siliconflow_row.base_url,
                nonce=bytes(siliconflow_row.api_key_nonce),
                ciphertext=bytes(siliconflow_row.api_key_ciphertext),
                key=secret_key,
            )
            == "unit-siliconflow-key"
        )
        assert len(generations) == 3
        models_by_id = {row.id: row for row in text_models}
        for generation in generations:
            model = models_by_id[generation.model_config_id]
            settings = model.settings if isinstance(model.settings, dict) else json.loads(model.settings)
            recipient = model_secret_recipient(
                generation.model_config_id,
                model.provider_adapter,
                settings,
            )
            envelope = SecretEnvelope(
                nonce=bytes(generation.nonce),
                ciphertext=bytes(generation.ciphertext),
            )
            assert envelope.materialize(recipient=recipient, key=secret_key) == b"unit-deepseek-key"

        # Direct bootstrap replays are read-only: no overwrites, no new
        # generations, and the SiliconFlow seed never adopts foreign rows.
        assert await bootstrap_default_system_model(factory, prepare_default_system_model_bootstrap()) is False
        assert await bootstrap_default_model_registry(factory, prepare_model_registry_bootstrap()) is False
        async with engine.connect() as connection:
            generation_count = await connection.scalar(text("SELECT count(*) FROM system_model_secret_generations"))
            provider_count = await connection.scalar(text("SELECT count(*) FROM model_providers"))
        assert generation_count == 3
        assert provider_count == 2
    finally:
        await engine.dispose()
