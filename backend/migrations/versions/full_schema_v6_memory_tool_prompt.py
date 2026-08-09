"""Pin tool-origin Memory history to the remember-tool-v1 contract."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v6"
down_revision = "full_schema_v5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_memory_history_entries_contract",
        "memory_history_entries",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_history_entries_contract",
        "memory_history_entries",
        "(origin = 'snip' AND snip_prompt_version <> '') OR (origin = 'tool' AND snip_prompt_version = 'remember-tool-v1')",
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
