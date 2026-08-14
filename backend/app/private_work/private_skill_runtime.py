"""Run-owned private Skill tree materialization and cleanup."""

from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from app.private_work.asset_runtime_contracts import PrivateSkillManifest
from app.private_work.errors import (
    PrivateWorkUnavailable,
)
from app.private_work.snapshot_repository import RunSnapshotAssetStale
from app.shared_assets.models import (
    AssetScope,
    ResolvedSkillSnapshot,
)
from deerflow.skills.parser import parse_skill_file
from deerflow.skills.types import Skill, SkillCategory

_PRIVATE_SKILL_CLEANUP_ATTEMPTS = 3
_RUNTIME_SKILL_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class PrivateRuntimeCleanupError(RuntimeError):
    """Stable internal error for a run-owned temporary tree left behind."""


def remove_private_skill_tree(root: Path) -> None:
    """Remove one private Skill tree with bounded, retryable semantics."""

    for attempt in range(_PRIVATE_SKILL_CLEANUP_ATTEMPTS):
        try:
            shutil.rmtree(root)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt + 1 == _PRIVATE_SKILL_CLEANUP_ATTEMPTS:
                raise PrivateRuntimeCleanupError("Private runtime cleanup failed") from None


def create_private_skill_root(
    run_id: str,
    request_id: str,
) -> Path:
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:80]
    try:
        return Path(tempfile.mkdtemp(prefix=f"deerflow-private-{safe_run_id}-")).resolve()
    except OSError:
        raise PrivateWorkUnavailable(request_id) from None


def write_skill_tree(
    root: Path,
    skill_snapshots: tuple[ResolvedSkillSnapshot, ...],
) -> tuple[tuple[PrivateSkillManifest, ...], tuple[Skill, ...]]:
    manifests: list[PrivateSkillManifest] = []
    skills: list[Skill] = []
    runtime_name_assets: dict[str, uuid.UUID] = {}
    materialized_asset_ids: set[uuid.UUID] = set()
    staging_root = root / ".staging"
    staging_root.mkdir(mode=0o700, parents=False, exist_ok=False)
    for snapshot in skill_snapshots:
        staged_skill_root = staging_root / snapshot.version_id.hex
        staged_skill_root.mkdir(
            mode=0o700,
            parents=False,
            exist_ok=False,
        )
        for archive_file in snapshot.files:
            relative = Path(archive_file.path)
            if relative.is_absolute() or ".." in relative.parts:
                raise RunSnapshotAssetStale
            destination = (staged_skill_root / relative).resolve()
            if staged_skill_root.resolve() not in destination.parents:
                raise RunSnapshotAssetStale
            destination.parent.mkdir(
                mode=0o700,
                parents=True,
                exist_ok=True,
            )
            destination.write_bytes(archive_file.content)
            destination.chmod(0o600)
        parsed = parse_skill_file(
            staged_skill_root / "SKILL.md",
            SkillCategory.CUSTOM,
            Path(snapshot.asset_id.hex),
        )
        if parsed is None or _RUNTIME_SKILL_NAME.fullmatch(parsed.name) is None or (parsed.name in runtime_name_assets and runtime_name_assets[parsed.name] != snapshot.asset_id):
            raise RunSnapshotAssetStale
        runtime_name_assets[parsed.name] = snapshot.asset_id
        category = SkillCategory.PUBLIC if snapshot.scope is AssetScope.SYSTEM else SkillCategory.CUSTOM
        first_asset_version = snapshot.asset_id not in materialized_asset_ids
        materialized_asset_ids.add(snapshot.asset_id)
        base_relative_root = parsed.name if category is SkillCategory.PUBLIC else snapshot.asset_id.hex
        relative_root = base_relative_root if first_asset_version else (f".versions/{snapshot.asset_id.hex}/{snapshot.version_id.hex}")
        skill_root = root / category.value / relative_root
        skill_root.parent.mkdir(
            mode=0o700,
            parents=True,
            exist_ok=True,
        )
        if skill_root.exists():
            raise RunSnapshotAssetStale
        staged_skill_root.rename(skill_root)
        manifests.append(
            PrivateSkillManifest(
                asset_id=snapshot.asset_id,
                version_id=snapshot.version_id,
                relative_root=relative_root,
            )
        )
        skills.append(
            replace(
                parsed,
                skill_dir=skill_root,
                skill_file=skill_root / "SKILL.md",
                relative_path=Path(relative_root),
                category=category,
                enabled=True,
                runtime_read_only=True,
            )
        )
    staging_root.rmdir()
    return tuple(manifests), tuple(skills)


__all__ = [
    "PrivateRuntimeCleanupError",
    "create_private_skill_root",
    "remove_private_skill_tree",
    "write_skill_tree",
]
