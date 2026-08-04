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

# Pytest imports conftest before collecting test modules. Install a deliberately
# non-sensitive unit-test default here so modules that construct
# Gateway state at import time never rely on an implicit repository dotenv
# load. Explicit caller-provided values (including core-suite databases) win.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://test-role@localhost/deerflow_test_unit",
)

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


@pytest.fixture(scope="session")
def postgres_admin_url() -> str:
    """Return a maintenance URL without ever logging credentials."""
    url = os.getenv("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL is required for PostgreSQL tests")
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
