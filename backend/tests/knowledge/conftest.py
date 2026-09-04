"""Install Schema V1 once; isolate Knowledge tests with cheap database clones."""

import pytest_asyncio
from postgres_utils import RedactedURL, temporary_postgres_database
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.persistence.bootstrap import _install_full_schema


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def knowledge_schema_template(postgres_admin_url):
    async with temporary_postgres_database(postgres_admin_url) as url:
        engine = create_async_engine(url)
        try:
            await _install_full_schema(engine)
        finally:
            # PostgreSQL can only clone a template with no open connections.
            await engine.dispose()
        yield make_url(url).database


@pytest_asyncio.fixture
async def postgres_database_url(postgres_admin_url, knowledge_schema_template):
    # Knowledge tasks do not admit Private Runs and need no writer-cohort lease.
    async with temporary_postgres_database(postgres_admin_url, template=knowledge_schema_template) as url:
        yield RedactedURL(url)


@pytest_asyncio.fixture
async def empty_postgres_database_url(postgres_admin_url):
    """Only schema-installation tests need a fresh, uninstalled database."""
    async with temporary_postgres_database(postgres_admin_url) as url:
        yield RedactedURL(url)
