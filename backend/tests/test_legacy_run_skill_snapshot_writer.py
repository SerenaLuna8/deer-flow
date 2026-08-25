from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from app.automations.dispatcher import AutomationDispatcher
from app.automations.errors import AutomationUnavailable
from app.gateway.routers.project_assets import raise_asset_domain
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import LegacyAdmissionBusy, PrivateWorkTooLarge
from app.private_work.legacy_run_skill_snapshot_writer import (
    LEGACY_ADMISSION_POLICY,
    RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION,
    LegacyAdmissionConfigurationError,
    LegacyAdmissionEnvelope,
    RunSkillSnapshotWriterReconfigurationError,
    freeze_run_skill_snapshot_writer,
    reset_run_skill_snapshot_writer_for_testing,
)
from app.shared_assets.errors import (
    AssetRunAdmissionBusy,
    AssetRunPayloadTooLarge,
)
from deerflow.config.run_skill_snapshot_config import RunSkillSnapshotConfig

_POLICY_DIGEST = "e01a816a3f20a4ecf088e2f0d37b92ba16634e5969860b900a14924312edb6e8"
_REJECTED_V1_POLICY_DIGEST = "45ca0c752375d148c2907f95fab86cb664942591cdd504e61a2691e58a2da238"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_writer_runtime() -> None:
    reset_run_skill_snapshot_writer_for_testing()
    try:
        yield
    finally:
        reset_run_skill_snapshot_writer_for_testing()


def test_release_policy_has_one_canonical_digest() -> None:
    assert LEGACY_ADMISSION_POLICY.canonical_payload() == {
        "max_codec_working_set_bytes_per_skill": 256 * 1024 * 1024,
        "max_encoded_bytes_per_run": 48 * 1024 * 1024,
        "max_source_bytes_per_skill": 36 * 1024 * 1024,
        "revision": 2,
    }
    assert LEGACY_ADMISSION_POLICY.canonical_digest() == _POLICY_DIGEST


