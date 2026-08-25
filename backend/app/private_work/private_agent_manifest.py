"""Secret-free private Agent manifest projection."""

from __future__ import annotations

from app.private_work.asset_runtime_contracts import (
    PrivateAgentManifest,
    PrivateMcpManifest,
    PrivateSkillManifest,
)
from app.private_work.snapshot_repository import RunSnapshotAssetStale
from app.shared_assets.agent_payload_checksum import (
    resolved_agent_payload_checksum_matches,
)
from app.shared_assets.models import ResolvedAgentSnapshot


def build_private_agent_manifest(
    agent: ResolvedAgentSnapshot,
    *,
    skills: tuple[PrivateSkillManifest, ...],
    mcps: tuple[PrivateMcpManifest, ...],
    runtime_key: str | None = None,
) -> PrivateAgentManifest:
    """Build the secret-free runtime manifest from one exact snapshot."""

    if not resolved_agent_payload_checksum_matches(
        agent.payload,
        agent.checksum,
        skill_version_ids=agent.skill_version_ids,
    ):
        raise RunSnapshotAssetStale
    return PrivateAgentManifest(
        agent_asset_id=agent.asset_id,
        agent_definition_id=agent.version_id,
        checksum=agent.checksum,
        catalog_generation=agent.catalog_generation,
        description=agent.payload.description,
        payload_schema_version=agent.payload.payload_schema_version,
        agents_instructions=agent.payload.agents_instructions,
        soul=agent.payload.soul,
        identity=agent.payload.identity,
        user_context=agent.payload.user_context,
        model_ref=agent.payload.model_ref,
        model_settings=agent.payload.model_settings,
        tool_groups=agent.payload.tool_groups,
        skills=skills,
        mcps=mcps,
        runtime_key=runtime_key,
    )


__all__ = ["build_private_agent_manifest"]
