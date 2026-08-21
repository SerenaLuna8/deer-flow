# ActWeave

ActWeave is a project-first system for governing reusable Agent capabilities and their execution.

## Agent assets

**Agent**:
A governed reusable execution definition. An Agent asset is distinct from the Lead Agent role it may take during a Run.
_Avoid_: Lead Agent

**Project Agent**:
An Agent authored and governed within one Project. It may reference Project Skill and System Skill assets by stable asset identity.

**System Agent**:
A platform-governed Agent with one Current Version numbered v1 whose definition may reference only System Skill assets. Projects can use it but cannot create, save, or activate its versions.

**Project Agent Version**:
An immutable snapshot of a Project Agent's complete authored definition at one point in its history.
_Avoid_: Editable Agent version

**Agent Design Session**:
An owner-private design conversation that progressively defines one prospective Project Agent and has at most one active Agent Design Generation Turn. It is distinct from a Thread and does not become part of the created Agent's execution history.
_Avoid_: Builder chat, Thread

**Agent Design Activity**:
A durable, ordered, user-visible observation of real work performed while advancing an Agent Design Session, such as model reasoning or a verified design stage. Activities are appended as work happens and remain replayable; they never represent simulated progress.
_Avoid_: Fake progress, Run Event

**Agent Design Generation Turn**:
One model-backed advancement of an Agent Design Session from an owner input through a validated design outcome. It is not a Run or part of the prospective Agent's execution history.
_Avoid_: Run, Graph Turn

**Agent Design Generation Preference**:
The owner's current model and thinking-intensity choice for future Agent Design Generation Turns in one Agent Design Session. Changing it never rewrites an earlier turn's effective configuration.
_Avoid_: Agent model settings, global model preference

**Agent Design Generation Profile**:
The immutable requested and effective model plus thinking intensity recorded for one Agent Design Generation Turn. It affects design generation only and is not part of the prospective Agent's authored definition.
_Avoid_: Agent model settings, Run Execution Profile

**Agent Design Turn Stop**:
An owner-requested terminal outcome that stops the active Agent Design Generation Turn without cancelling its Agent Design Session. Earlier activities remain part of the session history.
_Avoid_: Agent Design Session cancellation

**Agent Design Commit**:
The final operation that validates a ready design and creates its suspended Project Agent plus Candidate Version. Its real validation and persistence stages may emit Agent Design Activities, but a manual blueprint edit never represents model reasoning.
_Avoid_: Version Activation

## Skill assets

**Skill**:
A governed reusable capability used by an Agent during a Run.

**Skill Builder**:
The product surface for creating or revising a Project Skill through an owner-private Skill Design Session.

**Skill Design Session**:
An owner-private authoring conversation for one prospective or existing Project Skill. It may contain multiple Skill Builder Runs but has at most one active Run.
_Avoid_: Builder chat, Thread

**Skill Builder Run**:
A Run admitted within a Skill Design Session to advance its candidate package through model reasoning and governed authoring tools.
_Avoid_: Skill Generation Turn, one-shot generation

**Skill Design Activity**:
A durable, ordered, user-visible projection of real Skill Builder Run, validation, or commit work. It contains only explicitly safe observations and remains distinct from the Run Events that own execution and recovery facts.
_Avoid_: Raw Run Event, simulated progress

**Skill Builder Execution Preference**:
The owner's current model and thinking-intensity choice for future Skill Builder Runs in one Skill Design Session. Changing it never rewrites a prior Run Snapshot or the authored Skill.
_Avoid_: Skill model settings, global model preference

**Skill Builder Run Stop**:
An owner-requested terminal outcome that ends the active Skill Builder Run without cancelling its Skill Design Session. The candidate package returns to its pre-Run baseline while already recorded activities remain visible.
_Avoid_: Skill Design Session cancellation

**Skill Design Commit**:
The final operation that validates a candidate package and saves a Project Skill plus Candidate Version. Its real validation and persistence stages may emit Skill Design Activities, but manual file edits never represent model reasoning.
_Avoid_: Version Activation

**Project Skill**:
A Skill authored and governed within one Project.

**System Skill**:
A platform-governed Skill with one Current Version numbered v1. Projects can use it but cannot create, save, or activate its versions.

**Project Skill Version**:
An immutable snapshot of all files that comprise a Skill at one point in its history.
_Avoid_: Revision, current Skill

**Skill Distribution Package**:
A portable, installable package derived from one exact Project Skill Version or a System Skill's Current Version after standard distribution exclusions are applied. It contains only distributable Skill files, without platform-private state or version history.
_Avoid_: Skill backup, ActWeave export bundle

**Skill Distribution Exclusions**:
The project-wide policy that classifies development-only directories and generated artifacts as outside a Skill Distribution Package.
_Avoid_: Package cleanup

**Skill Export**:
The read-only creation of a Skill Distribution Package from one persisted Project Skill Version or a System Skill's Current Version; it may omit standard non-distribution files but does not otherwise change the Skill or its governance state.
_Avoid_: Backup, lifecycle archive

## Agent and Skill versions

