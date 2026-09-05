from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
from actweave_knowledge.persistence.models import KnowledgeOrmBase

import deerflow.persistence.models  # noqa: F401 -- populate metadata
from deerflow.persistence import bootstrap
from deerflow.persistence.base import Base
from deerflow.persistence.final_schema_contract import (
    FINAL_APP_TABLES,
    LANGGRAPH_COMMENT_SIGNATURE,
    _rows_digest,
)
from scripts import generate_schema_comments, setup_postgres

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"
COMMENTS_PATH = SCHEMA_PATH.with_name("schema_comments.sql")
CHINESE_TEXT_PATTERN = re.compile(r"[\u3400-\u9fff]")
SCHEMA_COMMENTS_PLACEHOLDER = "-- INCLUDE GENERATED SCHEMA COMMENTS FROM schema_comments.sql"


def test_generated_comment_artifact_is_current() -> None:
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8")
    tables = generate_schema_comments._parse_schema(schema_text)
    expected = generate_schema_comments._render(
        tables,
        schema_name=SCHEMA_PATH.name,
    )

    assert COMMENTS_PATH.read_bytes() == expected
    assert (
        generate_schema_comments.main(
            [
                "--schema",
                str(SCHEMA_PATH),
                "--output",
                str(COMMENTS_PATH),
                "--check",
            ]
        )
        == 0
    )


def test_full_schema_uses_one_external_comment_placeholder_before_partition_creation() -> None:
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8")

    assert not re.search(
        r"^COMMENT ON TABLE [a-z][a-z0-9_]* IS ",
        schema_text,
        re.MULTILINE,
    )
    assert not re.search(
        r"^COMMENT ON COLUMN [a-z][a-z0-9_]*\.[a-z][a-z0-9_]* IS ",
        schema_text,
        re.MULTILINE,
    )
    assert schema_text.count(SCHEMA_COMMENTS_PLACEHOLDER) == 1
    assert schema_text.index(SCHEMA_COMMENTS_PLACEHOLDER) < schema_text.index("SELECT ensure_run_events_month_partition(now());")


def test_bootstrap_composes_comments_into_one_transaction() -> None:
    comments = COMMENTS_PATH.read_text(encoding="utf-8").rstrip()

    payload = bootstrap._read_full_schema_sql()

    assert SCHEMA_COMMENTS_PLACEHOLDER not in payload
    assert comments in payload
    assert payload.index(comments) < payload.index("SELECT ensure_run_events_month_partition(now());")
    assert len(re.findall(r"^BEGIN;$", payload, re.MULTILINE)) == 1
    assert len(re.findall(r"^COMMIT;$", payload, re.MULTILINE)) == 1


def test_bootstrap_composes_comments_when_schema_marker_is_withheld() -> None:
    comments = COMMENTS_PATH.read_text(encoding="utf-8").rstrip()

    payload = bootstrap._read_full_schema_sql(publish_marker=False)

    assert comments in payload
    assert bootstrap._SCHEMA_MARKER_INSERT not in payload
    assert "Schema V1 marker is published only after setup bootstrap completes." in payload
    assert len(re.findall(r"^BEGIN;$", payload, re.MULTILINE)) == 1
    assert len(re.findall(r"^COMMIT;$", payload, re.MULTILINE)) == 1


