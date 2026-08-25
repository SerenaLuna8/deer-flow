from __future__ import annotations

import re
import stat
from pathlib import Path

import deerflow.persistence.models  # noqa: F401 -- populate metadata
from deerflow.persistence.base import Base
from deerflow.persistence.final_schema_contract import (
    LANGGRAPH_COMMENT_SIGNATURE,
    _rows_digest,
)
from scripts import generate_schema_comments, setup_postgres

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"
COMMENTS_PATH = SCHEMA_PATH.with_name("schema_comments.sql")
CHINESE_TEXT_PATTERN = re.compile(r"[\u3400-\u9fff]")


def test_generated_comment_artifact_and_embedded_schema_are_current() -> None:
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8")
    tables = generate_schema_comments._parse_schema(schema_text)
    expected = generate_schema_comments._render(
        tables,
        schema_name=SCHEMA_PATH.name,
    )

    assert COMMENTS_PATH.read_bytes() == expected
    assert SCHEMA_PATH.read_bytes() == generate_schema_comments._embedded_schema(
        schema_text,
        expected,
    )
    assert schema_text.index(generate_schema_comments._BLOCK_END) < schema_text.index("SELECT ensure_run_events_month_partition(now());")


def test_static_comments_exactly_cover_metadata_and_alembic() -> None:
    tables = generate_schema_comments._parse_schema(SCHEMA_PATH.read_text(encoding="utf-8"))
    definitions = {table.name: table.columns for table in tables}
    assert set(definitions) == set(Base.metadata.tables) | {"alembic_version"}
    assert definitions["alembic_version"] == ("version_num",)
    for table_name, table in Base.metadata.tables.items():
        assert definitions[table_name] == tuple(column.name for column in table.columns)

    comments = COMMENTS_PATH.read_text(encoding="utf-8")
    table_comments = re.findall(
        r"^COMMENT ON TABLE ([a-z][a-z0-9_]*) IS '(.+)';$",
        comments,
        re.MULTILINE,
    )
    column_comments = re.findall(
        r"^COMMENT ON COLUMN ([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*) IS '(.+)';$",
        comments,
        re.MULTILINE,
    )
    assert len(table_comments) == 97
    assert len(column_comments) == 1194
    assert {name for name, _comment in table_comments} == set(definitions)
    assert {(table, column) for table, column, _comment in column_comments} == {(table, column) for table, columns in definitions.items() for column in columns}
    assert all(CHINESE_TEXT_PATTERN.search(comment) for _name, comment in table_comments)
    assert all(CHINESE_TEXT_PATTERN.search(comment) for _table, _column, comment in column_comments)


def test_langgraph_readiness_comments_match_the_frozen_signature() -> None:
    inventory = sorted(
        setup_postgres._LANGGRAPH_COMMENT_INVENTORY,
        key=lambda item: item.table_name,
    )
    table_rows = tuple((item.table_name, item.table_comment) for item in inventory)
    column_rows = tuple((item.table_name, column_name, comment) for item in inventory for column_name, comment in item.column_comments)
    assert len(table_rows) == LANGGRAPH_COMMENT_SIGNATURE["table_comments"].count
    assert _rows_digest(table_rows) == LANGGRAPH_COMMENT_SIGNATURE["table_comments"].digest
    assert len(column_rows) == LANGGRAPH_COMMENT_SIGNATURE["column_comments"].count
    assert _rows_digest(column_rows) == LANGGRAPH_COMMENT_SIGNATURE["column_comments"].digest


def test_privacy_and_storage_sensitive_columns_use_table_specific_comments() -> None:
    expected = {
        ("system_model_configs", "max_input_tokens"),
        ("runs", "first_human_message"),
        ("runs", "last_ai_message"),
        ("run_events", "content"),
        ("skill_version_files", "content"),
        ("file_chunks", "content"),
        ("skill_design_draft_files", "content"),
        ("skill_design_operation_baseline_files", "content"),
        ("memory_documents", "content"),
        ("memory_document_versions", "content"),
        ("run_memory_context_snapshots", "content"),
        ("execution_approval_requests", "command_private_json"),
        ("execution_approval_requests", "source_run_id"),
        ("execution_approval_requests", "expires_at"),
        (
            "execution_approval_result_receipts",
            "result_private_json",
        ),
        ("execution_approval_result_receipts", "outcome"),
        (
            "execution_approval_output_delivery_obligations",
            "intent_private_json",
        ),
        (
            "execution_approval_output_delivery_obligations",
            "terminal_at",
        ),
        ("jobs", "execution_domain_affinity"),
        ("skill_versions", "revoked_at"),
        ("project_channel_group_bindings", "agent_scope"),
        ("project_channel_group_bindings", "agent_asset_id"),
        ("project_channel_group_bindings", "deleted_at"),
        ("run_asset_versions", "snapshot_json"),
        ("system_asset_upgrade_audit", "before_checksum"),
        ("system_asset_upgrade_audit", "after_checksum"),
        ("system_asset_upgrade_audit", "package_digest"),
        ("system_asset_upgrade_audit", "operator_identity"),
    }

    assert set(generate_schema_comments._TABLE_COLUMN_PHRASES) == expected
    assert "2000 字符" in generate_schema_comments._TABLE_COLUMN_PHRASES[("runs", "first_human_message")]
    assert "私有消息" in generate_schema_comments._TABLE_COLUMN_PHRASES[("run_events", "content")]
    assert "原始字节" in generate_schema_comments._TABLE_COLUMN_PHRASES[("file_chunks", "content")]


def test_partition_creator_copies_parent_table_and_column_comments() -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    function = schema.split(
        "CREATE OR REPLACE FUNCTION ensure_run_events_month_partition",
        1,
    )[1].split("$$ LANGUAGE plpgsql;", 1)[0]
    assert "obj_description('run_events'::regclass, 'pg_class')" in function
    assert "col_description(attribute.attrelid, attribute.attnum)" in function
    assert "COMMENT ON TABLE %I IS %L" in function
    assert "COMMENT ON COLUMN %I.%I IS %L" in function


def test_generator_atomic_write_preserves_existing_mode(tmp_path: Path) -> None:
    existing = tmp_path / "existing.sql"
    existing.write_bytes(b"old\n")
    existing.chmod(0o640)
    generated = tmp_path / "generated.sql"

    generate_schema_comments._atomic_write(existing, b"new\n")
    generate_schema_comments._atomic_write(generated, b"new\n")

    assert stat.S_IMODE(existing.stat().st_mode) == 0o640
    assert stat.S_IMODE(generated.stat().st_mode) == 0o644
