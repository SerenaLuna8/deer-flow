# Process Boundary Module Decomposition (Batch 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate Skill Builder, Execution Approval, and Agent Builder generation responsibilities into explicit Gateway- and Worker-owned modules without changing authorization, transactions, lifecycle outcomes, public contracts, or process responsibilities.

**Architecture:** Extract the Run-bound Skill Builder draft sink before moving the remaining Gateway lifecycle into its owner and retaining the old service module as a compatibility façade. Split Execution Approval in dependency order—policy, codec, recovery, Worker port, Gateway service—while preserving the existing transactional lifecycle owner. Isolate Agent Builder model-turn execution in one concrete collaborator composed by `AgentDesignService`; Gateway remains the only Agent Builder process and Worker gains no Agent Design dependency.

**Tech Stack:** Python 3.12+, SQLAlchemy async APIs, PostgreSQL, FastAPI, Pydantic 2, pytest 9, pytest-asyncio, Ruff, the existing Skill Builder and host-execution Harness contracts, and repository Make targets.

**Spec:** `docs/superpowers/specs/2026-09-02-python-module-decomposition-design.md`, sections 1-6, 10, and 14-18.

## Global Constraints

### Confirmed execution baseline

- Generate and execute this plan only from the existing linked worktree `/Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations` on branch `codex/python-module-decomposition-foundations`. This plan does not authorize another branch or worktree.
- The audited Batch 3 production baseline is commit `e35bf71549f14a30325c0301b0251e9cc43c397a`. Before implementation, require this commit or an explicitly reviewed descendant containing only accepted work. If a named production file has drifted, stop and re-audit that subbatch before moving code.
- Current production sizes are `skill_design_service.py` 3,865 lines, `execution_approval.py` 2,836 lines, and `agent_design_service.py` 2,406 lines. The larger figures in the design document describe the pre-Batch-1 baseline and are not execution coordinates.
- The current worktree contains these user-owned untracked documents:
  - `docs/superpowers/plans/2026-09-02-python-module-decomposition-foundations.md`
  - `docs/superpowers/plans/2026-09-02-python-module-decomposition-gateway-routes.md`
  - `docs/superpowers/specs/2026-09-02-python-module-decomposition-design.md`
  Preserve them. This plan is a fourth intentional untracked document until the user separately authorizes staging.
- Root `.env` and `config.yaml` are present, ignored, and byte-for-byte match the saved source checkout as of this audit. Never print their values. The approved non-production PostgreSQL and MinIO environment produced this current full-suite baseline:

  ```text
  backend core stats: collected=6493 passed=6493 failed=0 skipped=0
  6493 passed, 7 deselected, 26 warnings in 959.88s
  ```

- Because the Make target does not itself load every root environment value, use `uv run --env-file ../.env make test` from `backend/` unless the invoking shell already exports the same approved non-production environment.
- The full backend gate currently takes about 16 minutes, so Tasks 1-11 use focused/offline or selected PostgreSQL gates. Run the full `make test` path once in Task 12, and repeat it only if a subsequent fix invalidates that evidence.

### Scope boundary

- Production code first. Tests may change imports and monkeypatch targets with the corresponding moved owner and may add focused compatibility or characterization tests. Do not reorganize test directories, split existing large PostgreSQL files, or refactor the test framework.
- Batch 3 production scope is limited to:
  - `backend/app/shared_assets/skill_design_service.py`
  - new `backend/app/shared_assets/skill_design_lifecycle.py`
  - new `backend/app/shared_assets/skill_builder_draft_sink.py`
  - the exact Skill Builder Gateway and Worker consumers
  - `backend/app/private_work/execution_approval.py`
  - new `execution_approval_policy.py`, `execution_approval_codec.py`, `execution_approval_worker.py`, `execution_approval_recovery.py`, and `execution_approval_service.py`
  - existing `execution_approval_lifecycle.py` only for shared database-clock ownership
  - the exact Approval Gateway, Worker-executor, and settlement-handler consumers
  - `backend/app/shared_assets/agent_design_service.py`
  - new `backend/app/shared_assets/agent_design_generation_lifecycle.py`
  - focused tests and `backend/AGENTS.md`.
- `sandbox/tools.py`, Harness `runtime/runs/worker.py`, `RunAgentPrivateExecutor` decomposition, Knowledge implementation, schema/DDL, repositories, frontend, Scheduler, deployment, and Batch 4-6 targets are out of scope.
- Do not create a public Agent/Skill Builder base class, a new repository, a clock module, a sixth Execution Approval contracts module, a factory layer, or speculative interfaces.
- Do not change HTTP payloads, error codes/text, Activity payloads, stream behavior, Tool contracts, model prompts, secret handling, execution plans, result truncation, transaction count, commit ownership, lock order, lease validation, retry safety, or process responsibility.
- A structural move must not fix an observed behavior bug. Stop and request a separate design when behavior must change.
- New owners must never import their compatibility façade. Harness code must not import `app.*`.
- Use `apply_patch` for edits, preserve unrelated changes, and never use destructive reset/checkout commands.
- The plan itself authorizes no staging, commit, push, merge, publication, or cleanup. Each task's commit checkpoint is conditional on the execution mode receiving explicit local-commit authorization.

### Audited design refinements

1. Skill Builder Draft/Terminal Sink means both the current 72-line adapter and the complete Worker transaction/CAS block at `skill_design_service.py:1377-2082`. Moving only the adapter would preserve Worker coupling to the Gateway lifecycle and would not satisfy the design.
2. Skill Validate is currently a two-phase success flow plus a failure-record transaction; Commit is a three-stage flow plus a failure-record transaction; Cancel can wait outside a transaction and then performs its terminal cleanup transaction. “Do not split the transaction” means preserve these existing phases and commit owners, not merge them into one transaction.
3. `PrivateRunJobHandler` owns settlement-time baseline restoration and the unique Skill Builder terminal inside the Run Settlement transaction. It stays outside the new draft sink.
4. `execution_approval_lifecycle.py` already owns 984 lines of shared transactional convergence, and `execution_approval_audit.py` already owns its audit port. Keep both owners. Move only `_database_now` and the compatibility-only `_now` into the existing lifecycle module; do not create a clock helper module.
5. Execution Approval `decide()` already consists of decision transaction, transaction-external atomic Run admission, and a second verification transaction. Move the whole method unchanged; do not collapse or redistribute those phases.
6. Agent Builder is now Gateway-only. `agent_design_service.py:_prepare_turn` also owns generic idempotency/fencing and the manual Blueprint branch, so only its generation tail moves. Worker must not acquire an Agent Design import.
7. Internal module movement changes where Python function globals resolve. Move each affected test monkeypatch to the owning module in the same task; do not simulate façade monkeypatch propagation with wrappers or duplicated mutable globals.
8. Public process architecture is unchanged, so Batch 3 updates `backend/AGENTS.md` only. Do not add a README changelog paragraph.

### Final ownership layout

```text
backend/app/shared_assets/
├── skill_design_service.py                  # compatibility façade
├── skill_design_lifecycle.py                # Gateway session/lifecycle owner
├── skill_builder_draft_sink.py              # Run-bound Worker draft/terminal owner
├── skill_design_contracts.py                # existing immutable contracts
├── skill_design_codec.py                    # existing pure codec
├── skill_design_validation.py               # existing pure validation
├── agent_design_service.py                  # session/manual Blueprint/Commit/Cancel owner
└── agent_design_generation_lifecycle.py      # model-turn/stop/recovery collaborator

backend/app/private_work/
├── execution_approval.py                    # compatibility façade
├── execution_approval_policy.py             # frozen provider policy
├── execution_approval_codec.py              # plan/result envelope codec
├── execution_approval_worker.py             # Worker lease-bound port
├── execution_approval_recovery.py           # settlement/replay helpers
├── execution_approval_service.py            # Gateway reads/decision orchestration
├── execution_approval_lifecycle.py           # existing shared transactional convergence
└── execution_approval_audit.py               # existing audit port
```

Dependency direction (an arrow points from the lower-level owner to its consumer):

```text
Skill contracts/codec/validation/repository/activity
                         ↓
             skill_builder_draft_sink
                         ↓
              skill_design_lifecycle
                         ↓
             skill_design_service façade

execution_approval_policy ──→ execution_approval_codec
execution_approval_lifecycle ──→ execution_approval_recovery
execution_approval_lifecycle + policy + codec ──→ execution_approval_worker
execution_approval_lifecycle + policy + codec ──→ execution_approval_service
policy + codec + lifecycle + recovery + worker + service ──→ execution_approval façade

Agent contracts/codec/validation/generation/control/profile/repository/activity
                                      ↓
                 agent_design_generation_lifecycle
                                      ↓
                         agent_design_service
                                      ↓
                              Gateway Router
```

### Frozen compatibility surfaces

