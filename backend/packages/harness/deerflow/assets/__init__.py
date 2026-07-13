"""Safe read-only system asset catalog interfaces."""

from deerflow.assets.catalog import (
    ASSET_CATALOG_CUTOVER_CODE,
    ASSET_CATALOG_CUTOVER_MESSAGE,
    AssetCatalogAgentSnapshot,
    AssetCatalogMcpSnapshot,
    AssetCatalogProvider,
    AssetCatalogScope,
    AssetCatalogSkillFile,
    AssetCatalogSkillSnapshot,
    AssetCatalogUnavailable,
    areject_legacy_asset_mutation_after_cutover,
    get_asset_catalog_provider,
    reject_legacy_asset_mutation_after_cutover,
    require_system_asset,
    run_asset_catalog_lookup,
    set_asset_catalog_provider,
    trusted_asset_context,
)

__all__ = [
    "ASSET_CATALOG_CUTOVER_CODE",
    "ASSET_CATALOG_CUTOVER_MESSAGE",
    "AssetCatalogAgentSnapshot",
    "AssetCatalogMcpSnapshot",
    "AssetCatalogProvider",
    "AssetCatalogScope",
    "AssetCatalogSkillFile",
    "AssetCatalogSkillSnapshot",
    "AssetCatalogUnavailable",
    "areject_legacy_asset_mutation_after_cutover",
    "get_asset_catalog_provider",
    "require_system_asset",
    "reject_legacy_asset_mutation_after_cutover",
    "run_asset_catalog_lookup",
    "set_asset_catalog_provider",
    "trusted_asset_context",
]
