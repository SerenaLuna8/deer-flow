from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.shared_assets import (
    McpServerRow,
    McpServerVersionRow,
    ProjectMcpToolInventoryRow,
)

MAX_MCP_TOOL_INVENTORY_ITEMS = 128
MAX_MCP_TOOL_INVENTORY_NAME_CHARS = 255
MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS = 4_096
MCP_TOOL_INVENTORY_ERROR_CODES = frozenset(
    {
        "mcp_discovery_unavailable",
        "mcp_catalog_invalid",
    }
)

_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GRANT_DIGEST_DOMAIN = b"deerflow:mcp-tool-inventory:grant-closure:v1\0"

McpToolInventoryAttemptStatus = Literal["ready", "failed"]
McpToolInventoryErrorCode = Literal[
    "mcp_discovery_unavailable",
    "mcp_catalog_invalid",
]


@dataclass(frozen=True, slots=True)
class McpToolInventoryRecord:
    attempt_payload_checksum: str
    attempt_grant_digest: str
    attempt_status: McpToolInventoryAttemptStatus
    public_error_code: McpToolInventoryErrorCode | None
    tools: tuple[dict[str, str], ...]
    tools_payload_checksum: str | None
    tools_grant_digest: str | None
    last_attempt_at: datetime
    last_success_at: datetime | None


def mcp_grant_closure_digest(grant_ids: Sequence[uuid.UUID]) -> str:
    digest = hashlib.sha256(_GRANT_DIGEST_DOMAIN)
    for grant_id in sorted(grant_ids, key=lambda value: value.int):
        if not isinstance(grant_id, uuid.UUID):
            raise TypeError("MCP grant closure IDs must be UUIDs")
        digest.update(grant_id.bytes)
    return digest.hexdigest()


def normalize_mcp_tool_inventory(
    tools: Sequence[Mapping[str, object]],
) -> tuple[dict[str, str], ...]:
    if not isinstance(tools, (list, tuple)) or len(tools) > MAX_MCP_TOOL_INVENTORY_ITEMS:
        raise ValueError("invalid MCP tool inventory")
    normalized: list[dict[str, str]] = []
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, Mapping) or set(tool) != {"name", "description"}:
            raise ValueError("invalid MCP tool inventory")
        name = tool.get("name")
        description = tool.get("description")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > MAX_MCP_TOOL_INVENTORY_NAME_CHARS
            or _TOOL_NAME.fullmatch(name) is None
            or name in names
            or not isinstance(description, str)
            or len(description) > MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS
        ):
            raise ValueError("invalid MCP tool inventory")
        names.add(name)
        normalized.append({"name": name, "description": description})
    return tuple(normalized)


