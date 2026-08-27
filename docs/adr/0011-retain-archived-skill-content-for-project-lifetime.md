---
status: accepted
---

# Retain archived Skill content for the Project lifetime

Deleting a Project Skill is permanent archival only: it hides the Skill,
prevents future Run Admission from resolving it, and removes every Project Agent
binding without changing Agent lifecycle status. The archive retains the Current
Version pointer, every Version and file, quota reservation, Secret state,
Generations, and ciphertext for as long as the Project exists. Deleting Runs or
Threads and former-owner or account-private retention never removes that
content, and no Skill-specific physical purge or periodic reconciler exists.
Final deletion of the whole Project is the sole boundary that destroys the
archived Skill closure and releases its quota.

This accepts permanent Skill storage consumption within a live Project in
exchange for a single deletion contract, continued execution and retry of
already admitted Runs, and removal of reference-counted cleanup and secret-loss
failure modes. It supersedes ADR-0006's and ADR-0009's immediate Skill-secret
destruction and archived-Skill purge clauses.