- `skill_design_service.__all__`: 22 ordered names, digest `b9e7397e798f62e2ba3c2c2e58f48939c661c7e937a2c3d687ad321035affed6`.
- `execution_approval.__all__`: 6 ordered names, digest `5b0c7bc581882c2606df87a03d0da7023254266b8c73e19acc081009827d5702`.
- `agent_design_service.__all__`: 28 ordered names, digest `cb915819337a8b5c5ed19d6483393f1e44d68c9bc1b4aa30915138b3fad2f55c`.
- Repository consumers currently import 19 distinct names from `skill_design_service`, 7 from `execution_approval`, and 20 from `agent_design_service`. Task 1 records the exact sets as compatibility data before moving production code; later owner-import migrations may reduce actual legacy imports but must not remove those attributes from the compatibility modules.
- `skill_design_service` must continue exposing the 28 contract owner names, `SkillDesignActivity`, `SkillDesignGenerationRequest`, all existing codec/validation static descriptors, and the tested `_draft_snapshot`, `_file_views`, `_recover_stale_generating`, and `_progress_json` seams.
- `execution_approval` must preserve the exact six-name `__all__` plus these current private module-level definitions as same-object re-exports: `_now`, `_database_now`, `_canonical_digest`, `_bounded_text`, `_decision_digest`, `_idempotency_digest`, `_private_envelope`, `_frozen_plan_from_row`, `_result_payload`, `_outcome_from_receipt`, `_asset_closure`, and `_staged_approval_source_job_id`. Preserve the seven constants `_RESULT_TEXT_LIMIT`, `_CLAIM_TTL_SECONDS`, `_PRIVATE_ENVELOPE_SCHEMA_VERSION`, `_PROVIDER_POLICY_SCHEMA_VERSION`, `_RESULT_SCHEMA_VERSION`, `_CONTINUATION_NAME`, and `_HOST_EXECUTION_MODES` as well.
- `agent_design_service` remains a production owner, not a façade. Its public constructor and method signatures and its 28-name `__all__` remain unchanged.

---

## Task 1: Freeze Batch 3 compatibility and process-boundary contracts

**Files:**

- Create: `backend/tests/test_python_module_decomposition_process_boundaries.py`
- Verify: the three current monoliths and their direct production consumers

**Interfaces:**

- Consumes: current HEAD `e35bf71549f14a30325c0301b0251e9cc43c397a` and existing Batch 0-2 contracts.
- Produces: stable export/import inventories, signature checks, dependency scanners, and façade AST helpers used by Tasks 2-11.

- [ ] **Step 1: verify the exact baseline and preserved files**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations
  git branch --show-current
  git rev-parse HEAD
  git status --short
  git check-ignore -v .env config.yaml
  cmp -s /Users/jiangfeng/workspace/deer-flow/.env .env
  cmp -s /Users/jiangfeng/workspace/deer-flow/config.yaml config.yaml
  ```

  Require the existing branch, audited commit or reviewed descendant, ignored configuration, and only the four intentional documentation files after this plan is created.

- [ ] **Step 2: add reusable AST/import and export-digest helpers**

  ```python
  import ast
  import hashlib
  import inspect
  import json
  from pathlib import Path

  BACKEND_ROOT = Path(__file__).resolve().parents[1]


  def _absolute_imports(path: Path) -> set[str]:
      tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
      imports: set[str] = set()
      for node in ast.walk(tree):
          if isinstance(node, ast.Import):
              imports.update(alias.name for alias in node.names)
          elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
              imports.add(node.module)
      return imports


  def _tree_imports(path: Path) -> set[str]:
      return set().union(
          *(
              _absolute_imports(candidate)
              for candidate in sorted(path.rglob("*.py"))
          )
      )


  def _export_digest(module: object) -> tuple[int, str]:
      names = tuple(getattr(module, "__all__"))
      payload = json.dumps(names, separators=(",", ":")).encode()
      return len(names), hashlib.sha256(payload).hexdigest()


  def _top_level_runtime_nodes(path: Path) -> tuple[str, ...]:
      tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
      return tuple(
          type(node).__name__
          for node in tree.body
          if not (
              isinstance(node, ast.Expr)
              and isinstance(node.value, ast.Constant)
              and isinstance(node.value.value, str)
          )
      )
  ```

- [ ] **Step 3: freeze ordered exports and public signatures**

  Add assertions for the three exact `(count, digest)` pairs from Global Constraints. Freeze these signatures without freezing implementation module names:

  ```python
  def test_batch3_service_signatures_are_frozen() -> None:
      from app.private_work.execution_approval import (
          ExecutionApprovalService,
          WorkerHostExecutionApprovalPort,
      )
      from app.shared_assets.agent_design_service import AgentDesignService
      from app.shared_assets.skill_design_service import SkillDesignService

      expected = {
          SkillDesignService: {
              "__init__": (
                  "self", "session_factory", "generator", "skill_service",
                  "repository_factory", "run_admission", "quota", "audit",
                  "stale_generating_seconds",
              ),
              "terminal_sink": ("self", "context", "claim"),
          },
          AgentDesignService: {
              "__init__": (
                  "self", "session_factory", "generator", "agent_service",
                  "repository_factory", "default_tool_groups_provider",
                  "stale_generating_seconds", "generation_control",
              ),
              "submit_turn": ("self", "context", "session_id", "command"),
              "stop_turn": ("self", "context", "session_id"),
          },
      }
      for owner, methods in expected.items():
          for name, parameters in methods.items():
              assert tuple(inspect.signature(getattr(owner, name)).parameters) == parameters

      assert "approval_id" in inspect.signature(
          WorkerHostExecutionApprovalPort.complete_host_execution,
      ).parameters
      assert tuple(inspect.signature(ExecutionApprovalService.decide).parameters) == (
          "self", "context", "thread_id", "source_run_id", "approval_id",
          "decision", "expected_version", "idempotency_key",
      )
  ```

- [ ] **Step 4: freeze exact repository import inventories**

  Encode these audited compatibility sets exactly:

  ```python
  SKILL_DESIGN_LEGACY_NAMES = frozenset(
      {
          "CancelSkillDesignSession",
          "CommitSkillDesignSession",
          "CreateSkillDesignRevisionSession",
          "CreateSkillDesignSession",
          "SetSkillDesignExecutionPreference",
          "SkillDesignActivity",
          "SkillDesignClarificationResponse",
          "SkillDesignClarificationTurn",
          "SkillDesignCommitResult",
          "SkillDesignDraftUpdateTurn",
          "SkillDesignGenerationRequest",
          "SkillDesignMessageTurn",
          "SkillDesignService",
          "SkillDesignSessionSummary",
          "SkillDesignSessionView",
          "SkillDesignStatus",
          "SkillDesignTurnAttachment",
          "SubmitSkillDesignTurn",
          "ValidateSkillDesignSession",
      }
  )
  EXECUTION_APPROVAL_LEGACY_NAMES = frozenset(
      {
          "ExecutionApprovalProjection",
          "ExecutionApprovalService",
          "HostExecutionProviderPolicySnapshot",
          "WorkerHostExecutionApprovalPort",
          "_asset_closure",
          "recover_staged_execution_approval_id",
          "settle_staged_execution_approvals",
      }
  )
  AGENT_DESIGN_LEGACY_NAMES = frozenset(
      {
          "AGENT_DESIGN_SLUG_MAX_LENGTH",
          "AGENT_DESIGN_SLUG_MIN_LENGTH",
          "AGENT_DESIGN_SLUG_PATTERN",
          "AgentDesignBlueprint",
          "AgentDesignBlueprintTurn",
          "AgentDesignClarificationResponse",
          "AgentDesignClarificationTurn",
          "AgentDesignCommitResult",
          "AgentDesignMessageTurn",
          "AgentDesignService",
          "AgentDesignSessionPage",
          "AgentDesignSessionSummary",
          "AgentDesignSessionView",
          "AgentDesignStatus",
          "CancelAgentDesignSession",
          "CommitAgentDesignSession",
          "CreateAgentDesignSession",
          "MAX_INCOMPLETE_AGENT_DESIGN_SESSIONS_PER_OWNER_PROJECT",
          "SetAgentDesignGenerationPreference",
          "SubmitAgentDesignTurn",
      }
  )
  ```

  Scan `backend/app/**/*.py` plus `backend/tests/**/*.py`, excluding only each exact defining module path. Before movement, assert equality with these three sets. Keep the constants after consumers migrate, but change the enduring assertion to: every recorded name remains an attribute of its legacy module, and every remaining direct legacy import is a subset of the recorded set. This freezes compatibility without requiring production code to keep importing the façades.

- [ ] **Step 5: freeze process consumers and transaction-owner boundaries**

  Assert:

  - Worker executor has one Skill Builder service construction and no Agent Design import; `app/worker`, `app/reliability/run_execution`, and Harness remain free of Agent Design imports.
  - Gateway owns Agent Builder construction and calls.
  - Approval production imports are exactly `gateway/deps.py`, the two Private Work owner modules, executor, and handler.
  - Harness contains no `app.*` import.
  - Existing `execution_approval_lifecycle.py` and `execution_approval_audit.py` remain separate modules.

- [ ] **Step 6: run the characterization test before production movement**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_python_module_decomposition_contract.py -q
  ```

  Expected: PASS on untouched Batch 3 production code.

- [ ] **Step 7: checkpoint**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check tests/test_python_module_decomposition_process_boundaries.py
  uvx ruff format --check tests/test_python_module_decomposition_process_boundaries.py
  cd ..
  git diff --check
  git status --short
  ```

  If local commits are authorized, commit only the new test with message `test(app): freeze batch 3 process boundaries`.

## Task 2: Extract the Run-bound Skill Builder Draft/Terminal Sink

**Files:**

- Create: `backend/app/shared_assets/skill_builder_draft_sink.py`
- Modify: `backend/app/shared_assets/skill_design_service.py:344-430,1377-2089,3445-3504,3668-3691`
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py`
- Modify: `backend/tests/test_skill_builder_durable_agent_postgres.py`

