from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from detect_secrets.core.scan import _process_line_based_plugins
from detect_secrets.settings import default_settings
from pydantic import BaseModel, ConfigDict, Field, model_validator

SecretScope = Literal["tracked_tree", "review_diff", "git_history", "evidence", "support_bundle", "runtime_logs"]
ReasonKind = Literal["test_fake", "documentation_placeholder"]

REQUIRED_THREAT_FAMILIES = (
    "scoped_identifier_authority",
    "stale_runtime_authority",
    "credential_secret_containment",
    "web_session_boundary",
    "file_archive_sandbox_boundary",
    "durable_stream_boundary",
    "automation_scheduler_boundary",
    "recovery_integrity",
    "system_governance_observability",
)

_SHA256 = r"^[0-9a-f]{64}$"
_RULE = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_THREAT_ID = r"^M8-T[0-9]{2}$"
_SELECTOR = re.compile(r"^(?:pytest::tests/[A-Za-z0-9_./-]+\.py::[A-Za-z_][A-Za-z0-9_]*|playwright::tests/e2e/[A-Za-z0-9_./-]+\.spec\.ts::[^\r\n]+)$")
_CUSTOM_RULES = (
    (
        "PrivateKey",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]+?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
        ),
    ),
    ("ProviderToken", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    (
        "DatabaseURL",
        re.compile(r"\b(?:postgres(?:ql)?(?:\+[A-Za-z0-9_]+)?|mysql|redis|mongodb(?:\+srv)?)://[^\s\"']+:[^@\s\"']+@[^\s\"']+"),
    ),
    ("BearerToken", re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/-]{20,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b")),
    ("Cookie", re.compile(r"(?i)\b(?:session(?:_cookie)?|cookie)\s*[:=]\s*[A-Za-z0-9._~+/-]{20,}")),
    ("NonceCiphertext", re.compile(r"(?i)\b(?:nonce|ciphertext)\s*[:=]\s*[A-Fa-f0-9+/=_-]{32,}")),
)
_PLUGIN_PREFILTER = re.compile(
    r"(?i)(?:"
    r"AKIA|ASIA|AccountKey|"
    r"gh[pousr]_|github_pat_|glpat-|npm_|pypi-|"
    r"sk-|xox[a-z]-|SG\.|sq0|rk_live_|sk_live_|AIza|"
    r"(?:api|access|private|secret)[_-]?(?:key|token)"
    r")"
)


class SecurityContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ThreatControl(SecurityContract):
    threat_id: str = Field(pattern=_THREAT_ID)
    family: str
    attack_surface: str = Field(min_length=1, max_length=512)
    prevention_controls: tuple[str, ...] = Field(min_length=1)
    detective_controls: tuple[str, ...] = Field(min_length=1)
    matrix_case_ids: tuple[str, ...] = Field(min_length=1)
    test_selectors: tuple[str, ...] = Field(min_length=1)
    operator_response: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_closed_values(self) -> Self:
        if self.family not in REQUIRED_THREAT_FAMILIES:
            raise ValueError("unknown threat family")
        for values in (self.prevention_controls, self.detective_controls, self.matrix_case_ids, self.test_selectors):
            if len(set(values)) != len(values):
                raise ValueError("duplicate threat control value")
        if any(_SELECTOR.fullmatch(selector) is None for selector in self.test_selectors):
            raise ValueError("invalid threat evidence selector")
        return self


class ThreatControlCatalog(SecurityContract):
    schema_version: Literal[1] = 1
    items: tuple[ThreatControl, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if len({item.threat_id for item in self.items}) != len(self.items):
            raise ValueError("duplicate threat ID")
        if len({item.family for item in self.items}) != len(self.items):
            raise ValueError("duplicate threat family")
        return self

    def missing_required_families(self) -> tuple[str, ...]:
        present = {item.family for item in self.items}
        return tuple(family for family in REQUIRED_THREAT_FAMILIES if family not in present)


class SecretAllowlistEntry(SecurityContract):
    scope: SecretScope
    path: str = Field(min_length=1, max_length=512)
    rule: str = Field(pattern=_RULE)
    value_sha256: str = Field(pattern=_SHA256)
    reason_kind: ReasonKind

    @model_validator(mode="after")
    def validate_exact_path(self) -> Self:
        path = PurePosixPath(self.path)
        if self.path.startswith("/") or str(path) != self.path or any(part in {"", ".", ".."} for part in path.parts) or any(character in self.path for character in "*?[]\\"):
            raise ValueError("secret allowlist path must be exact and relative")
        return self


class SecretAllowlist(SecurityContract):
    schema_version: Literal[1] = 1
    entries: tuple[SecretAllowlistEntry, ...] = ()

    @model_validator(mode="after")
    def validate_unique_entries(self) -> Self:
        identities = {(entry.scope, entry.path, entry.rule, entry.value_sha256) for entry in self.entries}
        if len(identities) != len(self.entries):
            raise ValueError("duplicate secret allowlist entry")
        return self

    def permits(self, *, scope: SecretScope, path: str, rule: str, digest: str) -> bool:
        return any(entry.scope == scope and entry.path == path and entry.rule == rule and entry.value_sha256 == digest for entry in self.entries)


class SecretFinding(SecurityContract):
    scope: SecretScope
    rule: str = Field(pattern=_RULE)
    locator_digest: str = Field(pattern=_SHA256)
    line: int | None = Field(default=None, ge=1)


class DependencyFinding(SecurityContract):
    ecosystem: Literal["python", "node"]
    advisory_id: str = Field(min_length=1, max_length=128)
    package_name: str = Field(min_length=1, max_length=256)
    locked_version: str = Field(min_length=1, max_length=128)
    database_timestamp: datetime


class DependencyAuditReport(SecurityContract):
    ecosystem: Literal["python", "node"]
    scanned_packages: int = Field(ge=0)
    findings: tuple[DependencyFinding, ...]
    exclusion_ids: tuple[str, ...] = ()
    effective_findings: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.effective_findings != len(self.findings):
            raise ValueError("dependency finding count mismatch")
        if self.exclusion_ids:
            raise ValueError("dependency exclusions require a separate absence proof")
        return self


class SecurityGateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _DetectedSecret:
    rule: str
    value_sha256: str
    line: int | None


def value_digest(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def locator_digest(scope: str, locator: str) -> str:
    return hashlib.sha256(f"m8-secret-locator\0{scope}\0{locator}".encode()).hexdigest()


def load_threat_catalog(path: Path) -> ThreatControlCatalog:
    return ThreatControlCatalog.model_validate_json(path.read_text(encoding="utf-8"))


def load_secret_allowlist(path: Path) -> SecretAllowlist:
    return SecretAllowlist.model_validate_json(path.read_text(encoding="utf-8"))


class SecretScanner:
    def __init__(self, *, allowlist: SecretAllowlist | None = None, max_blob_bytes: int = 8 * 1024 * 1024) -> None:
        if type(max_blob_bytes) is not int or max_blob_bytes < 1:
            raise ValueError("secret scanner blob limit is invalid")
        self._allowlist = allowlist or SecretAllowlist()
        self._max_blob_bytes = max_blob_bytes
        self._settings_active = False
        self._candidate_count = 0
        self._matched_allowlist_entries: set[tuple[SecretScope, str, str, str]] = set()
        self._scanned_allowlist_paths: dict[SecretScope, set[str]] = {}

    @property
    def candidate_count(self) -> int:
        return self._candidate_count

    @staticmethod
    def _line_number(value: str, start: int) -> int:
        return value.count("\n", 0, start) + 1

    @contextmanager
    def _detection_session(self):
        if self._settings_active:
            yield
            return
        with default_settings() as settings:
            settings.disable_plugins(
                "Base64HighEntropyString",
                "BasicAuthDetector",
                "HexHighEntropyString",
                "IPPublicDetector",
                "JwtTokenDetector",
                "KeywordDetector",
            )
            self._settings_active = True
            try:
                yield
            finally:
                self._settings_active = False

    def _detect(self, data: bytes, *, filename: str = "m8-in-memory") -> tuple[_DetectedSecret, ...]:
        text = data.decode("utf-8", errors="replace")
        detected: dict[tuple[str, str, int | None], _DetectedSecret] = {}
        custom_digests_by_line: dict[int, set[str]] = {}
        for rule, pattern in _CUSTOM_RULES:
            for match in pattern.finditer(text):
                line = self._line_number(text, match.start())
                secret = match.group(0)
                item = _DetectedSecret(rule=rule, value_sha256=value_digest(secret), line=line)
                custom_digests_by_line.setdefault(line, set()).add(item.value_sha256)
                detected[(item.rule, item.value_sha256, item.line)] = item
        if _PLUGIN_PREFILTER.search(text) is None:
            return tuple(detected[key] for key in sorted(detected))
        with self._detection_session():
            lines = list(enumerate(text.splitlines(), start=1))
            for secret in _process_line_based_plugins(lines=lines, filename=filename):
                digest = value_digest(secret.secret_value)
                if digest in custom_digests_by_line.get(secret.line_number, set()):
                    continue
                item = _DetectedSecret(
                    rule=re.sub(r"[^A-Za-z0-9_.-]", "_", secret.type)[:128] or "DetectSecrets",
                    value_sha256=digest,
                    line=secret.line_number,
                )
                detected[(item.rule, item.value_sha256, item.line)] = item
        return tuple(detected[key] for key in sorted(detected))

    def _finding(self, *, scope: SecretScope, locator: str, rule: str, line: int | None) -> SecretFinding:
        coordinate = f"{locator}\0{rule}\0{line or 0}"
        return SecretFinding(scope=scope, rule=rule, locator_digest=locator_digest(scope, coordinate), line=line)

    def scan_bytes(
        self,
        data: bytes,
        *,
        scope: SecretScope,
        locator: str,
        allowlist_path: str | None = None,
        allowlist_paths: Sequence[str] | None = None,
    ) -> tuple[SecretFinding, ...]:
        if not isinstance(data, bytes):
            raise TypeError("secret scanner requires bytes")
        if allowlist_path is not None and allowlist_paths is not None:
            raise ValueError("secret scanner received conflicting allowlist paths")
        paths = tuple(allowlist_paths or ((allowlist_path or locator),))
        findings = []
        detected_items = self._detect(data, filename=paths[0])
        self._candidate_count += len(detected_items)
        for detected in detected_items:
            matched_path = next(
                (
                    path
                    for path in paths
                    if self._allowlist.permits(
                        scope=scope,
                        path=path,
                        rule=detected.rule,
                        digest=detected.value_sha256,
                    )
                ),
                None,
            )
            if matched_path is not None:
                self._matched_allowlist_entries.add((scope, matched_path, detected.rule, detected.value_sha256))
                continue
            findings.append(self._finding(scope=scope, locator=locator, rule=detected.rule, line=detected.line))
        return tuple(findings)

    def unused_allowlist_findings(self, scopes: Iterable[SecretScope]) -> tuple[SecretFinding, ...]:
        selected = set(scopes)
        return tuple(
            self._finding(
                scope=entry.scope,
                locator=f"{entry.path}\0{entry.rule}\0{entry.value_sha256}",
                rule="UNUSED_ALLOWLIST_ENTRY",
                line=None,
            )
            for entry in self._allowlist.entries
            if entry.scope in selected
            and not (entry.scope == "review_diff" and entry.path not in self._scanned_allowlist_paths.get("review_diff", set()))
            and (entry.scope, entry.path, entry.rule, entry.value_sha256) not in self._matched_allowlist_entries
        )

    def scan_file(self, path: Path, *, scope: SecretScope, relative_path: str) -> tuple[SecretFinding, ...]:
        try:
            before = path.lstat()
        except OSError:
            return (self._finding(scope=scope, locator=relative_path, rule="UNSCANNED_NON_REGULAR", line=None),)
        if not stat.S_ISREG(before.st_mode):
            return (self._finding(scope=scope, locator=relative_path, rule="UNSCANNED_NON_REGULAR", line=None),)
        if before.st_size > self._max_blob_bytes:
            return (self._finding(scope=scope, locator=relative_path, rule="UNSCANNED_OVERSIZED_BLOB", line=None),)
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise OSError
            if opened.st_size > self._max_blob_bytes:
                os.close(descriptor)
                descriptor = -1
                return (
                    self._finding(
                        scope=scope,
                        locator=relative_path,
                        rule="UNSCANNED_OVERSIZED_BLOB",
                        line=None,
                    ),
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(self._max_blob_bytes + 1)
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            return (self._finding(scope=scope, locator=relative_path, rule="UNSCANNED_NON_REGULAR", line=None),)
        if len(data) > self._max_blob_bytes:
            del data
            return (self._finding(scope=scope, locator=relative_path, rule="UNSCANNED_OVERSIZED_BLOB", line=None),)
        try:
            return self.scan_bytes(data, scope=scope, locator=relative_path, allowlist_path=relative_path)
        finally:
            del data

    def scan_directory(self, root: Path, *, scope: Literal["evidence", "support_bundle"]) -> tuple[SecretFinding, ...]:
        findings: list[SecretFinding] = []
        if not root.is_dir() or root.is_symlink():
            return (self._finding(scope=scope, locator=root.name or ".", rule="UNSCANNED_NON_REGULAR", line=None),)
        with self._detection_session():
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                if path.is_dir() and not path.is_symlink():
                    continue
                findings.extend(self.scan_file(path, scope=scope, relative_path=relative))
        return tuple(findings)

    def scan_runtime_log(self, path: Path, *, start_offset: int, relative_path: str) -> tuple[SecretFinding, ...]:
        descriptor = -1
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError
            if type(start_offset) is not int or start_offset < 0 or start_offset > opened.st_size or opened.st_size - start_offset > self._max_blob_bytes:
                raise ValueError
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                handle.seek(start_offset)
                data = handle.read(self._max_blob_bytes + 1)
            if len(data) > self._max_blob_bytes:
                raise ValueError
        except (OSError, ValueError):
            if descriptor >= 0:
                os.close(descriptor)
            return (self._finding(scope="runtime_logs", locator=relative_path, rule="UNSCANNED_RUNTIME_LOG_RANGE", line=None),)
        try:
            return self.scan_bytes(data, scope="runtime_logs", locator=f"{relative_path}:{start_offset}", allowlist_path=relative_path)
        finally:
            del data

    @staticmethod
    def _git(repository: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
        try:
            result = subprocess.run(
                ("git", *args),
                cwd=repository,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            raise SecurityGateError("GIT_SECRET_SCAN_FAILED") from None
        return result.stdout

    def scan_tracked_tree(self, repository: Path) -> tuple[SecretFinding, ...]:
        findings: list[SecretFinding] = []
        paths = (item for item in self._git(repository, "ls-files", "-z").split(b"\0") if item)
        with self._detection_session():
            for encoded in paths:
                relative = encoded.decode("utf-8", errors="surrogateescape")
                findings.extend(self.scan_file(repository / relative, scope="tracked_tree", relative_path=relative))
        return tuple(findings)

    def scan_review_diff(self, repository: Path, review_base: str) -> tuple[SecretFinding, ...]:
        if re.fullmatch(r"[0-9a-f]{7,64}", review_base) is None:
            raise SecurityGateError("REVIEW_BASE_INVALID")
        encoded_paths = self._git(
            repository,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"{review_base}..HEAD",
            "--",
        )
        findings: list[SecretFinding] = []
        with self._detection_session():
            for encoded in sorted({item for item in encoded_paths.split(b"\0") if item}):
                relative = encoded.decode("utf-8", errors="surrogateescape")
                if relative.startswith("/") or ".." in PurePosixPath(relative).parts:
                    findings.append(
                        self._finding(
                            scope="review_diff",
                            locator=relative,
                            rule="UNSCANNED_NON_REGULAR",
                            line=None,
                        )
                    )
                    continue
                self._scanned_allowlist_paths.setdefault("review_diff", set()).add(relative)
                revision = "HEAD" if (repository / relative).is_file() and not (repository / relative).is_symlink() else review_base
                try:
                    data = self._git(repository, "show", f"{revision}:{relative}")
                except SecurityGateError:
                    findings.append(
                        self._finding(
                            scope="review_diff",
                            locator=relative,
                            rule="UNSCANNED_NON_REGULAR",
                            line=None,
                        )
                    )
                    continue
                locator = f"{revision}:{relative}"
                if len(data) > self._max_blob_bytes:
                    findings.append(
                        self._finding(
                            scope="review_diff",
                            locator=locator,
                            rule="UNSCANNED_OVERSIZED_BLOB",
                            line=None,
                        )
                    )
                else:
                    try:
                        findings.extend(
                            self.scan_bytes(
                                data,
                                scope="review_diff",
                                locator=locator,
                                allowlist_path=relative,
                            )
                        )
                    finally:
                        del data
        return tuple(findings)

    @staticmethod
    def _read_exact(stream, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            block = stream.read(min(64 * 1024, size - len(result)))
            if not block:
                raise SecurityGateError("GIT_SECRET_SCAN_FAILED")
            result.extend(block)
        return bytes(result)

    @staticmethod
    def _discard_exact(stream, size: int) -> None:
        remaining = size
        while remaining:
            block = stream.read(min(64 * 1024, remaining))
            if not block:
                raise SecurityGateError("GIT_SECRET_SCAN_FAILED")
            remaining -= len(block)

    def scan_git_history(self, repository: Path) -> tuple[SecretFinding, ...]:
        objects = self._git(repository, "rev-list", "--objects", "--all").splitlines()
        paths_by_oid: dict[str, set[str]] = {}
        for row in objects:
            oid_bytes, separator, path_bytes = row.partition(b" ")
            oid = oid_bytes.decode("ascii")
            path = path_bytes.decode("utf-8", errors="surrogateescape") if separator else f"object/{oid}"
            paths_by_oid.setdefault(oid, set()).add(path)
        if not paths_by_oid:
            return ()
        check_input = "".join(f"{oid}\n" for oid in paths_by_oid).encode("ascii")
        metadata = self._git(repository, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)", input_bytes=check_input)
        blobs: list[tuple[str, int]] = []
        for row in metadata.splitlines():
            oid, object_type, size = row.decode("ascii").split()
            if object_type == "blob":
                blobs.append((oid, int(size)))

        findings: list[SecretFinding] = []
        try:
            process = subprocess.Popen(
                ("git", "cat-file", "--batch"),
                cwd=repository,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise SecurityGateError("GIT_SECRET_SCAN_FAILED") from None
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise SecurityGateError("GIT_SECRET_SCAN_FAILED")
        try:
            with self._detection_session():
                for oid, expected_size in blobs:
                    process.stdin.write(f"{oid}\n".encode("ascii"))
                    process.stdin.flush()
                    header = process.stdout.readline().decode("ascii").strip().split()
                    if len(header) != 3 or header[0] != oid or header[1] != "blob" or int(header[2]) != expected_size:
                        raise SecurityGateError("GIT_SECRET_SCAN_FAILED")
                    allowlist_paths = tuple(sorted(paths_by_oid[oid]))
                    path = allowlist_paths[0]
                    locator = f"{oid}:{path}"
                    if expected_size > self._max_blob_bytes:
                        self._discard_exact(process.stdout, expected_size)
                        findings.append(
                            self._finding(
                                scope="git_history",
                                locator=locator,
                                rule="UNSCANNED_OVERSIZED_BLOB",
                                line=None,
                            )
                        )
                    else:
                        data = self._read_exact(process.stdout, expected_size)
                        try:
                            findings.extend(
                                self.scan_bytes(
                                    data,
                                    scope="git_history",
                                    locator=locator,
                                    allowlist_paths=allowlist_paths,
                                )
                            )
                        finally:
                            del data
                    if process.stdout.read(1) != b"\n":
                        raise SecurityGateError("GIT_SECRET_SCAN_FAILED")
            process.stdin.close()
            if process.wait(timeout=10) != 0:
                raise SecurityGateError("GIT_SECRET_SCAN_FAILED")
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            process.kill()
            process.wait()
            raise SecurityGateError("GIT_SECRET_SCAN_FAILED") from None
        except BaseException:
            process.kill()
            process.wait()
            raise
        return tuple(findings)


class BackendDependencyAuditor:
    def __init__(self, backend_root: Path) -> None:
        self._backend_root = backend_root

    def run(self) -> DependencyAuditReport:
        scanned_at = datetime.now(UTC)
        with tempfile.TemporaryDirectory(prefix="deerflow-m8-audit-") as directory:
            requirements = Path(directory) / "requirements.txt"
            try:
                subprocess.run(
                    (
                        "uv",
                        "export",
                        "--locked",
                        "--no-dev",
                        "--no-emit-workspace",
                        "--format",
                        "requirements.txt",
                        "--output-file",
                        str(requirements),
                    ),
                    cwd=self._backend_root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )
                audited = subprocess.run(
                    (str(self._backend_root / ".venv" / "bin" / "pip-audit"), "--requirement", str(requirements), "--format", "json", "--disable-pip"),
                    cwd=self._backend_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                raise SecurityGateError("BACKEND_DEPENDENCY_AUDIT_FAILED") from None
        try:
            payload = json.loads(audited.stdout)
            dependencies = payload.get("dependencies", []) if isinstance(payload, dict) else payload
            findings = tuple(
                DependencyFinding(
                    ecosystem="python",
                    advisory_id=str(vulnerability["id"]),
                    package_name=str(dependency["name"]),
                    locked_version=str(dependency["version"]),
                    database_timestamp=scanned_at,
                )
                for dependency in dependencies
                for vulnerability in dependency.get("vulns", [])
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise SecurityGateError("BACKEND_DEPENDENCY_AUDIT_FAILED") from None
        if audited.returncode not in {0, 1}:
            raise SecurityGateError("BACKEND_DEPENDENCY_AUDIT_FAILED")
        return DependencyAuditReport(
            ecosystem="python",
            scanned_packages=len(dependencies),
            findings=findings,
            effective_findings=len(findings),
        )


class FrontendDependencyAuditor:
    def __init__(self, frontend_root: Path) -> None:
        self._frontend_root = frontend_root

    def run(self) -> DependencyAuditReport:
        scanned_at = datetime.now(UTC)
        try:
            audited = subprocess.run(
                ("pnpm", "audit", "--prod", "--audit-level", "low", "--json"),
                cwd=self._frontend_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            raise SecurityGateError("FRONTEND_DEPENDENCY_AUDIT_FAILED") from None
        try:
            payload = json.loads(audited.stdout)
            metadata = payload["metadata"]
            scanned_packages = int(metadata["dependencies"])
            advisories = payload.get("advisories", {})
            findings = tuple(
                DependencyFinding(
                    ecosystem="node",
                    advisory_id=str(advisory_id),
                    package_name=str(advisory["module_name"]),
                    locked_version=str(item["version"]),
                    database_timestamp=scanned_at,
                )
                for advisory_id, advisory in sorted(advisories.items())
                for item in advisory.get("findings", [])
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise SecurityGateError("FRONTEND_DEPENDENCY_AUDIT_FAILED") from None
        if audited.returncode not in {0, 1}:
            raise SecurityGateError("FRONTEND_DEPENDENCY_AUDIT_FAILED")
        return DependencyAuditReport(
            ecosystem="node",
            scanned_packages=scanned_packages,
            findings=findings,
            effective_findings=len(findings),
        )


def _security_result(findings: Iterable[SecretFinding], *, scanned: int) -> dict[str, object]:
    items = tuple(findings)
    return {
        "schema_version": 1,
        "scanned": scanned,
        "effective_findings": len(items),
        "findings": [item.model_dump(mode="json") for item in items],
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded M8 security gates")
    parser.add_argument(
        "--scope",
        action="append",
        required=True,
        choices=("dependencies-backend", "dependencies-frontend", "tracked-tree", "git-history", "review-diff"),
    )
    parser.add_argument("--review-base")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    backend_root = Path(__file__).resolve().parents[2]
    repository_root = backend_root.parent
    allowlist = load_secret_allowlist(repository_root / "contracts" / "m8_secret_allowlist.json")
    scanner = SecretScanner(allowlist=allowlist)
    effective = 0
    outputs: list[dict[str, object]] = []
    scanned_secret_scopes: set[SecretScope] = set()
    for scope in args.scope:
        if scope == "dependencies-backend":
            report = BackendDependencyAuditor(backend_root).run()
            outputs.append(report.model_dump(mode="json"))
            effective += report.effective_findings
        elif scope == "dependencies-frontend":
            report = FrontendDependencyAuditor(repository_root / "frontend").run()
            outputs.append(report.model_dump(mode="json"))
            effective += report.effective_findings
        elif scope == "tracked-tree":
            scanned_secret_scopes.add("tracked_tree")
            before = scanner.candidate_count
            result = _security_result(
                scanner.scan_tracked_tree(repository_root),
                scanned=scanner.candidate_count - before,
            )
            outputs.append(result)
            effective += int(result["effective_findings"])
        elif scope == "git-history":
            scanned_secret_scopes.add("git_history")
            before = scanner.candidate_count
            result = _security_result(
                scanner.scan_git_history(repository_root),
                scanned=scanner.candidate_count - before,
            )
            outputs.append(result)
            effective += int(result["effective_findings"])
        else:
            scanned_secret_scopes.add("review_diff")
            if not args.review_base:
                raise SystemExit("--review-base is required for review-diff")
            before = scanner.candidate_count
            result = _security_result(
                scanner.scan_review_diff(repository_root, args.review_base),
                scanned=scanner.candidate_count - before,
            )
            outputs.append(result)
            effective += int(result["effective_findings"])
    unused_allowlist = scanner.unused_allowlist_findings(scanned_secret_scopes)
    if unused_allowlist:
        result = _security_result(unused_allowlist, scanned=0)
        outputs.append(result)
        effective += len(unused_allowlist)
    print(json.dumps({"schema_version": 1, "effective_findings": effective, "results": outputs}, sort_keys=True))
    return 1 if effective else 0


if __name__ == "__main__":
    raise SystemExit(main())
