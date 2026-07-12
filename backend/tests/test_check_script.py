from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT_PATH = REPO_ROOT / "scripts" / "check.py"


spec = importlib.util.spec_from_file_location("deerflow_check_script", CHECK_SCRIPT_PATH)
assert spec is not None
assert spec.loader is not None
check_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_script)


def test_find_pnpm_command_prefers_resolved_executable(monkeypatch):
    def fake_which(name: str) -> str | None:
        if name == "pnpm":
            return r"C:\Users\tester\AppData\Roaming\npm\pnpm.CMD"
        if name == "pnpm.cmd":
            return r"C:\Users\tester\AppData\Roaming\npm\pnpm.cmd"
        return None

    monkeypatch.setattr(check_script.shutil, "which", fake_which)

    assert check_script.find_pnpm_command() == [r"C:\Users\tester\AppData\Roaming\npm\pnpm.CMD"]


def test_find_pnpm_command_falls_back_to_corepack(monkeypatch):
    def fake_which(name: str) -> str | None:
        if name == "corepack":
            return r"C:\Program Files\nodejs\corepack.exe"
        return None

    monkeypatch.setattr(check_script.shutil, "which", fake_which)

    assert check_script.find_pnpm_command() == [
        r"C:\Program Files\nodejs\corepack.exe",
        "pnpm",
    ]


def test_find_pnpm_command_falls_back_to_corepack_cmd(monkeypatch):
    def fake_which(name: str) -> str | None:
        if name == "corepack":
            return None
        if name == "corepack.cmd":
            return r"C:\Program Files\nodejs\corepack.cmd"
        return None

    monkeypatch.setattr(check_script.shutil, "which", fake_which)

    assert check_script.find_pnpm_command() == [
        r"C:\Program Files\nodejs\corepack.cmd",
        "pnpm",
    ]


def test_postgres_endpoint_requires_database_url(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(check_script, "PROJECT_ROOT", tmp_path)

    result = check_script.check_postgres_endpoint()

    assert result.ok is False
    assert "DATABASE_URL" in result.detail
    assert "make setup-db" in result.fix


def test_postgres_endpoint_reads_database_url_from_root_dotenv_without_overriding_env(
    tmp_path,
    monkeypatch,
):
    (tmp_path / ".env").write_text(
        "DATABASE_URL='postgresql://dotenv-user:dotenv-secret@db.internal:5432/deerflow'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_script, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        check_script.socket,
        "create_connection",
        lambda address, **_kwargs: type("Connection", (), {"close": lambda self: None})() if address == ("db.internal", 5432) else None,
    )

    result = check_script.check_postgres_endpoint()

    assert result.ok is True
    assert result.detail == "db.internal:5432/deerflow"
    assert "dotenv-secret" not in result.detail

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://env-user:env-secret@env.internal:5433/explicit",
    )
    monkeypatch.setattr(
        check_script.socket,
        "create_connection",
        lambda address, **_kwargs: type("Connection", (), {"close": lambda self: None})() if address == ("env.internal", 5433) else None,
    )
    explicit = check_script.check_postgres_endpoint()
    assert explicit.detail == "env.internal:5433/explicit"


def test_postgres_endpoint_probes_tcp_without_exposing_credentials(monkeypatch):
    secret = "credential-must-not-render"
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql://owner:{secret}@127.0.0.1:5432/deerflow",
    )
    monkeypatch.setattr(check_script.socket, "create_connection", lambda *_args, **_kwargs: object())

    result = check_script.check_postgres_endpoint()

    assert result.ok is True
    rendered = f"{result.detail}\n{result.fix}"
    assert "127.0.0.1:5432/deerflow" in rendered
    assert secret not in rendered
    assert "owner" not in rendered
    assert "postgresql://" not in rendered


def test_postgres_endpoint_reports_docker_port_guidance_without_exception_text(monkeypatch):
    secret = "credential-must-not-render"
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql://owner:{secret}@127.0.0.1:5432/deerflow",
    )

    def fail(*_args, **_kwargs):
        raise OSError(f"private socket detail {secret}")

    monkeypatch.setattr(check_script.socket, "create_connection", fail)
    result = check_script.check_postgres_endpoint()

    assert result.ok is False
    assert "5432" in result.fix
    assert secret not in f"{result.detail}\n{result.fix}"
