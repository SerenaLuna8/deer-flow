from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import replace

import pytest

import app.shared_assets.run_snapshot_codec as snapshot_codec
from app.private_work import snapshot_repository as snapshot_repository_module
from app.private_work.asset_runtime import _private_agent_manifest
from app.private_work.errors import PrivateWorkTooLarge
from app.private_work.snapshot_repository import RunSnapshotAssetStale
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_payload_checksum import (
    agent_payload_checksum,
    legacy_agent_payload_checksum,
)
from app.shared_assets.errors import AssetResolutionUnavailable
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
    ResolvedMcpSnapshot,
    ResolvedSkillSnapshot,
    SkillArchiveFile,
    SkillAssetRef,
    SkillSecretRequirementSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver, _ResolvedRecord
from app.shared_assets.run_snapshot_codec import (
    RunAssetSnapshotInvalid,
    RunAssetSnapshotTooLarge,
    decode_run_asset_snapshot,
    encode_run_asset_snapshot,
)
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow, SkillRow, SkillVersionRow

_MODEL_REF = "88888888-8888-4888-8888-888888888891"


class _EmptyScalarResult:
    def __init__(self, rows: list[object] | None = None) -> None:
        self._rows = [] if rows is None else rows

    def scalars(self) -> _EmptyScalarResult:
        return self

    def all(self) -> list[object]:
        return self._rows


class _EmptyRefSession:
    async def execute(self, _statement) -> _EmptyScalarResult:
        return _EmptyScalarResult()


class _ProjectSkillRefSession:
    async def execute(self, _statement) -> _EmptyScalarResult:
        return _EmptyScalarResult([("project", uuid.uuid4())])


class _SequencedRefSession:
    def __init__(self, rows: list[list[object]]) -> None:
        self._rows = iter(rows)

    async def execute(self, _statement) -> _EmptyScalarResult:
        return _EmptyScalarResult(next(self._rows))


def _snapshot() -> ResolvedAgentSnapshot:
    payload = AgentPayload(
        description="original",
        soul="soul",
        model_ref=_MODEL_REF,
        tool_groups=("task",),
        skill_refs=(),
        mcp_version_ids=(),
        agents_instructions="instructions",
        identity="identity",
        user_context="user",
        payload_schema_version=4,
    )
    return ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum=agent_payload_checksum(payload),
        catalog_generation=7,
        dependency_version_ids=(),
        skill_version_ids=(),
        payload=payload,
    )


