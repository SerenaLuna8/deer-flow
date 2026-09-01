"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing the
# composed structural schema and comments snapshot in a disposable PostgreSQL
# database.
SCHEMA_V1_CANONICAL_DIGEST = "63dfbfd28bda1e70859cdfca6a5b8d51920471a463034384892c43b58130786f"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
