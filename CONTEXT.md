# ActWeave

ActWeave is a project-first system for governing reusable Agent capabilities and their execution.

## Agent assets

**Agent**:
A governed reusable execution definition. An Agent asset is distinct from the Lead Agent role it may take during a Run.
_Avoid_: Lead Agent

**Project Agent**:
An Agent authored and governed within one Project through one mutable Agent Definition. It may reference Project Skill and System Skill assets by stable asset identity.

**System Agent**:
A platform-governed Agent with one immutable definition that may reference only System Skill assets. Projects can use it but cannot create or save its definition.

**Agent Definition**:
The one mutable authored definition of a Project Agent. A successful save replaces what future Run Admission resolves, advances the Agent revision, and rotates an opaque Definition identity, while admitted Runs retain their exact earlier definition in their Run Snapshots. The Definition identity is an execution-generation fact, not a user-visible Version or retained history.
_Avoid_: Agent Version, Current Agent Version

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
The final operation that validates a ready design and creates its suspended Project Agent with its initial Agent Definition. Its real validation and persistence stages may emit Agent Design Activities, but a manual blueprint edit never represents model reasoning.
_Avoid_: Agent Version Activation

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

**Skill Deletion**:
The irreversible archival of a Project Skill that hides it from project use, prevents future Run Admission from resolving it, and removes it from every Project Agent Definition without changing those Agents' lifecycle status. Its Current Version, Versions, files, storage allocation, and Configuration Secrets remain part of the Project until the whole Project is finally deleted.
_Avoid_: Skill Suspension, immediate physical purge

**System Skill**:
A platform-governed Skill installed once as an immutable v1. Projects can use it but cannot create, save, activate, or replace its definition; changed content requires a different System Skill identity.

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

## Knowledge assets

**Knowledge Base**:
A Project-shared collection of Knowledge Documents. An empty Knowledge Base may start unconfigured, with no embedding Provider Model. Before its first document upload, it binds one embedding Provider Model for indexing and search and optionally one reranker Provider Model for result ordering. Unconfigured empty bases do not participate in retrieval.
_Avoid_: Dataset, vector table

**Knowledge Document**:
A source file uploaded to one Knowledge Base. It owns the stored file location, processing status, metadata values, segments, and embeddings.
_Avoid_: Source Blob, upload row

**Knowledge Segment**:
A stable, ordered text chunk derived from one version of a Knowledge Document.
_Avoid_: random chunk

**Knowledge Segment Child**:
A finer-grained chunk derived from one Knowledge Segment in parent-child mode. Children carry the vectors used for recall; a hit rolls up to its parent Segment for reranking and citation.
_Avoid_: child segment, sub-document

**Knowledge Metadata Field**:
A Knowledge Base–scoped custom field definition typed as string, number, or time. Knowledge Documents hold typed values for these fields, and retrieval filters on them.
_Avoid_: tag, label, free-form attribute

**Knowledge Task**:
A durable background work item for document ingestion or deletion.
_Avoid_: Run, Job, fire-and-forget background task

**Knowledge Citation**:
A reference to the Knowledge Base, Knowledge Document, and Knowledge Segment that supplied one search result.
_Avoid_: complete source document

**Knowledge Query**:
A logged retrieval request against a Project's Knowledge Bases, recording its source, result count, and top score for hit statistics.
_Avoid_: chat message, Run

**Model Provider**:
A platform-administered retrieval-model vendor in the host model registry: a display name, an OpenAI-compatible endpoint, and one write-only API key held as its Configuration Secret. It owns Provider Models. It is not a System Model Configuration and carries no `provider_adapter`; those concepts belong to Agent model execution.
_Avoid_: Knowledge Model Configuration, provider adapter, System Model Configuration

**Provider Model**:
One typed model under a Model Provider: embedding (with a fixed vector dimension) or reranker. An unconfigured empty Knowledge Base has no model binding. Its initial configuration binds one embedding Provider Model; after that, this binding changes only through rebuild and cannot be cleared. It may also bind at most one reranker Provider Model, effective on save. A Provider Model referenced by any Knowledge Base is in use, and neither it nor its Model Provider can be disabled or deleted.
_Avoid_: Knowledge Model Configuration, model configuration row

## Configuration secrets

**Configuration Secret**:
Secret material owned by exactly one model, Skill, MCP, or Channel configuration. It is protected at rest, cannot be managed or reused independently, and may be materialized only inside that configuration's authorized execution boundary or an authorized server-side re-encryption boundary.
_Avoid_: Credential, shared secret asset

**Configuration Secret Generation**:
One immutable protected value for a Configuration Secret at a point in its replacement history. When its ciphertext is destroyed, only a Secret Tombstone remains.
_Avoid_: Credential Version, latest secret

**Secret Envelope**:
The business-neutral authenticated-encryption representation stored inside a consuming domain's Configuration Secret Generation. It contains ciphertext and a random nonce bound to the exact recipient identity, but owns no catalog, permission, lifecycle, or reusable secret identity of its own.
_Avoid_: Credential Envelope, shared secret record

**Current Secret Generation**:
The Configuration Secret Generation selected by a configuration for its next authorized resolution boundary. Run Admission selects and pins the exact Model, Skill, and MCP Generation reference without decrypting it; a Worker materializes it only at the authorized execution boundary. Channel delivery instead selects the Channel Instance's then-current Generation.
_Avoid_: Latest Credential, active secret version

**Secret Readiness**:
Whether a configuration has every required Configuration Secret needed for new execution. It is the aggregate of per-slot `configured` status and is independent of higher-level runtime readiness such as MCP discovery and lifecycle state such as Current, active, default, or enabled.
_Avoid_: Lifecycle enabled

