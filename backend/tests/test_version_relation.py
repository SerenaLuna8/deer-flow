from __future__ import annotations

import uuid

import pytest

from app.shared_assets.models import AssetScope, VersionRelation
from app.shared_assets.version_relation import (
    VersionLineageNode,
    classify_version_relations,
)


def _id(value: int) -> uuid.UUID:
    return uuid.UUID(int=value)


def test_project_relations_are_derived_from_current_pointer_and_forward_lineage() -> None:
    nodes = (
        VersionLineageNode(_id(1), 1, None),
        VersionLineageNode(_id(2), 2, _id(1)),
        VersionLineageNode(_id(3), 3, _id(2)),
        VersionLineageNode(_id(4), 4, _id(3)),
    )

    assert classify_version_relations(
        scope=AssetScope.PROJECT,
        current_version_id=_id(3),
        nodes=nodes,
    ) == {
        _id(1): VersionRelation.HISTORICAL,
        _id(2): VersionRelation.HISTORICAL,
        _id(3): VersionRelation.CURRENT,
        _id(4): VersionRelation.CANDIDATE,
    }


def test_first_activation_candidates_are_the_single_forward_head_lineage() -> None:
    nodes = (
        VersionLineageNode(_id(1), 1, None),
        VersionLineageNode(_id(2), 2, _id(1)),
        VersionLineageNode(_id(3), 3, _id(1)),
    )

    assert classify_version_relations(
        scope=AssetScope.PROJECT,
        current_version_id=None,
        nodes=nodes,
    ) == {
        _id(1): VersionRelation.CANDIDATE,
        _id(2): VersionRelation.HISTORICAL,
        _id(3): VersionRelation.CANDIDATE,
    }


def test_newer_numbered_old_branch_stays_historical() -> None:
    nodes = (
        VersionLineageNode(_id(1), 1, None),
        VersionLineageNode(_id(2), 2, _id(1)),
        VersionLineageNode(_id(3), 3, _id(2)),
        VersionLineageNode(_id(4), 4, _id(1)),
    )

    assert classify_version_relations(
        scope=AssetScope.PROJECT,
        current_version_id=_id(2),
        nodes=nodes,
    ) == {
        _id(1): VersionRelation.HISTORICAL,
        _id(2): VersionRelation.CURRENT,
        _id(3): VersionRelation.CANDIDATE,
        _id(4): VersionRelation.HISTORICAL,
    }


def test_system_requires_one_current_v1() -> None:
    node = VersionLineageNode(_id(1), 1, None)
    assert classify_version_relations(
        scope=AssetScope.SYSTEM,
        current_version_id=_id(1),
        nodes=(node,),
    ) == {_id(1): VersionRelation.CURRENT}

    with pytest.raises(ValueError, match="System asset must have one Current v1"):
        classify_version_relations(
            scope=AssetScope.SYSTEM,
            current_version_id=_id(1),
            nodes=(node, VersionLineageNode(_id(2), 2, _id(1))),
        )
