from __future__ import annotations

import json
import stat
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from scripts.release_acceptance.contracts import load_isolation_matrix
from scripts.release_acceptance.security import (
    BackendDependencyAuditor,
    FrontendDependencyAuditor,
    SecretAllowlist,
    SecretAllowlistEntry,
    SecretScanner,
    load_threat_catalog,
    value_digest,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MATRIX = _REPO_ROOT / "contracts" / "m8_isolation_matrix.json"
_THREATS = _REPO_ROOT / "contracts" / "m8_threat_controls.json"


def _provider_token() -> str:
    return "sk" + "-" + "a" * 48


def _private_key() -> str:
    return "-----BEGIN " + "PRIVATE KEY-----\n" + "b" * 80 + "\n-----END " + "PRIVATE KEY-----"


def _database_url() -> str:
    return "postgresql" + "://user:generated-password@database.invalid/app"


def _jwt() -> str:
    return ".".join(("eyJ" + "e" * 21, "f" * 32, "g" * 40))


def test_every_threat_has_preventive_detective_and_executable_evidence() -> None:
    threats = load_threat_catalog(_THREATS)
    matrix = load_isolation_matrix(_MATRIX)
    assert threats.missing_required_families() == ()
    for threat in threats.items:
        assert threat.prevention_controls
        assert threat.detective_controls
        assert set(threat.matrix_case_ids) <= matrix.case_ids
        assert set(threat.test_selectors) <= {*matrix.pytest_selectors(), *matrix.playwright_selectors()}


def test_secret_finding_never_contains_match_value(tmp_path: Path) -> None:
    secret = _provider_token()
    secret_file = tmp_path / "fixture.txt"
    secret_file.write_text(f"provider_key={secret}\n", encoding="utf-8")
    finding = SecretScanner().scan_file(secret_file, scope="support_bundle", relative_path="bundle/fixture.txt")[0]
    encoded = finding.model_dump_json()
    assert secret not in encoded
    assert finding.locator_digest
    assert set(finding.model_dump()) == {"scope", "rule", "locator_digest", "line"}


def test_allowlist_requires_exact_path_rule_and_digest() -> None:
    with pytest.raises(ValidationError):
        SecretAllowlistEntry(
            scope="tracked_tree",
            path="backend/tests/**",
            rule="*",
            value_sha256="*",
            reason_kind="test_fake",
        )


def test_exact_allowlist_matches_only_one_scope_path_rule_and_value(tmp_path: Path) -> None:
    secret = _provider_token()
    path = tmp_path / "fixture.txt"
    path.write_text(secret, encoding="utf-8")
    entry = SecretAllowlistEntry(
        scope="tracked_tree",
        path="backend/tests/fixture.txt",
        rule="ProviderToken",
        value_sha256=value_digest(secret),
        reason_kind="test_fake",
    )
    scanner = SecretScanner(allowlist=SecretAllowlist(entries=(entry,)))
    assert scanner.scan_file(path, scope="tracked_tree", relative_path="backend/tests/fixture.txt") == ()
    assert scanner.candidate_count == 1
    findings = scanner.scan_file(path, scope="support_bundle", relative_path="backend/tests/fixture.txt")
    assert len(findings) == 1
    assert findings[0].rule == "ProviderToken"


def test_unused_allowlist_entry_fails_closed_without_echoing_identity(tmp_path: Path) -> None:
    secret = _provider_token()
    path = tmp_path / "fixture.txt"
    path.write_text(secret, encoding="utf-8")
    used = SecretAllowlistEntry(
        scope="tracked_tree",
        path="backend/tests/fixture.txt",
        rule="ProviderToken",
        value_sha256=value_digest(secret),
        reason_kind="test_fake",
    )
    unused = used.model_copy(update={"path": "backend/tests/old-fixture.txt"})
    scanner = SecretScanner(allowlist=SecretAllowlist(entries=(used, unused)))
    assert scanner.scan_file(path, scope="tracked_tree", relative_path="backend/tests/fixture.txt") == ()
    findings = scanner.unused_allowlist_findings(("tracked_tree",))
    assert len(findings) == 1
    assert findings[0].rule == "UNUSED_ALLOWLIST_ENTRY"
    encoded = findings[0].model_dump_json()
    assert "old-fixture" not in encoded and secret not in encoded


@pytest.mark.parametrize(
    ("rule", "value"),
    (
        ("PrivateKey", _private_key()),
        ("ProviderToken", _provider_token()),
        ("DatabaseURL", _database_url()),
        ("JWT", _jwt()),
        ("Cookie", "session" + "_cookie=" + "c" * 48),
        ("NonceCiphertext", "cipher" + "text=" + "d" * 64),
        ("AWS_Access_Key", "AK" + "IA" + "A" * 16),
    ),
)
def test_secret_shapes_are_detected_without_value_echo(rule: str, value: str) -> None:
    findings = SecretScanner().scan_bytes(
        ("prefix\x00" + value).encode(),
        scope="evidence",
        locator="generated/result.bin",
    )
    assert rule in {finding.rule for finding in findings}
    assert value not in json.dumps([finding.model_dump(mode="json") for finding in findings])


def test_two_different_secret_shapes_on_one_line_are_both_detected() -> None:
    provider = _provider_token()
    aws = "AK" + "IA" + "B" * 16
    findings = SecretScanner().scan_bytes(
        f"provider={provider} cloud={aws}".encode(),
        scope="evidence",
        locator="generated/two-secrets.txt",
    )
    assert {"ProviderToken", "AWS_Access_Key"} <= {finding.rule for finding in findings}
    encoded = json.dumps([finding.model_dump(mode="json") for finding in findings])
    assert provider not in encoded and aws not in encoded


def test_directory_scans_reject_symlinks_and_cover_evidence_and_support_bundle(tmp_path: Path) -> None:
    evidence = tmp_path / ".release-evidence"
    support = tmp_path / "support"
    evidence.mkdir()
    support.mkdir()
    (evidence / "result.json").write_text(_jwt(), encoding="utf-8")
    (support / "bundle.txt").write_text(_database_url(), encoding="utf-8")
    (support / "escape").symlink_to(evidence / "result.json")
    scanner = SecretScanner()
    evidence_findings = scanner.scan_directory(evidence, scope="evidence")
    support_findings = scanner.scan_directory(support, scope="support_bundle")
    assert "JWT" in {finding.rule for finding in evidence_findings}
    assert {"DatabaseURL", "UNSCANNED_NON_REGULAR"} <= {finding.rule for finding in support_findings}


def test_support_bundle_zip_scans_members_and_rejects_archive_symlinks(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "support-bundle.zip"
    link = zipfile.ZipInfo("escape")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("nested/result.txt", _provider_token())
        archive.writestr(link, "nested/result.txt")

    findings = SecretScanner().scan_zip_archive(
        archive_path,
        relative_path="support-bundle.zip",
    )

    assert {"ProviderToken", "UNSCANNED_NON_REGULAR"} <= {finding.rule for finding in findings}
    assert _provider_token() not in json.dumps([finding.model_dump(mode="json") for finding in findings])


def test_runtime_log_scan_reads_only_bytes_appended_after_start_offset(tmp_path: Path) -> None:
    log = tmp_path / "gateway.log"
    before = _provider_token()
    after = _jwt()
    log.write_text(before, encoding="utf-8")
    offset = log.stat().st_size
    with log.open("a", encoding="utf-8") as handle:
        handle.write("\n" + after)
    findings = SecretScanner().scan_runtime_log(log, start_offset=offset, relative_path="logs/gateway.log")
    assert {finding.rule for finding in findings} == {"JWT"}
    encoded = json.dumps([finding.model_dump(mode="json") for finding in findings])
    assert before not in encoded and after not in encoded


def test_runtime_log_scan_rejects_symlink_identity(tmp_path: Path) -> None:
    target = tmp_path / "gateway.log"
    target.write_text(_jwt(), encoding="utf-8")
    link = tmp_path / "current.log"
    link.symlink_to(target)
    findings = SecretScanner().scan_runtime_log(link, start_offset=0, relative_path="logs/current.log")
    assert len(findings) == 1
    assert findings[0].rule == "UNSCANNED_RUNTIME_LOG_RANGE"


def test_git_history_scans_deleted_and_renamed_binary_blob_without_checkout(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "m8@example.invalid"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "M8 Test"], cwd=repository, check=True)
    old = repository / "old.bin"
    old.write_bytes(b"\x00" + _provider_token().encode())
    subprocess.run(["git", "add", "old.bin"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repository, check=True)
    old.rename(repository / "renamed.bin")
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "rename"], cwd=repository, check=True)
    (repository / "renamed.bin").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "delete"], cwd=repository, check=True)

    findings = SecretScanner().scan_git_history(repository)
    assert "ProviderToken" in {finding.rule for finding in findings}
    assert _provider_token() not in json.dumps([finding.model_dump(mode="json") for finding in findings])