**Secret Tombstone**:
The non-secret identity and integrity metadata retained after a Configuration Secret Generation's ciphertext is destroyed. It can explain an exact historical reference but cannot materialize the secret.
_Avoid_: Deleted Credential, archived secret

**System Model Configuration**:
A platform-administered model endpoint and capability definition with a stable identity and no version history. It owns an independently replaceable API Key as its Configuration Secret.
_Avoid_: System Model Version, model Credential

## Skill versions

**Current Version**:
The one Skill definition resolved for future Run Admission. A Project Skill selects it through Version Activation; a System Skill always uses its sole v1.
_Avoid_: Active Version

**Candidate Version**:
A saved, immutable Project Skill Version on the forward lineage after the Current Version and still eligible for activation.
_Avoid_: Editable Version

**Historical Version**:
A persisted Project Skill Version that can no longer become the Current Version. It may remain referenced by exact Run Snapshots admitted while it was current.
_Avoid_: Rollback Version

**Version Activation**:
The forward-only selection of a Candidate Version as the Current Version together with enabling its Project Skill. Future Run Admission resolves the new Current Version, and any bypassed Candidate Versions become Historical Versions.
_Avoid_: Release, Rollback

**Asset Suspension**:
The temporary prevention of new execution for a Project Agent or Project Skill without changing its Agent Definition or Skill Current Version.
_Avoid_: Version deactivation

**System Asset Upgrade**:
The maintenance replacement of a System Agent's sole definition at the same deterministic identity. It affects only Runs admitted after the replacement; immutable System Skills are excluded.

**System Governance Eligibility**:
Whether a System Skill's current v1 definition is permitted for new Run Admission. Security revocation removes eligibility without becoming a version lifecycle state.

## Agent execution

**Thread**:
The durable conversation-continuity boundary within a Project. A Thread owns an ordered history of Runs and their shared graph state; each new Run resolves its Agent Definition and the Current Versions of its Skill assets.
_Avoid_: Chat, Session

**Thread Deletion**:
An irreversible owner action that hides a Thread and revokes its active execution while retaining its Checkpoint, files, Artifacts, and admitted Runs until an explicit retention cleanup. Retained files continue to consume storage quota, and deletion alone does not create a restore surface.
_Avoid_: Thread purge, recoverable archive

**Run**:
One admitted request to advance a Thread under a fixed execution definition. A Run owns the user-visible business outcome and may span multiple Job Attempts.
_Avoid_: Job, Attempt, Graph Turn

**Run Admission**:
The acceptance boundary that fixes what a Run may execute and pairs the Run with durable work. Admission is distinct from execution.
_Avoid_: Harness Execution

**Run Snapshot**:
The immutable execution closure authorized for one Run: exact Agent and MCP payloads, a copied System Model Configuration, exact Skill Versions, Configuration Secret Generation references, and runtime policy. A Skill's file bytes are retained once by its immutable Version and pinned by the Run's manifest plus database reference; retry, resume, and replay never resolve Current Version again. The closure does not preserve destroyed secret material, and Channel delivery secrets are resolved separately from the current Channel configuration.
_Avoid_: Current configuration

**Run Workload Profile**:
The server-confirmed `interactive` or `research` workload policy frozen into one Run Snapshot. It selects bounded Sub-Agent workload limits, is inherited by hidden Graph Turns and Job Attempts of that Run, and never grants additional tools, project capabilities, or data authority.
_Avoid_: Agent mode, permission profile, model-selected research

**ToolCallControl**:
The single Harness arbitration seam that applies frozen repeated-call policy and internal tool-call hard limits before ToolNode execution. Under Runtime Policy v6, one Lead binding counts across its Run, including each `task` delegation call, while every Sub-Agent Task execution binding owns a separate count; parallel Tasks never consume one another's limit and there is no additional Run-wide aggregate. Replay receipts remain idempotent within the exact binding. Already admitted Runs and retained v2–v5 policies preserve their frozen legacy count shared by the Lead and all Sub-Agent Tasks. ToolCallControl owns count binding and replay receipts, but does not own Sub-Agent concurrency, Token Budget, tool authorization, or approval authority.
_Avoid_: Loop detector, tool permission policy

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
A delegated Agent execution inside its parent Run. It may have its own graph, tools, model, and other execution constraints. Under Runtime Policy v6 it owns an internal tool-call count distinct from the Lead and every sibling Task while retaining the parent Run's authority; retained v2–v5 policies keep the legacy shared Run count. It is not a Run, Job, or Job Attempt.
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

**Context Subject**:
The Lead Thread or one Sub-Agent Task execution whose model-visible context is measured independently. Sibling Sub-Agent Tasks never share one Context Subject.
_Avoid_: Agent, Run-wide Token total

**Context Window**:
The model-visible retained context for one Context Subject at a point in its execution continuity. It is distinct from cumulative Run usage and billing.
_Avoid_: Token usage, Run total

**Context Window Generation**:
One continuity generation of a Context Window between whole-history replacements. Compaction, rollback, branching, or another replacement begins a new generation without erasing prior measurement facts.
_Avoid_: Run, Session

**Context Evidence**:
An immutable, safely minimized fact about request admission, Provider observation, or compaction for one Context Window Generation. It records measurement provenance without containing conversation content.
_Avoid_: Run Event, Checkpoint, raw Prompt

**Context Projection**:
A rebuildable reading of current context occupancy, quality, capacity, and compaction state for one Context Subject. It may combine exact and estimated Context Evidence and never grants execution authority.
_Avoid_: Context Evidence, Provider receipt, execution guard

**Context Contribution**:
One uniquely owned piece of model-visible material assigned to exactly one stable Context Projection lane. Runtime settings that do not shape a model request are not Context Contributions.
_Avoid_: Runtime Policy, duplicated Token category

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
