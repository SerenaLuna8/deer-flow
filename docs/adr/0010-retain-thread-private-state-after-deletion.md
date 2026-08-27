---
status: accepted
---

# Retain Thread private state after deletion

Thread Deletion hides the Thread and revokes active execution but retains its Checkpoint, files, Artifacts, admitted Runs, and file quota reservation; ordinary reads continue to require an active Thread. Only failed-create or branch compensation and explicit project, account, or former-owner retention may physically clear that private state. This keeps user deletion logically distinct from purge at the cost of retained storage and PostgreSQL growth, and supersedes the Thread checkpoint/private-presentation cleanup clause in ADR-0009.

Foreground create or branch compensation physically purges only when the exact-generation tombstone, branch-authority rollback, file/quota cleanup, raw checkpoint cleanup, and final metadata purge all succeed. If any step cannot be proved or completed, compensation fails closed and leaves a hidden retained tombstone for explicit retention cleanup; it does not infer destructive provenance or start a separate metadata/file purge from the raw-checkpoint reconciler. Tombstones that already recorded `pending` or `retry_required` before this decision remain grandfathered raw-checkpoint cleanup requests because their business-delete versus compensation provenance cannot be reconstructed; already-removed checkpoints cannot be recovered.
