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
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MAX_METADATA_FIELDS_PER_BASE,
    KNOWLEDGE_MAX_METADATA_NAME_LENGTH,
    KNOWLEDGE_MAX_METADATA_STRING_LENGTH,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeDocumentView,
    KnowledgeError,
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
                row = await session.scalar(select(KnowledgeMetadataFieldRow).where(KnowledgeMetadataFieldRow.project_id == project_id, KnowledgeMetadataFieldRow.id == field_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "元数据字段不存在")
                old_name = row.name
                if cleaned != old_name:
                    row.name = cleaned
                    row.updated_at = func.now()  # type: ignore[assignment]
                    # Surfaces a name conflict before documents are rewritten.
                    await session.flush()
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
                row = await session.scalar(select(KnowledgeMetadataFieldRow).where(KnowledgeMetadataFieldRow.project_id == project_id, KnowledgeMetadataFieldRow.id == field_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "元数据字段不存在")
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
                row = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id, KnowledgeDocumentRow.id == document_id).with_for_update())
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "Document 不存在")
                if row.status == "deleting":
                    raise _invalid("删除中的文档不支持编辑元数据")
                fields = (
                    await session.scalars(
                        select(KnowledgeMetadataFieldRow).where(
                            KnowledgeMetadataFieldRow.knowledge_base_id == row.knowledge_base_id,
                        )
                    )
                ).all()
                field_types = {field.name: field.field_type for field in fields}
                merged = dict(row.doc_metadata)
                for key, value in values.items():
                    field_type = field_types.get(key) if isinstance(key, str) else None
                    if field_type is None:
                        raise _invalid(f"元数据字段 {key} 未定义")
                    if value is None:
                        merged.pop(key, None)
                    else:
                        merged[key] = validated_metadata_value(key, field_type, value)
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
