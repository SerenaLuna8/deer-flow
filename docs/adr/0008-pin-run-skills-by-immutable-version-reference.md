---
status: accepted
---

# Pin Run Skills by immutable Version reference

Run Admission keeps a referentially complete Skill closure. A new Skill Run
Snapshot stores a small version-4 manifest plus an exact
`run_skill_version_refs` row; the authenticated file bytes remain owned once by
the immutable `skill_versions` / `skill_version_files` graph. Foreign keys,
closure seals, pin-first mutation guards, and execution-time checksum
verification keep the referenced bytes available and unchanged for every
retained Run.

This changes the physical Run Snapshot contract without changing execution
determinism. Admission alone resolves Current Version, Asset Suspension,
bindings, and System Governance Eligibility. Retry, resume, and replay read the
exact pinned Skill Version and never resolve Current Version again. Agent and
MCP payload representation and Configuration Secret ownership are unchanged.

The previous self-contained v2/v3 Skill byte payload remains readable during
the compatibility window. Readers are deployed before the v4 writer switch;
after any v4 Run exists, rollback is limited to a release that still reads v4.
The Schema V1 catalog is recreated explicitly for this disposable development
database. Runtime migration, in-place stamping, and the historical-data
importer are outside this decision.

We rejected keeping a Run-owned immutable Skill bundle because it would retain
one large byte copy per Run and preserve the PostgreSQL TOAST/WAL amplification
that caused the redesign. We also rejected resolving Current Version in the
Worker because it would make retry and replay nondeterministic, and rejected a
new object-store or content-addressed authority because the existing immutable
Version graph already owns the bytes and the extra authority would add
unnecessary operational complexity.

This decision supersedes only the physical "self-contained Skill bytes" and
"Worker decode-only" clauses of ADR-0002 and the backend guide. ADR-0002's
Project Skill Current/Candidate/Historical Version and Version Activation
semantics remain accepted; ADR-0009 supersedes its version semantics for both
Project and System Agents, and ADR-0007's System Skill identity immutability
remains accepted.
