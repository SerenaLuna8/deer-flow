from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from deerflow.config.agents_config import AgentModelSettings


class AssetScope(StrEnum):
    SYSTEM = "system"
    PROJECT = "project"


class AssetKind(StrEnum):
    AGENT = "agent"
    SKILL = "skill"
    MCP = "mcp"


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    PUBLISHED = "published"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AssetSelection:
    kind: AssetKind
    asset_id: uuid.UUID
    version_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AgentPayload:
    description: str
    soul: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skill_version_ids: tuple[uuid.UUID, ...]
    mcp_version_ids: tuple[uuid.UUID, ...]
    agents_instructions: str = ""
    identity: str = ""
    user_context: str = ""
    payload_schema_version: int = 1
    model_settings: AgentModelSettings = field(default_factory=AgentModelSettings)


@dataclass(frozen=True)
class SkillArchiveFile:
    path: str
    content: bytes
    media_type: str = "application/octet-stream"


@dataclass(frozen=True)
class ResolvedAssetSnapshot:
    kind: AssetKind
    scope: AssetScope
    asset_id: uuid.UUID
    version_id: uuid.UUID
    checksum: str
    catalog_generation: int
    dependency_version_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class ResolvedAgentSnapshot(ResolvedAssetSnapshot):
    payload: AgentPayload


@dataclass(frozen=True)
class ResolvedSkillSnapshot(ResolvedAssetSnapshot):
    files: tuple[SkillArchiveFile, ...]
    secret_requirements: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedMcpSnapshot(ResolvedAssetSnapshot):
    definition: Mapping[str, object]
    credential_grant_ids: tuple[uuid.UUID, ...]