**Interfaces:**

- Consumes: `SkillBuilderDraftSink` Protocol, `PrivateWorkContext`, `JobClaim`, `SkillDesignRepository`, `SkillService`, and current lease/context validation.
- Produces: `SkillDesignDraftSink`, `_draft_snapshot`, and `_draft_checksum_from_metadata`; `SkillDesignService.terminal_sink()` returns this exact implementation.

- [ ] **Step 1: add the failing owner/import-direction contract**

  ```python
  def test_skill_design_draft_sink_is_the_run_bound_owner() -> None:
      from app.shared_assets import skill_builder_draft_sink as draft_sink_owner
      from app.shared_assets.skill_builder_draft_sink import (
          SkillDesignDraftSink,
          _draft_snapshot,
      )
      from app.shared_assets.skill_design_service import SkillDesignService

      assert SkillDesignService.__dict__["_draft_snapshot"].__func__ is _draft_snapshot
      assert (
          SkillDesignService.__dict__["_append_row_message"].__func__
          is draft_sink_owner._append_row_message
      )
      expected = {
          "list_candidate_files": ("self", "request"),
          "read_candidate_file": ("self", "request"),
          "upsert_candidate_file": ("self", "request"),
          "delete_candidate_file": ("self", "request"),
          "request_clarification": ("self", "result"),
          "finalize_candidate": ("self", "request", "dependencies"),
      }
      for name, parameters in expected.items():
          assert tuple(inspect.signature(getattr(SkillDesignDraftSink, name)).parameters) == parameters
  ```

  Also assert the new owner imports neither `skill_design_service` nor `skill_design_lifecycle`.

- [ ] **Step 2: run RED before creating the owner**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py::test_skill_design_draft_sink_is_the_run_bound_owner -q
  ```

  Expected: FAIL with `ModuleNotFoundError` for `skill_builder_draft_sink`.

- [ ] **Step 3: create the one concrete Run-bound implementation**

  Use this exact constructor and no additional factory/protocol:

  ```python
  class SkillDesignDraftSink:
      def __init__(
          self,
          session_factory: Callable[[], AsyncSession],
          context: PrivateWorkContext,
          claim: JobClaim,
          *,
          skill_service: SkillService | None = None,
          repository_factory: Callable[[AsyncSession], SkillDesignRepository] = SkillDesignRepository,
      ) -> None:
          self._session_factory = session_factory
          self._context = context
          self._claim = claim
          self._skill_service = skill_service or SkillService(session_factory)
          self._repository_factory = repository_factory
  ```

  Replace the current adapter at lines 344-415 and move `_BuilderToolTransaction`, `_SkillBuilderDraftState`, plus the complete transaction/CAS/terminal implementation at lines 1377-2082 into this class. Its six public methods must match the existing `SkillBuilderDraftSink` Protocol exactly: `list_candidate_files`, `read_candidate_file`, `upsert_candidate_file`, `delete_candidate_file`, `request_clarification`, and `finalize_candidate`. Bind `context` and `claim` in the constructor rather than accepting model-controlled coordinates; remove the old private Service entry methods because the repository has no direct caller for them.

  Bind existing lower owners directly rather than copying helpers:

  ```python
  from app.shared_assets.skill_design_codec import (
      _clarification_json,
      _clarification_request,
      _message_json,
      _progress_json,
      _request_checksum,
  )
  from app.shared_assets.skill_design_validation import (
      _require_message_capacity,
      _require_preview_name,
      _validate_builder_files,
      _validate_partial_builder_files,
  )
  ```

  Replace class-qualified calls in the moved block with these owner functions and the module-level draft helpers. Continue importing `contains_secret_like_material` from `skill_design_generation`.

- [ ] **Step 4: preserve the complete authority and transaction sequence**

  `_builder_tool_transaction()` must still perform, in order, issued-context validation, exact Run/Job/Attempt coordinate checks, transaction-bound ProjectContext resolution, read/edit capability checks, operation/session locks, target-live validation, `assert_execution_active()`, mutation, flush, and exception mapping. It must create exactly one `session.begin()` per Tool/terminal call and never commit explicitly.

- [ ] **Step 5: retain shared exact helpers and the legacy entry**

  Own `_draft_snapshot`, `_draft_checksum_from_metadata`, and the current mutating `_append_row_message` body at module level. `_append_row_message` must call `_require_message_capacity` and `_message_json` directly and keep its secret check and `datetime.now(UTC)` timestamp. This avoids a Sink-to-lifecycle cycle without polluting the pure codec or validation modules.

  Import `_append_row_message` and `_draft_snapshot` into the lifecycle owner and retain both as exact static compatibility aliases on `SkillDesignService`:

  ```python
  _append_row_message = staticmethod(_append_row_message_impl)
  _draft_snapshot = staticmethod(_draft_snapshot_impl)
  ```

  Replace `terminal_sink()` with:

  ```python
  def terminal_sink(
      self,
      context: PrivateWorkContext,
      claim: JobClaim,
  ) -> SkillBuilderDraftSink:
      return SkillDesignDraftSink(
          self._session_factory,
          context,
          claim,
          skill_service=self._skill_service,
          repository_factory=self._repository_factory,
      )
  ```

  Do not move quota/audit no-ops, Run admission, stop/cancel, stale recovery, or Handler settlement fallback.

- [ ] **Step 6: prove new and compatibility paths share behavior**

  Extend the durable PostgreSQL test so one exact claimed Run uses `SkillDesignDraftSink` for the first terminal and `SkillDesignService.terminal_sink()` for idempotent replay. Assert identical receipt, draft checksum, Activity, baseline clearing, and terminal state.

- [ ] **Step 7: run focused and PostgreSQL gates**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_skill_builder_draft_projection.py -q

  PYTHONPATH=. uv run python tests/support/core_gate_plugin.py \
    tests/test_skill_builder_durable_agent_postgres.py::test_durable_builder_run_replay_retry_delta_cancel_and_delete_link \
    tests/test_skill_builder_revision_postgres.py::test_revision_delete_fails_open_sessions_and_revokes_in_flight_tools \
    -q -m "not provider_integration"
  ```

  Expected: PASS with zero PostgreSQL skips.

- [ ] **Step 8: static checkpoint**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/shared_assets/skill_builder_draft_sink.py \
    app/shared_assets/skill_design_service.py \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_skill_builder_durable_agent_postgres.py
  uvx ruff format --check \
    app/shared_assets/skill_builder_draft_sink.py \
    app/shared_assets/skill_design_service.py \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_skill_builder_durable_agent_postgres.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit with message `refactor(skill-builder): extract draft sink`.

## Task 3: Make the Worker executor construct the Draft Sink owner directly

**Files:**

- Modify: `backend/app/reliability/run_execution/executor.py:108,588-608`
- Modify: `backend/tests/knowledge/test_agent_tool.py:836-867`
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py`

**Interfaces:**

- Consumes: `SkillDesignDraftSink(session_factory, context, claim)` from Task 2.
- Produces: Worker Skill Builder construction independent of the Gateway `SkillDesignService`.

- [ ] **Step 1: add the failing production-import assertion**

  Assert executor imports `app.shared_assets.skill_builder_draft_sink` and does not import `app.shared_assets.skill_design_service`. Run it before the production edit and require failure on the legacy import.

- [ ] **Step 2: replace only the import and construction expression**

  ```python
  from app.shared_assets.skill_builder_draft_sink import SkillDesignDraftSink

  # inside _resolve_agent_factory()
  draft_sink=SkillDesignDraftSink(
      self._factory,
      execution.context,
      claim,
  ),
  ```

  Preserve the surrounding `SkillBuilderAgentFactory`, authoring catalog, runtime-kind branch, Knowledge exclusion, and factory order.

- [ ] **Step 3: migrate the corresponding monkeypatch seam**

  In `TestExecutorFactoryResolution.test_skill_builder_run_never_receives_the_knowledge_tool`, patch `executor_module.SkillDesignDraftSink`. The fake must capture the exact factory, issued context, and `JobClaim` and return the existing fake sink contract. Do not reorganize the Knowledge test file.

- [ ] **Step 4: run Worker construction gates**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_skill_builder_provider_execution.py::test_skill_builder_executor_installs_private_file_authority_with_exact_skill_mount \
    tests/knowledge/test_agent_tool.py::TestExecutorFactoryResolution::test_skill_builder_run_never_receives_the_knowledge_tool \
    tests/test_skill_builder_terminal_recovery.py \
    tests/test_run_execution_modules.py -q
  ```

  Expected: PASS; no Agent Design import appears in Worker code.

- [ ] **Step 5: inspect the mechanical diff and checkpoint**

  Remove imports from before/after executor ASTs and assert the only remaining AST change is the constructor name and eliminated `.terminal_sink()` call at the same branch position. Then run:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/reliability/run_execution/executor.py \
    tests/knowledge/test_agent_tool.py \
    tests/test_python_module_decomposition_process_boundaries.py
  uvx ruff format --check \
    app/reliability/run_execution/executor.py \
    tests/knowledge/test_agent_tool.py \
    tests/test_python_module_decomposition_process_boundaries.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit with message `refactor(worker): use skill builder draft sink`.

