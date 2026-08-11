from __future__ import annotations

from pathlib import Path

import detect_uv_extras
import pytest
import support_bundle
from local_runtime_paths import resolve_environment_path

from deerflow.config.app_config import AppConfig
from deerflow.config.paths import Paths
from deerflow.config.runtime_paths import project_root, runtime_home
from deerflow.config.skills_config import SkillsConfig
from deerflow.runtime.events.store.jsonl import JsonlRunEventStore
from scripts import rotate_credentials, run_runtime

LOCAL_PATH_ENV_NAMES = (
    "ACT_WEAVE_PROJECT_ROOT",
    "DEER_FLOW_PROJECT_ROOT",
    "ACT_WEAVE_HOME",
    "DEER_FLOW_HOME",
    "ACT_WEAVE_CONFIG_PATH",
    "DEER_FLOW_CONFIG_PATH",
    "ACT_WEAVE_SKILLS_PATH",
    "DEER_FLOW_SKILLS_PATH",
)


@pytest.fixture(autouse=True)
def _clear_local_path_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LOCAL_PATH_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_runtime_home_defaults_to_project_act_weave_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert project_root() == tmp_path
    assert runtime_home() == tmp_path / ".act-weave"
    assert Paths().base_dir == tmp_path / ".act-weave"
    assert JsonlRunEventStore()._base_dir == tmp_path / ".act-weave"


def test_runtime_path_aliases_accept_same_normalized_value_and_prefer_act_weave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACT_WEAVE_PROJECT_ROOT", ".")
    monkeypatch.setenv("DEER_FLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ACT_WEAVE_HOME", ".act-weave")
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path / ".act-weave"))

    assert project_root() == tmp_path
    assert runtime_home() == tmp_path / ".act-weave"
    assert Paths().base_dir == tmp_path / ".act-weave"


@pytest.mark.parametrize(
    ("canonical_name", "legacy_name", "call"),
    [
        ("ACT_WEAVE_PROJECT_ROOT", "DEER_FLOW_PROJECT_ROOT", project_root),
        ("ACT_WEAVE_HOME", "DEER_FLOW_HOME", runtime_home),
    ],
)
def test_runtime_path_aliases_reject_normalized_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_name: str,
    legacy_name: str,
    call: object,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(canonical_name, str(first))
    monkeypatch.setenv(legacy_name, str(second))

    with pytest.raises(ValueError, match=rf"{canonical_name}.*{legacy_name}"):
        call()  # type: ignore[operator]


def test_config_and_skills_aliases_use_act_weave_name_and_reject_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("config_version: 1\n", encoding="utf-8")
    skills = tmp_path / "skills-a"
    skills.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("ACT_WEAVE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ACT_WEAVE_CONFIG_PATH", str(config))
    monkeypatch.setenv("ACT_WEAVE_SKILLS_PATH", str(skills))

    assert AppConfig.resolve_config_path() == config
    assert SkillsConfig().get_skills_path() == skills

    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(tmp_path / "other.yaml"))
    with pytest.raises(ValueError, match="ACT_WEAVE_CONFIG_PATH.*DEER_FLOW_CONFIG_PATH"):
        AppConfig.resolve_config_path()

    monkeypatch.delenv("DEER_FLOW_CONFIG_PATH")
    monkeypatch.setenv("DEER_FLOW_SKILLS_PATH", str(other))
    with pytest.raises(ValueError, match="ACT_WEAVE_SKILLS_PATH.*DEER_FLOW_SKILLS_PATH"):
        SkillsConfig().get_skills_path()


def test_standard_library_alias_resolver_normalizes_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    environment = {
        "ACT_WEAVE_HOME": ".act-weave",
        "DEER_FLOW_HOME": str(tmp_path / ".act-weave"),
    }
    assert (
        resolve_environment_path(
            "ACT_WEAVE_HOME",
            "DEER_FLOW_HOME",
            environment=environment,
            base=tmp_path,
        )
        == tmp_path / ".act-weave"
    )

    environment["DEER_FLOW_HOME"] = str(tmp_path / ".deer-flow")
    with pytest.raises(ValueError, match="ACT_WEAVE_HOME.*DEER_FLOW_HOME"):
        resolve_environment_path(
            "ACT_WEAVE_HOME",
            "DEER_FLOW_HOME",
            environment=environment,
            base=tmp_path,
        )


def test_backend_role_environment_exports_both_aliases_with_canonical_defaults(
    tmp_path: Path,
) -> None:
    environment = run_runtime.build_runtime_environment(
        tmp_path / "missing.env",
        base_environment={},
        repository_root=tmp_path,
    )

    assert environment["ACT_WEAVE_PROJECT_ROOT"] == str(tmp_path)
    assert environment["DEER_FLOW_PROJECT_ROOT"] == str(tmp_path)
    assert environment["ACT_WEAVE_HOME"] == str(tmp_path / ".act-weave")
    assert environment["DEER_FLOW_HOME"] == str(tmp_path / ".act-weave")
    assert environment["ACT_WEAVE_CONFIG_PATH"] == str(tmp_path / "config.yaml")
    assert environment["DEER_FLOW_CONFIG_PATH"] == str(tmp_path / "config.yaml")


