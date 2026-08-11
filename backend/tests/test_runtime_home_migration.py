from __future__ import annotations

import os
from pathlib import Path

import pytest
from migrate_runtime_home import main

from deerflow.config import runtime_home_migration
from deerflow.config.runtime_home_migration import (
    RuntimeHomeMigrationError,
    copy_runtime_home,
    migrate_runtime_home,
    plan_runtime_home_migration,
)


def _write_source(source: Path) -> int:
    source.mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "state.json").write_text('{"ready": true}\n', encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "payload.bin").write_bytes(b"\x00\x01\x02")
    return len(b'{"ready": true}\n') + 3


def test_default_migration_is_a_read_only_dry_run(tmp_path: Path) -> None:
    source = tmp_path / ".deer-flow"
    total_bytes = _write_source(source)

    result = migrate_runtime_home(tmp_path)

    assert result.sources == (source,)
    assert result.target == tmp_path / ".act-weave"
    assert result.file_count == 2
    assert result.total_bytes == total_bytes
    assert result.applied is False
    assert result.verified is False
    assert source.is_dir()
    assert not result.target.exists()


def test_default_sources_merge_non_conflicting_legacy_runtime_homes(tmp_path: Path) -> None:
    root_source = tmp_path / ".deer-flow"
    backend_source = tmp_path / "backend" / ".deer-flow"
    root_source.mkdir()
    backend_source.mkdir(parents=True)
    (root_source / "shared.json").write_text("same\n", encoding="utf-8")
    (backend_source / "shared.json").write_text("same\n", encoding="utf-8")
    (root_source / "root-only.txt").write_text("root\n", encoding="utf-8")
    (backend_source / "backend-only.txt").write_text("backend\n", encoding="utf-8")

    result = migrate_runtime_home(tmp_path, apply=True)

    assert result.sources == (root_source, backend_source)
    assert result.file_count == 3
    assert result.verified is True
    assert root_source.is_dir()
    assert backend_source.is_dir()
    assert (result.target / "shared.json").read_text(encoding="utf-8") == "same\n"
    assert (result.target / "root-only.txt").read_text(encoding="utf-8") == "root\n"
    assert (result.target / "backend-only.txt").read_text(encoding="utf-8") == "backend\n"


def test_default_sources_reject_conflicting_relative_paths(tmp_path: Path) -> None:
    root_source = tmp_path / ".deer-flow"
    backend_source = tmp_path / "backend" / ".deer-flow"
    root_source.mkdir()
    backend_source.mkdir(parents=True)
    (root_source / "state.json").write_text("root\n", encoding="utf-8")
    (backend_source / "state.json").write_text("backend\n", encoding="utf-8")

    with pytest.raises(RuntimeHomeMigrationError, match=r"冲突.*state\.json"):
        plan_runtime_home_migration(tmp_path)


def test_source_union_requires_identical_permissions_for_duplicate_paths(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "state.json").write_text("same\n", encoding="utf-8")
    (second / "state.json").write_text("same\n", encoding="utf-8")
    (first / "state.json").chmod(0o600)
    (second / "state.json").chmod(0o644)

    with pytest.raises(RuntimeHomeMigrationError, match=r"冲突.*state\.json"):
        plan_runtime_home_migration(tmp_path, source=[first, second])


def test_default_sources_require_at_least_one_legacy_runtime_home(tmp_path: Path) -> None:
    with pytest.raises(RuntimeHomeMigrationError, match="未找到"):
        plan_runtime_home_migration(tmp_path)


def test_source_and_target_must_not_be_symlinks_or_nested(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    _write_source(source)
    source_link = tmp_path / "legacy-link"
    source_link.symlink_to(source, target_is_directory=True)

    with pytest.raises(RuntimeHomeMigrationError, match="符号链接"):
        plan_runtime_home_migration(tmp_path, source=source_link)

    with pytest.raises(RuntimeHomeMigrationError, match="嵌套"):
        plan_runtime_home_migration(tmp_path, source=source, target=source / "new-home")


@pytest.mark.parametrize("target_kind", ["file", "directory", "broken_symlink"])
def test_target_must_not_exist_in_any_form(tmp_path: Path, target_kind: str) -> None:
    source = tmp_path / "legacy"
    _write_source(source)
    target = tmp_path / "new-home"
    if target_kind == "file":
        target.write_text("occupied", encoding="utf-8")
    elif target_kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "missing")

    with pytest.raises(RuntimeHomeMigrationError, match="目标路径已存在"):
        plan_runtime_home_migration(tmp_path, source=source, target=target)


