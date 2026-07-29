from __future__ import annotations

import subprocess
from pathlib import Path

import review_changed_public_skills as runner


def _completed(
    command: list[str],
    *,
    stdout: bytes = b"",
    returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout=stdout,
        stderr=b"",
    )


def _write_skill(repo_root: Path, package: str) -> None:
    skill_md = repo_root / "skills" / "public" / package / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n\n# Demo\n",
        encoding="utf-8",
    )


def test_changed_support_file_reviews_owning_public_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_skill(tmp_path, "alpha")
    diff = b"M\0skills/public/alpha/references/guide.md\0"
    reviewed: list[str] = []

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: _completed(command, stdout=diff),
    )
    monkeypatch.setattr(
        runner,
        "run_review",
        lambda package, repo_root, python: reviewed.append(package.relative_to(repo_root).as_posix()) or 0,
    )

    assert (
        runner.main(
            [
                "--base-ref",
                "base",
                "--head-ref",
                "head",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert reviewed == ["skills/public/alpha"]


def test_fully_deleted_package_is_skipped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    diff = b"D\0skills/public/removed/SKILL.md\0D\0skills/public/removed/scripts/helper.py\0"
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: _completed(command, stdout=diff),
    )
    monkeypatch.setattr(
        runner,
        "run_review",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deleted package must not be reviewed")),
    )

    assert (
        runner.main(
            [
                "--before",
                "before",
                "--after",
                "after",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )


def test_deleted_skill_md_with_remaining_package_fails_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    helper = tmp_path / "skills" / "public" / "broken" / "scripts" / "helper.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("print('still here')\n", encoding="utf-8")
    diff = b"D\0skills/public/broken/SKILL.md\0M\0skills/public/broken/scripts/helper.py\0"
    reviewed: list[str] = []
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: _completed(command, stdout=diff),
    )
    monkeypatch.setattr(
        runner,
        "run_review",
        lambda package, repo_root, python: reviewed.append(package.relative_to(repo_root).as_posix()) or 1,
    )

    assert (
        runner.main(
            [
                "--before",
                "before",
                "--after",
                "after",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert reviewed == ["skills/public/broken"]


def test_only_deleted_skill_md_with_remaining_directory_is_reviewed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "skills" / "public" / "broken"
    package.mkdir(parents=True)
    (package / "README.md").write_text("residual file\n", encoding="utf-8")
    diff = b"D\0skills/public/broken/SKILL.md\0"
    reviewed: list[str] = []
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: _completed(command, stdout=diff),
    )
    monkeypatch.setattr(
        runner,
        "run_review",
        lambda selected, repo_root, python: reviewed.append(selected.relative_to(repo_root).as_posix()) or 1,
    )

    assert (
        runner.main(
            [
                "--before",
                "before",
                "--after",
                "after",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert reviewed == ["skills/public/broken"]


def test_review_invokes_deterministic_cli_with_incomplete_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "skills" / "public" / "alpha"
    package.mkdir(parents=True)
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _completed(command)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner.run_review(package, tmp_path, "test-python") == 0
    assert captured["command"] == [
        "test-python",
        "-m",
        "deerflow.skills.review.cli",
        "skills/public/alpha",
        "--format",
        "text",
        "--fail-on",
        "error",
        "--fail-on-incomplete",
    ]