## Task 4: Move Skill Design lifecycle ownership and finish its façade

**Files:**

- Create: `backend/app/shared_assets/skill_design_lifecycle.py`
- Rewrite: `backend/app/shared_assets/skill_design_service.py`
- Modify: `backend/app/gateway/routers/project_skill_builder.py:54-72`
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py`

**Interfaces:**

- Consumes: Task 2 sink owner and existing contracts/codec/validation/repository/activity/generation/admission ports.
- Produces: `skill_design_lifecycle.SkillDesignService`; legacy `skill_design_service.SkillDesignService` is the exact same class object.

- [ ] **Step 1: add failing lifecycle/façade identity and import-order tests**

  Assert:

  ```python
  legacy.SkillDesignService is owning.SkillDesignService
  _export_digest(legacy) == (
      22,
      "b9e7397e798f62e2ba3c2c2e58f48939c661c7e937a2c3d687ad321035affed6",
  )
  ```

  Add fresh subprocesses for owner-first and façade-first imports. Parse the final façade and permit only its docstring, imports, and the exact `__all__` assignment. Confirm RED because `skill_design_lifecycle.py` does not exist.

- [ ] **Step 2: move the complete remaining lifecycle class as one owner**

  Move `_constraint_name`, `_RepositoryFactory`, both Run quota/audit no-ops, lifecycle constants, and the remaining `SkillDesignService` implementation into `skill_design_lifecycle.py`. Preserve the constructor signature and exact existing phases for create/revision/list/get/submit/preference/activity/stop/Validate/Commit/Cancel/manual draft update/generation/stale recovery.

  Import `SkillDesignDraftSink`, `_draft_snapshot`, and `_draft_checksum_from_metadata` from Task 2. Do not import the façade.

- [ ] **Step 3: keep tested compatibility descriptors on the same class**

  Preserve all 15 codec and 26 validation descriptors as exact owner functions. Preserve `_draft_snapshot` as the Task 2 function object and retain `_file_views`, `_recover_stale_generating`, `_progress_json`, and `terminal_sink()` behavior.

- [ ] **Step 4: replace the old module with explicit compatibility imports**

  Set its docstring to `"""Compatibility façade for Project Skill Builder lifecycle."""`. Re-export the exact lifecycle class, all 28 contract names, `SkillDesignActivity`, `SkillDesignGenerationRequest`, and every name in `SKILL_DESIGN_LEGACY_NAMES`. Do not add the new Sink class or module-level helpers to the legacy module merely for discoverability; compatibility is carried by the same `SkillDesignService` class object and its frozen descriptors. Keep the exact existing 22-name ordered `__all__`; do not create wrappers, subclasses, warnings, or mutable proxy state.

- [ ] **Step 5: migrate the Gateway consumer to owners**

  `project_skill_builder.py` must import DTOs/commands from `skill_design_contracts`, `SkillDesignActivity` from `skill_design_activity`, and `SkillDesignService` from `skill_design_lifecycle`. Keep the service factory, app-state cache, HTTP handlers, error mapping, and route order unchanged.

- [ ] **Step 6: run Skill Builder focused gates**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_python_module_decomposition_contract.py \
    tests/test_skill_builder_admission_boundary.py \
    tests/test_skill_builder_design_record.py \
    tests/test_skill_builder_draft_projection.py \
    tests/test_skill_builder_durable_boundaries.py \
    tests/test_skill_builder_execution_options.py \
    tests/test_skill_builder_revision_contract.py \
    tests/test_skill_builder_session_summary.py \
    tests/test_skill_builder_terminal_recovery.py \
    -q -m "not postgres and not provider_integration"

  PYTHONPATH=. uv run python tests/support/core_gate_plugin.py \
    'tests/test_skill_builder_revision_postgres.py::test_create_session_validate_and_commit_saves_candidate_v1' \
    'tests/test_skill_builder_revision_postgres.py::test_stale_commit_is_recovered_with_failed_terminal[asyncio-persistence]' \
    'tests/test_skill_builder_durable_agent_postgres.py::test_cancel_waits_for_active_builder_settlement_before_clearing' \
    'tests/test_skill_builder_durable_agent_postgres.py::test_stale_builder_run_restores_baseline_and_records_failed_terminal' \
    -q -m "not provider_integration"
  ```

  These four nodes cover Validate/Commit success, stale Commit recovery, generating Cancel after Worker settlement, and stale Run baseline restoration. Require zero skips; the three complete Skill Builder PostgreSQL files run once in Task 12.

- [ ] **Step 7: verify final Skill import direction and checkpoint**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  ! rg -n '^[[:space:]]*(from|import).*skill_design_service([[:space:].]|$)' \
    app --glob '*.py'
  uvx ruff check \
    app/shared_assets/skill_design_lifecycle.py \
    app/shared_assets/skill_design_service.py \
    app/gateway/routers/project_skill_builder.py \
    tests/test_python_module_decomposition_process_boundaries.py
  uvx ruff format --check \
    app/shared_assets/skill_design_lifecycle.py \
    app/shared_assets/skill_design_service.py \
    app/gateway/routers/project_skill_builder.py \
    tests/test_python_module_decomposition_process_boundaries.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  The negated import scan must return success because there are zero production imports of the legacy module (the façade does not import itself). If authorized, commit with message `refactor(skill-builder): own lifecycle separately`.

## Task 5: Extract Execution Approval provider policy

**Files:**

- Create: `backend/app/private_work/execution_approval_policy.py`
- Modify: `backend/app/private_work/execution_approval.py:110,113-120,134-142,177-371`
- Modify: `backend/tests/test_execution_approval_lifecycle_postgres.py:43-49` (import owner only)
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py`

**Interfaces:**

- Consumes: typed `AppConfig`, Sandbox host-execution mode, and `HostExecutionDomainSnapshot` inputs.
- Produces: `_canonical_digest` and `HostExecutionProviderPolicySnapshot`; Tasks 6, 8, and 9 import them one-way.

- [ ] **Step 1: add and run the failing exact-owner test**

  Assert legacy and owner export the same `_canonical_digest` and `HostExecutionProviderPolicySnapshot`, and the owner does not import the legacy module. Expected RED: missing `execution_approval_policy`.

- [ ] **Step 2: move the exact policy block**

  Move `_PROVIDER_POLICY_SCHEMA_VERSION`, `_HOST_EXECUTION_MODES`, `_canonical_digest`, and the complete dataclass. `_canonical_digest` stays here deliberately: codec must import policy to decode snapshots, so putting the digest in codec would create a cycle.

- [ ] **Step 3: preserve all policy contracts**

  Keep exact `from_app_config()` path normalization, approval enablement, timeout minimum, mount tuple ordering, strict payload key set, schema version, digest bytes, validation errors, and non-secret fields. Re-export same objects from the legacy module.

  Change only the existing `HostExecutionProviderPolicySnapshot` import in `test_execution_approval_lifecycle_postgres.py` to the new policy owner. Do not reorganize that large PostgreSQL test module; the two focused policy nodes below remain the immediate gate.

- [ ] **Step 4: run policy and identity tests**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_execution_approval_lifecycle_postgres.py::test_provider_policy_snapshot_is_derived_from_typed_app_config \
    tests/test_execution_approval_lifecycle_postgres.py::test_provider_policy_snapshot_binds_local_mount_and_skill_mapping \
    -q
  ```

- [ ] **Step 5: static checkpoint**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/private_work/execution_approval_policy.py \
    app/private_work/execution_approval.py \
    tests/test_execution_approval_lifecycle_postgres.py \
    tests/test_python_module_decomposition_process_boundaries.py
  uvx ruff format --check \
    app/private_work/execution_approval_policy.py \
    app/private_work/execution_approval.py \
    tests/test_execution_approval_lifecycle_postgres.py \
    tests/test_python_module_decomposition_process_boundaries.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit with message `refactor(approval): extract provider policy`.

## Task 6: Extract Execution Approval envelope and receipt codec

**Files:**

- Create: `backend/app/private_work/execution_approval_codec.py`
- Modify: `backend/app/private_work/execution_approval.py:107,109,111,145-150,374-493`
- Create: `backend/tests/test_execution_approval_codec.py`
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py`

**Interfaces:**

- Consumes: Task 5 policy snapshot/digest and existing Harness plan/domain/outcome types.
- Produces: `_bounded_text`, `_private_envelope`, `_frozen_plan_from_row`, `_result_payload`, and `_outcome_from_receipt` with the existing schema constants.

- [ ] **Step 1: write RED identity and pure-codec tests**

  Import the missing owner and assert exact legacy identities. Build one typed `HostExecutionPlan`, one typed `HostExecutionProviderPolicySnapshot`, and one typed `HostExecutionDomainSnapshot`. Use `types.SimpleNamespace` as the smallest DB-free row double with exactly `command_private_json`, `tool_call_id`, `source_run_id`, `thread_id`, `command_digest`, `source_agent_path`, and `execution_domain_affinity`; populate it from the three typed objects and `_private_envelope(...)`.

  Assert:

  ```python
  decoded_plan, decoded_policy, decoded_domain = _frozen_plan_from_row(row)
  assert decoded_plan == plan
  assert decoded_policy == policy
  assert decoded_domain == domain
  ```

  Parametrize one mutation at a time for provider-policy digest, plan digest, agent path, execution-domain affinity, and timeout; each must raise `ValueError`. Verify 20,001-character stdout/stderr/result values truncate to 20,000 with the exact flags, and receipt digest/scope mismatch fails closed.

- [ ] **Step 2: run RED before creating the codec**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest tests/test_execution_approval_codec.py -q
  ```

  Expected: collection/import failure for `execution_approval_codec`.

