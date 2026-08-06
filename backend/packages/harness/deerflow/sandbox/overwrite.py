"""Helpers for ``Overwrite``-wrapped sandbox channel values."""

from langgraph.types import Overwrite


def unwrap_sandbox(sandbox: object) -> tuple[object, bool]:
    """Return the channel value and whether it came from a fork restore."""
    if isinstance(sandbox, Overwrite):
        return sandbox.value, True
    return sandbox, False
