"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing the
# composed structural schema and comments snapshot in a disposable PostgreSQL
# database.
SCHEMA_V1_CANONICAL_DIGEST = "0233910a6b5ea58f1594dd7caea8c01bc6aab91de4bcf5be5950e3ca5a1e1344"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
