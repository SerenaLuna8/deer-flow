from __future__ import annotations

import ast
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from support.detectors.blocking_io_static import scan_paths

from app.scheduler.service import ScheduledTaskService
from scripts import migrate_automations

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REQUEST_PATHS = (
    BACKEND_ROOT / "app" / "automations",
    BACKEND_ROOT / "app" / "gateway" / "routers" / "project_automations.py",
    BACKEND_ROOT / "app" / "scheduler",
)
MIGRATION_CLI = BACKEND_ROOT / "scripts" / "migrate_automations.py"


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def test_automation_async_paths_have_no_static_blocking_io_findings() -> None:
    assert scan_paths((*REQUEST_PATHS, MIGRATION_CLI)) == []


def test_request_paths_do_not_import_sync_alembic_command() -> None:
    offenders: list[str] = []
    for path in REQUEST_PATHS:
        sources = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "alembic" and any(alias.name == "command" for alias in node.names):
                    offenders.append(str(source.relative_to(BACKEND_ROOT)))
                if isinstance(node, ast.Import) and any(alias.name in {"alembic", "alembic.command"} for alias in node.names):
                    offenders.append(str(source.relative_to(BACKEND_ROOT)))
    assert offenders == []


def test_migration_cli_wraps_its_only_sync_alembic_call_in_to_thread() -> None:
    tree = ast.parse(MIGRATION_CLI.read_text(encoding="utf-8"))
    alembic_references = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and _dotted_name(node) == "command.upgrade"]
    offloaded_upgrades = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _dotted_name(node.func) == "asyncio.to_thread" and node.args and _dotted_name(node.args[0]) == "command.upgrade"]
    assert len(alembic_references) == 1
    assert len(offloaded_upgrades) == 1


@pytest.mark.asyncio
async def test_scheduler_poll_path_runs_under_strict_runtime_gate() -> None:
    service = ScheduledTaskService(
        occurrences=SimpleNamespace(
            due_definitions=AsyncMock(return_value=()),
        ),
        dispatcher=SimpleNamespace(admit_occurrence=AsyncMock()),
        reconciler=SimpleNamespace(reconcile_restart=AsyncMock()),
        poll_interval_seconds=60,
        max_concurrent_runs=3,
    )

    await service.run_once(now=service._clock())


@pytest.mark.asyncio
async def test_migration_alembic_upgrade_yields_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread = threading.get_ident()
    upgrade_thread: int | None = None

    def blocking_upgrade(*_args) -> None:
        nonlocal upgrade_thread
        upgrade_thread = threading.get_ident()

    monkeypatch.setattr(migrate_automations.command, "upgrade", blocking_upgrade)
    monkeypatch.setattr(
        migrate_automations,
        "_get_alembic_config",
        lambda _engine: object(),
    )
    await migrate_automations._upgrade_database(object(), "head")

    assert upgrade_thread is not None
    assert upgrade_thread != event_loop_thread
