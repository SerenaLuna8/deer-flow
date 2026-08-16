"""Test configuration for the backend test suite.

Sets up sys.path and pre-mocks modules that would cause circular import
issues when unit-testing lightweight config/registry code in isolation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

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

# Break the circular import chain that exists in production code:
#   deerflow.subagents.__init__
#     -> .executor (SubagentExecutor, SubagentResult)
#       -> deerflow.agents.thread_state
#         -> deerflow.agents.__init__
#           -> lead_agent.agent
#             -> subagent_limit_middleware
#               -> deerflow.subagents.executor  <-- circular!
#
# By injecting a mock for deerflow.subagents.executor *before* any test module
# triggers the import, __init__.py's "from .executor import ..." succeeds
# immediately without running the real executor module.
_executor_mock = MagicMock()
_executor_mock.SubagentExecutor = MagicMock
_executor_mock.SubagentResult = MagicMock
_executor_mock.SubagentStatus = MagicMock
_executor_mock.MAX_CONCURRENT_SUBAGENTS = 3
_executor_mock.get_background_task_result = MagicMock()

sys.modules["deerflow.subagents.executor"] = _executor_mock


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
async def postgres_database_url(postgres_admin_url: str):
    async with temporary_postgres_database(postgres_admin_url) as url:
        yield RedactedURL(url)


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
