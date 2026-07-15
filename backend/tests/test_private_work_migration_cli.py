from __future__ import annotations

from pathlib import Path

import pytest

from scripts.migrate_private_work import build_parser


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
    assert (args.repo_root / "backend").is_dir()


def test_parser_accepts_execute_and_explicit_data_root(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--execute",
            "--owner-map",
            str(tmp_path / "owners.json"),
            "--backup-dir",
            str(tmp_path / "backup"),
            "--data-root",
            str(tmp_path / "data"),
        ]
    )

    assert args.execute is True
    assert args.dry_run is False
    assert args.data_root == tmp_path / "data"


def test_makefiles_expose_private_work_migration_target() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    backend_makefile = (repo_root / "backend/Makefile").read_text(encoding="utf-8")
    root_makefile = (repo_root / "Makefile").read_text(encoding="utf-8")

    assert "migrate-private-work:" in backend_makefile
    assert "scripts/migrate_private_work.py $(ARGS)" in backend_makefile
    assert "migrate-private-work:" in root_makefile
    assert "-C backend migrate-private-work" in root_makefile
    assert "make migrate-private-work" in root_makefile
