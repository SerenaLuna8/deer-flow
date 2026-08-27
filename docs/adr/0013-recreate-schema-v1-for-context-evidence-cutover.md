---
status: accepted
---

# Recreate Schema V1 for the Context Evidence cutover

The Context Evidence architecture is introduced through an explicit operator rebuild of Schema V1 rather than an application migration or compatibility backfill. Existing database history is intentionally discarded at this cutover, and the runtime must continue to reject an outdated non-empty catalog instead of creating, stamping, repairing, or migrating application tables automatically. The rebuild is a separate destructive operator action; accepting this decision does not authorize a runtime process or ordinary startup path to execute it.

Implementation, the complete Schema V1 snapshot, ORM/catalog parity, documentation, and isolated-database verification must finish before the live target is touched. The cutover drains and stops all writers, presents the exact database target and irreversible inventory, performs one explicit rebuild, starts Gateway, Frontend, Worker, and Scheduler from the matching release, and verifies database readiness, health, Context Projection, final request admission, compaction, and parallel Sub-Agent isolation. No startup script or Worker may perform the rebuild.
