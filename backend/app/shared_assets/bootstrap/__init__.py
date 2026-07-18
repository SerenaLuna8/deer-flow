"""Deterministic packaged system-asset bootstrap."""

from app.shared_assets.bootstrap.catalog import (
    BootstrapCatalog,
    BootstrapCatalogError,
    BootstrapEntry,
    load_bootstrap_catalog,
)
from app.shared_assets.bootstrap.service import (
    BUILTIN_ASSET_EMAIL,
    BUILTIN_ASSET_USER_ID,
    BootstrapConflict,
    BootstrapResult,
    bootstrap_system_assets,
)

__all__ = [
    "BUILTIN_ASSET_EMAIL",
    "BUILTIN_ASSET_USER_ID",
    "BootstrapCatalog",
    "BootstrapCatalogError",
    "BootstrapConflict",
    "BootstrapEntry",
    "BootstrapResult",
    "bootstrap_system_assets",
    "load_bootstrap_catalog",
]
