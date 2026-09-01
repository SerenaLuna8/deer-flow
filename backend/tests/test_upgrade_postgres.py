from __future__ import annotations

import importlib
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def upgrade_module():
    return importlib.import_module("scripts.upgrade_postgres")


def _current_result(upgrade_module):
    return upgrade_module.UpgradeResult(
        host="127.0.0.1",
        port=9432,
        database="deerflow",
        previous_revision="schema_v1",
        current_revision="schema_v1",
        upgraded=False,
    )


def test_upgrade_result_contains_only_safe_target_and_revision_metadata(
    upgrade_module,
) -> None:
    current = _current_result(upgrade_module)

    assert asdict(current) == {
        "host": "127.0.0.1",
        "port": 9432,
        "database": "deerflow",
        "previous_revision": "schema_v1",
        "current_revision": "schema_v1",
        "upgraded": False,
    }
    assert current.previous_revision == current.current_revision == "schema_v1"
    assert current.upgraded is False
    assert not {"username", "password", "url"} & set(
        upgrade_module.UpgradeResult.__dataclass_fields__,
    )


def test_cli_requires_database_url_before_running_upgrade(
    upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    upgrade = AsyncMock()
    monkeypatch.setattr(upgrade_module, "upgrade_postgres", upgrade)

    assert upgrade_module.main([]) == 2

    upgrade.assert_not_awaited()
    output = capsys.readouterr()
    rendered = output.out + output.err
    assert "DATABASE_URL" in rendered


def test_cli_never_prints_database_secret_or_connection_url(
    upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = "postgresql+asyncpg://owner:do-not-print-this-password@127.0.0.1:9432/deerflow"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(
        upgrade_module,
        "upgrade_postgres",
        AsyncMock(
            side_effect=upgrade_module.PostgresUpgradeError(
                f"upgrade failed for {database_url}",
            ),
        ),
    )

    assert upgrade_module.main([]) == 1

    output = capsys.readouterr()
    rendered = output.out + output.err
    assert "do-not-print-this-password" not in rendered
    assert database_url not in rendered
    assert "postgresql" not in rendered


@pytest.mark.asyncio
async def test_upgrade_validates_artifacts_before_creating_database_engine(
    upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validate = MagicMock(side_effect=RuntimeError("stale migration artifact"))
    create_engine = MagicMock(
        side_effect=AssertionError("engine must not be created before preflight"),
    )
    monkeypatch.setattr(
        upgrade_module,
        "validate_schema_upgrade_artifacts",
        validate,
    )
    monkeypatch.setattr(upgrade_module, "create_async_engine", create_engine)

    with pytest.raises(
        upgrade_module.PostgresUpgradeError,
        match="产物|预检|upgrade|migration",
    ):
        await upgrade_module.upgrade_postgres(
            "postgresql+asyncpg://owner:secret@127.0.0.1:9432/deerflow",
        )

    validate.assert_called_once_with()
    create_engine.assert_not_called()


def test_cli_reports_already_current_result_without_secrets(
    upgrade_module,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = "postgresql+asyncpg://owner:result-secret@127.0.0.1:9432/deerflow"
    result = _current_result(upgrade_module)
    upgrade = AsyncMock(return_value=result)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(upgrade_module, "upgrade_postgres", upgrade)

    assert upgrade_module.main([]) == 0

    upgrade.assert_awaited_once_with(database_url)
    output = capsys.readouterr()
    rendered = output.out + output.err
    assert "127.0.0.1:9432/deerflow" in rendered
    assert "schema_v1" in rendered
    assert "result-secret" not in rendered
    assert database_url not in rendered
    assert "postgresql" not in rendered
