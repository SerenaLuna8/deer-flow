"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Write explicit DDL only — never import ORM models (D3). Update
``full_schema.sql``, ``KNOWN_CHAIN_REVISIONS`` in
``deerflow/persistence/bootstrap.py``, and the final-schema catalog contract
in the same change; ``tests/test_schema_migration_parity.py`` enforces that
the migrated catalog is byte-equivalent to a fresh install.
"""

from __future__ import annotations

${imports if imports else "from alembic import op"}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    raise RuntimeError("ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead")
