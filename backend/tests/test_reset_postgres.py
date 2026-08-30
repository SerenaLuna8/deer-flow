from __future__ import annotations

import io
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts import reset_postgres

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_noninteractive_cli_requires_exact_database_confirmation(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://owner:reset-secret@127.0.0.1:9432/deerflow",
    )
    monkeypatch.delenv("POSTGRES_ADMIN_URL", raising=False)
    monkeypatch.delenv("CONFIRM_DATABASE", raising=False)
    monkeypatch.setattr(reset_postgres.sys, "stdin", io.StringIO())
    reset = AsyncMock()
    monkeypatch.setattr(reset_postgres, "reset_and_initialize", reset)

    assert reset_postgres.main([]) == 2

    reset.assert_not_awaited()
    output = capsys.readouterr()
    rendered = output.out + output.err
    assert "127.0.0.1:9432/deerflow" in rendered
    assert "CONFIRM_DATABASE=deerflow" in rendered
    assert "reset-secret" not in rendered
    assert "postgresql" not in rendered


@pytest.mark.parametrize("database", ["postgres", "template0", "template1"])
def test_cli_refuses_protected_databases(
    database: str,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql://owner:reset-secret@127.0.0.1:9432/{database}",
    )
    monkeypatch.delenv("POSTGRES_ADMIN_URL", raising=False)
    monkeypatch.setenv("CONFIRM_DATABASE", database)
    reset = AsyncMock()
    monkeypatch.setattr(reset_postgres, "reset_and_initialize", reset)

    assert reset_postgres.main([]) == 2

    reset.assert_not_awaited()
    output = capsys.readouterr()
    rendered = output.out + output.err
    assert "受保护数据库" in rendered
    assert "reset-secret" not in rendered


def test_cli_reports_only_safe_target_after_confirmed_reset(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://owner:reset-secret@127.0.0.1:9432/deerflow",
    )
    monkeypatch.setenv(
        "POSTGRES_ADMIN_URL",
        "postgresql+asyncpg://admin:admin-secret@127.0.0.1:9432/postgres",
    )
    monkeypatch.delenv("CONFIRM_DATABASE", raising=False)
    reset = AsyncMock(
        return_value=reset_postgres.SetupResult(
            host="127.0.0.1",
            port=9432,
            database="deerflow",
            owner="owner",
            created=False,
            revision="schema_v1",
        )
    )
    monkeypatch.setattr(reset_postgres, "reset_and_initialize", reset)

    assert reset_postgres.main(["--confirm-database", "deerflow"]) == 0

    output = capsys.readouterr()
    rendered = output.out + output.err
    assert "127.0.0.1:9432/deerflow" in rendered
    assert "schema_v1" in rendered
    assert "reset-secret" not in rendered
    assert "admin-secret" not in rendered
    assert "postgresql" not in rendered
    assert reset.await_args.kwargs["admin_url"] == "postgresql+asyncpg://admin:admin-secret@127.0.0.1:9432/postgres"


class _ResetEngine:
    def __init__(
        self,
        *,
        current_database: str = "deerflow",
        current_schema: str = "public",
        fail_after_transaction: bool = False,
    ) -> None:
        self.current_database = current_database
        self.current_schema = current_schema
        self.fail_after_transaction = fail_after_transaction
        self.statements: list[str] = []
        self.dispose = AsyncMock()

    @asynccontextmanager
    async def begin(self):
        connection = AsyncMock()

        async def execute(statement) -> None:
            self.statements.append(str(statement))

        connection.execute.side_effect = execute

        async def scalar(statement):
            rendered = str(statement)
            if "current_database" in rendered:
                return self.current_database
            if "current_schema" in rendered:
                return self.current_schema
            raise AssertionError(f"unexpected scalar query: {rendered}")

        connection.scalar.side_effect = scalar
        yield connection
        if self.fail_after_transaction:
            raise ConnectionError("postgresql://owner:commit-secret@127.0.0.1/deerflow")


_ADMIN_URL = "postgresql+asyncpg://admin:admin-secret@127.0.0.1:9432/postgres"


def _install_successful_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        reset_postgres,
        "prepare_default_system_model_bootstrap",
        lambda: object(),
        raising=False,
    )
    monkeypatch.setattr(
        reset_postgres,
        "prepare_model_registry_bootstrap",
        lambda: object(),
        raising=False,
    )
    monkeypatch.setattr(
        reset_postgres,
        "ensure_vector_extension",
        AsyncMock(),
        raising=False,
    )