def test_release_policy_accepts_each_exact_ceiling_and_cumulative_boundary() -> None:
    policy = LEGACY_ADMISSION_POLICY

    assert (
        policy.require_admissible(
            (
                LegacyAdmissionEnvelope(
                    source_bytes=policy.max_source_bytes_per_skill,
                    codec_working_set_bytes=(policy.max_codec_working_set_bytes_per_skill),
                    encoded_upper_bound_bytes=(policy.max_encoded_bytes_per_run // 2),
                ),
                LegacyAdmissionEnvelope(
                    source_bytes=0,
                    codec_working_set_bytes=0,
                    encoded_upper_bound_bytes=(policy.max_encoded_bytes_per_run - policy.max_encoded_bytes_per_run // 2),
                ),
            ),
            request_id="policy-at-boundary",
        )
        == policy.max_encoded_bytes_per_run
    )


def test_release_policy_accepts_near_ceiling_without_rounding_drift() -> None:
    policy = LEGACY_ADMISSION_POLICY

    assert policy.require_admissible(
        (
            LegacyAdmissionEnvelope(
                source_bytes=policy.max_source_bytes_per_skill - 1,
                codec_working_set_bytes=(policy.max_codec_working_set_bytes_per_skill - 1),
                encoded_upper_bound_bytes=policy.max_encoded_bytes_per_run - 1,
            ),
        ),
        request_id="policy-near-boundary",
    ) == (policy.max_encoded_bytes_per_run - 1)


@pytest.mark.parametrize(
    "envelopes",
    [
        (
            LegacyAdmissionEnvelope(
                source_bytes=(LEGACY_ADMISSION_POLICY.max_source_bytes_per_skill + 1),
                codec_working_set_bytes=0,
                encoded_upper_bound_bytes=0,
            ),
        ),
        (
            LegacyAdmissionEnvelope(
                source_bytes=0,
                codec_working_set_bytes=(LEGACY_ADMISSION_POLICY.max_codec_working_set_bytes_per_skill + 1),
                encoded_upper_bound_bytes=0,
            ),
        ),
        (
            LegacyAdmissionEnvelope(
                source_bytes=0,
                codec_working_set_bytes=0,
                encoded_upper_bound_bytes=(LEGACY_ADMISSION_POLICY.max_encoded_bytes_per_run),
            ),
            LegacyAdmissionEnvelope(
                source_bytes=0,
                codec_working_set_bytes=0,
                encoded_upper_bound_bytes=1,
            ),
        ),
    ],
)
def test_release_policy_rejects_every_over_ceiling_before_admission(
    envelopes: tuple[LegacyAdmissionEnvelope, ...],
) -> None:
    with pytest.raises(PrivateWorkTooLarge) as caught:
        LEGACY_ADMISSION_POLICY.require_admissible(
            envelopes,
            request_id="policy-over-boundary",
        )

    assert caught.value.request_id == "policy-over-boundary"


def test_writer_mode_defaults_to_v4_and_legacy_requires_release_readback() -> None:
    default = RunSkillSnapshotConfig()
    assert default.writer_mode == "v4_reference"
    assert default.expected_artifact_version is None
    assert default.expected_legacy_policy_digest is None

    with pytest.raises(ValidationError):
        RunSkillSnapshotConfig(writer_mode="legacy_v3")
    with pytest.raises(ValidationError):
        RunSkillSnapshotConfig(
            writer_mode="legacy_v3",
            expected_artifact_version=RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION,
        )
    with pytest.raises(ValidationError):
        RunSkillSnapshotConfig.model_validate({"writer_mode": 3})
    with pytest.raises(ValidationError):
        RunSkillSnapshotConfig(
            expected_artifact_version=(RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION),
            expected_legacy_policy_digest=_POLICY_DIGEST,
        )
    with pytest.raises(ValidationError):
        RunSkillSnapshotConfig.model_validate(
            {
                "writer_mode": "legacy_v3",
                "expected_artifact_version": (RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION),
                "expected_legacy_policy_digest": _POLICY_DIGEST,
                "max_encoded_bytes_per_run": 1,
            }
        )


def test_example_config_keeps_v4_writer_as_the_only_enabled_identity() -> None:
    example_text = (_REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8")
    example = yaml.safe_load(example_text)

    assert example["run_skill_snapshots"] == {"writer_mode": "v4_reference"}
    for documented in (
        example_text,
        (_REPO_ROOT / "README.md").read_text(encoding="utf-8"),
    ):
        assert RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION in documented
        assert _POLICY_DIGEST in documented


def test_legacy_writer_fails_closed_on_mixed_or_missing_release_identity() -> None:
    with pytest.raises(LegacyAdmissionConfigurationError):
        freeze_run_skill_snapshot_writer(
            RunSkillSnapshotConfig(
                writer_mode="legacy_v3",
                expected_artifact_version="run-skill-snapshot-writer-v1",
                expected_legacy_policy_digest=_REJECTED_V1_POLICY_DIGEST,
            )
        )
    with pytest.raises(LegacyAdmissionConfigurationError):
        freeze_run_skill_snapshot_writer(
            RunSkillSnapshotConfig(
                writer_mode="legacy_v3",
                expected_artifact_version="different-artifact",
                expected_legacy_policy_digest=_POLICY_DIGEST,
            )
        )
    with pytest.raises(LegacyAdmissionConfigurationError):
        freeze_run_skill_snapshot_writer(
            RunSkillSnapshotConfig(
                writer_mode="legacy_v3",
                expected_artifact_version=RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION,
                expected_legacy_policy_digest=_REJECTED_V1_POLICY_DIGEST,
            )
        )


def test_writer_mode_is_restart_frozen_and_exposes_secret_free_readback() -> None:
    legacy = RunSkillSnapshotConfig(
        writer_mode="legacy_v3",
        expected_artifact_version=RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION,
        expected_legacy_policy_digest=_POLICY_DIGEST,
    )
    readback = freeze_run_skill_snapshot_writer(legacy)

    assert readback.as_public_dict() == {
        "writer_mode": "legacy_v3",
        "artifact_version": RUN_SKILL_SNAPSHOT_WRITER_ARTIFACT_VERSION,
        "legacy_policy_digest": _POLICY_DIGEST,
        "ready": True,
    }
    assert freeze_run_skill_snapshot_writer(legacy) is readback
    with pytest.raises(RunSkillSnapshotWriterReconfigurationError):
        freeze_run_skill_snapshot_writer(RunSkillSnapshotConfig())


def test_gateway_maps_legacy_busy_to_retryable_503_and_oversize_to_413() -> None:
    busy = private_work_http_exception(LegacyAdmissionBusy("busy-request"))
    assert busy.status_code == 503
    assert busy.headers == {"Retry-After": "1"}
    assert busy.detail == {
        "code": "PRIVATE_WORK_UNAVAILABLE",
        "message": "Private work is unavailable.",
        "request_id": "busy-request",
    }

    oversized = private_work_http_exception(PrivateWorkTooLarge("oversize-request"))
    assert oversized.status_code == 413
    assert oversized.headers is None


def test_skill_builder_preserves_busy_retry_and_permanent_oversize() -> None:
    with pytest.raises(HTTPException) as busy:
        raise_asset_domain(AssetRunAdmissionBusy("builder-busy"))
    assert busy.value.status_code == 503
    assert busy.value.headers == {"Retry-After": "1"}
    assert busy.value.detail["request_id"] == "builder-busy"

    with pytest.raises(HTTPException) as oversized:
        raise_asset_domain(AssetRunPayloadTooLarge("builder-oversize"))
    assert oversized.value.status_code == 413
    assert oversized.value.headers is None
    assert oversized.value.detail["request_id"] == "builder-oversize"


def test_scheduler_keeps_legacy_busy_and_oversize_retryable() -> None:
    busy = AutomationDispatcher._map_error(  # noqa: SLF001
        LegacyAdmissionBusy("scheduler-busy")
    )
    oversized = AutomationDispatcher._map_error(  # noqa: SLF001
        PrivateWorkTooLarge("scheduler-oversize")
    )

    assert type(busy) is AutomationUnavailable
    assert type(oversized) is AutomationUnavailable
