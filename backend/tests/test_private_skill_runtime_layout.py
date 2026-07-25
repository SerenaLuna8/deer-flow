from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.private_work.asset_runtime import _write_skill_tree
from app.private_work.snapshot_repository import RunSnapshotAssetStale
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    ResolvedSkillSnapshot,
    SkillArchiveFile,
)
from deerflow.skills.types import SkillCategory


def _snapshot(
    *,
    scope: AssetScope,
    name: str,
    asset_id: uuid.UUID | None = None,
) -> ResolvedSkillSnapshot:
    selected_asset_id = asset_id or uuid.uuid4()
    manifest = (f"---\nname: {name}\ndescription: {name} runtime fixture\n---\n# Runtime fixture\n").encode()
    return ResolvedSkillSnapshot(
        kind=AssetKind.SKILL,
        scope=scope,
        asset_id=selected_asset_id,
        version_id=uuid.uuid4(),
        checksum="a" * 64,
        catalog_generation=7,
        dependency_version_ids=(),
        files=(
            SkillArchiveFile(
                path="SKILL.md",
                content=manifest,
                media_type="text/markdown",
            ),
            SkillArchiveFile(
                path="scripts/run.py",
                content=b"print('ok')\n",
                media_type="text/x-python",
            ),
        ),
        secret_requirements=(),
    )


def test_write_skill_tree_preserves_public_paths_and_project_isolation(
    tmp_path: Path,
) -> None:
    system = _snapshot(scope=AssetScope.SYSTEM, name="image-generation")
    project = _snapshot(scope=AssetScope.PROJECT, name="project-helper")

    manifests, skills = _write_skill_tree(tmp_path, (system, project))

    assert [skill.category for skill in skills] == [
        SkillCategory.PUBLIC,
        SkillCategory.CUSTOM,
    ]
    assert skills[0].get_container_path() == "/mnt/skills/public/image-generation"
    assert skills[1].get_container_path() == (f"/mnt/skills/custom/{project.asset_id.hex}")
    assert (tmp_path / "public" / "image-generation" / "scripts" / "run.py").read_text() == "print('ok')\n"
    assert (tmp_path / "custom" / project.asset_id.hex / "scripts" / "run.py").read_text() == "print('ok')\n"
    assert [manifest.relative_root for manifest in manifests] == [
        "image-generation",
        project.asset_id.hex,
    ]
    assert all(skill.runtime_read_only for skill in skills)


def test_write_skill_tree_rejects_duplicate_runtime_names(tmp_path: Path) -> None:
    system = _snapshot(scope=AssetScope.SYSTEM, name="shared-name")
    project = _snapshot(scope=AssetScope.PROJECT, name="shared-name")

    with pytest.raises(RunSnapshotAssetStale):
        _write_skill_tree(tmp_path, (system, project))
