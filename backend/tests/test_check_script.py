from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT_PATH = REPO_ROOT / "scripts" / "check.py"
PNPM_SCRIPT_PATH = REPO_ROOT / "scripts" / "pnpm.py"


def _load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_script = _load_script(CHECK_SCRIPT_PATH, "deerflow_check_script")


def test_check_script_uses_shared_pnpm_runner() -> None:
    source = CHECK_SCRIPT_PATH.read_text(encoding="utf-8")

    assert 'Path(__file__).with_name("pnpm.py")' in source


def test_check_script_preserves_runner_failure_diagnostics(monkeypatch) -> None:
    module = _load_script(CHECK_SCRIPT_PATH, "deerflow_check_script_failure")
    observed: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(
            args=["python", "pnpm.py", "-v"],
            returncode=42,
            stdout="partial pnpm output\n",
            stderr="Error: pnpm command failed with exit status 42.\n",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.run_pnpm_version() == (
        None,
        False,
        "Error: pnpm command failed with exit status 42.\npartial pnpm output",
    )
    assert observed["cwd"] == REPO_ROOT / "frontend"
    assert observed["shell"] is False


def test_check_script_preserves_corepack_resolution_hint(monkeypatch) -> None:
    module = _load_script(CHECK_SCRIPT_PATH, "deerflow_check_script_corepack")

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["python", "pnpm.py", "-v"],
            returncode=0,
            stdout="10.26.2\n",
            stderr="Using pnpm via Corepack.\n",
        ),
    )

    assert module.run_pnpm_version() == ("10.26.2", True, None)


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
