# Secondary Hotspot Module Decomposition (Batch 6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-audit the four secondary hotspots named by spec section 13 and extract only their pure contracts, admission rules, receipt-repair collaborators, and prompt/turn/receipt planners into explicit owner modules, while the Job claim/settlement/requeue state machine, the atomic Run Snapshot admission transaction, Checkpoint write/lease/Approval-deletion control, and the LangChain summarization hooks stay exactly where they are.

**Architecture:** Four independent sub-batches, ordered by risk. `deerflow.persistence.jobs.contracts` receives the 27 pure Job contract names and `sql.py` re-imports them unchanged. `app.private_work.snapshot_contracts` and `snapshot_admission_rules` receive DTOs, Protocols, the stale exception, the admission encoder, pure validators, and the pure asset-row planner that `create_run_with_snapshot_in_session()` keeps calling at the same position between the same two `await`s. `app.private_work.checkpoint_receipt_repair` receives the Memory/Context/Provider checkpoint repair functions and the two config readers while `_ScopedCheckpointSaver` keeps thin delegating methods and every transaction. `deerflow.agents.middlewares.{snip_planner,turn_compaction,compaction_receipts}` receive the snip prompt planner (parameterised by a frozen `SnipPromptBudget`), the pure turn-boundary functions, and the receipt/evidence functions (parameterised by the observer), while `DeerFlowSummarizationMiddleware` keeps `__init__`, the LangChain hooks, model invocation, `_prepare_compaction`, retention predicates, and result assembly.

**Tech Stack:** Python 3.12+, dataclasses, `typing.Protocol`, LangChain `SummarizationMiddleware`, LangGraph `Runtime`/`BaseCheckpointSaver`, SQLAlchemy async sessions (app owners only), pytest, Ruff, repository Make targets.

**Spec:** `docs/superpowers/specs/2026-09-02-python-module-decomposition-design.md`, sections 3, 5, 13, 14-18. Section 13 makes Batch 6 conditional on a fresh audit; the audit results and execute/defer decisions are recorded under "Audit decisions" below and are part of this plan's contract.

## Global Constraints

### Confirmed execution baseline

- Generate and execute this plan only in `/Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations` on branch `codex/python-module-decomposition-foundations`. This plan does not authorize another branch or worktree.
- The audited production baseline is commit `7dcb05c9a5a2d777cba69a820261a7374522874f` (`docs(worker): document execution owners`, the last Batch 5 commit). At plan-generation time the worktree also carries three uncommitted Batch 5 review-polish edits (`backend/packages/harness/deerflow/runtime/serialization.py` docstring, `backend/tests/test_python_module_decomposition_process_boundaries.py` comment, `backend/tests/test_python_module_decomposition_worker_runtime.py` identity assertion), the pre-existing P1 change to `backend/tests/test_skill_builder_durable_agent_postgres.py`, and six user-owned untracked `docs/superpowers/` documents. Before Task 1, the three polish edits must be either committed as Batch 5 closure (suggested message `test(worker): tighten batch 5 compatibility identity and refresh owner docs`, only if explicitly authorized) or preserved byte-for-byte; never sweep them, the P1 test, or the documents into a Batch 6 commit.
- Audited target sizes at the baseline (`wc -l`): `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py` 2168 (63 methods on `DeerFlowSummarizationMiddleware` L266-2035); `backend/app/private_work/snapshot_repository.py` 1971 (`create_run_with_snapshot_in_session()` L902-1383, 482 lines, 19 `await` expressions); `backend/app/private_work/checkpointer.py` 2169 (`_ScopedCheckpointSaver` L265-2059); `backend/packages/harness/deerflow/persistence/jobs/sql.py` 2297 (`JobRepository` L561-2270, `__all__` L2273-2297). Line numbers cited in tasks are these baseline coordinates.
- No `jobs/contracts.py`, `snapshot_contracts.py`, `snapshot_admission_rules.py`, `checkpoint_receipt_repair.py`, `snip_planner.py`, `turn_compaction.py`, or `compaction_receipts.py` exists yet.
- Root `.env` and `config.yaml` are present and ignored. Never print their values. Use `uv run --env-file ../.env` for database-backed gates unless the invoking shell already exports the same approved non-production environment.
- The focused Batch 6 offline baseline for the 32-file set listed in Task 1 Step 6 is `543 passed, 1 skipped, 2 deselected in 12.73s`. The full backend gate at the baseline reports `collected=6566 passed=6566 failed=0 skipped=0` in about 16 minutes. These are plan-generation baselines, not completion evidence; every task requires fresh results.

### Scope boundary

- Production code first. Tests may move imports and monkeypatch targets only with their corresponding production owner, and may add focused compatibility or characterization coverage. Do not split `test_memory_snip_compaction.py`, `test_execution_approval_private_lifecycle_postgres.py`, or any other large test file; do not reorganize test directories or the test framework.
- Batch 6 production scope is limited to:
  - `backend/packages/harness/deerflow/persistence/jobs/sql.py` and new `backend/packages/harness/deerflow/persistence/jobs/contracts.py`;
  - `backend/app/private_work/snapshot_repository.py` and new `backend/app/private_work/{snapshot_contracts,snapshot_admission_rules}.py`;
  - `backend/app/private_work/checkpointer.py` and new `backend/app/private_work/checkpoint_receipt_repair.py`;
  - `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py` and new `backend/packages/harness/deerflow/agents/middlewares/{snip_planner,turn_compaction,compaction_receipts}.py`;
  - focused tests and `backend/AGENTS.md`.
- Do not modify `jobs/{__init__,model}.py`, `app/shared_assets/run_snapshot_codec.py`, `app/private_work/{run_admission,run_repository,thread_service,checkpoint_delete_recovery,context_projection,context_replacement,execution_approval_lifecycle,output_delivery_obligation}.py`, `deerflow/runtime/context_compaction.py`, `deerflow/agents/memory/snip.py`, `deerflow/agents/middlewares/assembly.py`, `deerflow/agents/lead_agent/agent.py`, Schema/DDL, frontend, deployment, or the items listed under "Deferred" below.
- Do not change business rules, error codes or error text, log message text, `ValueError`/`TypeError`/`PrivateWork*`/`ContextProviderCallAmbiguousError`/`RunSnapshotAssetStale` raise sites, dataclass field order, `__post_init__` validation order, Protocol method sets, `sql.__all__`, the order of any `await` in `create_run_with_snapshot_in_session()`, `_atombstone_thread()`, `acleanup_compensated_create()`, `aput()`, or `_aput_execution_atomic()`, any `session.begin()`/`postgres_checkpoint_transaction` boundary, any lock (`with_for_update`, `lock_execution_approval_private_rows`, `revalidator.require(lock=True)`), or the LangChain hook order in `before_model`/`abefore_model`/`compact_state`/`acompact_state`.
- Repositories keep their commit ownership: `create_run_with_snapshot()` remains the only transaction owner in `snapshot_repository.py`; `_locked_active()`, `_atombstone_thread()`, `acleanup_compensated_create()`, and `postgres_checkpoint_transaction()` remain the only transaction boundaries in `checkpointer.py`. New owners never open sessions, never call `commit`, `begin`, or `flush`, and never acquire locks.
- Compatibility re-exports are exact objects. Never wrap, subclass, copy, or emit deprecation warnings. Every name a repository test or production module imports from a legacy module today stays importable from that module as the same object. Class-level private helpers that tests or staying code call (`RunSnapshotRepository._validate_project_mcp_secret_slots`, `_ScopedCheckpointSaver._thread_id`, `DeerFlowSummarizationMiddleware._complete_turn_ranges`, ...) remain class attributes as `staticmethod(owner_function)` aliases or thin delegating methods with the exact legacy signature.
- Re-export aliases cannot propagate monkeypatch assignment into another module's globals. Every test that patches a moved body's own globals moves to the owner in the same task (`snapshot_repository_module.encode_run_asset_snapshot`, `checkpointer_module.ContextEvidenceRepository`, `checkpointer_module.ContextProjectionTransaction`); every seam whose caller stays in the legacy module keeps being resolved from that module's globals (`summarization_module._ensure_snip_summary_output_budget`, `snapshot_module.{AsyncSession,PrivateRunRepository,PrivateThreadRepository,require_active_run_skill_writer_cohort}`, `job_sql.datetime`, `checkpointer_module.transition_output_delivery_obligation_for_approval_terminal`).
- New Harness owners (`jobs/contracts.py`, `snip_planner.py`, `turn_compaction.py`, `compaction_receipts.py`) must never import `app.*`, `sqlalchemy.orm`, `sqlalchemy.dialects`, or their legacy module; `jobs/contracts.py` may import `sqlalchemy.ext.asyncio.AsyncSession` for the Protocol annotations the baseline already carries (Task 1 Step 5 scopes the guard accordingly). New app owners (`snapshot_contracts.py`, `snapshot_admission_rules.py`, `checkpoint_receipt_repair.py`) must never import `snapshot_repository` or `checkpointer`.
- Use `apply_patch` for edits and preserve unrelated changes. No task authorizes destructive reset/checkout, staging, commit, push, merge, or publication. Suggested commits are conditional on explicit local-commit authorization and must add only the listed Batch 6 paths.
- Stop the current task on any identity drift, changed `await` order, changed error code or text, import cycle, new blocking-I/O finding, unexpected production consumer, or unexplained test failure. Revert the complete task with a reviewable patch or discard only its isolated uncommitted paths; never hide a structural regression with a behavior fix.
- If moving code reveals a real behavior defect, record it and request a separate design. Batch 6 must not repair it while changing ownership.

### Audit decisions (spec section 13 re-audit, 2026-09-03)

Executed in this plan:

| Spec | Target | Extract | Keep in place |
| --- | --- | --- | --- |
| 13.4 | `jobs/sql.py` | 27 pure contract names (Literal aliases, `_SHA256_HEX`, nonretryable code set, three pure helpers, 14 dataclasses, 2 Protocols, 3 exceptions, `retry_backoff_seconds`) → `jobs/contracts.py` | `JobRepository`, `_is_durable_terminal_successor`, `_lease_token_hash`, the `weakref`/`Lock` requeue-event registry and dead-terminal cursors, `consume_issued_dead_job_requeued_event`, `__all__` |
| 13.2 | `snapshot_repository.py` | DTOs/Protocols/stale exception/`agent_model_snapshot_purpose` → `snapshot_contracts.py`; encoder, schema-version reader, secret-key rejection, recursion-limit clamp, four pure validators, and the pure asset-row planner (L1214-1337) → `snapshot_admission_rules.py` | `create_run_with_snapshot()` (transaction owner), `create_run_with_snapshot_in_session()` with all 19 `await`s in order, every `_agent/_skills/_mcps/_validate_dependency_order/_mcp_secret_closures/_validate_mcp_discovery_readiness/_admit_memory_context_snapshot/_continuation_workload_profile` I/O staticmethod, secret listing methods, the L1340-1371 lazily evaluated `add_all` generators |
| 13.3 | `checkpointer.py` | Memory/Context/Provider receipt repair (L381-741) and the two config readers `_thread_id`/`_checkpoint_id` → `checkpoint_receipt_repair.py` | `ProjectScopedCheckpointer`, `_ScopedCheckpointSaver` write/read/lease/`_locked_active`/`_aput_execution_atomic` paths, the complete Approval deletion transaction (`_cancel_thread_execution_approval`, `_prepare_thread_execution_approval_deletion`, `_atombstone_thread`, `adelete_thread`, `atombstone_compensated_create`, `acleanup_compensated_create`, `_cleanup_compensated_thread_files`), sync wrappers, `_AlreadyAuthorizedCheckpointSaver` |
| 13.1 | `summarization_middleware.py` | Snip prompt planning and multi-stage reduction planning → `snip_planner.py`; pure turn-boundary and trigger-count helpers → `turn_compaction.py`; archive/receipt/evidence update helpers → `compaction_receipts.py` | `__init__`, `state_schema`, `_get_profile_limits`, `_create_summary`/`_acreate_summary`, `before_model`/`abefore_model`, `_prompt_within_budget`/`_prompt_with_repair_within_budget`, `_invoke_snip_prompt`/`_ainvoke_snip_prompt`, `_reduce_snip_summaries`/`_areduce_snip_summaries`, `_summarize_with`/`_asummarize_with`, `_parse_snip_response`, `measure_*`/`_measure_trigger_usage`, `_count_rendered_summary_prompt_tokens`, `_requested_cutoff`, `_provider_safe_retention_cutoff`, `_compacted_result_fits_provider_profile`, `_overbudget_progress_cutoff`, `_profile_trigger_reached`, `fixed_component_over_trigger`, `_prepare_compaction`, `compact_state`/`acompact_state`, `_maybe_summarize`/`_amaybe_summarize`, `_server_abort_event`, `_context_model_token_counter`, `ContextTriggerUsage`, `ContextUsageMeasurement`, `freeze_summarization_profile`, `create_summarization_middleware` |

Deferred (recorded, not executed; each needs its own design):

- Checkpointer thread-cleanup collaborator: the only pure slice is `_cleanup_compensated_thread_files` (L1925-1984, 60 lines, reads only `self._quota`); `_cancel_thread_execution_approval`, `_prepare_thread_execution_approval_deletion`, `_atombstone_thread`, and `acleanup_compensated_create` are the Approval deletion transaction that section 13.3 pins to the Saver. A 60-line single-function module is a fragment without a business meaning (spec 3.2).
- Summarization `_prepare_compaction` (L1464-1581), `_requested_cutoff`, `_overbudget_progress_cutoff`, `_provider_safe_retention_cutoff`, `_compacted_result_fits_provider_profile`, `_profile_trigger_reached`, `fixed_component_over_trigger`: they read frozen profile state (`keep`, `_trigger_conditions`, `_partial_token_counter`, `_compact_all_complete_turns`) that `freeze_summarization_profile()` mutates after construction and call base-class hooks (`_determine_cutoff_index`, `_should_summarize`, `_ensure_message_ids` which mutates message ids). Tests call `_provider_safe_retention_cutoff` and `_compacted_result_fits_provider_profile` on instances.
- Job `_is_durable_terminal_successor` (claim predicate over an ORM row), `_lease_token_hash` (lease mechanics), and the requeue-event registry: claim/heartbeat/settlement/requeue state machine per section 13.4.
- Snapshot `_validate_dependency_order` (awaits at L748 and L760) and the `RunMcpSecretSnapshotRow`/`RunSkillSecretSnapshotRow` generator expressions passed to `session.add_all` at L1340-1371 (evaluated lazily inside `add_all`; lifting them changes `KeyError` timing).
- Section 13.5 items: `deerflow.subagents.lifecycle`, `run_skill_tree_materializer.py`, Knowledge Retrieval Service, Model Registry Service.

### Audited design refinements

