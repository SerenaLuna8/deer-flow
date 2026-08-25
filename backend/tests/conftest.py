"""Test configuration for the backend test suite.

Sets up sys.path and pre-mocks modules that would cause circular import
issues when unit-testing lightweight config/registry code in isolation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Keep the caller's development connection separate from the non-sensitive
# import-time fallback below. ``make test`` loads the root development
# environment before pytest starts; focused unit tests without that environment
# continue to skip real PostgreSQL cases.
_DEVELOPMENT_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Pytest imports conftest before collecting test modules. Always replace the
# application-facing value with a deliberately non-sensitive, nonexistent test
# target so import-time Gateway state can never connect to the development
# database. Real PostgreSQL fixtures retain the captured URL above and derive
# their own random ``deerflow_test_*`` databases from it.
os.environ["DATABASE_URL"] = "postgresql://test-role@localhost/deerflow_test_unit"

# Make 'app' and 'deerflow' importable from any working directory
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from postgres_utils import RedactedURL, replace_database, temporary_postgres_database  # noqa: E402
from support.core_gate_plugin import pytest_sessionfinish  # noqa: E402, F401


@pytest.fixture(scope="session", autouse=True)
def install_test_only_system_model_adapter():
    """Expose the deterministic vision fake only inside pytest processes."""

    from app.system_settings import validation

    adapter_name = "vision_bridge_fake"
    previous = validation.PROVIDER_ADAPTERS.get(adapter_name)
    validation.PROVIDER_ADAPTERS[adapter_name] = validation.ProviderAdapterSpec(
        "support.fake_models:FakeVisionBridgeChatModel",
        False,
    )
    try:
        yield
    finally:
        if previous is None:
            validation.PROVIDER_ADAPTERS.pop(adapter_name, None)
        else:
            validation.PROVIDER_ADAPTERS[adapter_name] = previous


@pytest.fixture(scope="session")
def postgres_admin_url() -> str:
    """Derive a maintenance URL from the development URL without logging it."""
    url = _DEVELOPMENT_DATABASE_URL
    if not url:
        pytest.skip("DATABASE_URL from the development environment is required for PostgreSQL tests")
    return RedactedURL(replace_database(url, "postgres"))


@pytest_asyncio.fixture()
async def postgres_database_url(
    postgres_admin_url: str,
    request: pytest.FixtureRequest,
):
    async with temporary_postgres_database(postgres_admin_url) as url:
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.private_work.legacy_run_skill_snapshot_writer import (
            freeze_run_skill_snapshot_writer,
            reset_run_skill_snapshot_writer_for_testing,
        )
        from app.private_work.run_skill_writer_cohort import (
            RunSkillWriterCohortLease,
        )
        from deerflow.config.run_skill_snapshot_config import (
            RunSkillSnapshotConfig,
        )

        reset_run_skill_snapshot_writer_for_testing()
        cohort_control = request.node.get_closest_marker("run_skill_writer_cohort_control") is not None
        engine = create_async_engine(url) if not cohort_control else None
        lease = None
        if engine is not None:
            writer = freeze_run_skill_snapshot_writer(RunSkillSnapshotConfig())
            lease = await RunSkillWriterCohortLease.acquire(
                engine,
                writer,
                process_role="gateway",
                process_authority=True,
            )
        try:
            yield RedactedURL(url)
        finally:
            if lease is not None:
                await lease.close()
            reset_run_skill_snapshot_writer_for_testing()
            if engine is not None:
                await engine.dispose()


@pytest_asyncio.fixture()
async def migrated_postgres_database_url(postgres_database_url: str):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.system_runtime_settings.bootstrap import (
        bootstrap_system_runtime_policies,
    )
    from deerflow.persistence.bootstrap import bootstrap_schema

    engine = create_async_engine(postgres_database_url)
    try:
        await bootstrap_schema(engine)
        await bootstrap_system_runtime_policies(
            async_sessionmaker(engine, expire_on_commit=False),
        )
        yield postgres_database_url
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def mock_non_postgres_run_skill_writer_cohort_assertion(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give fake-session unit tests one test-only Admission assertion seam."""

    if (
        request.node.get_closest_marker("postgres") is not None
        or "postgres_database_url" in request.fixturenames
        or "migrated_postgres_database_url" in request.fixturenames
        or request.node.get_closest_marker("run_skill_writer_cohort_control") is not None
    ):
        return
    from support.run_skill_writer_cohort import install_mock_cohort_assertion

    install_mock_cohort_assertion(monkeypatch)
