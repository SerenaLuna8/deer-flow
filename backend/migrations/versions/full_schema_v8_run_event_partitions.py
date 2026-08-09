"""Partition run events by UTC month without weakening global invariants."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v8"
down_revision = "full_schema_v7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER trg_run_events_stream_terminal ON run_events")
    op.execute("ALTER SEQUENCE run_events_id_seq OWNED BY NONE")
    op.execute("ALTER TABLE run_events ALTER COLUMN id DROP DEFAULT")
    op.execute("ALTER TABLE run_events RENAME TO run_events_v7")
    op.execute("ALTER TABLE run_events_v7 DROP CONSTRAINT run_events_pkey")
    op.execute("ALTER TABLE run_events_v7 DROP CONSTRAINT uq_run_events_private_seq")
    op.execute("ALTER TABLE run_events_v7 DROP CONSTRAINT uq_events_thread_seq")
    op.execute("DROP INDEX ix_events_run")
    op.execute("DROP INDEX ix_events_thread_cat_seq")
    op.execute("DROP INDEX ix_run_events_owner_user_id")
    op.execute("DROP INDEX ix_run_events_project_id")
    op.execute("DROP INDEX uq_run_events_stream_terminal")

    op.execute(
        """CREATE TABLE run_event_partition_state (
            singleton BOOLEAN DEFAULT true NOT NULL,
            retained_from TIMESTAMP WITH TIME ZONE,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (singleton),
            CONSTRAINT ck_run_event_partition_state_singleton CHECK (singleton)
        )"""
    )
    op.execute("INSERT INTO run_event_partition_state (singleton) VALUES (true)")
    op.execute(
        """CREATE TABLE run_event_invariants (
            id BIGINT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            project_id UUID NOT NULL,
            owner_user_id VARCHAR(36) NOT NULL,
            thread_id VARCHAR(64) NOT NULL,
            run_id VARCHAR(64) NOT NULL,
            seq BIGINT NOT NULL,
            is_stream_terminal BOOLEAN DEFAULT false NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT fk_run_event_invariants_private_run
                FOREIGN KEY(project_id, owner_user_id, thread_id, run_id)
                REFERENCES runs (project_id, owner_user_id, thread_id, run_id)
                ON DELETE CASCADE,
            CONSTRAINT uq_run_events_private_seq
                UNIQUE (project_id, owner_user_id, thread_id, run_id, seq),
            CONSTRAINT uq_events_thread_seq UNIQUE (thread_id, seq)
        )"""
    )
    op.execute("CREATE INDEX ix_run_event_invariants_created_at ON run_event_invariants (created_at)")
    op.execute(
        """CREATE UNIQUE INDEX uq_run_events_stream_terminal
        ON run_event_invariants (project_id, owner_user_id, thread_id, run_id)
        WHERE is_stream_terminal"""
    )
    op.execute(
        """CREATE TABLE run_events (
            id BIGINT DEFAULT nextval('run_events_id_seq'::regclass) NOT NULL,
            thread_id VARCHAR(64) NOT NULL,
            run_id VARCHAR(64) NOT NULL,
            owner_user_id VARCHAR(36) NOT NULL,
            event_type VARCHAR(32) NOT NULL,
            category VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            event_metadata JSON NOT NULL,
            seq BIGINT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            project_id UUID NOT NULL,
            PRIMARY KEY (id, created_at),
            CONSTRAINT fk_run_events_owner
                FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_run_events_private_run
                FOREIGN KEY(project_id, owner_user_id, thread_id, run_id)
                REFERENCES runs (project_id, owner_user_id, thread_id, run_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_run_events_project_membership
                FOREIGN KEY(project_id, owner_user_id)
                REFERENCES project_memberships (project_id, user_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_run_events_project
                FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
        ) PARTITION BY RANGE (created_at)"""
    )
    op.execute("CREATE INDEX ix_events_run ON run_events (thread_id, run_id, seq)")
    op.execute("CREATE INDEX ix_events_thread_cat_seq ON run_events (thread_id, category, seq)")
    op.execute("CREATE INDEX ix_run_events_owner_user_id ON run_events (owner_user_id)")
    op.execute("CREATE INDEX ix_run_events_project_id ON run_events (project_id)")
    op.execute(
        """CREATE INDEX ix_run_events_stream_terminal
        ON run_events (project_id, owner_user_id, thread_id, run_id)
        WHERE category = 'stream' AND event_type = 'stream.end'"""
    )

    op.execute(
        """CREATE OR REPLACE FUNCTION ensure_run_events_month_partition(target_at TIMESTAMP WITH TIME ZONE)
        RETURNS text AS $$
        DECLARE
            month_start TIMESTAMP WITH TIME ZONE;
            month_end TIMESTAMP WITH TIME ZONE;
            partition_name text;
            retention_watermark TIMESTAMP WITH TIME ZONE;
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
            END IF;
            RETURN partition_name;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION drop_run_event_partitions_before(cutoff_at TIMESTAMP WITH TIME ZONE)
        RETURNS integer AS $$
        DECLARE
            keep_from TIMESTAMP WITH TIME ZONE;
            retention_watermark TIMESTAMP WITH TIME ZONE;
            month_key text;
            month_start TIMESTAMP WITH TIME ZONE;
            month_end TIMESTAMP WITH TIME ZONE;
            partition_name text;
            dropped integer := 0;
        BEGIN
            IF cutoff_at IS NULL THEN
                RAISE EXCEPTION 'run event retention cutoff is required'
                    USING ERRCODE = 'not_null_violation';
            END IF;
            IF NOT isfinite(cutoff_at) OR cutoff_at > clock_timestamp() THEN
                RAISE EXCEPTION 'run event retention cutoff cannot be in the future'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            keep_from := date_trunc('month', cutoff_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
            LOCK TABLE run_events IN ACCESS EXCLUSIVE MODE;
            SELECT retained_from
              INTO retention_watermark
              FROM run_event_partition_state
             WHERE singleton
               FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'run event partition state is missing'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF retention_watermark IS NULL OR keep_from > retention_watermark THEN
                retention_watermark := keep_from;
            END IF;
            UPDATE run_event_partition_state
               SET retained_from = retention_watermark,
                   updated_at = now()
             WHERE singleton;
            FOR partition_name IN
                SELECT child.relname
                  FROM pg_inherits inheritance
                  JOIN pg_class parent ON parent.oid = inheritance.inhparent
                  JOIN pg_class child ON child.oid = inheritance.inhrelid
                  JOIN pg_namespace namespace ON namespace.oid = child.relnamespace
                 WHERE parent.oid = 'run_events'::regclass
                   AND namespace.nspname = current_schema()
                   AND child.relname ~ '^run_events_p[0-9]{6}$'
                 ORDER BY child.relname
            LOOP
                month_key := substring(partition_name FROM '^run_events_p([0-9]{6})$');
                month_start := to_date(month_key, 'YYYYMM')::timestamp AT TIME ZONE 'UTC';
                month_end := month_start + INTERVAL '1 month';
                IF month_end <= retention_watermark THEN
                    EXECUTE format('DROP TABLE %I', partition_name);
                    DELETE FROM run_event_invariants
                     WHERE created_at >= month_start AND created_at < month_end;
                    dropped := dropped + 1;
                END IF;
            END LOOP;
            PERFORM ensure_run_events_month_partition(now());
            PERFORM ensure_run_events_month_partition(now() + INTERVAL '1 month');
            RETURN dropped;
        END;
        $$ LANGUAGE plpgsql"""
    )

    op.execute(
        """DO $$
        DECLARE existing_month record;
        BEGIN
            FOR existing_month IN
                SELECT DISTINCT date_trunc(
                    'month', created_at AT TIME ZONE 'UTC'
                ) AT TIME ZONE 'UTC' AS month_start
                FROM run_events_v7
            LOOP
                PERFORM ensure_run_events_month_partition(existing_month.month_start);
            END LOOP;
            PERFORM ensure_run_events_month_partition(now());
            PERFORM ensure_run_events_month_partition(now() + INTERVAL '1 month');
        END;
        $$"""
    )
    op.execute(
        """INSERT INTO run_event_invariants (
            id, created_at, project_id, owner_user_id, thread_id, run_id, seq,
            is_stream_terminal
        )
        SELECT id, created_at, project_id, owner_user_id, thread_id, run_id, seq,
               category = 'stream' AND event_type = 'stream.end'
          FROM run_events_v7"""
    )
    op.execute(
        """INSERT INTO run_events (
            id, thread_id, run_id, owner_user_id, event_type, category, content,
            event_metadata, seq, created_at, project_id
        )
        SELECT id, thread_id, run_id, owner_user_id, event_type, category, content,
               event_metadata, seq, created_at, project_id
          FROM run_events_v7"""
    )
    op.execute("DROP TABLE run_events_v7")
    op.execute("ALTER SEQUENCE run_events_id_seq OWNED BY run_events.id")
    op.execute(
        """SELECT setval(
            'run_events_id_seq',
            COALESCE(MAX(id), 1),
            MAX(id) IS NOT NULL
        ) FROM run_events"""
    )

    op.execute(
        """CREATE OR REPLACE FUNCTION enforce_stream_terminal_invariant()
        RETURNS trigger AS $$
        BEGIN
            -- Serialize every Thread's cross-partition invariant checks. The event
            -- store already holds this advisory lock, so normal writes are reentrant.
            PERFORM pg_advisory_xact_lock(hashtext(NEW.thread_id)::bigint);
            PERFORM 1
              FROM run_event_partition_state
             WHERE singleton
               AND (retained_from IS NULL OR NEW.created_at >= retained_from);
            IF NOT FOUND THEN
                RAISE EXCEPTION 'run event timestamp precedes the retention watermark'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            INSERT INTO run_event_invariants (
                id, created_at, project_id, owner_user_id, thread_id, run_id, seq,
                is_stream_terminal
            ) VALUES (
                NEW.id, NEW.created_at, NEW.project_id, NEW.owner_user_id,
                NEW.thread_id, NEW.run_id, NEW.seq,
                NEW.category = 'stream' AND NEW.event_type = 'stream.end'
            );
            IF NEW.category = 'stream' THEN
                IF NEW.event_type <> 'stream.end' AND EXISTS (
                    SELECT 1 FROM run_event_invariants
                     WHERE project_id = NEW.project_id
                       AND owner_user_id = NEW.owner_user_id
                       AND thread_id = NEW.thread_id
                       AND run_id = NEW.run_id
                       AND is_stream_terminal
                ) THEN
                    RAISE EXCEPTION 'stream event cannot follow terminal event'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION cleanup_run_event_invariant()
        RETURNS trigger AS $$
        BEGIN
            DELETE FROM run_event_invariants WHERE id = OLD.id;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION enforce_run_event_identity_immutable()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
               OR NEW.thread_id IS DISTINCT FROM OLD.thread_id
               OR NEW.run_id IS DISTINCT FROM OLD.run_id
               OR NEW.seq IS DISTINCT FROM OLD.seq
               OR NEW.category IS DISTINCT FROM OLD.category
               OR NEW.event_type IS DISTINCT FROM OLD.event_type THEN
                RAISE EXCEPTION 'run event identity is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER trg_run_events_stream_terminal
        BEFORE INSERT ON run_events FOR EACH ROW
        EXECUTE FUNCTION enforce_stream_terminal_invariant()"""
    )
    op.execute(
        """CREATE TRIGGER trg_run_events_identity_immutable
        BEFORE UPDATE OF id, created_at, project_id, owner_user_id, thread_id,
                         run_id, seq, category, event_type
        ON run_events FOR EACH ROW
        EXECUTE FUNCTION enforce_run_event_identity_immutable()"""
    )
    op.execute(
        """CREATE TRIGGER trg_run_events_invariant_cleanup
        AFTER DELETE ON run_events FOR EACH ROW
        EXECUTE FUNCTION cleanup_run_event_invariant()"""
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
