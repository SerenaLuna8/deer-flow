---
status: accepted
---

# Use one Project Agent Definition and archive deleted Skills

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

Logical deletion immediately destroys the Project Skill's Configuration Secret
ciphertext and retains non-secret tombstones. A Run Snapshot pins a Secret
Generation identity but does not copy the ciphertext, so an already admitted
Run that has not materialized the secret, or later retries, fails closed. This
execution cost is accepted in exchange for immediate secret destruction. An
already executing Run that materialized the secret before deletion may finish.

Archived Skill Version files and quota remain owned until no retained Run
references any Version of that Skill. A trusted archived Skill purge then
clears the Current Version pointer, removes files and Version rows, releases
storage quota, and keeps the minimal archived Skill row plus its non-secret
Secret Tombstones.

Individual terminal Run deletion and owner/account retention cleanup can release
the final references and invoke the purger; a low-frequency sweep repairs missed
invocations. Ordinary successful Runs have no age-based expiry. Thread deletion
continues to hide the Thread and remove its checkpoint/private presentation
state, but it does not physically delete its retained Runs or their Run
Snapshots, so it is not a reference-release event. Expanding Thread deletion to
destroy all terminal Run closures would be a separate product decision.

This trades immediate storage reclamation for deterministic execution, retry,
and replay of already admitted Runs without preserving obsolete Agent versions.
