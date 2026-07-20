from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_COMMAND_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class ReviewBindingError(RuntimeError):
    """An independent review does not bind the exact candidate evidence."""


class StageId(StrEnum):
    PREFLIGHT = "preflight"
    CONTRACTS = "contracts"
    POSTGRES = "postgres"
    BACKEND = "backend"
    FRONTEND = "frontend"
    SECURITY = "security"
    HOST_SETUP = "host_setup"
    CHROMIUM = "chromium"
    DEEPSEEK = "deepseek"
    RECOVERY = "recovery"
    CLEANUP = "cleanup"


STAGE_ORDER = tuple(StageId)
_STAGE_INDEX = {stage: index for index, stage in enumerate(STAGE_ORDER)}


class AcceptanceStatus(StrEnum):
    FAILED = "failed"
    CANDIDATE_READY = "candidate_ready"
    FINAL_PASS = "final_pass"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TestSummary(StrictModel):
    kind: Literal["tests"] = "tests"
    collected: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)


class SecuritySummary(StrictModel):
    kind: Literal["security"] = "security"
    scanned: int = Field(ge=0)
    effective_findings: int = Field(ge=0)


class LiveModelSummary(StrictModel):
    kind: Literal["live_model"] = "live_model"
    provider: Literal["deepseek"]
    logical_model_name: str = Field(min_length=1, max_length=128)
    provider_model_id: Literal["deepseek-v4-pro"]
    outcome: Literal["completed", "provider_rejected", "failed"]
    frame_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    terminal_count: int = Field(ge=0)
    cursor_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)


class RecoverySummary(StrictModel):
    kind: Literal["recovery"] = "recovery"
    archive_schema_version: Literal[7]
    schema_revision: Literal["0001_project_saas_baseline"]
    tombstone_count: int = Field(ge=0)
    rto_ms: int = Field(ge=0)
    rpo_outcome: Literal["archive_point_confirmed", "failed"]
    restored_count: int = Field(ge=0)


class CleanupSummary(StrictModel):
    kind: Literal["cleanup"] = "cleanup"
    residual_processes: int = Field(ge=0)
    residual_ports: int = Field(ge=0)
    residual_databases: int = Field(ge=0)
    residual_paths: int = Field(ge=0)
    retained_evidence: int = Field(ge=0)


StageSummary = Annotated[TestSummary | SecuritySummary | LiveModelSummary | RecoverySummary | CleanupSummary, Field(discriminator="kind")]


class StageEvidence(StrictModel):
    stage: StageId
    command_id: str = Field(pattern=_COMMAND_ID.pattern, max_length=128)
    status: Literal["passed", "failed"]
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    summary: StageSummary

    @model_validator(mode="after")
    def validate_stage_contract(self) -> Self:
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None or self.finished_at < self.started_at:
            raise ValueError("invalid stage timestamps")
        elapsed_ms = int((self.finished_at - self.started_at).total_seconds() * 1000)
        if abs(elapsed_ms - self.duration_ms) > 1:
            raise ValueError("stage duration does not match timestamps")
        expected_kind = {
            StageId.SECURITY: "security",
            StageId.DEEPSEEK: "live_model",
            StageId.RECOVERY: "recovery",
            StageId.CLEANUP: "cleanup",
        }.get(self.stage, "tests")
        if self.summary.kind != expected_kind:
            raise ValueError("summary kind does not match stage")
        if isinstance(self.summary, TestSummary) and (self.passed, self.failed, self.skipped) != (self.summary.passed, self.summary.failed, self.summary.skipped):
            raise ValueError("stage counts do not match summary")
        if self.status == "passed" and self.failed:
            raise ValueError("passed stage cannot contain failures")
        if self.status == "failed" and not self.failed:
            raise ValueError("failed stage must contain a failure")
        return self


class AwaitingReview(StrictModel):
    status: Literal["awaiting_review"] = "awaiting_review"


