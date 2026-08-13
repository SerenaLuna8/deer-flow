from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.shared_assets import skill_service as skill_service_module
from app.shared_assets.bootstrap import catalog as catalog_module
from app.shared_assets.bootstrap import service as bootstrap_service
from app.shared_assets.bootstrap.skill_archive import (
    dump_skill_archive,
    load_skill_archive,
)
from app.shared_assets.models import SkillArchiveFile
from deerflow.skills.skillscan import StaticScannerError


def _entry(source_key: str, version: int) -> catalog_module.BootstrapEntry:
    slug = source_key.rsplit(":", 1)[-1]
    return catalog_module.BootstrapEntry.model_validate(
        {
            "source_key": source_key,
            "kind": "skill",
            "slug": slug,
            "display_name": slug,
            "version": version,
            "payload_path": f"content/{slug}-v{version}.skill.json",
            "payload_format": "skill_archive_v1",
            "sha256": "a" * 64,
        }
    )


@pytest.mark.asyncio
async def test_exact_history_rejects_catalog_owned_extra_version() -> None:
    source_key = "builtin:skill:exact-history"
    entry = _entry(source_key, 1)
    asset_id = bootstrap_service._stable_id(source_key)
    expected_version_id = bootstrap_service._version_id(entry)
    extra_version_id = uuid.uuid4()
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                all=lambda: [
                    (expected_version_id, 1),
                    (extra_version_id, 2),
                ]
            )
        )
    )

    with pytest.raises(
        bootstrap_service.BootstrapConflict,
        match="release history",
    ):
        await bootstrap_service._assert_exact_version_history(
            session,
            bootstrap_service.SkillVersionRow,
            bootstrap_service.SkillVersionRow.skill_id,
            asset_id,
            (entry,),
        )


@pytest.mark.asyncio
async def test_exact_history_accepts_only_expected_release_ids_and_numbers() -> None:
    source_key = "builtin:skill:exact-history"
    entries = (_entry(source_key, 1), _entry(source_key, 2))
    asset_id = bootstrap_service._stable_id(source_key)
    expected = [(bootstrap_service._version_id(entry), entry.version) for entry in entries]
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: expected)))

    await bootstrap_service._assert_exact_version_history(
        session,
        bootstrap_service.SkillVersionRow,
        bootstrap_service.SkillVersionRow.skill_id,
        asset_id,
        entries,
    )

    statement = session.execute.await_args.args[0]
    assert statement._for_update_arg is not None
    assert list(statement._for_update_arg.of) == [
        bootstrap_service.SkillVersionRow.__table__,
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", True),
        ("timeout_seconds", 1.0),
        ("timeout_seconds", "1"),
    ],
)
def test_authenticated_mcp_payload_rejects_coerced_timeout(
    field: str,
    value: object,
) -> None:
    payload = {
        "transport": "stdio",
        field: value,
    }

    with pytest.raises(bootstrap_service.BootstrapCatalogError):
        bootstrap_service._decode_json(
            bootstrap_service._McpPayload,
            json.dumps(payload).encode(),
        )


@pytest.mark.parametrize("required", [1, 0, 1.0, "true"])
def test_authenticated_mcp_payload_rejects_coerced_slot_required(
    required: object,
) -> None:
    payload = {
        "transport": "stdio",
        "credential_slots": [
            {
                "name": "token",
                "payload_schema": {},
                "required": required,
            }
        ],
    }

    with pytest.raises(bootstrap_service.BootstrapCatalogError):
        bootstrap_service._decode_json(
            bootstrap_service._McpPayload,
            json.dumps(payload).encode(),
        )


def test_legacy_scan_metadata_drift_does_not_rewrite_history() -> None:
    entry = _entry("builtin:skill:legacy-scan", 1)
    preview = SimpleNamespace(
        scan_decision="warn",
        scan_summary={
            "rule_ids": ["new-rule"],
            "severity_counts": {"LOW": 1},
        },
    )

    assert bootstrap_service._entry_scan_snapshot(
        entry,
        preview,
        is_latest=True,
    ) == (preview.scan_decision, preview.scan_summary)


