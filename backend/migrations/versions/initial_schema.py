"""Initial public schema represented by the complete full_schema.sql snapshot.

No-op by design: fresh installs execute the snapshot and stamp this root
revision directly. All pre-release schema work is folded into this baseline.
Downgrade is fail-closed.
"""

from __future__ import annotations

revision = "initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """The initial catalog comes from full_schema.sql; nothing to apply."""


def downgrade() -> None:
    raise RuntimeError("ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead")
