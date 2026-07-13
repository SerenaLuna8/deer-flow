from __future__ import annotations

import uuid

import pytest

from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    ResolvedMcpSnapshot,
    ResolvedSkillSnapshot,
    SkillArchiveFile,
)


def _must_not_open_session():
    raise AssertionError("type validation must run before database access")


@pytest.mark.asyncio
async def test_materializer_rejects_skill_snapshots_before_database_access() -> None:
    from app.shared_assets.resolver import ProjectAssetResolver

    resolver = ProjectAssetResolver(_must_not_open_session)
    snapshot = ResolvedSkillSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="a" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        files=(SkillArchiveFile("SKILL.md", b"---\nname: demo\n---\n"),),
        secret_requirements=(),
    )

    with pytest.raises(AssetValidationFailed):
        await resolver.materialize_mcp_secrets(snapshot)


def test_materialized_secrets_never_render_plaintext_in_repr() -> None:
    from app.shared_assets.resolver import MaterializedMcpSecrets

    materialized = MaterializedMcpSecrets(
        mcp_version_id=uuid.uuid4(),
        by_slot={"primary": {"env": {"API_TOKEN": "do-not-render"}}},
    )

    assert "do-not-render" not in repr(materialized)
    assert materialized.by_slot["primary"]["env"]["API_TOKEN"] == "do-not-render"


@pytest.mark.asyncio
async def test_materializer_rejects_duplicate_grant_references_before_database_access() -> None:
    from app.shared_assets.resolver import ProjectAssetResolver

    grant_id = uuid.uuid4()
    snapshot = ResolvedMcpSnapshot(
        kind=AssetKind.MCP,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="b" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        definition={"transport": "http"},
        credential_grant_ids=(grant_id, grant_id),
    )

    with pytest.raises(AssetValidationFailed):
        await ProjectAssetResolver(_must_not_open_session).materialize_mcp_secrets(snapshot)