def test_latest_snapshotted_release_must_match_current_scanner() -> None:
    raw = _entry("builtin:skill:snapshotted-scan", 1).model_dump()
    raw.update(
        {
            "scan_decision": "allow",
            "scan_summary": {"rule_ids": [], "severity_counts": {}},
        }
    )
    entry = bootstrap_service.BootstrapEntry.model_validate(raw)
    preview = SimpleNamespace(
        scan_decision="warn",
        scan_summary={
            "rule_ids": ["new-rule"],
            "severity_counts": {"LOW": 1},
        },
    )

    with pytest.raises(
        bootstrap_service.BootstrapCatalogError,
        match="scan snapshot is stale",
    ):
        bootstrap_service._entry_scan_snapshot(
            entry,
            preview,
            is_latest=True,
        )


def test_historical_snapshotted_release_uses_immutable_manifest_snapshot() -> None:
    raw = _entry("builtin:skill:snapshotted-history", 1).model_dump()
    manifest_summary = {"rule_ids": [], "severity_counts": {}}
    raw.update(
        {
            "scan_decision": "allow",
            "scan_summary": manifest_summary,
        }
    )
    entry = bootstrap_service.BootstrapEntry.model_validate(raw)
    preview = SimpleNamespace(
        scan_decision="warn",
        scan_summary={
            "rule_ids": ["new-rule"],
            "severity_counts": {"LOW": 1},
        },
    )

    assert bootstrap_service._entry_scan_snapshot(
        entry,
        preview,
        is_latest=False,
    ) == ("allow", manifest_summary)


