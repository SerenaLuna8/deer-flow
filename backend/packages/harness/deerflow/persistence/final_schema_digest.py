"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "b8ddc6099f1607fd0b230ce29c769e905ad64c3ce50c65d084c6d1f50f73486a"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
