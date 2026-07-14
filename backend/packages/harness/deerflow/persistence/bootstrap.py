"""Hybrid schema bootstrap for DeerFlow's application tables.

Replaces the unconditional ``Base.metadata.create_all`` at Gateway startup.
Combines two ideas:

1. ``create_all`` stays the empty-DB fast path -- it renders PostgreSQL
   ``Base.metadata`` faithfully without anyone having to hand-keep a mirror
   baseline in sync with the models.
2. **Alembic owns every change from baseline onward.** Any new ORM column /
   table / index must ship as a revision under ``migrations/versions/``.

Three-branch decision (see ``_decide_state``)
---------------------------------------------

| DB state                              | Action                                  |
|---------------------------------------|-----------------------------------------|
| empty (no DeerFlow tables)            | ``create_all`` + ``stamp head`` + empty-source probes + cutover marker |
| legacy (DeerFlow tables, no alembic)  | baseline-era backfill + ``stamp 0001`` + ``upgrade 0007`` + require explicit M4 migration |
| versioned at M4 final/head            | no-op                                   |
| versioned before M4 final             | require explicit M4 migration before any upgrade |

The legacy branch handles pre-alembic databases that already have at least one
DeerFlow-owned table. A frozen 0001-era catalog runs first because stamping at
``0001_baseline`` makes alembic skip the baseline's own ``create_table`` DDL on
the subsequent upgrade -- so any table added to the baseline after the user's
DB was first provisioned (e.g. the
``channel_*`` tables from PR #1930 for users upgrading across multiple
releases) would otherwise never be created, and the first request hitting that
table would 500 with ``no such table``. The backfill is **restricted to
``_BASELINE_TABLE_NAMES``** and baseline-era columns/constraints so it does not
introduce final ORM dependencies before their owning revisions. It also does
not create tables that future revisions introduce -- those revisions' own
``op.create_table`` would then
fail with ``relation already exists``. A guard test pins the restriction
set against ``0001_baseline.upgrade()``'s actual output.

Column-level shape through revision 0007 (the pre-#3658 vs post-#3658 vs
manual-ALTER cases for ``token_usage_by_model``) is answered by each
``versions/*.py`` revision via
the idempotent helpers in ``migrations/_helpers.py`` (``safe_add_column``
no-ops when the column is already present and ``logger.warning``s on
shape drift). The M4 boundary is crossed only by ``make migrate-private-work``;
ordinary startup never invokes 0008/0009 for an existing database.

Concurrency safety
------------------

PostgreSQL ``pg_advisory_lock`` runs
  the whole reflect-and-act sequence under an exclusive lock that survives
  cross-process. Concurrent Gateway instances queue cleanly and the second
  one observes head as a no-op.
Column revisions additionally use idempotent helpers so repeated
post-baseline changes, manual ALTERs, or retries do not duplicate work.

``alembic upgrade head`` on a DB already at head is a no-op by alembic's own
semantics, so the second-N-th actor simply observes head and exits.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from deerflow.config.runtime_paths import runtime_home

logger = logging.getLogger(__name__)


# Where the alembic environment lives, relative to this file.
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Cached migration head, computed once per process from the disk script tree.
_HEAD_REVISION: str | None = None

# Baseline (stamp target for legacy DBs). Pinned here so the bootstrap layer
# fails loudly if the baseline revision is ever renamed without updating the
# stamp call. ``tests/test_persistence_bootstrap.py`` asserts this string is a
# real revision id in the script tree.
_BASELINE_REVISION = "0001_baseline"

# Stable advisory-lock key for Postgres. Two random 32-bit halves picked once
# so we never collide with any other application's advisory locks. Do not
# change without coordinating a one-time migration (a key change effectively
# releases the prior lock).
_PG_LOCK_KEY = 0x0DEE_12F1_0BEE_3682

# Failed empty-schema cleanup must finish before the advisory lock is released,
# but destructive DDL must not wait forever on another session's table lock.
# Connection acquisition remains bounded by the engine's configured pool_timeout.
_EMPTY_BOOTSTRAP_LOCK_TIMEOUT_MS = 5_000
_EMPTY_BOOTSTRAP_STATEMENT_TIMEOUT_MS = 10_000

_PRIVATE_WORK_SAFE_REVISIONS = frozenset(
    {
        "0009_project_private_work_finalize",
        "0010_private_file_source",
    }
)
_PRIVATE_WORK_PRE_EXPAND_REVISION = "0007_project_shared_assets"
_LEGACY_PRIVATE_WORK_DB_TABLES: tuple[str, ...] = (
    "threads_meta",
    "runs",
    "run_events",
    "feedback",
    "channel_connections",
    "channel_oauth_states",
    "channel_conversations",
)
_LANGGRAPH_CHECKPOINT_TABLES: tuple[str, ...] = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)


# Tables created by ``0001_baseline.upgrade()``. The legacy branch restricts
# its ``create_all`` backfill to this set so it does NOT pre-empt later
# ``op.create_table`` revisions for models added after baseline -- those
# revisions would otherwise fail with ``relation already exists`` if
# ``create_all`` had created their table first. (Column revisions are
# already safe via the idempotent helpers in ``migrations/_helpers.py``;
# there is no analogous ``safe_create_table`` yet, so we keep table-level
# safety at this layer instead of pushing it onto every future revision.)
#
# ``test_baseline_table_names_constant_matches_0001`` pins this set against
# what 0001 actually creates -- editing 0001 without updating this constant
# (or vice versa) fires that test.
_BASELINE_TABLE_NAMES: frozenset[str] = frozenset(
    {
        "channel_connections",
        "channel_conversations",
        "channel_credentials",
        "channel_oauth_states",
        "feedback",
        "run_events",
        "runs",
        "threads_meta",
        "users",
    }
)

_BASELINE_COLUMNS: dict[str, tuple[str, ...]] = {
    "channel_connections": (
        "id",
        "owner_user_id",
        "provider",
        "status",
        "external_account_id",
        "external_account_name",
        "workspace_id",
        "workspace_name",
        "bot_user_id",
        "scopes_json",
        "capabilities_json",
        "metadata_json",
        "created_at",
        "updated_at",
        "last_seen_at",
        "last_error_at",
    ),
    "channel_oauth_states": (
        "state_hash",
        "owner_user_id",
        "provider",
        "code_verifier_encrypted",
        "nonce_hash",
        "redirect_after",
        "requested_scopes_json",
        "metadata_json",
        "expires_at",
        "consumed_at",
        "created_at",
    ),
    "feedback": (
        "feedback_id",
        "run_id",
        "thread_id",
        "user_id",
        "message_id",
        "rating",
        "comment",
        "created_at",
    ),
    "run_events": (
        "id",
        "thread_id",
        "run_id",
        "user_id",
        "event_type",
        "category",
        "content",
        "event_metadata",
        "seq",
        "created_at",
    ),
    "runs": (
        "run_id",
        "thread_id",
        "assistant_id",
        "user_id",
        "status",
        "model_name",
        "multitask_strategy",
        "metadata_json",
        "kwargs_json",
        "error",
        "message_count",
        "first_human_message",
        "last_ai_message",
        "total_input_tokens",
        "total_output_tokens",
        "total_tokens",
        "llm_call_count",
        "lead_agent_tokens",
        "subagent_tokens",
        "middleware_tokens",
        "token_usage_by_model",
        "follow_up_to_run_id",
        "created_at",
        "updated_at",
    ),
    "threads_meta": (
        "thread_id",
        "assistant_id",
        "user_id",
        "display_name",
        "status",
        "metadata_json",
        "created_at",
        "updated_at",
    ),
    "users": (
        "id",
        "email",
        "password_hash",
        "system_role",
        "created_at",
        "oauth_provider",
        "oauth_id",
        "needs_setup",
        "token_version",
    ),
    "channel_conversations": (
        "id",
        "connection_id",
        "owner_user_id",
        "provider",
        "external_conversation_id",
        "external_topic_id",
        "thread_id",
        "created_at",
        "updated_at",
    ),
    "channel_credentials": (
        "connection_id",
        "encrypted_access_token",
        "encrypted_refresh_token",
        "token_type",
        "expires_at",
        "refresh_expires_at",
        "encrypted_extra_json",
        "version",
        "updated_at",
    ),
}

_BASELINE_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "channel_connections": ("id",),
    "channel_oauth_states": ("state_hash",),
    "feedback": ("feedback_id",),
    "run_events": ("id",),
    "runs": ("run_id",),
    "threads_meta": ("thread_id",),
    "users": ("id",),
    "channel_conversations": ("id",),
    "channel_credentials": ("connection_id",),
}

_BASELINE_INDEXES: tuple[tuple[str, str, tuple[str, ...], bool, str | None], ...] = (
    ("channel_connections", "idx_channel_connections_event_lookup", ("provider", "workspace_id", "bot_user_id"), False, None),
    ("channel_connections", "ix_channel_connections_owner_user_id", ("owner_user_id",), False, None),
    ("channel_connections", "ix_channel_connections_provider", ("provider",), False, None),
    (
        "channel_connections",
        "uq_channel_connection_active_identity",
        ("provider", "external_account_id", "workspace_id"),
        True,
        "status = 'connected'",
    ),
    ("channel_oauth_states", "ix_channel_oauth_states_owner_user_id", ("owner_user_id",), False, None),
    ("channel_oauth_states", "ix_channel_oauth_states_provider", ("provider",), False, None),
    ("feedback", "ix_feedback_run_id", ("run_id",), False, None),
    ("feedback", "ix_feedback_thread_id", ("thread_id",), False, None),
    ("feedback", "ix_feedback_user_id", ("user_id",), False, None),
    ("run_events", "ix_events_run", ("thread_id", "run_id", "seq"), False, None),
    ("run_events", "ix_events_thread_cat_seq", ("thread_id", "category", "seq"), False, None),
    ("run_events", "ix_run_events_user_id", ("user_id",), False, None),
    ("runs", "ix_runs_thread_id", ("thread_id",), False, None),
    ("runs", "ix_runs_thread_status", ("thread_id", "status"), False, None),
    ("runs", "ix_runs_user_id", ("user_id",), False, None),
    ("threads_meta", "ix_threads_meta_assistant_id", ("assistant_id",), False, None),
    ("threads_meta", "ix_threads_meta_user_id", ("user_id",), False, None),
    ("users", "idx_users_oauth_identity", ("oauth_provider", "oauth_id"), True, "oauth_provider IS NOT NULL AND oauth_id IS NOT NULL"),
    ("users", "ix_users_email", ("email",), True, None),
    ("channel_conversations", "ix_channel_conversations_connection_id", ("connection_id",), False, None),
    ("channel_conversations", "ix_channel_conversations_owner_user_id", ("owner_user_id",), False, None),
    ("channel_conversations", "ix_channel_conversations_provider", ("provider",), False, None),
    ("channel_conversations", "ix_channel_conversations_thread_id", ("thread_id",), False, None),
)


def _escape_url_for_alembic(url: str) -> str:
    """Double literal ``%`` so ``ConfigParser`` interpolation leaves the URL intact.

    ``alembic.config.Config.set_main_option`` forwards to ``ConfigParser.set``,
    which performs ``%(name)s``-style interpolation on the value. A URL-encoded
    password like ``p%40ss`` (``@`` escaped to ``%40``) would otherwise raise
    ``InterpolationSyntaxError``. Doubling every literal ``%`` makes
    ConfigParser unescape it back to one. Shared with
    ``scripts/_autogen_revision.py`` so the round-trip rule lives in one place.
    """
    return url.replace("%", "%%")


def _alembic_safe_url(engine: AsyncEngine) -> str:
    """Render *engine*'s URL in a form alembic ``set_main_option`` accepts.

    Two pitfalls handled:

    1. ``str(engine.url)`` (and ``URL.render_as_string()`` without args) masks
       the password as ``***`` -- so alembic's stamp/upgrade would open its own
       connection with garbage credentials and fail at runtime, even though
       the live engine connects fine. Fix: ``render_as_string(hide_password=False)``.
    2. ConfigParser interpolation on ``%`` -- delegated to
       ``_escape_url_for_alembic`` so the rule is shared with the autogen
       script.
    """
    rendered = engine.url.render_as_string(hide_password=False)
    return _escape_url_for_alembic(rendered)


def _get_alembic_config(engine: AsyncEngine) -> AlembicConfig:
    """Build an in-process alembic config pointing at our migrations dir.

    Avoids reading ``alembic.ini`` from disk so the production runtime doesn't
    depend on a working-directory-relative file lookup. The ``script_location``
    is anchored at the package path on disk.
    """
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", _alembic_safe_url(engine))
    return cfg


def _get_head_revision() -> str:
    """Return the head revision id from ``versions/``, cached per process."""
    global _HEAD_REVISION
    if _HEAD_REVISION is None:
        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
        if head is None:
            raise RuntimeError("alembic has no head revision -- versions/ directory is empty")
        _HEAD_REVISION = head
    return _HEAD_REVISION


def _reflect_state(sync_conn: Any) -> dict[str, bool]:
    """Inspect *sync_conn* (sync connection inside ``run_sync``) and return:

    - ``has_alembic_version``: bool
    - ``has_deerflow_tables``: True iff at least one table that ``Base.metadata``
      knows about is present in the DB. Computed as ``reflected ∩ metadata`` so
      the bootstrap layer never hardcodes a specific table or column name --
      adding a new ORM model only changes ``Base.metadata``, not this module.
    """
    from deerflow.persistence.base import Base

    # Make sure every ORM model is imported, otherwise ``Base.metadata.tables``
    # may miss tables registered by submodules that haven't been imported yet.
    try:
        import deerflow.persistence.models  # noqa: F401
    except ImportError:
        logger.debug("deerflow.persistence.models not found; metadata may be incomplete")

    insp = sa_inspect(sync_conn)
    reflected = set(insp.get_table_names())
    metadata_tables = set(Base.metadata.tables)
    return {
        "has_alembic_version": "alembic_version" in reflected,
        "has_deerflow_tables": bool(reflected & metadata_tables),
    }


def _decide_state(state: dict[str, bool]) -> str:
    """Map a reflected DB state to one of three branch labels.

    The legacy branch covers every pre-alembic DB uniformly -- whether the
    columns added by later revisions are present or not is a question each
    revision answers for itself via the idempotent helpers in
    ``migrations/_helpers.py``.
    """
    if state["has_alembic_version"]:
        return "versioned"
    if not state["has_deerflow_tables"]:
        # Either a brand-new DB or a DB containing only tables we don't own
        # (e.g. LangGraph's checkpointer tables on a fresh deployment). The
        # empty branch provisions the tables alembic owns, then stamps head.
        return "empty"
    return "legacy"


def _requires_explicit_private_work_migration(revision: str) -> bool:
    """Return whether ordinary startup must stop at the M4 staged boundary."""
    return revision not in _PRIVATE_WORK_SAFE_REVISIONS


def _filesystem_has_legacy_private_source(home: Path) -> bool:
    """Probe known private-work paths without exposing names or contents."""
    if not home.exists():
        return False
    memory_candidates = [home / "memory.json"]
    memory_candidates.extend(home.glob("agents/*/memory.json"))
    memory_candidates.extend(home.glob("users/*/memory.json"))
    memory_candidates.extend(home.glob("users/*/agents/*/memory.json"))
    if any(path.is_file() or path.is_symlink() for path in memory_candidates):
        return True

    for user_data_pattern in ("threads/*/user-data", "users/*/threads/*/user-data"):
        for user_data in home.glob(user_data_pattern):
            for directory_name in ("uploads", "workspace", "outputs"):
                directory = user_data / directory_name
                if directory.is_dir() and any(path.is_file() or path.is_symlink() for path in directory.rglob("*")):
                    return True
    return False


def _database_has_legacy_private_source_sync(sync_conn: Any) -> bool:
    inspector = sa_inspect(sync_conn)
    present = set(inspector.get_table_names())
    for table in _LEGACY_PRIVATE_WORK_DB_TABLES:
        if table in present and sync_conn.execute(text(f'SELECT EXISTS (SELECT 1 FROM "{table}" LIMIT 1)')).scalar_one():  # noqa: S608 - fixed table allowlist
            return True

    checkpoint_tables = present & set(_LANGGRAPH_CHECKPOINT_TABLES)
    if not checkpoint_tables:
        return False

    marker_is_valid = """
        jsonb_typeof(metadata -> 'deerflow_private_scope') = 'object'
        AND jsonb_typeof(metadata -> 'deerflow_private_scope' -> 'project_id') = 'string'
        AND jsonb_typeof(metadata -> 'deerflow_private_scope' -> 'owner_user_id') = 'string'
        AND metadata -> 'deerflow_private_scope' ->> 'project_id' <> ''
        AND metadata -> 'deerflow_private_scope' ->> 'owner_user_id' <> ''
    """
    if "checkpoints" in checkpoint_tables:
        has_unmarked_checkpoint = sync_conn.execute(text(f"SELECT EXISTS (SELECT 1 FROM checkpoints WHERE ({marker_is_valid}) IS NOT TRUE LIMIT 1)")).scalar_one()
        if has_unmarked_checkpoint:
            return True

    if "checkpoint_blobs" in checkpoint_tables:
        has_unmarked_blob = sync_conn.execute(
            text(
                f"""SELECT EXISTS (
                    SELECT 1 FROM checkpoint_blobs blob
                    WHERE NOT EXISTS (
                        SELECT 1 FROM checkpoints checkpoint
                        WHERE checkpoint.thread_id = blob.thread_id
                          AND checkpoint.checkpoint_ns = blob.checkpoint_ns
                          AND ({marker_is_valid})
                    )
                    LIMIT 1
                )"""
            )
        ).scalar_one()
        if has_unmarked_blob:
            return True

    if "checkpoint_writes" in checkpoint_tables:
        has_unmarked_write = sync_conn.execute(
            text(
                f"""SELECT EXISTS (
                    SELECT 1 FROM checkpoint_writes write
                    WHERE NOT EXISTS (
                        SELECT 1 FROM checkpoints checkpoint
                        WHERE checkpoint.thread_id = write.thread_id
                          AND checkpoint.checkpoint_ns = write.checkpoint_ns
                          AND checkpoint.checkpoint_id = write.checkpoint_id
                          AND ({marker_is_valid})
                    )
                    LIMIT 1
                )"""
            )
        ).scalar_one()
        if has_unmarked_write:
            return True
    return False


async def _write_empty_install_cutover_marker(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        database_has_source = await conn.run_sync(_database_has_legacy_private_source_sync)
        if database_has_source:
            raise RuntimeError("private-work staged migration required; stop writers and run make migrate-private-work")
        await conn.execute(
            text(
                """INSERT INTO private_work_cutover_state
                (id, stage, migration_run_id, empty_domain_probe_complete,
                 checkpoint_marker_probe_complete, cutover_at, updated_at)
                VALUES (1, 'cutover_complete', NULL, true, true, now(), now())
                ON CONFLICT (id) DO NOTHING"""
            )
        )


def _run_create_all_sync(sync_conn: Any) -> None:
    """Create all DeerFlow-owned tables on *sync_conn*."""
    # Import here to ensure all model classes are registered with Base.metadata.
    from deerflow.persistence.base import Base

    try:
        import deerflow.persistence.models  # noqa: F401
    except ImportError:
        logger.debug("deerflow.persistence.models not found; bootstrap will create empty schema")

    Base.metadata.create_all(sync_conn)
    # M4's descriptive revision identifiers exceed Alembic's historical
    # VARCHAR(32) default. Empty installs create the control table explicitly;
    # existing installs are widened by revision 0008.
    sync_conn.execute(
        text(
            """CREATE TABLE IF NOT EXISTS alembic_version (
                version_num VARCHAR(64) NOT NULL,
                CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            )"""
        )
    )


def _build_baseline_metadata() -> sa.MetaData:
    """Build the immutable 0001-era table catalog used for legacy backfill."""
    from deerflow.persistence.base import Base

    try:
        import deerflow.persistence.models  # noqa: F401
    except ImportError:
        logger.debug("deerflow.persistence.models not found; baseline backfill may be incomplete")

    metadata = sa.MetaData()
    core_owner_tables = {"threads_meta", "runs", "run_events", "feedback"}
    for table_name, column_names in _BASELINE_COLUMNS.items():
        source_table = Base.metadata.tables[table_name]
        columns: list[sa.Column[Any]] = []
        for column_name in column_names:
            source_name = "owner_user_id" if column_name == "user_id" and table_name in core_owner_tables else column_name
            source_column = source_table.c[source_name]
            column_type = source_column.type.copy()
            nullable = source_column.nullable
            if column_name == "user_id" and table_name in core_owner_tables:
                column_type = sa.String(64)
                nullable = True
            elif column_name == "owner_user_id":
                column_type = sa.String(64)
            server_default = None
            if table_name == "runs" and column_name == "token_usage_by_model":
                server_default = sa.text("'{}'")
            columns.append(
                sa.Column(
                    column_name,
                    column_type,
                    nullable=nullable,
                    autoincrement=True if table_name == "run_events" and column_name == "id" else "auto",
                    server_default=server_default,
                )
            )

        constraints: list[sa.Constraint] = [sa.PrimaryKeyConstraint(*_BASELINE_PRIMARY_KEYS[table_name])]
        if table_name == "channel_connections":
            constraints.append(
                sa.UniqueConstraint(
                    "owner_user_id",
                    "provider",
                    "external_account_id",
                    "workspace_id",
                    name="uq_channel_connection_owner_provider_identity",
                )
            )
        elif table_name == "feedback":
            constraints.append(
                sa.UniqueConstraint(
                    "thread_id",
                    "run_id",
                    "user_id",
                    name="uq_feedback_thread_run_user",
                )
            )
        elif table_name == "run_events":
            constraints.append(sa.UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"))
        elif table_name == "channel_conversations":
            constraints.extend(
                (
                    sa.ForeignKeyConstraint(
                        ["connection_id"],
                        ["channel_connections.id"],
                        ondelete="CASCADE",
                    ),
                    sa.UniqueConstraint(
                        "connection_id",
                        "external_conversation_id",
                        "external_topic_id",
                        name="uq_channel_conversation_connection_external",
                    ),
                )
            )
        elif table_name == "channel_credentials":
            constraints.append(
                sa.ForeignKeyConstraint(
                    ["connection_id"],
                    ["channel_connections.id"],
                    ondelete="CASCADE",
                )
            )
        sa.Table(table_name, metadata, *columns, *constraints)

    for table_name, index_name, column_names, unique, predicate in _BASELINE_INDEXES:
        table = metadata.tables[table_name]
        kwargs: dict[str, Any] = {}
        if predicate is not None:
            kwargs["postgresql_where"] = sa.text(predicate)
        sa.Index(index_name, *(table.c[name] for name in column_names), unique=unique, **kwargs)
    return metadata


def _run_baseline_create_all_sync(sync_conn: Any) -> None:
    """Create only the baseline tables on *sync_conn* (idempotent via checkfirst).

    Used by the legacy branch to backfill baseline-era tables missing from
    the user's DB. Restricting the table list to ``_BASELINE_TABLE_NAMES``
    is the safety property: an unrestricted ``create_all`` would also create
    tables introduced by later revisions, which would then collide with
    those revisions' ``op.create_table`` calls when alembic ran upgrade.
    """
    baseline_metadata = _build_baseline_metadata()
    baseline_tables = [baseline_metadata.tables[name] for name in _BASELINE_TABLE_NAMES]
    baseline_metadata.create_all(sync_conn, tables=baseline_tables, checkfirst=True)


def _reset_failed_empty_bootstrap_sync(sync_conn: Any) -> None:
    """Restore the DeerFlow-owned portion of a database that started empty."""
    from deerflow.persistence.base import Base

    Base.metadata.drop_all(sync_conn, checkfirst=True)
    sync_conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


async def _attempt_failed_empty_bootstrap_cleanup(engine: AsyncEngine) -> None:
    """Finish best-effort cleanup without replacing the primary failure.

    Caller cancellation is deliberately absorbed while cleanup is in flight:
    returning early would release the advisory lock while destructive cleanup
    was still running on a detached task. Once a connection is acquired (which
    is governed by the engine's pool_timeout), transaction-local PostgreSQL
    deadlines bound lock waits and the cleanup statement itself.
    """

    async def cleanup() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('lock_timeout', :value, true)"),
                {"value": f"{_EMPTY_BOOTSTRAP_LOCK_TIMEOUT_MS}ms"},
            )
            await conn.execute(
                text("SELECT set_config('statement_timeout', :value, true)"),
                {"value": f"{_EMPTY_BOOTSTRAP_STATEMENT_TIMEOUT_MS}ms"},
            )
            await conn.run_sync(_reset_failed_empty_bootstrap_sync)

    task = asyncio.create_task(cleanup(), name="deerflow-empty-bootstrap-cleanup")
    while True:
        try:
            await asyncio.shield(task)
            return
        except asyncio.CancelledError:
            if not task.done():
                continue
            try:
                task.result()
            except BaseException:
                pass
            logger.error("bootstrap: empty-schema cleanup did not complete; original failure preserved")
            return
        except BaseException:
            logger.error("bootstrap: empty-schema cleanup did not complete; original failure preserved")
            return


def _stamp(cfg: AlembicConfig, revision: str) -> None:
    """Synchronous alembic stamp; callers must wrap in ``asyncio.to_thread``."""
    alembic_command.stamp(cfg, revision)


def _upgrade(cfg: AlembicConfig, revision: str) -> None:
    """Synchronous alembic upgrade; callers must wrap in ``asyncio.to_thread``."""
    alembic_command.upgrade(cfg, revision)


# ---------------------------------------------------------------------------
# Cross-process locking
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _postgres_lock(engine: AsyncEngine):
    """Hold a Postgres session-level advisory lock for the body of the block.

    Session-level (not transaction-level) so the lock outlives implicit
    transactions opened by alembic during ``stamp`` / ``upgrade``. The lock
    is released explicitly on the way out and -- as a safety net -- when the
    backing session disconnects (process crash, kill -9).

    Idle-in-transaction protection
    ------------------------------

    A dedicated ``NullPool`` connection avoids consuming the application's
    pool while alembic opens a different connection. This matters for valid
    ``pool_size=1, max_overflow=0`` deployments, which would otherwise starve
    during startup. The lock connection auto-begins a transaction on its first
    ``execute`` and then sits idle while ``asyncio.to_thread(_upgrade, ...)``
    runs alembic. Managed Postgres
    (RDS, Cloud SQL, Supabase) ships with ``idle_in_transaction_session_
    timeout`` set to 1-10 minutes by default; if alembic takes longer than
    that, the host kills this idle-in-transaction session, and because
    advisory locks are session-scoped, the lock is **silently released**.
    A second Gateway then acquires it and runs DDL concurrently with the
    first -- defeating the whole purpose of the lock.

    Defence: ``SET LOCAL idle_in_transaction_session_timeout = 0`` and
    ``SET LOCAL statement_timeout = 0`` disable both timeout classes **for
    this transaction only** (no global / role-level effect).
    Self-hosted Postgres usually ships with the timeout off, so this is a
    no-op there; on managed PG it is what keeps the lock alive while DDL
    runs. Must execute *before* ``pg_advisory_lock`` so a slow lock acquire
    on a heavily-contended cluster is itself protected.
    """
    lock_engine = create_async_engine(engine.url.render_as_string(hide_password=False), poolclass=NullPool)
    try:
        async with lock_engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
                await conn.execute(text("SET LOCAL statement_timeout = 0"))
                await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _PG_LOCK_KEY})
                try:
                    logger.info("bootstrap: acquired postgres advisory lock key=0x%x", _PG_LOCK_KEY)
                    yield
                finally:
                    try:
                        unlocked = await conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": _PG_LOCK_KEY})
                        if unlocked is not True:
                            logger.warning("bootstrap: postgres advisory lock was not held during unlock")
                    except Exception:  # noqa: BLE001
                        logger.warning("bootstrap: pg_advisory_unlock raised; session close will release", exc_info=True)
    finally:
        await lock_engine.dispose()


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def _run_alembic_offload(function, *args) -> None:
    """Run synchronous Alembic work without releasing the DB lock on cancel."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    pending_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            # Each shield wait blocks on the worker again, so repeated cancels
            # are absorbed without spinning until synchronous Alembic exits.
            if pending_cancellation is None:
                pending_cancellation = exc
        except Exception:  # noqa: BLE001 - cancellation remains authoritative
            if pending_cancellation is None:
                raise
            logger.exception("Alembic offload failed while bootstrap cancellation was pending")
            raise pending_cancellation
    if pending_cancellation is not None:
        raise pending_cancellation


async def bootstrap_schema(engine: AsyncEngine) -> None:
    """Bring the DB schema to head.

    PostgreSQL calls are serialised across processes with an advisory lock.

    Branch dispatch is documented at module top. ``alembic.command.stamp`` and
    ``alembic.command.upgrade`` are synchronous and would block the event
    loop; both are wrapped in ``asyncio.to_thread``.
    """
    head = _get_head_revision()
    cfg = _get_alembic_config(engine)

    async with _postgres_lock(engine):
        async with engine.connect() as conn:
            state = await conn.run_sync(_reflect_state)
        decision = _decide_state(state)

        if decision == "empty":
            logger.info("bootstrap: branch=empty -> create_all + stamp head (%s)", head)
            async with engine.begin() as conn:
                await conn.run_sync(_run_create_all_sync)
            try:
                await _run_alembic_offload(_stamp, cfg, head)
                has_filesystem_source = await asyncio.to_thread(_filesystem_has_legacy_private_source, runtime_home())
                if has_filesystem_source:
                    raise RuntimeError("private-work staged migration required; stop writers and run make migrate-private-work")
                await _write_empty_install_cutover_marker(engine)
            except BaseException:
                # This invocation proved the DeerFlow-owned schema was empty
                # before create_all. Restore that state so a failed stamp never
                # turns the next retry into a false legacy migration that
                # collides with post-baseline tables created from current
                # metadata.
                await _attempt_failed_empty_bootstrap_cleanup(engine)
                raise

        elif decision == "legacy":
            async with engine.connect() as conn:
                database_has_source = await conn.run_sync(_database_has_legacy_private_source_sync)
            filesystem_has_source = await asyncio.to_thread(_filesystem_has_legacy_private_source, runtime_home())
            if database_has_source or filesystem_has_source:
                raise RuntimeError("private-work staged migration required; stop writers and run make migrate-private-work")
            logger.info(
                "bootstrap: branch=legacy -> baseline-era backfill + stamp %s + upgrade %s + require explicit M4 migration",
                _BASELINE_REVISION,
                _PRIVATE_WORK_PRE_EXPAND_REVISION,
            )
            # ``_run_baseline_create_all_sync`` is restricted to
            # ``_BASELINE_TABLE_NAMES`` -- a plain ``Base.metadata.create_all``
            # would also create tables introduced by later revisions and
            # collide with their ``op.create_table`` on the subsequent
            # upgrade. With the restriction, missing baseline tables are
            # backfilled and post-baseline ``create_table`` revisions run
            # against a DB where their tables genuinely do not yet exist.
            # The post-create_all column-add revisions still no-op via
            # ``safe_add_column`` because baseline-era tables now have the
            # columns those revisions would add.
            async with engine.begin() as conn:
                await conn.run_sync(_run_baseline_create_all_sync)
            await _run_alembic_offload(_stamp, cfg, _BASELINE_REVISION)
            await _run_alembic_offload(_upgrade, cfg, _PRIVATE_WORK_PRE_EXPAND_REVISION)
            raise RuntimeError("private-work staged migration required; stop writers and run make migrate-private-work")

        elif decision == "versioned":
            logger.info("bootstrap: branch=versioned -> upgrade head (%s)", head)
            async with engine.connect() as conn:
                current_revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            if _requires_explicit_private_work_migration(str(current_revision)):
                raise RuntimeError("private-work staged migration required; stop writers and run make migrate-private-work")
            await _run_alembic_offload(_upgrade, cfg, "head")

        else:  # pragma: no cover -- defensive
            raise RuntimeError(f"bootstrap: unhandled decision {decision!r}")

    logger.info("bootstrap: complete (backend=postgres)")