class McpToolInventoryRepository:
    """Project-scoped, display-only cache for Worker MCP discovery results."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def record_success(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        payload_checksum: str,
        grant_digest: str,
        tools: Sequence[Mapping[str, object]],
        attempted_at: datetime | None = None,
    ) -> None:
        normalized = normalize_mcp_tool_inventory(tools)
        timestamp = attempted_at or datetime.now(UTC)
        await self._require_exact_version(
            project_id=project_id,
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=mcp_server_version_id,
            payload_checksum=payload_checksum,
        )
        statement = insert(ProjectMcpToolInventoryRow).values(
            project_id=project_id,
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=mcp_server_version_id,
            attempt_payload_checksum=payload_checksum,
            attempt_grant_digest=grant_digest,
            attempt_status="ready",
            public_error_code=None,
            tools=list(normalized),
            tools_payload_checksum=payload_checksum,
            tools_grant_digest=grant_digest,
            last_attempt_at=timestamp,
            last_success_at=timestamp,
            revision=1,
        )
        excluded = statement.excluded
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    ProjectMcpToolInventoryRow.project_id,
                    ProjectMcpToolInventoryRow.mcp_server_version_id,
                ],
                set_={
                    "mcp_server_id": excluded.mcp_server_id,
                    "attempt_payload_checksum": excluded.attempt_payload_checksum,
                    "attempt_grant_digest": excluded.attempt_grant_digest,
                    "attempt_status": excluded.attempt_status,
                    "public_error_code": None,
                    "tools": excluded.tools,
                    "tools_payload_checksum": excluded.tools_payload_checksum,
                    "tools_grant_digest": excluded.tools_grant_digest,
                    "last_attempt_at": excluded.last_attempt_at,
                    "last_success_at": excluded.last_success_at,
                    "revision": ProjectMcpToolInventoryRow.revision + 1,
                },
                where=excluded.last_attempt_at > ProjectMcpToolInventoryRow.last_attempt_at,
            )
        )

    async def record_failure(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        payload_checksum: str,
        grant_digest: str,
        public_error_code: str,
        attempted_at: datetime | None = None,
    ) -> None:
        if public_error_code not in MCP_TOOL_INVENTORY_ERROR_CODES:
            raise ValueError("invalid MCP tool inventory error code")
        timestamp = attempted_at or datetime.now(UTC)
        await self._require_exact_version(
            project_id=project_id,
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=mcp_server_version_id,
            payload_checksum=payload_checksum,
        )
        statement = insert(ProjectMcpToolInventoryRow).values(
            project_id=project_id,
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=mcp_server_version_id,
            attempt_payload_checksum=payload_checksum,
            attempt_grant_digest=grant_digest,
            attempt_status="failed",
            public_error_code=public_error_code,
            tools=[],
            tools_payload_checksum=None,
            tools_grant_digest=None,
            last_attempt_at=timestamp,
            last_success_at=None,
            revision=1,
        )
        excluded = statement.excluded
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    ProjectMcpToolInventoryRow.project_id,
                    ProjectMcpToolInventoryRow.mcp_server_version_id,
                ],
                set_={
                    "mcp_server_id": excluded.mcp_server_id,
                    "attempt_payload_checksum": excluded.attempt_payload_checksum,
                    "attempt_grant_digest": excluded.attempt_grant_digest,
                    "attempt_status": excluded.attempt_status,
                    "public_error_code": excluded.public_error_code,
                    "last_attempt_at": excluded.last_attempt_at,
                    "revision": ProjectMcpToolInventoryRow.revision + 1,
                },
                where=excluded.last_attempt_at > ProjectMcpToolInventoryRow.last_attempt_at,
            )
        )

    async def get(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
    ) -> McpToolInventoryRecord | None:
        row = (
            await self.session.execute(
                select(ProjectMcpToolInventoryRow).where(
                    ProjectMcpToolInventoryRow.project_id == project_id,
                    ProjectMcpToolInventoryRow.mcp_server_id == mcp_server_id,
                    ProjectMcpToolInventoryRow.mcp_server_version_id == mcp_server_version_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if (
            not isinstance(row.attempt_payload_checksum, str)
            or _HEX_DIGEST.fullmatch(row.attempt_payload_checksum) is None
            or not isinstance(row.attempt_grant_digest, str)
            or _HEX_DIGEST.fullmatch(row.attempt_grant_digest) is None
            or row.attempt_status not in {"ready", "failed"}
            or (row.attempt_status == "ready" and row.public_error_code is not None)
            or (row.attempt_status == "failed" and row.public_error_code not in MCP_TOOL_INVENTORY_ERROR_CODES)
            or (row.tools_payload_checksum is not None and (not isinstance(row.tools_payload_checksum, str) or _HEX_DIGEST.fullmatch(row.tools_payload_checksum) is None))
            or (row.tools_grant_digest is not None and (not isinstance(row.tools_grant_digest, str) or _HEX_DIGEST.fullmatch(row.tools_grant_digest) is None))
            or ((row.tools_payload_checksum is None) != (row.tools_grant_digest is None))
            or ((row.tools_payload_checksum is None) != (row.last_success_at is None))
        ):
            raise ValueError("invalid persisted MCP tool inventory")
        return McpToolInventoryRecord(
            attempt_payload_checksum=row.attempt_payload_checksum,
            attempt_grant_digest=row.attempt_grant_digest,
            attempt_status=cast(McpToolInventoryAttemptStatus, row.attempt_status),
            public_error_code=cast(
                McpToolInventoryErrorCode | None,
                row.public_error_code,
            ),
            tools=normalize_mcp_tool_inventory(row.tools),
            tools_payload_checksum=row.tools_payload_checksum,
            tools_grant_digest=row.tools_grant_digest,
            last_attempt_at=row.last_attempt_at,
            last_success_at=row.last_success_at,
        )

    async def _require_exact_version(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        payload_checksum: str,
    ) -> None:
        found = await self.session.scalar(
            select(McpServerVersionRow.id)
            .join(
                McpServerRow,
                McpServerRow.id == McpServerVersionRow.mcp_server_id,
            )
            .where(
                McpServerVersionRow.id == mcp_server_version_id,
                McpServerVersionRow.mcp_server_id == mcp_server_id,
                McpServerVersionRow.payload_checksum == payload_checksum,
                or_(
                    and_(
                        McpServerRow.scope == "project",
                        McpServerRow.project_id == project_id,
                    ),
                    and_(
                        McpServerRow.scope == "system",
                        McpServerRow.project_id.is_(None),
                    ),
                ),
            )
        )
        if found is None:
            raise ValueError("stale MCP tool inventory target")


__all__ = [
    "MAX_MCP_TOOL_INVENTORY_DESCRIPTION_CHARS",
    "MAX_MCP_TOOL_INVENTORY_ITEMS",
    "MAX_MCP_TOOL_INVENTORY_NAME_CHARS",
    "MCP_TOOL_INVENTORY_ERROR_CODES",
    "McpToolInventoryRecord",
    "McpToolInventoryRepository",
    "mcp_grant_closure_digest",
    "normalize_mcp_tool_inventory",
]
