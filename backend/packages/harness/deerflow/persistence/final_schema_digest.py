"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "9e44267f0924c64b592d65fedee23480d89dc2340f30abb699bffbdc96ae3c44"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
