"""Full-schema bootstrap keeps snapshot file I/O off the event loop."""

import asyncio

import pytest

from deerflow.persistence import bootstrap


@pytest.mark.asyncio
async def test_full_schema_install_offloads_snapshot_read(monkeypatch) -> None:
    calls: list[tuple[object, tuple[object, ...]]] = []
    original_to_thread = asyncio.to_thread

    async def spy_to_thread(function, *args):
        calls.append((function, args))
        return await original_to_thread(function, *args)

    monkeypatch.setattr(bootstrap.asyncio, "to_thread", spy_to_thread)

    class DriverConnection:
        executed: list[str] = []

        async def execute(self, payload: str) -> None:
            self.executed.append(payload)

    driver = DriverConnection()

    class RawConnection:
        driver_connection = driver

    class Connection:
        async def get_raw_connection(self) -> RawConnection:
            return RawConnection()

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args) -> None:
            return None

    class Engine:
        def connect(self) -> ConnectionContext:
            return ConnectionContext()

    await bootstrap._install_full_schema(Engine())

    assert calls == [(bootstrap._read_full_schema_sql, ())]
    assert len(driver.executed) == 1
    assert driver.executed[0].startswith("BEGIN;\n")
