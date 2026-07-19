from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from deerflow.persistence import bootstrap


@pytest.mark.asyncio
async def test_classify_database_accepts_only_truly_empty_schema(monkeypatch) -> None:
    connection = AsyncMock()
    monkeypatch.setattr(bootstrap, "list_user_relations", AsyncMock(return_value=frozenset()))

    assert await bootstrap.classify_database(connection) == "empty"
    connection.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_classify_database_accepts_exact_m7_schema(monkeypatch) -> None:
    connection = AsyncMock()
    connection.scalar.return_value = bootstrap.M7_FINAL_SCHEMA_REVISION
    monkeypatch.setattr(
        bootstrap,
        "list_user_relations",
        AsyncMock(return_value=bootstrap._FINAL_APP_TABLES | {"alembic_version"}),
    )

    assert await bootstrap.classify_database(connection) == "m7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relations,revision",
    [
        ({"alembic_version"}, "0015_project_reliability_finalize"),
        ({"unknown_table"}, None),
        (bootstrap._FINAL_APP_TABLES | {"alembic_version", "unknown_table"}, bootstrap.M7_FINAL_SCHEMA_REVISION),
    ],
)
async def test_classify_database_rejects_old_or_unknown_nonempty_schema_before_mutation(
    monkeypatch,
    relations: set[str] | frozenset[str],
    revision: str | None,
) -> None:
    connection = AsyncMock()
    connection.scalar.return_value = revision
    monkeypatch.setattr(
        bootstrap,
        "list_user_relations",
        AsyncMock(return_value=frozenset(relations)),
    )

    with pytest.raises(bootstrap.M7RecreateRequired) as captured:
        await bootstrap.classify_database(connection)
    assert captured.value.code == "M7_RECREATE_REQUIRED"


def test_migration_graph_has_one_final_head() -> None:
    assert bootstrap._get_head_revision() == "0001_project_saas_baseline"


@pytest.mark.asyncio
async def test_bootstrap_requires_an_async_engine() -> None:
    with pytest.raises(TypeError, match="AsyncEngine"):
        await bootstrap.bootstrap_schema(object())  # type: ignore[arg-type]
