"""Add the durable thread-scoped Dream preparation job and state machine."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v11"
down_revision = "full_schema_v10"
branch_labels = None
depends_on = None


_CREATE_PREPARATION_TABLE = """CREATE TABLE memory_dream_prepare_runs (
    job_id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    namespace VARCHAR(255) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    operation_id UUID NOT NULL,
    request_id VARCHAR(512) NOT NULL,
    phase VARCHAR(24) DEFAULT 'queued' NOT NULL,
    compacted_passes INTEGER DEFAULT 0 NOT NULL,
    last_checkpoint_id VARCHAR(128),
    dream_job_id UUID,
    history_count INTEGER,
    admission_kind VARCHAR(16),
    result_disposition VARCHAR(24) DEFAULT 'queued' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (job_id),
    CONSTRAINT ck_memory_dream_prepare_runs_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_dream_prepare_runs_request CHECK (request_id <> ''),
    CONSTRAINT ck_memory_dream_prepare_runs_phase CHECK (phase IN ('queued', 'draining', 'verifying', 'dream_admitted', 'succeeded', 'cancelled', 'failed')),
    CONSTRAINT ck_memory_dream_prepare_runs_disposition CHECK (result_disposition IN ('queued', 'already_running', 'nothing_pending', 'cancelled', 'failed')),
    CONSTRAINT ck_memory_dream_prepare_runs_passes CHECK (compacted_passes >= 0),
    CONSTRAINT ck_memory_dream_prepare_runs_terminal CHECK ((phase IN ('succeeded', 'cancelled', 'failed')) = (completed_at IS NOT NULL)),
    CONSTRAINT ck_memory_dream_prepare_runs_child CHECK (
        (dream_job_id IS NULL AND admission_kind IS NULL AND
         (history_count IS NULL OR
          (result_disposition = 'nothing_pending' AND history_count = 0)))
        OR
        (dream_job_id IS NOT NULL AND history_count BETWEEN 0 AND 20 AND
         admission_kind IN ('history', 'budget_rewrite'))
    ),
    CONSTRAINT ck_memory_dream_prepare_runs_admission_kind CHECK ((admission_kind = 'budget_rewrite') = (dream_job_id IS NOT NULL AND history_count = 0)),
    CONSTRAINT uq_memory_dream_prepare_runs_job_scope UNIQUE (job_id, project_id, owner_user_id, namespace),
    CONSTRAINT uq_memory_dream_prepare_runs_operation UNIQUE (project_id, owner_user_id, operation_id),
    CONSTRAINT fk_memory_dream_prepare_runs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_job FOREIGN KEY(job_id, project_id, owner_user_id, namespace) REFERENCES jobs (id, project_id, owner_user_id, namespace) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_dream FOREIGN KEY(dream_job_id, project_id, owner_user_id, namespace) REFERENCES memory_dream_runs (job_id, project_id, owner_user_id, namespace) ON DELETE RESTRICT
)"""

_COMMENTS = (
    "COMMENT ON TABLE memory_dream_prepare_runs IS '保存线程消息排空、进度恢复与子记忆整理任务准入的持久化状态。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.job_id IS '记忆整理准备运行：任务标识。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.project_id IS '记忆整理准备运行：所属项目标识。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.owner_user_id IS '记忆整理准备运行：私有数据所有者的用户标识。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.namespace IS '记忆整理准备运行：私有数据命名空间。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.thread_id IS '记忆整理准备运行：线程标识。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.operation_id IS '记忆整理准备运行：操作标识。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.request_id IS '记忆整理准备运行：请求标识。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.phase IS '记忆整理准备运行：阶段。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.compacted_passes IS '记忆整理准备运行：压缩轮次。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.last_checkpoint_id IS '记忆整理准备运行：最近检查点标识。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.dream_job_id IS '记忆整理准备运行：记忆整理任务标识。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.history_count IS '记忆整理准备运行：历史数量。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.admission_kind IS '记忆整理准备运行：准入类型。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.result_disposition IS '记忆整理准备运行：结果处置。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.created_at IS '记忆整理准备运行：记录创建时间。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.updated_at IS '记忆整理准备运行：记录最近更新时间。'",
    "COMMENT ON COLUMN memory_dream_prepare_runs.completed_at IS '记忆整理准备运行：完成时间。'",
)


def upgrade() -> None:
    op.execute("ALTER TABLE jobs DROP CONSTRAINT ck_jobs_authority_shape")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT ck_jobs_memory_namespace")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT ck_jobs_type")
    op.execute(
        """ALTER TABLE jobs ADD CONSTRAINT ck_jobs_authority_shape CHECK (
        (job_type = 'private_run' AND run_id IS NOT NULL AND
         owner_user_id IS NOT NULL AND automation_occurrence_id IS NULL AND
         origin_trace_id IS NOT NULL)
        OR (job_type = 'automation_run' AND run_id IS NOT NULL AND
            owner_user_id IS NOT NULL AND
            automation_occurrence_id IS NOT NULL AND
            origin_trace_id IS NOT NULL)
        OR (job_type = 'retention_purge' AND run_id IS NULL AND
            automation_occurrence_id IS NULL AND origin_trace_id IS NULL)
        OR (job_type = 'mcp_discovery' AND owner_user_id IS NOT NULL AND
            run_id IS NULL AND automation_occurrence_id IS NULL AND
            origin_trace_id IS NULL)
        OR (job_type = 'memory_dream' AND owner_user_id IS NOT NULL AND
            namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND
            automation_occurrence_id IS NULL AND origin_trace_id IS NULL)
        OR (job_type = 'memory_dream_prepare' AND owner_user_id IS NOT NULL AND
            namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND
            automation_occurrence_id IS NULL AND origin_trace_id IS NULL)
        OR (job_type = 'memory_seal' AND owner_user_id IS NOT NULL AND
            namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND
            automation_occurrence_id IS NULL AND origin_trace_id IS NULL)
        )"""
    )
    op.execute("ALTER TABLE jobs ADD CONSTRAINT ck_jobs_memory_namespace CHECK ((job_type IN ('memory_dream', 'memory_dream_prepare', 'memory_seal')) = (namespace IS NOT NULL))")
    op.execute("ALTER TABLE jobs ADD CONSTRAINT ck_jobs_type CHECK (job_type IN ('private_run', 'automation_run', 'retention_purge', 'mcp_discovery', 'memory_dream', 'memory_dream_prepare', 'memory_seal'))")
    op.execute(_CREATE_PREPARATION_TABLE)
    op.execute("CREATE UNIQUE INDEX uq_memory_dream_prepare_runs_active_thread ON memory_dream_prepare_runs (project_id, owner_user_id, thread_id) WHERE completed_at IS NULL")
    op.execute("CREATE INDEX ix_memory_dream_prepare_runs_scope_updated ON memory_dream_prepare_runs (project_id, owner_user_id, updated_at DESC, job_id DESC)")
    for statement in _COMMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
