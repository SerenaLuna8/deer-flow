from pathlib import Path

import pytest

from deerflow.config.paths import Paths
from deerflow.runtime.events.store.jsonl import JsonlRunEventStore


def test_default_runtime_home_uses_fluva_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ACT_WEAVE_HOME", raising=False)
    monkeypatch.delenv("ACT_WEAVE_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    assert Paths().base_dir == tmp_path / ".fluva-flow"


@pytest.mark.asyncio
async def test_jsonl_store_default_writes_under_fluva_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    store = JsonlRunEventStore()

    await store.put(
        thread_id="thread-default-home",
        run_id="run-default-home",
        event_type="run_started",
        category="lifecycle",
    )

    expected = tmp_path / ".fluva-flow" / "threads" / "thread-default-home" / "runs" / "run-default-home.jsonl"
    assert expected.is_file()
