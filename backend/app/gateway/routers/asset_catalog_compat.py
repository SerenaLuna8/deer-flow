"""Compatibility guards for legacy file-backed system asset routes."""

from __future__ import annotations

from fastapi import HTTPException

from deerflow.assets.catalog import (
    ASSET_CATALOG_CUTOVER_CODE,
    ASSET_CATALOG_CUTOVER_MESSAGE,
    AssetCatalogUnavailable,
    areject_legacy_asset_mutation_after_cutover,
    get_asset_catalog_provider,
)

CUTOVER_DETAIL = {
    "code": ASSET_CATALOG_CUTOVER_CODE,
    "message": ASSET_CATALOG_CUTOVER_MESSAGE,
}


def cutover_conflict() -> HTTPException:
    return HTTPException(status_code=409, detail=dict(CUTOVER_DETAIL))


async def is_catalog_cutover_enabled() -> bool:
    provider = get_asset_catalog_provider()
    return provider is not None and await provider.is_cutover_enabled()


async def reject_legacy_asset_mutation_after_cutover() -> None:
    try:
        await areject_legacy_asset_mutation_after_cutover()
    except AssetCatalogUnavailable as exc:
        if ASSET_CATALOG_CUTOVER_CODE not in str(exc):
            raise
        raise cutover_conflict() from None


__all__ = [
    "cutover_conflict",
    "CUTOVER_DETAIL",
    "is_catalog_cutover_enabled",
    "reject_legacy_asset_mutation_after_cutover",
]
