#!/usr/bin/env python3
# ruff: noqa: E402
"""Write a closed independent-review record for one exact M8 candidate."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pydantic import ValidationError

from scripts.release_acceptance.contracts import canonical_json_bytes
from scripts.release_acceptance.models import (
    M8_REVIEW_BASE_COMMIT,
    AcceptanceStatus,
    AwaitingReview,
    ReleaseEvidence,
    ReviewReport,
)


class ReviewReportError(RuntimeError):
    """The requested report does not safely bind one exact candidate."""


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _evidence_path(repository: Path, path: Path, *, kind: str) -> Path:
    repository = repository.resolve()
    evidence_root = repository / ".release-evidence"
    absolute = path if path.is_absolute() else repository / path
    lexical = Path(os.path.abspath(absolute))
    if not _inside(evidence_root, lexical):
        raise ReviewReportError(f"{kind}_OUTSIDE_EVIDENCE")
    try:
        root_info = os.lstat(evidence_root)
    except FileNotFoundError:
        raise ReviewReportError("EVIDENCE_ROOT_UNSAFE") from None
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise ReviewReportError("EVIDENCE_ROOT_UNSAFE")
    try:
        parent = lexical.parent.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise ReviewReportError(f"{kind}_PARENT_UNSAFE") from None
    if not _inside(evidence_root.resolve(), parent):
        raise ReviewReportError(f"{kind}_OUTSIDE_EVIDENCE")
    return lexical


def _read_candidate(repository: Path, manifest: Path) -> ReleaseEvidence:
    path = _evidence_path(repository, manifest, kind="CANDIDATE_MANIFEST")
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ReviewReportError("CANDIDATE_MANIFEST_UNSAFE")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise ReviewReportError("CANDIDATE_MANIFEST_UNSAFE")
            data = os.read(descriptor, 2 * 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        raise ReviewReportError("CANDIDATE_MANIFEST_UNSAFE") from None
    if len(data) > 2 * 1024 * 1024:
        raise ReviewReportError("CANDIDATE_MANIFEST_UNSAFE")
    try:
        candidate = ReleaseEvidence.model_validate_json(data)
    except ValidationError:
        raise ReviewReportError("CANDIDATE_MANIFEST_INVALID") from None
    if candidate.status is not AcceptanceStatus.CANDIDATE_READY or not isinstance(candidate.review, AwaitingReview):
        raise ReviewReportError("CANDIDATE_MANIFEST_INVALID")
    return candidate


def load_review_report(repository: Path, report_path: Path) -> ReviewReport:
    """Load one safe report and prove it was written beside its exact candidate."""

    repository = repository.resolve()
    path = _evidence_path(repository, report_path, kind="REVIEW_REPORT")
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ReviewReportError("REVIEW_REPORT_UNSAFE")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise ReviewReportError("REVIEW_REPORT_UNSAFE")
            data = os.read(descriptor, 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        raise ReviewReportError("REVIEW_REPORT_UNSAFE") from None
    if len(data) > 1024 * 1024:
        raise ReviewReportError("REVIEW_REPORT_UNSAFE")
    try:
        report = ReviewReport.model_validate_json(data)
    except ValidationError:
        raise ReviewReportError("REVIEW_REPORT_INVALID") from None
    candidate = _read_candidate(repository, path.with_name("manifest.json"))
    if report.candidate_commit != candidate.git_commit or report.stage_manifest_digest != candidate.stage_manifest_digest or report.candidate_evidence_digest != candidate.candidate_evidence_digest:
        raise ReviewReportError("REVIEW_BINDING_MISMATCH")
    return report


def _git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ("git", *args),
            cwd=repository,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise ReviewReportError("REVIEW_GIT_IDENTITY_INVALID") from None


def _resolve_review_identity(
    repository: Path,
    *,
    candidate: ReleaseEvidence,
    review_base: str,
    review_range: str,
    required_review_base: str | None = None,
) -> tuple[str, str]:
    if review_range.count("..") != 1:
        raise ReviewReportError("REVIEW_RANGE_MISMATCH")
    left, right = review_range.split("..", 1)
    try:
        base_commit = _git(repository, "rev-parse", "--verify", f"{review_base}^{{commit}}").stdout.strip()
        left_commit = _git(repository, "rev-parse", "--verify", f"{left}^{{commit}}").stdout.strip()
        right_commit = _git(repository, "rev-parse", "--verify", f"{right}^{{commit}}").stdout.strip()
        head_commit = _git(repository, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    except ReviewReportError:
        raise ReviewReportError("REVIEW_RANGE_MISMATCH") from None
    if base_commit != left_commit or right_commit != candidate.git_commit or head_commit != candidate.git_commit or (required_review_base is not None and base_commit != required_review_base):
        raise ReviewReportError("REVIEW_RANGE_MISMATCH")
    ancestor = _git(
        repository,
        "merge-base",
        "--is-ancestor",
        base_commit,
        candidate.git_commit,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ReviewReportError("REVIEW_RANGE_MISMATCH")
    return base_commit, f"{base_commit}..{candidate.git_commit}"


def _write_new_report(repository: Path, output: Path, report: ReviewReport) -> None:
    path = _evidence_path(repository, output, kind="REVIEW_OUTPUT")
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise ReviewReportError("REVIEW_OUTPUT_EXISTS")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(canonical_json_bytes(report))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            raise ReviewReportError("REVIEW_OUTPUT_EXISTS") from None
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _relative_evidence_locator(repository: Path, output: Path) -> str:
    repository = repository.resolve()
    return (
        _evidence_path(
            repository,
            output,
            kind="REVIEW_OUTPUT",
        )
        .relative_to(repository)
        .as_posix()
    )


def create_review_report(
    *,
    repository: Path,
    candidate_manifest: Path,
    review_base: str,
    review_range: str,
    critical: int,
    important: int,
    minor: int,
    output: Path,
    required_review_base: str | None = None,
) -> ReviewReport:
    if any(type(value) is not int or value < 0 for value in (critical, important, minor)):
        raise ReviewReportError("REVIEW_FINDING_COUNT_INVALID")
    repository = repository.resolve()
    candidate = _read_candidate(repository, candidate_manifest)
    base_commit, exact_range = _resolve_review_identity(
        repository,
        candidate=candidate,
        review_base=review_base,
        review_range=review_range,
        required_review_base=required_review_base,
    )
    report = ReviewReport.for_candidate(
        candidate,
        review_base_commit=base_commit,
        review_range=exact_range,
        critical=critical,
        important=important,
        minor=minor,
    )
    _write_new_report(repository, output, report)
    return report


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("finding count must be nonnegative")
    return parsed


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-manifest", required=True, type=Path)
    parser.add_argument("--review-base", required=True)
    parser.add_argument("--review-range", required=True)
    parser.add_argument("--critical", required=True, type=_nonnegative)
    parser.add_argument("--important", required=True, type=_nonnegative)
    parser.add_argument("--minor", required=True, type=_nonnegative)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repository = Path(__file__).resolve().parents[2]
    try:
        report = create_review_report(
            repository=repository,
            candidate_manifest=args.candidate_manifest,
            review_base=args.review_base,
            review_range=args.review_range,
            critical=args.critical,
            important=args.important,
            minor=args.minor,
            output=args.output,
            required_review_base=M8_REVIEW_BASE_COMMIT,
        )
    except ReviewReportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    relative = _relative_evidence_locator(repository, args.output)
    print(
        json.dumps(
            {
                "status": report.status,
                "verdict": report.verdict,
                "review_relative_locator": relative,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
