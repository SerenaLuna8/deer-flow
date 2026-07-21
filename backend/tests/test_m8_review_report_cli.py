from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.create_m8_review_report import (
    ReviewReportError,
    _relative_evidence_locator,
    create_review_report,
)
from scripts.release_acceptance.models import (
    ReleaseEvidence,
    ReviewBindingError,
    ReviewReport,
    StageEvidence,
    StageId,
)
from scripts.release_acceptance.models import (
    TestSummary as AcceptanceTestSummary,
)


def test_documented_review_cli_bootstraps_backend_imports() -> None:
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        (sys.executable, "scripts/create_m8_review_report.py", "--help"),
        cwd=backend,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--candidate-manifest" in result.stdout


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "m8@example.invalid")
    _git(repository, "config", "user.name", "M8 Test")
    (repository / ".gitignore").write_text("/.release-evidence/\n", encoding="utf-8")
    tracked = repository / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".gitignore", "tracked.txt")
    _git(repository, "commit", "-qm", "base")
    base = _git(repository, "rev-parse", "HEAD")
    tracked.write_text("candidate\n", encoding="utf-8")
    _git(repository, "commit", "-qam", "candidate")
    return repository, base, _git(repository, "rev-parse", "HEAD")


def _candidate(repository: Path, commit: str) -> tuple[ReleaseEvidence, Path]:
    started = datetime(2026, 7, 21, tzinfo=UTC)
    stage = StageEvidence(
        stage=StageId.CONTRACTS,
        command_id="contracts.schemas",
        status="passed",
        started_at=started,
        finished_at=started + timedelta(milliseconds=1),
        duration_ms=1,
        passed=1,
        failed=0,
        skipped=0,
        summary=AcceptanceTestSummary(collected=1, passed=1, failed=0, skipped=0),
    )
    candidate = ReleaseEvidence.candidate(
        acceptance_run_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        git_commit=commit,
        stage_manifest_digest="b" * 64,
        stages=(stage,),
    )
    manifest = repository / ".release-evidence" / str(candidate.acceptance_run_id) / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(candidate.model_dump_json(), encoding="utf-8")
    return candidate, manifest


def test_report_binds_exact_candidate_commit_manifest_evidence_and_range(tmp_path: Path) -> None:
    repository, base, commit = _repository(tmp_path)
    candidate, manifest = _candidate(repository, commit)
    output = manifest.with_name("review.json")

    report = create_review_report(
        repository=repository,
        candidate_manifest=manifest,
        review_base=base[:12],
        review_range=f"{base[:12]}..HEAD",
        critical=0,
        important=0,
        minor=0,
        output=output,
    )

    loaded = ReviewReport.model_validate_json(output.read_text(encoding="utf-8"))
    assert loaded == report
    assert report.candidate_commit == candidate.git_commit
    assert report.stage_manifest_digest == candidate.stage_manifest_digest
    assert report.candidate_evidence_digest == candidate.candidate_evidence_digest
    assert report.review_base_commit == base
    assert report.review_range == f"{base}..{commit}"
    assert report.verdict == "passed"
    assert output.stat().st_mode & 0o777 == 0o600


def test_nonzero_report_is_valid_record_but_cannot_unlock_final(tmp_path: Path) -> None:
    repository, base, commit = _repository(tmp_path)
    candidate, manifest = _candidate(repository, commit)
    report = create_review_report(
        repository=repository,
        candidate_manifest=manifest,
        review_base=base,
        review_range=f"{base}..{commit}",
        critical=0,
        important=0,
        minor=1,
        output=manifest.with_name("review.json"),
    )
    assert report.verdict == "findings_present"
    with pytest.raises(ReviewBindingError, match="REVIEW_FINDINGS_PRESENT"):
        ReleaseEvidence.final(candidate=candidate, review=report)


