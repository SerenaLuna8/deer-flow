from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedRunAssetClosure,
    ResolvedSkillVersionSnapshot,
    SkillSecretRequirementSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver
from app.shared_assets.run_snapshot_codec import (
    RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION,
    RunAssetSnapshotInvalid,
    RunAssetSnapshotTooLarge,
    decode_run_skill_version_manifest,
    encode_run_skill_version_manifest,
)
from deerflow.persistence.shared_assets import SkillVersionRow


def _resolved() -> ResolvedSkillVersionSnapshot:
    return ResolvedSkillVersionSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope.PROJECT,
        asset_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        version_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        checksum="a" * 64,
        catalog_generation=218,
        dependency_version_ids=(),
        file_count=12_922,
        content_size_bytes=79_243_541,
        secret_requirements=(
            SkillSecretRequirementSnapshot(
                name="example",
                target_env="EXAMPLE_TOKEN",
                optional=False,
            ),
        ),
    )


def test_v4_skill_manifest_is_byte_free_and_decodes_exact_facts() -> None:
    encoded = encode_run_skill_version_manifest(_resolved())

    assert encoded == {
        "schema_version": RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION,
        "kind": "skill",
        "scope": "project",
        "asset_id": "11111111-1111-1111-1111-111111111111",
        "version_id": "22222222-2222-2222-2222-222222222222",
        "checksum": "a" * 64,
        "catalog_generation": 218,
        "dependency_version_ids": [],
        "skill": {
            "source": "skill_version_ref",
            "file_count": 12_922,
            "content_size_bytes": 79_243_541,
        },
    }
    assert "EXAMPLE_TOKEN" not in str(encoded)

    decoded = decode_run_skill_version_manifest(encoded)
    assert decoded.asset_id == _resolved().asset_id
    assert decoded.version_id == _resolved().version_id
    assert decoded.file_count == 12_922
    assert decoded.content_size_bytes == 79_243_541


def test_run_closure_accepts_metadata_only_skill_versions() -> None:
    skill = _resolved()
    agent = ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        version_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        checksum="b" * 64,
        catalog_generation=218,
        dependency_version_ids=(skill.version_id,),
        payload=AgentPayload(
            description="",
            soul="metadata only",
            model_ref="55555555-5555-4555-8555-555555555555",
            tool_groups=(),
            skill_refs=(),
            mcp_version_ids=(),
        ),
        skill_version_ids=(skill.version_id,),
    )

    closure = ResolvedRunAssetClosure(
        lead_agent=agent,
        delegated_agents=(),
        skills=(skill,),
        mcps=(),
        main_skill_version_ids=(skill.version_id,),
        main_mcp_version_ids=(),
    )

    assert closure.skills == (skill,)


@pytest.mark.asyncio
async def test_resolver_builds_skill_version_metadata_without_file_query() -> None:
    skill = _resolved()
    version = SkillVersionRow(
        id=skill.version_id,
        skill_id=skill.asset_id,
        version_number=1,
        description="metadata",
        frontmatter={},
        secret_requirements=[
            {
                "name": "example",
                "target_env": "EXAMPLE_TOKEN",
                "optional": False,
            }
        ],
        scan_decision="allow",
        scan_summary={},
        payload_checksum=skill.checksum,
        file_count=skill.file_count,
        content_size_bytes=skill.content_size_bytes,
        files_sealed=True,
        created_by_user_id="66666666-6666-4666-8666-666666666666",
    )
    record = SimpleNamespace(
        scope=skill.scope,
        asset=SimpleNamespace(id=skill.asset_id),
        version=version,
    )

    snapshot = await ProjectAssetResolver(
        lambda: None,  # type: ignore[arg-type]
    )._skill_version_snapshot(
        object(),  # type: ignore[arg-type]
        SimpleNamespace(request_id="metadata-only"),  # type: ignore[arg-type]
        record,  # type: ignore[arg-type]
        skill.catalog_generation,
    )

    assert snapshot == _resolved()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("top", "files", []),
        ("top", "content_base64", ""),
        ("skill", "files", []),
        ("skill", "archive_base64", ""),
        ("skill", "content_base64", ""),
        ("skill", "codec", "canonical-frame-zlib-6"),
        ("skill", "compressed_size", 1),
        ("skill", "secret_requirements", []),
    ],
)
def test_v4_skill_manifest_rejects_unknown_or_byte_bearing_fields(
    section: str,
    field: str,
    value: object,
) -> None:
    encoded = encode_run_skill_version_manifest(_resolved())
    if section == "top":
        encoded[field] = value
    else:
        encoded["skill"] = {**encoded["skill"], field: value}  # type: ignore[arg-type]

    with pytest.raises(RunAssetSnapshotInvalid):
        decode_run_skill_version_manifest(encoded)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "inline"),
        ("file_count", 0),
        ("file_count", 16_385),
        ("file_count", True),
        ("content_size_bytes", -1),
        ("content_size_bytes", 104_857_601),
        ("content_size_bytes", True),
    ],
)
def test_v4_skill_manifest_rejects_invalid_source_and_facts(
    field: str,
    value: object,
) -> None:
    encoded = encode_run_skill_version_manifest(_resolved())
    encoded["skill"] = {**encoded["skill"], field: value}  # type: ignore[arg-type]

    with pytest.raises(RunAssetSnapshotInvalid):
        decode_run_skill_version_manifest(encoded)


def test_v4_skill_manifest_rejects_more_than_256_kib() -> None:
    encoded = encode_run_skill_version_manifest(_resolved())
    encoded["dependency_version_ids"] = [str(uuid.UUID(int=index)) for index in range(8_000)]

    with pytest.raises(RunAssetSnapshotTooLarge):
        decode_run_skill_version_manifest(encoded)
