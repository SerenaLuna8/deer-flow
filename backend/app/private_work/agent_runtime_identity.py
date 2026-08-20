"""Persisted Agent identity checks for private runtime materialization."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from app.private_work.snapshot_repository import RunSnapshotAssetStale
from app.shared_assets.models import (
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedMcpSnapshot,
    ResolvedSkillSnapshot,
)

_RUNTIME_AGENT_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


@dataclass(frozen=True, slots=True)
class AgentRuntimeIdentity:
    asset_id: uuid.UUID
    scope: AssetScope
    slug: str
    source_key: str | None

    @property
    def runtime_key(self) -> str:
        return f"{self.scope.value}/{self.slug}"


def agent_runtime_identities(
    agents: tuple[ResolvedAgentSnapshot, ...],
) -> tuple[AgentRuntimeIdentity, ...]:
    """Derive runtime identities exclusively from the admitted Run snapshot."""

    asset_ids = tuple(agent.asset_id for agent in agents)
    if not asset_ids or len(set(asset_ids)) != len(asset_ids):
        raise RunSnapshotAssetStale
    identities: list[AgentRuntimeIdentity] = []
    runtime_keys: set[str] = set()
    for agent in agents:
        if not isinstance(agent.slug, str) or _RUNTIME_AGENT_SLUG.fullmatch(agent.slug) is None:
            raise RunSnapshotAssetStale
        identity = AgentRuntimeIdentity(
            asset_id=agent.asset_id,
            scope=agent.scope,
            slug=agent.slug,
            source_key=agent.source_key,
        )
        if identity.runtime_key in runtime_keys:
            raise RunSnapshotAssetStale
        runtime_keys.add(identity.runtime_key)
        identities.append(identity)
    return tuple(identities)


def main_pool_prefix[SnapshotT: (ResolvedSkillSnapshot, ResolvedMcpSnapshot)](
    snapshots: tuple[SnapshotT, ...],
) -> tuple[SnapshotT, ...]:
    """Return the current-version prefix and reject ambiguous ordering."""

    current: list[SnapshotT] = []
    seen_asset_ids: set[uuid.UUID] = set()
    historical_started = False
    for snapshot in snapshots:
        if snapshot.asset_id in seen_asset_ids:
            historical_started = True
            continue
        if historical_started:
            # Admission writes all current/bound versions before historical
            # delegate-only versions.
            raise RunSnapshotAssetStale
        seen_asset_ids.add(snapshot.asset_id)
        current.append(snapshot)
    return tuple(current)


__all__ = [
    "AgentRuntimeIdentity",
    "agent_runtime_identities",
    "main_pool_prefix",
]