def _skill_snapshot(
    files: tuple[SkillArchiveFile, ...] | None = None,
) -> ResolvedSkillSnapshot:
    resolved_files = files or (
        SkillArchiveFile(
            "SKILL.md",
            b"---\nname: compressed-skill\n---\nUse the references.\n",
            "text/markdown",
        ),
        SkillArchiveFile(
            "references/data.bin",
            bytes(range(256)) * 4,
            "application/octet-stream",
        ),
    )
    canonical = json.dumps(
        [
            {
                "path": item.path,
                "sha256": hashlib.sha256(item.content).hexdigest(),
                "size_bytes": len(item.content),
            }
            for item in resolved_files
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return ResolvedSkillSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum=hashlib.sha256(canonical).hexdigest(),
        catalog_generation=7,
        dependency_version_ids=(),
        files=resolved_files,
        secret_requirements=(
            SkillSecretRequirementSnapshot(
                name="API_TOKEN",
                target_env="API_TOKEN",
                optional=True,
            ),
        ),
    )


def _mcp_snapshot() -> ResolvedMcpSnapshot:
    definition: dict[str, object] = {
        "args": [],
        "command": None,
        "secret_slots": [],
        "description": "MCP",
        "env": {},
        "headers": {},
        "oauth": {},
        "routing": {},
        "timeout_seconds": 30,
        "tool_overrides": {},
        "transport": "http",
        "url": "https://mcp.example.test",
    }
    return ResolvedMcpSnapshot(
        kind=AssetKind.MCP,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum=snapshot_codec._mcp_checksum(definition),  # noqa: SLF001
        catalog_generation=7,
        dependency_version_ids=(),
        definition=definition,
        secret_generation_ids=(),
        secret_digest="a" * 64,
    )


def _v2_skill_encoding(snapshot: ResolvedSkillSnapshot) -> dict[str, object]:
    return {
        "schema_version": 2,
        "kind": snapshot.kind.value,
        "scope": snapshot.scope.value,
        "asset_id": str(snapshot.asset_id),
        "version_id": str(snapshot.version_id),
        "checksum": snapshot.checksum,
        "catalog_generation": snapshot.catalog_generation,
        "dependency_version_ids": [],
        "skill": {
            "files": [
                {
                    "path": item.path,
                    "media_type": item.media_type,
                    "content_base64": base64.b64encode(item.content).decode(
                        "ascii",
                    ),
                }
                for item in snapshot.files
            ],
            "secret_requirements": [
                {
                    "name": item.name,
                    "target_env": item.target_env,
                    "optional": item.optional,
                }
                for item in snapshot.secret_requirements
            ],
        },
    }


def test_skill_snapshot_v3_is_deterministic_compact_and_round_trips() -> None:
    snapshot = _skill_snapshot()

    first = encode_run_asset_snapshot(snapshot)
    second = encode_run_asset_snapshot(snapshot)

    assert first == second
    assert first["schema_version"] == 3
    skill = first["skill"]
    assert isinstance(skill, dict)
    assert set(skill) == {
        "archive_base64",
        "codec",
        "compressed_size",
        "content_size",
        "file_count",
        "secret_requirements",
        "uncompressed_size",
    }
    assert skill["codec"] == "canonical-frame-zlib-6"
    assert isinstance(skill["archive_base64"], str)
    assert "content_base64" not in json.dumps(first)
    assert decode_run_asset_snapshot(first) == snapshot


def test_v2_agent_skill_and_mcp_snapshots_remain_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _snapshot()
    skill = _skill_snapshot()
    mcp = _mcp_snapshot()
    encoded_agent = encode_run_asset_snapshot(agent)
    encoded_agent["schema_version"] = 2
    encoded_mcp = encode_run_asset_snapshot(mcp)
    encoded_mcp["schema_version"] = 2
    monkeypatch.setattr(snapshot_codec, "MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES", 1)

    assert decode_run_asset_snapshot(encoded_agent) == agent
    assert decode_run_asset_snapshot(_v2_skill_encoding(skill)) == skill
    decoded_mcp = decode_run_asset_snapshot(encoded_mcp)
    assert isinstance(decoded_mcp, ResolvedMcpSnapshot)
    assert decoded_mcp.asset_id == mcp.asset_id
    assert decoded_mcp.version_id == mcp.version_id
    assert decoded_mcp.checksum == mcp.checksum
    assert decoded_mcp.definition["url"] == mcp.definition["url"]


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    [
        pytest.param("MAX_SKILL_ARCHIVE_FILES", 1, id="file-count"),
        pytest.param("MAX_SKILL_ARCHIVE_FILE_BYTES", 32, id="single-file"),
        pytest.param("MAX_SKILL_ARCHIVE_BYTES", 64, id="total-size"),
    ],
)
def test_v2_skill_snapshot_preflights_limits_before_base64_decode(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
) -> None:
    encoded = _v2_skill_encoding(_skill_snapshot())
    decode_calls = 0

    def reject_decode(*_args, **_kwargs):
        nonlocal decode_calls
        decode_calls += 1
        raise AssertionError("base64 decode must not run before limit preflight")

    monkeypatch.setattr(snapshot_codec, limit_name, limit)
    monkeypatch.setattr(snapshot_codec.base64, "b64decode", reject_decode)

    with pytest.raises(RunAssetSnapshotInvalid):
        decode_run_asset_snapshot(encoded)
    assert decode_calls == 0


