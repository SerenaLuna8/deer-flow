"""Chain root: the frozen full_schema_v5 release snapshot.

No-op by design (D2): databases installed by the original v5 release already
carry this revision id, so Alembic adopts them without backfill. Current fresh
installs stamp the current chain head directly; the immutable SQL represented
by this root lives at ``backend/migrations/baseline/full_schema_v5.sql``.
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
