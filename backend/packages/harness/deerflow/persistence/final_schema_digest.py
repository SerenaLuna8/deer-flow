"""Dependency-free frozen digests for recognized PostgreSQL schemas."""

M7_BASELINE_CANONICAL_SCHEMA_DIGEST = "733b36328c630e554f052bc49728786b3f6ccc1cbbd1694095923a22f07a924a"
# Frozen digest for the exact 0002 Skill Builder catalog. Never rewrite this
# value when a later forward-only revision becomes current.
M7_SKILL_BUILDER_CANONICAL_SCHEMA_DIGEST = "c137e79f477af19c9df902f886c4d04f328c9fce5eecf5c56c6c19294e69eab4"
# Frozen digest for the exact 0003 Skill Credential catalog. Never rewrite
# this value when a later forward-only revision becomes current.
M7_SKILL_CREDENTIAL_CANONICAL_SCHEMA_DIGEST = "6aedfde94b4ac6081181cf46741a4d29661a69a8708b7275a9f136aa55090dc4"
# Updated together with FINAL_M7_CATALOG_SIGNATURE after applying the current
# forward-only migration chain to a disposable PostgreSQL database.
M7_CANONICAL_SCHEMA_DIGEST = "9f0df54ef2506347bc4a64b5aa0d0e8cbc49950710f77c1ce7fe11a43a6d74a7"

__all__ = [
    "M7_BASELINE_CANONICAL_SCHEMA_DIGEST",
    "M7_CANONICAL_SCHEMA_DIGEST",
    "M7_SKILL_BUILDER_CANONICAL_SCHEMA_DIGEST",
    "M7_SKILL_CREDENTIAL_CANONICAL_SCHEMA_DIGEST",
]