def test_noninteractive_cli_requires_admin_url_after_confirmation(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://owner:reset-secret@127.0.0.1:9432/deerflow",
    )
    monkeypatch.delenv("POSTGRES_ADMIN_URL", raising=False)
    monkeypatch.setenv("CONFIRM_DATABASE", "deerflow")
    reset = AsyncMock()
    monkeypatch.setattr(reset_postgres, "reset_and_initialize", reset)

    assert reset_postgres.main([]) == 2

    reset.assert_not_awaited()
    output = capsys.readouterr()
    rendered = output.out + output.err
    assert "POSTGRES_ADMIN_URL" in rendered
    assert "reset-secret" not in rendered
    assert "postgresql+asyncpg" not in rendered


@pytest.mark.asyncio
async def test_reset_rejects_invalid_bootstrap_before_ddl(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        reset_postgres,
        "prepare_default_system_model_bootstrap",
        lambda: (_ for _ in ()).throw(reset_postgres.DefaultSystemModelBootstrapConfigurationInvalid()),
        raising=False,
    )
    monkeypatch.setattr(
        reset_postgres,
        "create_async_engine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("DDL engine must not be created")),
    )

    with pytest.raises(reset_postgres.PostgresResetError, match="预检"):
        await reset_postgres.reset_and_initialize(
            "postgresql://owner:secret@127.0.0.1:9432/deerflow",
            expected_database="deerflow",
            admin_url=_ADMIN_URL,
        )


@pytest.mark.asyncio
async def test_reset_rejects_invalid_admin_url_before_ddl(
    monkeypatch,
) -> None:
    _install_successful_preflight(monkeypatch)
    monkeypatch.setattr(
        reset_postgres,
        "create_async_engine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("DDL engine must not be created")),
    )

    with pytest.raises(reset_postgres.PostgresResetError, match="POSTGRES_ADMIN_URL"):
        await reset_postgres.reset_and_initialize(
            "postgresql://owner:secret@127.0.0.1:9432/deerflow",
            expected_database="deerflow",
            admin_url="postgresql://admin:secret@127.0.0.1:9432/deerflow",
        )


@pytest.mark.asyncio
async def test_reset_rejects_missing_model_registry_bootstrap_before_ddl(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        reset_postgres,
        "prepare_default_system_model_bootstrap",
        lambda: object(),
        raising=False,
    )
    monkeypatch.setattr(
        reset_postgres,
        "prepare_model_registry_bootstrap",
        lambda: (_ for _ in ()).throw(reset_postgres.ModelRegistryBootstrapConfigurationInvalid()),
        raising=False,
    )
    monkeypatch.setattr(
        reset_postgres,
        "create_async_engine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("DDL engine must not be created")),
    )

    with pytest.raises(reset_postgres.PostgresResetError, match="模型供应商 bootstrap"):
        await reset_postgres.reset_and_initialize(
            "postgresql://owner:secret@127.0.0.1:9432/deerflow",
            expected_database="deerflow",
            admin_url=_ADMIN_URL,
        )


