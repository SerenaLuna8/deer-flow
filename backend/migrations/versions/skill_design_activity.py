"""Add durable Skill Builder activity, preference, and stop rollback state.

Revision ID: skill_design_activity
Revises: agent_design_activity_terminal
"""

from __future__ import annotations

from alembic import op

revision = "skill_design_activity"
down_revision = "agent_design_activity_terminal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """ALTER TABLE skill_design_sessions
           ADD COLUMN execution_model_ref VARCHAR(36),
           ADD COLUMN execution_mode VARCHAR(16),
           ADD COLUMN execution_thinking_enabled BOOLEAN,
           ADD COLUMN execution_reasoning_effort VARCHAR(16),
           ADD CONSTRAINT ck_skill_design_sessions_execution_preference CHECK (
               (execution_model_ref IS NULL
                AND execution_mode IS NULL
                AND execution_thinking_enabled IS NULL
                AND execution_reasoning_effort IS NULL)
               OR
               (execution_model_ref IS NOT NULL
                AND execution_mode IN ('flash', 'thinking', 'pro', 'ultra')
                AND execution_thinking_enabled IS NOT NULL
                AND (execution_reasoning_effort IS NULL
                     OR execution_reasoning_effort IN ('none', 'low', 'medium', 'high')))
           )""",
    )
    op.execute(
        """ALTER TABLE skill_design_operations
           ADD COLUMN stop_requested_at TIMESTAMP WITH TIME ZONE,
           DROP CONSTRAINT ck_skill_design_operations_status,
           DROP CONSTRAINT ck_skill_design_operations_completion,
           ADD CONSTRAINT ck_skill_design_operations_status
               CHECK (status IN ('in_progress', 'completed', 'failed', 'stopped')),
           ADD CONSTRAINT ck_skill_design_operations_completion CHECK (
               (status = 'in_progress' AND result_revision IS NULL
                AND public_error_code IS NULL)
               OR (status = 'completed' AND result_revision IS NOT NULL
                   AND public_error_code IS NULL)
               OR (status = 'failed' AND result_revision IS NOT NULL
                   AND public_error_code IS NOT NULL)
               OR (status = 'stopped' AND result_revision IS NOT NULL
                   AND public_error_code IS NULL)
           ),
           ADD CONSTRAINT uq_skill_design_operations_private_scope
               UNIQUE (project_id, owner_user_id, session_id, id)""",
    )
    op.execute(
        """CREATE TABLE skill_design_activities (
               seq BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
               project_id UUID NOT NULL,
               owner_user_id VARCHAR(36) NOT NULL,
               session_id UUID NOT NULL,
               operation_id UUID NOT NULL,
               run_id VARCHAR(64),
               attempt BIGINT,
               source_event_id VARCHAR(255),
               kind VARCHAR(40) NOT NULL,
               payload_json JSONB DEFAULT '{}'::jsonb NOT NULL,
               created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
               CONSTRAINT pk_skill_design_activities PRIMARY KEY (seq),
               CONSTRAINT ck_skill_design_activities_attempt
                   CHECK (attempt IS NULL OR attempt >= 1),
               CONSTRAINT ck_skill_design_activities_kind CHECK (kind IN (
                   'request_accepted', 'attempt_started', 'reasoning',
                   'tool_started', 'tool_completed', 'tool_failed',
                   'candidate_generated', 'validation_started',
                   'validation_passed', 'validation_failed', 'repair_started',
                   'run_terminal', 'commit_accepted',
                   'commit_validation_started', 'commit_validation_passed',
                   'commit_persistence_started',
                   'commit_persistence_completed', 'commit_terminal'
               )),
               CONSTRAINT fk_skill_design_activities_session
                   FOREIGN KEY (project_id, owner_user_id, session_id)
                   REFERENCES skill_design_sessions
                       (project_id, owner_user_id, id) ON DELETE CASCADE,
               CONSTRAINT fk_skill_design_activities_operation
                   FOREIGN KEY (project_id, owner_user_id, session_id, operation_id)
                   REFERENCES skill_design_operations
                       (project_id, owner_user_id, session_id, id) ON DELETE CASCADE
           )""",
    )
    op.execute(
        """CREATE INDEX ix_skill_design_activities_session_seq
           ON skill_design_activities
               (project_id, owner_user_id, session_id, seq)""",
    )
    op.execute(
        """CREATE UNIQUE INDEX uq_skill_design_activities_source_event
           ON skill_design_activities (operation_id, source_event_id)
           WHERE source_event_id IS NOT NULL""",
    )
    op.execute(
        """CREATE UNIQUE INDEX uq_skill_design_activities_terminal
           ON skill_design_activities (operation_id)
           WHERE kind IN ('run_terminal', 'commit_terminal')""",
    )
    op.execute(
        """CREATE TABLE skill_design_operation_baseline_files (
               project_id UUID NOT NULL,
               owner_user_id VARCHAR(36) NOT NULL,
               session_id UUID NOT NULL,
               operation_id UUID NOT NULL,
               path VARCHAR(1024) NOT NULL,
               media_type VARCHAR(255) NOT NULL,
               size_bytes BIGINT NOT NULL,
               sha256 CHAR(64) NOT NULL,
               content BYTEA NOT NULL,
               created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
               CONSTRAINT pk_skill_design_operation_baseline_files
                   PRIMARY KEY (
                       project_id, owner_user_id, session_id, operation_id, path
                   ),
               CONSTRAINT fk_skill_design_operation_baseline_files_operation
                   FOREIGN KEY (project_id, owner_user_id, session_id, operation_id)
                   REFERENCES skill_design_operations
                       (project_id, owner_user_id, session_id, id) ON DELETE CASCADE,
               CONSTRAINT ck_skill_design_operation_baseline_files_safe_path
                   CHECK (path <> '' AND path !~ '(^/|(^|/)\\.\\.(/|$))'),
               CONSTRAINT ck_skill_design_operation_baseline_files_size
                   CHECK (size_bytes >= 0 AND size_bytes <= 2097152),
               CONSTRAINT ck_skill_design_operation_baseline_files_content_size
                   CHECK (size_bytes = octet_length(content)),
               CONSTRAINT ck_skill_design_operation_baseline_files_sha256
                   CHECK (sha256 ~ '^[0-9a-f]{64}$')
           )""",
    )
    for statement in (
        "COMMENT ON COLUMN skill_design_sessions.execution_model_ref IS '技能设计会话：执行模型引用。'",
        "COMMENT ON COLUMN skill_design_sessions.execution_mode IS '技能设计会话：执行模式。'",
        "COMMENT ON COLUMN skill_design_sessions.execution_thinking_enabled IS '技能设计会话：执行思考启用。'",
        "COMMENT ON COLUMN skill_design_sessions.execution_reasoning_effort IS '技能设计会话：执行推理强度。'",
        "COMMENT ON COLUMN skill_design_operations.stop_requested_at IS '技能设计操作：停止请求时间。'",
        "COMMENT ON TABLE skill_design_activities IS '保存技能设计会话中可回放的公开思考与执行过程。'",
        "COMMENT ON COLUMN skill_design_activities.seq IS '技能设计活动：单调序号。'",
        "COMMENT ON COLUMN skill_design_activities.project_id IS '技能设计活动：所属项目标识。'",
        "COMMENT ON COLUMN skill_design_activities.owner_user_id IS '技能设计活动：私有数据所有者的用户标识。'",
        "COMMENT ON COLUMN skill_design_activities.session_id IS '技能设计活动：会话标识。'",
        "COMMENT ON COLUMN skill_design_activities.operation_id IS '技能设计活动：操作标识。'",
        "COMMENT ON COLUMN skill_design_activities.run_id IS '技能设计活动：运行标识。'",
        "COMMENT ON COLUMN skill_design_activities.attempt IS '技能设计活动：尝试。'",
        "COMMENT ON COLUMN skill_design_activities.source_event_id IS '技能设计活动：来源事件标识。'",
        "COMMENT ON COLUMN skill_design_activities.kind IS '技能设计活动：业务类型。'",
        "COMMENT ON COLUMN skill_design_activities.payload_json IS '技能设计活动：公开载荷 JSON 数据。'",
        "COMMENT ON COLUMN skill_design_activities.created_at IS '技能设计活动：记录创建时间。'",
        "COMMENT ON TABLE skill_design_operation_baseline_files IS '保存技能生成轮次开始前用于停止或失败回滚的草稿快照。'",
        "COMMENT ON COLUMN skill_design_operation_baseline_files.project_id IS '技能设计操作基线文件：所属项目标识。'",
        "COMMENT ON COLUMN skill_design_operation_baseline_files.owner_user_id IS '技能设计操作基线文件：私有数据所有者的用户标识。'",
        "COMMENT ON COLUMN skill_design_operation_baseline_files.session_id IS '技能设计操作基线文件：会话标识。'",
        "COMMENT ON COLUMN skill_design_operation_baseline_files.operation_id IS '技能设计操作基线文件：操作标识。'",
        "COMMENT ON COLUMN skill_design_operation_baseline_files.path IS '技能设计操作基线文件：路径。'",
        "COMMENT ON COLUMN skill_design_operation_baseline_files.media_type IS '技能设计操作基线文件：媒体类型。'",
        "COMMENT ON COLUMN skill_design_operation_baseline_files.size_bytes IS '技能设计操作基线文件：大小字节数。'",
        "COMMENT ON COLUMN skill_design_operation_baseline_files.sha256 IS '技能设计操作基线文件：内容的 SHA-256 摘要。'",
        "COMMENT ON COLUMN skill_design_operation_baseline_files.content IS '技能设计操作基线文件：技能设计操作基线文件的原始字节内容。'",
        "COMMENT ON COLUMN skill_design_operation_baseline_files.created_at IS '技能设计操作基线文件：记录创建时间。'",
    ):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead",
    )
