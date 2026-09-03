"""Dependency-free digest for the supported full PostgreSQL schema."""

# Updated together with FINAL_SCHEMA_V1_CATALOG_SIGNATURE after installing the
# composed structural schema and comments snapshot in a disposable PostgreSQL
# database.
SCHEMA_V1_CANONICAL_DIGEST = "7cef7170a12ae8b130a7a39acdbdaba5a2af450904526606215e18687ae617a1"

__all__ = ["SCHEMA_V1_CANONICAL_DIGEST"]
