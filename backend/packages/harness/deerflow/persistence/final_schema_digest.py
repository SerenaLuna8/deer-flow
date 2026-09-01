"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "d1c330c374dae3e74d515c53af367fe7956d33d7dba6af2990f2404fd0beab72"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
