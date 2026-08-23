"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "9eab8d864de1d8bcf6b05d8683290a8b902d101c435caee5675e6a01ea423a0d"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