def test_backend_role_environment_rejects_alias_conflict(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ACT_WEAVE_HOME.*DEER_FLOW_HOME"):
        run_runtime.build_runtime_environment(
            tmp_path / "missing.env",
            base_environment={
                "ACT_WEAVE_HOME": str(tmp_path / "new"),
                "DEER_FLOW_HOME": str(tmp_path / "old"),
            },
            repository_root=tmp_path,
        )


@pytest.mark.parametrize("legacy_relative", [".deer-flow", "backend/.deer-flow"])
def test_backend_role_environment_requires_explicit_migration_before_new_default(
    tmp_path: Path,
    legacy_relative: str,
) -> None:
    (tmp_path / legacy_relative).mkdir(parents=True)

    with pytest.raises(run_runtime.RuntimeHomeMigrationRequired, match="migrate-runtime-home"):
        run_runtime.build_runtime_environment(
            tmp_path / "missing.env",
            base_environment={},
            repository_root=tmp_path,
        )

    compatible = run_runtime.build_runtime_environment(
        tmp_path / "missing.env",
        base_environment={"DEER_FLOW_HOME": str(tmp_path / legacy_relative)},
        repository_root=tmp_path,
    )
    assert compatible["ACT_WEAVE_HOME"] == str(tmp_path / legacy_relative)
    assert compatible["DEER_FLOW_HOME"] == str(tmp_path / legacy_relative)


def test_config_detector_supports_new_alias_and_rejects_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.yaml"
    other = tmp_path / "other.yaml"
    config.write_text("config_version: 1\n", encoding="utf-8")
    other.write_text("config_version: 1\n", encoding="utf-8")
    monkeypatch.setenv("ACT_WEAVE_CONFIG_PATH", str(config))

    assert detect_uv_extras.find_config_file() == config

    monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(other))
    with pytest.raises(ValueError, match="ACT_WEAVE_CONFIG_PATH.*DEER_FLOW_CONFIG_PATH"):
        detect_uv_extras.find_config_file()


def test_local_artifact_defaults_use_act_weave_home_and_names(tmp_path: Path) -> None:
    support_path = support_bundle._default_out_path(tmp_path, environment={})
    ledger_path = rotate_credentials._default_rotation_ledger_path(
        repository_root=tmp_path,
        environment={},
    )

    assert support_path.parent == tmp_path / ".act-weave" / "support-bundles"
    assert support_path.name.startswith("actweave-support-bundle-")
    assert support_path.suffix == ".zip"
    assert ledger_path == tmp_path / ".act-weave" / "migrations" / "credentials"


def test_local_artifact_defaults_honor_home_alias_and_reject_conflict(
    tmp_path: Path,
) -> None:
    custom = tmp_path / "custom-home"
    environment = {
        "ACT_WEAVE_HOME": "custom-home",
        "DEER_FLOW_HOME": str(custom),
    }

    assert (
        support_bundle._default_out_path(
            tmp_path,
            environment=environment,
        ).parent
        == custom / "support-bundles"
    )
    assert (
        rotate_credentials._default_rotation_ledger_path(
            repository_root=tmp_path,
            environment=environment,
        )
        == custom / "migrations" / "credentials"
    )

    environment["DEER_FLOW_HOME"] = str(tmp_path / "different")
    with pytest.raises(ValueError, match="ACT_WEAVE_HOME.*DEER_FLOW_HOME"):
        support_bundle._default_out_path(tmp_path, environment=environment)
    with pytest.raises(ValueError, match="ACT_WEAVE_HOME.*DEER_FLOW_HOME"):
        rotate_credentials._default_rotation_ledger_path(
            repository_root=tmp_path,
            environment=environment,
        )


def test_repository_local_runtime_path_contract() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    serve = (repository_root / "scripts/serve.sh").read_text(encoding="utf-8")
    root_makefile = (repository_root / "Makefile").read_text(encoding="utf-8")
    backend_makefile = (repository_root / "backend/Makefile").read_text(encoding="utf-8")
    config_upgrade = (repository_root / "scripts/config-upgrade.sh").read_text(
        encoding="utf-8",
    )
    gitignore = (repository_root / ".gitignore").read_text(encoding="utf-8")

    assert 'DEFAULT_RUNTIME_HOME="$REPO_ROOT/.act-weave"' in serve
    assert "ACT_WEAVE_PROJECT_ROOT" in serve
    assert "ACT_WEAVE_HOME" in serve
    assert "ACT_WEAVE_CONFIG_PATH" in serve
    assert 'BACKEND_RUNTIME_HOME="$REPO_ROOT/backend/.deer-flow"' not in serve
    assert "migrate-runtime-home" in root_makefile
    clean_recipe = root_makefile.split("clean: stop", maxsplit=1)[1].split(
        "# Docker development",
        maxsplit=1,
    )[0]
    assert "rm -rf .act-weave" not in clean_recipe
    assert "rm -rf backend/.deer-flow" not in clean_recipe
    assert "preserving .act-weave runtime state" in clean_recipe
    assert "../.act-weave/blocking-io-findings.json" in backend_makefile
    assert "ACT_WEAVE_CONFIG_PATH" in config_upgrade
    assert "DEER_FLOW_CONFIG_PATH" in config_upgrade
    assert ".act-weave/" in gitignore
    assert ".deer-flow/" in gitignore


def test_replay_gateway_freezes_both_home_and_config_aliases() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    source = (repository_root / "backend/scripts/run_replay_gateway.py").read_text(
        encoding="utf-8",
    )

    assert 'os.environ["ACT_WEAVE_HOME"] = str(home)' in source
    assert 'os.environ["DEER_FLOW_HOME"] = str(home)' in source
    assert 'os.environ["ACT_WEAVE_CONFIG_PATH"] = str(cfg)' in source
    assert 'os.environ["DEER_FLOW_CONFIG_PATH"] = str(cfg)' in source
