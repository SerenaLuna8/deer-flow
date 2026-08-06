"""LangGraph-compatible runtime — runs, streaming, and lifecycle management.

Re-exports the project runtime and PostgreSQL-only persistence providers.
"""

from importlib import import_module

from .checkpointer import checkpointer_context, get_checkpointer, make_checkpointer, reset_checkpointer
from .private_scope import PrivateResourceScope
from .runs import ConflictError, DisconnectMode, RunManager, RunRecord, RunStatus, UnsupportedStrategyError
from .serialization import serialize, serialize_channel_values, serialize_channel_values_for_api, serialize_lc_object, serialize_messages_tuple, strip_data_url_image_blocks
from .store import get_store, make_store, reset_store, store_context


def __getattr__(name: str):
    """Keep graph execution lazy so Gateway/Scheduler imports stay worker-free."""

    if name not in {"RunContext", "run_agent"}:
        raise AttributeError(name)
    worker = import_module("deerflow.runtime.runs.worker")
    return getattr(worker, name)


__all__ = [
    # checkpointer
    "checkpointer_context",
    "get_checkpointer",
    "make_checkpointer",
    "reset_checkpointer",
    # private scope
    "PrivateResourceScope",
    # runs
    "ConflictError",
    "DisconnectMode",
    "RunContext",
    "RunManager",
    "RunRecord",
    "RunStatus",
    "UnsupportedStrategyError",
    "run_agent",
    # serialization
    "serialize",
    "serialize_channel_values",
    "serialize_channel_values_for_api",
    "serialize_lc_object",
    "serialize_messages_tuple",
    "strip_data_url_image_blocks",
    # store
    "get_store",
    "make_store",
    "reset_store",
    "store_context",
]
