from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import deerflow.persistence.models  # noqa: F401 -- populate metadata
from deerflow.persistence.base import Base
from deerflow.persistence.context_evidence import ContextEvidenceAppend
from deerflow.runtime.context_evidence import (
    ContextEvidence,
    ContextSubject,
    ContextWindowGeneration,
    WindowOpenedV1,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def test_context_evidence_relations_are_registered_with_private_thread_ownership() -> None:
    expected = {
        "context_evidence_sequences",
        "context_evidence",
        "context_projection_heads",
    }

    assert expected <= set(Base.metadata.tables)

    evidence = Base.metadata.tables["context_evidence"]
    foreign_targets = {element.target_fullname for constraint in evidence.foreign_key_constraints for element in constraint.elements}
    assert {
        "context_evidence_sequences.project_id",
        "context_evidence_sequences.owner_user_id",
        "context_evidence_sequences.thread_id",
    } <= foreign_targets
    assert all(not target.startswith("runs.") for target in foreign_targets)


def test_schema_v1_installs_the_append_only_ledger_and_mutable_projection_head() -> None:
    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")

    for table in (
        "context_evidence_sequences",
        "context_evidence",
        "context_projection_heads",
    ):
        assert f"CREATE TABLE {table} (" in schema

    assert "CREATE OR REPLACE FUNCTION enforce_context_evidence_append_only()" in schema
    assert ("CREATE TRIGGER trg_context_evidence_append_only BEFORE UPDATE OR DELETE ON context_evidence") in " ".join(schema.split())
    assert "fk_context_evidence_sequence" in schema
    assert "fk_context_projection_heads_sequence" in schema
    assert "FOREIGN KEY (project_id, owner_user_id, thread_id, origin_run_id)" not in schema


def test_persistence_adapter_consumes_the_core_safe_evidence_contract() -> None:
    thread_id = str(uuid.uuid4())
    generation_id = uuid.uuid4()
    evidence = ContextEvidence(
        evidence_seq="7",
        subject=ContextSubject.lead_thread(thread_id=thread_id),
        generation=ContextWindowGeneration(generation_id=generation_id),
        origin_run_id=None,
        occurred_at=datetime(2026, 8, 27, tzinfo=UTC),
        payload=WindowOpenedV1(
            model_identity_digest="a" * 64,
            context_window_tokens=300_000,
            compaction_enabled=True,
            compaction_threshold_tokens=240_000,
            compaction_authority="frozen_run",
        ),
    )

    command, evidence_seq = ContextEvidenceAppend.from_safe_contract(evidence)

    assert evidence_seq == 7
    assert command.subject.subject_id == thread_id
    assert command.context_window_generation == generation_id
    assert command.event_type == "context.window.opened.v1"
    assert command.idempotency_key == evidence.idempotency_key
    assert command.payload == evidence.to_safe_mapping()["payload"]
