"""Fail-closed embedded session wiring for the TUI.

The CLI has no authenticated project authority, so its default session is
stateless. Trusted embeddings may construct :class:`Session` with an explicitly
project-scoped client and writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the heavy client during pure planning
    from deerflow.client import DeerFlowClient

    from .cli import LaunchPlan
    from .persistence import ThreadMetaWriter, _LoopThread


@dataclass
class Session:
    client: DeerFlowClient
    writer: ThreadMetaWriter | None = None
    _loop: _LoopThread | None = None

    def resolve_thread(self, plan: LaunchPlan) -> str | None:
        """Resolve the thread id to run against, honoring --resume / --continue."""
        if plan.thread_id:
            return self.resolve_ref(plan.thread_id)
        if plan.continue_recent:
            try:
                threads = self.client.list_threads(limit=1).get("thread_list", [])
            except Exception:  # noqa: BLE001 - no scoped persistence is expected
                return None
            if threads:
                return threads[0].get("thread_id")
        return None

    def resolve_ref(self, ref: str) -> str:
        """Resolve a thread reference (id or title) to a thread id.

        Matches an existing thread by id first, then by exact title. Falls back to
        the literal ref (treated as an id) when nothing matches, so an unknown id
        still continues/creates that namespace.
        """
        try:
            threads = self.client.list_threads(limit=100).get("thread_list", [])
        except Exception:  # noqa: BLE001 - resolution is best-effort
            return ref
        if any(t.get("thread_id") == ref for t in threads):
            return ref
        for thread in threads:
            if (thread.get("title") or "") == ref:
                return thread.get("thread_id") or ref
        return ref

    def recent_threads(self, limit: int = 20) -> list[dict]:
        try:
            return self.client.list_threads(limit=limit).get("thread_list", [])
        except Exception:  # noqa: BLE001 - no scoped persistence is expected
            return []

    def close(self) -> None:
        """Stop the background DB loop and dispose the engine (best-effort)."""
        loop = self._loop
        if loop is None:
            return
        self._loop = None
        try:
            from deerflow.persistence.engine import close_engine

            loop.run(close_engine())
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass
        loop.close()


def open_session(persistence: bool = True) -> Session:
    """Build a stateless embedded session.

    ``persistence`` is retained for CLI call compatibility but never grants
    project authority. A TUI process without an authenticated immutable project
    scope cannot read or write persisted Thread data.
    """
    from deerflow.client import DeerFlowClient

    return Session(client=DeerFlowClient())
