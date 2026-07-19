"""Runtime path policy tests for standalone harness usage."""

from pathlib import Path

import pytest

from deerflow.config import skills_config as skills_config_module
from deerflow.config.paths import Paths
from deerflow.config.runtime_paths import project_root
from deerflow.config.skills_config import SkillsConfig


def _clear_path_env(monkeypatch):
    for name in (
        "DEER_FLOW_CONFIG_PATH",
        "DEER_FLOW_HOME",
        "DEER_FLOW_PROJECT_ROOT",
        "DEER_FLOW_SKILLS_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_runtime_paths_resolve_from_current_project(tmp_path: Path, monkeypatch):
    _clear_path_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    (tmp_path / "skills").mkdir()

    assert Paths().base_dir == tmp_path / ".deer-flow"
    assert SkillsConfig().get_skills_path() == tmp_path / "skills"


def test_deer_flow_project_root_overrides_current_directory(tmp_path: Path, monkeypatch):
    _clear_path_env(monkeypatch)
    project_root = tmp_path / "project"
    other_cwd = tmp_path / "other"
    project_root.mkdir()
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.setenv("DEER_FLOW_PROJECT_ROOT", str(project_root))

    assert Paths().base_dir == project_root / ".deer-flow"
    assert SkillsConfig(path="custom-skills").get_skills_path() == project_root / "custom-skills"


def test_deer_flow_skills_path_overrides_project_default(tmp_path: Path, monkeypatch):
    _clear_path_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEER_FLOW_SKILLS_PATH", "team-skills")

    assert SkillsConfig().get_skills_path() == tmp_path / "team-skills"


def test_deer_flow_project_root_must_exist(tmp_path: Path, monkeypatch):
    _clear_path_env(monkeypatch)
    missing_root = tmp_path / "missing"
    monkeypatch.setenv("DEER_FLOW_PROJECT_ROOT", str(missing_root))

    with pytest.raises(ValueError, match="does not exist"):
        project_root()


def test_deer_flow_project_root_must_be_directory(tmp_path: Path, monkeypatch):
    _clear_path_env(monkeypatch)
    project_root_file = tmp_path / "project-root"
    project_root_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEER_FLOW_PROJECT_ROOT", str(project_root_file))

    with pytest.raises(ValueError, match="not a directory"):
        project_root()


def test_skills_config_falls_back_to_legacy_when_project_root_lacks_skills(tmp_path: Path, monkeypatch):
    """When DEER_FLOW_PROJECT_ROOT is unset and cwd has no `skills/`, the legacy
    repo-root candidate must be used so monorepo runs (cwd=backend/) keep finding
    `<repo>/skills` instead of `<repo>/backend/skills` (regression test for #2694)."""
    _clear_path_env(monkeypatch)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    legacy_skills = tmp_path / "legacy-repo" / "skills"
    legacy_skills.mkdir(parents=True)

    monkeypatch.setattr(
        skills_config_module,
        "_legacy_skills_candidates",
        lambda: (legacy_skills,),
    )

    assert SkillsConfig().get_skills_path() == legacy_skills


def test_skills_config_returns_project_default_when_neither_exists(tmp_path: Path, monkeypatch):
    """When nothing exists, fall back to the project-root default path so callers
    surface a stable empty location instead of silently picking a stale legacy dir."""
    _clear_path_env(monkeypatch)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    monkeypatch.setattr(skills_config_module, "_legacy_skills_candidates", lambda: ())

    assert SkillsConfig().get_skills_path() == cwd / "skills"
