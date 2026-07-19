from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.migrate_assets import (
    AssetMigrationError,
    InventoryItem,
    MigrationCursor,
    OwnerMap,
    SourceLayout,
    _stored_agent_payload,
    _stored_mcp_definition,
    build_inventory,
    build_migration_parser,
    create_secure_backup,
    resolve_data_root,
    validate_executable_inventory,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_inventory_marks_legacy_shared_owner_unresolved(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    _write(repo / "skills/public/demo/SKILL.md", "---\nname: demo\ndescription: demo\n---\nbody\n")
    _write(data / "skills/custom/private/SKILL.md", "---\nname: private\n---\nprivate\n")
    inventory = build_inventory(SourceLayout(repo_root=repo, data_root=data), OwnerMap({}))

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


def test_default_runtime_home_and_repo_default_agent_are_discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    data = repo / ".deer-flow"
    monkeypatch.delenv("DEER_FLOW_HOME", raising=False)
    _write(
        repo / "config.yaml",
        "models:\n  - name: first-model\n    use: example:Model\ntool_groups:\n  - name: web\n  - name: bash\n",
    )
    _write(data / "SOUL.md", "Default operator soul.\n")
    _write(repo / "skills/public/demo/SKILL.md", "---\nname: demo\ndescription: demo\n---\nbody\n")

    assert resolve_data_root(repo, None) == data
    inventory = build_inventory(
        SourceLayout(repo_root=repo, data_root=resolve_data_root(repo, None)),
        OwnerMap({}, system_actor=str(uuid.uuid4())),
    )
    default_agent = next(item for item in inventory if item.source_key == "system-agent:lead-agent")

    assert default_agent.source_label == "repo-default-agent"
    assert default_agent.payload["model_ref"] == "first-model"
    assert default_agent.payload["tool_groups"] == ("web", "bash")
    assert default_agent.payload["skill_slugs"] is None  # omitted means all enabled skills
    assert default_agent.payload["soul"] == "Default operator soul.\n"
    assert {file.archive_path for file in default_agent.files} == {"config.yaml", "SOUL.md"}


def test_source_and_backup_parent_symlinks_cannot_bypass_nofollow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_source = tmp_path / "real-source"
    _write(real_source / "SKILL.md", "---\nname: safe\n---\nbody\n")
    linked_source = tmp_path / "linked-source"
    linked_source.symlink_to(real_source, target_is_directory=True)
    # Simulate a check/use race: path-level checks saw a regular parent, but
    # the actual open must still reject the now-symlinked component.
    monkeypatch.setattr(Path, "is_symlink", lambda _self: False)
    with pytest.raises(AssetMigrationError, match="safely|symlink"):
        InventoryItem.for_skill(
            source_key="system-skill:parent-link",
            slug="parent-link",
            display_name="Parent link",
            scope="system",
            project_id=None,
            owner_user_id=str(uuid.uuid4()),
            files=(linked_source / "SKILL.md",),
        )

    source = real_source / "SKILL.md"
    item = InventoryItem.for_skill(
        source_key="system-skill:write-parent-link",
        slug="write-parent-link",
        display_name="Write parent link",
        scope="system",
        project_id=None,
        owner_user_id=str(uuid.uuid4()),
        files=(source,),
    )
    real_backup = tmp_path / "real-backup"
    real_backup.mkdir()
    linked_backup = tmp_path / "linked-backup"
    linked_backup.symlink_to(real_backup, target_is_directory=True)
    with pytest.raises(AssetMigrationError, match="backup path|secure backup"):
        create_secure_backup((item,), linked_backup, run_id=uuid.uuid4())


def test_inventory_rejects_symlink_anywhere_in_selected_skill_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "skills/public/demo"
    _write(skill / "SKILL.md", "---\nname: demo\n---\nbody\n")
    outside = tmp_path / "outside"
    _write(outside / "payload.txt", "payload")
    (skill / "linked-dir").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AssetMigrationError, match="symlink"):
        build_inventory(
            SourceLayout(repo_root=repo, data_root=tmp_path / "data"),
            OwnerMap({}, system_actor=str(uuid.uuid4())),
        )


def test_agent_skills_omitted_or_null_mean_all_enabled_but_empty_means_none(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for name, skills_line in (("omitted", ""), ("null", "skills: null\n"), ("empty", "skills: []\n")):
        _write(repo / f"agents/{name}/config.yaml", f"name: {name}\n{skills_line}")
        _write(repo / f"agents/{name}/SOUL.md", f"{name} soul")

    inventory = build_inventory(
        SourceLayout(repo_root=repo, data_root=tmp_path / "data"),
        OwnerMap({}, system_actor=str(uuid.uuid4())),
    )
    by_slug = {item.slug: item for item in inventory if item.kind == "agent"}

    assert by_slug["omitted"].payload["skill_slugs"] is None
    assert by_slug["null"].payload["skill_slugs"] is None
    assert by_slug["empty"].payload["skill_slugs"] == []


def test_stored_agent_and_mcp_payloads_are_canonically_reconstructed() -> None:
    skill_version_id = uuid.uuid4()
    mcp_version_id = uuid.uuid4()
    agent = SimpleNamespace(
        description="stored description",
        soul="stored soul",
        model_ref="stored-model",
        tool_groups=["web"],
    )
    payload = _stored_agent_payload(agent, (skill_version_id,), (mcp_version_id,))
    assert payload.description == "stored description"
    assert payload.soul == "stored soul"
    assert payload.skill_version_ids == (skill_version_id,)
    agent.description = "tampered"
    assert _stored_agent_payload(agent, (skill_version_id,), (mcp_version_id,)) != payload

    mcp = SimpleNamespace(
        description="stored MCP",
        transport="http",
        command=None,
        args=[],
        url="https://example.invalid/mcp",
        non_secret_env={},
        non_secret_headers={},
        oauth_metadata={"client_id": "client"},
        routing={"mode": "prefer", "priority": 1, "keywords": ["demo"]},
        tool_overrides={},
        timeout_seconds=30,
    )
    slot = SimpleNamespace(name="legacy-secrets", purpose="auth", payload_schema={"headers": ["Authorization"]}, required=True)
    definition = _stored_mcp_definition(mcp, (slot,))
    assert definition.description == "stored MCP"
    assert definition.credential_slots[0].payload_schema == {"headers": ("Authorization",)}
    mcp.timeout_seconds = 31
    assert _stored_mcp_definition(mcp, (slot,)) != definition


def test_cli_normalizes_unexpected_failure_without_secret_or_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import migrate_assets

    repo = tmp_path / "repo"
    _write(repo / "skills/public/demo/SKILL.md", "---\nname: demo\n---\nbody\n")

    async def fail(*_args, **_kwargs):
        raise RuntimeError("SQL params plain-token ciphertext nonce")

    monkeypatch.setattr(migrate_assets, "_run_cli", fail)
    assert migrate_assets.main(["--execute", "--repo-root", str(repo), "--data-root", str(tmp_path / "data")]) == 1
    output = capsys.readouterr()
    assert output.err.strip() == "asset migration failed safely"
    assert "plain-token" not in output.out + output.err
    assert "ciphertext" not in output.out + output.err
    assert "nonce" not in output.out + output.err
    assert "Traceback" not in output.out + output.err
