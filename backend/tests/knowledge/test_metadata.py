"""K4 gates: metadata field definitions, document metadata, and their routes.

Service tests run against the installed Schema V1 snapshot so the
case-insensitive unique index, the JSONB key rewrites on rename/delete, and
the quota lock are exercised for real. HTTP tests pin the new route contract
over ASGI with a recording fake module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MAX_METADATA_FIELDS_PER_BASE,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeMetadataFieldView,
)
from actweave_knowledge.metadata import KnowledgeMetadataService
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeMetadataFieldRow,
    KnowledgeModelConfigurationRow,
)
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge import gateway
from deerflow.persistence.bootstrap import _install_full_schema

# ---------------------------------------------------------------------------
# Service harness
# ---------------------------------------------------------------------------


class _Harness:
    def __init__(self, engine, factory, service: KnowledgeMetadataService) -> None:  # noqa: ANN001
        self.engine = engine
        self.factory = factory
        self.service = service


async def _harness(postgres_database_url: str) -> _Harness:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    return _Harness(engine, factory, KnowledgeMetadataService(session_factory=factory))


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
        {"user_id": user_id, "email": f"{label}@example.invalid", "username": f"k4_{label}"},
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:project_id, :slug, :display_name, :user_id)"""
        ),
        {"project_id": project_id, "slug": f"k4-{label}", "display_name": label, "user_id": user_id},
    )
    return project_id


def _configuration_row() -> KnowledgeModelConfigurationRow:
    configuration_id = uuid.uuid4()
    return KnowledgeModelConfigurationRow(
        id=configuration_id,
        display_name=f"cfg-{configuration_id.hex[:12]}",
        status="active",
        base_url="https://provider.invalid/v1",
        embedding_model="embed-model",
        embedding_dimension=3,
        embedding_max_batch=64,
        reranker_model="rerank-model",
        reranker_max_batch=32,
        request_timeout_seconds=30,
        api_key_nonce=b"n" * 12,
        api_key_ciphertext=b"c" * 16,
    )


def _document_row(project_id: uuid.UUID, base_id: uuid.UUID, *, name: str, status: str = "ready") -> KnowledgeDocumentRow:
    document_id = uuid.uuid4()
    return KnowledgeDocumentRow(
        id=document_id,
        project_id=project_id,
        knowledge_base_id=base_id,
        name=name,
        original_name=f"{name}.md",
        storage_key=f"projects/{project_id}/knowledge/{base_id}/{document_id}.md",
        size_bytes=64,
        status=status,
        version=1,
        chunk_size=1000,
        chunk_overlap=100,
    )


async def _seed_base(harness: _Harness) -> tuple[uuid.UUID, uuid.UUID]:
    """Project + configuration + one active base. Returns (project_id, base_id)."""

    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        configuration = _configuration_row()
        session.add(configuration)
        await session.flush()
        base = KnowledgeBaseRow(
            id=uuid.uuid4(),
            project_id=project_id,
            name=f"base-{uuid.uuid4().hex[:6]}",
            model_configuration_id=configuration.id,
            status="active",
        )
        session.add(base)
    return project_id, base.id


async def _doc_metadata(harness: _Harness, document_id: uuid.UUID) -> dict[str, Any]:
    async with harness.factory() as session:
        return dict(await session.scalar(select(KnowledgeDocumentRow.doc_metadata).where(KnowledgeDocumentRow.id == document_id)))


