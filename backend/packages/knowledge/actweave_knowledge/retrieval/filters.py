"""Metadata-filter validation and the SQL scope predicates of retrieval.

Both recall and the final review build their predicates through
``current_scope_filters`` so a scope change mid-search can never leak past the
review. Custom conditions read ``knowledge_documents.doc_metadata``; builtin
conditions read the document authority columns.
"""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

from sqlalchemy import Numeric, case, cast, func, literal, null

from ..contracts import (
    KNOWLEDGE_BUILTIN_FILTER_FIELD_TYPES,
    KNOWLEDGE_FILTER_OPERATORS_BY_TYPE,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MAX_METADATA_FILTERS,
    KNOWLEDGE_MAX_METADATA_NAME_LENGTH,
    KNOWLEDGE_MAX_METADATA_STRING_LENGTH,
    KnowledgeError,
    KnowledgeMetadataFilter,
)
from ..persistence.models import KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeSegmentRow

__all__ = [
    "builtin_filter_conditions",
    "current_scope_filters",
    "metadata_filter_conditions",
    "validated_metadata_filters",
]


def _invalid(message: str) -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_INVALID_REQUEST, message)


def validated_metadata_filters(
    filters: tuple[KnowledgeMetadataFilter, ...] | None,
) -> tuple[KnowledgeMetadataFilter, ...]:
    """Bound and type-check manual metadata conditions (AND semantics).

    Field names are not resolved against definitions here: a condition on a
    name no targeted base defines simply matches no document of that base,
    mirroring the "missing key never matches" rule.
    """

    if filters is None:
        return ()
    if not isinstance(filters, (tuple, list)):
        raise _invalid("metadata_filters 必须是条件数组")
    if len(filters) > KNOWLEDGE_MAX_METADATA_FILTERS:
        raise _invalid(f"metadata_filters 最多 {KNOWLEDGE_MAX_METADATA_FILTERS} 个条件")
    validated: list[KnowledgeMetadataFilter] = []
    for item in filters:
        name = item.name.strip() if isinstance(item.name, str) else ""
        if not name or len(name) > KNOWLEDGE_MAX_METADATA_NAME_LENGTH:
            raise _invalid(f"过滤条件的 name 必须是 1-{KNOWLEDGE_MAX_METADATA_NAME_LENGTH} 个字符的非空文本")
        if item.field_kind not in ("custom", "builtin"):
            raise _invalid("过滤条件的 field_kind 只能是 custom 或 builtin")
        if item.operator not in ("eq", "contains", "gte", "lte"):
            raise _invalid("过滤条件的 operator 只能是 eq、contains、gte 或 lte")
        value = item.value
        if item.field_kind == "builtin":
            # Builtin fields are a frozen vocabulary with known types, so a
            # condition that could never match is a client error, not a
            # silent non-match.
            field_type = KNOWLEDGE_BUILTIN_FILTER_FIELD_TYPES.get(name)
            if field_type is None:
                raise _invalid(f"未知的内建过滤字段 {name}")
            if item.operator not in KNOWLEDGE_FILTER_OPERATORS_BY_TYPE[field_type]:
                raise _invalid(f"内建字段 {name} 不支持 {item.operator} 条件")
            if field_type == "string":
                if not isinstance(value, str) or not 1 <= len(value) <= KNOWLEDGE_MAX_METADATA_STRING_LENGTH:
                    raise _invalid(f"内建字段 {name} 的 value 必须是字符串")
            elif type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
                raise _invalid(f"内建字段 {name} 的 value 必须是有限数字（epoch 秒）")
        elif item.operator == "contains":
            if not isinstance(value, str) or not 1 <= len(value) <= KNOWLEDGE_MAX_METADATA_STRING_LENGTH:
                raise _invalid("contains 条件的 value 必须是非空字符串")
        elif item.operator in ("gte", "lte"):
            if type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
                raise _invalid(f"{item.operator} 条件的 value 必须是有限数字")
        elif isinstance(value, str):
            if len(value) > KNOWLEDGE_MAX_METADATA_STRING_LENGTH:
                raise _invalid(f"eq 条件的字符串 value 最多 {KNOWLEDGE_MAX_METADATA_STRING_LENGTH} 个字符")
        elif type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
            raise _invalid("eq 条件的 value 必须是字符串或有限数字")
        validated.append(KnowledgeMetadataFilter(name=name, operator=item.operator, value=value, field_kind=item.field_kind))
    return tuple(validated)


