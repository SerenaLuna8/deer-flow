"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "c76843276791315e4ae602454703bc48bf815f3d7ad00b750166431297be9fbb"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
