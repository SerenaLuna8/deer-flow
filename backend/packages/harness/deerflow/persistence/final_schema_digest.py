"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "05bca61d40c24c93aacd464416b6185d5726dd0aaffce07005fa8ffedb7bc7fc"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
