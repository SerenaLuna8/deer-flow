from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from scripts.migrate_assets import (
    AssetMigrationError,
    InventoryItem,
    MigrationCursor,
    OwnerMap,
    SourceLayout,
    build_inventory,
    build_migration_parser,
    create_secure_backup,
    render_inventory,
    validate_executable_inventory,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_inventory_is_redacted_and_marks_legacy_shared_owner_unresolved(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    _write(repo / "skills/public/demo/SKILL.md", "---\nname: demo\ndescription: demo\n---\nbody\n")
    _write(data / "skills/custom/private/SKILL.md", "---\nname: private\n---\nprivate\n")
    _write(
        repo / "extensions_config.json",
        json.dumps(
            {
                "mcpServers": {
                    "private-api": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "plain-token"},
                    }
                }
            }
        ),
    )

    inventory = build_inventory(SourceLayout(repo_root=repo, data_root=data), OwnerMap({}))
    output = render_inventory(inventory)

    assert "plain-token" not in output
    assert "Authorization" not in output
    assert "https://example.invalid/mcp" not in output
    assert any(item.status == "unresolved_owner" for item in inventory)
    with pytest.raises(AssetMigrationError, match="unresolved_owner"):
        validate_executable_inventory(inventory)


def test_migration_parser_requires_exactly_one_mode_and_validates_batch_resume() -> None:
    parser = build_migration_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["--dry-run", "--execute"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--dry-run", "--batch-size", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--execute", "--resume-cursor", "not-a-uuid"])
    args = parser.parse_args(["--dry-run"])
    assert args.dry_run is True and args.execute is False
    assert args.batch_size == 100 and args.resume_cursor is None
    cursor = MigrationCursor(uuid.uuid4())
    args = parser.parse_args(["--execute", "--batch-size", "5", "--resume-cursor", str(cursor.item_id)])
    assert args.batch_size == 5 and args.resume_cursor == cursor


def test_user_custom_source_without_default_project_fails_closed(tmp_path: Path) -> None:
    user_id = str(uuid.uuid4())
    data = tmp_path / "data"
    _write(data / f"users/{user_id}/skills/custom/private/SKILL.md", "---\nname: private\n---\nbody\n")
    inventory = build_inventory(SourceLayout(tmp_path / "repo", data), OwnerMap({}))
    item = next(item for item in inventory if item.slug == "private")
    assert item.status == "unresolved_owner"
    with pytest.raises(AssetMigrationError, match="unresolved_owner"):
        validate_executable_inventory(inventory)


def test_explicit_legacy_owner_mapping_allows_execute(tmp_path: Path) -> None:
    user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    data = tmp_path / "data"
    _write(data / "skills/custom/private/SKILL.md", "---\nname: private\n---\nbody\n")

    inventory = build_inventory(
        SourceLayout(repo_root=tmp_path / "repo", data_root=data),
        OwnerMap({user_id: project_id}, legacy_shared_owner=user_id),
    )

    validate_executable_inventory(inventory)
    item = next(item for item in inventory if item.slug == "private")
    assert item.scope == "project"
    assert item.project_id == project_id
    assert item.owner_user_id == user_id


def test_secure_backup_uses_private_modes_and_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")
    item = InventoryItem.for_skill(
        source_key="system-skill:demo",
        slug="demo",
        display_name="Demo",
        scope="system",
        project_id=None,
        owner_user_id=str(uuid.uuid4()),
        files=(source,),
    )

    backup = create_secure_backup((item,), tmp_path / "migrations", run_id=uuid.uuid4())

    assert os.stat(backup.run_dir).st_mode & 0o777 == 0o700
    assert os.stat(backup.backup_dir).st_mode & 0o777 == 0o700
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in backup.files)
    assert backup.ledger_path.read_text(encoding="utf-8").find("payload") == -1
    assert os.stat(backup.ledger_path).st_mode & 0o777 == 0o600

    link = tmp_path / "linked.txt"
    link.symlink_to(source)
    with pytest.raises(AssetMigrationError, match="safely|symlink"):
        linked = InventoryItem.for_skill(
            source_key="system-skill:linked",
            slug="linked",
            display_name="Linked",
            scope="system",
            project_id=None,
            owner_user_id=item.owner_user_id,
            files=(link,),
        )
        create_secure_backup((linked,), tmp_path / "other", run_id=uuid.uuid4())


def test_inventory_checksum_changes_when_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("first", encoding="utf-8")
    first = InventoryItem.for_skill(
        source_key="system-skill:demo",
        slug="demo",
        display_name="Demo",
        scope="system",
        project_id=None,
        owner_user_id=str(uuid.uuid4()),
        files=(source,),
    )
    source.write_text("second", encoding="utf-8")
    second = InventoryItem.for_skill(
        source_key=first.source_key,
        slug=first.slug,
        display_name=first.display_name,
        scope=first.scope,
        project_id=None,
        owner_user_id=first.owner_user_id,
        files=(source,),
    )
    assert first.checksum != second.checksum
