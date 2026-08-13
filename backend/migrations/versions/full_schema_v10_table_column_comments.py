"""Document every initialized application, Alembic, LangGraph, and partition table."""

from __future__ import annotations

import hashlib
from pathlib import Path

from alembic import op

revision = "full_schema_v10"
down_revision = "full_schema_v9"
branch_labels = None
depends_on = None

_COMMENTS_PATH = Path(__file__).with_name("full_schema_v10_comments.sql")
_COMMENTS_SHA256 = "451cf95164716a3914e6090b158b18fe6e3edc986d8684d30ac6042d610f0f2f"

_ENSURE_PARTITION_SQL = """CREATE OR REPLACE FUNCTION ensure_run_events_month_partition(target_at TIMESTAMP WITH TIME ZONE)
RETURNS text AS $$
DECLARE
    month_start TIMESTAMP WITH TIME ZONE;
    month_end TIMESTAMP WITH TIME ZONE;
    partition_name text;
    retention_watermark TIMESTAMP WITH TIME ZONE;
    parent_table_comment text;
    parent_column record;
BEGIN
    IF target_at IS NULL THEN
        RAISE EXCEPTION 'run event partition timestamp is required'
            USING ERRCODE = 'not_null_violation';
    END IF;
    SELECT retained_from
      INTO retention_watermark
      FROM run_event_partition_state
     WHERE singleton;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'run event partition state is missing'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF retention_watermark IS NOT NULL AND target_at < retention_watermark THEN
        RAISE EXCEPTION 'run event timestamp precedes the retention watermark'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    month_start := date_trunc('month', target_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    month_end := month_start + INTERVAL '1 month';
    partition_name := 'run_events_p' || to_char(month_start AT TIME ZONE 'UTC', 'YYYYMM');
    IF to_regclass(partition_name) IS NOT NULL THEN
        RETURN partition_name;
    END IF;
    LOCK TABLE run_events IN ACCESS EXCLUSIVE MODE;
    SELECT retained_from
      INTO retention_watermark
      FROM run_event_partition_state
     WHERE singleton
       FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'run event partition state is missing'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF retention_watermark IS NOT NULL AND target_at < retention_watermark THEN
        RAISE EXCEPTION 'run event timestamp precedes the retention watermark'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF to_regclass(partition_name) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF run_events FOR VALUES FROM (%L) TO (%L)',
            partition_name,
            month_start,
            month_end
        );
        parent_table_comment := obj_description('run_events'::regclass, 'pg_class');
        IF parent_table_comment IS NULL OR btrim(parent_table_comment) = '' THEN
            RAISE EXCEPTION 'run_events table comment is missing'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        EXECUTE format(
            'COMMENT ON TABLE %I IS %L',
            partition_name,
            parent_table_comment
        );
        FOR parent_column IN
            SELECT attribute.attname,
                   col_description(attribute.attrelid, attribute.attnum) AS description
              FROM pg_attribute attribute
             WHERE attribute.attrelid = 'run_events'::regclass
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
             ORDER BY attribute.attnum
        LOOP
            IF parent_column.description IS NULL
               OR btrim(parent_column.description) = '' THEN
                RAISE EXCEPTION 'run_events column comment is missing'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            EXECUTE format(
                'COMMENT ON COLUMN %I.%I IS %L',
                partition_name,
                parent_column.attname,
                parent_column.description
            );
        END LOOP;
    END IF;
    RETURN partition_name;
END;
$$ LANGUAGE plpgsql"""

_BACKFILL_PARTITION_COMMENTS_SQL = """DO $$
DECLARE
    child record;
    parent_column record;
    parent_table_comment text;
BEGIN
    parent_table_comment := obj_description('run_events'::regclass, 'pg_class');
    IF parent_table_comment IS NULL OR btrim(parent_table_comment) = '' THEN
        RAISE EXCEPTION 'run_events table comment is missing';
    END IF;
    FOR child IN
        SELECT child_relation.relname
          FROM pg_inherits inheritance
          JOIN pg_class parent_relation
            ON parent_relation.oid = inheritance.inhparent
          JOIN pg_class child_relation
            ON child_relation.oid = inheritance.inhrelid
          JOIN pg_namespace child_namespace
            ON child_namespace.oid = child_relation.relnamespace
         WHERE parent_relation.oid = 'run_events'::regclass
           AND child_namespace.nspname = current_schema()
         ORDER BY child_relation.relname
    LOOP
        EXECUTE format(
            'COMMENT ON TABLE %I IS %L',
            child.relname,
            parent_table_comment
        );
        FOR parent_column IN
            SELECT attribute.attname,
                   col_description(attribute.attrelid, attribute.attnum) AS description
              FROM pg_attribute attribute
             WHERE attribute.attrelid = 'run_events'::regclass
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
             ORDER BY attribute.attnum
        LOOP
            IF parent_column.description IS NULL
               OR btrim(parent_column.description) = '' THEN
                RAISE EXCEPTION 'run_events column comment is missing';
            END IF;
            EXECUTE format(
                'COMMENT ON COLUMN %I.%I IS %L',
                child.relname,
                parent_column.attname,
                parent_column.description
            );
        END LOOP;
    END LOOP;
END;
$$"""

