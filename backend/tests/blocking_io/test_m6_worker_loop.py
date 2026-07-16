from __future__ import annotations

import ast
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from support.detectors.blocking_io_static import scan_paths

from app.worker import app as worker_app
from app.worker.service import WorkerService
from deerflow.config.worker_config import WorkerConfig

BACKEND_ROOT = Path(__file__).resolve().parents[2]
WORKER_PATHS = (
    BACKEND_ROOT / "app" / "worker",
    BACKEND_ROOT / "app" / "reliability" / "workers.py",
)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _EmptySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return _Transaction()


class _EmptyFactory:
    def __call__(self):
        return _EmptySession()


class _EmptyRepository:
    def __init__(self, _session) -> None:
        pass

    async def claim_next(self, **_kwargs):
        return None


class _Registry:
    async def register(self, *_args, **_kwargs) -> None:
        pass

    async def heartbeat(self, *_args, **_kwargs) -> bool:
        return True

    async def mark_draining(self, *_args, **_kwargs) -> bool:
        return True

    async def remove(self, *_args, **_kwargs) -> bool:
        return True


def test_worker_async_paths_have_no_static_blocking_io_findings() -> None:
    assert scan_paths(WORKER_PATHS) == []


def test_worker_entrypoint_uses_asyncio_not_blocking_process_or_file_calls() -> None:
    app_path = BACKEND_ROOT / "app" / "worker" / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    forbidden = {
        "subprocess.run",
        "subprocess.call",
        "subprocess.Popen",
        "time.sleep",
    }

    def dotted(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = dotted(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return None

    calls = {dotted(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert calls.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_worker_poll_loop_runs_under_strict_runtime_gate() -> None:
    service = WorkerService(
        _EmptyFactory(),
        _Registry(),
        {},
        WorkerConfig(),
        repository_builder=_EmptyRepository,
    )
    await service.run_until_idle()


@pytest.mark.asyncio
async def test_worker_config_loading_is_offloaded_from_event_loop(monkeypatch) -> None:
    def blocking_config_load():
        time.sleep(0.01)
        return SimpleNamespace(worker=SimpleNamespace(enabled=False))

    monkeypatch.setattr(worker_app, "get_app_config", blocking_config_load)
    await worker_app.run_worker()
