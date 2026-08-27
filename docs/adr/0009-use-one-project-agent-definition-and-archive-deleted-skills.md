---
status: accepted
---

# Use one Project Agent Definition and archive deleted Skills

> **Partially superseded:** ADR-0011 replaces this ADR's immediate Skill-secret
> destruction and archived-Skill purge clauses. ADR-0010 separately replaces its
> former Thread private-state cleanup clause.

A Project Agent owns one mutable Agent Definition rather than a
Current/Candidate/Historical Version lineage. Saving that Definition immediately
changes future Run Admission under optimistic revision control. Each successful
mutation rotates an opaque Definition identity, advances the Agent revision, and
recomputes the payload checksum. That identity is an execution-generation fact,
not a user-visible Version or a retained history record. Each admitted Run keeps
the exact Agent payload and Definition identity in its immutable Run Snapshot.

Project and System Agent definition fields live with the Agent aggregate.
Project Agents own mutable Skill and MCP reference rows keyed by stable Agent
identity. System Agents keep one platform-managed definition and remain
unwritable through Project APIs. Project Agent instruction and capability saves
replace the current definition in place; there is no Candidate activation or
Historical Version API. Creating an Agent through Agent Design Commit still
creates it suspended, while editing an existing active or suspended Agent never
changes that lifecycle status.

A Project Skill remains versioned. Deletion is an irreversible transition to
`archived`, not immediate physical removal. It hides the Skill from every
ordinary list and detail surface, prevents new Run Admission from resolving it,
and removes its reference from every affected Project Agent Definition in the
same Project-governed transaction. Affected Agents keep their existing
`active` or `suspended` status. Their Definition identities, revisions, and
checksums advance without creating or activating an Agent Version. Only
non-archived Skills participate in Project slug and display-name uniqueness, so
a deleted name may be used by a newly created Skill while the tombstone remains.

Run Admission and Skill deletion serialize through the same Project governance
fence. Admission that commits first retains a complete pre-deletion closure;
deletion that commits first causes the next admission to read the complete
post-deletion Agent Definition. Partial dependency closures are not valid.

Per ADR-0011, logical deletion preserves the Project Skill's Current Version
pointer, every immutable Version and file, quota reservation, Secret state,
Generations, and ciphertext. It creates no Skill-specific physical-cleanup
eligibility, regardless of whether a retained Run still references a Version.

Terminal Run deletion, Thread deletion, and former-owner or account-private
retention do not remove archived Skill content, and no periodic reconciler scans
for it. Only final deletion of the whole Project destroys the archived Skill
closure and releases its quota. ADR-0010 separately specifies that Thread
Deletion retains the Thread's private state and admitted Runs.

This trades Skill-scoped storage reclamation for a simple permanent archive
boundary and deterministic execution, retry, and replay of admitted Runs without
preserving obsolete Agent versions.
