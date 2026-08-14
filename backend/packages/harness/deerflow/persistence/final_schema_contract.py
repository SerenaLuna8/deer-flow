"""Canonical, read-only PostgreSQL contract for the final M7 application schema."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

import deerflow.persistence.models  # noqa: F401 -- populate final metadata
from deerflow.persistence.base import Base
from deerflow.persistence.final_schema_digest import M7_CANONICAL_SCHEMA_DIGEST

FINAL_APP_TABLES = frozenset(Base.metadata.tables)
COMMENTED_ROOT_TABLES = FINAL_APP_TABLES | {"alembic_version"}
LANGGRAPH_TABLES = frozenset(
    {
        "checkpoint_blobs",
        "checkpoint_migrations",
        "checkpoint_writes",
        "checkpoints",
        "store",
        "store_migrations",
    }
)
FINAL_APP_SEQUENCES = frozenset(
    {
        ("memory_history_entries_sequence_seq", "memory_history_entries"),
        ("run_events_id_seq", "run_events"),
    }
)
ALEMBIC_INDEXES = frozenset({("alembic_version_pkc", "alembic_version")})
LANGGRAPH_INDEXES = frozenset(
    {
        ("checkpoint_blobs_pkey", "checkpoint_blobs"),
        ("checkpoint_blobs_thread_id_idx", "checkpoint_blobs"),
        ("checkpoint_migrations_pkey", "checkpoint_migrations"),
        ("checkpoint_writes_pkey", "checkpoint_writes"),
        ("checkpoint_writes_thread_id_idx", "checkpoint_writes"),
        ("checkpoints_pkey", "checkpoints"),
        ("checkpoints_thread_id_idx", "checkpoints"),
        ("idx_store_expires_at", "store"),
        ("store_pkey", "store"),
        ("store_prefix_idx", "store"),
        ("store_migrations_pkey", "store_migrations"),
    }
)
LANGGRAPH_SEQUENCES: frozenset[tuple[str, str]] = frozenset()
LANGGRAPH_ROOT_OBJECTS = frozenset(
    {f"relation:r:{table_name}" for table_name in LANGGRAPH_TABLES} | {f"index:{index_name}:{owner}" for index_name, owner in LANGGRAPH_INDEXES} | {f"sequence:{sequence_name}:{owner}" for sequence_name, owner in LANGGRAPH_SEQUENCES}
)
REQUIRED_FUNCTIONS = frozenset(
    {
        "bump_asset_catalog_generation",
        "cleanup_run_event_invariant",
        "drop_run_event_partitions_before",
        "enforce_run_model_snapshot_credential_closure",
        "enforce_run_event_identity_immutable",
        "enforce_scheduled_task_agent_project",
        "enforce_shared_asset_version_state_transition",
        "enforce_stream_terminal_invariant",
        "ensure_run_events_month_partition",
        "ensure_system_binding_published_version",
        "enforce_system_skill_version_revocation",
        "prevent_bound_published_version_downgrade",
        "prevent_memory_document_sections_mutation",
        "prevent_published_version_child_mutation",
        "prevent_run_memory_snapshot_sections_mutation",
        "prevent_shared_asset_version_payload_update",
        "reject_m7_append_only_mutation",
        "reject_direct_run_model_snapshot_mutation",
        "reject_direct_run_runtime_policy_snapshot_mutation",
        "set_m7_updated_at",
        "set_threads_meta_updated_at",
    }
)
_PARAMETERIZED_REQUIRED_FUNCTIONS = frozenset(
    {
        (
            "drop_run_event_partitions_before",
            "cutoff_at timestamp with time zone",
        ),
        (
            "ensure_run_events_month_partition",
            "target_at timestamp with time zone",
        ),
    }
)
REQUIRED_FUNCTION_IDENTITIES = frozenset({(name, "") for name in REQUIRED_FUNCTIONS if name not in {identity[0] for identity in _PARAMETERIZED_REQUIRED_FUNCTIONS}} | _PARAMETERIZED_REQUIRED_FUNCTIONS)


@dataclass(frozen=True)
class CatalogInvariant:
    count: int
    digest: str


# The current catalog is generated from ``full_schema.sql``. Values are read
# from PostgreSQL after installing the snapshot in an empty database.
FINAL_M7_CATALOG_SIGNATURE: dict[str, CatalogInvariant] = {
    "relations": CatalogInvariant(
        count=86,
        digest="6391510d9969596d293a7b886436203cb97a53e09e28cf6f6e3d595d268bdb06",
    ),
    "columns": CatalogInvariant(
        count=1079,
        digest="bb58a210af5095699231b7d2849ae8a42db97462eedfe789785eb2b5528cdaf3",
    ),
    "table_comments": CatalogInvariant(
        count=87,
        digest="6c2538480170283ca68966578a286829f1156f68b8322c66f3099a68581350d6",
    ),
    "column_comments": CatalogInvariant(
        count=1080,
        digest="e9a0e4a62abe689d3ad95306c8be9b7b03858550e249f42b79822752052665d7",
    ),
    "sequences": CatalogInvariant(
        count=2,
        digest="fce385d8c1dc9ee6f747d70a8f301fd78f6976767baa90c8fbead6caba2b614f",
    ),
    "constraints": CatalogInvariant(
        count=804,
        digest="8b5e997d78985d6b5afa3f56022643615619174aeffdc908ff4d6da807dbcf59",
    ),
    "indexes": CatalogInvariant(
        count=306,
        digest="106be3886fce817e41a119bd78ea35881089a2fabe8f76542c34da1d708f0697",
    ),
    "functions": CatalogInvariant(
        count=21,
        digest="8ceff26ea07e6587f4c96e8619e2f25995d5f624fefe884f74fc040c847277d5",
    ),
    "triggers": CatalogInvariant(
        count=88,
        digest="25f62ad0015251d0182bf1ca44476294a28ce8c17230219a9bd0e060129e1e99",
    ),
}

# LangGraph owns these tables and creates them after the application snapshot,
# so their comment signature is verified separately from the static catalog.
LANGGRAPH_COMMENT_SIGNATURE: dict[str, CatalogInvariant] = {
    "table_comments": CatalogInvariant(
        count=6,
        digest="0b4ff5f97c99f81e24deb8153b448cfffff31e714ed5653206c5f2cef0e526c3",
    ),
    "column_comments": CatalogInvariant(
        count=31,
        digest="703ea631289a62bf4ab94e0629eee646098d03528df0ed2a2e5e3e890e65a2c9",
    ),
}


def _catalog_signature_digest(signature: dict[str, CatalogInvariant]) -> str:
    payload = {category: {"count": invariant.count, "digest": invariant.digest} for category, invariant in sorted(signature.items())}
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


if M7_CANONICAL_SCHEMA_DIGEST != _catalog_signature_digest(FINAL_M7_CATALOG_SIGNATURE):  # pragma: no cover - import-time release invariant
    raise RuntimeError("M7 canonical schema digest does not match its catalog signature")


_VARCHAR_TEXT_ARRAY = re.compile(r"ARRAY\[(?P<body>(?:'(?:''|[^'])*'::character varying(?:::text)?(?:,\s*)?)+)\](?:::text\[\])?")
_CATALOG_QUERIES = {
    "relations": """
        SELECT c.relname, c.relkind::text, c.relpersistence::text,
               c.relrowsecurity, c.relforcerowsecurity,
               COALESCE(pg_get_partkeydef(c.oid), '')
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema()
          AND c.relname = ANY(CAST(:app_tables AS text[]))
        ORDER BY c.relname
    """,
    "columns": """
        SELECT c.relname, a.attnum, a.attname,
               pg_catalog.format_type(a.atttypid, a.atttypmod),
               a.attnotnull, a.attidentity::text, a.attgenerated::text,
               COALESCE(coll.collname, ''),
               COALESCE(regexp_replace(pg_get_expr(ad.adbin, ad.adrelid, true), '\\s+', ' ', 'g'), '')
        FROM pg_attribute a
        JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum
        LEFT JOIN pg_collation coll ON coll.oid=a.attcollation
        WHERE n.nspname=current_schema()
          AND c.relname = ANY(CAST(:app_tables AS text[]))
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
    """,
    "table_comments": """
        SELECT c.relname,
               COALESCE(obj_description(c.oid, 'pg_class'), '')
        FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema()
          AND c.relname = ANY(CAST(:comment_tables AS text[]))
        ORDER BY c.relname
    """,
    "column_comments": """
        SELECT c.relname, a.attnum, a.attname,
               COALESCE(col_description(c.oid, a.attnum), '')
        FROM pg_attribute a
        JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema()
          AND c.relname = ANY(CAST(:comment_tables AS text[]))
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
    """,
    "sequences": """
        SELECT c.relname, pg_catalog.format_type(s.seqtypid, NULL),
               s.seqstart, s.seqincrement, s.seqmax, s.seqmin,
               s.seqcache, s.seqcycle
        FROM pg_sequence s
        JOIN pg_class c ON c.oid=s.seqrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema()
          AND c.relname = ANY(CAST(:app_sequences AS text[]))
        ORDER BY c.relname
    """,
    "constraints": """
        SELECT c.relname, con.conname, con.contype::text,
               con.condeferrable, con.condeferred, con.convalidated,
               regexp_replace(pg_get_constraintdef(con.oid, true), '\\s+', ' ', 'g')
        FROM pg_constraint con
        JOIN pg_class c ON c.oid=con.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema()
          AND c.relname = ANY(CAST(:app_tables AS text[]))
        ORDER BY c.relname, con.conname
    """,
    "indexes": """
        SELECT c.relname, i.relname, x.indisunique, x.indisprimary,
               x.indisvalid, x.indisready,
               regexp_replace(pg_get_indexdef(i.oid, 0, true), '\\s+', ' ', 'g')
        FROM pg_index x
        JOIN pg_class c ON c.oid=x.indrelid
        JOIN pg_class i ON i.oid=x.indexrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema()
          AND c.relname = ANY(CAST(:app_tables AS text[]))
        ORDER BY c.relname, i.relname
    """,
    "functions": """
        SELECT p.proname, pg_get_function_identity_arguments(p.oid),
               p.prokind::text, p.provolatile::text, p.proisstrict,
               p.prosecdef, p.proleakproof, p.proparallel::text,
               regexp_replace(pg_get_functiondef(p.oid), '\\s+', ' ', 'g')
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname=current_schema()
          AND p.proname = ANY(CAST(:required_functions AS text[]))
        ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)
    """,
    "triggers": """
        SELECT c.relname, t.tgname, t.tgenabled, p.proname,
               regexp_replace(pg_get_triggerdef(t.oid, true), '\\s+', ' ', 'g')
        FROM pg_trigger t
        JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_proc p ON p.oid=t.tgfoid
        WHERE n.nspname=current_schema()
          AND c.relname = ANY(CAST(:app_tables AS text[]))
          AND NOT t.tgisinternal
        ORDER BY c.relname, t.tgname
    """,
}


def _rows_digest(rows: tuple[tuple[object, ...], ...]) -> str:
    normalized = tuple(tuple(_normalize_catalog_value(value) for value in row) for row in rows)
    payload = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_catalog_value(value: object) -> object:
    if not isinstance(value, str):
        return value

    # PostgreSQL may render text-array ANY expressions with a cast on each
    # element even though the baseline DDL uses one cast on the array. Normalize
    # that presentation-only difference for stable catalog verification.
    def normalize_varchar_text_array(match: re.Match[str]) -> str:
        body = match.group("body").replace(
            "::character varying::text",
            "::character varying",
        )
        return f"ARRAY[{body}]"

    return _VARCHAR_TEXT_ARRAY.sub(normalize_varchar_text_array, value)


async def read_m7_catalog_signature(connection: AsyncConnection) -> dict[str, CatalogInvariant]:
    """Read stable final-schema invariants directly from ``pg_catalog``."""

    parameters = {
        "app_tables": sorted(FINAL_APP_TABLES),
        "comment_tables": sorted(COMMENTED_ROOT_TABLES),
        "app_sequences": sorted(name for name, _owner in FINAL_APP_SEQUENCES),
        "required_functions": sorted(REQUIRED_FUNCTIONS),
    }
    signature: dict[str, CatalogInvariant] = {}
    for category, query in _CATALOG_QUERIES.items():
        result = await connection.execute(text(query), parameters)
        rows = tuple(tuple(row) for row in result)
        signature[category] = CatalogInvariant(len(rows), _rows_digest(rows))
    return signature


async def verify_m7_catalog(connection: AsyncConnection) -> bool:
    """Return whether all current catalog invariants match exactly."""

    signature = await read_m7_catalog_signature(connection)
    return signature == FINAL_M7_CATALOG_SIGNATURE and await _run_event_partition_catalog_is_valid(connection) and await _langgraph_comments_are_valid(connection)


async def _langgraph_comments_are_valid(connection: AsyncConnection) -> bool:
    """Require comments whenever the optional third-party schema is present."""

    table_rows = tuple(
        await connection.execute(
            text(
                """SELECT relation.relname,
                          obj_description(relation.oid, 'pg_class')
                     FROM pg_class relation
                     JOIN pg_namespace namespace
                       ON namespace.oid=relation.relnamespace
                    WHERE namespace.nspname=current_schema()
                      AND relation.relkind IN ('r', 'p')
                      AND relation.relname=ANY(CAST(:tables AS text[]))
                    ORDER BY relation.relname"""
            ),
            {"tables": sorted(LANGGRAPH_TABLES)},
        )
    )
    if not table_rows:
        return True
    table_signature = CatalogInvariant(
        count=len(table_rows),
        digest=_rows_digest(tuple(tuple(row) for row in table_rows)),
    )
    if table_signature != LANGGRAPH_COMMENT_SIGNATURE["table_comments"]:
        return False

    column_rows = tuple(
        await connection.execute(
            text(
                """SELECT relation.relname, attribute.attname,
                          col_description(relation.oid, attribute.attnum)
                     FROM pg_class relation
                     JOIN pg_namespace namespace
                       ON namespace.oid=relation.relnamespace
                     JOIN pg_attribute attribute
                       ON attribute.attrelid=relation.oid
                    WHERE namespace.nspname=current_schema()
                      AND relation.relname=ANY(CAST(:tables AS text[]))
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                    ORDER BY relation.relname, attribute.attnum"""
            ),
            {"tables": sorted(LANGGRAPH_TABLES)},
        )
    )
    column_signature = CatalogInvariant(
        count=len(column_rows),
        digest=_rows_digest(tuple(tuple(row) for row in column_rows)),
    )
    return column_signature == LANGGRAPH_COMMENT_SIGNATURE["column_comments"]


_RUN_EVENT_PARTITION_NAME = re.compile(r"run_events_p(?P<month>[0-9]{6})\Z")
_RUN_EVENT_PARTITION_BOUND = re.compile(
    r"FOR VALUES FROM \('(?P<start>[^']+)'(?:\:\:[^)]+)?\) TO \('(?P<end>[^']+)'(?:\:\:[^)]+)?\)\Z",
)
_RUN_EVENT_REQUIRED_TRIGGERS = frozenset(
    {
        "trg_run_events_identity_immutable",
        "trg_run_events_invariant_cleanup",
        "trg_run_events_stream_terminal",
    }
)


def _next_utc_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


async def _run_event_partition_catalog_is_valid(connection: AsyncConnection) -> bool:
    """Validate only actual direct monthly children of ``run_events``."""

    state_rows = tuple(
        await connection.execute(
            text(
                """SELECT singleton,
                          retained_from IS NULL
                          OR (
                              isfinite(retained_from)
                              AND retained_from = (
                                  date_trunc('month', retained_from AT TIME ZONE 'UTC')
                                  AT TIME ZONE 'UTC'
                              )
                              AND retained_from <= (
                                  date_trunc('month', now() AT TIME ZONE 'UTC')
                                  AT TIME ZONE 'UTC'
                              )
                          ) AS is_valid
                     FROM run_event_partition_state"""
            )
        )
    )
    if len(state_rows) != 1 or state_rows[0][0] is not True or state_rows[0][1] is not True:
        return False

    result = await connection.execute(
        text(
            """SELECT child.relname,
                      pg_get_expr(child.relpartbound, child.oid, true)
                 FROM pg_inherits inheritance
                 JOIN pg_class parent ON parent.oid = inheritance.inhparent
                 JOIN pg_class child ON child.oid = inheritance.inhrelid
                 JOIN pg_namespace namespace ON namespace.oid = child.relnamespace
                WHERE parent.oid = 'run_events'::regclass
                  AND namespace.nspname = current_schema()
                ORDER BY child.relname"""
        )
    )
    rows = tuple(result)
    if not rows:
        return False
    child_names: set[str] = set()
    for name_value, bound_value in rows:
        name = str(name_value)
        child_names.add(name)
        bound = str(bound_value)
        name_match = _RUN_EVENT_PARTITION_NAME.fullmatch(name)
        bound_match = _RUN_EVENT_PARTITION_BOUND.fullmatch(bound)
        if name_match is None or bound_match is None:
            return False
        try:
            start = datetime.fromisoformat(bound_match.group("start")).astimezone(UTC)
            end = datetime.fromisoformat(bound_match.group("end")).astimezone(UTC)
        except ValueError:
            return False
        if start != datetime.strptime(name_match.group("month"), "%Y%m").replace(tzinfo=UTC) or end != _next_utc_month(start):
            return False

    parent_table_comment = await connection.scalar(text("SELECT obj_description('run_events'::regclass, 'pg_class')"))
    parent_column_rows = tuple(
        await connection.execute(
            text(
                """SELECT attribute.attname,
                          col_description(attribute.attrelid, attribute.attnum)
                     FROM pg_attribute attribute
                    WHERE attribute.attrelid='run_events'::regclass
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                    ORDER BY attribute.attnum"""
            )
        )
    )
    if not isinstance(parent_table_comment, str) or not parent_table_comment.strip():
        return False
    if not parent_column_rows or any(not isinstance(comment, str) or not comment.strip() for _column_name, comment in parent_column_rows):
        return False
    child_comment_rows = tuple(
        await connection.execute(
            text(
                """SELECT child.relname,
                          obj_description(child.oid, 'pg_class'),
                          attribute.attname,
                          col_description(child.oid, attribute.attnum)
                     FROM pg_inherits inheritance
                     JOIN pg_class parent ON parent.oid=inheritance.inhparent
                     JOIN pg_class child ON child.oid=inheritance.inhrelid
                     JOIN pg_namespace namespace ON namespace.oid=child.relnamespace
                     JOIN pg_attribute attribute ON attribute.attrelid=child.oid
                    WHERE parent.oid='run_events'::regclass
                      AND namespace.nspname=current_schema()
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                    ORDER BY child.relname, attribute.attnum"""
            )
        )
    )
    child_comments: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for child_value, table_comment, column_value, column_comment in child_comment_rows:
        if not isinstance(table_comment, str) or not isinstance(column_comment, str):
            return False
        child_name = str(child_value)
        existing_table_comment, columns = child_comments.setdefault(
            child_name,
            (table_comment, []),
        )
        if existing_table_comment != table_comment:
            return False
        columns.append((str(column_value), column_comment))
    expected_columns = tuple((str(column_name), str(comment)) for column_name, comment in parent_column_rows)
    if set(child_comments) != child_names or any(table_comment != parent_table_comment or tuple(columns) != expected_columns for table_comment, columns in child_comments.values()):
        return False

    trigger_rows = tuple(
        await connection.execute(
            text(
                """SELECT child.relname,
                          child_trigger.tgname,
                          child_trigger.tgenabled,
                          parent_trigger.tgname
                     FROM pg_inherits inheritance
                     JOIN pg_class parent ON parent.oid=inheritance.inhparent
                     JOIN pg_class child ON child.oid=inheritance.inhrelid
                     JOIN pg_namespace namespace ON namespace.oid=child.relnamespace
                     JOIN pg_trigger child_trigger
                       ON child_trigger.tgrelid=child.oid
                      AND NOT child_trigger.tgisinternal
                     LEFT JOIN pg_trigger parent_trigger
                       ON parent_trigger.oid=child_trigger.tgparentid
                    WHERE parent.oid='run_events'::regclass
                      AND namespace.nspname=current_schema()
                    ORDER BY child.relname, child_trigger.tgname"""
            )
        )
    )
    observed_triggers: set[tuple[str, str]] = set()
    for child_value, trigger_value, enabled_value, parent_trigger_value in trigger_rows:
        child_name = str(child_value)
        trigger_name = str(trigger_value)
        enabled = enabled_value.decode("ascii") if isinstance(enabled_value, bytes) else str(enabled_value)
        if enabled != "O" or parent_trigger_value is None or str(parent_trigger_value) != trigger_name:
            return False
        observed_triggers.add((child_name, trigger_name))
    expected_triggers = {(child_name, trigger_name) for child_name in child_names for trigger_name in _RUN_EVENT_REQUIRED_TRIGGERS}
    return observed_triggers == expected_triggers


_USER_SCHEMA_INVENTORY_SQL = """
            SELECT 'relation:' || c.relkind::text || ':' || c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema()
              AND c.relkind IN ('r','p','v','m','f','c')
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_inherits inheritance
                  JOIN pg_class partition_parent ON partition_parent.oid=inheritance.inhparent
                  JOIN pg_namespace partition_namespace ON partition_namespace.oid=partition_parent.relnamespace
                  WHERE inheritance.inhrelid=c.oid
                    AND partition_parent.relname='run_events'
                    AND partition_namespace.nspname=current_schema()
              )
            UNION ALL
            SELECT 'sequence:' || seq.relname || ':' || COALESCE(owner.relname, '')
            FROM pg_class seq
            JOIN pg_namespace n ON n.oid=seq.relnamespace
            LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass
                AND d.objid=seq.oid AND d.refclassid='pg_class'::regclass
                AND d.deptype IN ('a','i')
            LEFT JOIN pg_class owner ON owner.oid=d.refobjid
            WHERE n.nspname=current_schema() AND seq.relkind='S'
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend ext
                  WHERE ext.classid='pg_class'::regclass AND ext.objid=seq.oid AND ext.deptype='e'
              )
            UNION ALL
            SELECT 'index:' || idx.relname || ':' || owner.relname
            FROM pg_class idx
            JOIN pg_namespace n ON n.oid=idx.relnamespace
            JOIN pg_index x ON x.indexrelid=idx.oid
            JOIN pg_class owner ON owner.oid=x.indrelid
            WHERE n.nspname=current_schema() AND idx.relkind IN ('i','I')
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend ext
                  WHERE ext.classid='pg_class'::regclass AND ext.objid=idx.oid AND ext.deptype='e'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_inherits index_inheritance
                  JOIN pg_class parent_index ON parent_index.oid=index_inheritance.inhparent
                  JOIN pg_index parent_index_definition ON parent_index_definition.indexrelid=parent_index.oid
                  JOIN pg_class partition_parent ON partition_parent.oid=parent_index_definition.indrelid
                  JOIN pg_namespace partition_namespace ON partition_namespace.oid=partition_parent.relnamespace
                  WHERE index_inheritance.inhrelid=idx.oid
                    AND partition_parent.relname='run_events'
                    AND partition_namespace.nspname=current_schema()
              )
            UNION ALL
            SELECT 'routine:' || p.proname || ':' || pg_get_function_identity_arguments(p.oid)
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'type:' || t.typtype::text || ':' || t.typname
            FROM pg_type t
            JOIN pg_namespace n ON n.oid=t.typnamespace
            LEFT JOIN pg_class c ON c.oid=t.typrelid
            WHERE n.nspname=current_schema()
              AND t.typelem=0
              AND t.typtype IN ('c','d','e','r','m')
              AND (t.typrelid=0 OR c.relkind='c')
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend dep
                  WHERE dep.classid='pg_type'::regclass AND dep.objid=t.oid AND dep.deptype='e'
              )
            UNION ALL
            SELECT 'collation:' || coll.collname
            FROM pg_collation coll
            JOIN pg_namespace n ON n.oid=coll.collnamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_collation'::regclass AND d.objid=coll.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'conversion:' || conv.conname
            FROM pg_conversion conv
            JOIN pg_namespace n ON n.oid=conv.connamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_conversion'::regclass AND d.objid=conv.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'operator:' || op.oprname || ':' || op.oprleft::text || ':' || op.oprright::text
            FROM pg_operator op
            JOIN pg_namespace n ON n.oid=op.oprnamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_operator'::regclass AND d.objid=op.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'opclass:' || opc.opcname
            FROM pg_opclass opc JOIN pg_namespace n ON n.oid=opc.opcnamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_opclass'::regclass AND d.objid=opc.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'opfamily:' || opf.opfname
            FROM pg_opfamily opf JOIN pg_namespace n ON n.oid=opf.opfnamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_opfamily'::regclass AND d.objid=opf.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'tsconfig:' || cfg.cfgname
            FROM pg_ts_config cfg JOIN pg_namespace n ON n.oid=cfg.cfgnamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_ts_config'::regclass AND d.objid=cfg.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'tsdict:' || dict.dictname
            FROM pg_ts_dict dict JOIN pg_namespace n ON n.oid=dict.dictnamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_ts_dict'::regclass AND d.objid=dict.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'tsparser:' || prs.prsname
            FROM pg_ts_parser prs JOIN pg_namespace n ON n.oid=prs.prsnamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_ts_parser'::regclass AND d.objid=prs.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'tstemplate:' || tmpl.tmplname
            FROM pg_ts_template tmpl JOIN pg_namespace n ON n.oid=tmpl.tmplnamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_ts_template'::regclass AND d.objid=tmpl.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'statistics:' || stat.stxname
            FROM pg_statistic_ext stat JOIN pg_namespace n ON n.oid=stat.stxnamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_statistic_ext'::regclass AND d.objid=stat.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'rule:' || rule.rulename || ':' || owner.relname
            FROM pg_rewrite rule JOIN pg_class owner ON owner.oid=rule.ev_class
            JOIN pg_namespace n ON n.oid=owner.relnamespace
            WHERE n.nspname=current_schema() AND rule.rulename <> '_RETURN'
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_rewrite'::regclass AND d.objid=rule.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'policy:' || policy.polname || ':' || owner.relname
            FROM pg_policy policy JOIN pg_class owner ON owner.oid=policy.polrelid
            JOIN pg_namespace n ON n.oid=owner.relnamespace
            WHERE n.nspname=current_schema()
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_policy'::regclass AND d.objid=policy.oid AND d.deptype='e'
              )
            UNION ALL
            SELECT 'trigger:' || trigger.tgname || ':' || owner.relname
            FROM pg_trigger trigger JOIN pg_class owner ON owner.oid=trigger.tgrelid
            JOIN pg_namespace n ON n.oid=owner.relnamespace
            WHERE n.nspname=current_schema() AND NOT trigger.tgisinternal
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid='pg_trigger'::regclass AND d.objid=trigger.oid AND d.deptype='e'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_trigger parent_trigger
                  JOIN pg_class partition_parent ON partition_parent.oid=parent_trigger.tgrelid
                  JOIN pg_namespace partition_namespace ON partition_namespace.oid=partition_parent.relnamespace
                  WHERE parent_trigger.oid=trigger.tgparentid
                    AND partition_parent.relname='run_events'
                    AND partition_namespace.nspname=current_schema()
              )
            """


async def inventory_user_schema_objects(connection: AsyncConnection) -> frozenset[str]:
    """List non-extension-owned root objects in the active user schema."""

    result = await connection.execute(text(_USER_SCHEMA_INVENTORY_SQL))
    return frozenset(str(value) for value in result.scalars())


def inventory_is_m7_allowed(objects: frozenset[str]) -> bool:
    """Validate exact app-only or complete app-plus-LangGraph root objects."""

    allowed_relations = FINAL_APP_TABLES | LANGGRAPH_TABLES | {"alembic_version"}
    app_sequences: set[tuple[str, str]] = set()
    alembic_indexes: set[tuple[str, str]] = set()
    langgraph_objects: set[str] = set()
    for descriptor in objects:
        kind, _, remainder = descriptor.partition(":")
        if kind == "relation":
            relkind, _, name = remainder.partition(":")
            if relkind not in {"r", "p"} or name not in allowed_relations:
                return False
            if name in LANGGRAPH_TABLES:
                langgraph_objects.add(descriptor)
        elif kind == "sequence":
            name, _, owner = remainder.partition(":")
            identity = (name, owner)
            if owner in FINAL_APP_TABLES:
                app_sequences.add(identity)
                if identity not in FINAL_APP_SEQUENCES:
                    return False
            elif owner in LANGGRAPH_TABLES:
                langgraph_objects.add(descriptor)
            else:
                return False
        elif kind == "index":
            name, _, owner = remainder.partition(":")
            identity = (name, owner)
            if owner in FINAL_APP_TABLES:
                # Full definitions for every app index are locked by the canonical digest.
                continue
            if owner in LANGGRAPH_TABLES:
                langgraph_objects.add(descriptor)
            elif owner == "alembic_version":
                alembic_indexes.add(identity)
                if identity not in ALEMBIC_INDEXES:
                    return False
            else:
                return False
        elif kind == "trigger":
            _name, _, owner = remainder.partition(":")
            if owner not in FINAL_APP_TABLES:
                return False
        elif kind == "routine":
            name, _, identity_arguments = remainder.partition(":")
            if (name, identity_arguments) not in REQUIRED_FUNCTION_IDENTITIES:
                return False
        else:
            return False
    if app_sequences != FINAL_APP_SEQUENCES or alembic_indexes != ALEMBIC_INDEXES:
        return False
    return not langgraph_objects or langgraph_objects == set(LANGGRAPH_ROOT_OBJECTS)


__all__ = [
    "CatalogInvariant",
    "ALEMBIC_INDEXES",
    "COMMENTED_ROOT_TABLES",
    "FINAL_APP_TABLES",
    "FINAL_APP_SEQUENCES",
    "FINAL_M7_CATALOG_SIGNATURE",
    "M7_CANONICAL_SCHEMA_DIGEST",
    "LANGGRAPH_INDEXES",
    "LANGGRAPH_COMMENT_SIGNATURE",
    "LANGGRAPH_ROOT_OBJECTS",
    "LANGGRAPH_SEQUENCES",
    "LANGGRAPH_TABLES",
    "REQUIRED_FUNCTIONS",
    "REQUIRED_FUNCTION_IDENTITIES",
    "inventory_is_m7_allowed",
    "inventory_user_schema_objects",
    "read_m7_catalog_signature",
    "verify_m7_catalog",
]
