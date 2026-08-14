"""Secret-free value contracts for a materialized private Agent runtime."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.shared_assets.models import AgentModelSettings


@dataclass(frozen=True, slots=True)
class PrivateSkillManifest:
    asset_id: uuid.UUID
    version_id: uuid.UUID
    relative_root: str


@dataclass(frozen=True, slots=True)
class PrivateMcpManifest:
    asset_id: uuid.UUID
    version_id: uuid.UUID
    definition: dict[str, object]


@dataclass(frozen=True, slots=True, repr=False)
class PrivateAgentManifest:
    agent_asset_id: uuid.UUID
    agent_version_id: uuid.UUID
    checksum: str
    catalog_generation: int
    description: str
    payload_schema_version: int
    agents_instructions: str
    soul: str
    identity: str
    user_context: str
    model_ref: str
    tool_groups: tuple[str, ...]
    skills: tuple[PrivateSkillManifest, ...]
    mcps: tuple[PrivateMcpManifest, ...]
    model_settings: AgentModelSettings = AgentModelSettings()
    runtime_key: str | None = None

    def __repr__(self) -> str:
        return (
            "PrivateAgentManifest("
            f"agent_asset_id={self.agent_asset_id!r}, "
            f"agent_version_id={self.agent_version_id!r}, "
            f"checksum={self.checksum!r}, "
            f"catalog_generation={self.catalog_generation!r}, "
            f"payload_schema_version={self.payload_schema_version!r}, "
            f"model_ref={self.model_ref!r}, "
            f"runtime_key={self.runtime_key!r}, "
            f"tool_groups={self.tool_groups!r}, "
            f"skill_count={len(self.skills)!r}, "
            f"mcp_count={len(self.mcps)!r})"
        )


__all__ = [
    "PrivateAgentManifest",
    "PrivateMcpManifest",
    "PrivateSkillManifest",
]
