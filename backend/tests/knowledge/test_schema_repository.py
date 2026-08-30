"""M1 gates: knowledge tables in Schema V1, ORM parity, queue and bootstrap.

Every test installs the real ``full_schema.sql`` snapshot into an isolated
``deerflow_test_*`` database, so the constraints exercised here are the ones
operators actually get from ``make setup-db``.
"""

from __future__ import annotations

import uuid
from base64 import b64encode
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeOrmBase,
    KnowledgeSegmentRow,
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
    ModelRegistryBootstrapSkipped,
    ModelRegistrySeed,
    bootstrap_default_model_registry,
    prepare_model_registry_bootstrap,
)
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.final_schema_contract import FINAL_APP_TABLES
from deerflow.persistence.model_registry import ModelProviderModelRow, ModelProviderRow

KNOWLEDGE_TABLES = (
    "knowledge_bases",
    "knowledge_documents",
    "knowledge_metadata_fields",
    "knowledge_segments",
    "knowledge_segment_children",
    "knowledge_queries",
    "knowledge_tasks",
)

# 宿主级模型注册表表（M9）：无 knowledge_ 前缀，knowledge_bases 通过 FK 绑定。
MODEL_REGISTRY_TABLES = (
    "model_providers",
    "model_provider_models",
)

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
    assert set(KnowledgeOrmBase.metadata.tables) == set(KNOWLEDGE_TABLES)

    from scripts.check_postgres import REQUIRED_TABLES

    assert set(KNOWLEDGE_TABLES) <= set(REQUIRED_TABLES)
    assert set(MODEL_REGISTRY_TABLES) <= set(REQUIRED_TABLES)

    engine = create_async_engine(postgres_database_url)
    try:
        await _install_full_schema(engine)
        # The registry rows live on the host harness metadata, not the
        # package-isolated KnowledgeOrmBase.
        host_metadata = ModelProviderRow.metadata
        async with engine.connect() as connection:
            for table_name in (*KNOWLEDGE_TABLES, *MODEL_REGISTRY_TABLES):
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

        # A lost bootstrap race finds the winner's rows and must not write.
        assert await bootstrap_default_model_registry(factory, _seed()) is False
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
        assert marker_count == 0
        assert provider_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_with_explicit_skip_installs_schema_without_seed(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit skip installs full Schema V1 with zero seeded providers."""

    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", _SECRET_KEY)
    monkeypatch.setenv("ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY", "unit-model-key")

    from app.system_settings.bootstrap import prepare_default_system_model_bootstrap
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
            provider_count = await connection.scalar(text("SELECT count(*) FROM model_providers"))
        assert marker_count == 1
        assert provider_count == 0
    finally:
        await engine.dispose()
