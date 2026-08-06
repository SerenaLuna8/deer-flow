"""Abstract interface for run event storage.

RunEventStore is the unified storage interface for run event streams.
Messages (frontend display) and execution traces (debugging/audit) go
through the same interface, distinguished by the ``category`` field.

Implementations:
- DbRunEventStore: PostgreSQL implementation
- Future: DB-backed store (SQLAlchemy ORM), JSONL file store
"""

from __future__ import annotations

import abc
from collections.abc import Iterable, Mapping

from deerflow.runtime.private_scope import PrivateResourceScope


class RunEventStore(abc.ABC):
    """Run event stream storage interface.

    All implementations must guarantee:
    1. put() events are retrievable in subsequent queries
    2. seq is strictly increasing within the same thread
    3. list_messages() only returns category="message" events
    4. list_events() returns all events for the specified run
    5. Returned dicts match the RunEvent field structure
    """

    @abc.abstractmethod
    async def put(
        self,
        *,
        thread_id: str,
        run_id: str,
        event_type: str,
        category: str,
        content: str | dict = "",
        metadata: dict | None = None,
        created_at: str | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> dict:
        """Write an event, auto-assign seq, return the complete record."""

    @abc.abstractmethod
    async def put_batch(
        self,
        events: list[dict],
        *,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict]:
        """Batch-write events. Used by RunJournal flush buffer.

        Each dict's keys match put()'s keyword arguments.
        Returns complete records with seq assigned.
        """

    @abc.abstractmethod
    async def list_messages(
        self,
        thread_id: str,
        *,
        limit: int = 50,
        before_seq: int | None = None,
        after_seq: int | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict]:
        """Return displayable messages (category=message) for a thread, ordered by seq ascending.

        Supports bidirectional cursor pagination:
        - before_seq: return the last ``limit`` records with seq < before_seq (ascending)
        - after_seq: return the first ``limit`` records with seq > after_seq (ascending)
        - neither: return the latest ``limit`` records (ascending)
        """

    @abc.abstractmethod
    async def list_events(
        self,
        thread_id: str,
        run_id: str,
        *,
        event_types: list[str] | None = None,
        task_id: str | None = None,
        limit: int = 500,
        after_seq: int | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict]:
        """Return the full event stream for a run, ordered by seq ascending.

        Optionally filter by ``event_types`` and/or ``task_id`` (matched against
        ``metadata["task_id"]``). ``after_seq`` is a forward cursor returning the
        first ``limit`` records with seq > after_seq, so callers can page through
        a single subagent task's events without the run-wide ``limit`` truncating
        the tail (#3779).
        """

    @abc.abstractmethod
    async def list_messages_by_run(
        self,
        thread_id: str,
        run_id: str,
        *,
        limit: int = 50,
        before_seq: int | None = None,
        after_seq: int | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict]:
        """Return displayable messages (category=message) for a specific run, ordered by seq ascending.

        Supports bidirectional cursor pagination:
        - after_seq: return the first ``limit`` records with seq > after_seq (ascending)
        - before_seq: return the last ``limit`` records with seq < before_seq (ascending)
        - neither: return the latest ``limit`` records (ascending)
        """

    @abc.abstractmethod
    async def count_messages(self, thread_id: str, *, scope: PrivateResourceScope | None = None) -> int:
        """Count displayable messages (category=message) in a thread."""

    async def get_last_visible_ai_seq_by_run(
        self,
        thread_id: str,
        run_ids: Iterable[str],
        *,
        scope: PrivateResourceScope | None = None,
    ) -> dict[str, int]:
        """Resolve each Run's last visible lead-AI sequence.

        Stores may override this with a grouped query. The bounded fallback is
        intentionally based on the public scoped message reader so non-SQL
        implementations retain the same semantics.
        """

        result: dict[str, int] = {}
        for run_id in {value for value in run_ids if isinstance(value, str) and value}:
            before_seq: int | None = None
            while True:
                rows = await self.list_messages_by_run(
                    thread_id,
                    run_id,
                    limit=200,
                    before_seq=before_seq,
                    scope=scope,
                )
                for row in reversed(rows):
                    content = row.get("content")
                    metadata = row.get("metadata")
                    caller = metadata.get("caller") if isinstance(metadata, Mapping) else None
                    additional_kwargs = content.get("additional_kwargs") if isinstance(content, Mapping) else None
                    if (
                        isinstance(content, Mapping)
                        and content.get("type") in {"ai", "assistant"}
                        and not (isinstance(caller, str) and caller.startswith(("middleware:", "subagent:")))
                        and not (isinstance(additional_kwargs, Mapping) and additional_kwargs.get("hide_from_ui") is True)
                        and type(row.get("seq")) is int
                    ):
                        result[run_id] = row["seq"]
                        break
                if run_id in result or len(rows) < 200:
                    break
                first_seq = rows[0].get("seq") if rows else None
                if type(first_seq) is not int or first_seq <= 0:
                    break
                before_seq = first_seq
        return result

    @abc.abstractmethod
    async def delete_by_thread(self, thread_id: str, *, scope: PrivateResourceScope | None = None) -> int:
        """Delete all events for a thread. Return the number of deleted events."""

    @abc.abstractmethod
    async def delete_by_run(
        self,
        thread_id: str,
        run_id: str,
        *,
        scope: PrivateResourceScope | None = None,
    ) -> int:
        """Delete all events for a specific run. Return the number of deleted events."""

    async def append_stream_frame(self, *args, **kwargs):
        """Append one durable SSE frame inside a caller-owned transaction."""

        raise NotImplementedError("durable stream frames require a database event store")

    async def list_stream_frames(self, *args, **kwargs):
        """Read scoped durable SSE frames after a thread cursor."""

        raise NotImplementedError("durable stream frames require a database event store")