def test_formal_report_creation_rejects_a_shorter_review_baseline(
    tmp_path: Path,
) -> None:
    repository, base, commit = _repository(tmp_path)
    _candidate_value, manifest = _candidate(repository, commit)

    with pytest.raises(ReviewReportError, match="REVIEW_RANGE_MISMATCH"):
        create_review_report(
            repository=repository,
            candidate_manifest=manifest,
            review_base=base,
            review_range=f"{base}..{commit}",
            critical=0,
            important=0,
            minor=0,
            output=manifest.with_name("review.json"),
            required_review_base="f" * 40,
        )


@pytest.mark.parametrize("wrong_range", ("HEAD..HEAD", "base..candidate", ""))
def test_report_rejects_non_exact_review_range_without_writing(tmp_path: Path, wrong_range: str) -> None:
    repository, base, commit = _repository(tmp_path)
    _candidate_value, manifest = _candidate(repository, commit)
    output = manifest.with_name("review.json")
    with pytest.raises(ReviewReportError, match="REVIEW_RANGE_MISMATCH"):
        create_review_report(
            repository=repository,
            candidate_manifest=manifest,
            review_base=base,
            review_range=wrong_range,
            critical=0,
            important=0,
            minor=0,
            output=output,
        )
    assert not output.exists()


def test_report_rejects_outside_symlink_and_overwrite_targets(tmp_path: Path) -> None:
    repository, base, commit = _repository(tmp_path)
    _candidate_value, manifest = _candidate(repository, commit)
    arguments = dict(
        repository=repository,
        candidate_manifest=manifest,
        review_base=base,
        review_range=f"{base}..{commit}",
        critical=0,
        important=0,
        minor=0,
    )
    with pytest.raises(ReviewReportError, match="REVIEW_OUTPUT_OUTSIDE_EVIDENCE"):
        create_review_report(**arguments, output=tmp_path / "outside.json")

    target = manifest.with_name("target.json")
    target.write_text("preserve", encoding="utf-8")
    link = manifest.with_name("review.json")
    link.symlink_to(target)
    with pytest.raises(ReviewReportError, match="REVIEW_OUTPUT_EXISTS"):
        create_review_report(**arguments, output=link)
    assert target.read_text(encoding="utf-8") == "preserve"
    link.unlink()

    output = manifest.with_name("review.json")
    output.write_text(json.dumps({"preserve": True}), encoding="utf-8")
    with pytest.raises(ReviewReportError, match="REVIEW_OUTPUT_EXISTS"):
        create_review_report(**arguments, output=output)
    assert json.loads(output.read_text(encoding="utf-8")) == {"preserve": True}


def test_candidate_manifest_must_be_inside_evidence_and_cannot_be_symlink(tmp_path: Path) -> None:
    repository, base, commit = _repository(tmp_path)
    candidate, manifest = _candidate(repository, commit)
    outside = tmp_path / "candidate.json"
    outside.write_text(candidate.model_dump_json(), encoding="utf-8")
    with pytest.raises(ReviewReportError, match="CANDIDATE_MANIFEST_OUTSIDE_EVIDENCE"):
        create_review_report(
            repository=repository,
            candidate_manifest=outside,
            review_base=base,
            review_range=f"{base}..{commit}",
            critical=0,
            important=0,
            minor=0,
            output=manifest.with_name("review.json"),
        )

    original = manifest.with_name("original.json")
    os.replace(manifest, original)
    manifest.symlink_to(original)
    with pytest.raises(ReviewReportError, match="CANDIDATE_MANIFEST_UNSAFE"):
        create_review_report(
            repository=repository,
            candidate_manifest=manifest,
            review_base=base,
            review_range=f"{base}..{commit}",
            critical=0,
            important=0,
            minor=0,
            output=manifest.with_name("review.json"),
        )


def test_relative_report_locator_is_resolved_from_repository_not_shell_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, _base, commit = _repository(tmp_path)
    _candidate_value, manifest = _candidate(repository, commit)
    monkeypatch.chdir(repository / ".git")

    assert (
        _relative_evidence_locator(
            repository,
            manifest.parent.relative_to(repository) / "review.json",
        )
        == manifest.with_name("review.json").relative_to(repository).as_posix()
    )