def test_source_tree_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    _write_source(source)
    (source / "state-link").symlink_to(source / "state.json")

    with pytest.raises(RuntimeHomeMigrationError, match="符号链接"):
        plan_runtime_home_migration(tmp_path, source=source, target=tmp_path / "new-home")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable on this platform")
def test_source_tree_rejects_special_files(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    _write_source(source)
    os.mkfifo(source / "runtime.pipe")

    with pytest.raises(RuntimeHomeMigrationError, match="特殊文件"):
        plan_runtime_home_migration(tmp_path, source=source, target=tmp_path / "new-home")


def test_copy_verifies_manifest_and_retains_source(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    total_bytes = _write_source(source)
    target = tmp_path / "new-home"
    plan = plan_runtime_home_migration(tmp_path, source=source, target=target)

    result = copy_runtime_home(plan)

    assert result.applied is True
    assert result.verified is True
    assert result.file_count == 2
    assert result.total_bytes == total_bytes
    assert source.is_dir()
    assert (source / "state.json").read_bytes() == (target / "state.json").read_bytes()
    assert (source / "nested" / "payload.bin").read_bytes() == (target / "nested" / "payload.bin").read_bytes()
    assert (target / "empty").is_dir()
    assert not tuple(tmp_path.glob("new-home.migration-*.staging"))


def test_copy_rejects_source_changes_and_cleans_only_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "legacy"
    _write_source(source)
    target = tmp_path / "new-home"
    plan = plan_runtime_home_migration(tmp_path, source=source, target=target)
    original_build_manifest = runtime_home_migration._build_manifest
    calls = 0

    def changing_manifest(root: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            (source / "state.json").write_text("changed\n", encoding="utf-8")
        return original_build_manifest(root)

    monkeypatch.setattr(runtime_home_migration, "_build_manifest", changing_manifest)

    with pytest.raises(RuntimeHomeMigrationError, match="源目录在复制期间发生变化"):
        copy_runtime_home(plan)

    assert source.is_dir()
    assert (source / "state.json").read_text(encoding="utf-8") == "changed\n"
    assert not target.exists()
    assert not tuple(tmp_path.glob("new-home.migration-*.staging"))


def test_atomic_publish_rejects_target_creation_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "legacy"
    _write_source(source)
    target = tmp_path / "new-home"
    plan = plan_runtime_home_migration(tmp_path, source=source, target=target)
    atomic_rename = runtime_home_migration._atomic_rename_no_replace

    def create_racing_target(staging: Path, destination: Path) -> None:
        destination.mkdir()
        atomic_rename(staging, destination)

    monkeypatch.setattr(runtime_home_migration, "_atomic_rename_no_replace", create_racing_target)

    with pytest.raises(RuntimeHomeMigrationError, match="目标路径已出现"):
        copy_runtime_home(plan)

    assert source.is_dir()
    assert target.is_dir()
    assert not any(target.iterdir())
    assert not tuple(tmp_path.glob("new-home.migration-*.staging"))


def test_cli_defaults_to_dry_run_and_reports_verification(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_source(tmp_path / ".deer-flow")

    assert main(["--repository-root", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert "dry-run" in captured.out
    assert "files: 2" in captured.out
    assert "bytes:" in captured.out
    assert "verified: no" in captured.out
    assert "sources retained: yes" in captured.out
    assert not (tmp_path / ".act-weave").exists()


def test_cli_accepts_multiple_explicit_sources(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "one.txt").write_text("one", encoding="utf-8")
    (second / "two.txt").write_text("two", encoding="utf-8")

    assert (
        main(
            [
                "--repository-root",
                str(tmp_path),
                "--source",
                str(first),
                "--source",
                str(second),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert f"source: {first}" in captured.out
    assert f"source: {second}" in captured.out
    assert "files: 2" in captured.out
    assert not (tmp_path / ".act-weave").exists()
