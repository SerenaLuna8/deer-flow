"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "c1a5fae30ae0091373972c05153b8b2216cd11b80fad583007a919ffc38d866b"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