# ---------------------------------------------------------------------------
# Field definition CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_field_create_then_list_round_trips_in_creation_order(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)

        department = await harness.service.create_metadata_field(project_id, base_id, name="部门", field_type="string")
        year = await harness.service.create_metadata_field(project_id, base_id, name="year", field_type="number")
        published = await harness.service.create_metadata_field(project_id, base_id, name="published_at", field_type="time")

        assert department.field_type == "string"
        assert department.knowledge_base_id == base_id
        listed = await harness.service.list_metadata_fields(project_id, base_id)
        assert [(field.name, field.field_type) for field in listed] == [
            ("部门", "string"),
            ("year", "number"),
            ("published_at", "time"),
        ]
        assert [field.id for field in listed] == [department.id, year.id, published.id]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_field_names_are_unique_per_base_case_insensitively(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        await harness.service.create_metadata_field(project_id, base_id, name="Category", field_type="string")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_metadata_field(project_id, base_id, name="category", field_type="number")
        assert error.value.code == KNOWLEDGE_NAME_CONFLICT

        # The same name in another base is a separate namespace.
        async with harness.factory() as session, session.begin():
            configuration = _configuration_row()
            session.add(configuration)
            await session.flush()
            sibling = KnowledgeBaseRow(
                id=uuid.uuid4(),
                project_id=project_id,
                name=f"base-{uuid.uuid4().hex[:6]}",
                model_configuration_id=configuration.id,
                status="active",
            )
            session.add(sibling)
        created = await harness.service.create_metadata_field(project_id, sibling.id, name="category", field_type="number")
        assert created.name == "category"
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "field_type", "fragment"),
    [
        ("   ", "string", "name"),
        ("字" * 65, "string", "name"),
        ("dept", "boolean", "field_type"),
    ],
)
async def test_field_create_rejects_invalid_name_and_type(postgres_database_url: str, name: str, field_type: str, fragment: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_metadata_field(project_id, base_id, name=name, field_type=field_type)  # type: ignore[arg-type]
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert fragment in error.value.message
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_field_create_enforces_the_per_base_quota(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        async with harness.factory() as session, session.begin():
            for index in range(KNOWLEDGE_MAX_METADATA_FIELDS_PER_BASE):
                session.add(
                    KnowledgeMetadataFieldRow(
                        id=uuid.uuid4(),
                        project_id=project_id,
                        knowledge_base_id=base_id,
                        name=f"field_{index}",
                        field_type="string",
                    )
                )

        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_metadata_field(project_id, base_id, name="超额", field_type="string")
        assert error.value.code == KNOWLEDGE_QUOTA_EXCEEDED
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_field_operations_are_project_scoped(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        other_project, _ = await _seed_base(harness)
        field = await harness.service.create_metadata_field(project_id, base_id, name="dept", field_type="string")

        for call in (
            harness.service.list_metadata_fields(other_project, base_id),
            harness.service.create_metadata_field(other_project, base_id, name="x", field_type="string"),
            harness.service.rename_metadata_field(other_project, field.id, name="y"),
            harness.service.delete_metadata_field(other_project, field.id),
        ):
            with pytest.raises(KnowledgeError) as error:
                await call
            assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Document metadata assignment
# ---------------------------------------------------------------------------


async def _seed_fielded_document(harness: _Harness) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Base with string/number/time fields plus one ready document."""

    project_id, base_id = await _seed_base(harness)
    await harness.service.create_metadata_field(project_id, base_id, name="部门", field_type="string")
    await harness.service.create_metadata_field(project_id, base_id, name="year", field_type="number")
    await harness.service.create_metadata_field(project_id, base_id, name="published_at", field_type="time")
    async with harness.factory() as session, session.begin():
        document = _document_row(project_id, base_id, name="手册")
        session.add(document)
    return project_id, base_id, document.id


@pytest.mark.asyncio
async def test_set_document_metadata_merges_and_removes_keys(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, document_id = await _seed_fielded_document(harness)

        view = await harness.service.set_document_metadata(project_id, document_id, {"部门": "工程", "year": 2026})
        assert view.doc_metadata == {"部门": "工程", "year": 2026}

        # Partial update merges; None removes exactly that key.
        view = await harness.service.set_document_metadata(project_id, document_id, {"published_at": 1756400000, "year": None})
        assert view.doc_metadata == {"部门": "工程", "published_at": 1756400000}
        assert await _doc_metadata(harness, document_id) == {"部门": "工程", "published_at": 1756400000}

        # Removing an absent key is a no-op, not an error.
        view = await harness.service.set_document_metadata(project_id, document_id, {"year": None})
        assert view.doc_metadata == {"部门": "工程", "published_at": 1756400000}
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_set_document_metadata_rejects_unknown_keys_and_wrong_types(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _, document_id = await _seed_fielded_document(harness)

        cases: list[tuple[dict[str, Any], str]] = [
            ({"未定义": "x"}, "未定义"),
            ({"部门": 123}, "字符串"),
            ({"部门": "字" * 501}, "500"),
            ({"year": "2026"}, "数字"),
            ({"year": True}, "数字"),  # bool is an int subclass; must not pass
            ({"published_at": "昨天"}, "epoch"),
            ({"year": float("inf")}, "数字"),
        ]
        for values, fragment in cases:
            with pytest.raises(KnowledgeError) as error:
                await harness.service.set_document_metadata(project_id, document_id, values)
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST, values
            assert fragment in error.value.message, values

        # A rejected call must not partially apply.
        assert await _doc_metadata(harness, document_id) == {}
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# Rename / delete rewrite document keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_field_rewrites_document_keys_only_in_its_base(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_id = await _seed_fielded_document(harness)
        listed = await harness.service.list_metadata_fields(project_id, base_id)
        department = next(field for field in listed if field.name == "部门")
        await harness.service.set_document_metadata(project_id, document_id, {"部门": "工程", "year": 2026})

        # Another base defines its own 部门 field; its document must not change.
        other_project, other_base = await _seed_base(harness)
        await harness.service.create_metadata_field(other_project, other_base, name="部门", field_type="string")
        async with harness.factory() as session, session.begin():
            other_document = _document_row(other_project, other_base, name="别库文档")
            session.add(other_document)
        await harness.service.set_document_metadata(other_project, other_document.id, {"部门": "市场"})

        renamed = await harness.service.rename_metadata_field(project_id, department.id, name="所属部门")

        assert renamed.name == "所属部门"
        assert await _doc_metadata(harness, document_id) == {"所属部门": "工程", "year": 2026}
        assert await _doc_metadata(harness, other_document.id) == {"部门": "市场"}

        # Renaming onto an existing name in the same base conflicts.
        with pytest.raises(KnowledgeError) as error:
            await harness.service.rename_metadata_field(project_id, renamed.id, name="YEAR")
        assert error.value.code == KNOWLEDGE_NAME_CONFLICT
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_delete_field_strips_the_key_from_documents(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_id = await _seed_fielded_document(harness)
        listed = await harness.service.list_metadata_fields(project_id, base_id)
        year = next(field for field in listed if field.name == "year")
        await harness.service.set_document_metadata(project_id, document_id, {"部门": "工程", "year": 2026})

        await harness.service.delete_metadata_field(project_id, year.id)

        assert await _doc_metadata(harness, document_id) == {"部门": "工程"}
        remaining = await harness.service.list_metadata_fields(project_id, base_id)
        assert [field.name for field in remaining] == ["部门", "published_at"]

        # The freed name can be defined again, now with a different type.
        await harness.service.create_metadata_field(project_id, base_id, name="year", field_type="time")
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# HTTP contract for the K4 routes
# ---------------------------------------------------------------------------

_REQUEST_ID = "knowledge-k4-contract"
_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_BASE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_DOCUMENT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_FIELD_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_CONFIGURATION_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
_NOW = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)


def _field_view(**overrides: object) -> KnowledgeMetadataFieldView:
    values: dict[str, object] = {
        "id": _FIELD_ID,
        "knowledge_base_id": _BASE_ID,
        "name": "部门",
        "field_type": "string",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return KnowledgeMetadataFieldView(**values)  # type: ignore[arg-type]


def _document_view(**overrides: object) -> KnowledgeDocumentView:
    values: dict[str, object] = {
        "id": _DOCUMENT_ID,
        "project_id": _PROJECT_ID,
        "knowledge_base_id": _BASE_ID,
        "name": "季度报告",
        "original_name": "report.pdf",
        "media_type": "application/pdf",
        "size_bytes": 11,
        "status": "ready",
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
        "segment_count": 1,
        "word_count": 12,
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
        self.calls: list[tuple[str, Any]] = []
        self.error: KnowledgeError | None = None

    def _record(self, verb: str, payload: Any):  # noqa: ANN401, ANN202
        self.calls.append((verb, payload))
        if self.error is not None:
            raise self.error

    async def list_metadata_fields(self, project_id, base_id):  # noqa: ANN001
        self._record("list", (project_id, base_id))
        return [_field_view()]

    async def create_metadata_field(self, project_id, base_id, *, name, field_type):  # noqa: ANN001
        self._record("create", (project_id, base_id, name, field_type))
        return _field_view(name=name, field_type=field_type)

    async def rename_metadata_field(self, project_id, field_id, *, name):  # noqa: ANN001
        self._record("rename", (project_id, field_id, name))
        return _field_view(name=name)

    async def delete_metadata_field(self, project_id, field_id):  # noqa: ANN001
        self._record("delete", (project_id, field_id))

    async def set_document_metadata(self, project_id, document_id, values):  # noqa: ANN001
        self._record("set_metadata", (project_id, document_id, values))
        applied = {key: value for key, value in values.items() if value is not None}
        return _document_view(doc_metadata=applied)


def _app(module: _FakeModule) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway.project_router)
    context = SimpleNamespace(project_id=_PROJECT_ID, request_id=_REQUEST_ID)
    app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: context
    app.dependency_overrides[gateway.require_project_knowledge_edit] = lambda: context
    app.state.knowledge_module = module
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_http_metadata_routes_round_trip() -> None:
    module = _FakeModule()
    async with _client(_app(module)) as client:
        listed = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/metadata-fields")
        created = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/metadata-fields",
            json={"name": "year", "field_type": "number"},
        )
        renamed = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/metadata-fields/{_FIELD_ID}",
            json={"name": "发布年份"},
        )
        deleted = await client.delete(f"/api/projects/{_PROJECT_ID}/knowledge/metadata-fields/{_FIELD_ID}")
        assigned = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/metadata",
            json={"values": {"部门": "工程", "year": 2026, "published_at": None}},
        )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["name"] == "部门"
    assert listed.json()["request_id"] == _REQUEST_ID
    assert created.status_code == 200
    assert created.json()["item"]["field_type"] == "number"
    assert renamed.status_code == 200
    assert renamed.json()["item"]["name"] == "发布年份"
    assert deleted.status_code == 200
    assert deleted.json() == {"request_id": _REQUEST_ID}
    assert assigned.status_code == 200
    assert assigned.json()["item"]["doc_metadata"] == {"部门": "工程", "year": 2026}

    verbs = [verb for verb, _payload in module.calls]
    assert verbs == ["list", "create", "rename", "delete", "set_metadata"]
    _, (_, _, values) = module.calls[4]
    assert values == {"部门": "工程", "year": 2026, "published_at": None}
    assert type(values["year"]) is int
    assert values["published_at"] is None


@pytest.mark.asyncio
async def test_http_metadata_routes_reject_bad_bodies_and_map_package_errors() -> None:
    module = _FakeModule()
    async with _client(_app(module)) as client:
        bad_type = await client.post(
            f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/metadata-fields",
            json={"name": "x", "field_type": "boolean"},
        )
        bad_values = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/metadata",
            json={"values": {"部门": ["列表"]}},
        )
        assert bad_type.status_code == 422
        assert bad_values.status_code == 422
        assert module.calls == []

        module.error = KnowledgeError(KNOWLEDGE_NOT_FOUND, "元数据字段不存在")
        missing = await client.delete(f"/api/projects/{_PROJECT_ID}/knowledge/metadata-fields/{_FIELD_ID}")
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == KNOWLEDGE_NOT_FOUND
        assert missing.json()["detail"]["request_id"] == _REQUEST_ID