@pytest.mark.asyncio
async def test_historical_snapshot_skips_future_scanner_but_latest_release_is_scanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "historical-scan-boundary"
    source_key = f"builtin:skill:{name}"

    def release_payload(version: int, marker: str) -> bytes:
        return dump_skill_archive(
            (
                SkillArchiveFile(
                    path="SKILL.md",
                    media_type="text/markdown",
                    content=(f"---\nname: {name}\ndescription: Version {version}.\n---\n\n# {name}\n\nrelease-marker: {marker}\n").encode(),
                ),
            )
        )

    payload_one = release_payload(1, "historical")
    payload_two = release_payload(2, "latest")

    def snapshotted_entry(version: int, payload: bytes) -> catalog_module.BootstrapEntry:
        raw = _entry(source_key, version).model_dump()
        raw.update(
            {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "scan_decision": "allow",
                "scan_summary": {"rule_ids": [], "severity_counts": {}},
            }
        )
        return catalog_module.BootstrapEntry.model_validate(raw)

    entry_one = snapshotted_entry(1, payload_one)
    entry_two = snapshotted_entry(2, payload_two)
    catalog = catalog_module.BootstrapCatalog.model_validate(
        {
            "schema_version": 3,
            "entries": [entry_one.model_dump(), entry_two.model_dump()],
        }
    )
    catalog._payloads = MappingProxyType(
        {
            (source_key, 1): payload_one,
            (source_key, 2): payload_two,
        }
    )

    normalized_files = {
        entry.version: bootstrap_service.normalize_skill_files(
            load_skill_archive(payload),
            request_id=source_key,
        )
        for entry, payload in (
            (entry_one, payload_one),
            (entry_two, payload_two),
        )
    }
    previews = {
        entry.version: bootstrap_service._validated_skill_preview(
            entry,
            normalized_files[entry.version],
        )
        for entry in (entry_one, entry_two)
    }

    asset_id = bootstrap_service._stable_id(source_key)
    version_ids = {entry.version: bootstrap_service._version_id(entry) for entry in (entry_one, entry_two)}
    asset = SimpleNamespace(
        id=asset_id,
        scope="system",
        project_id=None,
        slug=name,
        display_name=name,
        status="active",
        version=2,
        source_key=source_key,
        created_by_user_id=str(bootstrap_service.BUILTIN_ASSET_USER_ID),
        current_published_version_id=version_ids[2],
    )

    def version_row(version: int) -> SimpleNamespace:
        preview = previews[version]
        return SimpleNamespace(
            id=version_ids[version],
            skill_id=asset_id,
            version_number=version,
            workflow_status="published",
            description=preview.description,
            frontmatter=dict(preview.frontmatter),
            compatibility=preview.compatibility,
            secret_requirements=[{"name": requirement.name, "optional": requirement.optional} for requirement in preview.secret_requirements],
            scan_decision="allow",
            scan_summary={"rule_ids": [], "severity_counts": {}},
            supersedes_version_id=version_ids[version - 1] if version > 1 else None,
            payload_checksum=preview.checksum,
            submitted_at=None,
            reviewed_at=None,
            reviewed_by_user_id=None,
            review_note=None,
            created_by_user_id=str(bootstrap_service.BUILTIN_ASSET_USER_ID),
        )

    versions = {version: version_row(version) for version in (1, 2)}

    def persisted_file_result(version: int) -> SimpleNamespace:
        rows = [
            SimpleNamespace(
                skill_version_id=version_ids[version],
                path=file.path,
                media_type=file.media_type,
                size_bytes=len(file.content),
                sha256=hashlib.sha256(file.content).hexdigest(),
                content=file.content,
            )
            for file in normalized_files[version]
        ]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    history_result = SimpleNamespace(all=lambda: [(version_ids[1], 1), (version_ids[2], 2)])

    class Session:
        execute = AsyncMock(
            side_effect=[
                persisted_file_result(1),
                persisted_file_result(2),
                history_result,
            ]
        )

        async def get(self, _model, version_id):
            return next(row for version, row in versions.items() if version_ids[version] == version_id)

    preflight = Mock(wraps=skill_service_module._preflight_skill_frontmatter)
    checksum = Mock(wraps=skill_service_module._snapshot_checksum)
    scanner_calls: list[str] = []

    def future_scanner(root: Path, *, skill_name: str):
        marker = "historical" if "release-marker: historical" in (root / "SKILL.md").read_text() else "latest"
        scanner_calls.append(marker)
        if marker == "historical":
            raise StaticScannerError("future scanner cannot evaluate the historical release")
        return {"findings": [], "blocked": False, "scanner_errors": []}

    monkeypatch.setattr(skill_service_module, "_preflight_skill_frontmatter", preflight)
    monkeypatch.setattr(skill_service_module, "_snapshot_checksum", checksum)
    monkeypatch.setattr(skill_service_module, "enforce_static_scan_result", future_scanner)
    monkeypatch.setattr(
        bootstrap_service.asyncio,
        "to_thread",
        AsyncMock(side_effect=lambda function, *args, **kwargs: function(*args, **kwargs)),
    )
    monkeypatch.setattr(
        bootstrap_service,
        "_existing_asset",
        AsyncMock(return_value=asset),
    )

    assert await bootstrap_service._seed_skill(Session(), catalog, entry_one) is False
    assert await bootstrap_service._seed_skill(Session(), catalog, entry_two) is False
    assert scanner_calls == ["latest"]
    assert preflight.call_count == 2
    assert checksum.call_count == 2


@pytest.mark.asyncio
async def test_bootstrap_archives_retired_actweave_docs_mcp() -> None:
    catalog = catalog_module.BootstrapCatalog.model_validate(
        {
            "schema_version": 3,
            "entries": [_entry("builtin:skill:kept-skill", 1).model_dump(mode="json")],
        }
    )
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        status="active",
        source_key="builtin:mcp:deerflow-docs",
    )
    executed: list[object] = []

    class Session:
        async def execute(self, statement):
            executed.append(statement)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [asset]))

        async def flush(self) -> None:
            return None

    await bootstrap_service._retire_removed_system_mcps(Session(), catalog)

    assert asset.status == "archived"
    assert executed