- [ ] **Step 3: move only pure serialization code**

  Move `_RESULT_TEXT_LIMIT`, `_PRIVATE_ENVELOPE_SCHEMA_VERSION`, `_RESULT_SCHEMA_VERSION`, and the five exact functions. Import `_canonical_digest` and `HostExecutionProviderPolicySnapshot` from Task 5. The codec must not import session factories, Gateway services, Worker ports, or the façade.

- [ ] **Step 4: run codec plus existing Approval contract tests**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest \
    tests/test_execution_approval_codec.py \
    tests/test_execution_approval_api.py \
    tests/test_execution_approval_skill_source_closure.py \
    tests/test_python_module_decomposition_process_boundaries.py -q
  ```

- [ ] **Step 5: static checkpoint**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/private_work/execution_approval_codec.py \
    app/private_work/execution_approval.py \
    tests/test_execution_approval_codec.py \
    tests/test_python_module_decomposition_process_boundaries.py
  uvx ruff format --check \
    app/private_work/execution_approval_codec.py \
    app/private_work/execution_approval.py \
    tests/test_execution_approval_codec.py \
    tests/test_python_module_decomposition_process_boundaries.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit with message `refactor(approval): extract envelope codec`.

## Task 7: Extract Execution Approval settlement and replay recovery

**Files:**

- Modify: `backend/app/private_work/execution_approval_lifecycle.py`
- Create: `backend/app/private_work/execution_approval_recovery.py`
- Modify: `backend/app/private_work/execution_approval.py:123-131,2022-2254`
- Modify: `backend/tests/test_execution_approval_private_lifecycle_postgres.py`
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py`

**Interfaces:**

- Consumes: caller-owned SQLAlchemy session, exact `JobClaim`, existing lifecycle/audit/output-delivery/run-marker owners.
- Produces: `_staged_approval_source_job_id`, `settle_staged_execution_approvals`, and `recover_staged_execution_approval_id`; existing lifecycle owns `_database_now` and compatibility `_now`.

- [ ] **Step 1: add failing owner and transaction-shape tests**

  Assert owner/legacy function identity, one-way imports, and that recovery functions accept a caller session and expose no `session_factory` parameter. Confirm RED on the missing recovery module.

- [ ] **Step 2: move shared clock helpers into the existing lifecycle owner**

  Move `_now()` and `_database_now(session)` without changing behavior. `_database_now` remains the authority; `_now` remains compatibility-only and must not become a settlement clock.

- [ ] **Step 3: move all three recovery functions together**

  Preserve exact lock/query order, settlement-only predecessor validation, durable suspension marker checks, output-delivery transitions, audit appends, TTL calculation, and exception types. Do not add `session.begin()` or commit.

- [ ] **Step 4: migrate corresponding monkeypatch paths**

  Change only the three string patches of `_now` to `app.private_work.execution_approval_lifecycle._now`. The asset-closure patch remains for Task 8. Keep the tests proving DB clock ignores the compatibility clock.

- [ ] **Step 5: run recovery and lifecycle gates**

  ```bash
  cd backend
  PYTHONPATH=. uv run python tests/support/core_gate_plugin.py \
    tests/test_execution_approval_lifecycle_postgres.py::test_durable_success_recovers_only_exact_attempt_staged_approval \
    tests/test_execution_approval_lifecycle_postgres.py::test_marker_before_stream_terminal_repairs_success_without_graph_rerun \
    tests/test_execution_approval_lifecycle_postgres.py::test_settle_staged_uses_clock_after_approval_lock_wait \
    tests/test_execution_approval_lifecycle_postgres.py::test_typed_suspension_coordinate_mismatch_rolls_back_source_settlement \
    -q -m "not provider_integration"
  ```

  These nodes cover exact-marker recovery, takeover/repair, post-lock database time, and mismatch rollback. Require PASS with zero skips; complete Approval PostgreSQL files run once in Task 12.

- [ ] **Step 6: static checkpoint**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/private_work/execution_approval_lifecycle.py \
    app/private_work/execution_approval_recovery.py \
    app/private_work/execution_approval.py \
    tests/test_execution_approval_private_lifecycle_postgres.py \
    tests/test_python_module_decomposition_process_boundaries.py
  uvx ruff format --check \
    app/private_work/execution_approval_lifecycle.py \
    app/private_work/execution_approval_recovery.py \
    app/private_work/execution_approval.py \
    tests/test_execution_approval_private_lifecycle_postgres.py \
    tests/test_python_module_decomposition_process_boundaries.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit with message `refactor(approval): extract recovery`.

## Task 8: Move the complete Worker Host Execution Approval port

**Files:**

- Create: `backend/app/private_work/execution_approval_worker.py`
- Modify: `backend/app/private_work/execution_approval.py:503-2019`
- Modify: `backend/tests/test_execution_approval_private_lifecycle_postgres.py`
- Modify: `backend/tests/test_execution_approval_skill_source_closure.py`
- Modify: `backend/tests/test_pinned_skill_version_materializer_postgres.py`
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py`

**Interfaces:**

- Consumes: Tasks 5-7 owners, existing transaction lifecycle/audit/output-delivery owners, `PrivateWorkContext`, and exact `JobClaim`/lease authority.
- Produces: `_asset_closure` and the same `WorkerHostExecutionApprovalPort` class object.

- [ ] **Step 1: write and run the failing owner identity test**

  Assert `legacy.WorkerHostExecutionApprovalPort is owner.WorkerHostExecutionApprovalPort` and `legacy._asset_closure is owner._asset_closure`; reject owner imports of legacy/service/recovery. Expected RED: missing worker owner.

- [ ] **Step 2: move `_asset_closure` and the entire 1,370-line class**

  Move every method from `prepare_host_execution_environment()` through `_complete_host_execution()` together. Do not split `_stage`, claim, final spawn authorization, completion, output-delivery, or lock helpers into collaborators.

- [ ] **Step 3: preserve exact transaction and lock behavior**

  Keep stage/claim/spawn transactions and completion's deletion-tolerant shell locks. Preserve `Project → Membership → Thread → Job → Run → Attempt → Approval → Receipt`, database-clock read positions, source/continuation asset closure comparison, spawn fence, receipt idempotency, retry-safety fence, and output-delivery transaction ownership.

- [ ] **Step 4: move corresponding tests and patch targets to the owner**

  Import `_asset_closure` from the worker owner in the two direct tests. Change the paused closure monkeypatch string to `app.private_work.execution_approval_worker._asset_closure`. Retain separate legacy identity assertions.

- [ ] **Step 5: run Worker/Approval gates**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_execution_approval_skill_source_closure.py \
    tests/test_worker_execution_approval_composition.py \
    tests/test_host_execution_approval.py \
    tests/test_host_execution_continuation_runner.py \
    tests/test_run_worker_host_execution_pause.py \
    tests/test_host_execution_batch_barrier.py -q

  PYTHONPATH=. uv run python tests/support/core_gate_plugin.py \
    tests/test_execution_approval_lifecycle_postgres.py::test_stage_rechecks_source_lease_after_scope_lock_wait \
    tests/test_execution_approval_private_lifecycle_postgres.py::test_claim_revalidates_lease_after_waiting_for_approval_lock \
    tests/test_execution_approval_private_lifecycle_postgres.py::test_claim_rechecks_ttl_after_asset_closure_before_side_effect_mark \
    tests/test_execution_approval_private_lifecycle_postgres.py::test_final_spawn_authorization_is_a_durable_one_shot_cas \
    'tests/test_execution_approval_private_lifecycle_postgres.py::test_completion_prefix_serializes_cleanup_without_deadlock[run_delete]' \
    tests/test_execution_approval_private_lifecycle_postgres.py::test_completion_persists_receipt_after_membership_has_left \
    'tests/test_execution_approval_lifecycle_postgres.py::test_receipt_and_output_delivery_survive_lease_loss_without_respawn[intent_recorded]' \
    tests/test_pinned_skill_version_materializer_postgres.py::test_admission_worker_begin_and_approval_metadata_reads_do_not_detoast_legacy_skill \
    -q -m "not provider_integration"
  ```

  These nodes cover stage, claim revalidation, post-closure TTL, one-shot spawn CAS, deletion-tolerant completion lock order, receipt after membership loss, lease-loss retry safety, and pinned Skill materialization.

- [ ] **Step 6: static checkpoint**

  Compare moved class/function ASTs to the Task 7 checkpoint after ignoring source locations only. Then run:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/private_work/execution_approval_worker.py \
    app/private_work/execution_approval.py \
    tests/test_execution_approval_private_lifecycle_postgres.py \
    tests/test_execution_approval_skill_source_closure.py \
    tests/test_pinned_skill_version_materializer_postgres.py \
    tests/test_python_module_decomposition_process_boundaries.py
  uvx ruff format --check \
    app/private_work/execution_approval_worker.py \
    app/private_work/execution_approval.py \
    tests/test_execution_approval_private_lifecycle_postgres.py \
    tests/test_execution_approval_skill_source_closure.py \
    tests/test_pinned_skill_version_materializer_postgres.py \
    tests/test_python_module_decomposition_process_boundaries.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit with message `refactor(approval): move worker port`.

