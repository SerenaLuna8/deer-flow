from __future__ import annotations

import inspect

import pytest

from scripts import profile_v4_100_run_storage as profiler


def test_v3_whole_package_snapshot_is_an_explicit_storage_redline() -> None:
    legacy_snapshot = {
        "schema_version": 3,
        "kind": "skill",
        "skill": {
            "codec": "canonical-frame-zlib-6",
            "archive_base64": "eJz" + "a" * 256,
        },
    }

    with pytest.raises(
        profiler.StorageAcceptanceError,
        match="byte-free v4 Skill manifest",
    ):
        profiler.assert_byte_free_v4_skill_snapshot(legacy_snapshot)


def test_v4_reference_manifest_passes_the_storage_redline() -> None:
    profiler.assert_byte_free_v4_skill_snapshot(
        {
            "schema_version": 4,
            "kind": "skill",
            "skill": {
                "source": "skill_version_ref",
                "file_count": 12_922,
                "content_size_bytes": 79_243_539,
            },
        }
    )

    assert profiler._legacy_whole_package_redline_rejects() is True


def test_sql_capture_flags_content_reads_and_large_json_parameters() -> None:
    capture = profiler.AdmissionSQLCapture()
    capture.record(
        "SELECT skill_version_files.content FROM skill_version_files",
        (),
    )
    capture.record(
        "INSERT INTO run_asset_versions (snapshot_json) VALUES ($1)",
        ("x" * (50 * 1024 * 1024),),
    )

    assert capture.file_content_select_count == 1
    assert capture.max_json_parameter_bytes == 50 * 1024 * 1024
    assert capture.assertions()["gateway_admission_did_not_select_skill_content"] is False
    assert capture.assertions()["gateway_admission_did_not_send_large_jsonb"] is False
    assert capture.assertions()["gateway_metadata_reads_do_not_implicitly_detoast_payloads"] is False


def test_profile_contract_uses_production_admission_and_disposable_database() -> None:
    source = inspect.getsource(profiler)

    assert "RunSnapshotRepository" in source
    assert ".create_run_with_snapshot(" in source
    assert "RunSkillWriterCohortLease.acquire(" in source
    assert "_temporary_database" in source
    assert "pg_current_wal_lsn()" in source
    assert "pg_wal_lsn_diff" in source
    assert "pg_relation_size" in source
    assert "skill_version_files" in source
    assert "run_asset_versions" in source
    assert "run_skill_version_refs" in source
