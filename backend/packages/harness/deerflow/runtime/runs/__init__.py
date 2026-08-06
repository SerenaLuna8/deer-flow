"""Run lifecycle management for LangGraph Platform API compatibility."""

from importlib import import_module

from .manager import ConflictError, RunManager, RunRecord, UnsupportedStrategyError
from .schemas import DisconnectMode, RunStatus


def __getattr__(name: str):
    """Load graph execution only for explicit Worker-side consumers."""

    if name not in {"RunContext", "run_agent"}:
        raise AttributeError(name)
    worker = import_module("deerflow.runtime.runs.worker")
    return getattr(worker, name)


__all__ = [
    "ConflictError",
    "DisconnectMode",
    "RunContext",
    "RunManager",
    "RunRecord",
    "RunStatus",
    "UnsupportedStrategyError",
    "run_agent",
]