@pytest.mark.parametrize(
    "paths",
    [
        pytest.param((".",), id="dot"),
        pytest.param(("references/file:name.txt",), id="colon"),
        pytest.param(("C:/run.py",), id="windows-drive"),
        pytest.param(("references/cafe\u0301.txt",), id="non-nfc"),
        pytest.param(
            ("SKILL.md", "references/A.txt", "references/a.txt"),
            id="casefold-collision",
        ),
    ],
)
def test_v2_and_v3_skill_snapshots_reject_noncanonical_paths(
    paths: tuple[str, ...],
) -> None:
    files = tuple(SkillArchiveFile(path, b"content", "text/plain") for path in sorted(paths))
    snapshot = _skill_snapshot(files)

    with pytest.raises(RunAssetSnapshotInvalid):
        encode_run_asset_snapshot(snapshot)
    with pytest.raises(RunAssetSnapshotInvalid):
        decode_run_asset_snapshot(_v2_skill_encoding(snapshot))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("compressed_size", 0, id="compressed-size"),
        pytest.param("uncompressed_size", 1, id="uncompressed-size"),
        pytest.param("content_size", 1, id="content-size"),
        pytest.param("file_count", 1, id="file-count"),
        pytest.param("codec", "unknown", id="codec"),
        pytest.param("archive_base64", "not-base64!", id="base64"),
    ],
)
def test_skill_snapshot_v3_rejects_invalid_archive_declarations(
    field: str,
    value: object,
) -> None:
    encoded = encode_run_asset_snapshot(_skill_snapshot())
    skill = encoded["skill"]
    assert isinstance(skill, dict)
    skill[field] = value

    with pytest.raises(RunAssetSnapshotInvalid):
        decode_run_asset_snapshot(encoded)


def test_skill_snapshot_v3_rejects_unknown_shape_and_checksum_tampering() -> None:
    encoded = encode_run_asset_snapshot(_skill_snapshot())
    skill = encoded["skill"]
    assert isinstance(skill, dict)
    skill["unexpected"] = True

    with pytest.raises(RunAssetSnapshotInvalid):
        decode_run_asset_snapshot(encoded)

    encoded = encode_run_asset_snapshot(_skill_snapshot())
    encoded["checksum"] = "0" * 64
    with pytest.raises(RunAssetSnapshotInvalid):
        decode_run_asset_snapshot(encoded)


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    [
        pytest.param("MAX_SKILL_ARCHIVE_FILES", 1, id="file-count"),
        pytest.param("MAX_SKILL_ARCHIVE_FILE_BYTES", 32, id="single-file"),
        pytest.param("MAX_SKILL_ARCHIVE_BYTES", 64, id="total-size"),
    ],
)
def test_skill_snapshot_v3_enforces_skill_archive_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
) -> None:
    monkeypatch.setattr(snapshot_codec, limit_name, limit)

    with pytest.raises(RunAssetSnapshotInvalid):
        encode_run_asset_snapshot(_skill_snapshot())


def test_skill_snapshot_v3_rejects_decompression_beyond_declared_size() -> None:
    encoded = encode_run_asset_snapshot(_skill_snapshot())
    skill = encoded["skill"]
    assert isinstance(skill, dict)
    declared = skill["uncompressed_size"]
    assert isinstance(declared, int)
    skill["uncompressed_size"] = declared - 1

    with pytest.raises(RunAssetSnapshotInvalid):
        decode_run_asset_snapshot(encoded)


def test_skill_snapshot_v3_applies_final_json_size_gate_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = encode_run_asset_snapshot(_skill_snapshot())
    encoded_size = len(json.dumps(encoded, ensure_ascii=False).encode())
    monkeypatch.setattr(
        snapshot_codec,
        "MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES",
        encoded_size - 1,
    )

    with pytest.raises(snapshot_codec.RunAssetSnapshotTooLarge):
        encode_run_asset_snapshot(_skill_snapshot())


def test_skill_snapshot_v3_encoded_size_cap_allows_current_ppt_payload_class() -> None:
    assert snapshot_codec.MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES == 80 * 1024 * 1024


def test_encoded_run_asset_snapshot_json_size_matches_persistence_payload() -> None:
    encoded = encode_run_asset_snapshot(_skill_snapshot())

    assert snapshot_codec.encoded_run_asset_snapshot_json_size(encoded) == len(
        json.dumps(encoded, ensure_ascii=False).encode(),
    )


def test_private_runtime_manifest_recomputes_agent_payload_checksum() -> None:
    snapshot = _snapshot()
    tampered = replace(
        snapshot,
        payload=replace(snapshot.payload, description="tampered"),
    )

    with pytest.raises(RunSnapshotAssetStale):
        _private_agent_manifest(tampered, skills=(), mcps=())


def test_snapshot_persistence_maps_codec_size_limit_to_public_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = "snapshot-too-large"

    def reject_oversized(_snapshot: object) -> dict[str, object]:
        raise RunAssetSnapshotTooLarge("internal persistence budget")

    monkeypatch.setattr(
        snapshot_repository_module,
        "encode_run_asset_snapshot",
        reject_oversized,
    )

    with pytest.raises(PrivateWorkTooLarge) as caught:
        snapshot_repository_module._RunAssetSnapshotAdmissionEncoder(
            request_id=request_id,
        ).encode(_snapshot())

    assert caught.value.request_id == request_id


