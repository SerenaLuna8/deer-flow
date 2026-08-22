from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.shared_assets import AssetCatalogStateRow


class CatalogStateRepository:
    """Catalog generation helpers used inside the caller's transaction.

    M3 schema triggers already bump generation for every resolver-visible asset,
    binding or secret mutation. Services must therefore not call
    ``bump_generation`` for those writes; this explicit helper is reserved for
    future resolver-visible mutations that do not have a trigger.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def read_generation(self, *, for_update: bool = False) -> int:
        if for_update:
            await self.session.execute(text("LOCK TABLE asset_catalog_state IN SHARE ROW EXCLUSIVE MODE"))
        statement = select(AssetCatalogStateRow.generation).where(AssetCatalogStateRow.id == 1)
        value = (await self.session.execute(statement)).scalar_one_or_none()
        return 0 if value is None else int(value)

    async def ensure_and_lock(self) -> int:
        """Create the singleton state row when absent and lock it for bootstrap."""

        await self.session.execute(insert(AssetCatalogStateRow).values(id=1, generation=1).on_conflict_do_nothing(index_elements=[AssetCatalogStateRow.id]))
        value = (await self.session.execute(select(AssetCatalogStateRow.generation).where(AssetCatalogStateRow.id == 1).with_for_update())).scalar_one()
        return int(value)

    async def bump_generation(self) -> int:
        statement = (
            insert(AssetCatalogStateRow)
            .values(id=1, generation=1)
            .on_conflict_do_update(
                index_elements=[AssetCatalogStateRow.id],
                set_={
                    "generation": AssetCatalogStateRow.generation + 1,
                    "updated_at": func.now(),
                },
            )
            .returning(AssetCatalogStateRow.generation)
        )
        return int((await self.session.execute(statement)).scalar_one())
