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


class VersionRelation(StrEnum):
    CURRENT = "current"
    CANDIDATE = "candidate"
    HISTORICAL = "historical"


@dataclass(frozen=True)
class SkillAssetRef:
    scope: AssetScope
    asset_id: uuid.UUID


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
    skill_refs: tuple[SkillAssetRef, ...]
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
class SkillSecretRequirementSnapshot:
    name: str
    optional: bool


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
    skill_version_ids: tuple[uuid.UUID, ...]
    slug: str = ""
    source_key: str | None = None


@dataclass(frozen=True)
class ResolvedSkillSnapshot(ResolvedAssetSnapshot):
    files: tuple[SkillArchiveFile, ...]
    secret_requirements: tuple[SkillSecretRequirementSnapshot, ...]


@dataclass(frozen=True)
class ResolvedMcpSnapshot(ResolvedAssetSnapshot):
    definition: Mapping[str, object]
    secret_generation_ids: tuple[uuid.UUID, ...]
    secret_digest: str


@dataclass(frozen=True)
class ResolvedRunAssetClosure:
    """Exact immutable asset closure admitted for one Run.

    ``skills`` and ``mcps`` deliberately keep the canonical Main Agent's
    current project pool as a prefix. The explicit boundary lets the Worker
    expose that pool to Main while retaining the exact dependency closure
    admitted for every delegated Agent. Agent and Skill dependencies are
    always resolved from Current Version; MCP keeps exact release semantics.
    """

    lead_agent: ResolvedAgentSnapshot
    delegated_agents: tuple[ResolvedAgentSnapshot, ...]
    skills: tuple[ResolvedSkillSnapshot, ...]
    mcps: tuple[ResolvedMcpSnapshot, ...]
    main_skill_version_ids: tuple[uuid.UUID, ...]
    main_mcp_version_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        agents = (self.lead_agent, *self.delegated_agents)
        snapshots: tuple[ResolvedAssetSnapshot, ...] = (
            *agents,
            *self.skills,
            *self.mcps,
        )
        if (
            type(self.lead_agent) is not ResolvedAgentSnapshot
            or any(type(item) is not ResolvedAgentSnapshot for item in self.delegated_agents)
            or any(type(item) is not ResolvedSkillSnapshot for item in self.skills)
            or any(type(item) is not ResolvedMcpSnapshot for item in self.mcps)
            or any(not isinstance(value, uuid.UUID) for value in self.main_skill_version_ids)
            or any(not isinstance(value, uuid.UUID) for value in self.main_mcp_version_ids)
        ):
            raise TypeError("run asset closure contains an invalid snapshot")
        if len({item.version_id for item in agents}) != len(agents):
            raise ValueError("run asset closure contains duplicate Agent versions")
        if len({item.version_id for item in self.skills}) != len(self.skills):
            raise ValueError("run asset closure contains duplicate Skill versions")
        if len({item.version_id for item in self.mcps}) != len(self.mcps):
            raise ValueError("run asset closure contains duplicate MCP versions")
        if tuple(item.version_id for item in self.skills[: len(self.main_skill_version_ids)]) != self.main_skill_version_ids:
            raise ValueError("Main Skill versions must be the Skill closure prefix")
        if tuple(item.version_id for item in self.mcps[: len(self.main_mcp_version_ids)]) != self.main_mcp_version_ids:
            raise ValueError("Main MCP versions must be the MCP closure prefix")
        if any(item.catalog_generation != self.lead_agent.catalog_generation for item in snapshots):
            raise ValueError("run asset closure must use one catalog generation")