def test_review_diff_scans_changed_blob_with_exact_path_allowlist(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "m8@example.invalid"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "M8 Test"], cwd=repository, check=True)
    fixture = repository / "fixture.txt"
    fixture.write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "fixture.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    review_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    secret = _provider_token()
    fixture.write_text(secret, encoding="utf-8")
    subprocess.run(["git", "add", "fixture.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "candidate"], cwd=repository, check=True)

    assert {finding.rule for finding in SecretScanner().scan_review_diff(repository, review_base)} == {"ProviderToken"}
    entry = SecretAllowlistEntry(
        scope="review_diff",
        path="fixture.txt",
        rule="ProviderToken",
        value_sha256=value_digest(secret),
        reason_kind="test_fake",
    )
    unrelated = entry.model_copy(update={"path": "unchanged-fixture.txt"})
    scanner = SecretScanner(allowlist=SecretAllowlist(entries=(entry, unrelated)))
    assert scanner.scan_review_diff(repository, review_base) == ()
    assert scanner.unused_allowlist_findings(("review_diff",)) == ()

    stale = entry.model_copy(update={"value_sha256": "0" * 64})
    stale_scanner = SecretScanner(allowlist=SecretAllowlist(entries=(stale,)))
    assert {finding.rule for finding in stale_scanner.scan_review_diff(repository, review_base)} == {"ProviderToken"}
    assert {finding.rule for finding in stale_scanner.unused_allowlist_findings(("review_diff",))} == {"UNUSED_ALLOWLIST_ENTRY"}


def test_backend_dependency_auditor_exports_locked_no_dev_graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(args: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(tuple(args))
        if args[0] == "uv":
            return SimpleNamespace(returncode=0, stdout=b"")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"dependencies": [{"name": "safe-package", "version": "1.2.3", "vulns": []}]}).encode(),
        )

    monkeypatch.setattr("scripts.release_acceptance.security.subprocess.run", fake_run)
    report = BackendDependencyAuditor(tmp_path).run()
    assert report.scanned_packages == 1
    assert report.effective_findings == 0
    assert report.database_timestamp.tzinfo is not None
    assert report.exclusion_ids == ()
    assert calls[0][:5] == ("uv", "export", "--locked", "--no-dev", "--no-emit-workspace")
    assert "--disable-pip" in calls[1]


