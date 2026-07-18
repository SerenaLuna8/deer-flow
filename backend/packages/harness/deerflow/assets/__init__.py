"""Safe read-only system asset catalog interfaces."""

from deerflow.assets.catalog import (
    AssetCatalogAgentSnapshot,
    AssetCatalogMcpSnapshot,
    AssetCatalogProvider,
    AssetCatalogScope,
    AssetCatalogSkillFile,
    AssetCatalogSkillSnapshot,
    AssetCatalogUnavailable,
    get_asset_catalog_provider,
    require_asset_catalog_provider,
    require_system_asset,
    run_asset_catalog_lookup,
    set_asset_catalog_provider,
    trusted_asset_context,
)

__all__ = [
    "AssetCatalogAgentSnapshot",
    "AssetCatalogMcpSnapshot",
    "AssetCatalogProvider",
    "AssetCatalogScope",
    "AssetCatalogSkillFile",
    "AssetCatalogSkillSnapshot",
    "AssetCatalogUnavailable",
    "get_asset_catalog_provider",
    "require_asset_catalog_provider",
    "require_system_asset",
    "run_asset_catalog_lookup",
    "set_asset_catalog_provider",
    "trusted_asset_context",
]
