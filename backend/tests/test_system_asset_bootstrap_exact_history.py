from __future__ import annotations

import hashlib
import json
import uuid
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.shared_assets.bootstrap import catalog as catalog_module
from app.shared_assets.bootstrap import service as bootstrap_service
from app.shared_assets.bootstrap.skill_archive import (
    dump_skill_archive,
    load_skill_archive,
)
from app.shared_assets.models import SkillArchiveFile


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
    entries = (_entry(source_key, 1),)
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
        "secret_slots": [
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


@pytest.mark.asyncio
async def test_system_skill_bootstrap_ignores_legacy_scan_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry("builtin:skill:legacy-columns", 1)
    archive = dump_skill_archive(
        (
            SkillArchiveFile(
                path="SKILL.md",
                media_type="text/markdown",
                content=b"---\nname: legacy-columns\ndescription: Legacy columns.\n---\n",
            ),
        )
    )
    entry = entry.model_copy(update={"sha256": hashlib.sha256(archive).hexdigest()})
    catalog = catalog_module.BootstrapCatalog.model_validate({"schema_version": 3, "entries": [entry.model_dump(mode="json")]})
    catalog._payloads = MappingProxyType({(entry.source_key, 1): archive})
    files = bootstrap_service.normalize_skill_files(
        load_skill_archive(archive),
        request_id=entry.source_key,
    )
    preview = bootstrap_service._validated_skill_preview(entry, files)
    asset_id = bootstrap_service._stable_id(entry.source_key)
    version_id = bootstrap_service._version_id(entry)
    asset = SimpleNamespace(
        id=asset_id,
        scope="system",
        project_id=None,
        slug=entry.slug,
        display_name=entry.display_name,
        status="active",
        current_version_id=version_id,
        revision=1,
        source_key=entry.source_key,
        created_by_user_id=str(bootstrap_service.BUILTIN_ASSET_USER_ID),
    )
    version = SimpleNamespace(
        id=version_id,
        skill_id=asset_id,
        version_number=1,
        description=preview.description,
        frontmatter=dict(preview.frontmatter),
        compatibility=preview.compatibility,
        secret_requirements=[],
        scan_decision="block",
        scan_summary={"rule_ids": ["legacy-rule"]},
        supersedes_version_id=None,
        payload_checksum=preview.checksum,
        created_by_user_id=str(bootstrap_service.BUILTIN_ASSET_USER_ID),
    )
    persisted_files = bootstrap_service._skill_file_rows(version_id, files)
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [version])),
                SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: persisted_files)),
            ]
        )
    )
    monkeypatch.setattr(
        bootstrap_service,
        "_existing_asset",
        AsyncMock(return_value=asset),
    )

    assert await bootstrap_service._seed_skill(session, catalog, entry) is False


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
