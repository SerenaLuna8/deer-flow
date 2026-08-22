"""Cross-thread change signal for lifecycle-owned Sub-Agent Task state.

Subagent graphs mutate their internal result holder from the lifecycle's
isolated event-loop thread, while the task Adapter waits on the parent owner
loop. The lifecycle keeps a heartbeat as a staleness bound but wakes waiters
the moment something actually changes.

Design points:

- ``notify`` is thread-safe and marshals each wake-up through
  ``loop.call_soon_threadsafe`` onto the waiter's own loop.
- Terminal notifications always fire immediately; that is the latency win.
- Non-terminal notifications (progress messages) are debounced (default 1s)
  so a chatty subagent cannot generate an event storm — a suppressed
  notification is picked up by the heartbeat, exactly like before.
- The terminal state is latched: subscribing after the terminal transition
  returns an already-set event, so a waiter can never sleep through a
  completion that happened between its state read and its subscribe call.
"""

from __future__ import annotations

import asyncio
import threading
import time

_DEFAULT_DEBOUNCE_SECONDS = 1.0


class SubagentChangeSignal:
    """Wake event-loop waiters when lifecycle graph state changes."""

    __slots__ = ("_debounce_seconds", "_last_notify", "_lock", "_terminal", "_waiters")

    def __init__(self, *, debounce_seconds: float = _DEFAULT_DEBOUNCE_SECONDS) -> None:
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        self._debounce_seconds = debounce_seconds
        self._last_notify = float("-inf")
        self._terminal = False

    def subscribe(self) -> asyncio.Event:
        """Register the calling event loop for wake-ups.

        Must run on the loop that will await the returned event.
        """

        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        with self._lock:
            self._waiters.append((loop, event))
            if self._terminal:
                # Latch: a terminal transition before subscribe is never missed.
                event.set()
        return event

    def unsubscribe(self, event: asyncio.Event) -> None:
        with self._lock:
            self._waiters = [(loop, waiter) for loop, waiter in self._waiters if waiter is not event]

    def notify(self, *, terminal: bool = False) -> None:
        """Wake every subscribed waiter; safe from any thread.

        Non-terminal notifications inside the debounce window are dropped —
        the heartbeat bounds their staleness. Terminal notifications always
        deliver and latch the signal for late subscribers.
        """

        now = time.monotonic()
        with self._lock:
            if terminal:
                self._terminal = True
            else:
                # ``mark_running`` commonly fires before task_tool subscribes.
                # With nobody to wake there is no event storm to debounce, and
                # consuming the window here would suppress the first real
                # progress notification after subscription for up to a full
                # heartbeat.
                if not self._waiters:
                    return
                if now - self._last_notify < self._debounce_seconds:
                    return
            self._last_notify = now
            waiters = list(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # The waiter's loop already closed; nothing to wake.
                continue


async def wait_for_change(event: asyncio.Event, *, heartbeat_seconds: float) -> bool:
    """Wait for the next change or one heartbeat, whichever comes first.

    Returns ``True`` when woken by a change, ``False`` on heartbeat timeout.
    Callers clear the event before re-reading state (clear → read → wait), so
    a change racing with the state read re-sets the event and the next wait
    returns immediately instead of sleeping a full heartbeat.
    """

    if heartbeat_seconds <= 0:
        return event.is_set()
    try:
        await asyncio.wait_for(event.wait(), timeout=heartbeat_seconds)
    except TimeoutError:
        return False
    return True


__all__ = ["SubagentChangeSignal", "wait_for_change"]