class ReviewReport(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["reviewed"] = "reviewed"
    verdict: Literal["passed", "findings_present"]
    candidate_commit: str = Field(pattern=_GIT_COMMIT.pattern)
    stage_manifest_digest: str = Field(pattern=_SHA256.pattern)
    candidate_evidence_digest: str = Field(pattern=_SHA256.pattern)
    review_base_commit: str = Field(pattern=_GIT_COMMIT.pattern)
    critical: int = Field(ge=0)
    important: int = Field(ge=0)
    minor: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_verdict(self) -> Self:
        expected = "passed" if self.critical == self.important == self.minor == 0 else "findings_present"
        if self.verdict != expected:
            raise ValueError("review verdict does not match finding counts")
        return self

    @classmethod
    def for_candidate(
        cls,
        candidate: ReleaseEvidence,
        *,
        review_base_commit: str,
        critical: int,
        important: int,
        minor: int,
    ) -> ReviewReport:
        return cls(
            verdict="passed" if critical == important == minor == 0 else "findings_present",
            candidate_commit=candidate.git_commit,
            stage_manifest_digest=candidate.stage_manifest_digest,
            candidate_evidence_digest=candidate.candidate_evidence_digest,
            review_base_commit=review_base_commit,
            critical=critical,
            important=important,
            minor=minor,
        )


ReviewState = Annotated[AwaitingReview | ReviewReport, Field(discriminator="status")]


class ReleaseEvidence(StrictModel):
    schema_version: Literal[1] = 1
    acceptance_run_id: uuid.UUID
    status: AcceptanceStatus
    git_commit: str = Field(pattern=_GIT_COMMIT.pattern)
    stage_manifest_digest: str = Field(pattern=_SHA256.pattern)
    candidate_evidence_digest: str = Field(pattern=_SHA256.pattern)
    stages: tuple[StageEvidence, ...] = Field(min_length=1, max_length=len(STAGE_ORDER))
    review: ReviewState

    @model_validator(mode="after")
    def validate_release_contract(self) -> Self:
        stage_indexes = [_STAGE_INDEX[item.stage] for item in self.stages]
        if stage_indexes != sorted(set(stage_indexes)):
            raise ValueError("release stages are duplicated or out of order")
        expected_digest = _candidate_binding_digest(
            acceptance_run_id=self.acceptance_run_id,
            git_commit=self.git_commit,
            stage_manifest_digest=self.stage_manifest_digest,
            stages=self.stages,
        )
        if self.candidate_evidence_digest != expected_digest:
            raise ValueError("candidate evidence digest mismatch")
        if self.status is AcceptanceStatus.CANDIDATE_READY and not isinstance(self.review, AwaitingReview):
            raise ValueError("candidate must await review")
        if self.status is AcceptanceStatus.FINAL_PASS:
            if not isinstance(self.review, ReviewReport):
                raise ValueError("final pass requires review report")
            _assert_review_binding(self, self.review)
        return self

    @classmethod
    def candidate(
        cls,
        *,
        acceptance_run_id: uuid.UUID,
        git_commit: str,
        stage_manifest_digest: str,
        stages: tuple[StageEvidence, ...],
    ) -> ReleaseEvidence:
        digest = _candidate_binding_digest(
            acceptance_run_id=acceptance_run_id,
            git_commit=git_commit,
            stage_manifest_digest=stage_manifest_digest,
            stages=stages,
        )
        return cls(
            acceptance_run_id=acceptance_run_id,
            status=AcceptanceStatus.CANDIDATE_READY,
            git_commit=git_commit,
            stage_manifest_digest=stage_manifest_digest,
            candidate_evidence_digest=digest,
            stages=stages,
            review=AwaitingReview(),
        )

    @classmethod
    def final(cls, *, candidate: ReleaseEvidence, review: ReviewReport) -> ReleaseEvidence:
        if candidate.status is not AcceptanceStatus.CANDIDATE_READY or not isinstance(candidate.review, AwaitingReview):
            raise ReviewBindingError("REVIEW_CANDIDATE_INVALID")
        _assert_review_binding(candidate, review)
        return cls(
            acceptance_run_id=candidate.acceptance_run_id,
            status=AcceptanceStatus.FINAL_PASS,
            git_commit=candidate.git_commit,
            stage_manifest_digest=candidate.stage_manifest_digest,
            candidate_evidence_digest=candidate.candidate_evidence_digest,
            stages=candidate.stages,
            review=review,
        )


def _candidate_binding_digest(
    *,
    acceptance_run_id: uuid.UUID,
    git_commit: str,
    stage_manifest_digest: str,
    stages: tuple[StageEvidence, ...],
) -> str:
    payload = {
        "schema_version": 1,
        "acceptance_run_id": str(acceptance_run_id),
        "git_commit": git_commit,
        "stage_manifest_digest": stage_manifest_digest,
        "stages": [stage.model_dump(mode="json") for stage in stages],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_review_binding(candidate: ReleaseEvidence, review: ReviewReport) -> None:
    if review.candidate_commit != candidate.git_commit or review.stage_manifest_digest != candidate.stage_manifest_digest or review.candidate_evidence_digest != candidate.candidate_evidence_digest:
        raise ReviewBindingError("REVIEW_BINDING_MISMATCH")
    if review.verdict != "passed" or review.critical or review.important or review.minor:
        raise ReviewBindingError("REVIEW_FINDINGS_PRESENT")
