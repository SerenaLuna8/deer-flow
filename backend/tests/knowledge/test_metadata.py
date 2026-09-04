"""K4 gates: metadata field definitions, document metadata, and their routes.

Service tests run against the installed Schema V1 snapshot so the
case-insensitive unique index, the JSONB key rewrites on rename/delete, and
the quota lock are exercised for real. HTTP tests pin the new route contract
over ASGI with a recording fake module.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MAX_BATCH_METADATA_DOCUMENTS,
    KNOWLEDGE_MAX_BATCH_METADATA_FIELDS,
    KNOWLEDGE_MAX_FILTER_DISCOVERY_BASES,
    KNOWLEDGE_MAX_METADATA_FIELDS_PER_BASE,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeMetadataBatchPatch,
    KnowledgeMetadataFieldView,
)
from actweave_knowledge.metadata import KnowledgeMetadataService
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeMetadataFieldRow,
)
from fastapi import FastAPI
from registry_helpers import seed_registry_models
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.knowledge import gateway
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole

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
    """Project + registry embedding model + one active base. Returns (project_id, base_id)."""

    embedding_model_id, _ = await seed_registry_models(harness.factory)
    async with harness.factory() as session, session.begin():
        project_id = await _seed_project(session, uuid.uuid4().hex[:8])
        base = KnowledgeBaseRow(
            id=uuid.uuid4(),
            project_id=project_id,
            name=f"base-{uuid.uuid4().hex[:6]}",
            embedding_model_id=embedding_model_id,
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
async def test_field_names_are_unique_per_base_case_insensitively(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        await harness.service.create_metadata_field(project_id, base_id, name="Category", field_type="string")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.create_metadata_field(project_id, base_id, name="category", field_type="number")
        assert error.value.code == KNOWLEDGE_NAME_CONFLICT

        # The same name in another base is a separate namespace.
        sibling_model_id, _ = await seed_registry_models(harness.factory)
        async with harness.factory() as session, session.begin():
            sibling = KnowledgeBaseRow(
                id=uuid.uuid4(),
                project_id=project_id,
                name=f"base-{uuid.uuid4().hex[:6]}",
                embedding_model_id=sibling_model_id,
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
# T6: filter-field discovery (builtin + custom)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_field_discovery_scopes_to_requested_active_bases(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        other_project, other_base = await _seed_base(harness)
        sibling_model_id, _ = await seed_registry_models(harness.factory)
        async with harness.factory() as session, session.begin():
            second = KnowledgeBaseRow(
                id=uuid.uuid4(),
                project_id=project_id,
                name=f"base-{uuid.uuid4().hex[:6]}",
                embedding_model_id=sibling_model_id,
                status="active",
            )
            deleting = KnowledgeBaseRow(
                id=uuid.uuid4(),
                project_id=project_id,
                name=f"base-{uuid.uuid4().hex[:6]}",
                embedding_model_id=sibling_model_id,
                status="deleting",
            )
            session.add_all([second, deleting])

        # Omitted scope: every active base of this project, nothing else.
        discovered = await harness.service.list_filter_fields(project_id)
        assert {entry.knowledge_base_id for entry in discovered} == {base_id, second.id}

        # Explicit scope narrows to exactly the requested bases.
        narrowed = await harness.service.list_filter_fields(project_id, base_ids=[second.id])
        assert [entry.knowledge_base_id for entry in narrowed] == [second.id]

        # Unknown, deleting, and cross-project ids are a broken resource chain.
        for bad in (uuid.uuid4(), deleting.id, other_base):
            with pytest.raises(KnowledgeError) as error:
                await harness.service.list_filter_fields(project_id, base_ids=[base_id, bad])
            assert error.value.code == KNOWLEDGE_NOT_FOUND
        del other_project
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_filter_field_discovery_over_budget_requires_narrowing(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        model_id, _ = await seed_registry_models(harness.factory)
        async with harness.factory() as session, session.begin():
            for index in range(KNOWLEDGE_MAX_FILTER_DISCOVERY_BASES):
                session.add(
                    KnowledgeBaseRow(
                        id=uuid.uuid4(),
                        project_id=project_id,
                        name=f"extra-{index}-{uuid.uuid4().hex[:6]}",
                        embedding_model_id=model_id,
                        status="active",
                    )
                )

        # 21 active bases: the full-project scope must refuse, not truncate.
        with pytest.raises(KnowledgeError) as error:
            await harness.service.list_filter_fields(project_id)
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST
        assert "base_ids" in error.value.message

        # An explicit over-long list refuses the same way.
        async with harness.factory() as session:
            all_ids = list((await session.scalars(select(KnowledgeBaseRow.id).where(KnowledgeBaseRow.project_id == project_id))).all())
        with pytest.raises(KnowledgeError) as error:
            await harness.service.list_filter_fields(project_id, base_ids=all_ids)
        assert error.value.code == KNOWLEDGE_INVALID_REQUEST

        # Narrowing to one base works.
        narrowed = await harness.service.list_filter_fields(project_id, base_ids=[base_id])
        assert [entry.knowledge_base_id for entry in narrowed] == [base_id]
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_filter_field_discovery_revalidates_project_authority(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, _ = await _seed_base(harness)

        class _RevokedAuthority:
            def __init__(self) -> None:
                self.project_id = project_id
                self.actor_user_id = uuid.uuid4()

            async def revalidate(self, session) -> None:  # noqa: ANN001
                del session
                raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")

        with pytest.raises(KnowledgeError) as error:
            await harness.service.list_filter_fields(project_id, authority=_RevokedAuthority())  # type: ignore[arg-type]
        assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await harness.engine.dispose()


# ---------------------------------------------------------------------------
# T6: bounded batch metadata assignment
# ---------------------------------------------------------------------------


async def _seed_batch_documents(harness: _Harness, count: int = 3) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """Base with 部门(string)/year(number) fields plus ``count`` ready documents."""

    project_id, base_id = await _seed_base(harness)
    await harness.service.create_metadata_field(project_id, base_id, name="部门", field_type="string")
    await harness.service.create_metadata_field(project_id, base_id, name="year", field_type="number")
    document_ids: list[uuid.UUID] = []
    async with harness.factory() as session, session.begin():
        for index in range(count):
            document = _document_row(project_id, base_id, name=f"文档{index}")
            session.add(document)
            document_ids.append(document.id)
    return project_id, base_id, document_ids


@pytest.mark.asyncio
async def test_batch_metadata_patch_applies_common_patch_in_input_order(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_ids = await _seed_batch_documents(harness)
        first, second, bystander = document_ids
        await harness.service.set_document_metadata(project_id, first, {"部门": "旧部门", "year": 2000})
        await harness.service.set_document_metadata(project_id, second, {"year": 2001})
        await harness.service.set_document_metadata(project_id, bystander, {"部门": "保持"})

        views = await harness.service.set_documents_metadata(
            project_id,
            base_id,
            KnowledgeMetadataBatchPatch(document_ids=(second, first), values={"部门": "工程", "year": None}),
        )

        # Results come back in input order; untouched keys stay, null clears.
        assert [view.id for view in views] == [second, first]
        assert views[0].doc_metadata == {"部门": "工程"}
        assert views[1].doc_metadata == {"部门": "工程"}
        assert await _doc_metadata(harness, bystander) == {"部门": "保持"}
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_batch_metadata_patch_validates_bounds_and_rejects_builtin_names(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_ids = await _seed_batch_documents(harness)
        target = document_ids[0]

        cases: list[tuple[KnowledgeMetadataBatchPatch, str]] = [
            (KnowledgeMetadataBatchPatch(document_ids=(), values={"部门": "x"}), "document_ids"),
            (
                KnowledgeMetadataBatchPatch(
                    document_ids=tuple(uuid.uuid4() for _ in range(KNOWLEDGE_MAX_BATCH_METADATA_DOCUMENTS + 1)),
                    values={"部门": "x"},
                ),
                str(KNOWLEDGE_MAX_BATCH_METADATA_DOCUMENTS),
            ),
            (KnowledgeMetadataBatchPatch(document_ids=(target, target), values={"部门": "x"}), "重复"),
            (KnowledgeMetadataBatchPatch(document_ids=(target,), values={}), "values"),
            (
                KnowledgeMetadataBatchPatch(
                    document_ids=(target,),
                    values={f"f{index}": "x" for index in range(KNOWLEDGE_MAX_BATCH_METADATA_FIELDS + 1)},
                ),
                str(KNOWLEDGE_MAX_BATCH_METADATA_FIELDS),
            ),
            # Builtin names are not addressable by writes: there is no custom
            # definition behind them unless the project created one.
            (KnowledgeMetadataBatchPatch(document_ids=(target,), values={"uploaded_at": 1}), "内建"),
            (KnowledgeMetadataBatchPatch(document_ids=(target,), values={"未定义": "x"}), "未定义"),
            (KnowledgeMetadataBatchPatch(document_ids=(target,), values={"year": "2026"}), "数字"),
        ]
        for patch, fragment in cases:
            with pytest.raises(KnowledgeError) as error:
                await harness.service.set_documents_metadata(project_id, base_id, patch)
            assert error.value.code == KNOWLEDGE_INVALID_REQUEST, patch
            assert fragment in error.value.message, patch
        assert await _doc_metadata(harness, target) == {}

        # A custom field may reuse a builtin name; writes then address the
        # custom field, never the authority column.
        await harness.service.create_metadata_field(project_id, base_id, name="file_type", field_type="string")
        views = await harness.service.set_documents_metadata(
            project_id,
            base_id,
            KnowledgeMetadataBatchPatch(document_ids=(target,), values={"file_type": "内部规范"}),
        )
        assert views[0].doc_metadata == {"file_type": "内部规范"}
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_batch_metadata_patch_rolls_back_the_whole_batch_on_any_conflict(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_ids = await _seed_batch_documents(harness)
        healthy, other, _ = document_ids

        # A foreign-base document id poisons the whole batch.
        foreign_project, foreign_base = await _seed_base(harness)
        async with harness.factory() as session, session.begin():
            foreign_document = _document_row(foreign_project, foreign_base, name="别库")
            session.add(foreign_document)
            deleting_document = _document_row(project_id, base_id, name="删除中", status="deleting")
            session.add(deleting_document)

        cases: list[tuple[tuple[uuid.UUID, ...], str]] = [
            ((healthy, uuid.uuid4()), KNOWLEDGE_NOT_FOUND),
            ((healthy, foreign_document.id), KNOWLEDGE_NOT_FOUND),
            ((healthy, deleting_document.id), KNOWLEDGE_INVALID_REQUEST),
        ]
        for ids, expected_code in cases:
            with pytest.raises(KnowledgeError) as error:
                await harness.service.set_documents_metadata(
                    project_id,
                    base_id,
                    KnowledgeMetadataBatchPatch(document_ids=ids, values={"部门": "不应落地"}),
                )
            assert error.value.code == expected_code, ids
            # The healthy document must not keep any partial write.
            assert await _doc_metadata(harness, healthy) == {}, ids
        assert await _doc_metadata(harness, foreign_document.id) == {}
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_batch_metadata_patch_never_queues_tasks_or_touches_content(postgres_database_url: str) -> None:
    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_ids = await _seed_batch_documents(harness)
        target = document_ids[0]
        async with harness.factory() as session:
            version_before = await session.scalar(select(KnowledgeDocumentRow.version).where(KnowledgeDocumentRow.id == target))

        await harness.service.set_documents_metadata(
            project_id,
            base_id,
            KnowledgeMetadataBatchPatch(document_ids=(target,), values={"部门": "工程"}),
        )

        async with harness.factory() as session:
            task_count = await session.scalar(text("SELECT count(*) FROM knowledge_tasks"))
            version_after = await session.scalar(select(KnowledgeDocumentRow.version).where(KnowledgeDocumentRow.id == target))
        # Metadata assignment is not a content change: no re-embed task, no
        # new content generation.
        assert task_count == 0
        assert version_after == version_before
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_rename_and_assignment_never_resurrect_old_keys(postgres_database_url: str) -> None:
    """Field mutations and assignments share one lock order (base first).

    Whatever interleaving wins, a document may never keep a metadata key
    that has no live field definition — the old name must not flow back
    after a rename.
    """

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id, document_ids = await _seed_batch_documents(harness)
        listed = await harness.service.list_metadata_fields(project_id, base_id)
        field_id = next(field.id for field in listed if field.name == "部门")

        async def _assign(round_index: int) -> None:
            try:
                await harness.service.set_documents_metadata(
                    project_id,
                    base_id,
                    KnowledgeMetadataBatchPatch(
                        document_ids=tuple(document_ids),
                        values={"部门": f"值{round_index}"},
                    ),
                )
                await harness.service.set_document_metadata(project_id, document_ids[0], {"部门": f"单写{round_index}"})
            except KnowledgeError as error:
                # The rename may win the race; the stale name is then simply
                # undefined for this batch — a full, clean rejection.
                assert error.code == KNOWLEDGE_INVALID_REQUEST

        async def _flip(round_index: int) -> None:
            new_name = "部门2" if round_index % 2 == 0 else "部门"
            await harness.service.rename_metadata_field(project_id, field_id, name=new_name)

        for round_index in range(4):
            await asyncio.gather(_assign(round_index), _flip(round_index))
            async with harness.factory() as session:
                defined = set((await session.scalars(select(KnowledgeMetadataFieldRow.name).where(KnowledgeMetadataFieldRow.knowledge_base_id == base_id))).all())
                rows = (await session.scalars(select(KnowledgeDocumentRow.doc_metadata).where(KnowledgeDocumentRow.knowledge_base_id == base_id))).all()
            for metadata in rows:
                assert set(metadata).issubset(defined), (metadata, defined)
    finally:
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_field_rewrite_and_batch_governance_share_document_lock_order(postgres_database_url: str) -> None:
    """A rename's bulk key rewrite takes document locks in UUID order.

    Batch enable/disable/delete locks document rows ordered by UUID without
    the base entry lock, so a scan-ordered bulk ``UPDATE`` could form a lock
    cycle with it. Reproduce that exact interleaving — the governance side
    holds the smaller UUID and requests the larger one while the rename is
    mid-flight — and require the rename to queue behind the held row instead
    of deadlocking either side.
    """

    harness = await _harness(postgres_database_url)
    try:
        project_id, base_id = await _seed_base(harness)
        created = await harness.service.create_metadata_field(project_id, base_id, name="部门", field_type="string")
        first_id, second_id = sorted((uuid.uuid4(), uuid.uuid4()))
        async with harness.factory() as session, session.begin():
            # Insert the larger UUID first: a scan-ordered lock walk would be
            # the exact reverse of the UUID order used by batch governance.
            for document_id in (second_id, first_id):
                row = _document_row(project_id, base_id, name=f"doc-{document_id.hex[:6]}")
                row.id = document_id
                row.doc_metadata = {"部门": "工程"}
                session.add(row)

        governance = harness.factory()
        rename: asyncio.Task | None = None
        try:
            # Batch governance's first lock: the smaller UUID.
            await governance.execute(select(KnowledgeDocumentRow.id).where(KnowledgeDocumentRow.id == first_id).with_for_update())
            rename = asyncio.create_task(harness.service.rename_metadata_field(project_id, created.id, name="部门2"))
            for _ in range(200):
                async with harness.factory() as watch:
                    blocked = await watch.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query ILIKE '%knowledge_documents%'"))
                if blocked:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("rename never queued behind the held document row")
            # Batch governance's second lock: the larger UUID. With opposite
            # lock orders this is the deadlock edge; with the unified order it
            # is granted immediately because the rename holds no document row.
            await governance.execute(select(KnowledgeDocumentRow.id).where(KnowledgeDocumentRow.id == second_id).with_for_update())
            await governance.rollback()
        finally:
            await governance.close()

        renamed = await asyncio.wait_for(rename, timeout=15)
        assert renamed.name == "部门2"
        for document_id in (first_id, second_id):
            assert await _doc_metadata(harness, document_id) == {"部门2": "工程"}
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

    async def list_metadata_fields(self, project_id, base_id, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("list", (project_id, base_id))
        return [_field_view()]

    async def create_metadata_field(self, project_id, base_id, *, name, field_type, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("create", (project_id, base_id, name, field_type))
        return _field_view(name=name, field_type=field_type)

    async def rename_metadata_field(self, project_id, field_id, *, name, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("rename", (project_id, field_id, name))
        return _field_view(name=name)

    async def delete_metadata_field(self, project_id, field_id, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("delete", (project_id, field_id))

    async def set_document_metadata(self, project_id, document_id, values, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("set_metadata", (project_id, document_id, values))
        applied = {key: value for key, value in values.items() if value is not None}
        return _document_view(doc_metadata=applied)

    async def list_filter_fields(self, project_id, base_ids=None, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("filter_fields", (project_id, base_ids))
        from actweave_knowledge import KnowledgeBaseFilterFields, KnowledgeFilterFieldView

        return [
            KnowledgeBaseFilterFields(
                knowledge_base_id=_BASE_ID,
                fields=(
                    KnowledgeFilterFieldView(kind="builtin", name="document_name", field_type="string", operators=("eq", "contains"), writable=False),
                    KnowledgeFilterFieldView(kind="custom", name="部门", field_type="string", operators=("eq", "contains"), writable=True),
                ),
            )
        ]

    async def set_documents_metadata(self, project_id, base_id, patch, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self._record("batch_metadata", (project_id, base_id, patch))
        applied = {key: value for key, value in patch.values.items() if value is not None}
        return [_document_view(id=document_id, doc_metadata=applied) for document_id in patch.document_ids]


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