**Current Version**:
The one Agent or Skill definition resolved for future Run Admission. A Project asset selects it through Version Activation; a System Agent or Skill always uses its sole v1.
_Avoid_: Active Version

**Candidate Version**:
A saved, immutable Project Agent or Project Skill Version on the forward lineage after the Current Version and still eligible for activation.
_Avoid_: Editable Version

**Historical Version**:
A persisted Project Agent or Project Skill Version that can no longer become the Current Version. It may remain referenced by exact Run Snapshots admitted while it was current.
_Avoid_: Rollback Version

**Version Activation**:
The forward-only selection of a Candidate Version as the Current Version together with enabling its Project Agent or Project Skill. Future Run Admission resolves the new Current Version, and any bypassed Candidate Versions become Historical Versions.
_Avoid_: Release, Rollback

**Asset Suspension**:
The temporary prevention of new execution for a Project Agent or Project Skill without changing its Current Version.
_Avoid_: Version deactivation

**System Asset Upgrade**:
The maintenance replacement of a System Agent or System Skill's sole v1 definition at the same deterministic version identity. It does not create another version and affects only Runs admitted after the replacement.

**System Governance Eligibility**:
Whether a System Skill's current v1 definition is permitted for new Run Admission. Security revocation removes eligibility without becoming a version lifecycle state.

## Agent execution

**Thread**:
The durable conversation-continuity boundary within a Project. A Thread owns an ordered history of Runs and their shared graph state; each new Run resolves the Current Versions of its Project and System Agent/Skill assets.
_Avoid_: Chat, Session

**Run**:
One admitted request to advance a Thread under a fixed execution definition. A Run owns the user-visible business outcome and may span multiple Job Attempts.
_Avoid_: Job, Attempt, Graph Turn

**Run Admission**:
The acceptance boundary that fixes what a Run may execute and pairs the Run with durable work. Admission is distinct from execution.
_Avoid_: Harness Execution

**Run Snapshot**:
The immutable, self-contained set of exact Agent and Skill definitions plus model, MCP, Credential, and runtime policy authorized for a Run. Later Version Activation or System Asset Upgrade cannot change it.
_Avoid_: Current configuration

**Job**:
The durable dispatch unit that carries an admitted Run to a Worker. A Job owns scheduling, retry, and dead-letter state, but not the user-visible conversation outcome.
_Avoid_: Run

**Job Attempt**:
One lease-bound Worker try for a Job. Retrying a Job creates a new Job Attempt while preserving the same Run and Run Snapshot.
_Avoid_: Retry Run

**Execution Lease**:
The temporary, exclusive authority held by one Job Attempt to mutate its Run's execution state. Loss of the Execution Lease ends that Attempt's authority even if local work is still running.
_Avoid_: Worker ownership

**Harness Execution**:
The in-process execution of an admitted Run by one Job Attempt, from runtime materialization through graph and resource finalization. Harness Execution is inside the wider Run lifecycle and excludes Run Admission and Run Settlement.
_Avoid_: Run lifecycle

**Agent Graph**:
The executable state machine that coordinates the Lead Agent, models, middleware, tools, checkpoints, and delegated work for a Harness Execution.
_Avoid_: Run

**Graph Turn**:
One invocation of the Agent Graph from an input until it finishes, pauses, or fails. A Run can contain more than one Graph Turn without becoming more than one Run.
_Avoid_: User turn, Run

**Lead Agent**:
The root Agent in a Run that owns the user-facing response and may delegate bounded work.
_Avoid_: Harness

**Sub-Agent Task**:
A delegated Agent execution inside its parent Run. It may have its own graph, tools, model, and budget, but inherits the parent Run's authority and is not a Run, Job, or Job Attempt.
_Avoid_: Child Run, background Job

**Goal Continuation**:
A server-driven additional Graph Turn in the same Run when an active goal still requires work.
_Avoid_: Follow-up Run

**Continuation Run**:
A newly admitted Run linked to a prior Run so execution can continue after a durable human-input or host-execution boundary. A Continuation Run does not reopen the source Run.
_Avoid_: Resumed source Run

**Run Event**:
A durable, ordered observation emitted during a Run for replay, progress, and diagnostics. A Run Event describes execution; it is not graph state.
_Avoid_: Checkpoint

**Checkpoint**:
A durable snapshot of materialized Agent Graph state used for continuity, resume, and rollback.
_Avoid_: Run Event, audit log

**Host Execution Suspension**:
A checkpoint-safe boundary created when a host command requires approval. The source Run terminates with a sealed suspension, and an approved command is handled by a linked Continuation Run.
_Avoid_: Paused Job Attempt

**File Finalization**:
The required validation and durable publication of a Run's file changes and output-delivery obligations before the Run can succeed.
_Avoid_: Cleanup

**Run Settlement**:
The authoritative convergence of Run, Job, Job Attempt, usage, quota, audit, approval, and output-delivery outcomes after Harness Execution or terminal recovery.
_Avoid_: Stream end

**Retry Safety**:
The classification of whether another Job Attempt may repeat work without risking an untracked duplicate external side effect. Retry Safety is independent of whether the underlying error is transient.
_Avoid_: Retryable error
