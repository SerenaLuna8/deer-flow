"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "928ae6e6f676d3e8f29c1aaad2adc193304c700d4f0f74d8a73062717474a1d1"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
