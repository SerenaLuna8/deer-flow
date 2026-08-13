from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal, Protocol, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import and_, case, column, exists, func, literal, or_, select, true, tuple_, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.errors import (
    AssetForbidden,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.mcp_tool_inventory_repository import mcp_grant_closure_digest
from app.shared_assets.skill_design_generation import (
    SkillBuilderDependencySnapshot,
    SkillBuilderMcpToolDependency,
    SkillBuilderSkillDependency,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    CredentialGrantRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectMcpToolInventoryRow,
    ProjectSystemMcpBindingRow,
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)

MAX_AUTHORING_SEARCH_QUERY_CHARS = 200
MAX_AUTHORING_SEARCH_RESULTS = 20
MAX_AUTHORING_SKILL_DESCRIPTION_CHARS = 2_000
MAX_AUTHORING_MCP_DESCRIPTION_CHARS = 2_000
MAX_AUTHORING_SKILL_TEXT_BYTES = 1024 * 1024
MAX_AUTHORING_MCP_SCAN_PER_PAGE = 256
MAX_AUTHORING_CURSOR_CHARS = 2_048

_TOOL_NAME_PATTERN = r"^[A-Za-z0-9_-]+$"
_HEX_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_CURSOR_PATTERN = r"^[A-Za-z0-9_-]+$"
_CURSOR_VERSION = 1
_SKILL_CURSOR_KIND = "skills"
_MCP_CURSOR_KIND = "mcp_tools"
_MCP_INVENTORY_ERROR_CODES = frozenset(
    {
        "mcp_discovery_unavailable",
        "mcp_catalog_invalid",
    }
)

_SearchQuery = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=0,
        max_length=MAX_AUTHORING_SEARCH_QUERY_CHARS,
    ),
]
_Cursor = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=MAX_AUTHORING_CURSOR_CHARS,
        pattern=_CURSOR_PATTERN,
    ),
]
_CanonicalPath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=1024),
]
_StrictLimit = Annotated[
    int,
    Field(strict=True, ge=1, le=MAX_AUTHORING_SEARCH_RESULTS),
]


class _StrictDto(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillCatalogSearch(_StrictDto):
    query: _SearchQuery
    limit: _StrictLimit = 10
    cursor: _Cursor | None = None


class SkillTextRead(_StrictDto):
    skill_id: uuid.UUID
    version_id: uuid.UUID
    path: _CanonicalPath

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _canonical_skill_path(value)


class McpToolCatalogSearch(_StrictDto):
    query: _SearchQuery
    limit: _StrictLimit = 10
    cursor: _Cursor | None = None


class McpToolMetadataInspect(_StrictDto):
    mcp_server_id: uuid.UUID
    version_id: uuid.UUID
    tool_name: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=1,
            max_length=255,
            pattern=_TOOL_NAME_PATTERN,
        ),
    ]


class SkillCatalogItem(_StrictDto):
    kind: Literal["skill"] = "skill"
    scope: Literal["project", "system"]
    skill_id: uuid.UUID
    version_id: uuid.UUID
    version_number: Annotated[int, Field(strict=True, ge=1)]
    slug: Annotated[str, StringConstraints(min_length=1, max_length=63)]
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    description: Annotated[
        str,
        StringConstraints(max_length=MAX_AUTHORING_SKILL_DESCRIPTION_CHARS),
    ]
    payload_checksum: Annotated[
        str,
        StringConstraints(pattern=_HEX_DIGEST_PATTERN),
    ]
    authoring_only: Literal[True] = True


class SkillCatalogSearchResult(_StrictDto):
    query: _SearchQuery
    items: tuple[SkillCatalogItem, ...] = Field(
        max_length=MAX_AUTHORING_SEARCH_RESULTS,
    )
    truncated: bool
    next_cursor: _Cursor | None = None

    @model_validator(mode="after")
    def validate_pagination(self) -> SkillCatalogSearchResult:
        if self.truncated != (self.next_cursor is not None):
            raise ValueError("Skill catalog pagination state is inconsistent")
        return self


class SkillTextReference(_StrictDto):
    kind: Literal["skill_text"] = "skill_text"
    scope: Literal["project", "system"]
    skill_id: uuid.UUID
    version_id: uuid.UUID
    payload_checksum: Annotated[
        str,
        StringConstraints(pattern=_HEX_DIGEST_PATTERN),
    ]
    path: _CanonicalPath
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    size_bytes: Annotated[
        int,
        Field(strict=True, ge=0, le=MAX_AUTHORING_SKILL_TEXT_BYTES),
    ]
    sha256: Annotated[str, StringConstraints(pattern=_HEX_DIGEST_PATTERN)]
    content: str
    authoring_only: Literal[True] = True

    @field_validator("content")
    @classmethod
    def validate_content_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_AUTHORING_SKILL_TEXT_BYTES:
            raise ValueError("Skill text exceeds the authoring read limit")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _canonical_skill_path(value)


