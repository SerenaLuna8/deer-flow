"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "f0d76d50349cf36ed87c3d4b9b0f6bfda94dc62aca4fc506260a6e2bbd72f496"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
