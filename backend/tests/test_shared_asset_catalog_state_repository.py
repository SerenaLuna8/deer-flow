from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import Insert


class _EmptyResult:
    def scalar_one_or_none(self):
        return None


class _ScalarResult:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self):
        return self.value


class _Session:
    def __init__(self, result) -> None:
        self.result = result
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return self.result


@pytest.mark.asyncio
async def test_empty_catalog_state_reads_as_generation_zero() -> None:
    from app.shared_assets.catalog_state_repository import CatalogStateRepository

    session = _Session(_EmptyResult())

    assert await CatalogStateRepository(session).read_generation(for_update=True) == 0


@pytest.mark.asyncio
async def test_catalog_generation_bump_uses_singleton_upsert() -> None:
    from app.shared_assets.catalog_state_repository import CatalogStateRepository

    session = _Session(_ScalarResult(1))

    assert await CatalogStateRepository(session).bump_generation() == 1
    assert isinstance(session.statement, Insert)
    assert "ON CONFLICT" in str(session.statement.compile(dialect=postgresql.dialect()))
