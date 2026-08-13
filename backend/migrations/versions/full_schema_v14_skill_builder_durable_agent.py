"""Add durable Skill Builder Thread and Run coordinates."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v14"
down_revision = "full_schema_v13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE threads_meta ADD COLUMN thread_kind VARCHAR(16) DEFAULT 'chat' NOT NULL")
    op.execute("ALTER TABLE threads_meta ADD CONSTRAINT ck_threads_meta_kind CHECK (thread_kind IN ('chat', 'skill_builder'))")
    op.execute("COMMENT ON COLUMN threads_meta.thread_kind IS '线程元数据：线程类型。'")

    op.execute("ALTER TABLE skill_design_operations ADD COLUMN run_id VARCHAR(64)")
    op.execute("ALTER TABLE skill_design_operations ADD CONSTRAINT fk_skill_design_operations_run FOREIGN KEY (project_id, owner_user_id, run_id) REFERENCES runs (project_id, owner_user_id, run_id) ON DELETE RESTRICT NOT VALID")
    op.execute("ALTER TABLE skill_design_operations VALIDATE CONSTRAINT fk_skill_design_operations_run")
    op.execute("ALTER TABLE skill_design_operations ADD CONSTRAINT uq_skill_design_operations_run UNIQUE (project_id, owner_user_id, run_id)")
    op.execute("COMMENT ON COLUMN skill_design_operations.run_id IS '技能设计操作：运行标识。'")


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
