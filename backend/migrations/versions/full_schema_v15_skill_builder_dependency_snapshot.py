"""Add trustworthy Skill Builder authoring dependency and terminal receipts."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v15"
down_revision = "full_schema_v14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE skill_design_sessions ADD COLUMN authoring_dependencies_json JSONB")
    op.execute(
        "ALTER TABLE skill_design_sessions ADD CONSTRAINT "
        "ck_skill_design_sessions_authoring_dependencies CHECK ("
        "authoring_dependencies_json IS NULL OR ("
        "jsonb_typeof(authoring_dependencies_json) = 'object' AND "
        "authoring_dependencies_json ->> 'version' = '1' AND "
        "(authoring_dependencies_json ->> 'draft_checksum') "
        "~ '^[0-9a-f]{64}$' AND "
        "jsonb_typeof(authoring_dependencies_json -> 'requirements') "
        "= 'array' AND "
        "jsonb_array_length(authoring_dependencies_json -> 'requirements') "
        "<= 64))"
    )
    op.execute("COMMENT ON COLUMN skill_design_sessions.authoring_dependencies_json IS '技能设计会话：编写用途依赖JSON 数据。'")

    op.execute("ALTER TABLE skill_design_operations ADD COLUMN terminal_kind VARCHAR(16)")
    op.execute("ALTER TABLE skill_design_operations ADD COLUMN terminal_request_checksum CHAR(64)")
    op.execute("ALTER TABLE skill_design_operations ADD CONSTRAINT ck_skill_design_operations_terminal_kind CHECK (terminal_kind IS NULL OR terminal_kind IN ('clarification', 'candidate'))")
    op.execute("ALTER TABLE skill_design_operations ADD CONSTRAINT ck_skill_design_operations_terminal_checksum CHECK (terminal_request_checksum IS NULL OR terminal_request_checksum ~ '^[0-9a-f]{64}$')")
    op.execute(
        "ALTER TABLE skill_design_operations ADD CONSTRAINT "
        "ck_skill_design_operations_terminal_pair CHECK ("
        "(terminal_kind IS NULL AND terminal_request_checksum IS NULL) OR "
        "(terminal_kind IS NOT NULL AND terminal_request_checksum IS NOT NULL))"
    )
    op.execute("COMMENT ON COLUMN skill_design_operations.terminal_kind IS '技能设计操作：终态类型。'")
    op.execute("COMMENT ON COLUMN skill_design_operations.terminal_request_checksum IS '技能设计操作：终态请求校验和。'")


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
