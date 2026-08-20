from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.shared_assets.models import AssetScope, VersionRelation


@dataclass(frozen=True, slots=True)
class VersionLineageNode:
    version_id: uuid.UUID
    version_number: int
    supersedes_version_id: uuid.UUID | None


def classify_version_relations(
    *,
    scope: AssetScope,
    current_version_id: uuid.UUID | None,
    nodes: tuple[VersionLineageNode, ...],
) -> dict[uuid.UUID, VersionRelation]:
    """Derive Current/Candidate/Historical without a persisted workflow state."""

    if not nodes:
        if scope is AssetScope.PROJECT and current_version_id is None:
            return {}
        raise ValueError("asset version lineage is empty")
    if any(
        not isinstance(node.version_id, uuid.UUID)
        or not isinstance(node.version_number, int)
        or isinstance(node.version_number, bool)
        or node.version_number < 1
        or (node.supersedes_version_id is not None and not isinstance(node.supersedes_version_id, uuid.UUID))
        for node in nodes
    ):
        raise ValueError("asset version lineage is invalid")
    by_id = {node.version_id: node for node in nodes}
    if len(by_id) != len(nodes) or len({node.version_number for node in nodes}) != len(nodes):
        raise ValueError("asset version lineage is ambiguous")

    if scope is AssetScope.SYSTEM:
        only = nodes[0]
        if len(nodes) != 1 or only.version_number != 1 or only.supersedes_version_id is not None or current_version_id != only.version_id:
            raise ValueError("System asset must have one Current v1")
        return {only.version_id: VersionRelation.CURRENT}

    if scope is not AssetScope.PROJECT:
        raise ValueError("asset scope is invalid")
    if current_version_id is not None and current_version_id not in by_id:
        raise ValueError("Current Version does not belong to the asset")
    for node in nodes:
        parent_id = node.supersedes_version_id
        if parent_id is None:
            continue
        parent = by_id.get(parent_id)
        if parent is None or parent.version_number >= node.version_number:
            raise ValueError("asset version lineage is invalid")

    candidate_pool = set(by_id)
    if current_version_id is not None:
        candidate_pool = {current_version_id}
        changed = True
        while changed:
            changed = False
            for node in nodes:
                if node.supersedes_version_id in candidate_pool and node.version_id not in candidate_pool:
                    candidate_pool.add(node.version_id)
                    changed = True

    head = max(
        (by_id[version_id] for version_id in candidate_pool),
        key=lambda node: node.version_number,
    )
    head_lineage: set[uuid.UUID] = set()
    cursor: VersionLineageNode | None = head
    while cursor is not None:
        if cursor.version_id in head_lineage:
            raise ValueError("asset version lineage contains a cycle")
        head_lineage.add(cursor.version_id)
        parent_id = cursor.supersedes_version_id
        cursor = by_id.get(parent_id) if parent_id is not None else None

    relations = {node.version_id: VersionRelation.HISTORICAL for node in nodes}
    if current_version_id is None:
        for version_id in head_lineage:
            relations[version_id] = VersionRelation.CANDIDATE
        return relations

    current_number = by_id[current_version_id].version_number
    relations[current_version_id] = VersionRelation.CURRENT
    for version_id in head_lineage:
        if by_id[version_id].version_number > current_number:
            relations[version_id] = VersionRelation.CANDIDATE
    return relations


__all__ = ["VersionLineageNode", "classify_version_relations"]
