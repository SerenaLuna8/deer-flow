"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing
# ``full_schema.sql`` in a disposable PostgreSQL database.
SCHEMA_V1_CANONICAL_DIGEST = "3f3136f55e941370cfd2ed98af32e87ba3638216e2d75007d3a007541edc8e06"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
