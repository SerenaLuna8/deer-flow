from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from deerflow.persistence import bootstrap
from deerflow.persistence.final_schema_contract import ALEMBIC_INDEXES, FINAL_APP_SEQUENCES


def _exact_app_only_objects() -> frozenset[str]:
    return frozenset({f"relation:r:{name}" for name in bootstrap._FINAL_APP_TABLES | {"alembic_version"}} | {f"sequence:{name}:{owner}" for name, owner in FINAL_APP_SEQUENCES} | {f"index:{name}:{owner}" for name, owner in ALEMBIC_INDEXES})


@pytest.mark.asyncio
async def test_classify_database_accepts_only_truly_empty_schema(monkeypatch) -> None:
    connection = AsyncMock()
    monkeypatch.setattr(bootstrap, "inventory_user_schema_objects", AsyncMock(return_value=frozenset()))

    assert await bootstrap.classify_database(connection) == "empty"
    connection.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_classify_database_accepts_exact_m7_schema(monkeypatch) -> None:
    connection = AsyncMock()
    connection.scalar.return_value = bootstrap.M7_FINAL_SCHEMA_REVISION
    monkeypatch.setattr(
        bootstrap,
        "inventory_user_schema_objects",
        AsyncMock(return_value=_exact_app_only_objects()),
    )
    monkeypatch.setattr(bootstrap, "verify_m7_catalog", AsyncMock(return_value=True))

    assert await bootstrap.classify_database(connection) == "m7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "objects,revision",
    [
        ({"relation:r:alembic_version"}, "0015_project_reliability_finalize"),
        ({"relation:r:unknown_table"}, None),
        (
            set(_exact_app_only_objects()) | {"relation:r:unknown_table"},
            bootstrap.M7_FINAL_SCHEMA_REVISION,
        ),
    ],
)
async def test_classify_database_rejects_old_or_unknown_nonempty_schema_before_mutation(
    monkeypatch,
    objects: set[str] | frozenset[str],
    revision: str | None,
) -> None:
    connection = AsyncMock()
    connection.scalar.return_value = revision
    monkeypatch.setattr(
        bootstrap,
        "inventory_user_schema_objects",
        AsyncMock(return_value=frozenset(objects)),
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