def test_frontend_dependency_auditor_reports_closed_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = {
        "metadata": {"dependencies": 7},
        "advisories": {
            "M8-ADVISORY": {
                "module_name": "example-package",
                "findings": [{"version": "2.3.4", "paths": ["root>example-package"]}],
            }
        },
    }

    def fake_run(args: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        assert args == ("pnpm", "audit", "--prod", "--audit-level", "low", "--json")
        return SimpleNamespace(returncode=1, stdout=json.dumps(payload).encode())

    monkeypatch.setattr("scripts.release_acceptance.security.subprocess.run", fake_run)
    report = FrontendDependencyAuditor(tmp_path).run()
    assert report.scanned_packages == 7
    assert report.effective_findings == 1
    assert report.database_timestamp.tzinfo is not None
    assert report.exclusion_ids == ()
    assert report.findings[0].model_dump().keys() == {
        "ecosystem",
        "advisory_id",
        "package_name",
        "locked_version",
        "database_timestamp",
    }


def test_oversized_blob_fails_closed_without_reading_secret(tmp_path: Path) -> None:
    path = tmp_path / "oversized.bin"
    path.write_bytes(b"x" * 17)
    findings = SecretScanner(max_blob_bytes=16).scan_file(path, scope="tracked_tree", relative_path="oversized.bin")
    assert len(findings) == 1
    assert findings[0].rule == "UNSCANNED_OVERSIZED_BLOB"
