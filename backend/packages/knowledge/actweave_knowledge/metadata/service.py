"""Metadata field definitions plus per-document metadata assignment.

Fields are defined per Knowledge Base (string/number/time); document values
live in ``knowledge_documents.doc_metadata`` keyed by the field NAME. Rename
and delete therefore rewrite the affected documents' keys in the same
transaction, so a filter never sees a key without a live definition.
"""

from __future__ import annotations

import logging
import math
import sys
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import String, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..authority import KnowledgeProjectAuthority, revalidate_project_authority
from ..contracts import (
    KNOWLEDGE_BUILTIN_FILTER_FIELD_TYPES,
    KNOWLEDGE_FILTER_OPERATORS_BY_TYPE,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MAX_BATCH_METADATA_DOCUMENTS,
    KNOWLEDGE_MAX_BATCH_METADATA_FIELDS,
    KNOWLEDGE_MAX_FILTER_DISCOVERY_BASES,
    KNOWLEDGE_MAX_METADATA_FIELDS_PER_BASE,
    KNOWLEDGE_MAX_METADATA_NAME_LENGTH,
    KNOWLEDGE_MAX_METADATA_STRING_LENGTH,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeBaseFilterFields,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeFilterFieldView,
    KnowledgeMetadataBatchPatch,
    KnowledgeMetadataFieldType,
    KnowledgeMetadataFieldView,
)
from ..documents.service import document_view
from ..persistence.derivations import document_delete_error_expression
from ..persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeMetadataFieldRow,
)

logger = logging.getLogger(__name__)

_FIELD_TYPES = ("string", "number", "time")


def _invalid(message: str) -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_INVALID_REQUEST, message)


def _storage_unavailable() -> KnowledgeError:
    """Public storage error; logs the database exception being mapped, if any."""

    if sys.exc_info()[0] is not None:
        logger.warning("knowledge database operation failed", exc_info=True)
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "Knowledge 存储暂时不可用")


def _validated_field_name(value: str) -> str:
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned or len(cleaned) > KNOWLEDGE_MAX_METADATA_NAME_LENGTH:
        raise _invalid(f"name 必须是 1-{KNOWLEDGE_MAX_METADATA_NAME_LENGTH} 个字符的非空文本")
    return cleaned


def validated_metadata_value(name: str, field_type: str, value: Any) -> Any:
    """Type-check one metadata value against its field definition.

    ``string`` accepts bounded text; ``number`` and ``time`` accept finite
    JSON numbers (``time`` is epoch seconds). Booleans are rejected because
    Python treats them as ints.
    """

    if field_type == "string":
        if not isinstance(value, str):
            raise _invalid(f"元数据字段 {name} 需要字符串值")
        if len(value) > KNOWLEDGE_MAX_METADATA_STRING_LENGTH:
            raise _invalid(f"元数据字段 {name} 的值最多 {KNOWLEDGE_MAX_METADATA_STRING_LENGTH} 个字符")
        return value
    label = "数字" if field_type == "number" else "epoch 秒时间戳"
    if type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
        raise _invalid(f"元数据字段 {name} 需要{label}（JSON 数字）")
    return value