def test_snapshot_persistence_maps_cumulative_size_limit_to_public_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = "cumulative-snapshot-too-large"
    limit = snapshot_codec.MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES
    encoded_sizes = iter((limit // 2, limit - (limit // 2) + 1))
    monkeypatch.setattr(
        snapshot_repository_module,
        "encoded_run_asset_snapshot_json_size",
        lambda _value: next(encoded_sizes),
        raising=False,
    )
    encoder = snapshot_repository_module._RunAssetSnapshotAdmissionEncoder(
        request_id=request_id,
    )

    encoder.encode(_snapshot())
    with pytest.raises(PrivateWorkTooLarge) as caught:
        encoder.encode(_mcp_snapshot())

    assert caught.value.request_id == request_id


def _legacy_snapshot() -> ResolvedAgentSnapshot:
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    payload = AgentPayload(
        description="legacy",
        soul="legacy soul",
        model_ref=_MODEL_REF,
        tool_groups=("task",),
        skill_refs=(SkillAssetRef(AssetScope.PROJECT, skill_id),),
        mcp_version_ids=(),
        agents_instructions="legacy instructions",
        identity="legacy identity",
        user_context="legacy user",
        payload_schema_version=3,
    )
    return ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum=legacy_agent_payload_checksum(payload, (skill_version_id,)),
        catalog_generation=9,
        dependency_version_ids=(skill_version_id,),
        skill_version_ids=(skill_version_id,),
        payload=payload,
    )


def test_legacy_agent_snapshot_round_trips_and_builds_manifest() -> None:
    snapshot = _legacy_snapshot()

    decoded = decode_run_asset_snapshot(encode_run_asset_snapshot(snapshot))

    assert decoded == snapshot
    assert _private_agent_manifest(snapshot, skills=(), mcps=()).checksum == snapshot.checksum


@pytest.mark.parametrize("tamper", ["content", "skill-version"])
def test_legacy_agent_snapshot_rejects_tampering(tamper: str) -> None:
    snapshot = _legacy_snapshot()
    encoded = encode_run_asset_snapshot(snapshot)
    agent = encoded["agent"]
    assert isinstance(agent, dict)
    if tamper == "content":
        agent["description"] = "tampered"
    else:
        agent["resolved_skill_version_ids"] = [str(uuid.uuid4())]

    with pytest.raises(RunAssetSnapshotInvalid):
        decode_run_asset_snapshot(encoded)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("description", "tool_groups"),
    [
        pytest.param("tampered", ["task"], id="content-mismatch"),
        pytest.param("original", {"task": True}, id="noncanonical-json-shape"),
    ],
)
async def test_resolver_recomputes_database_agent_payload_checksum(
    description: str,
    tool_groups: object,
) -> None:
    snapshot = _snapshot()
    project_id = uuid.uuid4()
    asset = AgentRow(
        id=snapshot.asset_id,
        scope="project",
        project_id=project_id,
        slug="agent-a",
        display_name="Agent A",
        status="active",
        current_version_id=snapshot.version_id,
        revision=1,
        created_by_user_id=str(uuid.uuid4()),
    )
    version = AgentVersionRow(
        id=snapshot.version_id,
        agent_id=snapshot.asset_id,
        version_number=1,
        description=description,
        agents_instructions=snapshot.payload.agents_instructions,
        soul=snapshot.payload.soul,
        identity=snapshot.payload.identity,
        user_context=snapshot.payload.user_context,
        model_ref=snapshot.payload.model_ref,
        model_settings={},
        tool_groups=tool_groups,
        supersedes_version_id=None,
        payload_schema_version=snapshot.payload.payload_schema_version,
        payload_checksum=snapshot.checksum,
        created_by_user_id=str(uuid.uuid4()),
    )
    context = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=project_id,
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(),
        membership_version=1,
        request_id="request-agent-checksum",
    )
    resolver = ProjectAssetResolver(lambda: None)  # type: ignore[arg-type]

    with pytest.raises(AssetResolutionUnavailable) as error:
        await resolver._agent_snapshot(  # noqa: SLF001 - exact runtime integrity boundary
            _EmptyRefSession(),  # type: ignore[arg-type]
            context,
            _ResolvedRecord(AssetScope.PROJECT, asset, version),
            snapshot.catalog_generation,
        )

    assert error.value.request_id == context.request_id


