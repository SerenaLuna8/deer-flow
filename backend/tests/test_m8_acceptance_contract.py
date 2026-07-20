from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.release_acceptance.contracts import canonical_digest, contract_digest, schema_bytes
from scripts.release_acceptance.models import (
    STAGE_ORDER,
    AcceptanceStatus,
    ReleaseEvidence,
    ReviewBindingError,
    ReviewReport,
    StageEvidence,
    StageId,
)
from scripts.release_acceptance.models import (
    TestSummary as AcceptanceTestSummary,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _REPO_ROOT / "contracts"
_EXPECTED_MATRIX_DIMENSIONS = {
    "actors": [
        "unauthenticated",
        "project_outsider",
        "admin",
        "editor",
        "runner",
        "viewer",
        "different_owner",
        "different_project",
        "different_account",
        "removed_membership",
        "left_membership",
        "stale_membership",
        "pending_deletion_project",
        "suspended_project",
        "system_admin_with_membership",
        "system_admin_without_membership",
        "ordinary_platform_user",
    ],
    "account_relationships": ["unauthenticated", "same_account", "different_account"],
    "project_relationships": ["none", "same_project", "different_project", "project_outsider"],
    "membership_states": [
        "none",
        "active_admin",
        "active_editor",
        "active_runner",
        "active_viewer",
        "removed",
        "left",
        "stale_version",
        "pending_deletion",
        "suspended",
    ],
    "platform_roles": ["user", "system_admin"],
    "resource_families": [
        "auth",
        "project",
        "membership",
        "invite",
        "lifecycle",
        "agent",
        "skill",
        "mcp",
        "version",
        "binding",
        "credential",
        "thread",
        "message",
        "run",
        "run_event",
        "checkpoint",
        "file",
        "artifact",
        "memory",
        "connection",
        "automation",
        "occurrence",
        "result",
        "job",
        "dead_job",
        "quota",
        "usage",
        "audit",
        "retention",
        "admin",
        "channel",
        "archive",
        "journal",
        "restore_proof",
    ],
    "scopes": ["account", "workspace", "project_shared", "project_private", "project_governance", "system_governance", "recovery"],
    "ownerships": ["not_applicable", "own", "other_owner", "server_owned"],
    "operations": [
        "create",
        "list",
        "search",
        "page",
        "get",
        "export",
        "update",
        "delete",
        "publish",
        "bind",
        "approve",
        "run",
        "stop",
        "stream",
        "reconnect",
        "manual",
        "automatic",
        "retry",
        "requeue",
        "restore",
        "purge",
    ],
    "layers": ["frontend", "api", "service", "repository", "database", "worker", "scheduler"],
}


def _stage(stage: StageId = StageId.CONTRACTS) -> StageEvidence:
    started = datetime(2026, 7, 20, 1, 2, 3, tzinfo=UTC)
    return StageEvidence(
        stage=stage,
        command_id="contracts.schemas",
        status="passed",
        started_at=started,
        finished_at=started + timedelta(milliseconds=5),
        duration_ms=5,
        passed=1,
        failed=0,
        skipped=0,
        summary=AcceptanceTestSummary(kind="tests", collected=1, passed=1, failed=0, skipped=0),
    )


def _candidate() -> ReleaseEvidence:
    return ReleaseEvidence.candidate(
        acceptance_run_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        git_commit="a" * 40,
        stage_manifest_digest="b" * 64,
        stages=(_stage(),),
    )


def _review(candidate: ReleaseEvidence, *, critical: int = 0, important: int = 0, minor: int = 0) -> ReviewReport:
    return ReviewReport.for_candidate(
        candidate,
        review_base_commit="c" * 40,
        critical=critical,
        important=important,
        minor=minor,
    )


def test_stage_manifest_is_closed_ordered_and_recursively_frozen() -> None:
    assert [stage.value for stage in STAGE_ORDER] == [
        "preflight",
        "contracts",
        "postgres",
        "backend",
        "frontend",
        "security",
        "host_setup",
        "chromium",
        "deepseek",
        "recovery",
        "cleanup",
    ]
    with pytest.raises(ValidationError):
        StageEvidence.model_validate({**_stage().model_dump(), "stdout": "must-not-persist"})
    with pytest.raises(ValidationError):
        AcceptanceTestSummary.model_validate({**_stage().summary.model_dump(), "detail": "must-not-persist"})
    with pytest.raises(ValidationError):
        _stage().duration_ms = 99


def test_stage_requires_matching_closed_summary_kind() -> None:
    payload = _stage().model_dump()
    payload["stage"] = "security"
    with pytest.raises(ValidationError, match="summary kind does not match stage"):
        StageEvidence.model_validate(payload)


def test_candidate_and_final_transitions_require_exact_review_binding() -> None:
    candidate = _candidate()
    assert candidate.status is AcceptanceStatus.CANDIDATE_READY
    assert candidate.review.status == "awaiting_review"
    assert len(candidate.candidate_evidence_digest) == 64

    report = _review(candidate)
    final = ReleaseEvidence.final(candidate=candidate, review=report)
    assert final.status is AcceptanceStatus.FINAL_PASS
    assert final.review.verdict == "passed"

    mismatched = report.model_copy(update={"candidate_evidence_digest": "0" * 64})
    with pytest.raises(ReviewBindingError, match="REVIEW_BINDING_MISMATCH"):
        ReleaseEvidence.final(candidate=candidate, review=mismatched)
    with pytest.raises(ReviewBindingError, match="REVIEW_FINDINGS_PRESENT"):
        ReleaseEvidence.final(candidate=candidate, review=_review(candidate, minor=1))


def test_candidate_digest_is_stable_and_changes_with_binding_fields() -> None:
    candidate = _candidate()
    same = ReleaseEvidence.candidate(
        acceptance_run_id=candidate.acceptance_run_id,
        git_commit=candidate.git_commit,
        stage_manifest_digest=candidate.stage_manifest_digest,
        stages=candidate.stages,
    )
    changed = ReleaseEvidence.candidate(
        acceptance_run_id=candidate.acceptance_run_id,
        git_commit="d" * 40,
        stage_manifest_digest=candidate.stage_manifest_digest,
        stages=candidate.stages,
    )
    assert same.candidate_evidence_digest == candidate.candidate_evidence_digest
    assert changed.candidate_evidence_digest != candidate.candidate_evidence_digest


def test_canonical_digest_uses_sorted_compact_utf8_json(tmp_path: Path) -> None:
    value = {"z": "中文", "a": [2, 1]}
    expected = canonical_digest(value)
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    assert contract_digest(path) == expected
    assert expected == canonical_digest({"a": [2, 1], "z": "中文"})


@pytest.mark.parametrize(
    ("model", "filename"),
    [
        (ReleaseEvidence, "m8_release_evidence.schema.json"),
        (ReviewReport, "m8_review_report.schema.json"),
    ],
)
def test_committed_json_schemas_match_models_byte_for_byte(model: type[object], filename: str) -> None:
    assert (_CONTRACTS / filename).read_bytes() == schema_bytes(model)


def test_committed_contract_authorities_are_closed() -> None:
    matrix = json.loads((_CONTRACTS / "m8_isolation_matrix.json").read_text(encoding="utf-8"))
    allowlist = json.loads((_CONTRACTS / "m8_secret_allowlist.json").read_text(encoding="utf-8"))
    assert matrix["schema_version"] == 1
    assert matrix["dimensions"] == _EXPECTED_MATRIX_DIMENSIONS
    assert matrix["cases"]
    assert set(matrix) == {"schema_version", "dimensions", "surface_manifest", "cases"}
    assert set(matrix["surface_manifest"]) == {"count", "sha256"}
    assert allowlist == {"schema_version": 1, "entries": []}