## Task 9: Move the Gateway Execution Approval service and finish the façade

**Files:**

- Create: `backend/app/private_work/execution_approval_service.py`
- Rewrite: `backend/app/private_work/execution_approval.py`
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py`

**Interfaces:**

- Consumes: policy, codec, existing lifecycle clock/convergence, run admission, revalidation, audit, quota, and Run audit ports. It does not import the Worker or recovery owners.
- Produces: `ExecutionApprovalProjection` and `ExecutionApprovalService`; the old module becomes an exact re-export façade.

- [ ] **Step 1: add failing service/façade/import-order tests**

  Assert exact Service/Projection identity, exact six-name export digest, fresh owner-first and façade-first imports, and an imports-only final façade. Confirm RED on the missing service owner.

- [ ] **Step 2: move the complete Gateway service block**

  Move `_CLAIM_TTL_SECONDS`, `_CONTINUATION_NAME`, `_decision_digest`, `_idempotency_digest`, `ExecutionApprovalProjection`, and the whole `ExecutionApprovalService` class. Keep `active()` and `get()` single-transaction reconciliation/projection behavior.

- [ ] **Step 3: preserve `decide()` as one orchestration unit**

  Preserve its exact three phases:

  1. authorization, locks, reconciliation, and deny/allow decision transaction;
  2. transaction-external `PrivateRunAdmissionService.admit()` atomic admission;
  3. authorization, locks, reconciliation, and continuation-link verification transaction.

  Do not move any phase to Worker/recovery, add a transaction, or convert admission into an in-session call.

- [ ] **Step 4: replace `execution_approval.py` with explicit imports**

  Set a compatibility-façade docstring. Re-export policy, codec, lifecycle clocks, recovery, Worker, and service objects, including every current module-defined private constant/helper. Preserve exactly:

  ```python
  __all__ = [
      "ExecutionApprovalProjection",
      "ExecutionApprovalService",
      "HostExecutionProviderPolicySnapshot",
      "WorkerHostExecutionApprovalPort",
      "recover_staged_execution_approval_id",
      "settle_staged_execution_approvals",
  ]
  ```

  The façade may contain no function/class body, wrapper, warning, object construction, or mutable proxy.

- [ ] **Step 5: run Gateway service and focused Approval PostgreSQL gates**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_execution_approval_api.py \
    tests/test_execution_approval_codec.py -q

  PYTHONPATH=. uv run python tests/support/core_gate_plugin.py \
    tests/test_execution_approval_lifecycle_postgres.py::test_decision_uses_clock_after_approval_lock_wait \
    tests/test_execution_approval_lifecycle_postgres.py::test_approved_unlinked_recovery_uses_clock_after_lock_wait \
    tests/test_execution_approval_lifecycle_postgres.py::test_reader_uses_clock_after_approval_lock_wait \
    tests/test_execution_approval_lifecycle_postgres.py::test_deny_cancels_deferred_output_delivery_obligation \
    tests/test_execution_approval_lifecycle_postgres.py::test_approved_running_continuation_with_pending_run_uses_job_lease \
    -q -m "not provider_integration"
  ```

  Keep every parameterization for decision, reader, and continuation-lease nodes. This covers the three-phase decision orchestration and active/get reconciliation without repeating the full files before Task 12.

- [ ] **Step 6: static checkpoint**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/private_work/execution_approval_service.py \
    app/private_work/execution_approval.py \
    tests/test_python_module_decomposition_process_boundaries.py
  uvx ruff format --check \
    app/private_work/execution_approval_service.py \
    app/private_work/execution_approval.py \
    tests/test_python_module_decomposition_process_boundaries.py
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py -q
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  The repeated process-boundary test is the fresh import-order smoke. If authorized, commit with message `refactor(approval): extract gateway service`.

## Task 10: Migrate Execution Approval production consumers to owners

**Files:**

- Modify: `backend/app/gateway/deps.py:310-313,431`
- Modify: `backend/app/gateway/routers/private_work_routes/contracts.py:11`
- Modify: `backend/app/gateway/routers/private_work_routes/dependencies.py:10`
- Modify: `backend/app/reliability/run_execution/executor.py:25-28`
- Modify: `backend/app/reliability/run_execution/handler.py:21-24`
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py`

**Interfaces:**

- Consumes: stable owner objects from Tasks 5-9.
- Produces: zero internal production imports of `app.private_work.execution_approval`; compatibility façade remains tested and available.

- [ ] **Step 1: add the failing production-import AST gate**

  Scan `backend/app/**/*.py`, excluding only the exact façade path. Detect both direct submodule imports and `from app.private_work import execution_approval`. Run before production changes and require the current five consumer files as findings.

- [ ] **Step 2: migrate imports only**

  Apply this exact mapping:

  - `gateway/deps.py`: policy from `execution_approval_policy`, Service from `execution_approval_service`; retain the delayed import location.
  - Private Work contracts: Projection from `execution_approval_service`.
  - Private Work dependencies: Service from `execution_approval_service`.
  - executor: policy from `execution_approval_policy`, Worker port from `execution_approval_worker`.
  - handler: settlement/recovery functions from `execution_approval_recovery`.

  Change no local symbol name, constructor, registration, handler, settlement order, or lazy-import timing.

- [ ] **Step 3: prove production files are import-only changes**

  Remove `Import`/`ImportFrom` nodes from before/after ASTs and require equality for all five production consumers. The final legacy-import AST findings must be empty.

- [ ] **Step 4: run composition, API, Worker, and PostgreSQL gates**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_worker_execution_approval_composition.py::test_worker_routes_execution_approval_ttl_to_job_handler \
    tests/test_worker_execution_approval_composition.py::test_worker_and_gateway_compose_the_provider_policy_snapshot \
    tests/test_execution_approval_api.py::test_execution_approval_routes_are_owner_scoped_and_strict \
    -q

  PYTHONPATH=. uv run python tests/support/core_gate_plugin.py \
    tests/test_execution_approval_output_delivery_e2e_postgres.py::test_pending_approval_in_one_thread_does_not_block_another_thread_run \
    -q -m "not provider_integration"
  ```

  Consumer changes are import-only; these nodes cover Worker/Gateway composition, strict owner-scoped API wiring, and cross-Thread isolation. The broader behavior suites already ran with their owning moves and run together once in Task 12.

- [ ] **Step 5: static checkpoint**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/gateway/deps.py \
    app/gateway/routers/private_work_routes/contracts.py \
    app/gateway/routers/private_work_routes/dependencies.py \
    app/reliability/run_execution/executor.py \
    app/reliability/run_execution/handler.py \
    tests/test_python_module_decomposition_process_boundaries.py
  uvx ruff format --check \
    app/gateway/deps.py \
    app/gateway/routers/private_work_routes/contracts.py \
    app/gateway/routers/private_work_routes/dependencies.py \
    app/reliability/run_execution/executor.py \
    app/reliability/run_execution/handler.py \
    tests/test_python_module_decomposition_process_boundaries.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit with message `refactor(approval): use process owners`.

## Task 11: Extract Agent Design generation lifecycle as one collaborator

**Files:**

- Create: `backend/app/shared_assets/agent_design_generation_lifecycle.py`
- Modify: `backend/app/shared_assets/agent_design_service.py:230,232-240,252-261,562-720,722-762,1323-1533,1581-2136,2158-2335,2363-2367`
- Modify: `backend/tests/test_agent_builder_model_selection.py:358-361`
- Modify: `backend/tests/test_python_module_decomposition_process_boundaries.py`

**Interfaces:**

- Consumes: current Agent Design contracts/codec/validation/generation/control/profile/repository/activity modules and a caller-owned Activity callback.
- Produces: one concrete `AgentDesignGenerationLifecycle` composed by `AgentDesignService`; no Worker consumer and no new public export.

- [ ] **Step 1: add the failing owner/signature/dependency tests**

  Add `_absolute_imports`/`_tree_imports` assertions and the exact public signatures from Task 1. Assert the owner never imports `agent_design_service`, the Service imports the owner, Worker/runtime/Harness trees import neither Agent Design module, and the existing 28-name export digest is unchanged. Confirm RED because the owner does not exist.

