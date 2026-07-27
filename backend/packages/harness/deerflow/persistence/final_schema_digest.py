"""Dependency-free frozen digests for recognized PostgreSQL schemas."""

M7_BASELINE_CANONICAL_SCHEMA_DIGEST = "733b36328c630e554f052bc49728786b3f6ccc1cbbd1694095923a22f07a924a"
# Updated together with FINAL_M7_CATALOG_SIGNATURE after applying the current
# forward-only migration chain to a disposable PostgreSQL database.
M7_CANONICAL_SCHEMA_DIGEST = "c137e79f477af19c9df902f886c4d04f328c9fce5eecf5c56c6c19294e69eab4"

__all__ = [
    "M7_BASELINE_CANONICAL_SCHEMA_DIGEST",
    "M7_CANONICAL_SCHEMA_DIGEST",
]
