"""PostgreSQL bootstrap keeps synchronous Alembic calls off the event loop."""

import inspect

from deerflow.persistence.bootstrap import bootstrap_schema


def test_bootstrap_offloads_alembic_commands() -> None:
    source = inspect.getsource(bootstrap_schema)
    assert "asyncio.to_thread(_stamp" in source
    assert "asyncio.to_thread(_upgrade" in source