- [ ] **Step 2: create the concrete collaborator with existing dependencies only**

  ```python
  class AgentDesignGenerationLifecycle:
      def __init__(
          self,
          session_factory: Callable[[], AsyncSession],
          *,
          generator: AgentDesignGenerationService,
          repository_factory: Callable[[AsyncSession], AgentDesignRepository],
          default_tool_groups_provider: Callable[[], tuple[str, ...]],
          stale_after: timedelta,
          generation_control: AgentDesignGenerationControl,
          clock: Callable[[], datetime],
      ) -> None:
          self._session_factory = session_factory
          self._generator = generator
          self._repository_factory = repository_factory
          self._default_tool_groups_provider = default_tool_groups_provider
          self._stale_after = stale_after
          self._generation_control = generation_control
          self._clock = clock
  ```

  Do not create a Protocol, mixin, base class, package, or Worker sink.

  Import the exact codec functions used by generation directly from `agent_design_codec`: `_blueprint_from_json`, `_blueprint_json`, `_clarification_answers`, `_clarification_history`, `_clarification_json`, `_clarification_request`, `_clarifications_from_json`, `_message_json`, `_progress_json`, `_session_view`, `_stable_generation_error_message`, and `blueprint_checksum`. Import only `_candidate_blueprint` from `agent_design_validation`. Keep profile resolution in `agent_design_profile`, frozen model material in `system_settings.execution_payload`, and model lookup in `system_settings.repository`; never reach them through `AgentDesignService`.

  The moved generation code also owns `_PUBLIC_ERROR_PATTERN` and `_GENERATION_STOP_POLL_SECONDS`. Move `_constraint_name` and `_CONFLICT_CONSTRAINTS` to this lower owner and explicitly import them back into Service so generation and non-generation database error mapping keep one implementation. Keep `_DEFAULT_STALE_GENERATING_SECONDS`, `DEFAULT_AGENT_MODEL_REF`, and `DEFAULT_AGENT_TOOL_GROUPS` in Service for its unchanged constructor/export surface; the collaborator uses `DEFAULT_MODEL_REF` directly, the injected default-tool provider, `AgentModelSettings`, and `AgentRepository`.

  Move `_reset_operation` as one stateless module-level helper because generation preparation and Service Cancel both use it. Import it in Service as `_reset_operation_impl` and bind it back as an exact `staticmethod`; keep `_new_operation` in Service.

  Keep the collaborator surface limited to these concrete methods:

  - `prepare_generation_in_transaction(session, repository, context, row, operation, command) -> tuple[int, AgentDesignGenerationRequest, AgentDesignGenerationContext, UUID, AgentDesignGenerationProfile | None]`; its precondition is that `command.input` is not `AgentDesignBlueprintTurn`.
  - `run_prepared_turn(context, session_id, *, operation_hash, generation_revision, request, generation_context, operation_id, generation_profile, requested_model_ref, started_at, activity_callback) -> AgentDesignSessionView`; the callback is `Callable[[str, int | None, dict[str, object]], Awaitable[None]]`.
  - `stop_turn(context, session_id, *, refresh) -> AgentDesignSessionView`, where `refresh` has the same async `(ProjectContext, UUID) -> AgentDesignSessionView` contract as `AgentDesignService.get`.
  - `request_stop_and_wait(context, session_id, operation_id) -> None`, used only by Service Cancel.
  - `resolve_generation_profile_values(session, context, *, requested_model_ref, requested_mode, thinking_enabled, reasoning_effort) -> AgentDesignGenerationProfile`.
  - `recover_stale_generating(repository, context, row, *, now, active_operations) -> bool` and `is_stale_generating(row, *, now) -> bool`; recovery runs inside the caller transaction and never commits.

  Finish/failure/stopped settlement, stop polling, and generation completion wait remain private methods of this same collaborator.

- [ ] **Step 3: compose it without changing the Service constructor**

  Keep the current constructor signature, stale-value validation, default creation order, and existing private attributes. After those assignments, construct:

  ```python
  self._generation_lifecycle = AgentDesignGenerationLifecycle(
      self._session_factory,
      generator=self._generator,
      repository_factory=self._repository_factory,
      default_tool_groups_provider=self._default_tool_groups_provider,
      stale_after=self._stale_after,
      generation_control=self._generation_control,
      clock=lambda: self._now(),
  )
  ```

  The dynamic clock forwarding is required: replacing `service._now` after construction must still affect generation paths. Inside the collaborator, replace moved `self._now()` calls with `self._clock()`; Service create/list/get continue computing their current single `now` value before calling stale helpers.

- [ ] **Step 4: retain Service admission and move only the generation tail**

  Keep `submit_turn()` validation, capability checks, UUID/idempotency/checksum, `_prepare_turn()`, `TURN_ACCEPTED`, and the Activity callback in Service. Delegate control registration, durable stop check, monitor task, model call, finish/failure/stop settlement, and `finally` cleanup to:

  ```python
  return await self._generation_lifecycle.run_prepared_turn(
      context,
      session_id,
      operation_hash=operation_hash,
      generation_revision=generation_revision,
      request=request,
      generation_context=generation_context,
      operation_id=operation_id,
      generation_profile=generation_profile,
      requested_model_ref=command.generation_model_ref,
      started_at=started_at,
      activity_callback=record_generation_activity,
  )
  ```

- [ ] **Step 5: keep generic `_prepare_turn` fencing and manual Blueprint ownership**

  Keep current lines 1323-1455 in Service. Replace only the generation branch at current lines 1457-1533 with:

  ```python
  return await self._generation_lifecycle.prepare_generation_in_transaction(
      session,
      repository,
      context,
      row,
      operation,
      command,
  )
  ```

  This collaborator method receives the already-open session/transaction and must not call `begin()`, `commit()`, or construct another session.

- [ ] **Step 6: delegate stop, preference support, cancel wait, and stale recovery**

  Keep public validation on `stop_turn()` and delegate after validation with:

  ```python
  return await self._generation_lifecycle.stop_turn(
      context,
      session_id,
      refresh=self.get,
  )
  ```

  Replace internal calls with `resolve_generation_profile_values`, `request_stop_and_wait`, `recover_stale_generating`, and `is_stale_generating` on the collaborator. Preserve the two stop branches (durable polling when no control entry exists; wait-then-`get()` when it does), Cancel's two transaction phases, and editor-only stale recovery.

- [ ] **Step 7: preserve tested private seams without an implicit mixin contract**

  Stateless helpers may keep exact descriptor identity. Implement them against direct codec functions, then bind:

  ```python
  _generation_request = classmethod(
      AgentDesignGenerationLifecycle.__dict__["_generation_request"].__func__
  )
  _generation_profile_is_valid = staticmethod(
      AgentDesignGenerationLifecycle.__dict__["_generation_profile_is_valid"].__func__
  )
  _first_user_message = staticmethod(
      AgentDesignGenerationLifecycle.__dict__["_first_user_message"].__func__
  )
  _require_matching_clarification_response = staticmethod(
      AgentDesignGenerationLifecycle.__dict__[
          "_require_matching_clarification_response"
      ].__func__
  )
  _reset_operation = staticmethod(_reset_operation_impl)
  ```

  Instance-state helpers must remain thin Service delegates so they execute against the composed lifecycle's `_clock` and default-tool provider rather than assuming hidden attributes on `AgentDesignService`:

  ```python
  def _append_turn_input(
      self,
      context: ProjectContext,
      row: AgentDesignSessionRow,
      turn: AgentDesignMessageTurn | AgentDesignClarificationTurn,
      *,
      operation_id: uuid.UUID | None = None,
  ) -> bool:
      return self._generation_lifecycle._append_turn_input(
          context,
          row,
          turn,
          operation_id=operation_id,
      )

  def _default_blueprint(self, description: str) -> AgentDesignBlueprint:
      return self._generation_lifecycle._default_blueprint(description)

  async def _default_blueprint_with_system_dependencies(
      self,
      session: AsyncSession,
      context: ProjectContext,
      description: str,
  ) -> AgentDesignBlueprint:
      return await self._generation_lifecycle._default_blueprint_with_system_dependencies(
          session,
          context,
          description,
      )
  ```

  Characterize exact descriptor identity for the four stateless helpers and `_reset_operation`; characterize behavior and signatures, not identity, for the three delegates. `_prepare_turn` remains a Service method. Do not add the collaborator to `__all__`.

- [ ] **Step 8: migrate the one module-global monkeypatch**

  Change the `SystemModelRepository` target in `test_agent_builder_model_selection.py` to `app.shared_assets.agent_design_generation_lifecycle.SystemModelRepository`. Keep other tests importing `AgentDesignService` so they exercise the compatibility surface.

- [ ] **Step 9: run Agent Builder focused and PostgreSQL gates**

  ```bash
  cd backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_python_module_decomposition_contract.py \
    tests/test_agent_builder_contract_version.py \
    tests/test_agent_builder_interview_flow.py \
    tests/test_agent_builder_model_selection.py \
    tests/test_agent_builder_allowed_assets.py \
    tests/test_agent_builder_safety_lifecycle.py \
    tests/test_agent_builder_generation_profile.py \
    tests/test_agent_builder_default_capabilities.py -q

  PYTHONPATH=. uv run python tests/support/core_gate_plugin.py \
    tests/test_agent_builder_clarification_postgres.py \
    tests/test_agent_builder_activity_postgres.py \
    -q -m "not provider_integration"
  ```

  Require PASS and zero PostgreSQL skips. Record the actual count instead of hardcoding it because Task 1 adds characterization tests.

- [ ] **Step 10: static checkpoint**

  Compare all moved method ASTs to baseline after normalizing only owner qualification and explicit delegation. Then run:

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    app/shared_assets/agent_design_generation_lifecycle.py \
    app/shared_assets/agent_design_service.py \
    tests/test_agent_builder_model_selection.py \
    tests/test_python_module_decomposition_process_boundaries.py
  uvx ruff format --check \
    app/shared_assets/agent_design_generation_lifecycle.py \
    app/shared_assets/agent_design_service.py \
    tests/test_agent_builder_model_selection.py \
    tests/test_python_module_decomposition_process_boundaries.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit with message `refactor(agent-builder): extract generation lifecycle`.

