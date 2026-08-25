from __future__ import annotations

import inspect
import uuid

from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.run_snapshot_codec import decode_run_asset_snapshot
from scripts.profile_v4_materialization_resources import (
    _install_and_seed,
    _legacy_v2_snapshot,
    _orchestrate,
    _release_source_mix,
    _RunCoordinates,
    _SeedCoordinates,
    _worker_profile,
)


def test_release_profile_source_mix_is_capacity_eight_and_covers_all_readers() -> None:
    mix = _release_source_mix(8)

    assert mix == ("v4", "v4", "v4", "v3", "v4", "v2", "v4", "v4")
    assert set(mix) == {"v2", "v3", "v4"}


def test_profile_coordinates_round_trip_each_source_identity() -> None:
    runs = tuple(
        _RunCoordinates(
            run_id=f"run-{source_kind}",
            source_kind=source_kind,
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            checksum=str(index) * 64,
            file_count=index,
            content_size_bytes=index * 10,
            snapshot_schema_version=schema_version,
        )
        for index, (source_kind, schema_version) in enumerate(
            (("v2", 2), ("v3", 3), ("v4", 4)),
            start=1,
        )
    )
    coordinates = _SeedCoordinates(
        database_name="deerflow_test_1_" + "a" * 32,
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        thread_id="mixed-profile-thread",
        runs=runs,
    )

    assert _SeedCoordinates.from_json(coordinates.as_json()) == coordinates


def test_v2_profile_snapshot_is_strict_inline_base64_from_real_files() -> None:
    files = (
        SkillArchiveFile(
            path="SKILL.md",
            media_type="text/markdown",
            content=b"---\nname: ppt-master\ndescription: test\n---\n",
        ),
        SkillArchiveFile(
            path="references/example.txt",
            media_type="text/plain",
            content=b"example",
        ),
    )
    encoded, snapshot = _legacy_v2_snapshot(
        files,
        scope="project",
        skill_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        catalog_generation=7,
    )

    assert encoded["schema_version"] == 2
    assert "files" in encoded["skill"]
    assert "archive_base64" not in encoded["skill"]
    assert decode_run_asset_snapshot(encoded) == snapshot


def test_worker_profile_records_pre_detoast_exclusivity_and_cancel_cleanup() -> None:
    source = inspect.getsource(_worker_profile)

    assert "LegacyInlineRunSkillSourceAdapter" in source
    assert "legacy_query_reservations" in source
    assert "v4_source_query_reservations" in source
    assert "legacy_pre_detoast_exclusive" in source
    assert "cancellation_waiter_removed" in source
    assert "cancellation_owner_cleaned" in source
    assert "cancellation_prevented_source_query" in source
    assert "source_kind_coverage" in source
    assert "worker_rss_delta_within_total_envelope" in source


def test_release_profile_seeds_large_run_closures_in_separate_transactions() -> None:
    source = inspect.getsource(_install_and_seed)

    assert source.count("async with factory() as session, session.begin():") == 2
    assert "for run in runs:\n            async with factory()" in source


def test_release_profile_oom_and_postmaster_baseline_cover_seed_phase() -> None:
    source = inspect.getsource(_orchestrate)

    assert source.index("oom_before =") < source.index("_install_and_seed(")
    assert source.index("before_identity =") < source.index("_install_and_seed(")


def test_legacy_fixture_bypasses_only_the_deferred_closure_verifier() -> None:
    source = inspect.getsource(_install_and_seed)

    assert 'if run.source_kind in {"v2", "v3"}:' in source
    assert 'text("ALTER TABLE runs DISABLE TRIGGER trg_runs_asset_closure_complete")' in source
    assert 'text("ALTER TABLE runs ENABLE TRIGGER trg_runs_asset_closure_complete")' in source
    assert "session_replication_role" not in source
