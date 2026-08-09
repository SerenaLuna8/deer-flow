from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from sqlalchemy.dialects.postgresql import dialect as postgres_dialect
from sqlalchemy.schema import AddConstraint

import deerflow.persistence.models  # noqa: F401
from app.audit.models import JobAuditMetadata
from app.final_schema import FINAL_REQUIRED_RELATIONS
from app.gateway.routers.admin_jobs import AdminJobResponse
from app.reliability.operations import JobType as OperationsJobType
from deerflow.persistence.base import Base
from deerflow.persistence.bootstrap import CURRENT_SCHEMA_REVISION
from deerflow.persistence.final_schema_contract import (
    FINAL_APP_SEQUENCES,
)
from deerflow.persistence.jobs.sql import JobType

MEMORY_DOCUMENT_TABLES = {
    "memory_history_entries",
    "memory_documents",
    "memory_dream_runs",
    "memory_document_versions",
    "memory_episodes",
    "run_memory_context_snapshots",
}

REMOVED_MEMORY_TABLES = {
    "user_project_memories",
    "user_project_memory_facts",
    "memory_source_batches",
    "memory_source_items",
    "memory_extraction_generations",
    "memory_consolidation_generations",
    "memory_candidates",
    "memory_facts",
    "memory_fact_revisions",
    "memory_fact_evidence",
    "memory_context_summaries",
    "memory_suppressions",
    "run_memory_context_items",
}


def test_final_memory_schema_uses_only_the_document_model() -> None:
    assert CURRENT_SCHEMA_REVISION == "full_schema_v9"
    assert MEMORY_DOCUMENT_TABLES <= set(Base.metadata.tables)
    assert REMOVED_MEMORY_TABLES.isdisjoint(Base.metadata.tables)
    assert MEMORY_DOCUMENT_TABLES <= set(FINAL_REQUIRED_RELATIONS)
    assert REMOVED_MEMORY_TABLES.isdisjoint(FINAL_REQUIRED_RELATIONS)


def test_full_schema_contains_only_the_final_memory_tables_and_job() -> None:
    schema_sql = (Path(__file__).resolve().parents[1] / "packages/harness/deerflow/persistence/full_schema.sql").read_text(encoding="utf-8")

    for table in MEMORY_DOCUMENT_TABLES:
        assert f"CREATE TABLE {table} (" in schema_sql
    for table in REMOVED_MEMORY_TABLES:
        assert f"CREATE TABLE {table} (" not in schema_sql

    assert "full_schema_v9" in schema_sql
    assert "full_schema_v4" not in schema_sql
    assert "memory_dream" in schema_sql
    assert "memory_seal" in schema_sql
    assert schema_sql.count("CREATE EXTENSION IF NOT EXISTS pg_trgm;") == 1
    assert "USING gin (tagged_text gin_trgm_ops)" in schema_sql
    assert "memory_retention_cutoff_at" not in schema_sql
    for removed_job in (
        "memory_extract",
        "memory_consolidate",
        "memory_retention_purge",
    ):
        assert removed_job not in schema_sql

    sql_tables = set(
        re.findall(r"^CREATE TABLE ([a-z0-9_]+) \(", schema_sql, re.MULTILINE),
    )
    assert sql_tables == set(Base.metadata.tables) | {"alembic_version"}


def test_deferred_dream_result_fk_matches_the_orm() -> None:
    schema_sql = (Path(__file__).resolve().parents[1] / "packages/harness/deerflow/persistence/full_schema.sql").read_text(encoding="utf-8")
    constraint = next(value for value in Base.metadata.tables["memory_dream_runs"].constraints if value.name == "fk_memory_dream_runs_result_version")
    expected = " ".join(
        str(AddConstraint(constraint).compile(dialect=postgres_dialect())).split(),
    )
    assert expected in " ".join(schema_sql.split())


def test_memory_document_sections_have_frozen_policy_provenance() -> None:
    schema_sql = (Path(__file__).resolve().parents[1] / "packages/harness/deerflow/persistence/full_schema.sql").read_text(encoding="utf-8")
    document = Base.metadata.tables["memory_documents"]
    snapshot = Base.metadata.tables["run_memory_context_snapshots"]
    provenance = next(constraint for constraint in document.foreign_key_constraints if constraint.name == "fk_memory_documents_sections_policy_version")

    assert document.c.sections.nullable is False
    assert document.c.sections_policy_section.nullable is False
    assert document.c.sections_policy_version_id.nullable is False
    assert tuple(element.parent.name for element in provenance.elements) == (
        "sections_policy_section",
        "sections_policy_version_id",
    )
    assert tuple(element.target_fullname for element in provenance.elements) == (
        "system_runtime_policy_versions.section",
        "system_runtime_policy_versions.id",
    )
    assert snapshot.c.sections.nullable is False
    assert "jsonb_path_exists(sections" in schema_sql
    assert "sections_policy_section = 'memory_document'" in schema_sql
    assert "CREATE OR REPLACE FUNCTION prevent_memory_document_sections_mutation()" in schema_sql
    assert "CREATE TRIGGER trg_memory_documents_sections_immutable" in schema_sql
    assert "CREATE TRIGGER trg_run_memory_context_snapshots_sections_immutable" in schema_sql


def test_tool_history_origin_pins_the_remember_prompt_contract() -> None:
    schema_sql = (Path(__file__).resolve().parents[1] / "packages/harness/deerflow/persistence/full_schema.sql").read_text(encoding="utf-8")
    constraint = next(value for value in Base.metadata.tables["memory_history_entries"].constraints if value.name == "ck_memory_history_entries_contract")
    expected = " ".join(
        str(AddConstraint(constraint).compile(dialect=postgres_dialect())).split(),
    )
    create_table_fragment = expected.removeprefix(
        "ALTER TABLE memory_history_entries ADD ",
    )

    assert "origin = 'tool'" in expected
    assert "snip_prompt_version = 'remember-tool-v1'" in expected
    assert create_table_fragment in " ".join(schema_sql.split())


def test_job_type_contract_is_consistent_across_persistence_and_public_models() -> None:
    expected = {
        "private_run",
        "automation_run",
        "retention_purge",
        "mcp_discovery",
        "memory_dream",
        "memory_seal",
    }
    contracts = (
        JobType,
        OperationsJobType,
        AdminJobResponse.model_fields["job_type"].annotation,
        JobAuditMetadata.model_fields["job_type"].annotation,
    )

    for contract in contracts:
        assert set(get_args(contract)) == expected


def test_history_identity_sequence_is_part_of_the_canonical_catalog() -> None:
    assert (
        "memory_history_entries_sequence_seq",
        "memory_history_entries",
    ) in FINAL_APP_SEQUENCES