class McpToolCatalogItem(_StrictDto):
    kind: Literal["mcp_tool"] = "mcp_tool"
    scope: Literal["project", "system"]
    mcp_server_id: uuid.UUID
    version_id: uuid.UUID
    version_number: Annotated[int, Field(strict=True, ge=1)]
    server_slug: Annotated[str, StringConstraints(min_length=1, max_length=63)]
    server_name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    server_description: Annotated[
        str,
        StringConstraints(max_length=MAX_AUTHORING_MCP_DESCRIPTION_CHARS),
    ]
    payload_checksum: Annotated[
        str,
        StringConstraints(pattern=_HEX_DIGEST_PATTERN),
    ]
    tool_name: Annotated[
        str,
        StringConstraints(min_length=1, max_length=255, pattern=_TOOL_NAME_PATTERN),
    ]
    tool_description: Annotated[str, StringConstraints(max_length=4096)]
    inventory_status: Literal["ready", "degraded"]
    inventory_error_code: Literal["mcp_discovery_unavailable", "mcp_catalog_invalid"] | None
    last_success_at: datetime
    authoring_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_inventory_state(self) -> McpToolCatalogItem:
        if (self.inventory_status == "ready") != (self.inventory_error_code is None):
            raise ValueError("MCP inventory status is inconsistent")
        return self


class McpToolCatalogSearchResult(_StrictDto):
    query: _SearchQuery
    items: tuple[McpToolCatalogItem, ...] = Field(
        max_length=MAX_AUTHORING_SEARCH_RESULTS,
    )
    truncated: bool
    next_cursor: _Cursor | None = None

    @model_validator(mode="after")
    def validate_pagination(self) -> McpToolCatalogSearchResult:
        if self.truncated != (self.next_cursor is not None):
            raise ValueError("MCP catalog pagination state is inconsistent")
        return self


class McpToolMetadata(_StrictDto):
    item: McpToolCatalogItem


class ProjectAuthoringCatalogRepositoryPort(Protocol):
    async def search_skills(
        self,
        context: ProjectContext,
        request: SkillCatalogSearch,
    ) -> SkillCatalogSearchResult: ...

    async def read_skill_text(
        self,
        context: ProjectContext,
        request: SkillTextRead,
    ) -> SkillTextReference: ...

    async def search_mcp_tools(
        self,
        context: ProjectContext,
        request: McpToolCatalogSearch,
    ) -> McpToolCatalogSearchResult: ...

    async def inspect_mcp_tool(
        self,
        context: ProjectContext,
        request: McpToolMetadataInspect,
    ) -> McpToolMetadata: ...


def _canonical_skill_path(value: str) -> str:
    if not value or len(value) > 1024 or "\\" in value or "\x00" in value or unicodedata.normalize("NFC", value) != value:
        raise ValueError("Skill path is not canonical")
    candidate = PurePosixPath(value)
    if not candidate.parts or value == "." or candidate.is_absolute() or candidate.as_posix() != value or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("Skill path is not canonical")
    return value


def _safe_metadata_text(value: object, *, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ValueError("Catalog metadata is invalid")
    characters: list[str] = []
    for character in value:
        if character.isspace():
            characters.append(" ")
        elif unicodedata.category(character) not in {"Cc", "Cf"}:
            characters.append(character)
    normalized = " ".join("".join(characters).split())
    return normalized[:max_chars]


def _escaped_like_pattern(value: str) -> str:
    escaped = re.sub(r"([%_\\])", r"\\\1", value.casefold())
    return f"%{escaped}%"


def _cursor_query_digest(query: str) -> str:
    return hashlib.sha256(query.casefold().encode("utf-8")).hexdigest()


def _encode_cursor(
    *,
    kind: Literal["skills", "mcp_tools"],
    query: str,
    position: tuple[int | str, ...],
) -> str:
    raw = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "k": kind,
            "q": _cursor_query_digest(query),
            "p": position,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(encoded) > MAX_AUTHORING_CURSOR_CHARS:
        raise ValueError("Authoring catalog cursor is too large")
    return encoded


def _decode_cursor(
    cursor: str,
    *,
    kind: Literal["skills", "mcp_tools"],
    query: str,
    position_length: int,
) -> tuple[object, ...]:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            f"{cursor}{padding}",
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Authoring catalog cursor is invalid") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"k", "p", "q", "v"}
        or type(payload.get("v")) is not int
        or payload.get("v") != _CURSOR_VERSION
        or payload.get("k") != kind
        or payload.get("q") != _cursor_query_digest(query)
        or not isinstance(payload.get("p"), list)
        or len(payload["p"]) != position_length
    ):
        raise ValueError("Authoring catalog cursor is invalid")
    return tuple(payload["p"])


def _cursor_rank(value: object) -> int:
    if type(value) is not int or value not in {0, 1, 2}:
        raise ValueError("Authoring catalog cursor is invalid")
    return value


def _cursor_scope_rank(value: object) -> int:
    if type(value) is not int or value not in {0, 1}:
        raise ValueError("Authoring catalog cursor is invalid")
    return value


