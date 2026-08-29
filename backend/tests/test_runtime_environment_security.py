from pathlib import Path

import pytest

import deerflow.sandbox.env_policy as sandbox_env_policy
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


def test_runtime_environment_drops_bootstrap_model_key_from_file_and_ambient(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY=file-bootstrap-key\nACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY=file-knowledge-key\n",
        encoding="utf-8",
    )

    environment = build_runtime_environment(
        env_file,
        base_environment={
            "ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY": "ambient-bootstrap-key",
            "ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY": "ambient-knowledge-key",
            "RUNTIME_MARKER": "preserved",
        },
    )

    assert environment == {"RUNTIME_MARKER": "preserved"}


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
        "ACT_WEAVE_CHANNEL_USER_ID",
        "stale-worker-channel-user",
    )

    assert "ACT_WEAVE_CHANNEL_USER_ID" not in build_sandbox_env()


def test_sandbox_environment_inherits_only_explicit_safe_host_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sandbox_env_policy.os,
        "environ",
        {
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp/test-home",
            "LANG": "en_US.UTF-8",
            "INNOCENT_HOST_METADATA": "must-not-leak",
            "UNUSUAL_PROVIDER_AUTH": "must-not-leak-either",
        },
    )

    environment = build_sandbox_env(
        {
            "DECLARED_SKILL_VALUE": "authorized-value",
            "UNUSUAL_PROVIDER_AUTH": "authorized-override",
        }
    )

    assert environment == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/test-home",
        "LANG": "en_US.UTF-8",
        "DECLARED_SKILL_VALUE": "authorized-value",
        "UNUSUAL_PROVIDER_AUTH": "authorized-override",
    }
