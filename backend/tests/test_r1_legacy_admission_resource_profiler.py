from __future__ import annotations

import inspect
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.shared_assets.models import SkillArchiveFile
from scripts.profile_r1_legacy_admission_resources import (
    _archive_facts,
    _assemble_version,
    _atomic_write_json,
    _attempt_worker,
    _deterministic_near_ceiling_subset,
    _exception_facts,
    _gate_release_probes,
    _orchestrate,
    _R1ProfilePhaseError,
    _R1SeedCoordinates,
    _R1VersionCoordinates,
    _release_role_attempts,
    _run_attempt_topology,
    _seed_scope,
    _seed_version_parent,
)

pytestmark = pytest.mark.run_skill_writer_cohort_control


def test_release_topology_uses_three_process_roles_and_eight_attempts() -> None:
    topology = _release_role_attempts()

    assert topology == (
        ("gateway-a", 3),
        ("gateway-b", 3),
        ("scheduler", 2),
    )
    assert sum(attempts for _role, attempts in topology) == 8


def test_seed_coordinates_round_trip_full_and_near_versions() -> None:
    versions = tuple(
        _R1VersionCoordinates(
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            checksum=str(index) * 64,
            file_count=index,
            content_size_bytes=index * 10,
        )
        for index in (1, 2)
    )
    coordinates = _R1SeedCoordinates(
        database_name="deerflow_test_1_" + "a" * 32,
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        thread_id="r1-resource-thread",
        full=versions[0],
        near=versions[1],
    )

    assert _R1SeedCoordinates.from_json(coordinates.as_json()) == coordinates


def test_near_ceiling_subset_keeps_whole_files_and_root_manifest() -> None:
    files = (
        SkillArchiveFile(
            path="SKILL.md",
            media_type="text/markdown",
            content=b"manifest",
        ),
        SkillArchiveFile(
            path="a.bin",
            media_type="application/octet-stream",
            content=b"a" * 9,
        ),
        SkillArchiveFile(
            path="b.bin",
            media_type="application/octet-stream",
            content=b"b" * 7,
        ),
        SkillArchiveFile(
            path="c.bin",
            media_type="application/octet-stream",
            content=b"c" * 5,
        ),
    )

    selected = _deterministic_near_ceiling_subset(
        files,
        max_content_bytes=17,
    )

    assert selected == (files[0], files[1])
    assert all(item in files for item in selected)


def test_attempt_worker_uses_production_writer_and_fail_fast_gate() -> None:
    source = inspect.getsource(_attempt_worker)

    assert "LegacyRunSkillSnapshotWriter" in source
    assert "freeze_run_skill_snapshot_writer" in source
    assert "RunSkillWriterCohortLease.acquire" in source
    assert "require_active_run_skill_writer_cohort" in source
    assert "cohort_readback" in source
    assert "content_query_count" in source
    assert "permit_attempt_count" in source
    assert "winner-ready" in source
    assert "LegacyAdmissionBusy" in source


def test_orchestrator_requires_real_subprocesses_and_coexistence_evidence() -> None:
    source = inspect.getsource(_orchestrate)
    topology_source = inspect.getsource(_run_attempt_topology)
    gate_source = inspect.getsource(_gate_release_probes)

    assert "asyncio.create_subprocess_exec" in topology_source
    assert "distinct_os_processes" in source
    assert "full_preflight_permit_attempts_zero" in source
    assert "full_preflight_content_queries_zero" in source
    assert "physical_connection_close_releases_permit" in gate_source
    assert "coexistence_no_oom_increment" in source
    assert "rollback_v4_reads_v3_and_v4" in source


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_three_process_topology_completes_with_tiny_skill_version(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    files = (
        SkillArchiveFile(
            path="SKILL.md",
            media_type="text/markdown",
            content=b"---\nname: tiny-r1-profile\n---\n",
        ),
        SkillArchiveFile(
            path="fixture.txt",
            media_type="text/plain",
            content=b"tiny fixture",
        ),
    )
    facts = _archive_facts(files)
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            user_id, project_id, agent_id, thread_id = await _seed_scope(session)
        async with factory() as session, session.begin():
            version = await _seed_version_parent(
                session,
                project_id=project_id,
                user_id=user_id,
                slug="tiny-r1-profile",
                facts=facts,
            )
            await _assemble_version(session, version=version, files=files)
    finally:
        await engine.dispose()

    coordinates = _R1SeedCoordinates(
        database_name="pytest-managed",
        user_id=user_id,
        project_id=project_id,
        agent_id=agent_id,
        thread_id=thread_id,
        full=version,
        near=version,
    )
    spec_path = tmp_path / "coordinates.json"
    _atomic_write_json(spec_path, coordinates.as_json())

    try:
        topology = await _run_attempt_topology(
            script_path=(Path(__file__).resolve().parents[1] / "scripts" / "profile_r1_legacy_admission_resources.py"),
            database_url=str(migrated_postgres_database_url),
            spec_path=spec_path,
            temporary_root=tmp_path,
            container_name="postgres",
            postgres_samples=[],
        )
    except _R1ProfilePhaseError as error:
        pytest.fail(json.dumps(_exception_facts(error), sort_keys=True))

    assert topology["process_count"] == 3
    assert topology["attempt_count"] == 8
    assert sum(attempt["outcome"] == "success" for attempt in topology["attempts"]) == 1
    assert sum(attempt["outcome"] == "retryable_busy" for attempt in topology["attempts"]) == 7
