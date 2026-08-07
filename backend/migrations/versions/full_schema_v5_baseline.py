"""Chain root: the full_schema_v5 snapshot installed by full_schema.sql.

No-op by design (D2): ``full_schema.sql`` already stamps exactly this
revision id into ``alembic_version``, so every database installed through
``make setup-db`` is natively at the chain root and Alembic adopts it in
place without any backfill. The frozen snapshot this root corresponds to
lives at ``backend/migrations/baseline/full_schema_v5.sql``.
"""

from __future__ import annotations

revision = "full_schema_v5"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """The baseline is the full_schema.sql snapshot itself — nothing to do."""


def downgrade() -> None:
    raise RuntimeError("ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead")
