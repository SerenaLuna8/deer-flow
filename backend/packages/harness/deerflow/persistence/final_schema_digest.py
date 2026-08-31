"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "069e39e2e3f478432f987c837c513220f4d215b9b3140d5d14097e4e98f09d24"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
