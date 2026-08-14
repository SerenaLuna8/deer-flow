"""Chain root and current head: the complete full_schema.sql snapshot.

No-op by design: fresh installs stamp this marker directly. Incremental
revisions, when they exist, will revise this root. Downgrade is fail-closed.
"""

from __future__ import annotations

revision = "full_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """The snapshot is full_schema.sql itself — nothing to do."""


def downgrade() -> None:
    raise RuntimeError("ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead")