@pytest.mark.asyncio
async def test_resolver_rejects_system_agent_project_skill_reference() -> None:
    snapshot = _snapshot()
    asset = AgentRow(
        id=snapshot.asset_id,
        scope="system",
        project_id=None,
        slug="system-agent",
        display_name="System Agent",
        status="active",
        current_version_id=snapshot.version_id,
        revision=1,
        source_key="builtin:agent:system-agent",
        created_by_user_id=str(uuid.uuid4()),
    )
    version = AgentVersionRow(
        id=snapshot.version_id,
        agent_id=snapshot.asset_id,
        version_number=1,
        description=snapshot.payload.description,
        agents_instructions=snapshot.payload.agents_instructions,
        soul=snapshot.payload.soul,
        identity=snapshot.payload.identity,
        user_context=snapshot.payload.user_context,
        model_ref=snapshot.payload.model_ref,
        model_settings={},
        tool_groups=list(snapshot.payload.tool_groups),
        supersedes_version_id=None,
        payload_schema_version=4,
        payload_checksum=snapshot.checksum,
        created_by_user_id=str(uuid.uuid4()),
    )
    context = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(),
        membership_version=1,
        request_id="system-agent-project-skill-ref",
    )

    with pytest.raises(AssetResolutionUnavailable):
        await ProjectAssetResolver(lambda: None)._agent_snapshot(  # noqa: SLF001
            _ProjectSkillRefSession(),  # type: ignore[arg-type]
            context,
            _ResolvedRecord(AssetScope.SYSTEM, asset, version),
            1,
        )


@pytest.mark.asyncio
async def test_resolver_projects_attested_legacy_agent_into_runtime_v4() -> None:
    project_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    current_skill_version_id = uuid.uuid4()
    asset = AgentRow(
        id=agent_id,
        scope="project",
        project_id=project_id,
        slug="legacy-agent",
        display_name="Legacy Agent",
        status="active",
        current_version_id=agent_version_id,
        revision=1,
        created_by_user_id=str(uuid.uuid4()),
    )
    version = AgentVersionRow(
        id=agent_version_id,
        agent_id=agent_id,
        version_number=3,
        description="legacy definition",
        agents_instructions="instructions",
        soul="soul",
        identity="identity",
        user_context="user",
        model_ref=_MODEL_REF,
        model_settings={},
        tool_groups=["task"],
        supersedes_version_id=None,
        payload_schema_version=3,
        payload_checksum="a" * 64,
        created_by_user_id=str(uuid.uuid4()),
    )
    skill = SkillRow(
        id=skill_id,
        scope="project",
        project_id=project_id,
        slug="current-skill",
        display_name="Current Skill",
        status="active",
        current_version_id=current_skill_version_id,
        revision=1,
        created_by_user_id=str(uuid.uuid4()),
    )
    skill_version = SkillVersionRow(
        id=current_skill_version_id,
        skill_id=skill_id,
        version_number=4,
        description="current skill",
        frontmatter={"name": "current-skill"},
        compatibility=None,
        secret_requirements=[],
        scan_decision="allow",
        scan_summary={"rule_ids": []},
        payload_checksum="b" * 64,
        created_by_user_id=str(uuid.uuid4()),
    )
    actor = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=project_id,
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(),
        membership_version=1,
        request_id="legacy-runtime-projection",
    )

    snapshot = await ProjectAssetResolver(lambda: None)._agent_snapshot(  # noqa: SLF001
        _SequencedRefSession([[("project", skill_id)], []]),  # type: ignore[arg-type]
        actor,
        _ResolvedRecord(AssetScope.PROJECT, asset, version),
        3,
        exact_dependency_records=(
            (_ResolvedRecord(AssetScope.PROJECT, skill, skill_version),),
            (),
        ),
    )

    assert snapshot.payload.payload_schema_version == 4
    assert snapshot.skill_version_ids == (current_skill_version_id,)
    assert snapshot.checksum == agent_payload_checksum(snapshot.payload)
    assert snapshot.checksum != version.payload_checksum
    assert decode_run_asset_snapshot(encode_run_asset_snapshot(snapshot)) == snapshot
