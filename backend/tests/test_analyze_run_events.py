"""Read-only run-event measurement query contracts (U2 Phase 0)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_content_volume_uses_postgresql_bytes_not_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/analyze_run_events.py"
    spec = importlib.util.spec_from_file_location("analyze_run_events_probe", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    queries: list[str] = []

    class _Engine:
        async def dispose(self) -> None:
            return None

    responses = iter(
        (
            [(1, 3, 1)],
            [(1, 1, 1, 3, 3, 3)],
            [("stream", "messages", 1, 3)],
            [],
        )
    )

    async def fetch_all(_engine, query: str):
        queries.append(query)
        return next(responses)

    monkeypatch.setattr(module, "create_async_engine", lambda *_args, **_kwargs: _Engine())
    monkeypatch.setattr(module, "_fetch_all", fetch_all)

    report = await module.analyze("postgresql+asyncpg://unused/db", days=None)

    assert "总内容字节: 3" in report
    assert len(queries) == 4
    assert sum(query.count("OCTET_LENGTH(content)") for query in queries) == 3
    assert all("SUM(LENGTH(content))" not in query for query in queries)