@pytest.mark.parametrize("placeholder_count", [0, 2])
def test_bootstrap_rejects_missing_or_duplicate_comment_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    placeholder_count: int,
) -> None:
    schema_path = tmp_path / "full_schema.sql"
    comments_path = tmp_path / "schema_comments.sql"
    placeholders = "\n".join(SCHEMA_COMMENTS_PLACEHOLDER for _ in range(placeholder_count))
    schema_path.write_text(
        "\n".join(
            (
                "BEGIN;",
                bootstrap._SCHEMA_MARKER_INSERT,
                placeholders,
                "SELECT ensure_run_events_month_partition(now());",
                "COMMIT;",
                "",
            )
        ),
        encoding="utf-8",
    )
    comments_path.write_text("COMMENT ON TABLE example IS 'example';\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "_FULL_SCHEMA_PATH", schema_path)
    monkeypatch.setattr(bootstrap, "_SCHEMA_COMMENTS_PATH", comments_path)

    with pytest.raises(RuntimeError, match="schema comments"):
        bootstrap._read_full_schema_sql()


def test_bootstrap_rejects_missing_comment_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "full_schema.sql"
    schema_path.write_text(
        "\n".join(
            (
                "BEGIN;",
                bootstrap._SCHEMA_MARKER_INSERT,
                SCHEMA_COMMENTS_PLACEHOLDER,
                "SELECT ensure_run_events_month_partition(now());",
                "COMMIT;",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_FULL_SCHEMA_PATH", schema_path)
    monkeypatch.setattr(
        bootstrap,
        "_SCHEMA_COMMENTS_PATH",
        tmp_path / "missing-schema-comments.sql",
    )

    with pytest.raises(RuntimeError, match="schema comments"):
        bootstrap._read_full_schema_sql()


@pytest.mark.parametrize(
    "injected_statement",
    [
        pytest.param("COMMIT;", id="transaction-boundary"),
        pytest.param("DROP TABLE users;", id="ddl"),
        pytest.param("DELETE FROM users;", id="dml"),
        pytest.param(bootstrap._SCHEMA_MARKER_INSERT, id="schema-marker"),
    ],
)
def test_bootstrap_rejects_non_comment_statements_in_comment_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    injected_statement: str,
) -> None:
    comments_path = tmp_path / "schema_comments.sql"
    comments_path.write_text(
        f"{COMMENTS_PATH.read_text(encoding='utf-8').rstrip()}\n{injected_statement}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_SCHEMA_COMMENTS_PATH", comments_path)

    with pytest.raises(RuntimeError, match="non-COMMENT statement"):
        bootstrap.validate_schema_installation_artifacts()


def test_bootstrap_rejects_symbolic_link_comment_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "schema-comments-target.sql"
    target.write_bytes(COMMENTS_PATH.read_bytes())
    comments_path = tmp_path / "schema_comments.sql"
    comments_path.symlink_to(target)
    monkeypatch.setattr(bootstrap, "_SCHEMA_COMMENTS_PATH", comments_path)

    with pytest.raises(RuntimeError, match="regular file"):
        bootstrap.validate_schema_installation_artifacts()


@pytest.mark.parametrize(
    ("content", "error_pattern"),
    [
        pytest.param(b"\xff", "unavailable", id="invalid-utf8"),
        pytest.param(
            b"\xef\xbb\xbf" + COMMENTS_PATH.read_bytes(),
            "invalid",
            id="utf8-bom",
        ),
        pytest.param(
            COMMENTS_PATH.read_bytes() + b"\x00",
            "invalid",
            id="nul-byte",
        ),
    ],
)
def test_bootstrap_rejects_invalid_comment_artifact_encoding_or_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: bytes,
    error_pattern: str,
) -> None:
    comments_path = tmp_path / "schema_comments.sql"
    comments_path.write_bytes(content)
    monkeypatch.setattr(bootstrap, "_SCHEMA_COMMENTS_PATH", comments_path)

    with pytest.raises(RuntimeError, match=error_pattern):
        bootstrap.validate_schema_installation_artifacts()


def test_bootstrap_rejects_syntactically_valid_comments_with_stale_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    comments_path = tmp_path / "schema_comments.sql"
    original = COMMENTS_PATH.read_text(encoding="utf-8")
    changed = original.replace(
        "记录项目邀请码失败尝试的限流窗口。",
        "记录项目邀请码失败尝试的另一个限流窗口。",
        1,
    )
    assert changed != original
    comments_path.write_text(changed, encoding="utf-8")
    monkeypatch.setattr(bootstrap, "_SCHEMA_COMMENTS_PATH", comments_path)

    with pytest.raises(RuntimeError, match="stale content manifest"):
        bootstrap.validate_schema_installation_artifacts()


def test_bootstrap_rejects_comment_placeholder_before_schema_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "full_schema.sql"
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8")
    schema_without_placeholder = schema_text.replace(
        SCHEMA_COMMENTS_PLACEHOLDER,
        "",
    )
    schema_path.write_text(
        schema_without_placeholder.replace(
            "BEGIN;\n",
            f"BEGIN;\n{SCHEMA_COMMENTS_PLACEHOLDER}\n",
            1,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "_FULL_SCHEMA_PATH", schema_path)

    with pytest.raises(RuntimeError, match="placeholder"):
        bootstrap.validate_schema_installation_artifacts()


@pytest.mark.parametrize("placeholder_count", [0, 2])
def test_generator_check_rejects_missing_or_duplicate_placeholder(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    placeholder_count: int,
) -> None:
    schema_path = tmp_path / "full_schema.sql"
    comments_path = tmp_path / "schema_comments.sql"
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8")
    replacement = "\n".join(SCHEMA_COMMENTS_PLACEHOLDER for _ in range(placeholder_count))
    schema_path.write_text(
        schema_text.replace(SCHEMA_COMMENTS_PLACEHOLDER, replacement),
        encoding="utf-8",
    )
    comments_path.write_bytes(COMMENTS_PATH.read_bytes())

    assert (
        generate_schema_comments.main(
            [
                "--schema",
                str(schema_path),
                "--output",
                str(comments_path),
                "--check",
            ]
        )
        == 1
    )
    assert "one external comment placeholder" in capsys.readouterr().err


def test_static_comments_exactly_cover_metadata_and_alembic() -> None:
    tables = generate_schema_comments._parse_schema(SCHEMA_PATH.read_text(encoding="utf-8"))
    definitions = {table.name: table.columns for table in tables}
    assert set(definitions) == FINAL_APP_TABLES | {"alembic_version"}
    assert definitions["alembic_version"] == ("version_num",)
    for table_name, table in Base.metadata.tables.items():
        assert definitions[table_name] == tuple(column.name for column in table.columns)
    for table_name, table in KnowledgeOrmBase.metadata.tables.items():
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
    assert len(table_comments) == 113
    assert len(column_comments) == 1450
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
        ("knowledge_documents", "source_sha256"),
        ("knowledge_documents", "published_extraction_id"),
        ("knowledge_documents", "parsing_profile"),
        ("knowledge_documents", "parse_warnings"),
        ("knowledge_documents", "capability_revision"),
        ("knowledge_documents", "upload_state"),
        ("knowledge_documents", "quota_state"),
        ("knowledge_extractions", "id"),
        ("knowledge_extractions", "project_id"),
        ("knowledge_extractions", "knowledge_base_id"),
        ("knowledge_extractions", "knowledge_document_id"),
        ("knowledge_extractions", "source_sha256"),
        ("knowledge_extractions", "parser_fingerprint"),
        ("knowledge_extractions", "normalization_version"),
        ("knowledge_extractions", "state"),
        ("knowledge_extractions", "manifest_storage_key"),
        ("knowledge_extractions", "manifest_sha256"),
        ("knowledge_extractions", "manifest_size_bytes"),
        ("knowledge_extractions", "manifest_upload_state"),
        ("knowledge_extractions", "manifest_quota_state"),
        ("knowledge_extractions", "created_task_id"),
        ("knowledge_extractions", "created_attempt"),
        ("knowledge_extractions", "created_claim_token"),
        ("knowledge_extractions", "target_document_version"),
        ("knowledge_extractions", "created_at"),
        ("knowledge_extractions", "completed_at"),
        ("knowledge_extractions", "unpublished_expires_at"),
        ("knowledge_extractions", "delete_error"),
        ("knowledge_attachments", "id"),
        ("knowledge_attachments", "extraction_id"),
        ("knowledge_attachments", "project_id"),
        ("knowledge_attachments", "knowledge_base_id"),
        ("knowledge_attachments", "knowledge_document_id"),
        ("knowledge_attachments", "sha256"),
        ("knowledge_attachments", "media_type"),
        ("knowledge_attachments", "size_bytes"),
        ("knowledge_attachments", "width"),
        ("knowledge_attachments", "height"),
        ("knowledge_attachments", "storage_key"),
        ("knowledge_attachments", "state"),
        ("knowledge_attachments", "upload_state"),
        ("knowledge_attachments", "quota_state"),
        ("knowledge_attachments", "delete_error"),
        ("knowledge_segment_attachments", "project_id"),
        ("knowledge_segment_attachments", "knowledge_base_id"),
        ("knowledge_segment_attachments", "knowledge_document_id"),
        ("knowledge_segment_attachments", "extraction_id"),
        ("knowledge_segment_attachments", "segment_id"),
        ("knowledge_segment_attachments", "attachment_id"),
        ("knowledge_segment_attachments", "position"),
        ("knowledge_segment_attachments", "alt_text"),
        ("knowledge_segments", "extraction_id"),
        ("knowledge_segments", "index_text"),
        ("knowledge_segments", "token_count"),
        ("knowledge_segments", "source_spans"),
        ("knowledge_segment_children", "index_text"),
        ("knowledge_segment_children", "token_count"),
        ("knowledge_segment_children", "source_spans"),
        ("knowledge_tasks", "extraction_id"),
        ("knowledge_system_settings", "etl_type"),
        ("knowledge_system_settings", "extraction_cache_enabled"),
        ("system_model_configs", "max_input_tokens"),
        ("system_model_configs", "provider_id"),
        ("system_model_configs", "deleted_at"),
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
        ("context_evidence", "payload_json"),
        ("context_projection_heads", "projection_json"),
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
        ("model_providers", "name"),
        ("model_providers", "base_url"),
        ("model_providers", "request_timeout_seconds"),
        ("model_providers", "api_key_nonce"),
        ("model_providers", "api_key_ciphertext"),
        ("model_providers", "deleted_at"),
        ("model_provider_models", "provider_id"),
        ("model_provider_models", "model_type"),
        ("model_provider_models", "model_name"),
        ("model_provider_models", "embedding_dimension"),
        ("model_provider_models", "max_batch"),
        ("model_provider_models", "status"),
        ("model_provider_models", "deleted_at"),
        ("knowledge_bases", "name"),
        ("knowledge_bases", "description"),
        ("knowledge_bases", "embedding_model_id"),
        ("knowledge_bases", "reranker_model_id"),
        ("knowledge_bases", "status"),
        ("knowledge_bases", "default_top_k"),
        ("knowledge_bases", "default_score_threshold"),
        ("knowledge_bases", "default_relative_cutoff"),
        ("knowledge_bases", "chunking_mode"),
        ("knowledge_bases", "retrieval_mode"),
        ("knowledge_bases", "summary_index_enabled"),
        ("knowledge_documents", "knowledge_base_id"),
        ("knowledge_documents", "name"),
        ("knowledge_documents", "original_name"),
        ("knowledge_documents", "storage_key"),
        ("knowledge_documents", "media_type"),
        ("knowledge_documents", "size_bytes"),
        ("knowledge_documents", "status"),
        ("knowledge_documents", "enabled"),
        ("knowledge_documents", "version"),
        ("knowledge_documents", "chunk_size"),
        ("knowledge_documents", "chunk_overlap"),
        ("knowledge_documents", "chunk_separator"),
        ("knowledge_documents", "remove_extra_spaces"),
        ("knowledge_documents", "remove_urls_emails"),
        ("knowledge_documents", "chunking_mode"),
        ("knowledge_documents", "child_chunk_size"),
        ("knowledge_documents", "child_chunk_separator"),
        ("knowledge_documents", "segment_count"),
        ("knowledge_documents", "word_count"),
        ("knowledge_documents", "hit_count"),
        ("knowledge_documents", "doc_metadata"),
        ("knowledge_documents", "error_message"),
        ("knowledge_documents", "published_version"),
        ("knowledge_metadata_fields", "knowledge_base_id"),
        ("knowledge_metadata_fields", "name"),
        ("knowledge_metadata_fields", "field_type"),
        ("knowledge_segments", "knowledge_base_id"),
        ("knowledge_segments", "knowledge_document_id"),
        ("knowledge_segments", "document_version"),
        ("knowledge_segments", "position"),
        ("knowledge_segments", "content"),
        ("knowledge_segments", "word_count"),
        ("knowledge_segments", "enabled"),
        ("knowledge_segments", "hit_count"),
        ("knowledge_segments", "source_position"),
        ("knowledge_segments", "embedding"),
        ("knowledge_segments", "lexical_tsv"),
        ("knowledge_segments", "lexical_version"),
        ("knowledge_segment_children", "knowledge_base_id"),
        ("knowledge_segment_children", "knowledge_document_id"),
        ("knowledge_segment_children", "knowledge_segment_id"),
        ("knowledge_segment_children", "document_version"),
        ("knowledge_segment_children", "position"),
        ("knowledge_segment_children", "content"),
        ("knowledge_segment_children", "word_count"),
        ("knowledge_segment_children", "embedding"),
        ("knowledge_segment_children", "lexical_tsv"),
        ("knowledge_segment_children", "lexical_version"),
        ("knowledge_segment_summaries", "knowledge_base_id"),
        ("knowledge_segment_summaries", "knowledge_document_id"),
        ("knowledge_segment_summaries", "knowledge_segment_id"),
        ("knowledge_segment_summaries", "document_version"),
        ("knowledge_segment_summaries", "content"),
        ("knowledge_segment_summaries", "source_content_digest"),
        ("knowledge_segment_summaries", "embedding"),
        ("knowledge_queries", "owner_user_id"),
        ("knowledge_queries", "knowledge_base_ids"),
        ("knowledge_queries", "query"),
        ("knowledge_queries", "source"),
        ("knowledge_queries", "result_count"),
        ("knowledge_queries", "top_score"),
        ("knowledge_queries", "top_score_kind"),
        ("knowledge_queries", "strategy_version"),
        ("knowledge_tasks", "resource_id"),
        ("knowledge_tasks", "kind"),
        ("knowledge_tasks", "target_version"),
        ("knowledge_tasks", "storage_key"),
        ("knowledge_tasks", "status"),
        ("knowledge_tasks", "attempt_count"),
        ("knowledge_tasks", "max_attempts"),
        ("knowledge_tasks", "available_at"),
        ("knowledge_tasks", "claim_token"),
        ("knowledge_tasks", "lease_until"),
        ("knowledge_tasks", "error_message"),
        ("knowledge_tasks", "finished_at"),
        ("knowledge_tasks", "reparse_settings"),
        ("knowledge_tasks", "stage"),
        ("knowledge_tasks", "completed_units"),
        ("knowledge_tasks", "total_units"),
        ("knowledge_tasks", "progress_updated_at"),
        ("knowledge_system_settings", "revision"),
        ("knowledge_system_settings", "enabled"),
        ("knowledge_system_settings", "worker_concurrency"),
        ("knowledge_system_settings", "task_timeout_seconds"),
        ("knowledge_system_settings", "upload_max_bytes"),
        ("knowledge_system_settings", "max_knowledge_bases_per_project"),
        ("knowledge_system_settings", "max_documents_per_knowledge_base"),
        ("knowledge_system_settings", "max_segments_per_document"),
        ("knowledge_system_settings", "minio_endpoint"),
        ("knowledge_system_settings", "minio_bucket"),
        ("knowledge_system_settings", "minio_access_key"),
        ("knowledge_system_settings", "minio_secure"),
        ("knowledge_system_settings", "minio_secret_nonce"),
        ("knowledge_system_settings", "minio_secret_ciphertext"),
        ("knowledge_system_settings", "summary_model_name"),
        ("knowledge_system_settings", "query_cache_enabled"),
        ("knowledge_system_settings", "query_cache_max_entries"),
        ("knowledge_system_settings", "query_cache_ttl_seconds"),
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
