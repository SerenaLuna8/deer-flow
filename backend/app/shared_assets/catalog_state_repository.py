from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.shared_assets import AssetCatalogStateRow


class CatalogStateRepository:
    """Catalog generation helpers used inside the caller's transaction.

    M3 schema triggers already bump generation for every resolver-visible asset,
    binding, credential and grant mutation. Services must therefore not call
    ``bump_generation`` for those writes; this explicit helper is reserved for
    future resolver-visible mutations that do not have a trigger.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def read_generation(self, *, for_update: bool = False) -> int:
        statement = select(AssetCatalogStateRow.generation).where(AssetCatalogStateRow.id == 1)
        if for_update:
            statement = statement.with_for_update()
        value = (await self.session.execute(statement)).scalar_one()
        return int(value)

    async def bump_generation(self) -> int:
        statement = update(AssetCatalogStateRow).where(AssetCatalogStateRow.id == 1).values(generation=AssetCatalogStateRow.generation + 1).returning(AssetCatalogStateRow.generation)
        return int((await self.session.execute(statement)).scalar_one())