def builtin_filter_conditions(item: KnowledgeMetadataFilter) -> tuple[Any, ...]:
    """One builtin condition against the live document authority columns.

    ``document_name`` reads the display name, ``uploaded_at`` compares
    ``created_at`` as epoch seconds, ``file_type`` derives the lowercased
    original-file extension (no extension never matches), and
    ``source_type`` is the fixed ingestion channel ``file_upload``.
    """

    if item.name == "document_name":
        if item.operator == "eq":
            return (KnowledgeDocumentRow.name == item.value,)
        return (func.strpos(KnowledgeDocumentRow.name, item.value) > 0,)
    if item.name == "uploaded_at":
        epoch = func.extract("epoch", KnowledgeDocumentRow.created_at)
        if item.operator == "eq":
            return (epoch == item.value,)
        if item.operator == "gte":
            return (epoch >= item.value,)
        return (epoch <= item.value,)
    if item.name == "file_type":
        extension = func.lower(func.substring(KnowledgeDocumentRow.original_name, r"\.([^.]+)$"))
        needle = str(item.value).lower()
        if item.operator == "eq":
            return (extension == needle,)
        return (func.strpos(extension, needle) > 0,)
    # source_type: every stored document came through file upload.
    if item.operator == "eq":
        return (literal("file_upload") == item.value,)
    return (func.strpos(literal("file_upload"), item.value) > 0,)


def metadata_filter_conditions(filters: tuple[KnowledgeMetadataFilter, ...]) -> tuple[Any, ...]:
    """Translate validated conditions into document-row SQL predicates.

    Custom ``eq`` uses GIN-indexable JSONB containment (type-exact);
    ``contains`` and the range operators guard on ``jsonb_typeof`` first —
    inside CASE so a string value can never reach the numeric cast — making
    a mismatched type a non-match instead of a query error. Builtin
    conditions read authority columns instead of ``doc_metadata``. Both
    recall and the final review build their predicates through here, so a
    scope change mid-search can never leak past the review.
    """

    conditions: list[Any] = []
    for item in filters:
        if item.field_kind == "builtin":
            conditions.extend(builtin_filter_conditions(item))
            continue
        value_json = KnowledgeDocumentRow.doc_metadata[item.name]
        if item.operator == "eq":
            conditions.append(KnowledgeDocumentRow.doc_metadata.contains(func.jsonb_build_object(item.name, item.value)))
        elif item.operator == "contains":
            conditions.append(func.jsonb_typeof(value_json) == "string")
            conditions.append(func.strpos(value_json.astext, item.value) > 0)
        else:
            numeric_value = case(
                (func.jsonb_typeof(value_json) == "number", cast(value_json.astext, Numeric)),
                else_=null(),
            )
            if item.operator == "gte":
                conditions.append(numeric_value >= item.value)
            else:
                conditions.append(numeric_value <= item.value)
    return tuple(conditions)


def current_scope_filters(
    project_id: UUID,
    metadata_filters: tuple[KnowledgeMetadataFilter, ...],
) -> tuple[Any, ...]:
    """Rows currently inside retrieval scope; recall and the final review share it.

    Governance switches: a disabled document or segment keeps its vectors but
    never enters recall (nor Agent citations). Manual metadata conditions AND
    onto every path, so a non-matching document neither reaches the reranker
    nor survives the final review.
    """

    return (
        KnowledgeBaseRow.project_id == project_id,
        KnowledgeBaseRow.status == "active",
        KnowledgeDocumentRow.status == "ready",
        KnowledgeDocumentRow.enabled.is_(True),
        KnowledgeSegmentRow.enabled.is_(True),
        KnowledgeSegmentRow.document_version == KnowledgeDocumentRow.version,
        *metadata_filter_conditions(metadata_filters),
    )
