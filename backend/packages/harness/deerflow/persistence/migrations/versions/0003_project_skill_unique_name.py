"""Make project Skill display names unique within each project."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_project_skill_unique_name"
down_revision = "0002_project_skill_hard_delete"
branch_labels = None
depends_on = None

_DEDUPLICATE_PROJECT_SKILL_DISPLAY_NAMES = """
DO $$
DECLARE
    duplicate_skill record;
    suffix_number bigint;
    suffix text;
    candidate text;
BEGIN
    FOR duplicate_skill IN
        WITH ranked AS (
            SELECT
                id,
                project_id,
                created_at,
                first_value(display_name) OVER (
                    PARTITION BY project_id, lower(display_name)
                    ORDER BY created_at, id
                ) AS base_name,
                row_number() OVER (
                    PARTITION BY project_id, lower(display_name)
                    ORDER BY created_at, id
                ) AS duplicate_number
            FROM skills
            WHERE scope = 'project'
        )
        SELECT id, project_id, created_at, base_name
        FROM ranked
        WHERE duplicate_number > 1
        ORDER BY project_id, lower(base_name), created_at, id
    LOOP
        suffix_number := 2;
        LOOP
            suffix := ' (' || suffix_number::text || ')';
            candidate :=
                left(
                    duplicate_skill.base_name,
                    greatest(0, 120 - char_length(suffix))
                ) || suffix;
            EXIT WHEN NOT EXISTS (
                SELECT 1
                FROM skills
                WHERE scope = 'project'
                  AND project_id = duplicate_skill.project_id
                  AND id <> duplicate_skill.id
                  AND lower(display_name) = lower(candidate)
            );
            suffix_number := suffix_number + 1;
        END LOOP;

        UPDATE skills
        SET display_name = candidate
        WHERE id = duplicate_skill.id;
    END LOOP;
END
$$
"""


def upgrade() -> None:
    # Migrations run with application processes stopped. Keep the legacy-name
    # rewrite and index creation in one transaction while preventing a writer
    # from introducing another duplicate between those two operations.
    op.execute("LOCK TABLE skills IN SHARE ROW EXCLUSIVE MODE")
    op.execute(_DEDUPLICATE_PROJECT_SKILL_DISPLAY_NAMES)
    op.create_index(
        "uq_skills_project_display_name",
        "skills",
        ["project_id", sa.literal_column("lower(display_name)")],
        unique=True,
        postgresql_where=sa.text("scope = 'project'"),
    )


def downgrade() -> None:
    raise RuntimeError("forward-only schema downgrade is unsupported")
