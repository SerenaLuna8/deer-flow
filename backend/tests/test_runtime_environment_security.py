import sys
from pathlib import Path

import pytest

import deerflow.sandbox.env_policy as sandbox_env_policy
from deerflow.sandbox.env_policy import build_sandbox_env
from scripts import run_runtime
from scripts.run_runtime import (
    build_database_upgrade_environment,
    build_runtime_environment,
)


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


def test_runtime_environment_drops_bootstrap_credentials_from_file_and_ambient(
    tmp_path: Path,
) -> None:
    # Current bootstrap names (DeepSeek System Model plus the M9 default Model
    # Provider key/skip pair) and the retired M8 knowledge names must all stay
    # installation-only: runtime roles never inherit them from file or ambient.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY=file-bootstrap-key\n"
        "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY=file-provider-key\n"
        "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP=1\n"
        "ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY=file-knowledge-key\n"
        "ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_SKIP=1\n"
        "ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT=file-storage:9000\n"
        "ACT_WEAVE_KNOWLEDGE_MINIO_BUCKET=file-bucket\n"
        "ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY=file-storage-access\n"
        "ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY=file-storage-secret\n",
        encoding="utf-8",
    )

    environment = build_runtime_environment(
        env_file,
        base_environment={
            "ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY": "ambient-bootstrap-key",
            "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY": "ambient-provider-key",
            "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP": "1",
            "ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY": "ambient-knowledge-key",
            "ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_SKIP": "1",
            "ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT": "ambient-storage:9000",
            "ACT_WEAVE_KNOWLEDGE_MINIO_BUCKET": "ambient-bucket",
            "ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY": "ambient-storage-access",
            "ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY": "ambient-storage-secret",
            "RUNTIME_MARKER": "preserved",
        },
    )

    assert environment == {"RUNTIME_MARKER": "preserved"}


def test_database_upgrade_environment_allows_only_database_url_and_safe_os_names(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql://file-user:file-secret@localhost/actweave\n"
        "ACT_WEAVE_SECRET_KEY=file-envelope-key\n"
        "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY=file-bootstrap-key\n"
        "ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY=file-storage-secret\n"
        "POSTGRES_ADMIN_URL=postgresql://admin:file-secret@localhost/postgres\n"
        "OPENAI_API_KEY=file-provider-key\n"
        "UNLISTED_PROVIDER_TOKEN=file-unlisted-provider-key\n",
        encoding="utf-8",
    )

    environment = build_database_upgrade_environment(
        env_file,
        base_environment={
            "DATABASE_URL": "postgresql://runtime@localhost/actweave",
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "TMPDIR": "/tmp/actweave-tests",
            "ACT_WEAVE_SECRET_KEY": "ambient-envelope-key",
            "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY": "ambient-bootstrap-key",
            "ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY": "ambient-storage-secret",
            "POSTGRES_ADMIN_URL": "postgresql://admin:ambient-secret@localhost/postgres",
            "OPENAI_API_KEY": "ambient-provider-key",
            "UNLISTED_PROVIDER_TOKEN": "ambient-unlisted-provider-key",
            "RUNTIME_MARKER": "must-not-cross-upgrade-boundary",
        },
    )

    assert environment == {
        "DATABASE_URL": "postgresql://runtime@localhost/actweave",
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "TMPDIR": "/tmp/actweave-tests",
    }


def test_database_upgrade_environment_loads_only_database_url_from_file(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql://runtime@localhost/actweave\nACT_WEAVE_SECRET_KEY=file-envelope-key\nARBITRARY_CREDENTIAL=file-secret\n",
        encoding="utf-8",
    )

    environment = build_database_upgrade_environment(
        env_file,
        base_environment={"PATH": "/usr/bin:/bin"},
    )

    assert environment == {
        "DATABASE_URL": "postgresql://runtime@localhost/actweave",
        "PATH": "/usr/bin:/bin",
    }


def test_database_upgrade_environment_rejects_case_variant_names_on_posix(
    tmp_path: Path,
) -> None:
    if run_runtime.os.name == "nt":
        pytest.skip("Windows environment names are case-insensitive")

    environment = build_database_upgrade_environment(
        tmp_path / "missing.env",
        base_environment={
            "database_url": "postgresql://lowercase-secret@localhost/actweave",
            "Path": "/untrusted/bin",
            "PATH": "/usr/bin:/bin",
        },
    )

    assert environment == {"PATH": "/usr/bin:/bin"}


def test_database_upgrade_mode_executes_with_the_minimal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql://runtime@localhost/actweave\nACT_WEAVE_SECRET_KEY=file-envelope-key\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def execvpe(program: str, command: list[str], environment: dict[str, str]) -> None:
        captured.update(
            program=program,
            command=command,
            environment=environment,
        )

    monkeypatch.setattr(
        run_runtime.os,
        "environ",
        {
            "PATH": "/usr/bin:/bin",
            "ACT_WEAVE_SECRET_KEY": "ambient-envelope-key",
            "POSTGRES_ADMIN_URL": "ambient-admin-url",
            "UNKNOWN_MODEL_CREDENTIAL": "ambient-provider-key",
        },
    )
    monkeypatch.setattr(run_runtime.os, "execvpe", execvpe)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_runtime.py",
            "--env-file",
            str(env_file),
            "--database-upgrade",
            "--",
            "python",
            "-m",
            "scripts.upgrade_postgres",
        ],
    )

    assert run_runtime.main() == 0
    assert captured == {
        "program": "python",
        "command": ["python", "-m", "scripts.upgrade_postgres"],
        "environment": {
            "DATABASE_URL": "postgresql://runtime@localhost/actweave",
            "PATH": "/usr/bin:/bin",
        },
    }


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
