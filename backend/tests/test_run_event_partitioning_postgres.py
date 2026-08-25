"""Monthly ``run_events`` partitions and global invariants."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from support.agent_definition_seed import direct_agent_definition_fields

from deerflow.persistence.bootstrap import (
    SchemaRecreateRequired,
    bootstrap_schema,
    classify_database,
)
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.private_scope import PrivateResourceScope
from deerflow.runtime.events.store.db import DbRunEventStore

pytestmark = pytest.mark.postgres


async def _seed_event_scope(connection: AsyncConnection) -> dict[str, object]:
    owner_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    definition = direct_agent_definition_fields(
        updated_by_user_id=owner_id,
        description="Partition Agent",
    )
    thread_id = f"partition-{uuid.uuid4()}"
    run_ids = {
        "sequence": str(uuid.uuid4()),
        "terminal": str(uuid.uuid4()),
        "retained": str(uuid.uuid4()),
    }
    await connection.execute(
        text(
            """INSERT INTO users
            (id,email,system_role,created_at,needs_setup,token_version)
            VALUES (:id,:email,'user',now(),false,0)"""
        ),
        {"id": owner_id, "email": f"{owner_id}@example.com"},
    )
    await connection.execute(
        text(
            """INSERT INTO projects
            (id,slug,display_name,created_by_user_id)
            VALUES (:id,:slug,'Partition Project',:owner)"""
        ),
        {
            "id": project_id,
            "slug": f"partition-{project_id.hex[:12]}",
            "owner": owner_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO project_memberships
            (id,project_id,user_id,role,status,version)
            VALUES (:id,:project,:owner,'admin','active',1)"""
        ),
        {"id": membership_id, "project": project_id, "owner": owner_id},
    )
    await connection.execute(
        text(
            """INSERT INTO agents
            (id,scope,project_id,slug,display_name,status,definition_id,
             description,agents_instructions,soul,identity,user_context,
             model_ref,model_settings,tool_groups,payload_schema_version,
             payload_checksum,revision,created_by_user_id,updated_by_user_id)
            VALUES (:id,'project',:project,'partition-agent','Partition Agent',
                    'active',:definition_id,:description,:agents_instructions,
                    :soul,:identity,:user_context,:model_ref,'{}'::jsonb,
                    '[]'::jsonb,:payload_schema_version,:payload_checksum,1,
                    :owner,:updated_by_user_id)"""
        ),
        {
            "id": agent_id,
            "project": project_id,
            "owner": owner_id,
            **definition,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO threads_meta
            (thread_id,owner_user_id,status,metadata_json,created_at,updated_at,
             project_id,agent_asset_id,agent_scope)
            VALUES (:thread,:owner,'idle','{}',now(),now(),:project,:agent,'project')"""
        ),
        {
            "thread": thread_id,
            "owner": owner_id,
            "project": project_id,
            "agent": agent_id,
        },
    )
    for run_id in run_ids.values():
        await connection.execute(
            text(
                """INSERT INTO runs
                (run_id,thread_id,owner_user_id,status,multitask_strategy,
                 metadata_json,kwargs_json,origin_trace_id,message_count,
                 total_input_tokens,total_output_tokens,total_tokens,llm_call_count,
                 lead_agent_tokens,subagent_tokens,middleware_tokens,
                 created_at,updated_at,project_id,asset_closure_sealed)
                VALUES (:run,:thread,:owner,'success','reject','{}','{}',:trace,
                        0,0,0,0,0,0,0,0,now(),now(),:project,true)"""
            ),
            {
                "run": run_id,
                "thread": thread_id,
                "owner": owner_id,
                "trace": str(uuid.uuid4()),
                "project": project_id,
            },
        )
    await connection.execute(
        text(
            """INSERT INTO thread_event_sequences
            (project_id,owner_user_id,thread_id,high_watermark)
            VALUES (:project,:owner,:thread,0)"""
        ),
        {"project": project_id, "owner": owner_id, "thread": thread_id},
    )
    return {
        "project_id": project_id,
        "owner_user_id": owner_id,
        "thread_id": thread_id,
        "run_ids": run_ids,
    }


async def _insert_event(
    connection: AsyncConnection,
    scope: dict[str, object],
    *,
    event_id: int,
    run_id: str,
    seq: int,
    created_at: datetime,
    event_type: str = "delta",
    category: str = "stream",
    content: str = "{}",
) -> None:
    await connection.execute(
        text(
            """INSERT INTO run_events
            (id,thread_id,run_id,owner_user_id,event_type,category,content,
             event_metadata,seq,created_at,project_id)
            VALUES (:id,:thread,:run,:owner,:event_type,:category,:content,'{}',
                    :seq,:created_at,:project)"""
        ),
        {
            "id": event_id,
            "thread": scope["thread_id"],
            "run": run_id,
            "owner": scope["owner_user_id"],
            "event_type": event_type,
            "category": category,
            "content": content,
            "seq": seq,
            "created_at": created_at,
            "project": scope["project_id"],
        },
    )


def test_run_event_orm_declares_range_partition_and_global_guard() -> None:
    table = RunEventRow.__table__
    assert table.dialect_options["postgresql"]["partition_by"] == "RANGE (created_at)"
    assert tuple(column.name for column in table.primary_key.columns) == (
        "id",
        "created_at",
    )
    assert "run_event_invariants" in table.metadata.tables
    assert "run_event_partition_state" in table.metadata.tables


@pytest.mark.asyncio
async def test_orm_and_db_store_create_missing_month_partitions(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    april = datetime(2035, 4, 10, tzinfo=UTC)
    may = datetime(2035, 5, 10, tzinfo=UTC)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            scope = await _seed_event_scope(connection)
        run_ids = scope["run_ids"]
        assert isinstance(run_ids, dict)

        async with session_factory.begin() as session:
            session.add(
                RunEventRow(
                    id=40_001,
                    thread_id=scope["thread_id"],
                    run_id=run_ids["sequence"],
                    project_id=scope["project_id"],
                    owner_user_id=scope["owner_user_id"],
                    event_type="human_message",
                    category="message",
                    content="direct ORM insert",
                    event_metadata={},
                    seq=1,
                    created_at=april,
                )
            )
            await session.flush()
            default_timestamp_row = RunEventRow(
                id=40_002,
                thread_id=scope["thread_id"],
                run_id=run_ids["terminal"],
                project_id=scope["project_id"],
                owner_user_id=scope["owner_user_id"],
                event_type="human_message",
                category="message",
                content="ORM default timestamp insert",
                event_metadata={},
                seq=2,
            )
            session.add(default_timestamp_row)
            await session.flush()
            assert default_timestamp_row.created_at.tzinfo is not None
            await session.execute(
                text(
                    """UPDATE thread_event_sequences SET high_watermark=2
                         WHERE project_id=:project AND owner_user_id=:owner
                           AND thread_id=:thread"""
                ),
                {
                    "project": scope["project_id"],
                    "owner": scope["owner_user_id"],
                    "thread": scope["thread_id"],
                },
            )

        store = DbRunEventStore(
            session_factory,
            run_event_notify_enabled=False,
        )
        stored = await store.put(
            thread_id=scope["thread_id"],
            run_id=run_ids["retained"],
            event_type="ai_message",
            category="message",
            content="DbRunEventStore insert",
            created_at=may.isoformat(),
            scope=PrivateResourceScope(
                project_id=str(scope["project_id"]),
                owner_user_id=str(scope["owner_user_id"]),
                membership_version=1,
            ),
        )
        assert stored["created_at"] == may.isoformat()

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('run_events_p203504')")) == "run_events_p203504"
            assert await connection.scalar(text("SELECT to_regclass('run_events_p203505')")) == "run_events_p203505"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_monthly_partitions_keep_sequence_and_terminal_global(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    january = datetime(2024, 1, 10, tzinfo=UTC)
    february = datetime(2024, 2, 10, tzinfo=UTC)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            scope = await _seed_event_scope(connection)
            await connection.execute(
                text("SELECT ensure_run_events_month_partition(:month)"),
                [{"month": january}, {"month": february}],
            )
            run_ids = scope["run_ids"]
            assert isinstance(run_ids, dict)
            await _insert_event(
                connection,
                scope,
                event_id=10_001,
                run_id=run_ids["sequence"],
                seq=1,
                created_at=january,
            )
            await _insert_event(
                connection,
                scope,
                event_id=10_002,
                run_id=run_ids["terminal"],
                seq=2,
                created_at=january,
                event_type="stream.end",
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await _insert_event(
                    connection,
                    scope,
                    event_id=10_003,
                    run_id=run_ids["sequence"],
                    seq=1,
                    created_at=february,
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await _insert_event(
                    connection,
                    scope,
                    event_id=10_001,
                    run_id=run_ids["retained"],
                    seq=5,
                    created_at=february,
                    category="message",
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await _insert_event(
                    connection,
                    scope,
                    event_id=10_004,
                    run_id=run_ids["terminal"],
                    seq=3,
                    created_at=february,
                    event_type="stream.end",
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await _insert_event(
                    connection,
                    scope,
                    event_id=10_005,
                    run_id=run_ids["terminal"],
                    seq=4,
                    created_at=february,
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_age_retention_drops_complete_partitions_and_guard_rows(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    january = datetime(2024, 1, 10, tzinfo=UTC)
    march = datetime(2024, 3, 10, tzinfo=UTC)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            scope = await _seed_event_scope(connection)
            await connection.execute(
                text("SELECT ensure_run_events_month_partition(:month)"),
                [{"month": january}, {"month": march}],
            )
            run_ids = scope["run_ids"]
            assert isinstance(run_ids, dict)
            await _insert_event(
                connection,
                scope,
                event_id=20_001,
                run_id=run_ids["sequence"],
                seq=1,
                created_at=january,
                category="message",
            )
            await _insert_event(
                connection,
                scope,
                event_id=20_002,
                run_id=run_ids["retained"],
                seq=2,
                created_at=march,
                category="message",
            )
            dropped = await connection.scalar(
                text("SELECT drop_run_event_partitions_before(:cutoff)"),
                {"cutoff": datetime(2024, 3, 1, tzinfo=UTC)},
            )
            assert dropped == 1

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('run_events_p202401')")) is None
            assert await connection.scalar(text("SELECT count(*) FROM run_events")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM run_event_invariants")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retention_watermark_blocks_db_store_from_recreating_old_month(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    january = datetime(2024, 1, 10, tzinfo=UTC)
    cutoff = datetime(2024, 3, 1, tzinfo=UTC)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            scope = await _seed_event_scope(connection)
            await connection.execute(
                text("SELECT ensure_run_events_month_partition(:month)"),
                {"month": january},
            )
            run_ids = scope["run_ids"]
            assert isinstance(run_ids, dict)
            await _insert_event(
                connection,
                scope,
                event_id=21_001,
                run_id=run_ids["sequence"],
                seq=1,
                created_at=january,
                category="message",
            )
            assert (
                await connection.scalar(
                    text("SELECT drop_run_event_partitions_before(:cutoff)"),
                    {"cutoff": cutoff},
                )
                == 1
            )

        store = DbRunEventStore(
            session_factory,
            run_event_notify_enabled=False,
        )
        with pytest.raises(DBAPIError):
            await store.put(
                thread_id=scope["thread_id"],
                run_id=run_ids["sequence"],
                event_type="human_message",
                category="message",
                content="must not resurrect an expired partition",
                created_at=january.isoformat(),
                scope=PrivateResourceScope(
                    project_id=str(scope["project_id"]),
                    owner_user_id=str(scope["owner_user_id"]),
                    membership_version=1,
                ),
            )

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('run_events_p202401')")) is None
            assert (
                await connection.scalar(
                    text(
                        """SELECT retained_from FROM run_event_partition_state
                       WHERE singleton=true"""
                    )
                )
                == cutoff
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partition_drop_rejects_future_cutoff(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    future_cutoff = datetime.now(UTC) + timedelta(days=40)
    try:
        await bootstrap_schema(engine)
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT drop_run_event_partitions_before(:cutoff)"),
                    {"cutoff": future_cutoff},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retention_watermark_is_monotonic(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    march_cutoff = datetime(2024, 3, 1, tzinfo=UTC)
    older_cutoff = datetime(2024, 2, 1, tzinfo=UTC)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT drop_run_event_partitions_before(:cutoff)"),
                {"cutoff": march_cutoff},
            )
            await connection.execute(
                text("SELECT drop_run_event_partitions_before(:cutoff)"),
                {"cutoff": older_cutoff},
            )
            assert (
                await connection.scalar(
                    text(
                        """SELECT retained_from FROM run_event_partition_state
                       WHERE singleton=true"""
                    )
                )
                == march_cutoff
            )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT ensure_run_events_month_partition(:month)"),
                    {"month": datetime(2024, 2, 15, tzinfo=UTC)},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partition_drop_recreates_current_and_next_month_children(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    now = datetime.now(UTC)
    current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if current_month.month == 12:
        next_month = current_month.replace(
            year=current_month.year + 1,
            month=1,
        )
    else:
        next_month = current_month.replace(month=current_month.month + 1)
    current_partition = f"run_events_p{current_month:%Y%m}"
    next_partition = f"run_events_p{next_month:%Y%m}"
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP TABLE {current_partition}, {next_partition}"))
        async with engine.begin() as connection:
            assert (
                await connection.scalar(
                    text("SELECT drop_run_event_partitions_before(:cutoff)"),
                    {"cutoff": now},
                )
                == 0
            )
        async with engine.connect() as connection:
            assert await connection.scalar(text(f"SELECT to_regclass('{current_partition}')")) == current_partition
            assert await connection.scalar(text(f"SELECT to_regclass('{next_partition}')")) == next_partition
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_existing_partition_ensure_uses_lock_free_fast_path(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(text("SELECT ensure_run_events_month_partition(now())"))
            lock_modes = set(
                (
                    await connection.execute(
                        text(
                            """SELECT mode FROM pg_locks
                                WHERE pid=pg_backend_pid()
                                  AND relation='run_events'::regclass
                                  AND granted"""
                        )
                    )
                ).scalars()
            )
            assert "AccessExclusiveLock" not in lock_modes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scoped_row_purge_cleans_guard_without_dropping_shared_partition(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    january = datetime(2024, 1, 10, tzinfo=UTC)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            first_scope = await _seed_event_scope(connection)
            second_scope = await _seed_event_scope(connection)
            await connection.execute(
                text("SELECT ensure_run_events_month_partition(:month)"),
                {"month": january},
            )
            first_runs = first_scope["run_ids"]
            second_runs = second_scope["run_ids"]
            assert isinstance(first_runs, dict)
            assert isinstance(second_runs, dict)
            await _insert_event(
                connection,
                first_scope,
                event_id=23_001,
                run_id=first_runs["sequence"],
                seq=1,
                created_at=january,
                category="message",
            )
            await _insert_event(
                connection,
                second_scope,
                event_id=23_002,
                run_id=second_runs["sequence"],
                seq=1,
                created_at=january,
                category="message",
            )
            await connection.execute(
                text(
                    """DELETE FROM run_events
                        WHERE project_id=:project AND owner_user_id=:owner"""
                ),
                {
                    "project": first_scope["project_id"],
                    "owner": first_scope["owner_user_id"],
                },
            )

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('run_events_p202401')")) == "run_events_p202401"
            assert await connection.scalar(text("SELECT count(*) FROM run_events WHERE id=23001")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM run_event_invariants WHERE id=23001")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM run_events WHERE id=23002")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM run_event_invariants WHERE id=23002")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partition_drop_waits_for_inflight_parent_write(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    january = datetime(2024, 1, 10, tzinfo=UTC)
    writer = None
    dropper = None
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            scope = await _seed_event_scope(connection)
            await connection.execute(
                text("SELECT ensure_run_events_month_partition(:month)"),
                {"month": january},
            )
        run_ids = scope["run_ids"]
        assert isinstance(run_ids, dict)

        writer = await engine.connect()
        writer_transaction = await writer.begin()
        await _insert_event(
            writer,
            scope,
            event_id=25_001,
            run_id=run_ids["sequence"],
            seq=1,
            created_at=january,
            category="message",
        )

        dropper = await engine.connect()
        drop_started = asyncio.Event()

        async def drop_old_partition() -> int:
            async with dropper.begin():
                await dropper.execute(text("SET LOCAL statement_timeout = 3000"))
                drop_started.set()
                value = await dropper.scalar(
                    text("SELECT drop_run_event_partitions_before(:cutoff)"),
                    {"cutoff": datetime(2024, 3, 1, tzinfo=UTC)},
                )
                return int(value or 0)

        drop_task = asyncio.create_task(drop_old_partition())
        await drop_started.wait()
        await asyncio.sleep(0.1)
        assert not drop_task.done()

        await writer_transaction.commit()
        assert await asyncio.wait_for(drop_task, timeout=2) == 1
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('run_events_p202401')")) is None
            assert await connection.scalar(text("SELECT count(*) FROM run_event_invariants WHERE id=25001")) == 0
    finally:
        if writer is not None:
            await writer.close()
        if dropper is not None:
            await dropper.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_rejects_misnamed_partition_child(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """CREATE TABLE run_events_p209902 PARTITION OF run_events
                       FOR VALUES FROM ('2099-01-01T00:00:00+00:00')
                                  TO ('2099-02-01T00:00:00+00:00')"""
                )
            )
        async with engine.connect() as connection:
            with pytest.raises(SchemaRecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_rejects_arbitrary_index_on_partition_child(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    current_partition = f"run_events_p{datetime.now(UTC):%Y%m}"
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"""CREATE INDEX unexpected_run_event_partition_index
                        ON {current_partition} (content)"""
                )
            )
        async with engine.connect() as connection:
            with pytest.raises(SchemaRecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_rejects_arbitrary_trigger_on_partition_child(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    current_partition = f"run_events_p{datetime.now(UTC):%Y%m}"
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"""CREATE TRIGGER unexpected_run_event_partition_trigger
                        AFTER DELETE ON {current_partition} FOR EACH ROW
                        EXECUTE FUNCTION cleanup_run_event_invariant()"""
                )
            )
        async with engine.connect() as connection:
            with pytest.raises(SchemaRecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_accepts_an_old_legal_partition_across_natural_rollover(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    current_month = datetime.now(UTC).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    old_month = (current_month - timedelta(days=1)).replace(day=1)
    if current_month.month == 12:
        next_month = current_month.replace(year=current_month.year + 1, month=1)
    else:
        next_month = current_month.replace(month=current_month.month + 1)
    current_partition = f"run_events_p{current_month:%Y%m}"
    next_partition = f"run_events_p{next_month:%Y%m}"
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT ensure_run_events_month_partition(:month)"),
                {"month": old_month},
            )
            await connection.execute(text(f"DROP TABLE {current_partition}, {next_partition}"))
        async with engine.connect() as connection:
            assert await classify_database(connection) == "current"
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "state_mutation",
    (
        "DELETE FROM run_event_partition_state WHERE singleton",
        "UPDATE run_event_partition_state SET retained_from='-infinity' WHERE singleton",
        "UPDATE run_event_partition_state SET retained_from=TIMESTAMPTZ '2024-03-15 12:00:00+00' WHERE singleton",
        "UPDATE run_event_partition_state SET retained_from=date_trunc('month', now() AT TIME ZONE 'UTC')     AT TIME ZONE 'UTC' + INTERVAL '1 month' WHERE singleton",
    ),
)
@pytest.mark.asyncio
async def test_catalog_requires_valid_partition_retention_state(
    postgres_database_url: str,
    state_mutation: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(text(state_mutation))
        async with engine.connect() as connection:
            with pytest.raises(SchemaRecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "trigger_name",
    (
        "trg_run_events_stream_terminal",
        "trg_run_events_identity_immutable",
        "trg_run_events_invariant_cleanup",
    ),
)
@pytest.mark.asyncio
async def test_catalog_rejects_disabled_partition_trigger_clone(
    postgres_database_url: str,
    trigger_name: str,
) -> None:
    engine = create_async_engine(postgres_database_url, poolclass=NullPool)
    current_partition = f"run_events_p{datetime.now(UTC):%Y%m}"
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            scope = await _seed_event_scope(connection)
            run_ids = scope["run_ids"]
            assert isinstance(run_ids, dict)
            await _insert_event(
                connection,
                scope,
                event_id=29_001,
                run_id=run_ids["sequence"],
                seq=1,
                created_at=datetime.now(UTC),
                category="message",
            )
            await connection.execute(text(f"ALTER TABLE {current_partition} DISABLE TRIGGER {trigger_name}"))
            if trigger_name == "trg_run_events_stream_terminal":
                await _insert_event(
                    connection,
                    scope,
                    event_id=29_002,
                    run_id=run_ids["sequence"],
                    seq=1,
                    created_at=datetime.now(UTC),
                    category="message",
                )

        async with engine.connect() as connection:
            if trigger_name == "trg_run_events_stream_terminal":
                assert (
                    await connection.scalar(
                        text(
                            """SELECT count(*) FROM run_events
                           WHERE thread_id=:thread AND seq=1"""
                        ),
                        {"thread": scope["thread_id"]},
                    )
                    == 2
                )
                assert (
                    await connection.scalar(
                        text(
                            """SELECT count(*) FROM run_event_invariants
                           WHERE thread_id=:thread AND seq=1"""
                        ),
                        {"thread": scope["thread_id"]},
                    )
                    == 1
                )
            with pytest.raises(SchemaRecreateRequired):
                await classify_database(connection)
    finally:
        await engine.dispose()
