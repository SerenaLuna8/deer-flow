from __future__ import annotations

from pathlib import Path

import pytest

from scripts.migrate_automations import build_parser


def test_parser_requires_one_mode_owner_map_and_backup_dir(tmp_path: Path) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--dry-run",
                "--execute",
                "--owner-map",
                str(tmp_path / "owners.json"),
                "--backup-dir",
                str(tmp_path / "backup"),
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(["--dry-run", "--backup-dir", str(tmp_path / "backup")])

    args = parser.parse_args(
        [
            "--dry-run",
            "--owner-map",
            str(tmp_path / "owners.json"),
            "--backup-dir",
            str(tmp_path / "backup"),
        ]
    )

    assert args.dry_run is True
    assert args.execute is False


def test_parser_accepts_execute(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--execute",
            "--owner-map",
            str(tmp_path / "owners.json"),
            "--backup-dir",
            str(tmp_path / "backup"),
        ]
    )

    assert args.execute is True
    assert args.dry_run is False


def test_makefiles_expose_automation_migration_target_and_dry_run_help() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    backend_makefile = (repo_root / "backend/Makefile").read_text(encoding="utf-8")
    root_makefile = (repo_root / "Makefile").read_text(encoding="utf-8")

    assert "migrate-automations:" in backend_makefile
    assert "scripts/migrate_automations.py $(ARGS)" in backend_makefile
    assert "migrate-automations:" in root_makefile
    assert "-C backend migrate-automations" in root_makefile
    assert "make migrate-automations" in root_makefile
    assert "dry-run" in root_makefile.split("make migrate-automations", 1)[1].splitlines()[0]