_LANGGRAPH_COMMENTS_SQL = """DO $$
DECLARE
    present_table_count integer;
BEGIN
    SELECT count(*)
      INTO present_table_count
      FROM pg_class relation
      JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
     WHERE namespace.nspname = current_schema()
       AND relation.relkind IN ('r', 'p')
       AND relation.relname = ANY(ARRAY[
           'checkpoint_blobs', 'checkpoint_migrations', 'checkpoint_writes',
           'checkpoints', 'store', 'store_migrations'
       ]);
    IF present_table_count = 0 THEN
        RETURN;
    END IF;
    IF present_table_count <> 6 OR EXISTS (
        WITH expected(table_name, column_name) AS (
            VALUES
                ('checkpoint_blobs', 'thread_id'),
                ('checkpoint_blobs', 'checkpoint_ns'),
                ('checkpoint_blobs', 'channel'),
                ('checkpoint_blobs', 'version'),
                ('checkpoint_blobs', 'type'),
                ('checkpoint_blobs', 'blob'),
                ('checkpoint_migrations', 'v'),
                ('checkpoint_writes', 'thread_id'),
                ('checkpoint_writes', 'checkpoint_ns'),
                ('checkpoint_writes', 'checkpoint_id'),
                ('checkpoint_writes', 'task_id'),
                ('checkpoint_writes', 'idx'),
                ('checkpoint_writes', 'channel'),
                ('checkpoint_writes', 'type'),
                ('checkpoint_writes', 'blob'),
                ('checkpoint_writes', 'task_path'),
                ('checkpoints', 'thread_id'),
                ('checkpoints', 'checkpoint_ns'),
                ('checkpoints', 'checkpoint_id'),
                ('checkpoints', 'parent_checkpoint_id'),
                ('checkpoints', 'type'),
                ('checkpoints', 'checkpoint'),
                ('checkpoints', 'metadata'),
                ('store', 'prefix'),
                ('store', 'key'),
                ('store', 'value'),
                ('store', 'created_at'),
                ('store', 'updated_at'),
                ('store', 'expires_at'),
                ('store', 'ttl_minutes'),
                ('store_migrations', 'v')
        ),
        actual AS (
            SELECT relation.relname, attribute.attname
              FROM pg_class relation
              JOIN pg_namespace namespace
                ON namespace.oid = relation.relnamespace
              JOIN pg_attribute attribute
                ON attribute.attrelid = relation.oid
             WHERE namespace.nspname = current_schema()
               AND relation.relkind IN ('r', 'p')
               AND relation.relname = ANY(ARRAY[
                   'checkpoint_blobs', 'checkpoint_migrations',
                   'checkpoint_writes', 'checkpoints', 'store',
                   'store_migrations'
               ])
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
        ),
        difference AS (
            (SELECT * FROM expected EXCEPT SELECT * FROM actual)
            UNION ALL
            (SELECT * FROM actual EXCEPT SELECT * FROM expected)
        )
        SELECT 1 FROM difference
    ) THEN
        RAISE EXCEPTION 'LangGraph schema does not match the v10 comment contract';
    END IF;

    EXECUTE 'COMMENT ON TABLE checkpoint_blobs IS ''LangGraph 检查点各通道版本对应的序列化数据。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_blobs.thread_id IS ''所属 LangGraph 线程标识。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_blobs.checkpoint_ns IS ''检查点命名空间。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_blobs.channel IS ''状态通道名称。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_blobs.version IS ''通道数据版本标识。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_blobs.type IS ''通道数据的序列化类型。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_blobs.blob IS ''通道数据的序列化二进制内容。''';
    EXECUTE 'COMMENT ON TABLE checkpoint_migrations IS ''LangGraph 检查点表结构的迁移版本记录。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_migrations.v IS ''已应用的检查点迁移版本号。''';
    EXECUTE 'COMMENT ON TABLE checkpoint_writes IS ''LangGraph 检查点任务产生的待处理通道写入。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_writes.thread_id IS ''所属 LangGraph 线程标识。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_writes.checkpoint_ns IS ''检查点命名空间。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_writes.checkpoint_id IS ''写入所属的检查点标识。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_writes.task_id IS ''产生写入的任务标识。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_writes.idx IS ''同一任务内的写入顺序编号。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_writes.channel IS ''写入目标的状态通道名称。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_writes.type IS ''写入数据的序列化类型。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_writes.blob IS ''写入数据的序列化二进制内容。''';
    EXECUTE 'COMMENT ON COLUMN checkpoint_writes.task_path IS ''任务在图执行过程中的路径，用于稳定排序。''';
    EXECUTE 'COMMENT ON TABLE checkpoints IS ''LangGraph 线程检查点的序列化状态和元数据。''';
    EXECUTE 'COMMENT ON COLUMN checkpoints.thread_id IS ''所属 LangGraph 线程标识。''';
    EXECUTE 'COMMENT ON COLUMN checkpoints.checkpoint_ns IS ''检查点命名空间。''';
    EXECUTE 'COMMENT ON COLUMN checkpoints.checkpoint_id IS ''检查点标识。''';
    EXECUTE 'COMMENT ON COLUMN checkpoints.parent_checkpoint_id IS ''父检查点标识；根检查点为空。''';
    EXECUTE 'COMMENT ON COLUMN checkpoints.type IS ''检查点的序列化类型兼容字段。''';
    EXECUTE 'COMMENT ON COLUMN checkpoints.checkpoint IS ''检查点状态的 JSON 数据。''';
    EXECUTE 'COMMENT ON COLUMN checkpoints.metadata IS ''检查点附加元数据的 JSON 数据。''';
    EXECUTE 'COMMENT ON TABLE store IS ''LangGraph 跨线程存储的命名空间键值数据。''';
    EXECUTE 'COMMENT ON COLUMN store.prefix IS ''存储项命名空间的编码前缀。''';
    EXECUTE 'COMMENT ON COLUMN store.key IS ''命名空间内的存储项键。''';
    EXECUTE 'COMMENT ON COLUMN store.value IS ''存储项内容的 JSON 数据。''';
    EXECUTE 'COMMENT ON COLUMN store.created_at IS ''存储项创建时间。''';
    EXECUTE 'COMMENT ON COLUMN store.updated_at IS ''存储项最后更新时间。''';
    EXECUTE 'COMMENT ON COLUMN store.expires_at IS ''存储项到期时间；永久有效时为空。''';
    EXECUTE 'COMMENT ON COLUMN store.ttl_minutes IS ''存储项的生存时长分钟数；未设置时为空。''';
    EXECUTE 'COMMENT ON TABLE store_migrations IS ''LangGraph 跨线程存储表结构的迁移版本记录。''';
    EXECUTE 'COMMENT ON COLUMN store_migrations.v IS ''已应用的跨线程存储迁移版本号。''';
END;
$$"""


