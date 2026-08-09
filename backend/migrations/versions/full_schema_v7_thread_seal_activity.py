"""Preserve Thread activity time for an isolated Memory Seal stamp."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v7"
down_revision = "full_schema_v6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION set_threads_meta_updated_at()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.memory_sealed_at IS DISTINCT FROM OLD.memory_sealed_at
               AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at
               AND (to_jsonb(NEW) - 'memory_sealed_at' - 'updated_at')
                   IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - 'memory_sealed_at' - 'updated_at') THEN
                NEW.updated_at := OLD.updated_at;
            ELSE
                NEW.updated_at := now();
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute("DROP TRIGGER trg_threads_meta_updated_at ON threads_meta")
    op.execute(
        """CREATE TRIGGER trg_threads_meta_updated_at
        BEFORE UPDATE ON threads_meta
        FOR EACH ROW EXECUTE FUNCTION set_threads_meta_updated_at()"""
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
