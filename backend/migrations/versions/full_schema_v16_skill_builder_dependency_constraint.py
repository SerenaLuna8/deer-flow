"""Normalize the Skill Builder dependency-array constraint safely."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v16"
down_revision = "full_schema_v15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE skill_design_sessions DROP CONSTRAINT ck_skill_design_sessions_authoring_dependencies")
    op.execute(
        "ALTER TABLE skill_design_sessions ADD CONSTRAINT "
        "ck_skill_design_sessions_authoring_dependencies CHECK ("
        "authoring_dependencies_json IS NULL OR ("
        "jsonb_typeof(authoring_dependencies_json) = 'object' AND "
        "authoring_dependencies_json ->> 'version' = '1' AND "
        "(authoring_dependencies_json ->> 'draft_checksum') "
        "~ '^[0-9a-f]{64}$' AND "
        "CASE WHEN jsonb_typeof("
        "authoring_dependencies_json -> 'requirements') = 'array' "
        "THEN jsonb_array_length("
        "authoring_dependencies_json -> 'requirements') <= 64 "
        "ELSE FALSE END))"
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
