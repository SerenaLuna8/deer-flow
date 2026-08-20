"""Persist exact Credential env source fields for Skill mappings.

Revision ID: skill_credential_source_field
Revises: agent_archived_slug_reuse
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "skill_credential_source_field"
down_revision = "agent_archived_slug_reuse"
branch_labels = None
depends_on = None


def _add_source_field(table_name: str, comment: str) -> None:
    op.add_column(
        table_name,
        sa.Column(
            "source_env_field_name",
            sa.String(length=255),
            nullable=True,
            comment=comment,
        ),
    )
    # The previous release only allowed exact-name mappings, so the target
    # environment name is the authoritative compatibility backfill.
    op.execute(
        sa.text(
            f"UPDATE {table_name} SET source_env_field_name = secret_name WHERE source_env_field_name IS NULL",
        ),
    )
    op.alter_column(
        table_name,
        "source_env_field_name",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.create_check_constraint(
        f"ck_{table_name}_source_env_field_name",
        table_name,
        "length(source_env_field_name) BETWEEN 1 AND 255",
    )


def upgrade() -> None:
    _add_source_field(
        "project_skill_credential_bindings",
        "技能凭据绑定：来源环境变量字段名称。",
    )
    _add_source_field(
        "run_skill_credential_snapshots",
        "运行技能凭据快照：来源环境变量字段名称。",
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead",
    )
