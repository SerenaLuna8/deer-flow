from importlib import import_module

from .config import SubagentConfig
from .registry import get_available_subagent_names, get_subagent_config, list_subagents

__all__ = [
    "SubagentConfig",
    "SubagentExecutor",
    "SubagentResult",
    "get_available_subagent_names",
    "get_subagent_config",
    "list_subagents",
]


def __getattr__(name: str):
    if name in {"SubagentExecutor", "SubagentResult"}:
        executor = import_module("deerflow.subagents.executor")

        exports = {
            "SubagentExecutor": executor.SubagentExecutor,
            "SubagentResult": executor.SubagentResult,
        }
        globals().update(exports)
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
