"""Canonical, read-only PostgreSQL contract for the final M7 application schema."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

import deerflow.persistence.models  # noqa: F401 -- populate final metadata
from deerflow.persistence.base import Base
from deerflow.persistence.final_schema_digest import M7_CANONICAL_SCHEMA_DIGEST

FINAL_APP_TABLES = frozenset(Base.metadata.tables)
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
        "enforce_run_model_snapshot_credential_closure",
        "enforce_scheduled_task_agent_project",
        "enforce_shared_asset_version_state_transition",
        "enforce_stream_terminal_invariant",
        "ensure_system_binding_published_version",
        "prevent_bound_published_version_downgrade",
        "prevent_published_version_child_mutation",
        "prevent_shared_asset_version_payload_update",
        "reject_m7_append_only_mutation",
        "reject_direct_run_model_snapshot_mutation",
        "reject_direct_run_runtime_policy_snapshot_mutation",
        "set_m7_updated_at",
    }
)


@dataclass(frozen=True)
class CatalogInvariant:
    count: int
    digest: str


# The current catalog is generated from ``full_schema.sql``. Values are read
# from PostgreSQL after installing the snapshot in an empty database.
FINAL_M7_CATALOG_SIGNATURE: dict[str, CatalogInvariant] = {
    "relations": CatalogInvariant(
        count=80,
        digest="b30b02aa4285731ee231fec8abe7f6b0fa77325d2d8a017674307061f3efec21",
    ),
    "columns": CatalogInvariant(
        count=975,
        digest="e589bf3423c3b2c39a50de7478aab55b0da0fdae86b80a15bde12317d0e223a2",
    ),
    "sequences": CatalogInvariant(
        count=2,
        digest="fce385d8c1dc9ee6f747d70a8f301fd78f6976767baa90c8fbead6caba2b614f",
    ),
    "constraints": CatalogInvariant(
        count=707,
        digest="6b0184f08355a69b508086d9340a4f9dc93d2146ebb44cbbbe739feea451bc97",
    ),
    "indexes": CatalogInvariant(
        count=276,
        digest="9472b2386cc7a4a296fc9d59169d38e709eb651db622b45d313b04624a560ef5",
    ),
    "functions": CatalogInvariant(
        count=13,
        digest="3986289c90206f94aa821fef7e8a58e253dec6a968d91c4cb43fde6722ea8f62",
    ),
    "triggers": CatalogInvariant(
        count=82,
        digest="e2cba6eab9bb8b3a50bb14cc4fd31133d776c0f0a83ce4d8617d8adb84e3d50d",
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
        SELECT c.relname, t.tgname, p.proname,
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
    return signature == FINAL_M7_CATALOG_SIGNATURE


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
            if name not in REQUIRED_FUNCTIONS or identity_arguments:
                return False
        else:
            return False
    if app_sequences != FINAL_APP_SEQUENCES or alembic_indexes != ALEMBIC_INDEXES:
        return False
    return not langgraph_objects or langgraph_objects == set(LANGGRAPH_ROOT_OBJECTS)


__all__ = [
    "CatalogInvariant",
    "ALEMBIC_INDEXES",
    "FINAL_APP_TABLES",
    "FINAL_APP_SEQUENCES",
    "FINAL_M7_CATALOG_SIGNATURE",
    "M7_CANONICAL_SCHEMA_DIGEST",
    "LANGGRAPH_INDEXES",
    "LANGGRAPH_ROOT_OBJECTS",
    "LANGGRAPH_SEQUENCES",
    "LANGGRAPH_TABLES",
    "REQUIRED_FUNCTIONS",
    "inventory_is_m7_allowed",
    "inventory_user_schema_objects",
    "read_m7_catalog_signature",
    "verify_m7_catalog",
]
