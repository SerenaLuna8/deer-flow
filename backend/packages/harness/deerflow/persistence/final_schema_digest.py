"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "d182fcdd0111b68a06b9c9e23e869a7907048e1fd725e9c84c4c8cce8283e8fc"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
