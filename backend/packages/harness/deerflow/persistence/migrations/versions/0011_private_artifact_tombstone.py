"""Add an explicit tombstone to private artifacts.

Revision ID: 0011_private_artifact_tombstone
Revises: 0010_private_file_source
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deerflow.persistence.migrations._helpers import safe_add_column, safe_drop_column

revision: str = "0011_private_artifact_tombstone"
down_revision: str | Sequence[str] | None = "0010_private_file_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize_predicate(value: object) -> str:
    rendered = str(value).strip()
    if rendered.startswith("(") and rendered.endswith(")"):
        rendered = rendered[1:-1].strip()
    return " ".join(rendered.lower().split())


def _safe_create_index(
    name: str,
    table: str,
    columns: list[str],
    **kwargs: object,
) -> None:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        raise RuntimeError(f"{table} table is required for {revision}")
    existing = next(
        (index for index in inspector.get_indexes(table) if index["name"] == name),
        None,
    )
    if existing is not None:
        expected_predicate = kwargs.get("postgresql_where")
        actual_predicate = existing.get("dialect_options", {}).get("postgresql_where")
        if existing.get("column_names") != columns or bool(existing.get("unique")) is not bool(kwargs.get("unique", False)) or _normalize_predicate(actual_predicate) != _normalize_predicate(expected_predicate):
            raise RuntimeError(f"{name} has an incompatible shape")
        return
    op.create_index(name, table, columns, **kwargs)


def _safe_drop_index(name: str, table: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return
    if name not in {index["name"] for index in inspector.get_indexes(table)}:
        return
    op.drop_index(name, table_name=table)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "artifacts" not in inspector.get_table_names():
        raise RuntimeError(f"artifacts table is required for {revision}")
    safe_add_column(
        "artifacts",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    _safe_create_index(
        "ix_artifacts_private_active",
        "artifacts",
        ["project_id", "owner_user_id", "thread_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    _safe_drop_index("ix_artifacts_private_active", "artifacts")
    safe_drop_column("artifacts", "deleted_at")
