from __future__ import annotations

from pathlib import Path

from app.gateway.routers import project_memory
from app.private_work.memory_service import PrivateMemoryService
from deerflow.agents.memory.manager import PROJECT_MEMORY_CAPABILITIES
from deerflow.agents.memory.storage import ProjectMemoryStorage
from deerflow.persistence.private_work.memory_repository import PrivateMemoryRepository

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_REMOVED_MODULES = (
    "packages/harness/deerflow/agents/middlewares/memory_middleware.py",
    "packages/harness/deerflow/agents/memory/queue.py",
    "packages/harness/deerflow/agents/memory/updater.py",
    "packages/harness/deerflow/agents/memory/summarization_hook.py",
    "packages/harness/deerflow/agents/memory/message_processing.py",
)
_REMOVED_REFERENCES = (
    "deerflow.agents.middlewares.memory_middleware",
    "deerflow.agents.memory.queue",
    "deerflow.agents.memory.updater",
    "deerflow.agents.memory.summarization_hook",
    "deerflow.agents.memory.message_processing",
    "MemoryMiddleware",
    "MemoryUpdater",
    "ProjectMemoryUpdateQueue",
    "get_project_memory_queue",
    "get_initialized_project_memory_queue",
)


def test_legacy_v1_automatic_write_chain_is_absent_from_production_source() -> None:
    for relative_path in _REMOVED_MODULES:
        assert not (_BACKEND_ROOT / relative_path).exists()

    production_sources = (
        *(_BACKEND_ROOT / "app").rglob("*.py"),
        *(_BACKEND_ROOT / "packages/harness/deerflow").rglob("*.py"),
    )
    for source in production_sources:
        content = source.read_text(encoding="utf-8")
        for removed_reference in _REMOVED_REFERENCES:
            assert removed_reference not in content, f"legacy Memory write reference {removed_reference!r} remains in {source}"


def test_v1_memory_surface_is_read_only() -> None:
    routes = {(route.path, method) for route in project_memory.router.routes for method in route.methods or ()}
    assert {
        ("/api/projects/{project_id}/memory", "GET"),
        ("/api/projects/{project_id}/memory/status", "GET"),
        ("/api/projects/{project_id}/memory/export", "GET"),
        ("/api/projects/{project_id}/memory/reload", "POST"),
    } <= routes
    assert {
        ("/api/projects/{project_id}/memory/import", "POST"),
        ("/api/projects/{project_id}/memory/facts", "POST"),
        ("/api/projects/{project_id}/memory/facts/{fact_id}", "PATCH"),
        ("/api/projects/{project_id}/memory/facts/{fact_id}", "DELETE"),
    }.isdisjoint(routes)

    for owner in (PrivateMemoryService, ProjectMemoryStorage, PrivateMemoryRepository):
        for method in ("import_memory", "create_fact", "update", "delete", "save", "clear"):
            assert not hasattr(owner, method)

    assert PROJECT_MEMORY_CAPABILITIES.supports_search is True
    assert PROJECT_MEMORY_CAPABILITIES.supports_fact_mutation is False
    assert PROJECT_MEMORY_CAPABILITIES.requires_passive_writes is False