1. `jobs/contracts.py` holds `DeadJobRequeuedEvent` while `_ISSUED_REQUEUE_EVENTS`, its `Lock`, and `consume_issued_dead_job_requeued_event()` stay in `sql.py`; `sql.py` imports the class so `type(value) is not DeadJobRequeuedEvent` (L391) and `object.__new__(DeadJobRequeuedEvent)` (L2230) see exactly one class object. `sql.py` re-imports every moved name, including `RetrySafety` (in `__all__`, imported by `app/reliability/jobs.py:27`) and `_DETERMINISTIC_NONRETRYABLE_ERROR_CODES` (no external consumer), so the `sql` namespace is unchanged; `sql.__all__` stays verbatim. `RetentionPurgeJobAuthority.__post_init__` is the only moved body with a runtime `datetime`/`UTC` reference (L122, L144); after the move the `job_sql.datetime` patch in `test_checkpoint_lease_atomicity_postgres.py:112` no longer reaches that `isinstance`, which no test exercises (the patched path enqueues `private_run`, not `retention_purge`). Record this in the handoff; do not add a seam for it.
2. `snapshot_contracts.py` is the lower leaf (`RunSnapshotAssetStale` and DTO/Protocol types); `snapshot_admission_rules.py` imports it. Every pure validator raises `RunSnapshotAssetStale`, so the exception must live below the rules module and be re-exported by `snapshot_repository` as the same object (12 production importers and `except RunSnapshotAssetStale` at L883 depend on identity). The four validators become public module functions with the class keeping `_asset_allowed = staticmethod(asset_allowed)`, `_validate_project_mcp_secret_slots = staticmethod(validate_project_mcp_secret_slots)`, `_validate_dependency_snapshots = staticmethod(validate_dependency_snapshots)`, `_validate_main_dependency_boundary = staticmethod(validate_main_dependency_boundary)`; the class-qualified calls `RunSnapshotRepository._asset_allowed(...)` at L633/L668/L715 and the `self.` calls at L1528/L1537/L1564/L1575/L1646/L1793 stay byte-identical.
3. The `_RunAssetSnapshotAdmissionEncoder` body resolves `encode_run_asset_snapshot` and `encoded_run_asset_snapshot_json_size` from its defining module's globals; `tests/test_agent_runtime_checksum.py:428,448` patch those names on `snapshot_repository` and construct the encoder through the same module. Both patches and both constructions move to `snapshot_admission_rules` in Task 3. `snapshot_repository` keeps importing `encode_run_asset_snapshot`/`encoded_run_asset_snapshot_json_size` only if a staying body still uses them (none does), so they leave the façade namespace; the migrated test patches the owner.
4. `plan_run_asset_rows()` lifts L1214-1337 verbatim into `snapshot_admission_rules.py` as one function that constructs the encoder internally with `context.request_id`, keeps the `dependency_order` counter, row order (lead → delegated → skills → MCPs), `catalog_generation=lead_agent.catalog_generation` on every row, and the `type(skill_snapshot) is not ResolvedSkillVersionSnapshot` exact-type check. `create_run_with_snapshot_in_session()` calls it exactly where L1214 was, after `await self._admit_memory_context_snapshot(...)` (await #15, L1206) and before `session.add_all(asset_rows)` (L1338). Its parameters are the locals the block reads: `context`, `thread_id`, `run`, `lead_agent`, `resolved_closure`, `skills`, `skill_snapshots`, `mcps`, `mcp_snapshots`, `prepared_legacy_skills`. The cohort assertion at L934-943 stays inline because `tests/test_run_skill_writer_cohort_contract.py:21-22` asserts its source text.
5. `checkpoint_receipt_repair.py` owns `thread_id_from_config()` and `checkpoint_id_from_config()` (the former `_ScopedCheckpointSaver._thread_id`/`_checkpoint_id` staticmethods) because the activation readers need them and a collaborator cannot import `checkpointer` back; the Saver rebinds `_thread_id = staticmethod(thread_id_from_config)` and `_checkpoint_id = staticmethod(checkpoint_id_from_config)` so every `self._thread_id(...)` call in the write path is unchanged and `PrivateWorkNotFound("unknown")` texts stay. Repair functions receive `session`, `context`, `item`, `thread_id` explicitly and never read `self._raw`, `self._revalidator`, `self._thread_kind`, or `self._session_factory`; the Saver keeps seven thin methods with the exact legacy names and signatures so `object.__new__(_ScopedCheckpointSaver)` fixtures that set only `_context` keep working. `_repair_context_provider_checkpoint` passes `observer_run_id=getattr(getattr(self, "_context_evidence_observer", None), "run_id", None)` (the exact L697-701 expression) into the owner.
6. Repair call sites do not move: `aget_tuple` (L787-801), `aget_tuple_already_authorized` (L858-872), `aput` (L996-1010, L1021-1038), `_aput_execution_atomic` (L1088-1102, L1155-1172), and `aput_already_authorized` (L1254-1268, L1279-1293) keep calling the Saver methods on the same session inside the same transaction. In particular the `_aput_execution_atomic` repairs stay in the pre/post app-connection transactions, outside the raw `postgres_checkpoint_transaction`.
7. The whole snip exception hierarchy (`SnipPromptBudgetTooSmall`, `SnipSourceTooLarge`, `SnipCompactionFailed`, `SnipModelOutputInvalid`, L111-137) moves to `snip_planner.py` even though spec 13.1 lists `SnipCompactionFailed` among the middleware's keeps: `turn_compaction` and `compaction_receipts` bodies raise it and may not import the middleware. The middleware re-exports all four as the same objects (`deerflow/runtime/context_compaction.py:19-25` and tests import them from the middleware).
8. Snip prompt planning is parameterised by one frozen `SnipPromptBudget(summary_prompt, dual_output_contract, prompt_within_budget, prompt_with_repair_within_budget)`. The audited `self` surface of the planner group is exactly `{summary_prompt, _dual_output_contract, trim_tokens_to_summarize, token_counter}`, the last two only through `_prompt_within_budget`/`_prompt_with_repair_within_budget`, which spec 13.1 keeps in the middleware; the middleware passes those two bound methods. Planner-internal calls become module-local function calls; the class keeps thin wrappers only for `_plan_reduction_step` (called by `_reduce_snip_summaries`/`_areduce_snip_summaries`), `_build_snip_prompt_plan` (called by `_summarize_with`/`_asummarize_with`/`_prepare_compaction`), and `_build_summary_prompt` (called by `_overbudget_progress_cutoff`/`_prepare_compaction` and by `test_memory_snip_compaction.py:1347,1372,1431`). `_ensure_snip_summary_output_budget` moves but its single call at L2146 inside `create_summarization_middleware` keeps the bare name so `test_memory_snip_compaction.py:2040,2081` patches on the middleware module keep working.
9. `turn_compaction.py` receives only pure functions. `complete_turn_ranges()` replaces the `classmethod` (whose `cls` was used only to reach sibling statics) and the class keeps `_complete_turn_ranges = staticmethod(complete_turn_ranges)` because `deerflow/runtime/context_compaction.py:86` calls it on the class and seven tests call it on instances; `_candidate_cutoffs`, `_snip_messages`, `_messages_for_trigger_count`, `_summary_count_message`, and `_context_progress` keep `staticmethod` aliases because staying methods call them via `self.`. No subclass of `DeerFlowSummarizationMiddleware` exists in the repository.
10. `compaction_receipts.py` functions take `observer: object | None` explicitly; the four Saver-side wrappers (`_receipt`, `_require_receipt_preconditions`, `_context_compaction_update`, `_acontext_compaction_update`) read `self._context_compaction_observer` at call time because `tests/test_subagent_context_compaction_evidence.py:22,33` assigns the attribute after construction. `acontext_compaction_update()` additionally takes `token_counter` and imports `messages_for_trigger_count`/`summary_count_message` from `turn_compaction` (the only runtime edge between the three new modules besides `SnipCompactionFailed`).
11. Moved bodies that log (`build_summary_prompt_from_formatted`, `build_summary_prompt`) emit under `deerflow.agents.middlewares.snip_planner`; no repository test asserts a logger name (the only `caplog` filter, `test_memory_snip_compaction.py:2160-2166`, matches message text from the staying `_invoke_snip_prompt`). Record the logger-name change in the handoff.

### Final ownership layout

```text
backend/packages/harness/deerflow/persistence/jobs/
├── sql.py                    # JobRepository state machine, lease hash, requeue-event
│                             # registry, consume_issued_dead_job_requeued_event, __all__
└── contracts.py              # JobType/RetrySafety, JobScope, EnqueueJob, JobClaim, ...,
                              # ports, exceptions, retry_backoff_seconds, pure code helpers

backend/app/private_work/
├── snapshot_repository.py    # RunSnapshotRepository: atomic admission, closure I/O,
│                             # secret listing; re-exports legacy names
├── snapshot_contracts.py     # RunSnapshotAssetStale, DTOs, admission Protocols,
│                             # agent_model_snapshot_purpose
├── snapshot_admission_rules.py  # encoder, schema version, secret-key rejection,
│                             # recursion-limit clamp, pure validators, plan_run_asset_rows
├── checkpointer.py           # ProjectScopedCheckpointer, _ScopedCheckpointSaver write/
│                             # read/lease/approval-deletion transactions, sync bridge
└── checkpoint_receipt_repair.py  # config readers, Memory/Context/Provider receipt
                              # activation and repair functions

backend/packages/harness/deerflow/agents/middlewares/
├── summarization_middleware.py  # DeerFlowSummarizationMiddleware hooks, invocation,
│                             # _prepare_compaction, retention, compact_state; factories
├── snip_planner.py           # snip exceptions, constants, plan dataclasses,
│                             # SnipPromptBudget, prompt/projection/reduction planners
├── turn_compaction.py        # turn boundary, clarification continuation, cutoff
│                             # candidates, trigger-count helpers, _PreparedCompaction
└── compaction_receipts.py    # ContextCompactionResult, archive/receipt/estimator,
                              # context compaction state updates
```

Dependency direction:

```text
jobs/contracts (leaf)                 <── jobs/sql
snapshot_contracts (leaf)             <── snapshot_admission_rules <── snapshot_repository
                                      <── snapshot_repository
checkpoint_receipt_repair (leaf)      <── checkpointer
snip_planner (leaf)                   <── compaction_receipts <── summarization_middleware
turn_compaction (leaf)                <── compaction_receipts
snip_planner, turn_compaction         <── summarization_middleware
```

No arrow may point from an owner back to `sql.py`, `snapshot_repository.py`, `checkpointer.py`, or `summarization_middleware.py`.

### Frozen compatibility surfaces

- `deerflow.persistence.jobs.sql.__all__` stays the 23-name list at L2273-2297 verbatim; `deerflow.persistence.jobs.__init__` is untouched and keeps exporting the same 18 objects; `JobTerminalEvent` and `JobTerminalPort` stay importable from `sql` although absent from `__all__`.
- `RunSnapshotRepository.__init__`, `ProjectScopedCheckpointer.__init__`, `_ScopedCheckpointSaver.__init__`, `JobRepository.__init__`, `DeerFlowSummarizationMiddleware.__init__`, `create_summarization_middleware`, and `freeze_summarization_profile` keep the parameter tuples frozen in Task 1.
- Dataclass field orders frozen in Task 1: `ContextCompactionResult`, `_PreparedCompaction`, `_SnipPromptPlan`, `_SnipSummary`, `EnqueueJob`, `JobClaim`, `RunMcpSecretSnapshot`, `RunSkillSecretSnapshot`.
- The ordered `await` callee names of `create_run_with_snapshot_in_session()` are frozen in Task 1 and re-asserted after Task 4.
- Every name in the four `*_COMPATIBILITY_NAMES` inventories (Task 1) stays importable from its legacy module and is the same object as its owner.

---

## Task 1: Freeze Batch 6 contracts

**Files:**

- Create: `backend/tests/test_python_module_decomposition_secondary_hotspots.py`
- Verify: `backend/packages/harness/deerflow/persistence/jobs/sql.py`
- Verify: `backend/app/private_work/snapshot_repository.py`
- Verify: `backend/app/private_work/checkpointer.py`
- Verify: `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py`

**Interfaces:**

- Consumes: the four legacy modules at the baseline, `deerflow.persistence.jobs` package exports, the repository test tree.
- Produces: signature/field freezes, the four compatibility inventories, the improved test-consumer scanner (imports, string patches, alias `setattr`, and alias attribute access), the ordered-`await` scanner, class-attribute retention lists, and the owner import-direction helper used by Tasks 2-9.

- [ ] **Step 1: verify branch, baseline, preserved state, and local configuration**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations
  git branch --show-current
  git rev-parse HEAD
  git status --short
  wc -l backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py \
    backend/app/private_work/snapshot_repository.py \
    backend/app/private_work/checkpointer.py \
    backend/packages/harness/deerflow/persistence/jobs/sql.py
  ls backend/packages/harness/deerflow/persistence/jobs \
    backend/app/private_work/snapshot_contracts.py backend/app/private_work/snapshot_admission_rules.py \
    backend/app/private_work/checkpoint_receipt_repair.py \
    backend/packages/harness/deerflow/agents/middlewares/snip_planner.py \
    backend/packages/harness/deerflow/agents/middlewares/turn_compaction.py \
    backend/packages/harness/deerflow/agents/middlewares/compaction_receipts.py 2>&1 | cat
  git check-ignore -v .env config.yaml
  ```

  Require branch `codex/python-module-decomposition-foundations`, `7dcb05c9…` or a reviewed descendant that contains only the Batch 5 polish commit, line counts `2168 1971 2169 2297`, `ls` reporting that all seven owner modules do not exist, and ignored configuration. Identify every pre-existing modified/untracked path as the Batch 5 polish (if uncommitted), the P1 test, or a user-owned document. Stop on any unexplained path.

- [ ] **Step 2: create the contract module with shape freezes**

  ```python
  """Batch 6 secondary-hotspot compatibility contracts.

  Characterization tests that pass on the untouched Job, Run Snapshot,
  Checkpointer, and Summarization baselines and keep passing while their
  legacy modules delegate to owning modules.
  """

  from __future__ import annotations

  import ast
  import dataclasses
  import importlib
  import inspect
  from pathlib import Path

  import deerflow.persistence.jobs as jobs_package
  from app.private_work import checkpointer as checkpointer_legacy
  from app.private_work import snapshot_repository as snapshot_legacy
  from deerflow.agents.middlewares import summarization_middleware as summarization_legacy
  from deerflow.persistence.jobs import sql as jobs_sql_legacy

  BACKEND_ROOT = Path(__file__).resolve().parents[1]
  TESTS_ROOT = BACKEND_ROOT / "tests"
  HARNESS_ROOT = BACKEND_ROOT / "packages" / "harness" / "deerflow"
  JOBS_ROOT = HARNESS_ROOT / "persistence" / "jobs"
  MIDDLEWARES_ROOT = HARNESS_ROOT / "agents" / "middlewares"
  PRIVATE_WORK_ROOT = BACKEND_ROOT / "app" / "private_work"
  JOBS_SQL_PATH = JOBS_ROOT / "sql.py"
  SNAPSHOT_PATH = PRIVATE_WORK_ROOT / "snapshot_repository.py"
  CHECKPOINTER_PATH = PRIVATE_WORK_ROOT / "checkpointer.py"
  SUMMARIZATION_PATH = MIDDLEWARES_ROOT / "summarization_middleware.py"
  JOBS_SQL_MODULE = "deerflow.persistence.jobs.sql"
  SNAPSHOT_MODULE = "app.private_work.snapshot_repository"
  CHECKPOINTER_MODULE = "app.private_work.checkpointer"
  SUMMARIZATION_MODULE = "deerflow.agents.middlewares.summarization_middleware"
  JOBS_OWNER_MODULES = ("contracts",)
  SNAPSHOT_OWNER_MODULES = ("snapshot_contracts", "snapshot_admission_rules")
  CHECKPOINTER_OWNER_MODULES = ("checkpoint_receipt_repair",)
  SUMMARIZATION_OWNER_MODULES = ("snip_planner", "turn_compaction", "compaction_receipts")

  EXPECTED_JOB_REPOSITORY_INIT_PARAMETERS = ("self", "session", "owner_ref_hasher", "terminal_port")
  EXPECTED_SNAPSHOT_REPOSITORY_INIT_PARAMETERS = (
      "self",
      "session_factory",
      "model_ref_resolver",
      "model_catalog",
      "runtime_policy",
      "endpoint_policy",
      "personalization_repository_builder",
      "audit",
  )
  EXPECTED_PROJECT_CHECKPOINTER_INIT_PARAMETERS = ("self", "raw_saver", "session_factory", "quota", "approval_audit", "run_event_store")
  EXPECTED_SCOPED_SAVER_INIT_PARAMETERS = (
      "self",
      "raw_saver",
      "session_factory",
      "context",
      "owner_loop",
      "quota",
      "approval_audit",
      "run_event_store",
      "thread_kind",
  )
  EXPECTED_SUMMARIZATION_INIT_PARAMETERS = (
      "self",
      "args",
      "compact_all_complete_turns",
      "context_model",
      "context_compaction_observer",
      "dual_output_contract",
      "kwargs",
  )
  EXPECTED_CREATE_SUMMARIZATION_PARAMETERS = ("app_config", "context_model", "keep", "context_compaction_observer")
  EXPECTED_FREEZE_PROFILE_PARAMETERS = ("middlewares", "profile")
  EXPECTED_JOBS_SQL_ALL = [
      "DurableDeadTerminalReconciliationRequest",
      "DurableTerminalSuccessorRebindRequest",
      "DurableTerminalTakeoverRequest",
      "EnqueueJob",
      "DeadJobRecord",
      "DeadJobRequeuedEvent",
      "JobAuditPort",
      "JobClaim",
      "JobHeartbeat",
      "JobIdempotencyConflict",
      "JobOwnerRef",
      "JobOwnerRefRequired",
      "JobRepository",
      "JobRetryResult",
      "JobRequeueForbidden",
      "JobScope",
      "JobTerminalResult",
      "JobType",
      "JobUnstartedClaimRelease",
      "RetrySafety",
      "RetentionPurgeJobAuthority",
      "consume_issued_dead_job_requeued_event",
      "retry_backoff_seconds",
  ]
  EXPECTED_CONTEXT_COMPACTION_RESULT_FIELDS = ("summary_text", "messages_to_summarize", "preserved_messages", "total_tokens", "memory_archive_receipt")
  EXPECTED_PREPARED_COMPACTION_FIELDS = ("source_messages", "snip_messages", "preserved_messages", "previous_summary", "total_tokens")
  EXPECTED_SNIP_PROMPT_PLAN_FIELDS = ("prompts", "hierarchical")
  EXPECTED_SNIP_SUMMARY_FIELDS = ("continuity", "tagged_text")
  EXPECTED_RUN_MCP_SECRET_SNAPSHOT_FIELDS = tuple(field.name for field in dataclasses.fields(snapshot_legacy.RunMcpSecretSnapshot))
  EXPECTED_RUN_SKILL_SECRET_SNAPSHOT_FIELDS = tuple(field.name for field in dataclasses.fields(snapshot_legacy.RunSkillSecretSnapshot))
  EXPECTED_ENQUEUE_JOB_FIELDS = tuple(field.name for field in dataclasses.fields(jobs_sql_legacy.EnqueueJob))
  EXPECTED_JOB_CLAIM_FIELDS = tuple(field.name for field in dataclasses.fields(jobs_sql_legacy.JobClaim))
  ```

  Immediately after writing the module, print the four `tuple(field.name ...)` values with `PYTHONPATH=. uv run python -c 'import tests.test_python_module_decomposition_secondary_hotspots as m; print(m.EXPECTED_RUN_MCP_SECRET_SNAPSHOT_FIELDS, m.EXPECTED_RUN_SKILL_SECRET_SNAPSHOT_FIELDS, m.EXPECTED_ENQUEUE_JOB_FIELDS, m.EXPECTED_JOB_CLAIM_FIELDS, sep="\n")'` and replace the four computed expressions with the printed literal tuples so the freeze is independent of the module under test.

  ```python
  def test_batch6_jobs_public_shapes_are_frozen() -> None:
      assert jobs_sql_legacy.__all__ == EXPECTED_JOBS_SQL_ALL
      assert tuple(inspect.signature(jobs_sql_legacy.JobRepository.__init__).parameters) == EXPECTED_JOB_REPOSITORY_INIT_PARAMETERS
      assert tuple(field.name for field in dataclasses.fields(jobs_sql_legacy.EnqueueJob)) == EXPECTED_ENQUEUE_JOB_FIELDS
      assert tuple(field.name for field in dataclasses.fields(jobs_sql_legacy.JobClaim)) == EXPECTED_JOB_CLAIM_FIELDS
      for name in jobs_package.__all__:
          assert hasattr(jobs_package, name), name
      assert jobs_package.JobRepository is jobs_sql_legacy.JobRepository
      assert jobs_package.EnqueueJob is jobs_sql_legacy.EnqueueJob
      assert callable(jobs_sql_legacy.JobTerminalEvent) and callable(jobs_sql_legacy.JobTerminalPort)


  def test_batch6_snapshot_public_shapes_are_frozen() -> None:
      assert tuple(inspect.signature(snapshot_legacy.RunSnapshotRepository.__init__).parameters) == EXPECTED_SNAPSHOT_REPOSITORY_INIT_PARAMETERS
      assert tuple(field.name for field in dataclasses.fields(snapshot_legacy.RunMcpSecretSnapshot)) == EXPECTED_RUN_MCP_SECRET_SNAPSHOT_FIELDS
      assert tuple(field.name for field in dataclasses.fields(snapshot_legacy.RunSkillSecretSnapshot)) == EXPECTED_RUN_SKILL_SECRET_SNAPSHOT_FIELDS
      assert issubclass(snapshot_legacy.RunSnapshotAssetStale, Exception)
      assert not hasattr(snapshot_legacy, "__all__")


  def test_batch6_checkpointer_public_shapes_are_frozen() -> None:
      assert tuple(inspect.signature(checkpointer_legacy.ProjectScopedCheckpointer.__init__).parameters) == EXPECTED_PROJECT_CHECKPOINTER_INIT_PARAMETERS
      assert tuple(inspect.signature(checkpointer_legacy._ScopedCheckpointSaver.__init__).parameters) == EXPECTED_SCOPED_SAVER_INIT_PARAMETERS
      assert checkpointer_legacy.PRIVATE_SCOPE_MARKER == "deerflow_private_scope"
      assert not hasattr(checkpointer_legacy, "__all__")


  def test_batch6_summarization_public_shapes_are_frozen() -> None:
      middleware = summarization_legacy.DeerFlowSummarizationMiddleware
      assert tuple(inspect.signature(middleware.__init__).parameters) == EXPECTED_SUMMARIZATION_INIT_PARAMETERS
      assert tuple(inspect.signature(summarization_legacy.create_summarization_middleware).parameters) == EXPECTED_CREATE_SUMMARIZATION_PARAMETERS
      assert tuple(inspect.signature(summarization_legacy.freeze_summarization_profile).parameters) == EXPECTED_FREEZE_PROFILE_PARAMETERS
      assert tuple(field.name for field in dataclasses.fields(summarization_legacy.ContextCompactionResult)) == EXPECTED_CONTEXT_COMPACTION_RESULT_FIELDS
      assert tuple(field.name for field in dataclasses.fields(summarization_legacy._PreparedCompaction)) == EXPECTED_PREPARED_COMPACTION_FIELDS
      assert tuple(field.name for field in dataclasses.fields(summarization_legacy._SnipPromptPlan)) == EXPECTED_SNIP_PROMPT_PLAN_FIELDS
      assert tuple(field.name for field in dataclasses.fields(summarization_legacy._SnipSummary)) == EXPECTED_SNIP_SUMMARY_FIELDS
      assert not hasattr(summarization_legacy, "__all__")
  ```

- [ ] **Step 3: freeze compatibility names and the repository test-consumer inventories**

  Each inventory is the union of (a) every name a repository production module imports from the legacy module and (b) every name a repository test imports from, patches on, or reads as an attribute of the legacy module today. The scanner below records imports, `patch("module.name...")` strings, `monkeypatch.setattr(alias, "name", ...)`, `patch.object(alias.Name, ...)`/`setattr(alias.Name, ...)`, and plain `alias.name` attribute reads.

  ```python
  JOBS_SQL_COMPATIBILITY_NAMES = frozenset(
      {
          "DeadJobRecord",
          "DeadJobRequeuedEvent",
          "DurableDeadTerminalReconciliationRequest",
          "DurableTerminalSuccessorRebindRequest",
          "DurableTerminalTakeoverRequest",
          "EnqueueJob",
          "JobAuditPort",
          "JobClaim",
          "JobHeartbeat",
          "JobIdempotencyConflict",
          "JobOwnerRef",
          "JobOwnerRefRequired",
          "JobRepository",
          "JobRequeueForbidden",
          "JobScope",
          "JobTerminalEvent",
          "JobTerminalPort",
          "JobTerminalResult",
          "JobType",
          "JobUnstartedClaimRelease",
          "RetentionPurgeJobAuthority",
          "RetrySafety",
          "consume_issued_dead_job_requeued_event",
          "_dead_error_code_for_failure",
          "datetime",
      }
  )
  JOBS_CONTRACT_NAMES = (
      "JobType",
      "RetrySafety",
      "_SHA256_HEX",
      "_DETERMINISTIC_NONRETRYABLE_ERROR_CODES",
      "_durable_terminal_successor_idempotency_key",
      "_dead_error_code_for_failure",
      "JobScope",
      "RetentionPurgeJobAuthority",
      "EnqueueJob",
      "JobClaim",
      "JobHeartbeat",
      "JobUnstartedClaimRelease",
      "JobOwnerRef",
      "DeadJobRecord",
      "DeadJobRequeuedEvent",
      "JobTerminalEvent",
      "JobTerminalResult",
      "DurableTerminalTakeoverRequest",
      "DurableDeadTerminalReconciliationRequest",
      "DurableTerminalSuccessorRebindRequest",
      "JobRetryResult",
      "JobAuditPort",
      "JobTerminalPort",
      "JobIdempotencyConflict",
      "JobOwnerRefRequired",
      "JobRequeueForbidden",
      "retry_backoff_seconds",
  )
  JOBS_SQL_RETAINED_NAMES = (
      "_is_durable_terminal_successor",
      "_lease_token_hash",
      "_ISSUED_REQUEUE_EVENTS",
      "_ISSUED_REQUEUE_EVENTS_LOCK",
      "_DeadTerminalReconciliationCursor",
      "_DEAD_TERMINAL_RECONCILIATION_PAGE_SIZE",
      "_DEAD_TERMINAL_RECONCILIATION_CURSORS",
      "_DEAD_TERMINAL_RECONCILIATION_CURSORS_LOCK",
      "_dead_terminal_reconciliation_cursor",
      "_advance_dead_terminal_reconciliation_cursor",
      "consume_issued_dead_job_requeued_event",
      "JobRepository",
  )

  SNAPSHOT_COMPATIBILITY_NAMES = frozenset(
      {
          "RunSnapshotRepository",
          "RunSnapshotAssetStale",
          "RunMcpSecretSnapshot",
          "RunModelSnapshotAdmissionPort",
          "RunRuntimePolicyAdmissionPort",
          "agent_model_snapshot_purpose",
          "_apply_runtime_recursion_limit",
          "_RunAssetSnapshotAdmissionEncoder",
          "AsyncSession",
          "PrivateRunRepository",
          "PrivateThreadRepository",
          "require_active_run_skill_writer_cohort",
          "encode_run_asset_snapshot",
          "encoded_run_asset_snapshot_json_size",
      }
  )
  SNAPSHOT_CONTRACT_NAMES = (
      "RunSnapshotAssetStale",
      "RunMcpSecretSnapshot",
      "RunSkillSecretSnapshot",
      "AdmittedRunModelSnapshot",
      "RunModelSnapshotAdmissionPort",
      "RunRuntimePolicyAdmissionPort",
      "agent_model_snapshot_purpose",
  )
  SNAPSHOT_RULE_MODULE_NAMES = (
      "_FORBIDDEN_PERSISTED_KEY_PARTS",
      "_reject_secret_bearing_keys",
      "_apply_runtime_recursion_limit",
      "_RunAssetSnapshotAdmissionEncoder",
      "_r1_snapshot_schema_version",
  )
  # legacy staticmethod name on RunSnapshotRepository -> owner function name
  SNAPSHOT_RULE_STATICMETHODS = {
      "_asset_allowed": "asset_allowed",
      "_validate_project_mcp_secret_slots": "validate_project_mcp_secret_slots",
      "_validate_dependency_snapshots": "validate_dependency_snapshots",
      "_validate_main_dependency_boundary": "validate_main_dependency_boundary",
  }
  EXPECTED_SNAPSHOT_ADMISSION_AWAITS = (
      "require_active_run_skill_writer_cohort",
      "admit_current_upload_snapshot",
      "validate_run_asset_closure_in_session",
      "validate_agent_closure_in_session",
      "prepare",
      "lock_agent_runtime_for_admission",
      "_continuation_workload_profile",
      "create_for_snapshot_assembly",
      "scalar",
      "admit_run_snapshot",
      "admit_model_snapshot",
      "admit_model_snapshot",
      "admit_model_snapshot",
      "update_admitted_execution_profile",
      "get",
      "_admit_memory_context_snapshot",
      "flush",
      "seal_asset_closure",
      "touch_activity",
  )

  CHECKPOINTER_COMPATIBILITY_NAMES = frozenset(
      {
          "ProjectScopedCheckpointer",
          "PRIVATE_SCOPE_MARKER",
          "_ScopedCheckpointSaver",
          "ContextEvidenceRepository",
          "ContextProjectionTransaction",
          "transition_output_delivery_obligation_for_approval_terminal",
          "PrivateWorkRevalidator",
          "PrivateThreadRepository",
      }
  )
  # legacy _ScopedCheckpointSaver attribute -> owner function name
  CHECKPOINT_REPAIR_STATICMETHODS = {
      "_thread_id": "thread_id_from_config",
      "_checkpoint_id": "checkpoint_id_from_config",
      "_validate_context_provider_checkpoint_response": "validate_context_provider_checkpoint_response",
  }
  CHECKPOINT_REPAIR_DELEGATING_METHODS = (
      "_memory_history_activation",
      "_repair_memory_archive_receipt",
      "_context_compaction_activation",
      "_repair_context_compaction_receipt",
      "_context_provider_checkpoint_activation",
      "_repair_context_provider_checkpoint",
  )
  CHECKPOINT_REPAIR_OWNER_NAMES = (
      "_MEMORY_ARCHIVE_RECEIPT_FIELDS",
      "thread_id_from_config",
      "checkpoint_id_from_config",
      "memory_history_activation",
      "repair_memory_archive_receipt",
      "context_compaction_activation",
      "repair_context_compaction_receipt",
      "context_provider_checkpoint_activation",
      "validate_context_provider_checkpoint_response",
      "repair_context_provider_checkpoint",
  )

  SUMMARIZATION_COMPATIBILITY_NAMES = frozenset(
      {
          "DeerFlowSummarizationMiddleware",
          "create_summarization_middleware",
          "freeze_summarization_profile",
          "SnipCompactionFailed",
          "SnipModelOutputInvalid",
          "SnipPromptBudgetTooSmall",
          "SnipSourceTooLarge",
          "ContextCompactionResult",
          "MIN_SNIP_SUMMARY_OUTPUT_TOKENS",
          "MAX_SNIP_HIERARCHICAL_MODEL_CALLS",
          "_ensure_snip_summary_output_budget",
          "ModelRuntime",
          "ModelRuntimeProfile",
      }
  )
  SNIP_PLANNER_NAMES = (
      "MIN_SNIP_SUMMARY_OUTPUT_TOKENS",
      "MAX_SNIP_HIERARCHICAL_LEAVES",
      "MAX_SNIP_HIERARCHICAL_MODEL_CALLS",
      "SnipPromptBudgetTooSmall",
      "SnipSourceTooLarge",
      "SnipCompactionFailed",
      "SnipModelOutputInvalid",
      "_ensure_snip_summary_output_budget",
      "_SnipSummary",
      "_ProjectionField",
      "_ProjectionFragment",
      "_SnipPromptPlan",
      "_SnipReductionStep",
  )
  TURN_COMPACTION_NAMES = (
      "_SUMMARY_TRIGGER_MESSAGE_NAME",
      "_ASK_CLARIFICATION_TOOL_NAME",
      "_PreparedCompaction",
  )
  COMPACTION_RECEIPT_NAMES = (
      "ContextCompactionResult",
      "_resolve_thread_id",
  )
  # legacy DeerFlowSummarizationMiddleware attribute -> (owner module, owner function)
  SUMMARIZATION_STATICMETHOD_ALIASES = {
      "_complete_turn_ranges": ("turn_compaction", "complete_turn_ranges"),
      "_candidate_cutoffs": ("turn_compaction", "candidate_cutoffs"),
      "_snip_messages": ("turn_compaction", "snip_messages"),
      "_summary_count_message": ("turn_compaction", "summary_count_message"),
      "_messages_for_trigger_count": ("turn_compaction", "messages_for_trigger_count"),
      "_context_progress": ("turn_compaction", "context_progress"),
  }
  SUMMARIZATION_DELEGATING_METHODS = (
      "_plan_reduction_step",
      "_build_snip_prompt_plan",
      "_build_summary_prompt",
      "_receipt",
      "_require_receipt_preconditions",
      "_context_compaction_update",
      "_acontext_compaction_update",
  )
  SUMMARIZATION_RETAINED_METHODS = (
      "__init__",
      "_get_profile_limits",
      "_parse_snip_response",
      "_create_summary",
      "_acreate_summary",
      "_prompt_within_budget",
      "_prompt_with_repair_within_budget",
      "_invoke_snip_prompt",
      "_ainvoke_snip_prompt",
      "_reduce_snip_summaries",
      "_areduce_snip_summaries",
      "_summarize_with",
      "_asummarize_with",
      "measure_context_usage",
      "_measure_trigger_usage",
      "measure_provider_request_usage",
      "_count_rendered_summary_prompt_tokens",
      "before_model",
      "abefore_model",
      "_requested_cutoff",
      "_provider_safe_retention_cutoff",
      "_compacted_result_fits_provider_profile",
      "_overbudget_progress_cutoff",
      "_profile_trigger_reached",
      "fixed_component_over_trigger",
      "_prepare_compaction",
      "compact_state",
      "acompact_state",
      "_maybe_summarize",
      "_amaybe_summarize",
  )
  SUMMARIZATION_MOVED_INTERNALS = (
      "_intermediate_summary_text",
      "_reduction_prompt",
      "_assemble_summary_input_text",
      "_build_summary_input_text",
      "_build_summary_prompt_from_formatted",
      "_projection_text",
      "_quoted_projection_value",
      "_projection_material",
      "_render_projection",
      "_projection_prompt",
      "_fit_projection_prefix",
      "_build_projection_prompts",
      "_is_turn_user",
      "_turn_prefix_start",
      "_clarification_request_tool_call_id",
      "_is_clarification_continuation",
      "_archive_context",
      "_source_checkpoint_id",
      "_resolve_compaction_estimator",
      "_context_state_digest",
  )
  ```

- [ ] **Step 4: add the scanners and helpers**

  ```python
  def _parse(path: Path) -> ast.Module:
      return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


  def _legacy_test_consumers(module_name: str) -> frozenset[str]:
      """Names imported from, patched on, or read as attributes of ``module_name`` across tests/."""

      observed: set[str] = set()
      for path in TESTS_ROOT.rglob("*.py"):
          tree = _parse(path)
          aliases: set[str] = set()
          for node in ast.walk(tree):
              if isinstance(node, ast.Import):
                  aliases.update(alias.asname or alias.name for alias in node.names if alias.name == module_name)
              elif isinstance(node, ast.ImportFrom) and node.module:
                  if node.module == module_name:
                      observed.update(alias.name for alias in node.names)
                  else:
                      aliases.update(alias.asname or alias.name for alias in node.names if f"{node.module}.{alias.name}" == module_name)
          for node in ast.walk(tree):
              if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
                  observed.add(node.attr)
                  continue
              if not isinstance(node, ast.Call):
                  continue
              callee = node.func
              is_setattr = isinstance(callee, ast.Attribute) and callee.attr == "setattr"
              is_patch = (isinstance(callee, ast.Name) and callee.id == "patch") or (isinstance(callee, ast.Attribute) and callee.attr == "patch")
              if not (is_setattr or is_patch) or not node.args:
                  continue
              target = node.args[0]
              if isinstance(target, ast.Constant) and isinstance(target.value, str) and target.value.startswith(f"{module_name}."):
                  observed.add(target.value.removeprefix(f"{module_name}.").split(".")[0])
              elif isinstance(target, ast.Name) and target.id in aliases and len(node.args) > 1:
                  attribute = node.args[1]
                  if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
                      observed.add(attribute.value)
      return frozenset(observed)


  def _function_node(path: Path, function_name: str, *, class_name: str | None = None) -> ast.FunctionDef | ast.AsyncFunctionDef:
      tree = _parse(path)
      scopes: list[ast.AST] = [tree]
      if class_name is not None:
          scopes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
      for scope in scopes:
          for node in ast.walk(scope):
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
                  return node
      raise AssertionError(f"{function_name} not found in {path.name}")


  def _await_callee_names(function: ast.AST) -> tuple[str, ...]:
      """Callee names of every ``await <call>`` in source order."""

      awaited: list[tuple[int, int, str]] = []
      for node in ast.walk(function):
          if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
              continue
          callee = node.value.func
          if isinstance(callee, ast.Attribute):
              awaited.append((node.lineno, node.col_offset, callee.attr))
          elif isinstance(callee, ast.Name):
              awaited.append((node.lineno, node.col_offset, callee.id))
      return tuple(name for _, _, name in sorted(awaited))


  def _module_imports(path: Path) -> set[str]:
      imports: set[str] = set()
      for node in ast.walk(_parse(path)):
          if isinstance(node, ast.Import):
              imports.update(alias.name for alias in node.names)
          elif isinstance(node, ast.ImportFrom):
              prefix = "." * node.level
              if node.module:
                  imports.add(f"{prefix}{node.module}")
                  imports.update(f"{prefix}{node.module}.{alias.name}" for alias in node.names)
              elif node.level > 0:
                  imports.add(prefix)
                  imports.update(f"{prefix}{alias.name}" for alias in node.names)
      return imports


  def _class_defined_names(path: Path, class_name: str) -> frozenset[str]:
      """Attribute names bound directly in ``class_name``'s body (defs and assignments)."""

      tree = _parse(path)
      class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
      names: set[str] = set()
      for node in class_node.body:
          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
              names.add(node.name)
          elif isinstance(node, ast.Assign):
              names.update(target.id for target in node.targets if isinstance(target, ast.Name))
          elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
              names.add(node.target.id)
      return frozenset(names)


  def _assert_owner_imports_are_clean(path: Path, *, forbidden_modules: tuple[str, ...], forbidden_prefixes: tuple[str, ...]) -> None:
      imports = _module_imports(path)
      offenders = {module for module in imports if module in forbidden_modules or any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes)}
      assert offenders == set(), (path.name, sorted(offenders))
  ```

- [ ] **Step 5: add the baseline-passing inventory, `await`-order, and retention tests**

  ```python
  def test_batch6_jobs_sql_test_consumers_stay_within_the_frozen_inventory() -> None:
      observed = _legacy_test_consumers(JOBS_SQL_MODULE)
      assert observed <= JOBS_SQL_COMPATIBILITY_NAMES, observed - JOBS_SQL_COMPATIBILITY_NAMES
      for name in JOBS_SQL_COMPATIBILITY_NAMES - {"datetime"}:
          assert hasattr(jobs_sql_legacy, name), name


  def test_batch6_snapshot_test_consumers_stay_within_the_frozen_inventory() -> None:
      observed = _legacy_test_consumers(SNAPSHOT_MODULE)
      assert observed <= SNAPSHOT_COMPATIBILITY_NAMES, observed - SNAPSHOT_COMPATIBILITY_NAMES
      for name in SNAPSHOT_CONTRACT_NAMES + SNAPSHOT_RULE_MODULE_NAMES:
          assert hasattr(snapshot_legacy, name), name
      for name in SNAPSHOT_RULE_STATICMETHODS:
          assert isinstance(snapshot_legacy.RunSnapshotRepository.__dict__[name], staticmethod), name


  def test_batch6_snapshot_admission_await_order_is_frozen() -> None:
      function = _function_node(SNAPSHOT_PATH, "create_run_with_snapshot_in_session", class_name="RunSnapshotRepository")
      assert _await_callee_names(function) == EXPECTED_SNAPSHOT_ADMISSION_AWAITS
      source = inspect.getsource(snapshot_legacy.RunSnapshotRepository.create_run_with_snapshot_in_session)
      assert "require_active_run_skill_writer_cohort" in source and "PrivateWorkUnavailable" in source


  def test_batch6_checkpointer_test_consumers_stay_within_the_frozen_inventory() -> None:
      observed = _legacy_test_consumers(CHECKPOINTER_MODULE)
      assert observed <= CHECKPOINTER_COMPATIBILITY_NAMES, observed - CHECKPOINTER_COMPATIBILITY_NAMES
      saver_names = _class_defined_names(CHECKPOINTER_PATH, "_ScopedCheckpointSaver")
      for name in tuple(CHECKPOINT_REPAIR_STATICMETHODS) + CHECKPOINT_REPAIR_DELEGATING_METHODS:
          assert name in saver_names, name
      for name in CHECKPOINT_REPAIR_STATICMETHODS:
          assert isinstance(checkpointer_legacy._ScopedCheckpointSaver.__dict__[name], staticmethod), name


  def test_batch6_summarization_test_consumers_stay_within_the_frozen_inventory() -> None:
      observed = _legacy_test_consumers(SUMMARIZATION_MODULE)
      assert observed <= SUMMARIZATION_COMPATIBILITY_NAMES, observed - SUMMARIZATION_COMPATIBILITY_NAMES
      for name in SUMMARIZATION_COMPATIBILITY_NAMES:
          assert hasattr(summarization_legacy, name), name
      class_names = _class_defined_names(SUMMARIZATION_PATH, "DeerFlowSummarizationMiddleware")
      for name in SUMMARIZATION_RETAINED_METHODS + SUMMARIZATION_DELEGATING_METHODS + tuple(SUMMARIZATION_STATICMETHOD_ALIASES):
          assert name in class_names, name
      assert "state_schema" in class_names


  def test_batch6_summarization_seam_is_called_by_bare_name_in_the_factory() -> None:
      factory = _function_node(SUMMARIZATION_PATH, "create_summarization_middleware")
      called = {node.func.id for node in ast.walk(factory) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
      assert "_ensure_snip_summary_output_budget" in called


  def test_batch6_owner_modules_never_import_facades_or_forbidden_packages() -> None:
      for name in JOBS_OWNER_MODULES:
          path = JOBS_ROOT / f"{name}.py"
          if path.exists():
              _assert_owner_imports_are_clean(path, forbidden_modules=(JOBS_SQL_MODULE, ".sql"), forbidden_prefixes=("app", "sqlalchemy.orm", "sqlalchemy.dialects"))
      for name in SUMMARIZATION_OWNER_MODULES:
          path = MIDDLEWARES_ROOT / f"{name}.py"
          if path.exists():
              _assert_owner_imports_are_clean(path, forbidden_modules=(SUMMARIZATION_MODULE, ".summarization_middleware"), forbidden_prefixes=("app", "sqlalchemy"))
      for name in SNAPSHOT_OWNER_MODULES:
          path = PRIVATE_WORK_ROOT / f"{name}.py"
          if path.exists():
              _assert_owner_imports_are_clean(path, forbidden_modules=(SNAPSHOT_MODULE, ".snapshot_repository"), forbidden_prefixes=())
      for name in CHECKPOINTER_OWNER_MODULES:
          path = PRIVATE_WORK_ROOT / f"{name}.py"
          if path.exists():
              _assert_owner_imports_are_clean(path, forbidden_modules=(CHECKPOINTER_MODULE, ".checkpointer"), forbidden_prefixes=())
  ```

  `jobs/contracts.py` may import `sqlalchemy.ext.asyncio.AsyncSession` for Protocol annotations (the baseline Protocols already annotate with it), so the jobs owner check forbids only `sqlalchemy.orm` and `sqlalchemy.dialects` plus `app`.

- [ ] **Step 6: run the new module and the complete focused Batch 6 baseline**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_secondary_hotspots.py -q
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_python_module_decomposition_secondary_hotspots.py \
    tests/test_job_owner_lifecycle_contract.py \
    tests/test_model_output_limit_settlement.py \
    tests/test_memory_document_contract.py \
    tests/test_worker_service.py \
    tests/test_agent_runtime_checksum.py \
    tests/test_memory_admission_snapshot.py \
    tests/test_mcp_query_secrets.py \
    tests/test_run_asset_facts.py \
    tests/test_run_execution_profile.py \
    tests/test_run_workload_profile.py \
    tests/test_automation_runtime_config.py \
    tests/test_run_skill_writer_cohort_contract.py \
    tests/test_agent_runtime_assessment.py \
    tests/test_mcp_runtime_contracts.py \
    tests/test_memory_contract.py \
    tests/test_skill_builder_checkpoint_scope.py \
    tests/test_context_compaction_checkpoint_repair.py \
    tests/test_context_provider_checkpoint_recovery.py \
    tests/test_output_delivery_obligation_cancellation.py \
    tests/test_memory_archive_receipt.py \
    tests/test_cancelled_delegation_settlement.py \
    tests/test_memory_snip_compaction.py \
    tests/test_compaction_trigger_capacity_clamp.py \
    tests/test_context_compaction_receipt_basis.py \
    tests/test_agent_assembly_golden.py \
    tests/test_agents_md_constants.py \
    tests/test_subagent_context_compaction_evidence.py \
    tests/test_memory_seal_worker.py \
    tests/test_memory_dream_prepare_worker.py \
    tests/test_private_compact_api.py \
    tests/test_chat_control_compaction_outcomes.py \
    -q -m "not postgres and not provider_integration"
  ```

  Require every new node to pass on the untouched baseline and the combined run to report `543 + <new contract tests>` passed, `1 skipped, 2 deselected`, zero failures. If `EXPECTED_SNAPSHOT_ADMISSION_AWAITS` does not match the observed tuple, stop: the baseline drifted from the audit and the plan must be re-audited before Task 4. If an inventory test reports an observed name outside its `*_COMPATIBILITY_NAMES` set, confirm the reporting test file and line are a legitimate current consumer of the legacy module, add the name to the inventory, and record the addition in the task report; never remove a name from an inventory to make a later task pass.

- [ ] **Step 7: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check tests/test_python_module_decomposition_secondary_hotspots.py
  uvx ruff format --check tests/test_python_module_decomposition_secondary_hotspots.py
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only the new test module with message `test(hotspots): freeze batch 6 secondary hotspot contracts`.

## Task 2: Extract Job contracts

**Files:**

- Create: `backend/packages/harness/deerflow/persistence/jobs/contracts.py`
- Modify: `backend/packages/harness/deerflow/persistence/jobs/sql.py:5-25,27-308,415-554,2273-2297`
- Modify: `backend/tests/test_python_module_decomposition_secondary_hotspots.py`

**Interfaces:**

- Consumes: `hashlib`, `re`, `uuid`, `dataclass`, `field`, `UTC`, `datetime`, `Literal`, `Protocol`, `AsyncSession` (annotation), `AccountPrivateGeneration`, `LLM_PUBLIC_ERROR_CODES`, `normalize_trace_id`.
- Produces: the 27 `JOBS_CONTRACT_NAMES` as exact objects importable from both `deerflow.persistence.jobs.contracts` and `deerflow.persistence.jobs.sql`.

- [ ] **Step 1: add the failing owner identity and characterization tests**

  ```python
  def test_jobs_contracts_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.persistence.jobs.contracts")
      for name in JOBS_CONTRACT_NAMES:
          assert getattr(jobs_sql_legacy, name) is getattr(owner, name), name
      for name in JOBS_SQL_RETAINED_NAMES:
          assert hasattr(jobs_sql_legacy, name), name
          assert not hasattr(owner, name), name
      assert jobs_sql_legacy.__all__ == EXPECTED_JOBS_SQL_ALL
      for name in jobs_package.__all__:
          if name.endswith("Row"):
              continue
          assert getattr(jobs_package, name) is getattr(jobs_sql_legacy, name), name


  def test_jobs_contracts_requeue_event_registry_stays_with_the_repository() -> None:
      owner = importlib.import_module("deerflow.persistence.jobs.contracts")
      event = object.__new__(owner.DeadJobRequeuedEvent)
      assert jobs_sql_legacy.consume_issued_dead_job_requeued_event(event) is False
      assert jobs_sql_legacy.consume_issued_dead_job_requeued_event(object()) is False
      assert jobs_sql_legacy.DeadJobRequeuedEvent is owner.DeadJobRequeuedEvent


  def test_jobs_contracts_retry_backoff_characterization() -> None:
      owner = importlib.import_module("deerflow.persistence.jobs.contracts")
      assert owner.retry_backoff_seconds is jobs_sql_legacy.retry_backoff_seconds
      assert owner.retry_backoff_seconds(attempt_count=1, initial_seconds=5, max_seconds=60) == 5
      assert owner.retry_backoff_seconds(attempt_count=3, initial_seconds=5, max_seconds=60) == 20
      assert owner.retry_backoff_seconds(attempt_count=10, initial_seconds=5, max_seconds=60) == 60
      with pytest.raises(ValueError, match="invalid retry backoff inputs"):
          owner.retry_backoff_seconds(attempt_count=0, initial_seconds=5, max_seconds=60)
  ```

  `retry_backoff_seconds` (L541-554) is keyword-only, doubles `initial_seconds` once per attempt after the first, and clamps at `max_seconds`; it has no test today, so this characterization records the baseline. Add `import pytest` to the module imports.

  Run only these nodes. Expected: RED with `ModuleNotFoundError: No module named 'deerflow.persistence.jobs.contracts'`.

- [ ] **Step 2: move the exact definitions into the leaf owner**

  Create `contracts.py` with docstring `"""Durable Job contracts: scopes, requests, claims, terminal events, ports, and errors."""` and move every definition in `JOBS_CONTRACT_NAMES` verbatim from the baseline coordinates: `JobType`/`RetrySafety` (27-36), `_SHA256_HEX` (38), `_DETERMINISTIC_NONRETRYABLE_ERROR_CODES` (39-54), `_durable_terminal_successor_idempotency_key` (57-62), `_dead_error_code_for_failure` (77-87), `JobScope` through `DeadJobRequeuedEvent` (91-308, including decorators, docstrings, and the L232-233 comment inside `EnqueueJob.__post_init__`), `JobTerminalEvent` through `JobRetryResult` (415-492), `JobAuditPort`/`JobTerminalPort` (495-526), the three exceptions (529-538), `retry_backoff_seconds` (541-554). Do not move `_is_durable_terminal_successor` (65-74), the registry block (311-411), `_lease_token_hash` (557-558), or anything below.

  Owner imports are exactly what the moved bodies use:

  ```python
  from __future__ import annotations

  import hashlib
  import re
  import uuid
  from dataclasses import dataclass, field
  from datetime import UTC, datetime
  from typing import Literal, Protocol

  from sqlalchemy.ext.asyncio import AsyncSession

  from deerflow.persistence.user.private_lifecycle import AccountPrivateGeneration
  from deerflow.public_error_codes import LLM_PUBLIC_ERROR_CODES
  from deerflow.trace_context import normalize_trace_id
  ```

  Give the owner no `__all__`; `sql.__all__` remains the public surface.

- [ ] **Step 3: re-import exact objects into `sql.py`**

  Delete the moved definitions and add, inside the first-party import block after `from deerflow.persistence.jobs.model import DeadJobRow, JobAttemptRow, JobRow`:

  ```python
  from deerflow.persistence.jobs.contracts import (  # noqa: F401 - compatibility exports
      _DETERMINISTIC_NONRETRYABLE_ERROR_CODES,
      _SHA256_HEX,
      DeadJobRecord,
      DeadJobRequeuedEvent,
      DurableDeadTerminalReconciliationRequest,
      DurableTerminalSuccessorRebindRequest,
      DurableTerminalTakeoverRequest,
      EnqueueJob,
      JobAuditPort,
      JobClaim,
      JobHeartbeat,
      JobIdempotencyConflict,
      JobOwnerRef,
      JobOwnerRefRequired,
      JobRequeueForbidden,
      JobRetryResult,
      JobScope,
      JobTerminalEvent,
      JobTerminalPort,
      JobTerminalResult,
      JobType,
      JobUnstartedClaimRelease,
      RetentionPurgeJobAuthority,
      RetrySafety,
      _dead_error_code_for_failure,
      _durable_terminal_successor_idempotency_key,
      retry_backoff_seconds,
  )
  ```

  Let `uvx ruff format`/`ruff check --fix` settle member order. Remove `re` (L6), `field` (L11), `Protocol` (L14), and `LLM_PUBLIC_ERROR_CODES` (L24) from `sql.py` imports; keep `hashlib` (`_lease_token_hash`), `Literal` (annotations L902, L1618, L1724, L1834-1835), `TypeGuard`, `dataclass` (`_DeadTerminalReconciliationCursor`), `weakref`, `Lock`, `AccountPrivateGeneration` (L1209, L2188), `normalize_trace_id` (L1473), `UTC`, `datetime`, `timedelta`, `secrets`, `uuid`, `Callable`, `sqlalchemy`, `insert`, `AsyncSession`, and every ORM row. `__all__` (L2273-2297) stays byte-identical. `jobs/__init__.py` is not modified.

- [ ] **Step 4: run job contract, settlement, and worker gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_secondary_hotspots.py \
    tests/test_python_module_decomposition_contract.py \
    tests/test_job_owner_lifecycle_contract.py \
    tests/test_model_output_limit_settlement.py \
    tests/test_memory_document_contract.py \
    tests/test_worker_service.py \
    -q -m "not postgres and not provider_integration"
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run --env-file ../.env python \
    tests/support/core_gate_plugin.py \
    tests/test_job_owner_lifecycle_postgres.py \
    tests/test_checkpoint_lease_atomicity_postgres.py \
    -q -m "not provider_integration"
  ```

  Require `test_job_owner_lifecycle_contract.py` to keep asserting `EnqueueJob(` call counts unchanged (`sql.py: 2`), the eight `__post_init__` error texts, `get_args(JobType)`, and `_dead_error_code_for_failure` outcomes; require the PostgreSQL gate to report `failed=0 skipped=0` (the `job_sql.datetime` patch still reaches `JobRepository._enqueue`/`_now`).

- [ ] **Step 5: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check packages/harness/deerflow/persistence/jobs tests/test_python_module_decomposition_secondary_hotspots.py
  uvx ruff format --check packages/harness/deerflow/persistence/jobs tests/test_python_module_decomposition_secondary_hotspots.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 2 paths with message `refactor(jobs): extract job contracts`.

## Task 3: Extract Run Snapshot contracts and admission rules

**Files:**

- Create: `backend/app/private_work/snapshot_contracts.py`
- Create: `backend/app/private_work/snapshot_admission_rules.py`
- Modify: `backend/app/private_work/snapshot_repository.py:1-169,172-331,577-584,801-831,1385-1495`
- Modify: `backend/tests/test_python_module_decomposition_secondary_hotspots.py`
- Modify: `backend/tests/test_agent_runtime_checksum.py:12,428-454`
- Modify: `backend/tests/test_automation_runtime_config.py:8`
- Modify: `backend/tests/test_memory_contract.py:110-116`

**Interfaces:**

- Consumes: `PrivateWorkTooLarge`, `PrivateWorkConflict`, `PrivateRunCreate`, `LockedAgentRuntimePolicy`, `AssetKind`, `AssetScope`, `ResolvedAssetSnapshot`, `ResolvedRunAssetClosure`, `ResolvedSkillVersionSnapshot`, `SharedAssetError`, `McpSecretClosure`, `McpDefinitionPolicyError`, `McpEndpointPolicy`, `validate_project_mcp_definition`, `parse_skill_secret_declarations`, `run_snapshot_codec` encoders/limits, `McpServerRow`, `McpServerVersionRow`.
- Produces: `snapshot_contracts.{RunSnapshotAssetStale, RunMcpSecretSnapshot, RunSkillSecretSnapshot, AdmittedRunModelSnapshot, RunModelSnapshotAdmissionPort, RunRuntimePolicyAdmissionPort, agent_model_snapshot_purpose}`; `snapshot_admission_rules.{_FORBIDDEN_PERSISTED_KEY_PARTS, _reject_secret_bearing_keys, _apply_runtime_recursion_limit, _RunAssetSnapshotAdmissionEncoder, _r1_snapshot_schema_version, asset_allowed, validate_project_mcp_secret_slots, validate_dependency_snapshots, validate_main_dependency_boundary}`.

- [ ] **Step 1: add the failing owner identity test**

  ```python
  def test_snapshot_owners_are_the_exact_legacy_exports() -> None:
      contracts = importlib.import_module("app.private_work.snapshot_contracts")
      rules = importlib.import_module("app.private_work.snapshot_admission_rules")
      for name in SNAPSHOT_CONTRACT_NAMES:
          assert getattr(snapshot_legacy, name) is getattr(contracts, name), name
      for name in SNAPSHOT_RULE_MODULE_NAMES:
          assert getattr(snapshot_legacy, name) is getattr(rules, name), name
      for legacy_name, owner_name in SNAPSHOT_RULE_STATICMETHODS.items():
          descriptor = snapshot_legacy.RunSnapshotRepository.__dict__[legacy_name]
          assert isinstance(descriptor, staticmethod), legacy_name
          assert descriptor.__func__ is getattr(rules, owner_name), legacy_name
      assert rules.RunSnapshotAssetStale is contracts.RunSnapshotAssetStale
      rules_imports = _module_imports(PRIVATE_WORK_ROOT / "snapshot_admission_rules.py")
      assert "app.private_work.snapshot_contracts" in rules_imports
      assert not {SNAPSHOT_MODULE, ".snapshot_repository"} & rules_imports
      assert not {SNAPSHOT_MODULE, ".snapshot_repository", "app.private_work.snapshot_admission_rules"} & _module_imports(PRIVATE_WORK_ROOT / "snapshot_contracts.py")
  ```

  Run only this node. Expected: RED with `ModuleNotFoundError: No module named 'app.private_work.snapshot_contracts'`.

- [ ] **Step 2: create `snapshot_contracts.py`**

  Docstring `"""Run Snapshot admission contracts: stale signal, secret DTOs, and admission ports."""`. Move verbatim: `agent_model_snapshot_purpose` (225-230), `RunMcpSecretSnapshot` (233-240 including decorator), `RunSkillSecretSnapshot` (243-250), `RunSnapshotAssetStale` (253-254), `AdmittedRunModelSnapshot` (257-265), `RunModelSnapshotAdmissionPort` (268-281), `RunRuntimePolicyAdmissionPort` (284-301). Imports:

  ```python
  from __future__ import annotations

  import uuid
  from collections.abc import Mapping
  from dataclasses import dataclass
  from typing import Protocol

  from sqlalchemy.ext.asyncio import AsyncSession

  from app.system_runtime_settings.models import LockedAgentRuntimePolicy
  ```

  Keep `TypeError("Agent definition_id must be a UUID")` and the `f"agent.{definition_id.hex}"` return unchanged.

- [ ] **Step 3: create `snapshot_admission_rules.py`**

  Docstring `"""Pure Run Snapshot admission rules: encoding budget, key rejection, recursion clamp, closure validators."""`. Move verbatim: `_FORBIDDEN_PERSISTED_KEY_PARTS` (162-169), `_RunAssetSnapshotAdmissionEncoder` (172-213 including decorator), `_r1_snapshot_schema_version` (216-222), `_apply_runtime_recursion_limit` (304-319), `_reject_secret_bearing_keys` (322-331). Move the four staticmethod bodies as module-level functions with the `@staticmethod` decorator and one indentation level removed, keeping every parameter, annotation, default, docstring, and raise site:

  ```python
  def asset_allowed(*, asset_scope: str, asset_project_id: uuid.UUID | None, project_id: uuid.UUID) -> bool:  # body of L578-584


  def validate_project_mcp_secret_slots(
      mcps: list[tuple[McpServerRow, McpServerVersionRow]],
      closures: Mapping[uuid.UUID, McpSecretClosure],
      *,
      endpoint_policy: McpEndpointPolicy | None,
  ) -> None:  # body of L802-831


  def validate_dependency_snapshots(rows: list[tuple[object, object]], snapshots: tuple[object, ...], *, catalog_generation: int) -> None:  # body of L1386-1432


  def validate_main_dependency_boundary(closure: ResolvedRunAssetClosure, *, canonical_main: bool) -> None:  # body of L1435-1495
  ```

  Imports:

  ```python
  from __future__ import annotations

  import uuid
  from collections.abc import Mapping
  from dataclasses import dataclass, replace

  from app.private_work.errors import PrivateWorkConflict, PrivateWorkTooLarge
  from app.private_work.run_repository import PrivateRunCreate
  from app.private_work.snapshot_contracts import RunSnapshotAssetStale
  from app.shared_assets.errors import SharedAssetError
  from app.shared_assets.mcp_secret_closure import McpSecretClosure
  from app.shared_assets.models import (
      AssetKind,
      AssetScope,
      ResolvedAssetSnapshot,
      ResolvedRunAssetClosure,
      ResolvedSkillVersionSnapshot,
  )
  from app.shared_assets.run_snapshot_codec import (
      MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES,
      MAX_RUN_SKILL_REFERENCE_MANIFEST_JSON_BYTES,
      RUN_ASSET_SNAPSHOT_SCHEMA_VERSION,
      RunAssetSnapshotTooLarge,
      encode_run_asset_snapshot,
      encode_run_skill_version_manifest,
      encoded_run_asset_snapshot_json_size,
  )
  from app.shared_assets.skill_secret_policy import parse_skill_secret_declarations
  from app.system_runtime_settings.models import LockedAgentRuntimePolicy
  from deerflow.mcp_definition_policy import McpDefinitionPolicyError, McpEndpointPolicy, validate_project_mcp_definition
  from deerflow.persistence.shared_assets.mcp_model import McpServerRow, McpServerVersionRow
  ```

  Verify with `PYTHONPATH=. uv run python -c 'import app.private_work.snapshot_admission_rules'` that none of these modules imports `snapshot_repository` (the audit confirmed `run_repository`, `errors`, `shared_assets.*`, `system_runtime_settings.models`, and `deerflow.mcp_definition_policy` do not).

- [ ] **Step 4: re-export from `snapshot_repository.py` and alias the staticmethods**

  Delete the moved definitions and the four staticmethod bodies. Add, next to the other `app.private_work` imports:

  ```python
  from app.private_work.snapshot_admission_rules import (  # noqa: F401 - compatibility exports
      _FORBIDDEN_PERSISTED_KEY_PARTS,
      _RunAssetSnapshotAdmissionEncoder,
      _apply_runtime_recursion_limit,
      _r1_snapshot_schema_version,
      _reject_secret_bearing_keys,
      asset_allowed,
      validate_dependency_snapshots,
      validate_main_dependency_boundary,
      validate_project_mcp_secret_slots,
  )
  from app.private_work.snapshot_contracts import (  # noqa: F401 - compatibility exports
      AdmittedRunModelSnapshot,
      RunMcpSecretSnapshot,
      RunModelSnapshotAdmissionPort,
      RunRuntimePolicyAdmissionPort,
      RunSkillSecretSnapshot,
      RunSnapshotAssetStale,
      agent_model_snapshot_purpose,
  )
  ```

  Inside `RunSnapshotRepository`, at the position of the former `_asset_allowed` (L577), add the four aliases:

  ```python
      _asset_allowed = staticmethod(asset_allowed)
      _validate_project_mcp_secret_slots = staticmethod(validate_project_mcp_secret_slots)
      _validate_dependency_snapshots = staticmethod(validate_dependency_snapshots)
      _validate_main_dependency_boundary = staticmethod(validate_main_dependency_boundary)
  ```

  The call sites `RunSnapshotRepository._asset_allowed(...)` (L633, L668, L715) and `self._validate_*` (L1528, L1537, L1564, L1575, L1646, L1793) do not change. Remove `PrivateWorkTooLarge`, `ResolvedAssetSnapshot`, `MAX_RUN_ASSET_SNAPSHOT_JSON_BYTES`, `MAX_RUN_SKILL_REFERENCE_MANIFEST_JSON_BYTES`, `RunAssetSnapshotTooLarge`, `encode_run_asset_snapshot`, `encode_run_skill_version_manifest`, `encoded_run_asset_snapshot_json_size`, `parse_skill_secret_declarations`, and `Protocol` from `snapshot_repository.py` imports only if no staying body still references them (the audit found none); keep `dataclass` only if a staying dataclass remains (none does after this task) and let Ruff report the exact set.

- [ ] **Step 5: migrate the encoder seam and the direct private imports**

  - `tests/test_agent_runtime_checksum.py`: change L12 to `from app.private_work import snapshot_admission_rules as snapshot_admission_rules_module` and update L428-432, L435, L448-453, L454 to patch `encode_run_asset_snapshot` / `encoded_run_asset_snapshot_json_size` on, and construct `_RunAssetSnapshotAdmissionEncoder` from, `snapshot_admission_rules_module`. Keep `RunSnapshotAssetStale` imported from `snapshot_repository` (L15) to exercise the façade.
  - `tests/test_automation_runtime_config.py:8`: import `_apply_runtime_recursion_limit` from `app.private_work.snapshot_admission_rules`.
  - `tests/test_memory_contract.py:110-116`: add `"private_work/snapshot_contracts.py"` and `"private_work/snapshot_admission_rules.py"` to the scanned tuple so the neutral-contract guard covers the new owners.
  - Leave `tests/test_memory_admission_snapshot.py`, `tests/support/run_skill_writer_cohort.py`, `tests/test_mcp_query_secrets.py` (calls `RunSnapshotRepository._validate_project_mcp_secret_slots`, satisfied by the alias), and `tests/test_run_skill_writer_cohort_contract.py` unchanged.

- [ ] **Step 6: run snapshot gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_secondary_hotspots.py \
    tests/test_agent_runtime_checksum.py \
    tests/test_memory_admission_snapshot.py \
    tests/test_mcp_query_secrets.py \
    tests/test_run_asset_facts.py \
    tests/test_run_execution_profile.py \
    tests/test_run_workload_profile.py \
    tests/test_automation_runtime_config.py \
    tests/test_run_skill_writer_cohort_contract.py \
    tests/test_agent_runtime_assessment.py \
    tests/test_mcp_runtime_contracts.py \
    tests/test_memory_contract.py \
    -q -m "not postgres and not provider_integration"
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run --env-file ../.env python \
    tests/support/core_gate_plugin.py \
    tests/test_run_skill_version_refs_postgres.py \
    tests/test_mcp_secret_lifecycle_postgres.py \
    -q -m "not provider_integration"
  ```

  Require the two encoder budget tests (`test_agent_runtime_checksum.py:420,442`) to still observe the patched encoders and raise `PrivateWorkTooLarge`, the cohort source contract to pass, and the PostgreSQL gate to report `failed=0 skipped=0`.

- [ ] **Step 7: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check app/private_work/snapshot_repository.py app/private_work/snapshot_contracts.py app/private_work/snapshot_admission_rules.py \
    tests/test_python_module_decomposition_secondary_hotspots.py tests/test_agent_runtime_checksum.py tests/test_automation_runtime_config.py tests/test_memory_contract.py
  uvx ruff format --check app/private_work/snapshot_repository.py app/private_work/snapshot_contracts.py app/private_work/snapshot_admission_rules.py \
    tests/test_python_module_decomposition_secondary_hotspots.py tests/test_agent_runtime_checksum.py tests/test_automation_runtime_config.py tests/test_memory_contract.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 3 paths with message `refactor(snapshot): extract admission contracts and rules`.

## Task 4: Extract the pure asset-row planning phase from `create_run_with_snapshot_in_session()`

**Files:**

- Modify: `backend/app/private_work/snapshot_admission_rules.py`
- Modify: `backend/app/private_work/snapshot_repository.py:1214-1339`
- Modify: `backend/tests/test_python_module_decomposition_secondary_hotspots.py`

**Interfaces:**

- Consumes: `_RunAssetSnapshotAdmissionEncoder`, `_r1_snapshot_schema_version`, `RunSnapshotAssetStale`, `RunAssetVersionRow`, `RunSkillVersionRefRow`, `AssetKind`, `AssetScope`, `ResolvedSkillVersionSnapshot`, `RUN_ASSET_SNAPSHOT_SCHEMA_VERSION`, `RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION`, `PreparedLegacyRunSkillSnapshots`, `PrivateRunRecord`, `PrivateWorkContext`, `ResolvedAgentSnapshot`, `ResolvedRunAssetClosure`, `SkillRow`, `SkillVersionRow`, `McpServerRow`, `McpServerVersionRow`.
- Produces: `PlannedRunAssetRows(asset_rows, skill_ref_rows)` and `plan_run_asset_rows(...)`.

- [ ] **Step 1: add the failing phase test**

  ```python
  def test_snapshot_asset_row_planning_is_owned_by_the_rules_module() -> None:
      rules = importlib.import_module("app.private_work.snapshot_admission_rules")
      assert tuple(field.name for field in dataclasses.fields(rules.PlannedRunAssetRows)) == ("asset_rows", "skill_ref_rows")
      assert tuple(inspect.signature(rules.plan_run_asset_rows).parameters) == (
          "context",
          "thread_id",
          "run",
          "lead_agent",
          "resolved_closure",
          "skills",
          "skill_snapshots",
          "mcps",
          "mcp_snapshots",
          "prepared_legacy_skills",
      )
      function = _function_node(SNAPSHOT_PATH, "create_run_with_snapshot_in_session", class_name="RunSnapshotRepository")
      assert _await_callee_names(function) == EXPECTED_SNAPSHOT_ADMISSION_AWAITS
      called = {node.func.id for node in ast.walk(function) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
      assert "plan_run_asset_rows" in called
      assert "_RunAssetSnapshotAdmissionEncoder" not in called
  ```

  Run only this node. Expected: RED with `AttributeError: module 'app.private_work.snapshot_admission_rules' has no attribute 'PlannedRunAssetRows'`.

- [ ] **Step 2: add the planner to `snapshot_admission_rules.py`**

  ```python
  @dataclass(frozen=True)
  class PlannedRunAssetRows:
      asset_rows: list[RunAssetVersionRow]
      skill_ref_rows: list[RunSkillVersionRefRow]


  def plan_run_asset_rows(
      *,
      context: PrivateWorkContext,
      thread_id: str,
      run: PrivateRunRecord,
      lead_agent: ResolvedAgentSnapshot,
      resolved_closure: ResolvedRunAssetClosure | None,
      skills: list[tuple[SkillRow, SkillVersionRow]],
      skill_snapshots: tuple[object, ...],
      mcps: list[tuple[McpServerRow, McpServerVersionRow]],
      mcp_snapshots: tuple[object, ...],
      prepared_legacy_skills: PreparedLegacyRunSkillSnapshots | None,
  ) -> PlannedRunAssetRows:
      """Build the Run asset and Skill reference rows in dependency order under one encoder budget."""

      # L1214-1337 verbatim, dedented one level.
      return PlannedRunAssetRows(asset_rows=asset_rows, skill_ref_rows=skill_ref_rows)
  ```

  The body is L1214-1337 with no textual change other than dedent: the encoder is constructed from `context.request_id`, `dependency_order` starts at `1` after the lead row, the delegated loop runs only `if resolved_closure is not None`, the skill loop keeps `zip(skills, skill_snapshots, strict=True)`, the `type(skill_snapshot) is not ResolvedSkillVersionSnapshot` guard, the `prepared_legacy_skills is None` branch pair, and the `RunSkillVersionRefRow` construction with `skill_project_id=(None if asset.scope == AssetScope.SYSTEM.value else asset.project_id)`; the MCP loop keeps `zip(mcps, mcp_snapshots, strict=True)`. Before moving, confirm the exact annotation of `skill_snapshots`/`mcp_snapshots` by reading how they are derived at L1006-1009 and use those types instead of `tuple[object, ...]` if they are narrower. Add these imports to the rules module: `from app.private_work.context import PrivateWorkContext`, `from app.private_work.legacy_run_skill_snapshot_writer import PreparedLegacyRunSkillSnapshots`, `from app.private_work.run_repository import PrivateRunRecord` (extend the existing `run_repository` import), `ResolvedAgentSnapshot` (extend the `models` import), `RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION` (extend the codec import), `from deerflow.persistence.private_work.model import RunAssetVersionRow, RunSkillVersionRefRow`, `from deerflow.persistence.shared_assets.skill_model import SkillRow, SkillVersionRow`. Verify `legacy_run_skill_snapshot_writer` and `context` do not import `snapshot_repository` (`rg -n 'snapshot_repository' backend/app/private_work/legacy_run_skill_snapshot_writer.py backend/app/private_work/context.py` must be empty).

- [ ] **Step 3: call the planner from the transaction body**

  Replace L1214-1337 with:

  ```python
          planned_rows = plan_run_asset_rows(
              context=context,
              thread_id=thread_id,
              run=run,
              lead_agent=lead_agent,
              resolved_closure=resolved_closure,
              skills=skills,
              skill_snapshots=skill_snapshots,
              mcps=mcps,
              mcp_snapshots=mcp_snapshots,
              prepared_legacy_skills=prepared_legacy_skills,
          )
          asset_rows = planned_rows.asset_rows
          skill_ref_rows = planned_rows.skill_ref_rows
  ```

  L1338-1339 (`session.add_all(asset_rows)`, `session.add_all(skill_ref_rows)`) and everything after stay byte-identical. Add `plan_run_asset_rows` to the `snapshot_admission_rules` import block in `snapshot_repository.py` (it is used, so it needs no `noqa`; keep it in the same block). Remove `RunAssetVersionRow`, `RunSkillVersionRefRow`, `RUN_SKILL_VERSION_REFERENCE_SCHEMA_VERSION`, and `ResolvedSkillVersionSnapshot` from `snapshot_repository.py` imports only if Ruff reports them unused (`RunAssetVersionRow` is still used by `list_asset_facts_in_session` L1692; `ResolvedSkillVersionSnapshot` is still used in annotations of secret methods).

- [ ] **Step 4: run snapshot gates**

  Run the same offline and PostgreSQL commands as Task 3 Step 6. Additionally run:

  ```bash
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run --env-file ../.env python \
    tests/support/core_gate_plugin.py \
    tests/test_agent_archive_run_postgres.py \
    tests/test_current_version_entry_points_postgres.py \
    tests/test_pinned_skill_version_materializer_postgres.py \
    -q -m "not provider_integration"
  ```

  Require `failed=0 skipped=0` on every PostgreSQL run: these files admit real Runs through `create_run_with_snapshot_in_session()` and read back `RunAssetVersionRow`/`RunSkillVersionRefRow` order and schema versions.

- [ ] **Step 5: static checkpoint and conditional commit**

  Run the Task 3 Step 7 commands. If authorized, commit only Task 4 paths with message `refactor(snapshot): extract asset row planning`.

## Task 5: Extract checkpoint receipt repair

**Files:**

- Create: `backend/app/private_work/checkpoint_receipt_repair.py`
- Modify: `backend/app/private_work/checkpointer.py:1-144,320-330,371-741`
- Modify: `backend/tests/test_python_module_decomposition_secondary_hotspots.py`
- Modify: `backend/tests/test_context_compaction_checkpoint_repair.py:8,152-161`
- Modify: `backend/tests/test_context_provider_checkpoint_recovery.py:10,247-252,295-300,350-355,394-399,444-449,488-493`

**Interfaces:**

- Consumes: `RunnableConfig`, `CheckpointTuple`, `AsyncSession`, `PrivateWorkContext`, `require_issued_private_work_context`, `PrivateWorkNotFound`, the Memory receipt constants, `MemoryDocumentRepository`, `MemoryDocumentScope`, `MemoryHistoryActivation`, `SystemModelExecutionProvenance`, `ContextEvidenceRepository`, `ContextEvidenceScope`, `ContextSubjectRef`, `ContextProjectionTransaction`, `context_evidence_record_to_core`, the three `context_replacement` sources, the `context_evidence` core types, `provider_response_digest`, `ContextProviderCallAmbiguousError`.
- Produces: `thread_id_from_config(config)`, `checkpoint_id_from_config(config)`, `memory_history_activation(context, item, *, thread_id)`, `repair_memory_archive_receipt(session, context, item, *, thread_id)`, `context_compaction_activation(item, *, thread_id)`, `repair_context_compaction_receipt(session, context, item, *, thread_id)`, `context_provider_checkpoint_activation(item, *, thread_id)`, `validate_context_provider_checkpoint_response(item, snapshot)`, `repair_context_provider_checkpoint(session, context, item, *, thread_id, observer_run_id)`.

- [ ] **Step 1: add the failing owner identity test**

  ```python
  def test_checkpoint_receipt_repair_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("app.private_work.checkpoint_receipt_repair")
      for name in CHECKPOINT_REPAIR_OWNER_NAMES:
          assert hasattr(owner, name), name
      saver = checkpointer_legacy._ScopedCheckpointSaver
      for legacy_name, owner_name in CHECKPOINT_REPAIR_STATICMETHODS.items():
          descriptor = saver.__dict__[legacy_name]
          assert isinstance(descriptor, staticmethod), legacy_name
          assert descriptor.__func__ is getattr(owner, owner_name), legacy_name
      for name in CHECKPOINT_REPAIR_DELEGATING_METHODS:
          assert callable(saver.__dict__[name]), name
      assert not hasattr(checkpointer_legacy, "_MEMORY_ARCHIVE_RECEIPT_FIELDS")
      owner_imports = _module_imports(PRIVATE_WORK_ROOT / "checkpoint_receipt_repair.py")
      assert not {CHECKPOINTER_MODULE, ".checkpointer"} & owner_imports
      for name in ("ContextEvidenceRepository", "ContextProjectionTransaction"):
          assert hasattr(owner, name), name
  ```

  Run only this node. Expected: RED with `ModuleNotFoundError: No module named 'app.private_work.checkpoint_receipt_repair'`.

- [ ] **Step 2: create the owner**

  Docstring `"""Checkpoint receipt repair: Memory, Context compaction, and Provider response convergence."""`. Move `_MEMORY_ARCHIVE_RECEIPT_FIELDS` (126-144) verbatim. Move the two staticmethod bodies as module functions:

  ```python
  def thread_id_from_config(config: RunnableConfig | None) -> str:  # body of L321-330, keeps PrivateWorkNotFound("unknown") x3


  def checkpoint_id_from_config(config: RunnableConfig | None) -> str | None:  # body of L372-379
  ```

  Move the seven repair methods as module functions with `self` replaced by explicit parameters and every other token unchanged:

  ```python
  def memory_history_activation(context: PrivateWorkContext, item: CheckpointTuple, *, thread_id: str) -> MemoryHistoryActivation | None:
      # L381-438: `require_issued_private_work_context(self._context)` -> `require_issued_private_work_context(context)`;
      # `self._checkpoint_id(` -> `checkpoint_id_from_config(`


  async def repair_memory_archive_receipt(session: AsyncSession, context: PrivateWorkContext, item: CheckpointTuple, *, thread_id: str) -> None:
      # L440-453: `self._memory_history_activation(item, thread_id=thread_id)` -> `memory_history_activation(context, item, thread_id=thread_id)`


  def context_compaction_activation(item: CheckpointTuple, *, thread_id: str) -> tuple[str, ContextCompactionCheckpointReceipt] | None:
      # L455-491: `self._checkpoint_id(` -> `checkpoint_id_from_config(`; `self._thread_id(` -> `thread_id_from_config(`


  async def repair_context_compaction_receipt(session: AsyncSession, context: PrivateWorkContext, item: CheckpointTuple, *, thread_id: str) -> None:
      # L493-544: `self._context_compaction_activation(` -> `context_compaction_activation(`;
      # `require_issued_private_work_context(self._context)` -> `require_issued_private_work_context(context)`


  def context_provider_checkpoint_activation(item: CheckpointTuple, *, thread_id: str) -> tuple[str, ContextCheckpointProjectionSnapshot] | None:
      # L546-590: same two substitutions as context_compaction_activation


  def validate_context_provider_checkpoint_response(item: CheckpointTuple, snapshot: ContextCheckpointProjectionSnapshot) -> None:
      # L593-623 verbatim (was already a staticmethod)


  async def repair_context_provider_checkpoint(
      session: AsyncSession,
      context: PrivateWorkContext,
      item: CheckpointTuple,
      *,
      thread_id: str,
      observer_run_id: str | None,
  ) -> str | None:
      # L625-741: `self._context_provider_checkpoint_activation(` -> `context_provider_checkpoint_activation(`;
      # `self._validate_context_provider_checkpoint_response(` -> `validate_context_provider_checkpoint_response(`;
      # `require_issued_private_work_context(self._context)` -> `require_issued_private_work_context(context)`;
      # delete the four-line `observer_run_id = getattr(getattr(self, "_context_evidence_observer", None), "run_id", None)` at L697-701
      # because the value now arrives as the parameter; keep `# pragma: no cover` at L648 and every error text.
  ```

  Imports are the repair-only group plus shared names: `from __future__ import annotations`; `uuid`; `Mapping`, `Sequence`; `RunnableConfig`; `CheckpointTuple`; `AsyncSession`; `PrivateWorkContext`, `require_issued_private_work_context`; `PrivateWorkNotFound`; `ContextProjectionTransaction`, `context_evidence_record_to_core`; `idle_checkpoint_projection_source`, `idle_compaction_projection_source`, `source_from_checkpoint_snapshot`; `MEMORY_ARCHIVE_RECEIPT_KEY`, `MEMORY_ARCHIVE_RECEIPT_VERSION`, `SNIP_ARCHIVE_PROMPT_VERSION`; `CONTEXT_COMPACTION_RECEIPT_STATE_KEY`, `CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY`, `provider_response_digest`; `SystemModelExecutionProvenance`; `ContextProviderCallAmbiguousError`; `ContextEvidenceRepository`, `ContextEvidenceScope`, `ContextSubjectRef`; `MemoryDocumentRepository`, `MemoryDocumentScope`, `MemoryHistoryActivation`; `CheckpointLinkedV1`, `CompactionCommittedV1`, `ContextCheckpointProjectionSnapshot`, `ContextCompactionCheckpointReceipt`, `ContextProjectionHead`, `ContextSubject`, `ProjectionPhase`, `ProviderCallDisposition`, `RequestPreparedV1`, `resolve_provider_call`. Verify that `app.private_work.context_projection` and `app.private_work.context_replacement` do not import `checkpointer` (`rg -n 'checkpointer' backend/app/private_work/context_projection.py backend/app/private_work/context_replacement.py` must be empty).

- [ ] **Step 3: delegate from `_ScopedCheckpointSaver`**

  Delete `_MEMORY_ARCHIVE_RECEIPT_FIELDS` and the bodies of `_thread_id`, `_checkpoint_id`, and the seven repair methods. Add `from app.private_work.checkpoint_receipt_repair import (...)` importing the nine functions (`checkpoint_id_from_config`, `context_compaction_activation`, `context_provider_checkpoint_activation`, `memory_history_activation`, `repair_context_compaction_receipt`, `repair_context_provider_checkpoint`, `repair_memory_archive_receipt`, `thread_id_from_config`, `validate_context_provider_checkpoint_response`); do not import `_MEMORY_ARCHIVE_RECEIPT_FIELDS`, which no staying body uses. Inside the class, at the former `_thread_id` position (L320):

  ```python
      _thread_id = staticmethod(thread_id_from_config)
      _checkpoint_id = staticmethod(checkpoint_id_from_config)
      _validate_context_provider_checkpoint_response = staticmethod(validate_context_provider_checkpoint_response)

      def _memory_history_activation(self, item: CheckpointTuple, *, thread_id: str) -> MemoryHistoryActivation | None:
          return memory_history_activation(self._context, item, thread_id=thread_id)

      async def _repair_memory_archive_receipt(self, session: AsyncSession, item: CheckpointTuple, *, thread_id: str) -> None:
          await repair_memory_archive_receipt(session, self._context, item, thread_id=thread_id)

      def _context_compaction_activation(self, item: CheckpointTuple, *, thread_id: str) -> tuple[str, ContextCompactionCheckpointReceipt] | None:
          return context_compaction_activation(item, thread_id=thread_id)

      async def _repair_context_compaction_receipt(self, session: AsyncSession, item: CheckpointTuple, *, thread_id: str) -> None:
          await repair_context_compaction_receipt(session, self._context, item, thread_id=thread_id)

      def _context_provider_checkpoint_activation(self, item: CheckpointTuple, *, thread_id: str) -> tuple[str, ContextCheckpointProjectionSnapshot] | None:
          return context_provider_checkpoint_activation(item, thread_id=thread_id)

      async def _repair_context_provider_checkpoint(self, session: AsyncSession, item: CheckpointTuple, *, thread_id: str) -> str | None:
          observer_run_id = getattr(
              getattr(self, "_context_evidence_observer", None),
              "run_id",
              None,
          )
          return await repair_context_provider_checkpoint(session, self._context, item, thread_id=thread_id, observer_run_id=observer_run_id)
  ```

  Every caller (`aget_tuple`, `aget_tuple_already_authorized`, `aput`, `_aput_execution_atomic`, `aput_already_authorized`) is untouched. Remove from `checkpointer.py` only the imports Ruff reports unused after the move (expected: `MEMORY_ARCHIVE_RECEIPT_KEY`, `MEMORY_ARCHIVE_RECEIPT_VERSION`, `SNIP_ARCHIVE_PROMPT_VERSION`, `provider_response_digest`, `SystemModelExecutionProvenance`, `ContextEvidenceRepository`, `ContextEvidenceScope`, `ContextSubjectRef`, `MemoryDocumentRepository`, `MemoryDocumentScope`, `ContextProjectionTransaction`, `context_evidence_record_to_core`, the three `context_replacement` sources, `CheckpointLinkedV1`, `CompactionCommittedV1`, `ContextProjectionHead`, `ContextSubject`, `ProjectionPhase`, `ProviderCallDisposition`, `RequestPreparedV1`, `resolve_provider_call`, `Sequence`). Keep `MemoryHistoryActivation`, `ContextCompactionCheckpointReceipt`, `ContextCheckpointProjectionSnapshot`, `CheckpointTuple`, `ContextProviderCallAmbiguousError` (still used by the wrappers' annotations and the `except` clauses at L805/876/1041/1139/1297), `PrivateWorkRevalidator`, `PrivateThreadRepository`, and `transition_output_delivery_obligation_for_approval_terminal` (string-patched and staying seams).

- [ ] **Step 4: migrate the repair monkeypatch targets**

  - `tests/test_context_compaction_checkpoint_repair.py`: add `from app.private_work import checkpoint_receipt_repair as repair_module` and retarget L152-161 `monkeypatch.setattr(repair_module, "ContextEvidenceRepository", ...)` / `(repair_module, "ContextProjectionTransaction", ...)`. Keep constructing `_ScopedCheckpointSaver` via `object.__new__` and calling `saver._repair_context_compaction_receipt(object(), item, thread_id=THREAD_ID)`.
  - `tests/test_context_provider_checkpoint_recovery.py`: same import and retarget at the six `setattr` pairs (L247/252, 295/300, 350/355, 394/399, 444/449, 488/493). Keep the direct `saver._repair_context_provider_checkpoint(...)` calls; the fixture omitting `_context_evidence_observer` still works through the wrapper's `getattr` default.
  - Leave `tests/test_output_delivery_obligation_cancellation.py` and `tests/test_skill_builder_checkpoint_scope.py` unchanged (their seams stay in `checkpointer`).

- [ ] **Step 5: run checkpointer gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_secondary_hotspots.py \
    tests/test_skill_builder_checkpoint_scope.py \
    tests/test_context_compaction_checkpoint_repair.py \
    tests/test_context_provider_checkpoint_recovery.py \
    tests/test_output_delivery_obligation_cancellation.py \
    tests/test_memory_archive_receipt.py \
    tests/test_cancelled_delegation_settlement.py \
    -q -m "not postgres and not provider_integration"
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run --env-file ../.env python \
    tests/support/core_gate_plugin.py \
    tests/test_checkpoint_lease_atomicity_postgres.py \
    tests/test_memory_archive_receipt_postgres.py \
    tests/test_thread_checkpoint_delete_recovery_postgres.py \
    tests/test_memory_seal_postgres.py \
    -q -m "not provider_integration"
  ```

  Require the eight provider-recovery tests and the compaction-repair test to pass with the retargeted patches, the memory archive receipt boundary ordering to pass, and the PostgreSQL gate to report `failed=0 skipped=0` (lease validation, repair-on-read commits through `_locked_active`, and thread deletion are exercised end to end).

- [ ] **Step 6: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check app/private_work/checkpointer.py app/private_work/checkpoint_receipt_repair.py \
    tests/test_python_module_decomposition_secondary_hotspots.py tests/test_context_compaction_checkpoint_repair.py tests/test_context_provider_checkpoint_recovery.py
  uvx ruff format --check app/private_work/checkpointer.py app/private_work/checkpoint_receipt_repair.py \
    tests/test_python_module_decomposition_secondary_hotspots.py tests/test_context_compaction_checkpoint_repair.py tests/test_context_provider_checkpoint_recovery.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 5 paths with message `refactor(checkpointer): extract receipt repair`.

## Task 6: Extract the snip prompt planner

**Files:**

- Create: `backend/packages/harness/deerflow/agents/middlewares/snip_planner.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py:1-92,111-137,151-166,180-217,457-532,771-1130,2146`
- Modify: `backend/tests/test_python_module_decomposition_secondary_hotspots.py`

**Interfaces:**

- Consumes: `hashlib`, `html`, `json`, `logging`, `Mapping`, `Callable`, `dataclass`, `Any`, `AIMessage`, `AnyMessage`, `HumanMessage`, `SystemMessage`, `ToolMessage`, `get_buffer_string`, `ContextCompactionFailureReason`, `validate_summary_prompt_template`.
- Produces: `MIN_SNIP_SUMMARY_OUTPUT_TOKENS`, `MAX_SNIP_HIERARCHICAL_LEAVES`, `MAX_SNIP_HIERARCHICAL_MODEL_CALLS`, `SnipPromptBudgetTooSmall`, `SnipSourceTooLarge`, `SnipCompactionFailed`, `SnipModelOutputInvalid`, `_ensure_snip_summary_output_budget`, `_SnipSummary`, `_ProjectionField`, `_ProjectionFragment`, `_SnipPromptPlan`, `_SnipReductionStep`, `SnipPromptBudget`, `intermediate_summary_text`, `assemble_summary_input_text`, `build_summary_input_text`, `projection_text`, `quoted_projection_value`, `projection_material`, `render_projection`, `reduction_prompt`, `plan_reduction_step`, `build_summary_prompt_from_formatted`, `projection_prompt`, `fit_projection_prefix`, `build_projection_prompts`, `build_snip_prompt_plan`, `build_summary_prompt`.

- [ ] **Step 1: add the failing owner identity and shape tests**

  ```python
  SNIP_PLANNER_FUNCTIONS = (
      "intermediate_summary_text",
      "assemble_summary_input_text",
      "build_summary_input_text",
      "projection_text",
      "quoted_projection_value",
      "projection_material",
      "render_projection",
      "reduction_prompt",
      "plan_reduction_step",
      "build_summary_prompt_from_formatted",
      "projection_prompt",
      "fit_projection_prefix",
      "build_projection_prompts",
      "build_snip_prompt_plan",
      "build_summary_prompt",
  )
  SNIP_PLANNER_MOVED_INTERNALS = (
      "_intermediate_summary_text",
      "_reduction_prompt",
      "_assemble_summary_input_text",
      "_build_summary_input_text",
      "_build_summary_prompt_from_formatted",
      "_projection_text",
      "_quoted_projection_value",
      "_projection_material",
      "_render_projection",
      "_projection_prompt",
      "_fit_projection_prefix",
      "_build_projection_prompts",
  )


  def test_snip_planner_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.agents.middlewares.snip_planner")
      for name in SNIP_PLANNER_NAMES:
          assert getattr(summarization_legacy, name) is getattr(owner, name), name
      for name in SNIP_PLANNER_FUNCTIONS:
          assert callable(getattr(owner, name)), name
      assert tuple(field.name for field in dataclasses.fields(owner.SnipPromptBudget)) == (
          "summary_prompt",
          "dual_output_contract",
          "prompt_within_budget",
          "prompt_with_repair_within_budget",
      )
      class_names = _class_defined_names(SUMMARIZATION_PATH, "DeerFlowSummarizationMiddleware")
      assert not set(SNIP_PLANNER_MOVED_INTERNALS) & class_names, set(SNIP_PLANNER_MOVED_INTERNALS) & class_names
      for name in ("_plan_reduction_step", "_build_snip_prompt_plan", "_build_summary_prompt", "_snip_prompt_budget"):
          assert name in class_names, name
      assert issubclass(owner.SnipModelOutputInvalid, owner.SnipCompactionFailed)
      _assert_owner_imports_are_clean(MIDDLEWARES_ROOT / "snip_planner.py", forbidden_modules=(SUMMARIZATION_MODULE, ".summarization_middleware", ".turn_compaction", ".compaction_receipts"), forbidden_prefixes=("app", "sqlalchemy"))
  ```

  Run only this node. Expected: RED with `ModuleNotFoundError: No module named 'deerflow.agents.middlewares.snip_planner'`.

- [ ] **Step 2: create the owner with the verbatim module-level moves**

  Docstring `"""SNIP prompt planning: budgets, projection prompts, and hierarchical reduction steps."""`, its own `logger = logging.getLogger(__name__)`. Move verbatim: `MIN_SNIP_SUMMARY_OUTPUT_TOKENS`, `MAX_SNIP_HIERARCHICAL_LEAVES`, `MAX_SNIP_HIERARCHICAL_MODEL_CALLS` (90-92); `SnipPromptBudgetTooSmall`, `SnipSourceTooLarge`, `SnipCompactionFailed` (with its `__init__` 126-133), `SnipModelOutputInvalid` (111-137); `_ensure_snip_summary_output_budget` (151-166); `_SnipSummary`, `_ProjectionField`, `_ProjectionFragment`, `_SnipPromptPlan`, `_SnipReductionStep` (180-217 including decorators). Then add:

  ```python
  @dataclass(frozen=True)
  class SnipPromptBudget:
      """Frozen prompt inputs and budget predicates the middleware lends to the planner."""

      summary_prompt: str
      dual_output_contract: bool
      prompt_within_budget: Callable[[str], bool]
      prompt_with_repair_within_budget: Callable[[str], bool]
  ```

- [ ] **Step 3: move the pure planner statics as functions**

  For each of `_intermediate_summary_text` (457-476), `_assemble_summary_input_text` (771-797), `_projection_text` (838-847), `_quoted_projection_value` (849-851), `_render_projection` (964-982): drop the `@staticmethod` decorator and the leading underscore, dedent one level, keep every parameter, annotation, docstring, and body token. For `_build_summary_input_text` (809-817) and `_projection_material` (853-962), drop `self`, rename to `build_summary_input_text`/`projection_material`, and replace `self._assemble_summary_input_text(` → `assemble_summary_input_text(`, `self._projection_text(` → `projection_text(`, `self._quoted_projection_value(` → `quoted_projection_value(`.

- [ ] **Step 4: move the budget-dependent planners as functions taking `budget`**

  ```python
  def reduction_prompt(budget: SnipPromptBudget, summaries: list[_SnipSummary], *, previous_summary: str | None) -> str | None:  # L478-490


  def plan_reduction_step(budget: SnipPromptBudget, summaries: list[_SnipSummary], *, previous_summary: str | None) -> _SnipReductionStep:  # L492-532


  def build_summary_prompt_from_formatted(budget: SnipPromptBudget, formatted_messages: str, *, previous_summary: str | None) -> str | None:  # L819-836


  def projection_prompt(budget: SnipPromptBudget, structure: str, fragments: list[_ProjectionFragment]) -> str | None:  # L984-995


  def fit_projection_prefix(budget: SnipPromptBudget, structure: str, fragments: list[_ProjectionFragment], field: _ProjectionField, start: int) -> int:  # L997-1022


  def build_projection_prompts(budget: SnipPromptBudget, messages: list[AnyMessage]) -> tuple[str, ...] | None:  # L1024-1082


  def build_snip_prompt_plan(budget: SnipPromptBudget, messages_to_summarize: list[AnyMessage], *, previous_summary: str | None) -> _SnipPromptPlan | None:  # L1084-1112


  def build_summary_prompt(budget: SnipPromptBudget, messages_to_summarize: list[AnyMessage], previous_summary: str | None = None) -> str | None:  # L1114-1130
  ```

  Apply exactly these token substitutions inside the moved bodies and nothing else: `self._prompt_within_budget(` → `budget.prompt_within_budget(`; `self._prompt_with_repair_within_budget(` → `budget.prompt_with_repair_within_budget(`; `self.summary_prompt` → `budget.summary_prompt`; `self._dual_output_contract` → `budget.dual_output_contract`; `self._intermediate_summary_text(` → `intermediate_summary_text(`; `self._build_summary_input_text(` → `build_summary_input_text(`; `self._render_projection(` → `render_projection(`; `self._projection_material(` → `projection_material(`; `self._reduction_prompt(` → `reduction_prompt(budget, `; `self._build_summary_prompt_from_formatted(` → `build_summary_prompt_from_formatted(budget, `; `self._projection_prompt(` → `projection_prompt(budget, `; `self._fit_projection_prefix(` → `fit_projection_prefix(budget, `; `self._build_projection_prompts(` → `build_projection_prompts(budget, `; `self._build_summary_prompt(` → `build_summary_prompt(budget, `. `logger.` calls stay as `logger.` (now the owner's logger; message text unchanged). Do not change any `SnipPromptBudgetTooSmall`/`SnipSourceTooLarge` raise, any `MAX_SNIP_HIERARCHICAL_LEAVES` comparison, or the `validate_summary_prompt_template` call. Do not move `_count_rendered_summary_prompt_tokens` (799-807); it stays in the middleware verbatim.

  Owner imports:

  ```python
  from __future__ import annotations

  import hashlib
  import html
  import json
  import logging
  from collections.abc import Callable, Mapping
  from dataclasses import dataclass
  from typing import Any

  from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage, get_buffer_string

  from deerflow.agents.context_compaction_warning import ContextCompactionFailureReason
  from deerflow.config.summarization_config import validate_summary_prompt_template
  ```

  Let Ruff report any import the moved bodies do not actually use and remove it.

- [ ] **Step 5: re-export, delegate, and keep the factory seam in the middleware**

  Delete the moved module-level definitions and the 15 moved methods from `DeerFlowSummarizationMiddleware`. Add one import block next to the other `deerflow.agents.middlewares` imports:

  ```python
  from deerflow.agents.middlewares.snip_planner import (  # noqa: F401 - compatibility exports
      MAX_SNIP_HIERARCHICAL_LEAVES,
      MAX_SNIP_HIERARCHICAL_MODEL_CALLS,
      MIN_SNIP_SUMMARY_OUTPUT_TOKENS,
      SnipCompactionFailed,
      SnipModelOutputInvalid,
      SnipPromptBudget,
      SnipPromptBudgetTooSmall,
      SnipSourceTooLarge,
      _ProjectionField,
      _ProjectionFragment,
      _SnipPromptPlan,
      _SnipReductionStep,
      _SnipSummary,
      _ensure_snip_summary_output_budget,
      build_snip_prompt_plan,
      build_summary_prompt,
      plan_reduction_step,
  )
  ```

  At the former `_intermediate_summary_text` position (L457) add the budget helper and the three wrappers with the exact legacy signatures:

  ```python
      def _snip_prompt_budget(self) -> SnipPromptBudget:
          return SnipPromptBudget(
              summary_prompt=self.summary_prompt,
              dual_output_contract=self._dual_output_contract,
              prompt_within_budget=self._prompt_within_budget,
              prompt_with_repair_within_budget=self._prompt_with_repair_within_budget,
          )

      def _plan_reduction_step(self, summaries: list[_SnipSummary], *, previous_summary: str | None) -> _SnipReductionStep:
          return plan_reduction_step(self._snip_prompt_budget(), summaries, previous_summary=previous_summary)

      def _build_snip_prompt_plan(self, messages_to_summarize: list[AnyMessage], *, previous_summary: str | None) -> _SnipPromptPlan | None:
          return build_snip_prompt_plan(self._snip_prompt_budget(), messages_to_summarize, previous_summary=previous_summary)

      def _build_summary_prompt(self, messages_to_summarize: list[AnyMessage], previous_summary: str | None = None) -> str | None:
          return build_summary_prompt(self._snip_prompt_budget(), messages_to_summarize, previous_summary=previous_summary)
  ```

  Line L2146 stays `model = _ensure_snip_summary_output_budget(model)` (bare name resolved from the middleware globals, which the compatibility import provides), so `test_memory_snip_compaction.py:2040,2081` patches keep working. Remove `html`, `json`, `get_buffer_string`, `AIMessage`, `SystemMessage`, `ToolMessage`, `validate_summary_prompt_template`, and `hashlib` from the middleware only if Ruff reports them unused (`hashlib`/`json`/`ToolMessage`/`AIMessage`/`SystemMessage` are still used by turn and receipt code until Tasks 7-8; `HumanMessage` remains used by `_prompt_within_budget`).

- [ ] **Step 6: run summarization gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_secondary_hotspots.py \
    tests/test_python_module_decomposition_contract.py \
    tests/test_memory_snip_compaction.py \
    tests/test_compaction_trigger_capacity_clamp.py \
    tests/test_context_compaction_receipt_basis.py \
    tests/test_agent_assembly_golden.py \
    tests/test_agents_md_constants.py \
    tests/test_subagent_context_compaction_evidence.py \
    tests/test_memory_seal_worker.py \
    tests/test_memory_dream_prepare_worker.py \
    tests/test_private_compact_api.py \
    tests/test_chat_control_compaction_outcomes.py \
    -q -m "not postgres and not provider_integration"
  ```

  Require the hierarchical plan, budget-too-small, source-too-large, repair-reinforcement, dual-output, and `_ensure_snip_summary_output_budget` seam tests in `test_memory_snip_compaction.py` to pass unchanged, and `test_package_dependency_direction_is_one_way` to still pass (it scans every Harness module for `app.*` imports).

- [ ] **Step 7: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check packages/harness/deerflow/agents/middlewares/summarization_middleware.py packages/harness/deerflow/agents/middlewares/snip_planner.py tests/test_python_module_decomposition_secondary_hotspots.py
  uvx ruff format --check packages/harness/deerflow/agents/middlewares/summarization_middleware.py packages/harness/deerflow/agents/middlewares/snip_planner.py tests/test_python_module_decomposition_secondary_hotspots.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 6 paths with message `refactor(summarization): extract snip planner`.

## Task 7: Extract turn compaction helpers

**Files:**

- Create: `backend/packages/harness/deerflow/agents/middlewares/turn_compaction.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py:88-89,245-251,662-675,1138-1284,1291-1309,1430-1432`
- Modify: `backend/tests/test_python_module_decomposition_secondary_hotspots.py`

**Interfaces:**

- Consumes: `Mapping`, `dataclass`, `AIMessage`, `AnyMessage`, `HumanMessage`, `SystemMessage`, `ToolMessage`, `read_human_input_response`, `is_dynamic_context_reminder`, `SUMMARY_MESSAGE_NAME`, `is_real_user_message`.
- Produces: `_SUMMARY_TRIGGER_MESSAGE_NAME`, `_ASK_CLARIFICATION_TOOL_NAME`, `_PreparedCompaction`, `is_turn_user`, `turn_prefix_start`, `clarification_request_tool_call_id`, `is_clarification_continuation`, `complete_turn_ranges`, `candidate_cutoffs`, `snip_messages`, `summary_count_message`, `messages_for_trigger_count`, `context_progress`.

- [ ] **Step 1: add the failing owner identity test**

  ```python
  TURN_COMPACTION_FUNCTIONS = (
      "is_turn_user",
      "turn_prefix_start",
      "clarification_request_tool_call_id",
      "is_clarification_continuation",
      "complete_turn_ranges",
      "candidate_cutoffs",
      "snip_messages",
      "summary_count_message",
      "messages_for_trigger_count",
      "context_progress",
  )


  def test_turn_compaction_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.agents.middlewares.turn_compaction")
      for name in TURN_COMPACTION_NAMES:
          assert getattr(summarization_legacy, name) is getattr(owner, name), name
      for name in TURN_COMPACTION_FUNCTIONS:
          assert callable(getattr(owner, name)), name
      middleware = summarization_legacy.DeerFlowSummarizationMiddleware
      for legacy_name, (module_name, owner_name) in SUMMARIZATION_STATICMETHOD_ALIASES.items():
          assert module_name == "turn_compaction"
          descriptor = middleware.__dict__[legacy_name]
          assert isinstance(descriptor, staticmethod), legacy_name
          assert descriptor.__func__ is getattr(owner, owner_name), legacy_name
      class_names = _class_defined_names(SUMMARIZATION_PATH, "DeerFlowSummarizationMiddleware")
      assert not {"_is_turn_user", "_turn_prefix_start", "_clarification_request_tool_call_id", "_is_clarification_continuation"} & class_names
      assert middleware._complete_turn_ranges([]) == ()
      _assert_owner_imports_are_clean(MIDDLEWARES_ROOT / "turn_compaction.py", forbidden_modules=(SUMMARIZATION_MODULE, ".summarization_middleware", ".snip_planner", ".compaction_receipts"), forbidden_prefixes=("app", "sqlalchemy"))
  ```

  Run only this node. Expected: RED with `ModuleNotFoundError: No module named 'deerflow.agents.middlewares.turn_compaction'`.

- [ ] **Step 2: create the owner**

  Docstring `"""Complete-turn boundaries, clarification continuation, cutoff candidates, and trigger-count helpers."""`. Move verbatim: `_SUMMARY_TRIGGER_MESSAGE_NAME`, `_ASK_CLARIFICATION_TOOL_NAME` (88-89); `_PreparedCompaction` (245-251 with decorator). Move as module functions, dropping decorators, `self`/`cls`, and the leading underscore, dedenting one level, keeping every parameter, annotation, default, docstring, and body token:

  ```python
  def summary_count_message(summary_text: str) -> HumanMessage:  # L662-664
  def messages_for_trigger_count(messages: list[AnyMessage], summary_text: str | None) -> list[AnyMessage]:  # L666-669; self._summary_count_message( -> summary_count_message(
  def context_progress(current: int | float, threshold: int | float) -> float:  # L671-675
  def is_turn_user(message: AnyMessage) -> bool:  # L1138-1150
  def turn_prefix_start(messages: list[AnyMessage], user_index: int) -> int:  # L1152-1165; cls._is_turn_user( -> is_turn_user(
  def clarification_request_tool_call_id(message: AnyMessage, request_id: str) -> str | None:  # L1167-1193
  def is_clarification_continuation(messages: list[AnyMessage], *, turn_start: int, response_index: int) -> bool:  # L1195-1229; cls._clarification_request_tool_call_id( -> clarification_request_tool_call_id(
  def complete_turn_ranges(messages: list[AnyMessage]) -> tuple[tuple[int, int], ...]:  # L1231-1284; cls._is_turn_user( -> is_turn_user(; cls._is_clarification_continuation( -> is_clarification_continuation(; cls._turn_prefix_start( -> turn_prefix_start(
  def candidate_cutoffs(messages: list[AnyMessage], requested_cutoff: int, *, protect_latest_complete_turn: bool = False) -> tuple[int, ...]:  # L1291-1309; self._complete_turn_ranges( -> complete_turn_ranges(
  def snip_messages(messages: list[AnyMessage]) -> list[AnyMessage]:  # L1430-1432
  ```

  Owner imports:

  ```python
  from __future__ import annotations

  from collections.abc import Mapping
  from dataclasses import dataclass

  from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

  from deerflow.agents.human_input import read_human_input_response
  from deerflow.agents.middlewares.dynamic_context_middleware import is_dynamic_context_reminder
  from deerflow.utils.messages import SUMMARY_MESSAGE_NAME, is_real_user_message
  ```

- [ ] **Step 3: alias from the middleware**

  Delete the moved constants, `_PreparedCompaction`, and the ten methods. Add the import block:

  ```python
  from deerflow.agents.middlewares.turn_compaction import (  # noqa: F401 - compatibility exports
      _ASK_CLARIFICATION_TOOL_NAME,
      _SUMMARY_TRIGGER_MESSAGE_NAME,
      _PreparedCompaction,
      candidate_cutoffs,
      complete_turn_ranges,
      context_progress,
      messages_for_trigger_count,
      snip_messages,
      summary_count_message,
  )
  ```

  At the former `_summary_count_message` position (L662) add:

  ```python
      _summary_count_message = staticmethod(summary_count_message)
      _messages_for_trigger_count = staticmethod(messages_for_trigger_count)
      _context_progress = staticmethod(context_progress)
      _complete_turn_ranges = staticmethod(complete_turn_ranges)
      _candidate_cutoffs = staticmethod(candidate_cutoffs)
      _snip_messages = staticmethod(snip_messages)
  ```

  Every staying caller (`measure_context_usage`, `_measure_trigger_usage`, `_provider_safe_retention_cutoff`, `_overbudget_progress_cutoff`, `_prepare_compaction`, `_acontext_compaction_update` until Task 8) keeps its `self._<name>(...)` call unchanged; `deerflow/runtime/context_compaction.py:86` keeps `DeerFlowSummarizationMiddleware._complete_turn_ranges(messages)` unchanged. Remove `read_human_input_response`, `is_dynamic_context_reminder`, `SUMMARY_MESSAGE_NAME`, `is_real_user_message`, `AIMessage`, `SystemMessage`, `ToolMessage` from the middleware only if Ruff reports them unused.

- [ ] **Step 4: run summarization gates**

  Run the Task 6 Step 6 command. Require the seven `_complete_turn_ranges` tests (`test_memory_snip_compaction.py:482-600`), the clarification-continuation tests, and the `context_compaction` wrapper tests (`test_memory_seal_worker.py`, `test_private_compact_api.py`) to pass unchanged.

- [ ] **Step 5: static checkpoint and conditional commit**

  Run the Task 6 Step 7 commands adding `packages/harness/deerflow/agents/middlewares/turn_compaction.py`. If authorized, commit only Task 7 paths with message `refactor(summarization): extract turn compaction`.

## Task 8: Extract compaction receipts and context updates

**Files:**

- Create: `backend/packages/harness/deerflow/agents/middlewares/compaction_receipts.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py:169-177,254-263,1583-1636,1785-2035`
- Modify: `backend/tests/test_python_module_decomposition_secondary_hotspots.py`

**Interfaces:**

- Consumes: `hashlib`, `json`, `Mapping`, `Callable`, `dataclass`, `cast`, `AgentState`, `AnyMessage`, `message_to_dict`, `get_config`, `Runtime`, `ValidationError`, `ContextCompactionFailureReason`, `MEMORY_ARCHIVE_CONTEXT_KEY`, `MemoryArchiveReceipt`, `SnipArchiveContext`, `build_memory_archive_receipt`, `contains_visual_material`, `CONTEXT_COMPACTION_RECEIPT_STATE_KEY`, `CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY`, `PROVIDER_REQUEST_PROFILE_STATE_KEY`, `ContextCheckpointEstimator`, `ContextCheckpointProjectionSnapshot`, `ContextCompactionCheckpointReceipt`, `VisualTokenCostContractError`; from `snip_planner`: `SnipCompactionFailed`; from `turn_compaction`: `_PreparedCompaction`, `messages_for_trigger_count`, `summary_count_message`.
- Produces: `ContextCompactionResult`, `_resolve_thread_id`, `read_archive_context`, `resolve_source_checkpoint_id`, `build_compaction_receipt`, `resolve_compaction_estimator`, `require_receipt_preconditions`, `context_compaction_update`, `acontext_compaction_update`, `context_state_digest`.

> **Execution amendment (2026-09-03):** the former `_archive_context` and `_source_checkpoint_id` bodies bind locals named `archive_context` and `source_checkpoint_id` (`archive_context = ...(runtime)` in `_receipt`; `source_checkpoint_id = ...(...)` in `_context_compaction_update`), so owner functions with the plan's original names would raise `UnboundLocalError`. The owners are named `read_archive_context(runtime)` and `resolve_source_checkpoint_id(runtime, archive_context)`; the substitution table below reads `self._archive_context(` → `read_archive_context(` and `self._source_checkpoint_id(` → `resolve_source_checkpoint_id(`; locals are never renamed. The `self._context_compaction_observer` → `observer` substitution also yields a no-op `observer = observer` statement in three functions, which is deleted.

- [ ] **Step 1: add the failing owner identity test**

  ```python
  COMPACTION_RECEIPT_FUNCTIONS = (
      "read_archive_context",
      "resolve_source_checkpoint_id",
      "build_compaction_receipt",
      "resolve_compaction_estimator",
      "require_receipt_preconditions",
      "context_compaction_update",
      "acontext_compaction_update",
      "context_state_digest",
  )


  def test_compaction_receipts_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.agents.middlewares.compaction_receipts")
      for name in COMPACTION_RECEIPT_NAMES:
          assert getattr(summarization_legacy, name) is getattr(owner, name), name
      for name in COMPACTION_RECEIPT_FUNCTIONS:
          assert callable(getattr(owner, name)), name
      assert tuple(inspect.signature(owner.require_receipt_preconditions).parameters) == ("observer", "state", "runtime", "asynchronous")
      assert tuple(inspect.signature(owner.context_compaction_update).parameters) == ("observer", "state", "result", "runtime")
      assert tuple(inspect.signature(owner.acontext_compaction_update).parameters) == ("observer", "token_counter", "state", "result", "runtime")
      class_names = _class_defined_names(SUMMARIZATION_PATH, "DeerFlowSummarizationMiddleware")
      assert not set(SUMMARIZATION_MOVED_INTERNALS) & class_names, set(SUMMARIZATION_MOVED_INTERNALS) & class_names
      for name in ("_receipt", "_require_receipt_preconditions", "_context_compaction_update", "_acontext_compaction_update"):
          assert name in class_names, name
      owner_imports = _module_imports(MIDDLEWARES_ROOT / "compaction_receipts.py")
      assert {"deerflow.agents.middlewares.snip_planner", "deerflow.agents.middlewares.turn_compaction"} <= owner_imports or {".snip_planner", ".turn_compaction"} <= owner_imports
      _assert_owner_imports_are_clean(MIDDLEWARES_ROOT / "compaction_receipts.py", forbidden_modules=(SUMMARIZATION_MODULE, ".summarization_middleware"), forbidden_prefixes=("app", "sqlalchemy"))
  ```

  Run only this node. Expected: RED with `ModuleNotFoundError: No module named 'deerflow.agents.middlewares.compaction_receipts'`.

- [ ] **Step 2: create the owner**

  Docstring `"""Compaction receipts, Context Evidence preconditions, and compaction state updates."""`. Move verbatim: `ContextCompactionResult` (169-177 with decorator), `_resolve_thread_id` (254-263). Move as module functions, dropping decorators and `self`, dedenting one level, keeping every parameter, annotation, default, docstring, `logger`-free body token (none of these methods logs):

  ```python
  def archive_context(runtime: Runtime) -> SnipArchiveContext | None:  # L1583-1589
  def source_checkpoint_id(runtime: Runtime, archive_context: SnipArchiveContext | None) -> str | None:  # L1591-1612
  def build_compaction_receipt(prepared: _PreparedCompaction, tagged_text: str, runtime: Runtime) -> MemoryArchiveReceipt | None:  # L1614-1636; self._archive_context( -> archive_context(; self._source_checkpoint_id( -> source_checkpoint_id(
  def resolve_compaction_estimator(state: AgentState) -> tuple[Mapping[str, object] | None, ContextCheckpointEstimator]:  # L1785-1824
  def require_receipt_preconditions(observer: object | None, state: AgentState, runtime: Runtime, *, asynchronous: bool) -> None:  # L1826-1887; self._context_compaction_observer -> observer; self._archive_context( -> archive_context(; self._resolve_compaction_estimator( -> resolve_compaction_estimator(; self._source_checkpoint_id( -> source_checkpoint_id(
  def context_compaction_update(observer: object | None, state: AgentState, result: ContextCompactionResult, runtime: Runtime) -> dict[str, object]:  # L1889-1966; same substitutions plus self._context_state_digest( -> context_state_digest(
  async def acontext_compaction_update(observer: object | None, token_counter: Callable[..., int], state: AgentState, result: ContextCompactionResult, runtime: Runtime) -> dict[str, object]:  # L1968-2018; self._context_compaction_observer -> observer; self._context_compaction_update( -> context_compaction_update(observer, ; self._context_state_digest( -> context_state_digest(; self._messages_for_trigger_count( -> messages_for_trigger_count(; self._summary_count_message( -> summary_count_message(; self.token_counter( -> token_counter(
  def context_state_digest(messages: list[AnyMessage], summary: str | None) -> str:  # L2020-2035
  ```

  Inside `source_checkpoint_id`, the parameter named `archive_context` shadows the module function of the same name only within that body; the body never calls the function, so keep the parameter name unchanged. Every `SnipCompactionFailed(...)` raise keeps its `ContextCompactionFailureReason` and text. Owner imports:

  ```python
  from __future__ import annotations

  import hashlib
  import json
  from collections.abc import Callable, Mapping
  from dataclasses import dataclass
  from typing import cast

  from langchain.agents import AgentState
  from langchain_core.messages import AnyMessage, message_to_dict
  from langgraph.config import get_config
  from langgraph.runtime import Runtime
  from pydantic import ValidationError

  from deerflow.agents.context_compaction_warning import ContextCompactionFailureReason
  from deerflow.agents.memory.snip import MEMORY_ARCHIVE_CONTEXT_KEY, MemoryArchiveReceipt, SnipArchiveContext, build_memory_archive_receipt
  from deerflow.agents.middlewares.provider_request_profile import contains_visual_material
  from deerflow.agents.middlewares.snip_planner import SnipCompactionFailed
  from deerflow.agents.middlewares.turn_compaction import _PreparedCompaction, messages_for_trigger_count, summary_count_message
  from deerflow.agents.provider_request_contract import CONTEXT_COMPACTION_RECEIPT_STATE_KEY, CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY, PROVIDER_REQUEST_PROFILE_STATE_KEY
  from deerflow.runtime.context_evidence import ContextCheckpointEstimator, ContextCheckpointProjectionSnapshot, ContextCompactionCheckpointReceipt, VisualTokenCostContractError
  ```

  Let Ruff report any import the moved bodies do not use and remove it.

- [ ] **Step 3: re-export and delegate from the middleware**

  Delete `ContextCompactionResult`, `_resolve_thread_id`, and the nine methods. Add the import block:

  ```python
  from deerflow.agents.middlewares.compaction_receipts import (  # noqa: F401 - compatibility exports
      ContextCompactionResult,
      _resolve_thread_id,
      acontext_compaction_update,
      build_compaction_receipt,
      context_compaction_update,
      require_receipt_preconditions,
  )
  ```

  At the former `_archive_context` position (L1583) add the wrappers with the exact legacy signatures; they read the observer at call time:

  ```python
      def _receipt(self, prepared: _PreparedCompaction, tagged_text: str, runtime: Runtime) -> MemoryArchiveReceipt | None:
          return build_compaction_receipt(prepared, tagged_text, runtime)

      def _require_receipt_preconditions(self, state: AgentState, runtime: Runtime, *, asynchronous: bool) -> None:
          require_receipt_preconditions(self._context_compaction_observer, state, runtime, asynchronous=asynchronous)

      def _context_compaction_update(self, state: AgentState, result: ContextCompactionResult, runtime: Runtime) -> dict[str, object]:
          return context_compaction_update(self._context_compaction_observer, state, result, runtime)

      async def _acontext_compaction_update(self, state: AgentState, result: ContextCompactionResult, runtime: Runtime) -> dict[str, object]:
          return await acontext_compaction_update(self._context_compaction_observer, self.token_counter, state, result, runtime)
  ```

  `compact_state`/`acompact_state` keep calling `self._receipt(...)` and `self._require_receipt_preconditions(...)`; `_maybe_summarize`/`_amaybe_summarize` keep calling `self._context_compaction_update(...)`/`self._acontext_compaction_update(...)`. Remove `message_to_dict`, `ValidationError`, `MEMORY_ARCHIVE_CONTEXT_KEY`, `SnipArchiveContext`, `build_memory_archive_receipt`, `contains_visual_material`, `CONTEXT_COMPACTION_RECEIPT_STATE_KEY`, `CONTEXT_PROJECTION_SNAPSHOT_STATE_KEY`, `ContextCheckpointEstimator`, `ContextCheckpointProjectionSnapshot`, `ContextCompactionCheckpointReceipt`, `VisualTokenCostContractError`, `hashlib`, `json`, and `cast` from the middleware only if Ruff reports them unused (`cast` is still used by `_server_abort_event` and `_provider_safe_retention_cutoff`; `MemoryArchiveReceipt` remains an annotation in `_receipt`).

- [ ] **Step 4: run summarization gates**

  Run the Task 6 Step 6 command plus:

  ```bash
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run --env-file ../.env python \
    tests/support/core_gate_plugin.py \
    tests/test_memory_archive_receipt_postgres.py \
    -q -m "not provider_integration"
  ```

  Require `test_subagent_context_compaction_evidence.py` (assigns `_context_compaction_observer` after construction, then calls `_acontext_compaction_update`), the 20 receipt-basis tests, and the PostgreSQL archive receipt tests to pass unchanged.

- [ ] **Step 5: static checkpoint and conditional commit**

  Run the Task 6 Step 7 commands adding `packages/harness/deerflow/agents/middlewares/turn_compaction.py` and `packages/harness/deerflow/agents/middlewares/compaction_receipts.py`. If authorized, commit only Task 8 paths with message `refactor(summarization): extract compaction receipts`.

## Task 9: Document ownership and run complete Batch 6 verification

**Files:**

- Modify: `backend/AGENTS.md:84` (add one bullet under `## Where changes live`, before the Builder `*_contracts.py` bullet)
- Modify: `backend/tests/test_python_module_decomposition_secondary_hotspots.py`
- Verify: every production and test path changed in Tasks 1-8
- Preserve: the Batch 5 polish edits (if still uncommitted), `backend/tests/test_skill_builder_durable_agent_postgres.py`, and the pre-existing untracked decomposition documents

**Interfaces:**

- Consumes: the seven owners, four legacy modules, migrated tests, and current compatibility/behavior tests.
- Produces: documented maintenance ownership plus current focused, PostgreSQL, import-order, static, and full-backend evidence.

- [ ] **Step 1: document the stable owner boundaries**

  Add this bullet to `backend/AGENTS.md` under `## Where changes live`, after the Harness Worker execution bullet and formatted to the guide width:

  > Secondary hotspot ownership: `packages/harness/deerflow/persistence/jobs/contracts.py` owns Job scopes, requests, claims, terminal events, ports, errors, and `retry_backoff_seconds`, while `jobs/sql.py` keeps `JobRepository`, lease hashing, the requeue-event registry, and `__all__`; `app/private_work/snapshot_contracts.py` owns `RunSnapshotAssetStale`, secret DTOs, and admission ports, `snapshot_admission_rules.py` owns the admission encoder budget, secret-key rejection, recursion clamp, closure validators, and `plan_run_asset_rows()`, and `snapshot_repository.py` keeps the atomic admission transaction with every `await` in place; `app/private_work/checkpoint_receipt_repair.py` owns checkpoint config readers and Memory/Context/Provider receipt repair while `checkpointer.py` keeps every write, lease, read-repair, and Approval-deletion transaction and calls the repair functions on the same session; `packages/harness/deerflow/agents/middlewares/snip_planner.py` owns snip exceptions, constants, and `SnipPromptBudget`-parameterised prompt planning, `turn_compaction.py` owns complete-turn boundaries and trigger-count helpers, `compaction_receipts.py` owns receipts, estimator preconditions, and compaction state updates, and `summarization_middleware.py` keeps the LangChain hooks, model invocation, `_prepare_compaction`, retention predicates, factories, and re-exports every legacy name. Tests patch `_ensure_snip_summary_output_budget` on the middleware, `encode_run_asset_snapshot` on `snapshot_admission_rules`, and `ContextEvidenceRepository`/`ContextProjectionTransaction` on `checkpoint_receipt_repair`.

  Update no README, frontend documentation, or feature changelog because runtime architecture, process responsibility, and public contracts are unchanged.

- [ ] **Step 2: add the final existence and façade-shape gate**

  ```python
  def test_batch6_all_owner_modules_exist_and_facades_keep_their_owners() -> None:
      for name in JOBS_OWNER_MODULES:
          assert (JOBS_ROOT / f"{name}.py").is_file(), name
      for name in SNAPSHOT_OWNER_MODULES + CHECKPOINTER_OWNER_MODULES:
          assert (PRIVATE_WORK_ROOT / f"{name}.py").is_file(), name
      for name in SUMMARIZATION_OWNER_MODULES:
          assert (MIDDLEWARES_ROOT / f"{name}.py").is_file(), name
      sql_names = {node.name for node in _parse(JOBS_SQL_PATH).body if isinstance(node, ast.ClassDef)}
      assert sql_names == {"_DeadTerminalReconciliationCursor", "JobRepository"}
      snapshot_classes = {node.name for node in _parse(SNAPSHOT_PATH).body if isinstance(node, ast.ClassDef)}
      assert snapshot_classes == {"RunSnapshotRepository"}
      checkpointer_classes = {node.name for node in _parse(CHECKPOINTER_PATH).body if isinstance(node, ast.ClassDef)}
      assert checkpointer_classes == {
          "PrivateCheckpointQuotaPort",
          "PrivateCheckpointAuditPort",
          "_NoopPrivateCheckpointQuota",
          "ProjectScopedCheckpointer",
          "_ScopedCheckpointSaver",
          "_AlreadyAuthorizedCheckpointSaver",
      }
      summarization_classes = {node.name for node in _parse(SUMMARIZATION_PATH).body if isinstance(node, ast.ClassDef)}
      assert summarization_classes == {"ContextTriggerUsage", "ContextUsageMeasurement", "DeerFlowSummarizationMiddleware"}
  ```

- [ ] **Step 3: run the combined focused suite once**

  Run the Task 1 Step 6 combined command. Record exact count, duration, deselections, skips, and warning categories. Require zero failures; the count must equal the Task 1 result plus the contract tests added in Tasks 2-9 (`543` baseline behaviour tests are unchanged).

- [ ] **Step 4: run the selected PostgreSQL authority gate**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run --env-file ../.env python \
    tests/support/core_gate_plugin.py \
    tests/test_job_owner_lifecycle_postgres.py \
    tests/test_checkpoint_lease_atomicity_postgres.py \
    tests/test_run_skill_version_refs_postgres.py \
    tests/test_mcp_secret_lifecycle_postgres.py \
    tests/test_thread_checkpoint_delete_recovery_postgres.py \
    tests/test_memory_archive_receipt_postgres.py \
    tests/test_execution_approval_private_lifecycle_postgres.py \
    -q -m "not provider_integration"
  ```

  Require `failed=0 skipped=0` against disposable `deerflow_test_*` databases. These files drive Job enqueue/claim/heartbeat/requeue, checkpoint writes under exact leases with repair-on-read, Run admission row order and schema versions, MCP secret closures, thread deletion/tombstone/compensated-create cleanup, archive receipts, and the Approval private lifecycle end to end.

- [ ] **Step 5: run fresh import-order and exact-object smoke without any process start**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run python -c '
  from deerflow.persistence.jobs import contracts, sql
  import deerflow.persistence.jobs as jobs
  from app.private_work import snapshot_contracts, snapshot_admission_rules, snapshot_repository
  from app.private_work import checkpoint_receipt_repair, checkpointer
  from deerflow.agents.middlewares import snip_planner, turn_compaction, compaction_receipts, summarization_middleware as sm
  import deerflow.runtime.context_compaction as cc
  assert jobs.EnqueueJob is sql.EnqueueJob is contracts.EnqueueJob
  assert sql.DeadJobRequeuedEvent is contracts.DeadJobRequeuedEvent
  assert sql.__all__[-2:] == ["consume_issued_dead_job_requeued_event", "retry_backoff_seconds"]
  assert snapshot_repository.RunSnapshotAssetStale is snapshot_contracts.RunSnapshotAssetStale is snapshot_admission_rules.RunSnapshotAssetStale
  assert snapshot_repository.RunSnapshotRepository.__dict__["_asset_allowed"].__func__ is snapshot_admission_rules.asset_allowed
  assert snapshot_repository.plan_run_asset_rows is snapshot_admission_rules.plan_run_asset_rows
  assert checkpointer._ScopedCheckpointSaver.__dict__["_thread_id"].__func__ is checkpoint_receipt_repair.thread_id_from_config
  assert sm.SnipCompactionFailed is snip_planner.SnipCompactionFailed is compaction_receipts.SnipCompactionFailed
  assert sm.ContextCompactionResult is compaction_receipts.ContextCompactionResult
  assert sm._PreparedCompaction is turn_compaction._PreparedCompaction
  assert sm.DeerFlowSummarizationMiddleware.__dict__["_complete_turn_ranges"].__func__ is turn_compaction.complete_turn_ranges
  assert sm._ensure_snip_summary_output_budget is snip_planner._ensure_snip_summary_output_budget
  assert cc.SnipCompactionFailed is sm.SnipCompactionFailed
  assert not hasattr(sm, "__all__") and not hasattr(snapshot_repository, "__all__") and not hasattr(checkpointer, "__all__")
  '
  PYTHONPATH=. uv run python -c '
  import deerflow.runtime.context_compaction as cc
  import app.reliability.execution as legacy
  from deerflow.agents.middlewares import summarization_middleware as sm
  from app.private_work import snapshot_repository, checkpointer
  from deerflow.persistence.jobs import sql
  assert cc.DeerFlowSummarizationMiddleware is sm.DeerFlowSummarizationMiddleware
  assert sql.JobClaim.__module__ == "deerflow.persistence.jobs.contracts"
  assert snapshot_repository.agent_model_snapshot_purpose.__module__ == "app.private_work.snapshot_contracts"
  assert checkpointer.PRIVATE_SCOPE_MARKER == "deerflow_private_scope"
  '
  ```

  Both commands must exit 0 without starting a Gateway, Worker, Scheduler, Sandbox, or database connection.

- [ ] **Step 6: run repository-required format, lint, blocking-I/O, and full backend gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  make format
  make lint
  make detect-blocking-io
  uv run --env-file ../.env make test
  ```

  Inspect `git status --short` immediately after `make format`. If formatting touches a file outside the Batch 6 set, the Batch 5 polish set, or the already-preserved P1 test, stop and separate the drift instead of including it. Require every command to exit 0, `detect-blocking-io` to report zero findings in the seven new owner modules and the four legacy modules, and the core suite to report `failed=0 skipped=0` (baseline `collected=6566 passed=6566`; the new count is that plus the Batch 6 contract tests). Do not use a production database or an unapproved external Provider/Sandbox.

- [ ] **Step 7: perform final structural and scope review**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations
  git diff --check
  git status --short
  git diff --stat 7dcb05c9a5a2d777cba69a820261a7374522874f
  ! rg -n '^[[:space:]]*(from|import)[[:space:]]+(deerflow[.]persistence[.]jobs[.]sql|[.]sql)\b' \
    backend/packages/harness/deerflow/persistence/jobs/contracts.py
  ! rg -n '^[[:space:]]*(from|import)[[:space:]]+app([[:space:].]|$)' \
    backend/packages/harness/deerflow/persistence/jobs/contracts.py \
    backend/packages/harness/deerflow/agents/middlewares/snip_planner.py \
    backend/packages/harness/deerflow/agents/middlewares/turn_compaction.py \
    backend/packages/harness/deerflow/agents/middlewares/compaction_receipts.py
  ! rg -n '^[[:space:]]*(from|import)[[:space:]]+(app[.]private_work[.](snapshot_repository|checkpointer)|[.](snapshot_repository|checkpointer))\b' \
    backend/app/private_work/snapshot_contracts.py \
    backend/app/private_work/snapshot_admission_rules.py \
    backend/app/private_work/checkpoint_receipt_repair.py
  ! rg -n '^[[:space:]]*(from|import)[[:space:]]+(deerflow[.]agents[.]middlewares[.]summarization_middleware|[.]summarization_middleware)\b' \
    backend/packages/harness/deerflow/agents/middlewares/snip_planner.py \
    backend/packages/harness/deerflow/agents/middlewares/turn_compaction.py \
    backend/packages/harness/deerflow/agents/middlewares/compaction_receipts.py
  ! rg -n 'session[.](begin|commit|flush)|with_for_update|postgres_checkpoint_transaction' \
    backend/app/private_work/snapshot_contracts.py \
    backend/app/private_work/snapshot_admission_rules.py \
    backend/app/private_work/checkpoint_receipt_repair.py \
    backend/packages/harness/deerflow/persistence/jobs/contracts.py
  wc -l backend/packages/harness/deerflow/persistence/jobs/sql.py backend/packages/harness/deerflow/persistence/jobs/contracts.py \
    backend/app/private_work/snapshot_repository.py backend/app/private_work/snapshot_contracts.py backend/app/private_work/snapshot_admission_rules.py \
    backend/app/private_work/checkpointer.py backend/app/private_work/checkpoint_receipt_repair.py \
    backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py \
    backend/packages/harness/deerflow/agents/middlewares/snip_planner.py \
    backend/packages/harness/deerflow/agents/middlewares/turn_compaction.py \
    backend/packages/harness/deerflow/agents/middlewares/compaction_receipts.py
  ```

  The five `rg` scans must return no matches. Review every changed/untracked path individually and attribute each to Batch 6, the Batch 5 polish, the P1 test, or a pre-existing user document. Confirm no Schema/DDL, frontend, `jobs/{__init__,model}.py`, `run_snapshot_codec.py`, `context_compaction.py`, `assembly.py`, `lead_agent/agent.py`, `run_admission.py`, `thread_service.py`, or unrelated test refactor changed. Line counts are reported as evidence, not as an acceptance gate.

- [ ] **Step 8: request one final independent review**

  Use `superpowers:requesting-code-review` over the exact Batch 6 path set. If local commits were authorized, review the Batch 6 commit range; otherwise review all tracked and untracked Batch 6 files listed by `git status --short` while excluding the Batch 5 polish, the preserved P1 test, and pre-existing documents from the implementation verdict.

  Require the reviewer to inspect:

  - byte-identity of every moved definition against `7dcb05c9` (AST-compare the four legacy modules' top-level and class-level definitions with the seven owners), and that the only rewritten bodies are the parameterised planner/receipt functions and the delegating wrappers;
  - `jobs/contracts.py`: single `DeadJobRequeuedEvent` class object, verbatim `__post_init__` error texts, `sql.__all__` unchanged, `RetentionPurgeJobAuthority` `datetime` note recorded;
  - `snapshot_repository.py`: the 19-`await` order, `session.add_all` positions, cohort source text, `_asset_allowed` class-qualified calls, `except RunSnapshotAssetStale` identity, `plan_run_asset_rows` inputs and row order;
  - `checkpointer.py`: repair call sites unchanged inside `_locked_active`/caller sessions, `_aput_execution_atomic` three-transaction shape, `getattr(self, "_context_evidence_observer", None)` wrapper, `PrivateWorkNotFound("unknown")` texts, `object.__new__` fixture compatibility;
  - `summarization_middleware.py`: frozen `__init__`, `state_schema`, hook order, `_prepare_compaction` unchanged, `SnipPromptBudget` capturing bound budget predicates, bare-name `_ensure_snip_summary_output_budget` call at the factory, staticmethod aliases used by `context_compaction.py:86`, observer read at call time in the four receipt wrappers;
  - migrated monkeypatch targets (`snapshot_admission_rules.encode_run_asset_snapshot`, `checkpoint_receipt_repair.ContextEvidenceRepository`/`ContextProjectionTransaction`) and unchanged seams;
  - import direction of all seven owners; absence of sessions/commits/locks in owners; logger-name changes recorded;
  - current validation evidence and scope exclusions.

  Resolve every Critical or Important finding, assess every Minor finding, and rerun the smallest affected focused group plus final structural gates before handoff.

- [ ] **Step 9: documentation checkpoint**

  If explicit local commits are authorized, commit only `backend/AGENTS.md` and the final contract-test additions with message `docs(hotspots): document secondary hotspot owners`. Otherwise leave them unstaged and report their exact diff.

## Completion Criteria

- `deerflow.persistence.jobs.sql` keeps `JobRepository` as its only public class, the verbatim 23-name `__all__`, the requeue-event registry and cursors, `_is_durable_terminal_successor`, `_lease_token_hash`, and `consume_issued_dead_job_requeued_event`; every `JOBS_CONTRACT_NAMES` entry is the exact `contracts.py` object; `deerflow.persistence.jobs` exports are unchanged.
- `app.private_work.snapshot_repository` keeps `RunSnapshotRepository` as its only class, the frozen `__init__` parameters, `create_run_with_snapshot()` as the only transaction owner, and `create_run_with_snapshot_in_session()` with the frozen 19-`await` order, the inline cohort assertion, and `plan_run_asset_rows()` between await #15 and `session.add_all`; `snapshot_contracts.py` and `snapshot_admission_rules.py` never import the repository; the four validators are `staticmethod` aliases of owner functions.
- `app.private_work.checkpointer` keeps `ProjectScopedCheckpointer`, `_ScopedCheckpointSaver`, and `_AlreadyAuthorizedCheckpointSaver` with every transaction boundary, lock, lease validation, Approval deletion step, and sync bridge; the seven repair methods delegate to `checkpoint_receipt_repair` on the same session at the same call sites; `_thread_id`/`_checkpoint_id` are `staticmethod` aliases; `PRIVATE_SCOPE_MARKER` is unchanged.
- `deerflow.agents.middlewares.summarization_middleware` keeps `DeerFlowSummarizationMiddleware` with the frozen `__init__`, `state_schema`, every method in `SUMMARIZATION_RETAINED_METHODS`, the seven delegating wrappers, the six `staticmethod` aliases, `_snip_prompt_budget()`, and the two factories; none of `SUMMARIZATION_MOVED_INTERNALS` remains on the class; every legacy name is the exact owner object; `snip_planner`/`turn_compaction` are leaves and `compaction_receipts` imports only them.
- Every moved body is byte-identical apart from the documented `self`/`cls` → parameter substitutions and dedent; every error text, log text, dataclass field order, and Protocol method set is unchanged.
- Tests move only with corresponding owners; no existing large test file or test framework is reorganized; `test_memory_contract.py` scans the two new snapshot owners.
- Focused, selected PostgreSQL, import-order, dependency, Ruff, blocking-I/O, and full backend zero-skip gates have current passing evidence, and the report states that focused/offline tests do not certify a live Provider, external Sandbox, or target deployment.
- The deferred items and the two recorded semantic notes (`RetentionPurgeJobAuthority` under a `job_sql.datetime` patch; planner logger names) appear in the handoff report.
- Batch 5 polish edits, the P1 test change, and earlier untracked documents remain separately attributable and are not staged or committed as Batch 6 work without explicit authorization.

## Execution Handoff

Plan approval does not start implementation. After approval, choose one:

1. **Subagent-Driven (recommended):** execute Tasks 1-9 sequentially in this existing worktree, with a fresh implementation agent and independent specification/code-quality review per task.
2. **Inline Execution:** execute the same tasks with `superpowers:executing-plans` and the same checkpoints.

Do not create another branch or worktree unless the user explicitly changes the existing-worktree requirement.
