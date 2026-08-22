"""Backend core-gate development database contract."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from support import core_gate_plugin


def test_pytest_process_never_exposes_the_development_database_to_imports() -> None:
    assert make_url(os.environ["DATABASE_URL"]).database == "deerflow_test_unit"


def test_development_database_url_accepts_the_application_database() -> None:
    url = "postgresql+asyncpg://developer:secret@127.0.0.1:5432/deerflow"

    assert core_gate_plugin.validate_development_database_url(url) == url


def test_development_database_url_falls_back_to_only_the_root_env_database_value(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://developer:secret@127.0.0.1:5432/deerflow\nAUTH_JWT_SECRET=must-not-be-loaded\n",
        encoding="utf-8",
    )
    environment: dict[str, str] = {}

    assert core_gate_plugin.resolve_development_database_url(environment=environment, env_file=env_file).endswith("/deerflow")
    assert environment == {}


def test_explicit_development_database_url_wins_over_the_root_env(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://file@127.0.0.1:5432/from_file\n",
        encoding="utf-8",
    )
    explicit = "postgresql+asyncpg://explicit@127.0.0.1:5432/deerflow"

    assert core_gate_plugin.resolve_development_database_url(environment={"DATABASE_URL": explicit}, env_file=env_file) == explicit


@pytest.mark.parametrize(
    "url",
    (
        "",
        "sqlite:///tmp/test.db",
        "postgresql+asyncpg://developer:secret@127.0.0.1:5432",
    ),
)
def test_development_database_url_rejects_missing_or_non_postgres_targets(url: str) -> None:
    with pytest.raises(ValueError):
        core_gate_plugin.validate_development_database_url(url)


def test_core_gate_reads_database_url_and_requires_zero_skips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[list[str]] = []
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://developer:secret@127.0.0.1:5432/deerflow",
    )
    monkeypatch.setenv("ACT_WEAVE_CORE_REQUIRE_ZERO_SKIPS", "0")
    monkeypatch.setattr(
        core_gate_plugin.pytest,
        "main",
        lambda args: observed.append(list(args)) or pytest.ExitCode.OK,
    )

    assert core_gate_plugin.main(["tests/", "-q"], env_file=tmp_path / "missing") == pytest.ExitCode.OK
    assert observed == [["tests/", "-q"]]


def test_core_gate_fails_before_collection_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert core_gate_plugin.main([], env_file=tmp_path / "missing") == pytest.ExitCode.USAGE_ERROR
    assert "DATABASE_URL is required" in capsys.readouterr().err
