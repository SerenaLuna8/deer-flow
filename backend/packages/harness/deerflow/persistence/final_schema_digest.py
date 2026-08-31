"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "6a833b8a4d98cae82b45cb6e9d476a3423bf03db6bba5d3318908745e30ea987"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
