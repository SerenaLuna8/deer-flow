from pathlib import Path

import pytest

from deerflow.sandbox.env_policy import build_sandbox_env
from scripts.run_runtime import build_runtime_environment


def test_runtime_environment_drops_installation_admin_credentials(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql://runtime@localhost/actweave\nPOSTGRES_ADMIN_URL=postgresql://admin:file-secret@localhost/postgres\n",
        encoding="utf-8",
    )

    environment = build_runtime_environment(
        env_file,
        base_environment={
            "POSTGRES_ADMIN_URL": "postgresql://admin:ambient-secret@localhost/postgres",
            "RUNTIME_MARKER": "preserved",
        },
    )

    assert environment["DATABASE_URL"] == "postgresql://runtime@localhost/actweave"
    assert environment["RUNTIME_MARKER"] == "preserved"
    assert "POSTGRES_ADMIN_URL" not in environment


def test_sandbox_environment_drops_ambient_postgres_admin_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "POSTGRES_ADMIN_URL",
        "postgresql://admin:ambient-secret@localhost/postgres",
    )

    assert "POSTGRES_ADMIN_URL" not in build_sandbox_env()


def test_sandbox_environment_drops_ambient_channel_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DEERFLOW_CHANNEL_USER_ID",
        "stale-worker-channel-user",
    )

    assert "DEERFLOW_CHANNEL_USER_ID" not in build_sandbox_env()