## Task 12: Document ownership and run complete Batch 3 verification

**Files:**

- Modify: `backend/AGENTS.md`
- Verify: every file changed in Tasks 1-11

**Interfaces:**

- Consumes: all stable Batch 3 owners and compatibility contracts.
- Produces: documented maintenance ownership and current full verification evidence.

- [ ] **Step 1: add one focused ownership bullet under `## Where changes live`**

  Add exactly this maintenance rule, formatted to the existing guide width:

  > Skill Builder Gateway lifecycle is owned by `app/shared_assets/skill_design_lifecycle.py`; its Run-bound Worker draft and terminal operations are owned by `skill_builder_draft_sink.py`; `skill_design_service.py` is a compatibility façade. Execution Approval policy, codec, Worker port, recovery, and Gateway service are owned by their `execution_approval_*.py` modules while `execution_approval_lifecycle.py` retains shared transactional convergence and `execution_approval.py` is a compatibility façade. Agent Design model-turn execution, generated-default preparation, stop coordination, and stale recovery are owned by `agent_design_generation_lifecycle.py`; `AgentDesignService` retains session admission, manual Blueprint updates, Commit, and Cancel ownership.

  Do not add a README paragraph or task changelog.

- [ ] **Step 2: run the combined focused offline suite once**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_process_boundaries.py \
    tests/test_python_module_decomposition_contract.py \
    tests/test_skill_builder_admission_boundary.py \
    tests/test_skill_builder_design_record.py \
    tests/test_skill_builder_draft_projection.py \
    tests/test_skill_builder_durable_boundaries.py \
    tests/test_skill_builder_execution_options.py \
    tests/test_skill_builder_revision_contract.py \
    tests/test_skill_builder_session_summary.py \
    tests/test_skill_builder_terminal_recovery.py \
    tests/test_execution_approval_api.py \
    tests/test_execution_approval_codec.py \
    tests/test_execution_approval_skill_source_closure.py \
    tests/test_worker_execution_approval_composition.py \
    tests/test_host_execution_approval.py \
    tests/test_host_execution_continuation_runner.py \
    tests/test_run_worker_host_execution_pause.py \
    tests/test_agent_builder_contract_version.py \
    tests/test_agent_builder_interview_flow.py \
    tests/test_agent_builder_model_selection.py \
    tests/test_agent_builder_allowed_assets.py \
    tests/test_agent_builder_safety_lifecycle.py \
    tests/test_agent_builder_generation_profile.py \
    tests/test_agent_builder_default_capabilities.py \
    -q -m "not postgres and not provider_integration"
  ```

  Record exact count, duration, skips, deselections, and warning categories.

- [ ] **Step 3: run the selected PostgreSQL atomicity/race/rollback gate**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run python \
    tests/support/core_gate_plugin.py \
    tests/test_skill_builder_durable_agent_postgres.py \
    tests/test_skill_builder_revision_postgres.py \
    tests/test_skill_builder_private_run_api.py \
    tests/test_execution_approval_lifecycle_postgres.py \
    tests/test_execution_approval_private_lifecycle_postgres.py \
    tests/test_execution_approval_output_delivery_e2e_postgres.py \
    tests/test_agent_builder_clarification_postgres.py \
    tests/test_agent_builder_activity_postgres.py \
    -q -m "not provider_integration"
  ```

  Require zero failures and zero skips against disposable `deerflow_test_*` databases.

- [ ] **Step 4: run fresh import and dependency smoke**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run python scripts/run_runtime.py -- python -c '
  from app.private_work import execution_approval as approval_facade
  from app.private_work import execution_approval_policy, execution_approval_recovery
  from app.private_work import execution_approval_service, execution_approval_worker
  from app.shared_assets import skill_design_lifecycle, skill_design_service
  from app.shared_assets.agent_design_generation_lifecycle import AgentDesignGenerationLifecycle, _reset_operation
  from app.shared_assets.agent_design_service import AgentDesignService
  assert skill_design_service.SkillDesignService is skill_design_lifecycle.SkillDesignService
  assert approval_facade.ExecutionApprovalService is execution_approval_service.ExecutionApprovalService
  assert approval_facade.WorkerHostExecutionApprovalPort is execution_approval_worker.WorkerHostExecutionApprovalPort
  assert approval_facade.HostExecutionProviderPolicySnapshot is execution_approval_policy.HostExecutionProviderPolicySnapshot
  assert approval_facade.recover_staged_execution_approval_id is execution_approval_recovery.recover_staged_execution_approval_id
  assert AgentDesignService.__dict__["_generation_request"].__func__ is AgentDesignGenerationLifecycle.__dict__["_generation_request"].__func__
  assert AgentDesignService.__dict__["_reset_operation"].__func__ is _reset_operation
  '
  ```

  Expected: exit 0 without starting Gateway lifespan services.

- [ ] **Step 5: run repository-required backend gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  make format
  make lint
  make detect-blocking-io
  uv run --env-file ../.env make test
  ```

  Inspect `git status --short` immediately after `make format`; if it touches a file outside the Batch 3 set, stop and separate that pre-existing formatting drift rather than silently including it. Require all commands to exit 0 and the core test summary to report `failed=0 skipped=0`. Do not use a production database, real Provider, or unapproved external environment.

- [ ] **Step 6: perform final structural and scope review**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations
  git diff --check
  git status --short
  git diff --stat e35bf71549f14a30325c0301b0251e9cc43c397a
  ! rg -n '^[[:space:]]*(from|import).*skill_design_service([[:space:].]|$)' backend/app --glob '*.py'
  ! rg -n '^[[:space:]]*(from|import).*execution_approval([[:space:].]|$)' backend/app --glob '*.py'
  ! rg -n 'agent_design_(service|generation_lifecycle)' \
    backend/app/worker backend/app/reliability/run_execution backend/packages/harness \
    --glob '*.py'
  ```

  Both legacy-import scans must return zero matches outside their exact façade paths; because a façade does not import itself, an empty result is expected. The Worker scan must also have no matches. Review every untracked production/test file shown by status in addition to the tracked diff, and confirm no schema, Knowledge, Sandbox, frontend, deployment, or unrelated test refactor is present.

- [ ] **Step 7: request one final independent review**

  Use `superpowers:requesting-code-review` over the exact Batch 3 base-to-current-checkout range. If commits were authorized, review `e35bf71549f14a30325c0301b0251e9cc43c397a..HEAD`; otherwise review the tracked working diff plus every untracked Batch 3 file listed by `git status --short`. Require the reviewer to inspect transaction counts/locks, Worker/Gateway boundaries, façade identities/import order, monkeypatch owners, Skill Sink lease checks, Approval decision phases, Agent stop/stale recovery, tests, and validation evidence. Resolve every finding before handoff.

- [ ] **Step 8: documentation checkpoint**

  If local commits are authorized, commit only `backend/AGENTS.md` with message `docs(backend): document process owners`. Otherwise leave it unstaged and report its exact diff.

## Completion Criteria

- Worker constructs `SkillDesignDraftSink` directly; `SkillDesignService.terminal_sink()` remains compatible and returns the same implementation.
- Skill draft Tool/terminal operations retain exact ProjectContext, capability, Run/Job/Attempt lease, CAS, dependency, Activity, and transaction behavior.
- `skill_design_lifecycle.SkillDesignService` is the exact legacy class object; `skill_design_service.py` is an imports-only façade with the unchanged 22-name export digest.
- Execution Approval policy, codec, recovery, Worker port, and Gateway service have one owner each; existing lifecycle/audit owners remain separate.
- `execution_approval.py` is an imports-only façade with the unchanged six-name export digest and exact private compatibility seams.
- `WorkerHostExecutionApprovalPort` remains one cohesive lease-bound class; `_stage`, claim, spawn authorization, completion, output delivery, and receipt settlement are not split.
- `ExecutionApprovalService.decide()` retains decision transaction, external admission, and verification transaction in the same order.
- Internal Approval consumers import owner modules; no owner imports the façade.
- `AgentDesignGenerationLifecycle` owns generated-default preparation, generation execution, stop coordination, atomic terminal settlement, and stale recovery; `AgentDesignService` retains request admission, manual Blueprint updates, Commit, and Cancel responsibilities.
- Agent Builder remains Gateway-only and Worker has no Agent Design dependency.
- No HTTP, DTO, error, Activity, secret, Tool, model prompt, transaction, lock, lease, stream, process, schema, or frontend behavior changes.
- Tests move only with corresponding production seams and add focused characterization; no whole-repository test reorganization occurs.
- Focused, selected PostgreSQL, import/dependency, Ruff, blocking-I/O, and full backend zero-skip gates pass on the current checkout.

## Execution Handoff

Plan approval does not itself start implementation. After approval, choose one:

1. **Subagent-Driven (recommended):** execute Tasks 1-12 sequentially in this session, with a fresh implementation agent and independent review/fix loop per task.
2. **Inline Execution:** execute the same tasks with `superpowers:executing-plans` and the same checkpoints.

Do not create another branch or worktree unless the user explicitly changes the existing-worktree requirement.
