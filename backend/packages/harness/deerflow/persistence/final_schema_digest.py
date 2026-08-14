"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_M7_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
M7_CANONICAL_SCHEMA_DIGEST = "61323e800e9d3f54fb087badbb0ba4d5990bcfa5a2ebc82cd399d3666ae48523"

__all__ = ["M7_CANONICAL_SCHEMA_DIGEST"]
