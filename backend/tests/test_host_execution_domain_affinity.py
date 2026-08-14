from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from deerflow.config.sandbox_config import SandboxConfig
from deerflow.runtime.host_execution_domain import (
    HostExecutionDomainSnapshot,
    host_execution_environment_fingerprint,
    validate_execution_domain_affinity,
)


def _snapshot(**overrides: object) -> HostExecutionDomainSnapshot:
    values: dict[str, object] = {
        "configured_id": "mac-primary",
        "public_label": "My Mac",
        "os_name": "posix",
        "sys_platform": "darwin",
        "machine": "arm64",
        "device_fingerprint": "d" * 64,
        "environment_fingerprint": "f" * 64,
        "euid": 501,
        "egid": 20,
        "runtime_base_dir": str(Path("/private/tmp/actweave").resolve()),
    }
    values.update(overrides)
    return HostExecutionDomainSnapshot(**values)


def test_local_approval_requires_stable_execution_domain_id() -> None:
    with pytest.raises(ValidationError, match="execution_domain_id"):
        SandboxConfig.model_validate(
            {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
                "host_execution_approval": {"mode": "approval_required"},
            },
        )


def test_isolated_provider_does_not_require_host_execution_domain() -> None:
    config = SandboxConfig.model_validate(
        {
            "use": "deerflow.sandbox.aio:AioSandboxProvider",
            "host_execution_approval": {"mode": "disabled"},
        },
    )

    assert config.host_execution_approval.execution_domain_id is None


@pytest.mark.parametrize(
    "label",
    [" hidden", "hidden ", "line\nbreak", "\x7f", "safe\u202eevil"],
)
def test_public_execution_domain_label_is_bounded_safe_text(label: str) -> None:
    with pytest.raises(ValidationError):
        SandboxConfig.model_validate(
            {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
                "host_execution_approval": {
                    "mode": "approval_required",
                    "execution_domain_id": "mac-primary",
                    "execution_domain_label": label,
                },
            },
        )


def test_private_domain_payload_is_strict_and_self_authenticating() -> None:
    snapshot = _snapshot()
    payload = snapshot.to_private_payload()

    assert HostExecutionDomainSnapshot.from_private_payload(payload) == snapshot
    assert payload["affinity"] == snapshot.affinity
    assert snapshot.configured_id not in repr(snapshot)
    assert snapshot.runtime_base_dir not in repr(snapshot)

    payload["euid"] = 502
    with pytest.raises(ValueError, match="affinity mismatch"):
        HostExecutionDomainSnapshot.from_private_payload(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("configured_id", "mac-secondary"),
        ("os_name", "nt"),
        ("sys_platform", "linux"),
        ("machine", "x86_64"),
        ("device_fingerprint", "e" * 64),
        ("environment_fingerprint", "a" * 64),
        ("euid", 502),
        ("egid", 21),
        ("runtime_base_dir", "/private/tmp/other"),
    ],
)
def test_every_execution_surface_coordinate_changes_affinity(
    field: str,
    value: object,
) -> None:
    baseline = _snapshot()
    changed = _snapshot(**{field: value})

    assert changed.affinity != baseline.affinity


def test_public_label_does_not_change_private_affinity() -> None:
    assert (
        _snapshot(public_label="Primary Mac").affinity
        == _snapshot(
            public_label="Worker host environment",
        ).affinity
    )


def test_sanitized_environment_fingerprint_is_canonical_and_value_sensitive() -> None:
    assert host_execution_environment_fingerprint(
        {"PATH": "/usr/bin", "LANG": "C"},
    ) == host_execution_environment_fingerprint(
        {"LANG": "C", "PATH": "/usr/bin"},
    )
    assert host_execution_environment_fingerprint(
        {"PATH": "/usr/bin", "LANG": "C"},
    ) != host_execution_environment_fingerprint(
        {"PATH": "/opt/bin", "LANG": "C"},
    )


def test_execution_domain_affinity_parser_is_closed() -> None:
    digest = _snapshot().affinity
    assert validate_execution_domain_affinity(digest) == digest
    assert validate_execution_domain_affinity(None) is None
    for invalid in ("", "A" * 64, "0" * 63, 1):
        with pytest.raises(ValueError):
            validate_execution_domain_affinity(invalid)  # type: ignore[arg-type]
