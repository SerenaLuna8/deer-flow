"""Child process that exercises the production scheduler ownership primitive.

The release test launches two copies of this module.  It deliberately contains
no alternate lock implementation: every attempt uses
``AutomationSchedulerOwnership`` and therefore a distinct PostgreSQL session.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time

from sqlalchemy.ext.asyncio import create_async_engine

from app.automations.errors import AutomationUnavailable
from app.automations.ownership import AutomationSchedulerOwnership


async def _run(database_url: str, deadline_seconds: float) -> None:
    engine = create_async_engine(database_url)
    ownership = AutomationSchedulerOwnership(engine)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)

    deadline = time.monotonic() + deadline_seconds
    reported_contention = False
    try:
        while True:
            try:
                await ownership.acquire()
            except AutomationUnavailable:
                if not reported_contention:
                    print(json.dumps({"state": "contended"}), flush=True)
                    reported_contention = True
                if time.monotonic() >= deadline:
                    raise TimeoutError("scheduler ownership was not acquired")
                await asyncio.sleep(0.05)
                continue
            print(
                json.dumps(
                    {
                        "state": "owned",
                        "backend_pid": ownership.backend_pid,
                    }
                ),
                flush=True,
            )
            await stop.wait()
            return
    finally:
        await ownership.release()
        await engine.dispose()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m tests.support.m6_scheduler_ownership_child DATABASE_URL")
    asyncio.run(_run(sys.argv[1], 20.0))


if __name__ == "__main__":
    main()