def _application_comment_statements() -> tuple[str, ...]:
    try:
        payload = _COMMENTS_PATH.read_bytes()
    except OSError:
        raise RuntimeError("full_schema_v10 comment resource is unavailable") from None
    if hashlib.sha256(payload).hexdigest() != _COMMENTS_SHA256:
        raise RuntimeError("full_schema_v10 comment resource checksum mismatch")
    statements: list[str] = []
    for raw_line in payload.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        if not line.endswith(";") or not line.startswith(
            ("COMMENT ON TABLE ", "COMMENT ON COLUMN "),
        ):
            raise RuntimeError("full_schema_v10 comment resource is malformed")
        statements.append(line[:-1])
    if sum(item.startswith("COMMENT ON TABLE ") for item in statements) != 84:
        raise RuntimeError("full_schema_v10 table comment coverage is invalid")
    if sum(item.startswith("COMMENT ON COLUMN ") for item in statements) != 1006:
        raise RuntimeError("full_schema_v10 column comment coverage is invalid")
    return tuple(statements)


def upgrade() -> None:
    for statement in _application_comment_statements():
        op.execute(statement)
    op.execute(_ENSURE_PARTITION_SQL)
    op.execute(_BACKFILL_PARTITION_COMMENTS_SQL)
    op.execute(_LANGGRAPH_COMMENTS_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
