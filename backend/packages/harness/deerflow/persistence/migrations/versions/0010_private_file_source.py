"""Add scope-safe source linkage for converted private files.

Revision ID: 0010_private_file_source
Revises: 0009_project_private_work_finalize
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_private_file_source"
down_revision: str | Sequence[str] | None = "0009_project_private_work_finalize"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("files", sa.Column("source_file_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_files_private_source",
        "files",
        "files",
        ["project_id", "owner_user_id", "thread_id", "source_file_id"],
        ["project_id", "owner_user_id", "thread_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_files_source_not_self",
        "files",
        "source_file_id IS NULL OR source_file_id <> id",
    )
    op.create_check_constraint(
        "ck_files_source_kind",
        "files",
        "source_file_id IS NULL OR kind = 'workspace'",
    )
    op.create_check_constraint(
        "ck_file_chunks_content_size",
        "file_chunks",
        "size = octet_length(content)",
    )
    op.create_check_constraint(
        "ck_file_chunks_bounded_size",
        "file_chunks",
        "size > 0 AND size <= 1048576",
    )


def downgrade() -> None:
    op.drop_constraint("ck_file_chunks_bounded_size", "file_chunks", type_="check")
    op.drop_constraint("ck_file_chunks_content_size", "file_chunks", type_="check")
    op.drop_constraint("ck_files_source_kind", "files", type_="check")
    op.drop_constraint("ck_files_source_not_self", "files", type_="check")
    op.drop_constraint("fk_files_private_source", "files", type_="foreignkey")
    op.drop_column("files", "source_file_id")