@pytest.mark.asyncio
async def test_reset_rebuilds_public_schema_then_runs_schema_v1_setup(
    monkeypatch,
) -> None:
    _install_successful_preflight(monkeypatch)
    engine = _ResetEngine()
    lock_events: list[str] = []

    @asynccontextmanager
    async def complete_bootstrap_lock(_database_url: str):
        lock_events.append("enter")
        yield
        lock_events.append("exit")

    monkeypatch.setattr(
        reset_postgres,
        "complete_bootstrap_lock",
        complete_bootstrap_lock,
        raising=False,
    )
    monkeypatch.setattr(
        reset_postgres,
        "create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    bootstrap_material = object()
    monkeypatch.setattr(
        reset_postgres,
        "prepare_default_system_model_bootstrap",
        lambda: bootstrap_material,
    )
    registry_material = object()
    monkeypatch.setattr(
        reset_postgres,
        "prepare_model_registry_bootstrap",
        lambda: registry_material,
    )

    async def prepare_vector(admin_url: str, database: str) -> None:
        assert admin_url == _ADMIN_URL
        assert database == "deerflow"
        lock_events.append("vector")

    monkeypatch.setattr(
        reset_postgres,
        "ensure_vector_extension",
        prepare_vector,
        raising=False,
    )

    async def bootstrap(
        _database_url: str,
        *,
        default_model_bootstrap,
        model_registry_bootstrap,
        force_public_schema: bool,
    ) -> str:
        assert default_model_bootstrap is bootstrap_material
        assert model_registry_bootstrap is registry_material
        assert force_public_schema is True
        lock_events.append("bootstrap")
        return "schema_v1"

    monkeypatch.setattr(
        reset_postgres,
        "bootstrap_empty_schema_under_lock",
        bootstrap,
        raising=False,
    )

    result = await reset_postgres.reset_and_initialize(
        "postgresql+asyncpg://owner:secret@127.0.0.1:9432/deerflow",
        expected_database="deerflow",
        admin_url=_ADMIN_URL,
    )

    assert engine.statements == [
        "DROP SCHEMA public CASCADE",
        "CREATE SCHEMA public AUTHORIZATION CURRENT_USER",
    ]
    assert all("GRANT ALL" not in statement for statement in engine.statements)
    assert engine.dispose.await_count == 1
    assert lock_events == ["enter", "vector", "bootstrap", "exit"]
    assert result.revision == "schema_v1"


@pytest.mark.asyncio
async def test_reset_rejects_non_public_effective_schema_before_ddl(
    monkeypatch,
) -> None:
    _install_successful_preflight(monkeypatch)
    engine = _ResetEngine(current_schema="tenant")

    @asynccontextmanager
    async def complete_bootstrap_lock(_database_url: str):
        yield

    monkeypatch.setattr(
        reset_postgres,
        "complete_bootstrap_lock",
        complete_bootstrap_lock,
    )
    monkeypatch.setattr(
        reset_postgres,
        "create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    bootstrap = AsyncMock()
    monkeypatch.setattr(
        reset_postgres,
        "bootstrap_empty_schema_under_lock",
        bootstrap,
    )

    with pytest.raises(reset_postgres.PostgresResetError, match="public"):
        await reset_postgres.reset_and_initialize(
            "postgresql://owner:secret@127.0.0.1:9432/deerflow",
            expected_database="deerflow",
            admin_url=_ADMIN_URL,
        )

    assert engine.statements == []
    bootstrap.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_reports_unknown_outcome_when_commit_confirmation_is_lost(
    monkeypatch,
) -> None:
    _install_successful_preflight(monkeypatch)
    engine = _ResetEngine(fail_after_transaction=True)

    @asynccontextmanager
    async def complete_bootstrap_lock(_database_url: str):
        yield

    monkeypatch.setattr(
        reset_postgres,
        "complete_bootstrap_lock",
        complete_bootstrap_lock,
    )
    monkeypatch.setattr(
        reset_postgres,
        "create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    bootstrap = AsyncMock()
    monkeypatch.setattr(
        reset_postgres,
        "bootstrap_empty_schema_under_lock",
        bootstrap,
    )

    with pytest.raises(reset_postgres.PostgresResetError) as exc_info:
        await reset_postgres.reset_and_initialize(
            "postgresql://owner:secret@127.0.0.1:9432/deerflow",
            expected_database="deerflow",
            admin_url=_ADMIN_URL,
        )

    rendered = str(exc_info.value)
    assert "提交结果未知" in rendered
    assert "可能已重建" in rendered
    assert "原 Schema 未提交删除" not in rendered
    assert "commit-secret" not in rendered
    assert "postgresql" not in rendered
    bootstrap.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_sanitizes_unknown_bootstrap_outcome_without_leaking_failure(
    monkeypatch,
) -> None:
    _install_successful_preflight(monkeypatch)
    engine = _ResetEngine()

    @asynccontextmanager
    async def complete_bootstrap_lock(_database_url: str):
        yield

    monkeypatch.setattr(
        reset_postgres,
        "complete_bootstrap_lock",
        complete_bootstrap_lock,
        raising=False,
    )
    monkeypatch.setattr(
        reset_postgres,
        "create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        reset_postgres,
        "bootstrap_empty_schema_under_lock",
        AsyncMock(side_effect=ValueError("postgresql://owner:setup-secret@127.0.0.1/deerflow")),
    )

    with pytest.raises(reset_postgres.PostgresResetError) as exc_info:
        await reset_postgres.reset_and_initialize(
            "postgresql://owner:secret@127.0.0.1:9432/deerflow",
            expected_database="deerflow",
            admin_url=_ADMIN_URL,
        )

    rendered = str(exc_info.value)
    assert "初始化结果未知" in rendered
    assert "可能完整或仅部分完成" in rendered
    assert "setup-secret" not in rendered
    assert "postgresql" not in rendered


def test_root_make_reset_db_delegates_confirmation_to_backend_script() -> None:
    result = subprocess.run(
        ["make", "-n", "reset-db", "CONFIRM_DATABASE=deerflow"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    rendered = result.stdout + result.stderr
    assert "python -m scripts.reset_postgres" in rendered
    assert "PYTHONPATH=." not in rendered
    root_makefile = REPOSITORY_ROOT.joinpath("Makefile").read_text(encoding="utf-8")
    assert "reset-db: export CONFIRM_DATABASE := $(CONFIRM_DATABASE)" in root_makefile