def _cursor_sort_text(value: object, *, max_chars: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_chars or "\x00" in value:
        raise ValueError("Authoring catalog cursor is invalid")
    return value


def _cursor_uuid(value: object) -> uuid.UUID:
    if not isinstance(value, str):
        raise ValueError("Authoring catalog cursor is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError("Authoring catalog cursor is invalid") from None
    if str(parsed) != value:
        raise ValueError("Authoring catalog cursor is invalid")
    return parsed


def _skill_cursor_position(
    cursor: str,
    *,
    query: str,
) -> tuple[int, int, str, uuid.UUID, uuid.UUID]:
    values = _decode_cursor(
        cursor,
        kind=_SKILL_CURSOR_KIND,
        query=query,
        position_length=5,
    )
    return (
        _cursor_rank(values[0]),
        _cursor_scope_rank(values[1]),
        _cursor_sort_text(values[2], max_chars=63),
        _cursor_uuid(values[3]),
        _cursor_uuid(values[4]),
    )


def _mcp_cursor_position(
    cursor: str,
    *,
    query: str,
) -> tuple[int, int, str, str, uuid.UUID, uuid.UUID]:
    values = _decode_cursor(
        cursor,
        kind=_MCP_CURSOR_KIND,
        query=query,
        position_length=6,
    )
    return (
        _cursor_rank(values[0]),
        _cursor_scope_rank(values[1]),
        _cursor_sort_text(values[2], max_chars=63),
        _cursor_sort_text(values[3], max_chars=255),
        _cursor_uuid(values[4]),
        _cursor_uuid(values[5]),
    )


class ProjectAuthoringCatalogRepository:
    """Read-only project catalog for Builder authoring references.

    The repository intentionally has no discovery, execution, activation,
    binding, credential-material, or dependency-write methods. Catalog reads
    are observations only and never become runtime authority.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _require_context(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))

    @staticmethod
    def _context_exists(context: ProjectContext):
        return exists(
            select(1)
            .select_from(ProjectMembershipRow)
            .join(ProjectRow, ProjectRow.id == ProjectMembershipRow.project_id)
            .where(
                ProjectMembershipRow.id == context.membership_id,
                ProjectMembershipRow.project_id == context.project_id,
                ProjectMembershipRow.user_id == str(context.user_id),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.version == context.membership_version,
                ProjectRow.id == context.project_id,
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
            )
        )

    @classmethod
    def _eligible_skills(cls, context: ProjectContext):
        project_skills = (
            select(
                literal(0).label("scope_rank"),
                literal("project").label("scope"),
                SkillRow.id.label("skill_id"),
                SkillVersionRow.id.label("version_id"),
                SkillVersionRow.version_number,
                SkillRow.slug,
                SkillRow.display_name,
                SkillVersionRow.description,
                SkillVersionRow.payload_checksum,
            )
            .select_from(SkillRow)
            .join(
                SkillVersionRow,
                and_(
                    SkillVersionRow.skill_id == SkillRow.id,
                    SkillRow.current_published_version_id == SkillVersionRow.id,
                ),
            )
            .where(
                SkillRow.scope == "project",
                SkillRow.project_id == context.project_id,
                SkillRow.status == "active",
                SkillVersionRow.workflow_status == "published",
                SkillVersionRow.revoked_at.is_(None),
            )
        )
        system_skills = (
            select(
                literal(1).label("scope_rank"),
                literal("system").label("scope"),
                SkillRow.id.label("skill_id"),
                SkillVersionRow.id.label("version_id"),
                SkillVersionRow.version_number,
                SkillRow.slug,
                SkillRow.display_name,
                SkillVersionRow.description,
                SkillVersionRow.payload_checksum,
            )
            .select_from(ProjectSystemSkillBindingRow)
            .join(
                SkillRow,
                and_(
                    SkillRow.id == ProjectSystemSkillBindingRow.system_skill_id,
                    SkillRow.scope == "system",
                    SkillRow.project_id.is_(None),
                ),
            )
            .join(
                SkillVersionRow,
                and_(
                    SkillVersionRow.skill_id == SkillRow.id,
                    SkillVersionRow.id == ProjectSystemSkillBindingRow.skill_version_id,
                ),
            )
            .where(
                ProjectSystemSkillBindingRow.project_id == context.project_id,
                ProjectSystemSkillBindingRow.enabled.is_(True),
                SkillRow.status == "active",
                SkillVersionRow.workflow_status == "published",
                SkillVersionRow.revoked_at.is_(None),
            )
        )
        return union_all(project_skills, system_skills).subquery("project_authoring_skills")

    @classmethod
    def _eligible_mcps(cls, context: ProjectContext):
        project_mcps = (
            select(
                literal(0).label("scope_rank"),
                literal("project").label("scope"),
                McpServerRow.id.label("mcp_server_id"),
                McpServerVersionRow.id.label("version_id"),
                McpServerVersionRow.version_number,
                McpServerRow.slug.label("server_slug"),
                McpServerRow.display_name.label("server_name"),
                McpServerVersionRow.description.label("server_description"),
                McpServerVersionRow.payload_checksum,
            )
            .select_from(McpServerRow)
            .join(
                McpServerVersionRow,
                and_(
                    McpServerVersionRow.mcp_server_id == McpServerRow.id,
                    McpServerRow.current_published_version_id == McpServerVersionRow.id,
                ),
            )
            .where(
                McpServerRow.scope == "project",
                McpServerRow.project_id == context.project_id,
                McpServerRow.status == "active",
                McpServerVersionRow.workflow_status == "published",
            )
        )
        system_mcps = (
            select(
                literal(1).label("scope_rank"),
                literal("system").label("scope"),
                McpServerRow.id.label("mcp_server_id"),
                McpServerVersionRow.id.label("version_id"),
                McpServerVersionRow.version_number,
                McpServerRow.slug.label("server_slug"),
                McpServerRow.display_name.label("server_name"),
                McpServerVersionRow.description.label("server_description"),
                McpServerVersionRow.payload_checksum,
            )
            .select_from(ProjectSystemMcpBindingRow)
            .join(
                McpServerRow,
                and_(
                    McpServerRow.id == ProjectSystemMcpBindingRow.system_mcp_server_id,
                    McpServerRow.scope == "system",
                    McpServerRow.project_id.is_(None),
                ),
            )
            .join(
                McpServerVersionRow,
                and_(
                    McpServerVersionRow.mcp_server_id == McpServerRow.id,
                    McpServerVersionRow.id == ProjectSystemMcpBindingRow.mcp_server_version_id,
                ),
            )
            .where(
                ProjectSystemMcpBindingRow.project_id == context.project_id,
                ProjectSystemMcpBindingRow.enabled.is_(True),
                McpServerRow.status == "active",
                McpServerVersionRow.workflow_status == "published",
            )
        )
        return union_all(project_mcps, system_mcps).subquery("project_authoring_mcps")

    async def search_skills(
        self,
        context: ProjectContext,
        request: SkillCatalogSearch,
    ) -> SkillCatalogSearchResult:
        self._require_context(context)
        if not isinstance(request, SkillCatalogSearch):
            raise AssetValidationFailed(context.request_id)
        catalog = self._eligible_skills(context)
        needle = request.query.casefold()
        match_rank = (
            case(
                (func.lower(catalog.c.slug) == needle, 0),
                (func.lower(catalog.c.display_name) == needle, 1),
                else_=2,
            )
            if request.query
            else literal(0)
        )
        sort_slug = func.lower(catalog.c.slug)
        statement = (
            select(
                catalog.c.scope,
                catalog.c.scope_rank,
                catalog.c.skill_id,
                catalog.c.version_id,
                catalog.c.version_number,
                catalog.c.slug,
                catalog.c.display_name,
                func.substr(
                    catalog.c.description,
                    1,
                    MAX_AUTHORING_SKILL_DESCRIPTION_CHARS,
                ).label("description"),
                catalog.c.payload_checksum,
                match_rank.label("match_rank"),
                sort_slug.label("sort_slug"),
            )
            .where(self._context_exists(context))
            .order_by(
                match_rank,
                catalog.c.scope_rank,
                sort_slug,
                catalog.c.skill_id,
                catalog.c.version_id,
            )
        )
        if request.query:
            pattern = _escaped_like_pattern(request.query)
            statement = statement.where(
                or_(
                    func.lower(catalog.c.slug).like(pattern, escape="\\"),
                    func.lower(catalog.c.display_name).like(pattern, escape="\\"),
                    func.lower(catalog.c.description).like(pattern, escape="\\"),
                )
            )
        if request.cursor is not None:
            try:
                after = _skill_cursor_position(
                    request.cursor,
                    query=request.query,
                )
            except ValueError:
                raise AssetValidationFailed(context.request_id) from None
            statement = statement.where(
                tuple_(
                    match_rank,
                    catalog.c.scope_rank,
                    sort_slug,
                    catalog.c.skill_id,
                    catalog.c.version_id,
                )
                > tuple_(*after)
            )
        statement = statement.limit(request.limit + 1)
        rows = tuple((await self.session.execute(statement)).all())
        items = tuple(self._skill_item(row) for row in rows[: request.limit])
        has_more = len(rows) > request.limit
        next_cursor = None
        if has_more:
            next_cursor = _encode_cursor(
                kind=_SKILL_CURSOR_KIND,
                query=request.query,
                position=self._skill_row_cursor_position(
                    rows[request.limit - 1],
                    query=request.query,
                ),
            )
        return SkillCatalogSearchResult(
            query=request.query,
            items=items,
            truncated=has_more,
            next_cursor=next_cursor,
        )

    async def read_skill_text(
        self,
        context: ProjectContext,
        request: SkillTextRead,
    ) -> SkillTextReference:
        self._require_context(context)
        if not isinstance(request, SkillTextRead):
            raise AssetValidationFailed(context.request_id)
        catalog = self._eligible_skills(context)
        statement = (
            select(
                catalog.c.scope,
                catalog.c.skill_id,
                catalog.c.version_id,
                catalog.c.payload_checksum,
                SkillVersionFileRow.path,
                SkillVersionFileRow.media_type,
                SkillVersionFileRow.size_bytes,
                SkillVersionFileRow.sha256,
                case(
                    (
                        SkillVersionFileRow.size_bytes <= MAX_AUTHORING_SKILL_TEXT_BYTES,
                        SkillVersionFileRow.content,
                    ),
                    else_=None,
                ).label("content"),
            )
            .select_from(catalog)
            .join(
                SkillVersionFileRow,
                SkillVersionFileRow.skill_version_id == catalog.c.version_id,
            )
            .where(
                self._context_exists(context),
                catalog.c.skill_id == request.skill_id,
                catalog.c.version_id == request.version_id,
                SkillVersionFileRow.path == request.path,
            )
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        if row.content is None:
            raise AssetValidationFailed(context.request_id)
        raw = bytes(row.content)
        if len(raw) != row.size_bytes or hashlib.sha256(raw).hexdigest() != row.sha256:
            raise AssetValidationFailed(context.request_id)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise AssetValidationFailed(context.request_id) from None
        if "\x00" in content:
            raise AssetValidationFailed(context.request_id)
        return SkillTextReference(
            scope=row.scope,
            skill_id=row.skill_id,
            version_id=row.version_id,
            payload_checksum=row.payload_checksum,
            path=row.path,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            content=content,
        )

    async def search_mcp_tools(
        self,
        context: ProjectContext,
        request: McpToolCatalogSearch,
    ) -> McpToolCatalogSearchResult:
        self._require_context(context)
        if not isinstance(request, McpToolCatalogSearch):
            raise AssetValidationFailed(context.request_id)
        after = None
        if request.cursor is not None:
            try:
                after = _mcp_cursor_position(
                    request.cursor,
                    query=request.query,
                )
            except ValueError:
                raise AssetValidationFailed(context.request_id) from None
        statement = self._mcp_tool_statement(
            context,
            query=request.query,
            after=after,
        ).limit(MAX_AUTHORING_MCP_SCAN_PER_PAGE + 1)
        rows = tuple((await self.session.execute(statement)).all())
        candidate_rows = rows[:MAX_AUTHORING_MCP_SCAN_PER_PAGE]
        grant_ids = await self._active_grant_ids(
            tuple(
                sorted(
                    {uuid.UUID(str(row.version_id)) for row in candidate_rows},
                    key=lambda value: value.int,
                )
            )
        )
        valid_rows_and_items: list[tuple[object, McpToolCatalogItem]] = []
        for row in candidate_rows:
            item = self._mcp_tool_item(
                row,
                active_grant_ids=grant_ids.get(
                    uuid.UUID(str(row.version_id)),
                    (),
                ),
            )
            if item is not None:
                valid_rows_and_items.append((row, item))
        selected = tuple(valid_rows_and_items[: request.limit])
        raw_has_more = len(rows) > MAX_AUTHORING_MCP_SCAN_PER_PAGE
        valid_has_more = len(valid_rows_and_items) > request.limit
        next_cursor = None
        if valid_has_more:
            cursor_row = selected[-1][0]
        elif raw_has_more:
            cursor_row = candidate_rows[-1]
        else:
            cursor_row = None
        if cursor_row is not None:
            next_cursor = _encode_cursor(
                kind=_MCP_CURSOR_KIND,
                query=request.query,
                position=self._mcp_row_cursor_position(
                    cursor_row,
                    query=request.query,
                ),
            )
        return McpToolCatalogSearchResult(
            query=request.query,
            items=tuple(item for _row, item in selected),
            truncated=next_cursor is not None,
            next_cursor=next_cursor,
        )

    async def inspect_mcp_tool(
        self,
        context: ProjectContext,
        request: McpToolMetadataInspect,
    ) -> McpToolMetadata:
        self._require_context(context)
        if not isinstance(request, McpToolMetadataInspect):
            raise AssetValidationFailed(context.request_id)
        statement = self._mcp_tool_statement(
            context,
            mcp_server_id=request.mcp_server_id,
            version_id=request.version_id,
            tool_name=request.tool_name,
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        grant_ids = await self._active_grant_ids((request.version_id,))
        item = self._mcp_tool_item(
            row,
            active_grant_ids=grant_ids.get(request.version_id, ()),
        )
        if item is None:
            raise AssetNotFound(context.request_id)
        return McpToolMetadata(item=item)

    async def revalidate_dependency_snapshot(
        self,
        context: ProjectContext,
        snapshot: SkillBuilderDependencySnapshot,
    ) -> SkillBuilderDependencySnapshot:
        """Re-resolve authoring requirements against current project authority.

        The returned snapshot is safe evidence for a candidate draft only. This
        method cannot mutate bindings, grants, inventories, or runtime policy.
        """

        self._require_context(context)
        if not isinstance(snapshot, SkillBuilderDependencySnapshot):
            raise AssetValidationFailed(context.request_id)
        requirements: list[SkillBuilderSkillDependency | SkillBuilderMcpToolDependency] = []
        for requirement in sorted(
            snapshot.requirements,
            key=lambda item: item.reference,
        ):
            if isinstance(requirement, SkillBuilderSkillDependency):
                requirements.append(
                    await self._revalidate_skill_dependency(
                        context,
                        requirement,
                    )
                )
            else:
                requirements.append(
                    await self._revalidate_mcp_dependency(
                        context,
                        requirement,
                    )
                )
        return SkillBuilderDependencySnapshot(
            draft_checksum=snapshot.draft_checksum,
            requirements=tuple(requirements),
        )

    async def _revalidate_skill_dependency(
        self,
        context: ProjectContext,
        requirement: SkillBuilderSkillDependency,
    ) -> SkillBuilderSkillDependency:
        catalog = self._eligible_skills(context)
        statement = select(
            catalog.c.scope,
            catalog.c.skill_id,
            catalog.c.version_id,
            catalog.c.version_number,
            catalog.c.slug,
            catalog.c.display_name,
            catalog.c.payload_checksum,
        ).where(
            self._context_exists(context),
            catalog.c.skill_id == requirement.skill_id,
            catalog.c.version_id == requirement.version_id,
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            raise AssetNotFound(context.request_id)
        current = SkillBuilderSkillDependency(
            reference=(f"skill:{row.scope}:{row.slug}:v{row.version_number}"),
            scope=row.scope,
            skill_id=row.skill_id,
            version_id=row.version_id,
            version_number=row.version_number,
            slug=row.slug,
            display_name=_safe_metadata_text(
                row.display_name,
                max_chars=120,
            ),
            payload_checksum=row.payload_checksum,
        )
        if current.reference != requirement.reference or current.scope != requirement.scope or current.payload_checksum != requirement.payload_checksum:
            raise AssetNotFound(context.request_id)
        return current

    async def _revalidate_mcp_dependency(
        self,
        context: ProjectContext,
        requirement: SkillBuilderMcpToolDependency,
    ) -> SkillBuilderMcpToolDependency:
        metadata = await self.inspect_mcp_tool(
            context,
            McpToolMetadataInspect(
                mcp_server_id=requirement.mcp_server_id,
                version_id=requirement.version_id,
                tool_name=requirement.tool_name,
            ),
        )
        item = metadata.item
        current = SkillBuilderMcpToolDependency(
            reference=(f"mcp:{item.scope}:{item.server_slug}:v{item.version_number}:{item.tool_name}"),
            scope=item.scope,
            mcp_server_id=item.mcp_server_id,
            version_id=item.version_id,
            version_number=item.version_number,
            server_slug=item.server_slug,
            server_name=item.server_name,
            tool_name=item.tool_name,
            payload_checksum=item.payload_checksum,
            inventory_status=item.inventory_status,
            inventory_error_code=item.inventory_error_code,
            last_success_at=item.last_success_at,
        )
        if current.reference != requirement.reference or current.scope != requirement.scope or current.payload_checksum != requirement.payload_checksum:
            raise AssetNotFound(context.request_id)
        return current

    def _mcp_tool_statement(
        self,
        context: ProjectContext,
        *,
        query: str | None = None,
        after: tuple[int, int, str, str, uuid.UUID, uuid.UUID] | None = None,
        mcp_server_id: uuid.UUID | None = None,
        version_id: uuid.UUID | None = None,
        tool_name: str | None = None,
    ):
        catalog = self._eligible_mcps(context)
        tool = func.jsonb_array_elements(ProjectMcpToolInventoryRow.tools).table_valued(column("value", JSONB)).lateral("cached_tool")
        cached_name = tool.c.value["name"].astext
        cached_description = tool.c.value["description"].astext
        match_rank = (
            case(
                (func.lower(cached_name) == query.casefold(), 0),
                (func.lower(catalog.c.server_slug) == query.casefold(), 1),
                else_=2,
            )
            if query
            else literal(0)
        )
        sort_server_slug = func.lower(catalog.c.server_slug)
        sort_tool_name = func.lower(cached_name)
        statement = (
            select(
                catalog.c.scope,
                catalog.c.scope_rank,
                catalog.c.mcp_server_id,
                catalog.c.version_id,
                catalog.c.version_number,
                catalog.c.server_slug,
                catalog.c.server_name,
                func.substr(
                    catalog.c.server_description,
                    1,
                    MAX_AUTHORING_MCP_DESCRIPTION_CHARS,
                ).label("server_description"),
                catalog.c.payload_checksum,
                cached_name.label("tool_name"),
                cached_description.label("tool_description"),
                ProjectMcpToolInventoryRow.attempt_payload_checksum,
                ProjectMcpToolInventoryRow.attempt_grant_digest,
                ProjectMcpToolInventoryRow.attempt_status,
                ProjectMcpToolInventoryRow.public_error_code,
                ProjectMcpToolInventoryRow.tools_payload_checksum,
                ProjectMcpToolInventoryRow.tools_grant_digest,
                ProjectMcpToolInventoryRow.last_success_at,
                match_rank.label("match_rank"),
                sort_server_slug.label("sort_server_slug"),
                sort_tool_name.label("sort_tool_name"),
            )
            .select_from(catalog)
            .join(
                ProjectMcpToolInventoryRow,
                and_(
                    ProjectMcpToolInventoryRow.project_id == context.project_id,
                    ProjectMcpToolInventoryRow.mcp_server_id == catalog.c.mcp_server_id,
                    ProjectMcpToolInventoryRow.mcp_server_version_id == catalog.c.version_id,
                ),
            )
            .join(tool, true())
            .where(
                self._context_exists(context),
                ProjectMcpToolInventoryRow.tools_payload_checksum == catalog.c.payload_checksum,
                ProjectMcpToolInventoryRow.last_success_at.is_not(None),
            )
        )
        if query is not None:
            if query:
                pattern = _escaped_like_pattern(query)
                statement = statement.where(
                    or_(
                        func.lower(cached_name).like(pattern, escape="\\"),
                        func.lower(cached_description).like(pattern, escape="\\"),
                        func.lower(catalog.c.server_slug).like(
                            pattern,
                            escape="\\",
                        ),
                        func.lower(catalog.c.server_name).like(
                            pattern,
                            escape="\\",
                        ),
                        func.lower(catalog.c.server_description).like(
                            pattern,
                            escape="\\",
                        ),
                    )
                )
            if after is not None:
                statement = statement.where(
                    tuple_(
                        match_rank,
                        catalog.c.scope_rank,
                        sort_server_slug,
                        sort_tool_name,
                        catalog.c.mcp_server_id,
                        catalog.c.version_id,
                    )
                    > tuple_(*after)
                )
            statement = statement.order_by(
                match_rank,
                catalog.c.scope_rank,
                sort_server_slug,
                sort_tool_name,
                catalog.c.mcp_server_id,
                catalog.c.version_id,
            )
        else:
            statement = statement.where(
                catalog.c.mcp_server_id == mcp_server_id,
                catalog.c.version_id == version_id,
                cached_name == tool_name,
            )
        return statement

    async def _active_grant_ids(
        self,
        version_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, tuple[uuid.UUID, ...]]:
        if not version_ids:
            return {}
        statement = (
            select(
                CredentialGrantRow.mcp_server_version_id,
                CredentialGrantRow.id,
            )
            .where(
                CredentialGrantRow.mcp_server_version_id.in_(version_ids),
                CredentialGrantRow.status == "active",
            )
            .order_by(
                CredentialGrantRow.mcp_server_version_id,
                CredentialGrantRow.id,
            )
        )
        grouped: defaultdict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for row in (await self.session.execute(statement)).all():
            grouped[uuid.UUID(str(row.mcp_server_version_id))].append(uuid.UUID(str(row.id)))
        return {key: tuple(values) for key, values in grouped.items()}

    @staticmethod
    def _skill_row_cursor_position(
        row: object,
        *,
        query: str,
    ) -> tuple[int, int, str, str, str]:
        raw_slug = str(getattr(row, "slug"))
        raw_display_name = str(getattr(row, "display_name"))
        fallback_rank = 0 if not query or raw_slug.casefold() == query.casefold() else 1 if raw_display_name.casefold() == query.casefold() else 2
        return (
            int(getattr(row, "match_rank", fallback_rank)),
            int(
                getattr(
                    row,
                    "scope_rank",
                    0 if getattr(row, "scope") == "project" else 1,
                )
            ),
            str(getattr(row, "sort_slug", raw_slug.lower())),
            str(uuid.UUID(str(getattr(row, "skill_id")))),
            str(uuid.UUID(str(getattr(row, "version_id")))),
        )

    @staticmethod
    def _mcp_row_cursor_position(
        row: object,
        *,
        query: str,
    ) -> tuple[int, int, str, str, str, str]:
        raw_server_slug = str(getattr(row, "server_slug"))
        raw_tool_name = str(getattr(row, "tool_name"))
        fallback_rank = 0 if not query or raw_tool_name.casefold() == query.casefold() else 1 if raw_server_slug.casefold() == query.casefold() else 2
        return (
            int(getattr(row, "match_rank", fallback_rank)),
            int(
                getattr(
                    row,
                    "scope_rank",
                    0 if getattr(row, "scope") == "project" else 1,
                )
            ),
            str(
                getattr(
                    row,
                    "sort_server_slug",
                    raw_server_slug.lower(),
                )
            ),
            str(getattr(row, "sort_tool_name", raw_tool_name.lower())),
            str(uuid.UUID(str(getattr(row, "mcp_server_id")))),
            str(uuid.UUID(str(getattr(row, "version_id")))),
        )

    @staticmethod
    def _skill_item(row: object) -> SkillCatalogItem:
        return SkillCatalogItem(
            scope=getattr(row, "scope"),
            skill_id=getattr(row, "skill_id"),
            version_id=getattr(row, "version_id"),
            version_number=getattr(row, "version_number"),
            slug=getattr(row, "slug"),
            display_name=_safe_metadata_text(
                getattr(row, "display_name"),
                max_chars=120,
            ),
            description=_safe_metadata_text(
                getattr(row, "description"),
                max_chars=MAX_AUTHORING_SKILL_DESCRIPTION_CHARS,
            ),
            payload_checksum=getattr(row, "payload_checksum"),
        )

    @staticmethod
    def _mcp_tool_item(
        row: object,
        *,
        active_grant_ids: tuple[uuid.UUID, ...],
    ) -> McpToolCatalogItem | None:
        current_grant_digest = mcp_grant_closure_digest(active_grant_ids)
        if getattr(row, "tools_payload_checksum") != getattr(row, "payload_checksum") or getattr(row, "tools_grant_digest") != current_grant_digest or getattr(row, "last_success_at") is None:
            return None
        attempt_matches = getattr(row, "attempt_payload_checksum") == getattr(row, "payload_checksum") and getattr(row, "attempt_grant_digest") == current_grant_digest
        attempt_status = getattr(row, "attempt_status")
        if attempt_status not in {"ready", "failed"}:
            raise ValueError("MCP inventory status is invalid")
        failed = attempt_status == "failed" and attempt_matches
        error_code = getattr(row, "public_error_code") if failed else None
        if failed and error_code not in _MCP_INVENTORY_ERROR_CODES:
            raise ValueError("MCP inventory error code is invalid")
        if attempt_status == "ready" and getattr(row, "public_error_code") is not None:
            raise ValueError("MCP inventory error code is invalid")
        return McpToolCatalogItem(
            scope=getattr(row, "scope"),
            mcp_server_id=getattr(row, "mcp_server_id"),
            version_id=getattr(row, "version_id"),
            version_number=getattr(row, "version_number"),
            server_slug=getattr(row, "server_slug"),
            server_name=_safe_metadata_text(
                getattr(row, "server_name"),
                max_chars=120,
            ),
            server_description=_safe_metadata_text(
                getattr(row, "server_description"),
                max_chars=MAX_AUTHORING_MCP_DESCRIPTION_CHARS,
            ),
            payload_checksum=getattr(row, "payload_checksum"),
            tool_name=getattr(row, "tool_name"),
            tool_description=_safe_metadata_text(
                getattr(row, "tool_description"),
                max_chars=4096,
            ),
            inventory_status="degraded" if failed else "ready",
            inventory_error_code=error_code,
            last_success_at=getattr(row, "last_success_at"),
        )


_ResultT = TypeVar("_ResultT")


class _SessionFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]: ...


class ProjectAuthoringCatalogTools:
    """Fixed-context, read-only operations for a future Builder Agent.

    Calling a method only reads authoring references. It never activates a
    Skill, performs MCP discovery or execution, reads credential material, or
    records a runtime dependency.
    """

    def __init__(
        self,
        session_factory: _SessionFactory,
        context: ProjectContext,
        *,
        repository_factory: Callable[
            [AsyncSession],
            ProjectAuthoringCatalogRepositoryPort,
        ] = ProjectAuthoringCatalogRepository,
    ) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))
        if Capability.SHARED_ASSETS_READ not in context.capabilities:
            raise AssetForbidden(context.request_id)
        self._session_factory = session_factory
        self._context = context
        self._repository_factory = repository_factory

    async def search_skills(
        self,
        request: SkillCatalogSearch,
    ) -> SkillCatalogSearchResult:
        if not isinstance(request, SkillCatalogSearch):
            raise AssetValidationFailed(self._context.request_id)
        return await self._execute(
            lambda repository: repository.search_skills(
                self._context,
                request,
            )
        )

    async def read_skill_text(
        self,
        request: SkillTextRead,
    ) -> SkillTextReference:
        if not isinstance(request, SkillTextRead):
            raise AssetValidationFailed(self._context.request_id)
        return await self._execute(
            lambda repository: repository.read_skill_text(
                self._context,
                request,
            )
        )

    async def search_mcp_tools(
        self,
        request: McpToolCatalogSearch,
    ) -> McpToolCatalogSearchResult:
        if not isinstance(request, McpToolCatalogSearch):
            raise AssetValidationFailed(self._context.request_id)
        return await self._execute(
            lambda repository: repository.search_mcp_tools(
                self._context,
                request,
            )
        )

    async def inspect_mcp_tool(
        self,
        request: McpToolMetadataInspect,
    ) -> McpToolMetadata:
        if not isinstance(request, McpToolMetadataInspect):
            raise AssetValidationFailed(self._context.request_id)
        return await self._execute(
            lambda repository: repository.inspect_mcp_tool(
                self._context,
                request,
            )
        )

    async def _execute(
        self,
        operation: Callable[
            [ProjectAuthoringCatalogRepositoryPort],
            Awaitable[_ResultT],
        ],
    ) -> _ResultT:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    return await operation(self._repository_factory(session))
        except SharedAssetError:
            raise
        except (DBAPIError, SATimeoutError, ValidationError, TypeError, ValueError):
            raise AssetStorageUnavailable(self._context.request_id) from None


__all__ = [
    "MAX_AUTHORING_CURSOR_CHARS",
    "MAX_AUTHORING_MCP_SCAN_PER_PAGE",
    "MAX_AUTHORING_SEARCH_QUERY_CHARS",
    "MAX_AUTHORING_SEARCH_RESULTS",
    "MAX_AUTHORING_SKILL_TEXT_BYTES",
    "McpToolCatalogItem",
    "McpToolCatalogSearch",
    "McpToolCatalogSearchResult",
    "McpToolMetadata",
    "McpToolMetadataInspect",
    "ProjectAuthoringCatalogRepository",
    "ProjectAuthoringCatalogRepositoryPort",
    "ProjectAuthoringCatalogTools",
    "SkillCatalogItem",
    "SkillCatalogSearch",
    "SkillCatalogSearchResult",
    "SkillTextRead",
    "SkillTextReference",
]