def _field_view(row: KnowledgeMetadataFieldRow) -> KnowledgeMetadataFieldView:
    return KnowledgeMetadataFieldView(
        id=row.id,
        knowledge_base_id=row.knowledge_base_id,
        name=row.name,
        field_type=row.field_type,  # type: ignore[arg-type]
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# Fixed read-only projections of document authority columns; dict order is
# the contract order of KNOWLEDGE_BUILTIN_FILTER_FIELDS.
_BUILTIN_FILTER_FIELD_VIEWS: tuple[KnowledgeFilterFieldView, ...] = tuple(
    KnowledgeFilterFieldView(
        kind="builtin",
        name=name,
        field_type=field_type,
        operators=KNOWLEDGE_FILTER_OPERATORS_BY_TYPE[field_type],
        writable=False,
    )
    for name, field_type in KNOWLEDGE_BUILTIN_FILTER_FIELD_TYPES.items()
)


def _custom_filter_field_view(row: KnowledgeMetadataFieldRow) -> KnowledgeFilterFieldView:
    return KnowledgeFilterFieldView(
        kind="custom",
        name=row.name,
        field_type=row.field_type,  # type: ignore[arg-type]
        operators=KNOWLEDGE_FILTER_OPERATORS_BY_TYPE[row.field_type],  # type: ignore[index]
        writable=True,
    )


def _merged_metadata(
    merged: dict[str, Any],
    values: dict[str, Any],
    field_types: dict[str, str],
) -> dict[str, Any]:
    """Merge one patch into a metadata dict under the base's definitions.

    Keys resolve against custom definitions only: a builtin name without a
    same-named custom field is read-only by construction, never writable.
    ``None`` removes the key; other values must match the field type.
    """

    for key, value in values.items():
        field_type = field_types.get(key) if isinstance(key, str) else None
        if field_type is None:
            if isinstance(key, str) and key in KNOWLEDGE_BUILTIN_FILTER_FIELD_TYPES:
                raise _invalid(f"{key} 是只读内建字段，不能通过元数据赋值写入")
            raise _invalid(f"元数据字段 {key} 未定义")
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = validated_metadata_value(key, field_type, value)
    return merged


class KnowledgeMetadataService:
    """Field-definition CRUD and document metadata assignment for one project."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_metadata_fields(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> list[KnowledgeMetadataFieldView]:
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                base_exists = await session.scalar(select(KnowledgeBaseRow.id).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id))
                if base_exists is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                rows = (
                    await session.scalars(
                        select(KnowledgeMetadataFieldRow)
                        .where(KnowledgeMetadataFieldRow.project_id == project_id, KnowledgeMetadataFieldRow.knowledge_base_id == base_id)
                        .order_by(KnowledgeMetadataFieldRow.created_at, KnowledgeMetadataFieldRow.id)
                    )
                ).all()
                return [_field_view(row) for row in rows]
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def list_filter_fields(
        self,
        project_id: UUID,
        base_ids: list[UUID] | tuple[UUID, ...] | None = None,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> list[KnowledgeBaseFilterFields]:
        """Discover the filterable fields (builtin then custom) per base.

        Returns definitions only — stable identity, type, allowed operators
        and writability — never values scanned from documents. The scope is
        the project's active bases, narrowed by ``base_ids``; a scope wider
        than ``KNOWLEDGE_MAX_FILTER_DISCOVERY_BASES`` refuses explicitly
        instead of truncating silently.
        """

        requested: list[UUID] | None = None
        if base_ids is not None:
            if not isinstance(base_ids, (list, tuple)) or not base_ids:
                raise _invalid("base_ids 必须是非空的 Knowledge Base ID 数组")
            if len(set(base_ids)) != len(base_ids):
                raise _invalid("base_ids 不能包含重复项")
            if len(base_ids) > KNOWLEDGE_MAX_FILTER_DISCOVERY_BASES:
                raise _invalid(f"字段发现一次最多 {KNOWLEDGE_MAX_FILTER_DISCOVERY_BASES} 个库，请用 base_ids 缩小范围")
            requested = list(base_ids)
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                statement = select(KnowledgeBaseRow.id, KnowledgeBaseRow.name, KnowledgeBaseRow.description).where(
                    KnowledgeBaseRow.project_id == project_id,
                    KnowledgeBaseRow.status == "active",
                )
                if requested is not None:
                    statement = statement.where(KnowledgeBaseRow.id.in_(requested))
                else:
                    statement = statement.order_by(KnowledgeBaseRow.created_at, KnowledgeBaseRow.id)
                found_rows = (await session.execute(statement)).all()
                names = {row.id: (row.name, row.description) for row in found_rows}
                found = [row.id for row in found_rows]
                if requested is not None:
                    if set(found) != set(requested):
                        raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                    ordered = requested
                else:
                    if len(found) > KNOWLEDGE_MAX_FILTER_DISCOVERY_BASES:
                        raise _invalid(f"项目内活跃库超过 {KNOWLEDGE_MAX_FILTER_DISCOVERY_BASES} 个，请用 base_ids 缩小发现范围")
                    ordered = found
                custom_by_base: dict[UUID, list[KnowledgeFilterFieldView]] = {base_id: [] for base_id in ordered}
                if ordered:
                    rows = (await session.scalars(select(KnowledgeMetadataFieldRow).where(KnowledgeMetadataFieldRow.knowledge_base_id.in_(ordered)).order_by(KnowledgeMetadataFieldRow.created_at, KnowledgeMetadataFieldRow.id))).all()
                    for row in rows:
                        custom_by_base[row.knowledge_base_id].append(_custom_filter_field_view(row))
                return [
                    KnowledgeBaseFilterFields(
                        knowledge_base_id=base_id,
                        knowledge_base_name=names[base_id][0],
                        description=names[base_id][1],
                        fields=_BUILTIN_FILTER_FIELD_VIEWS + tuple(custom_by_base[base_id]),
                    )
                    for base_id in ordered
                ]
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def create_metadata_field(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        name: str,
        field_type: KnowledgeMetadataFieldType,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeMetadataFieldView:
        cleaned = _validated_field_name(name)
        if field_type not in _FIELD_TYPES:
            raise _invalid("field_type 只能是 string、number 或 time")
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                # The base lock serializes concurrent creates so the quota
                # check cannot be raced past.
                base = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id).with_for_update())
                if base is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
                if base.status == "deleting":
                    raise _invalid("Knowledge Base 正在删除，不能管理元数据字段")
                field_count = await session.scalar(select(func.count()).select_from(KnowledgeMetadataFieldRow).where(KnowledgeMetadataFieldRow.knowledge_base_id == base_id))
                if int(field_count or 0) >= KNOWLEDGE_MAX_METADATA_FIELDS_PER_BASE:
                    raise KnowledgeError(
                        KNOWLEDGE_QUOTA_EXCEEDED,
                        f"元数据字段数量已达上限 {KNOWLEDGE_MAX_METADATA_FIELDS_PER_BASE}",
                    )
                row = KnowledgeMetadataFieldRow(
                    id=uuid4(),
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    name=cleaned,
                    field_type=field_type,
                )
                session.add(row)
                await session.flush()
                await session.refresh(row)
                return _field_view(row)
        except IntegrityError:
            raise KnowledgeError(KNOWLEDGE_NAME_CONFLICT, "同一 Knowledge Base 内已存在同名元数据字段") from None
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def rename_metadata_field(
        self,
        project_id: UUID,
        field_id: UUID,
        *,
        name: str,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeMetadataFieldView:
        """Rename the definition and rewrite the key across the base's documents."""

        cleaned = _validated_field_name(name)
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                row = await self._locked_field_row(session, project_id, field_id)
                old_name = row.name
                if cleaned != old_name:
                    row.name = cleaned
                    row.updated_at = func.now()  # type: ignore[assignment]
                    # Surfaces a name conflict before documents are rewritten.
                    await session.flush()
                    await self._lock_documents_with_key(session, row.knowledge_base_id, old_name)
                    old_key = literal(old_name, String)
                    await session.execute(
                        update(KnowledgeDocumentRow)
                        .where(
                            KnowledgeDocumentRow.knowledge_base_id == row.knowledge_base_id,
                            KnowledgeDocumentRow.doc_metadata.has_key(old_name),
                        )
                        .values(
                            doc_metadata=KnowledgeDocumentRow.doc_metadata.op("-", return_type=JSONB)(old_key).op("||", return_type=JSONB)(
                                func.jsonb_build_object(cleaned, KnowledgeDocumentRow.doc_metadata.op("->", return_type=JSONB)(old_key))
                            ),
                        )
                    )
                await session.flush()
                await session.refresh(row)
                return _field_view(row)
        except IntegrityError:
            raise KnowledgeError(KNOWLEDGE_NAME_CONFLICT, "同一 Knowledge Base 内已存在同名元数据字段") from None
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def delete_metadata_field(
        self,
        project_id: UUID,
        field_id: UUID,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> None:
        """Drop the definition and strip the key from the base's documents."""

        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                row = await self._locked_field_row(session, project_id, field_id)
                await self._lock_documents_with_key(session, row.knowledge_base_id, row.name)
                await session.execute(
                    update(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.knowledge_base_id == row.knowledge_base_id,
                        KnowledgeDocumentRow.doc_metadata.has_key(row.name),
                    )
                    .values(doc_metadata=KnowledgeDocumentRow.doc_metadata.op("-", return_type=JSONB)(literal(row.name, String)))
                )
                await session.delete(row)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    @staticmethod
    async def _lock_documents_with_key(session: AsyncSession, base_id: UUID, name: str) -> None:
        """Lock the documents a bulk key rewrite will touch, in UUID order.

        Batch document governance (enable/disable/delete) locks rows ordered
        by UUID without taking the base entry lock, so letting the rewrite's
        ``UPDATE`` acquire row locks in scan order could form a lock cycle
        with it. Membership cannot change underneath: every ``doc_metadata``
        writer serializes on the base lock this transaction already holds.
        """

        await session.execute(
            select(KnowledgeDocumentRow.id)
            .where(
                KnowledgeDocumentRow.knowledge_base_id == base_id,
                KnowledgeDocumentRow.doc_metadata.has_key(name),
            )
            .order_by(KnowledgeDocumentRow.id)
            .with_for_update()
        )

    @staticmethod
    async def _locked_base_row(session: AsyncSession, project_id: UUID, base_id: UUID) -> KnowledgeBaseRow:
        """Take the shared per-base entry lock for structure and assignment.

        Field create/rename/delete and single/batch metadata assignment all
        serialize on the base row before touching definitions or documents,
        so a rename can never interleave with an assignment and resurrect
        the old key.
        """

        base = await session.scalar(select(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id, KnowledgeBaseRow.id == base_id).with_for_update())
        if base is None:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Knowledge Base 不存在")
        return base

    async def _locked_field_row(self, session: AsyncSession, project_id: UUID, field_id: UUID) -> KnowledgeMetadataFieldRow:
        """Lock base first (unified order), then the field row itself."""

        base_id = await session.scalar(select(KnowledgeMetadataFieldRow.knowledge_base_id).where(KnowledgeMetadataFieldRow.project_id == project_id, KnowledgeMetadataFieldRow.id == field_id))
        if base_id is None:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "元数据字段不存在")
        await self._locked_base_row(session, project_id, base_id)
        # Re-read under the base lock: the definition may have been deleted
        # between the unlocked peek and lock acquisition.
        row = await session.scalar(select(KnowledgeMetadataFieldRow).where(KnowledgeMetadataFieldRow.project_id == project_id, KnowledgeMetadataFieldRow.id == field_id).with_for_update())
        if row is None:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "元数据字段不存在")
        return row

    async def set_document_metadata(
        self,
        project_id: UUID,
        document_id: UUID,
        values: dict[str, Any],
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeDocumentView:
        """Merge metadata values into one document (``None`` removes the key).

        Keys must exactly match a defined field name of the document's base and
        values must match the field type; unknown keys reject the whole call.
        """

        if not isinstance(values, dict):
            raise _invalid("values 必须是对象")
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                base_id = await session.scalar(select(KnowledgeDocumentRow.knowledge_base_id).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id))
                if base_id is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                await self._locked_base_row(session, project_id, base_id)
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                if row.status == "deleting":
                    raise _invalid("删除中的文档不支持编辑元数据")
                field_types = await self._field_types(session, row.knowledge_base_id)
                merged = _merged_metadata(dict(row.doc_metadata), values, field_types)
                row.doc_metadata = merged
                row.updated_at = func.now()  # type: ignore[assignment]
                await session.flush()
                await session.refresh(row)
                delete_error = await session.scalar(select(document_delete_error_expression(KnowledgeDocumentRow.id)).where(KnowledgeDocumentRow.id == row.id))
                return document_view(row, delete_error=delete_error)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def set_documents_metadata(
        self,
        project_id: UUID,
        base_id: UUID,
        patch: KnowledgeMetadataBatchPatch,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> list[KnowledgeDocumentView]:
        """Apply one bounded common patch to documents of one base, all-or-nothing.

        Untouched keys stay, ``None`` removes the key, and builtin field
        names are never addressable by writes. Locking follows the unified
        order — base, then definitions, then documents in UUID order — and
        any missing document, deleting row, unknown key or type conflict
        rolls the whole batch back. Metadata never changes content: no task
        is queued and no version moves.
        """

        document_ids = patch.document_ids
        if not isinstance(document_ids, (tuple, list)) or not document_ids:
            raise _invalid("document_ids 不能为空")
        if len(document_ids) > KNOWLEDGE_MAX_BATCH_METADATA_DOCUMENTS:
            raise _invalid(f"一次批量赋值最多 {KNOWLEDGE_MAX_BATCH_METADATA_DOCUMENTS} 个文档")
        if len(set(document_ids)) != len(document_ids):
            raise _invalid("document_ids 不能包含重复项")
        values = patch.values
        if not isinstance(values, dict) or not values:
            raise _invalid("values 必须是至少包含一个字段的对象")
        if len(values) > KNOWLEDGE_MAX_BATCH_METADATA_FIELDS:
            raise _invalid(f"一次批量赋值最多 {KNOWLEDGE_MAX_BATCH_METADATA_FIELDS} 个字段")
        try:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=project_id,
                )
                base = await self._locked_base_row(session, project_id, base_id)
                if base.status == "deleting":
                    raise _invalid("Knowledge Base 正在删除，不能编辑元数据")
                field_types = await self._field_types(session, base_id)
                # Surface key/type conflicts before any document is touched.
                _merged_metadata({}, values, field_types)
                rows = (
                    await session.scalars(
                        select(KnowledgeDocumentRow)
                        .where(
                            KnowledgeDocumentRow.project_id == project_id,
                            KnowledgeDocumentRow.knowledge_base_id == base_id,
                            KnowledgeDocumentRow.id.in_(document_ids),
                        )
                        .order_by(KnowledgeDocumentRow.id)
                        .with_for_update()
                    )
                ).all()
                if len(rows) != len(document_ids):
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "部分 Document 不存在或不属于该 Knowledge Base")
                for row in rows:
                    if row.status == "deleting":
                        raise _invalid("删除中的文档不支持编辑元数据")
                for row in rows:
                    row.doc_metadata = _merged_metadata(dict(row.doc_metadata), values, field_types)
                    row.updated_at = func.now()  # type: ignore[assignment]
                await session.flush()
                for row in rows:
                    await session.refresh(row)
                delete_errors = {
                    row_id: delete_error
                    for row_id, delete_error in (
                        await session.execute(
                            select(
                                KnowledgeDocumentRow.id,
                                document_delete_error_expression(KnowledgeDocumentRow.id),
                            ).where(KnowledgeDocumentRow.id.in_(document_ids))
                        )
                    ).all()
                }
                by_id = {row.id: row for row in rows}
                return [document_view(by_id[document_id], delete_error=delete_errors.get(document_id)) for document_id in document_ids]
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    @staticmethod
    async def _field_types(session: AsyncSession, base_id: UUID) -> dict[str, str]:
        rows = (
            await session.scalars(
                select(KnowledgeMetadataFieldRow).where(
                    KnowledgeMetadataFieldRow.knowledge_base_id == base_id,
                )
            )
        ).all()
        return {field.name: field.field_type for field in rows}
