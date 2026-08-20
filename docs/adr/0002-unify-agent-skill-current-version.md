# ADR-0002: Unify Agent and Skill Current Version semantics

- Status: Accepted
- Date: 2026-08-21

## Context

Agent and Skill assets previously combined an asset lifecycle, a separate
version workflow, and a live pointer. The same action was described differently
for Project and System assets, while Agent-to-Skill references pinned exact
Skill versions. Continued Threads could therefore be difficult to reason about
after an operator changed an asset.

MCP governance is outside this decision. Its approval workflow and exact
configuration bindings remain unchanged.

## Decision

Project and System Agent/Skill assets expose `current_version_id`.

- Saving a Project Agent or Skill creates an immutable Candidate Version without
  changing runtime behavior.
- Activating a Candidate Version moves `current_version_id` forward and enables
  the asset in one atomic operation. Versions earlier than the selected version,
  skipped candidates, rejected imports, and old branches are Historical Versions.
- A new version can be authored only from the latest forward head. Historical
  content cannot be edited, deleted, reactivated, copied into a new candidate, or
  used to manufacture a content rollback with a higher version number.
- Asset suspension remains an independent emergency stop. Re-enabling a
  suspended asset keeps the same Current Version.
- Editor and Admin may save and activate versions and enable or suspend assets.
  Admin alone manages Credential material. Editor sees readiness metadata and
  may activate a ready candidate.
- Skill Credential mappings are inherited into a forward Candidate Version when
  declarations remain compatible. Activation revalidates the exact candidate,
  required mappings, checksums, model/tool availability, dependency closure, and
  runtime-name uniqueness.
- Agent versions bind Skills by `{scope, asset_id}`. Run Admission resolves every
  referenced Skill asset's Current Version. MCP references remain exact
  configuration-version references.

Every Run Admission resolves the selected Agent asset and all Agent/Skill
dependencies from their Current Versions, including later messages in an
existing Thread, edited/regenerated messages, forks, Automations, and Channels.
Admission persists a complete immutable Run Snapshot. A Worker executes only
that snapshot and never rereads Current Versions during execution or retry.

System Agent and Skill assets have exactly one version, v1. Installation makes
v1 current automatically. Users cannot create, save, or activate System
versions. `make upgrade-system-assets` replaces the authenticated v1 definition
in place, keeps the same version number and deterministic version identity, and
never appends history. A changed System Skill package clears a prior governance
revocation because it is a new authenticated definition; an idempotent same-byte
upgrade preserves the revocation. Revocation remains admission eligibility, not
a version lifecycle state.

## Migration

- Existing live Agent/Skill pointers become `current_version_id`.
- A structurally valid forward descendant after the Current Version becomes a
  Candidate Version.
- Earlier versions, old branches, and former approval/rejection records become
  Historical Versions without deleting bytes or audit history.
- If no current pointer exists, the valid forward head is eligible for first
  activation.
- Existing Run Snapshots are not modified. Future Runs, including later messages
  in existing Threads, resolve Current Versions at admission.
- System assets are normalized to their authenticated v1 definition as the sole
  Current Version.

## Consequences

The version model has one runtime pointer and one forward-only activation
operation. Runtime behavior can change between Runs in the same Thread, but never
inside a Run. Historical audit records remain complete, while rollback through
either pointer movement or content copying is unavailable.
