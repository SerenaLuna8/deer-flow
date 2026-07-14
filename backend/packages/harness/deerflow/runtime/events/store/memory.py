"""In-memory RunEventStore. Used when run_events.backend=memory (default) and in tests.

Thread-safe for single-process async usage (no threading locks needed
since all mutations happen within the same event loop).
"""

from __future__ import annotations

import bisect
from datetime import UTC, datetime

from deerflow.runtime.events.store.base import RunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope


class MemoryRunEventStore(RunEventStore):
    def __init__(self) -> None:
        self._events: dict[str, list[dict]] = {}  # thread_id -> seq-sorted event list
        # Messages-only projection of ``_events`` (same dict objects, no copies),
        # kept in seq order so message pagination is O(log m + page) via bisect
        # instead of re-scanning every event on each request.
        self._messages: dict[str, list[dict]] = {}  # thread_id -> seq-sorted message list
        # Run-keyed projections of the two lists above (same dict objects, no
        # copies), kept in seq order. Per-run reads then cost O(events-in-run)
        # instead of O(events-in-thread): without these, ``list_events`` and
        # ``list_messages_by_run`` re-scan the whole thread's event log on every
        # request even though one run holds only a handful of events. This is
        # the per-run analogue of the thread-wide ``_messages`` projection.
        self._events_by_run: dict[str, dict[str, list[dict]]] = {}  # thread_id -> run_id -> seq-sorted events
        self._messages_by_run: dict[str, dict[str, list[dict]]] = {}  # thread_id -> run_id -> seq-sorted messages
        self._seq_counters: dict[str, int] = {}  # thread_id -> last assigned seq

    def _next_seq(self, thread_id: str) -> int:
        current = self._seq_counters.get(thread_id, 0)
        next_val = current + 1
        self._seq_counters[thread_id] = next_val
        return next_val

    def _put_one(
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
        seq = self._next_seq(thread_id)
        record = {
            "thread_id": thread_id,
            "run_id": run_id,
            "event_type": event_type,
            "category": category,
            "content": content,
            "metadata": metadata or {},
            "seq": seq,
            "created_at": created_at or datetime.now(UTC).isoformat(),
            "project_id": None if scope is None else scope.project_id,
            "owner_user_id": None if scope is None else scope.owner_user_id,
        }
        self._events.setdefault(thread_id, []).append(record)
        self._events_by_run.setdefault(thread_id, {}).setdefault(run_id, []).append(record)
        if category == "message":
            self._messages.setdefault(thread_id, []).append(record)
            self._messages_by_run.setdefault(thread_id, {}).setdefault(run_id, []).append(record)
        return record

    async def put(
        self,
        *,
        thread_id,
        run_id,
        event_type,
        category,
        content="",
        metadata=None,
        created_at=None,
        scope=None,
    ):
        return self._put_one(
            thread_id=thread_id,
            run_id=run_id,
            event_type=event_type,
            category=category,
            content=content,
            metadata=metadata,
            created_at=created_at,
            scope=scope,
        )

    async def put_batch(self, events, *, scope=None):
        results = []
        for ev in events:
            record = self._put_one(**ev, scope=scope)
            results.append(record)
        return results

    @staticmethod
    def _matches_scope(event: dict, scope: PrivateResourceScope | None) -> bool:
        return event.get("project_id") == (None if scope is None else scope.project_id) and event.get("owner_user_id") == (None if scope is None else scope.owner_user_id)

    async def list_messages(self, thread_id, *, limit=50, before_seq=None, after_seq=None, scope=None):
        # ``messages`` is messages-only and seq-sorted, so the seq window is a
        # contiguous slice located with bisect (O(log m)) rather than a full scan.
        messages = [e for e in self._messages.get(thread_id, []) if self._matches_scope(e, scope)]

        if before_seq is not None:
            # Records with seq < before_seq, then the last `limit` of them.
            hi = bisect.bisect_left(messages, before_seq, key=lambda e: e["seq"])
            return messages[max(0, hi - limit) : hi]
        elif after_seq is not None:
            # Records with seq > after_seq, then the first `limit` of them.
            lo = bisect.bisect_right(messages, after_seq, key=lambda e: e["seq"])
            return messages[lo : lo + limit]
        else:
            # Return the latest `limit` records, ascending.
            return messages[-limit:]

    async def list_events(self, thread_id, run_id, *, event_types=None, task_id=None, limit=500, after_seq=None, scope=None):
        # ``_events_by_run`` is already scoped to this run and seq-ordered, so we
        # touch only this run's events instead of scanning the whole thread.
        run_events = [e for e in self._events_by_run.get(thread_id, {}).get(run_id, []) if self._matches_scope(e, scope)]
        if event_types is not None:
            run_events = [e for e in run_events if e["event_type"] in event_types]
        if task_id is not None:
            run_events = [e for e in run_events if (e.get("metadata") or {}).get("task_id") == task_id]
        if after_seq is not None:
            run_events = [e for e in run_events if e.get("seq", 0) > after_seq]
        return run_events[:limit]

    async def list_messages_by_run(self, thread_id, run_id, *, limit=50, before_seq=None, after_seq=None, scope=None):
        # Per-run, messages-only, seq-sorted: the seq window is a contiguous
        # slice located with bisect (O(log m_run)) over only this run's
        # messages, instead of re-scanning the whole thread's event log.
        messages = [e for e in self._messages_by_run.get(thread_id, {}).get(run_id, []) if self._matches_scope(e, scope)]
        lo = 0 if after_seq is None else bisect.bisect_right(messages, after_seq, key=lambda e: e["seq"])
        hi = len(messages) if before_seq is None else bisect.bisect_left(messages, before_seq, key=lambda e: e["seq"])
        window = messages[lo:hi]
        # An ``after_seq`` cursor pages forward (first ``limit``); otherwise
        # return the last ``limit`` (the latest page, or the page ending just
        # before ``before_seq``). Matches the prior filter-based semantics.
        if after_seq is not None:
            return window[:limit]
        return window[-limit:]

    async def count_messages(self, thread_id, *, scope=None):
        return sum(1 for e in self._messages.get(thread_id, []) if self._matches_scope(e, scope))

    async def delete_by_thread(self, thread_id, *, scope=None):
        events = self._events.get(thread_id, [])
        removed = [event for event in events if self._matches_scope(event, scope)]
        remaining = [event for event in events if not self._matches_scope(event, scope)]
        self._events[thread_id] = remaining
        self._messages[thread_id] = [e for e in remaining if e["category"] == "message"]
        for run_id in {event["run_id"] for event in removed}:
            run_remaining = [event for event in remaining if event["run_id"] == run_id]
            if run_remaining:
                self._events_by_run.setdefault(thread_id, {})[run_id] = run_remaining
                self._messages_by_run.setdefault(thread_id, {})[run_id] = [e for e in run_remaining if e["category"] == "message"]
            else:
                self._events_by_run.get(thread_id, {}).pop(run_id, None)
                self._messages_by_run.get(thread_id, {}).pop(run_id, None)
        if not self._events_by_run.get(thread_id):
            self._events_by_run.pop(thread_id, None)
        if not self._messages_by_run.get(thread_id):
            self._messages_by_run.pop(thread_id, None)
        if not remaining:
            self._events.pop(thread_id, None)
            self._messages.pop(thread_id, None)
            self._seq_counters.pop(thread_id, None)
        return len(removed)

    async def delete_by_run(self, thread_id, run_id, *, scope=None):
        all_events = self._events.get(thread_id, [])
        if not all_events:
            return 0
        remaining = [e for e in all_events if not (e["run_id"] == run_id and self._matches_scope(e, scope))]
        removed = len(all_events) - len(remaining)
        self._events[thread_id] = remaining
        # Keep the message projection in lockstep (same surviving dict objects).
        self._messages[thread_id] = [e for e in remaining if e["category"] == "message"]
        # Drop the deleted run from the run-keyed projections.
        run_remaining = [e for e in remaining if e["run_id"] == run_id]
        if run_remaining:
            self._events_by_run.setdefault(thread_id, {})[run_id] = run_remaining
            self._messages_by_run.setdefault(thread_id, {})[run_id] = [e for e in run_remaining if e["category"] == "message"]
        else:
            self._events_by_run.get(thread_id, {}).pop(run_id, None)
            self._messages_by_run.get(thread_id, {}).pop(run_id, None)
        return removed
