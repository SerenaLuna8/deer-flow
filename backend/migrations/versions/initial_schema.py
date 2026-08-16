"""Root revision for databases installed from the initial public snapshot.

No-op by design: the historical initial snapshot already contained this
catalog. Fresh installs execute the current ``full_schema.sql`` and stamp the
current chain head; databases at this root advance through later revisions.
Downgrade is fail-closed.
"""

from __future__ import annotations

revision = "initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """The historical initial catalog already exists; nothing to apply."""


def downgrade() -> None:
    raise RuntimeError("ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead")
