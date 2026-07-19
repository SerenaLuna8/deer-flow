# Durable Project Streaming

M6+ streaming is PostgreSQL durable and project-scoped.

1. Gateway atomically admits a private Run and durable job.
2. Worker claims the job, executes the admitted graph and appends ordered stream events.
3. Gateway reads events by account/project/thread/run cursor and emits SSE.
4. Reconnect supplies the last accepted cursor; the reader resumes without replaying deduped events.
5. Every terminal Run has exactly one terminal stream invariant.

The frontend stores cursor and dedupe state under the exact account/project/thread query key. Account or project transitions abort in-flight reads and remove old state. There is no in-memory or Redis production stream fallback.

Worker/Gateway process-boundary tests cover reconnect, terminal events, stale cursors and cross-project denial.
