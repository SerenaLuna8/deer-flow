# Sandbox Tool Module Decomposition (Batch 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 2,780-line Sandbox Tool module into explicit path, Bash-policy, runtime, Host Execution, file, search, and Bash owners while preserving every Tool object, security boundary, error, schema, side-effect order, and legacy configuration path.

**Architecture:** Move existing contiguous responsibilities into seven leaf modules under `deerflow.sandbox.tooling`, in dependency order, and retain `deerflow.sandbox.tools` as a long-lived imports-only compatibility façade. Internal Harness consumers use the owning modules, while published `deerflow.sandbox.tools:<tool>` configuration strings continue resolving the exact same seven LangChain Tool objects.

**Tech Stack:** Python 3.12+, LangChain Tools, LangGraph Runtime/Command, synchronous and asynchronous Sandbox providers, Host Execution Approval ports, pytest, Ruff, and repository Make targets.

**Spec:** `docs/superpowers/specs/2026-09-02-python-module-decomposition-design.md`, sections 1-6, 11, and 14-18.

## Global Constraints

### Confirmed execution baseline

- Generate and execute this plan only in `/Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations` on branch `codex/python-module-decomposition-foundations`. This plan does not authorize another branch or worktree.
- The audited Batch 4 production baseline is commit `eb3dd904df46dd08534fd9aff1a23cd93c72a33e`. Require that commit or an explicitly reviewed descendant before implementation. If `sandbox/tools.py` or `runtime/host_execution_runner.py` changes, stop and re-audit symbol ownership, tests, and coordinates before moving code.
- `backend/packages/harness/deerflow/sandbox/tools.py` remains exactly 2,780 lines, with 100 top-level functions, one top-level class, and seven `@tool` definitions. It and `backend/packages/harness/deerflow/runtime/host_execution_runner.py` are byte-unchanged from design base `0d421ede2d52a2cf22a5c8fedfdfbb10e6e1394c`; no `sandbox/tooling/` directory currently exists.
- The current worktree already contains one accepted-but-uncommitted Batch 3 test change at `backend/tests/test_skill_builder_durable_agent_postgres.py` and four user-owned untracked design/plan documents. Preserve all five items, never sweep the P1 test into a Batch 4 commit, and treat this plan as one additional user-owned untracked document until staging is separately authorized.
- Root `.env` and `config.yaml` are present, ignored, and byte-for-byte match the saved source checkout as of this audit. Never print their values. Use `uv run --env-file ../.env` for the final database-backed gate unless the invoking shell already exports the same approved non-production environment.
- The current focused Sandbox/Host Execution baseline is `290 passed in 9.90s`; the selected PostgreSQL lease/approval/output-delivery baseline is `3 passed, 0 skipped in 3.03s`. These are plan-generation baselines, not completion evidence. Every implementation task requires fresh results.
- The full backend gate takes about 16 minutes. Tasks 1-8 use focused gates; Task 9 runs the full backend gate once and repeats it only if a later fix invalidates that evidence.

### Scope boundary

- Production code first. Tests may move imports and monkeypatch targets only with their corresponding production owner, and may add focused compatibility or characterization coverage. Do not split `test_sandbox_tools_security.py`, reorganize test directories, or refactor the test framework.
- Batch 4 production scope is limited to:
  - `backend/packages/harness/deerflow/sandbox/tools.py`;
  - new `backend/packages/harness/deerflow/sandbox/tooling/{path_mapping,bash_policy,runtime,host_execution,files,search_tools,bash}.py` owners and a non-eager package marker;
  - `backend/packages/harness/deerflow/runtime/host_execution_runner.py`;
  - the seven audited internal production consumers of `deerflow.sandbox.tools`;
  - the ownership comment in `sandbox/middleware.py`;
  - focused tests and `backend/AGENTS.md`.
- `sandbox/security.py` remains the only Host Bash provider/mode policy authority. Do not split or duplicate it.
- Do not modify `local_sandbox.py`, `sandbox_provider.py`, `runtime/runs/worker.py`, `app/reliability/run_execution/executor.py`, Approval persistence, Schema/DDL, frontend behavior, deployment, or Batch 5 targets.
- Do not change business rules, Tool names/descriptions/call schemas, arguments/defaults, return types, errors, truncation markers, output ordering, authorization operations, secret handling, path behavior, file-lock scope, process responsibility, or configuration semantics.
- The published strings in `config.example.yaml`, `scripts/wizard/writer.py`, frontend documentation, test replay fixtures, and `deerflow.tools.tools._is_host_bash_tool()` remain `deerflow.sandbox.tools:<tool>`. They prove the façade is still the supported configuration surface and must not be migrated.
- The legacy module currently has no `__all__`. Do not introduce a newly declared façade API during a structural move. Preserve all audited functions, constants, private seams, and Tool objects; incidental imported module/type names are not a compatibility contract. New owner modules may use narrow `__all__` lists; `tooling/__init__.py` must not eagerly aggregate them.
- A compatibility façade re-exports exact objects. It must not wrap functions, subclass objects, copy implementation, decorate a second Tool, or emit deprecation warnings.
- New owners must never import `deerflow.sandbox.tools`; Harness must never import `app.*` or SQLAlchemy from `sandbox/tooling/`.
- Use `apply_patch` for edits and preserve unrelated changes. No task authorizes destructive reset/checkout, staging, commit, push, merge, or publication. Suggested commits are conditional on explicit local-commit authorization and must add only the listed Batch 4 paths.
- Stop the current task on any Tool identity/schema drift, changed error or masking result, import cycle, new blocking-I/O finding, unexpected production consumer, or unexplained test failure. Revert the complete task with a reviewable patch or discard only its isolated uncommitted paths; never hide a structural regression with a behavior fix.
- If moving code reveals a real behavior defect, record it and request a separate design. Batch 4 must not repair it while changing ownership.

### Audited design refinements

1. Add an empty or docstring-only `tooling/__init__.py` because this is a stable core package, but do not re-export owner modules there; eager aggregation would recreate import-order risk.
2. `path_mapping.py` owns Skill/ACP/custom-mount discovery, virtual-to-host mapping, Sub-Agent output mapping, local path authorization, and path masking. It does not own Local Bash token parsing.
3. `bash_policy.py` is a separate second extraction step. It owns Local Bash command tokenization, path validation, virtual command rewriting, and workspace `cwd` prefixing; it consumes `path_mapping` and never replaces `sandbox.security`.
4. `runtime.py` owns Sandbox lookup/acquisition, sync/async initialization, async-to-thread Tool dispatch, authorization-before-side-effect order, runtime context overlay, directory initialization, and sanitized runtime errors.
5. `host_execution.py` owns channel identity, environment/Secret projection, Secret-value redaction, Bash-result truncation, exact Approval Plan creation, and approval staging. Keep `_prepare_local_host_execution` and `_truncate_bash_output` as the implementation names, then expose same-object public aliases `prepare_local_host_execution` and `truncate_bash_output` for `host_execution_runner.py`.
6. `files.py` owns `ls`, `read_file`, `write_file`, `str_replace`, the shared current-content reader, file-specific bounds/errors, and the existing file-operation lock scope. `search_tools.py` owns only `glob` and `grep` plus their result limits/formatting.
7. `bash.py` is last because it composes every earlier owner. It alone decorates and binds `bash_tool`; it does not absorb Host Execution planning or security policy.
8. Re-export aliases preserve import and object identity but cannot propagate monkeypatch assignment into another module's globals. Move each repository test patch to the actual call-site owner in the same task; do not add wrappers to preserve façade monkeypatching.
9. `ReadBeforeWriteMiddleware` keeps its wider authorization lock and only imports `files.read_current_file_content`; the Tool-internal file lock remains in `files.py` around the actual write or read-modify-write.
10. Batch 4 contains no database transaction owner. Approval stage/claim/final-spawn/completion and execution-boundary transactions remain in the existing App owners; `sandbox/tooling/**` must contain no SQLAlchemy session, `begin`, `commit`, or repository code.

### Final ownership layout

```text
backend/packages/harness/deerflow/sandbox/
├── security.py                         # unchanged Host Bash policy authority
├── tools.py                            # long-lived imports-only compatibility façade
└── tooling/
    ├── __init__.py                     # package marker only; no eager re-exports
    ├── path_mapping.py                 # virtual/host/delegated paths and masking
    ├── bash_policy.py                  # Local Bash command/path validation and rewrite
    ├── runtime.py                      # Sandbox resolution/init and async dispatch
    ├── host_execution.py               # identity, Secrets, Approval Plan, result redaction
    ├── files.py                        # ls/read/write/str_replace and file locks
    ├── search_tools.py                 # glob/grep limits, formatting, and Tools
    └── bash.py                         # one decorated Bash Tool

backend/packages/harness/deerflow/runtime/
└── host_execution_runner.py            # consumes public tooling owner APIs
```

Dependency direction:

```text
path_mapping
├── bash_policy
└── runtime

path_mapping + runtime
├── files
└── search_tools

bash_policy + runtime
└── host_execution

path_mapping + bash_policy + runtime + host_execution
└── bash

tools façade ──> all seven owners
host_execution_runner ──> host_execution + path_mapping
```

No arrow may point from an owner back to `deerflow.sandbox.tools`.

### Frozen compatibility surfaces

- The seven legacy reflection paths remain loadable: `deerflow.sandbox.tools:bash_tool`, `ls_tool`, `glob_tool`, `grep_tool`, `read_file_tool`, `write_file_tool`, and `str_replace_tool`.
- Legacy and owner paths must expose the same Tool, `.func`, and `.coroutine` objects. Exactly seven `@tool` decorators exist across `sandbox/tooling/**`; none remains in the façade.
- Canonical Tool-call-schema SHA-256 values, using sorted compact JSON with `ensure_ascii=False`, are:

  ```python
  EXPECTED_TOOL_SCHEMA_DIGESTS = {
      "bash_tool": "b07e70b944148a375b93af1750b64a8fae16c00d3299bfdc345518f75258e666",
      "ls_tool": "55c2718f133879b103d0387c00161c8a978a7b8ec269409c06dd6a1ce199a4a1",
      "glob_tool": "485f0916c26adf27eb0188e73a3524fff1d9149d76cdc2fc4fa2cbb199244f6c",
      "grep_tool": "d05bada5d19417c6e19e9ef025ff7e30ebc47f7d369d3c9fb3f6bc179bcd516e",
      "read_file_tool": "9b816fddbf8f58765692cd4e2e36ce7d3cbbdca75b3961aad08f0321c57efcd7",
      "write_file_tool": "a7040794be5144ae7d16e2437b1b5f89b551e288c9bc6780b7f85611c16f147a",
      "str_replace_tool": "3103e6f0c11943ed38de597478648bd9f809a5b41a1d70fb5ed1f00c0641fdbd",
  }
  ```

- Tool `.func` and `.coroutine` parameter names remain:

  ```python
  EXPECTED_TOOL_PARAMETERS = {
      "bash_tool": ("runtime", "description", "command"),
      "ls_tool": ("runtime", "description", "path"),
      "glob_tool": ("runtime", "description", "pattern", "path", "include_dirs", "max_results"),
      "grep_tool": ("runtime", "description", "pattern", "path", "glob", "literal", "case_sensitive", "max_results"),
      "read_file_tool": ("runtime", "description", "path", "start_line", "end_line"),
      "write_file_tool": ("runtime", "description", "path", "content", "append"),
      "str_replace_tool": ("runtime", "description", "path", "old_str", "new_str", "replace_all"),
  }
  ```

- Preserve the same function objects, including their attached caches, for `_get_skills_container_path`, `_get_custom_mounts`, `_get_acp_workspace_host_path`, and `_compiled_mask_patterns`.
- Preserve the async dispatch sequence: async Sandbox initialization, one `check_authorization_boundary()` call when requested, then one `asyncio.to_thread()` Tool invocation. Do not materialize private Skill Secrets before executor-queue admission.
- Preserve Local Bash result order: host-path masking, Secret-value masking, then middle truncation. Preserve every `finally` block that clears request-scoped Secret dictionaries.
- Preserve `get_file_operation_lock()` around one write and around the complete `str_replace` read-modify-write.
- The current seven direct production import consumers are frozen in Task 1 and must reach zero by Task 8; literal/configuration references remain intentionally unchanged.

---

## Task 1: Freeze Batch 4 Tool schemas, reflection paths, and consumer inventory

**Files:**

- Create: `backend/tests/test_python_module_decomposition_sandbox_tools.py`
- Verify: `backend/packages/harness/deerflow/sandbox/tools.py`
- Verify: the seven current internal production consumers

**Interfaces:**

- Consumes: the current `deerflow.sandbox.tools` module and `deerflow.reflection.resolve_variable()`.
- Produces: schema, signature, reflection-path, consumer-inventory, owner-identity, import-direction, and façade-shape helpers used by Tasks 2-8.

- [ ] **Step 1: verify the exact branch, production baseline, preserved changes, and local configuration**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations
  git branch --show-current
  git rev-parse HEAD
  git status --short
  git diff --quiet 0d421ede2d52a2cf22a5c8fedfdfbb10e6e1394c -- \
    backend/packages/harness/deerflow/sandbox/tools.py \
    backend/packages/harness/deerflow/runtime/host_execution_runner.py
  git check-ignore -v .env config.yaml
  cmp -s /Users/jiangfeng/workspace/deer-flow/.env .env
  cmp -s /Users/jiangfeng/workspace/deer-flow/config.yaml config.yaml
  ```

  Require the existing branch, audited commit or reviewed descendant, no Batch 4 production drift, ignored matching configuration, the four existing untracked documents, and the separate modified P1 test. Stop if any unexplained path appears.

- [ ] **Step 2: add exact Tool schema and legacy reflection characterization**

  Create the test module with these helpers and assertions:

  ```python
  from __future__ import annotations

  import ast
  import hashlib
  import inspect
  import json
  from pathlib import Path

  from langchain.tools import BaseTool

  from deerflow.reflection import resolve_variable
  from deerflow.sandbox import tools as legacy

  BACKEND_ROOT = Path(__file__).resolve().parents[1]
  HARNESS_ROOT = BACKEND_ROOT / "packages" / "harness"
  LEGACY_MODULE = "deerflow.sandbox.tools"

  EXPECTED_TOOL_SCHEMA_DIGESTS = {
      "bash_tool": "b07e70b944148a375b93af1750b64a8fae16c00d3299bfdc345518f75258e666",
      "ls_tool": "55c2718f133879b103d0387c00161c8a978a7b8ec269409c06dd6a1ce199a4a1",
      "glob_tool": "485f0916c26adf27eb0188e73a3524fff1d9149d76cdc2fc4fa2cbb199244f6c",
      "grep_tool": "d05bada5d19417c6e19e9ef025ff7e30ebc47f7d369d3c9fb3f6bc179bcd516e",
      "read_file_tool": "9b816fddbf8f58765692cd4e2e36ce7d3cbbdca75b3961aad08f0321c57efcd7",
      "write_file_tool": "a7040794be5144ae7d16e2437b1b5f89b551e288c9bc6780b7f85611c16f147a",
      "str_replace_tool": "3103e6f0c11943ed38de597478648bd9f809a5b41a1d70fb5ed1f00c0641fdbd",
  }

  EXPECTED_TOOL_PARAMETERS = {
      "bash_tool": ("runtime", "description", "command"),
      "ls_tool": ("runtime", "description", "path"),
      "glob_tool": ("runtime", "description", "pattern", "path", "include_dirs", "max_results"),
      "grep_tool": ("runtime", "description", "pattern", "path", "glob", "literal", "case_sensitive", "max_results"),
      "read_file_tool": ("runtime", "description", "path", "start_line", "end_line"),
      "write_file_tool": ("runtime", "description", "path", "content", "append"),
      "str_replace_tool": ("runtime", "description", "path", "old_str", "new_str", "replace_all"),
  }


  def _schema_digest(tool: BaseTool) -> str:
      payload = json.dumps(
          tool.tool_call_schema.model_json_schema(),
          ensure_ascii=False,
          sort_keys=True,
          separators=(",", ":"),
      ).encode()
      return hashlib.sha256(payload).hexdigest()


  def test_batch4_tool_call_schemas_and_signatures_are_frozen() -> None:
      for attribute, expected_digest in EXPECTED_TOOL_SCHEMA_DIGESTS.items():
          tool = getattr(legacy, attribute)
          expected_parameters = EXPECTED_TOOL_PARAMETERS[attribute]
          assert _schema_digest(tool) == expected_digest
          assert tuple(inspect.signature(tool.func).parameters) == expected_parameters
          assert tuple(inspect.signature(tool.coroutine).parameters) == expected_parameters


  def test_batch4_legacy_tool_references_resolve_exact_objects() -> None:
      for attribute in EXPECTED_TOOL_SCHEMA_DIGESTS:
          resolved = resolve_variable(
              f"{LEGACY_MODULE}:{attribute}",
              BaseTool,
          )
          assert resolved is getattr(legacy, attribute)
  ```

- [ ] **Step 3: freeze the seven production import consumers**

  Add this exact scanner and current inventory:

  ```python
  EXPECTED_LEGACY_PRODUCTION_CONSUMERS = frozenset(
      {
          "deerflow/agents/middlewares/read_before_write_middleware.py",
          "deerflow/agents/middlewares/tool_error_handling_middleware.py",
          "deerflow/agents/middlewares/tool_result_sanitization_middleware.py",
          "deerflow/agents/middlewares/view_image_middleware.py",
          "deerflow/runtime/host_execution_runner.py",
          "deerflow/tools/builtins/view_image_tool.py",
          "deerflow/vision/image_input.py",
      }
  )


  def _imports_legacy_tools(path: Path) -> bool:
      tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
      for node in ast.walk(tree):
          if isinstance(node, ast.ImportFrom) and node.module == LEGACY_MODULE:
              return True
          if (
              isinstance(node, ast.ImportFrom)
              and node.module == "deerflow.sandbox"
              and any(alias.name == "tools" for alias in node.names)
          ):
              return True
          if isinstance(node, ast.Import) and any(
              alias.name == LEGACY_MODULE for alias in node.names
          ):
              return True
      return False


  def _legacy_production_consumers() -> frozenset[str]:
      legacy_path = HARNESS_ROOT / "deerflow" / "sandbox" / "tools.py"
      return frozenset(
          path.relative_to(HARNESS_ROOT).as_posix()
          for path in HARNESS_ROOT.rglob("*.py")
          if path != legacy_path and _imports_legacy_tools(path)
      )


  def test_batch4_production_consumer_inventory_is_frozen() -> None:
      assert _legacy_production_consumers() == EXPECTED_LEGACY_PRODUCTION_CONSUMERS
  ```

  Keep the expected set current after every owner migration. Task 8 changes it to `frozenset()`; literal strings are intentionally outside this AST import scan.

- [ ] **Step 4: run the new characterization on the untouched production baseline**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_python_module_decomposition_contract.py::test_sandbox_tools_keep_their_public_shapes \
    -q
  ```

  Expected: PASS before any production movement. These are characterization tests, so RED is neither expected nor useful at this step.

- [ ] **Step 5: run and record the complete focused baseline**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_sandbox_tool_initialization_errors.py \
    tests/test_host_execution_provider_classification.py \
    tests/test_host_execution_approval.py \
    tests/test_host_execution_continuation_runner.py \
    tests/test_current_upload_vision.py \
    -q
  ```

  Require zero failures and record the new exact count, duration, deselections, skips, and warnings. The audited pre-characterization count for this same set without the new file is 290.

- [ ] **Step 6: checkpoint the characterization only**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check tests/test_python_module_decomposition_sandbox_tools.py
  uvx ruff format --check tests/test_python_module_decomposition_sandbox_tools.py
  cd ..
  git diff --check
  git status --short
  ```

  If explicit local commits are authorized, commit only the new test with message `test(sandbox): freeze batch 4 contracts`.

## Task 2: Extract virtual, delegated, and host-path mapping

**Files:**

- Create: `backend/packages/harness/deerflow/sandbox/tooling/__init__.py`
- Create: `backend/packages/harness/deerflow/sandbox/tooling/path_mapping.py`
- Modify: `backend/packages/harness/deerflow/sandbox/tools.py:91-92,146-421,461-470,501-515,568-920,1125-1127`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/view_image_middleware.py:298-305`
- Modify: `backend/packages/harness/deerflow/tools/builtins/view_image_tool.py:90-93`
- Modify: `backend/packages/harness/deerflow/vision/image_input.py:142-150`
- Modify: `backend/tests/test_python_module_decomposition_sandbox_tools.py`
- Modify: `backend/tests/test_sandbox_tools_security.py:15-42,338-733,1264-1669`

**Interfaces:**

- Consumes: `ThreadDataState`, `Runtime`, `RunScopedReadOnlyMount`, `require_private_file_authority()`, `VIRTUAL_PATH_PREFIX`, config path providers, and `build_output_mask_pattern()`.
- Produces: `replace_virtual_path()`, `delegated_output_root()`, `resolve_delegated_tool_path()`, `mask_local_paths_in_output()`, `validate_local_tool_path()`, `resolve_and_validate_user_data_path()`, and the frozen private compatibility helpers.

- [ ] **Step 1: add the failing path-owner identity and import-direction test**

  Add `import importlib`, then add:

  ```python
  def test_path_mapping_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module(
          "deerflow.sandbox.tooling.path_mapping",
      )
      names = (
          "_get_skills_container_path",
          "_get_skills_host_path",
          "_is_skills_path",
          "_extract_skill_name_from_skills_path",
          "_is_disabled_skill_path",
          "_is_trusted_run_scoped_skill_path",
          "_resolve_skills_path",
          "_is_acp_workspace_path",
          "_get_custom_mounts",
          "_is_custom_mount_path",
          "_get_custom_mount_for_path",
          "_extract_thread_id_from_thread_data",
          "_get_acp_workspace_host_path",
          "_resolve_acp_workspace_path",
          "_resolve_local_read_path",
          "_path_variants",
          "_path_separator_for_style",
          "_join_path_preserving_style",
          "replace_virtual_path",
          "delegated_output_root",
          "resolve_delegated_tool_path",
          "_delegated_result_exposes_hidden_runtime",
          "_thread_virtual_to_actual_mappings",
          "_thread_actual_to_virtual_mappings",
          "_compiled_mask_patterns",
          "mask_local_paths_in_output",
          "_reject_path_traversal",
          "validate_local_tool_path",
          "_validate_resolved_user_data_path",
          "_resolve_and_validate_user_data_path",
          "resolve_and_validate_user_data_path",
      )
      for name in names:
          assert getattr(legacy, name) is getattr(owner, name)
      assert legacy.VIRTUAL_PATH_PREFIX == owner.VIRTUAL_PATH_PREFIX
  ```

  Run only this node and require RED with `ModuleNotFoundError` for `deerflow.sandbox.tooling.path_mapping`.

- [ ] **Step 2: create the package marker and move the exact path implementation**

  `tooling/__init__.py` contains only:

  ```python
  """Owning modules for Sandbox path, runtime, and Tool behavior."""
  ```

  Move the listed constants and definitions without changing branches, messages, cache decorators, lazy imports, or exception handling. Give the owner this narrow public surface:

  ```python
  __all__ = [
      "VIRTUAL_PATH_PREFIX",
      "delegated_output_root",
      "mask_local_paths_in_output",
      "replace_virtual_path",
      "resolve_and_validate_user_data_path",
      "resolve_delegated_tool_path",
      "validate_local_tool_path",
  ]
  ```

  Keep `_resolve_local_read_path()` here because it is path-authorization logic shared by `glob` and `grep`. File reads intentionally use their different Skill/ACP resolution flow; do not deduplicate that behavior into this helper. Define `logger = logging.getLogger(__name__)` in this owner for fail-closed private-file-authority diagnostics. Keep all four cached function objects intact; do not clear or recreate their caches during import.

- [ ] **Step 3: replace moved definitions in the legacy module with exact imports**

  Import every name asserted in Step 1 from `tooling.path_mapping`; do not leave duplicate definitions. The intended pattern is direct aliasing:

  ```python
  from deerflow.sandbox.tooling.path_mapping import (
      VIRTUAL_PATH_PREFIX,
      delegated_output_root,
      mask_local_paths_in_output,
      replace_virtual_path,
      resolve_and_validate_user_data_path,
      resolve_delegated_tool_path,
      validate_local_tool_path,
  )
  ```

  Include the frozen underscore seams in the same explicit import block. Do not use a wrapper or wildcard import.

- [ ] **Step 4: migrate only the corresponding production consumers**

  - `view_image_middleware.py` imports `resolve_delegated_tool_path` from `tooling.path_mapping` at its existing lazy call site.
  - `view_image_tool.py` imports `resolve_delegated_tool_path` from `tooling.path_mapping`; its runtime helper remains on the legacy path until Task 4.
  - `vision/image_input.py` imports `mask_local_paths_in_output` from `tooling.path_mapping`; its Sandbox runtime helpers remain until Task 4.

  Remove these three paths from `EXPECTED_LEGACY_PRODUCTION_CONSUMERS` only when no other legacy import remains in the file. At this task, each still imports at least one runtime name from the legacy module, so the frozen seven-path set does not yet shrink.

- [ ] **Step 5: migrate path characterization imports and monkeypatch targets**

  In `test_sandbox_tools_security.py`, import the path symbols in Step 1 from `tooling.path_mapping`, while continuing to import Tool objects from the legacy façade. Change only these patch families:

  ```text
  deerflow.sandbox.tools._get_skills_container_path
      -> deerflow.sandbox.tooling.path_mapping._get_skills_container_path
  deerflow.sandbox.tools._get_skills_host_path
      -> deerflow.sandbox.tooling.path_mapping._get_skills_host_path
  deerflow.sandbox.tools._get_custom_mounts
      -> deerflow.sandbox.tooling.path_mapping._get_custom_mounts
  deerflow.sandbox.tools._get_acp_workspace_host_path
      -> deerflow.sandbox.tooling.path_mapping._get_acp_workspace_host_path
  ```

  Do not yet migrate Tool call-site patches such as `ensure_sandbox_initialized`; their consuming Tool bodies have not moved.

- [ ] **Step 6: run path and image-consumer gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_current_upload_vision.py \
    -q
  ```

  Require the path owner identity test, cached-mask tests, traversal tests, delegated-output tests, Skill/ACP/custom-mount tests, and image path/redaction tests to pass.

- [ ] **Step 7: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/path_mapping.py \
    packages/harness/deerflow/agents/middlewares/view_image_middleware.py \
    packages/harness/deerflow/tools/builtins/view_image_tool.py \
    packages/harness/deerflow/vision/image_input.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py
  uvx ruff format --check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/path_mapping.py \
    packages/harness/deerflow/agents/middlewares/view_image_middleware.py \
    packages/harness/deerflow/tools/builtins/view_image_tool.py \
    packages/harness/deerflow/vision/image_input.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 2 paths with message `refactor(sandbox): extract path mapping`.

## Task 3: Extract Local Bash command and path policy

**Files:**

- Create: `backend/packages/harness/deerflow/sandbox/tooling/bash_policy.py`
- Modify: `backend/packages/harness/deerflow/sandbox/tools.py:72-89,108-143,424-430,923-1273`
- Modify: `backend/tests/test_python_module_decomposition_sandbox_tools.py`
- Modify: `backend/tests/test_sandbox_tools_security.py:15-42,402-412,614-887,1294-1353,1408-1416,1459-1517,1586-1602`

**Interfaces:**

- Consumes: `VIRTUAL_PATH_PREFIX` and the Skill/ACP/custom-mount/path-rejection helpers from `tooling.path_mapping`.
- Produces: `validate_local_bash_command_paths()`, `replace_virtual_paths_in_command()`, `_apply_cwd_prefix()`, and the exact parser/allowlist helpers used by Host Execution and Bash.

- [ ] **Step 1: add the failing Bash-policy owner identity test**

  ```python
  def test_bash_policy_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module(
          "deerflow.sandbox.tooling.bash_policy",
      )
      names = (
          "_get_mcp_allowed_paths",
          "_is_non_file_url_token",
          "_non_file_url_spans",
          "_is_in_spans",
          "_has_dotdot_path_segment",
          "_split_shell_tokens",
          "_is_shell_command_separator",
          "_is_shell_redirection_operator",
          "_is_shell_assignment",
          "_is_allowed_local_bash_absolute_path",
          "_next_cd_target",
          "_validate_local_bash_cwd_target",
          "_validate_local_bash_root_path_args",
          "_validate_local_bash_shell_tokens",
          "_braces_are_identifier_placeholders_only",
          "_is_non_path_literal_fragment",
          "validate_local_bash_command_paths",
          "replace_virtual_paths_in_command",
          "_apply_cwd_prefix",
      )
      for name in names:
          assert getattr(legacy, name) is getattr(owner, name)
  ```

  Run this node before creating the module. Expected: RED with `ModuleNotFoundError` for `bash_policy`.

- [ ] **Step 2: move the exact policy constants and functions**

  Move the absolute-path/URL/brace regexes, allowed system prefixes, shell command sets, redirection sets, `_get_mcp_allowed_paths()`, and every definition in Step 1. Preserve the current `shlex` behavior, malformed-quote fallback, URL exclusions, brace-expansion distinction, root-command checks, path-traversal messages, and POSIX quoting.

  Use this public owner surface:

  ```python
  __all__ = [
      "replace_virtual_paths_in_command",
      "validate_local_bash_command_paths",
  ]
  ```

  Import these exact path dependencies from `tooling.path_mapping`: `VIRTUAL_PATH_PREFIX`, `_is_acp_workspace_path`, `_is_custom_mount_path`, `_is_skills_path`, `_reject_path_traversal`, and `replace_virtual_path`. In particular, `replace_virtual_paths_in_command()` must continue calling the owner `replace_virtual_path()` rather than a copied implementation.

  Import path facts from `tooling.path_mapping`. Do not move or copy `HostBashExecutionMode`, `resolve_host_bash_execution_mode()`, `resolve_local_host_bash_execution_mode()`, or `uses_local_sandbox_provider()` out of `sandbox.security`.

- [ ] **Step 3: re-export exact policy objects from the legacy module**

  Replace the moved block with explicit imports for every Step 1 name. Leave the current Bash Tool body in `tools.py`; it must resolve the imported policy functions without a wrapper.

- [ ] **Step 4: migrate only the policy tests**

  In `test_sandbox_tools_security.py`, import `_apply_cwd_prefix`, `replace_virtual_paths_in_command`, and `validate_local_bash_command_paths` from `tooling.bash_policy`. Keep path-helper imports in `tooling.path_mapping` and Tool objects in the legacy façade. Existing path-owner patches remain valid because the imported path helper functions retain their original globals.

- [ ] **Step 5: run the complete path/Bash-policy characterization**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    -q
  ```

  Require every traversal, URL, multiline `cd`, shell-wrapper, brace-expansion, Skill/ACP/custom-mount, virtual substitution, and cwd-prefix node to pass with unchanged messages.

- [ ] **Step 6: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/path_mapping.py \
    packages/harness/deerflow/sandbox/tooling/bash_policy.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py
  uvx ruff format --check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/path_mapping.py \
    packages/harness/deerflow/sandbox/tooling/bash_policy.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 3 paths with message `refactor(sandbox): extract bash policy`.

## Task 4: Extract Sandbox runtime resolution and async dispatch

**Files:**

- Create: `backend/packages/harness/deerflow/sandbox/tooling/runtime.py`
- Modify: `backend/packages/harness/deerflow/sandbox/tools.py:518-529,1276-1613`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/view_image_middleware.py:42`
- Modify: `backend/packages/harness/deerflow/tools/builtins/view_image_tool.py:90-93`
- Modify: `backend/packages/harness/deerflow/vision/image_input.py:68-81`
- Modify: `backend/packages/harness/deerflow/sandbox/middleware.py:238-250`
- Modify: `backend/tests/test_python_module_decomposition_sandbox_tools.py`
- Modify: `backend/tests/test_sandbox_tool_initialization_errors.py`
- Modify: `backend/tests/test_sandbox_tools_security.py:15-42,103-127`
- Modify: `backend/tests/test_current_upload_vision.py:760-780`
- Modify: `backend/tests/test_host_execution_approval.py:1175-1462`

**Interfaces:**

- Consumes: Sandbox provider lookup, `RunScopedReadOnlyMount`, runtime user identity, private file authority, the path-mask owner, and `check_authorization_boundary()`.
- Produces: `get_thread_data()`, `is_local_sandbox()`, `sandbox_from_runtime()`, `ensure_sandbox_initialized()`, `ensure_sandbox_initialized_async()`, `_run_sync_tool_after_async_sandbox_init()`, `ensure_thread_directories_exist()`, and `_sanitize_error()`.

- [ ] **Step 1: add direct sync/async runtime characterization on the legacy implementation**

  Add `from deerflow.sandbox import tools as sandbox_tools` to `test_sandbox_tool_initialization_errors.py`, then extend it with one fake provider that implements `acquire`, `acquire_async`, and `get` plus this behavior:

  ```python
  @pytest.mark.asyncio
  async def test_sync_and_async_initialization_use_matching_provider_paths(
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      class Sandbox:
          id = "sandbox-1"

      class Provider:
          def __init__(self) -> None:
              self.calls: list[str] = []
              self.sandbox = Sandbox()

          def acquire(self, thread_id: str, *, user_id: str | None = None) -> str:
              assert thread_id == "thread-1"
              del user_id
              self.calls.append("acquire")
              return self.sandbox.id

          async def acquire_async(
              self,
              thread_id: str,
              *,
              user_id: str | None = None,
          ) -> str:
              assert thread_id == "thread-1"
              del user_id
              self.calls.append("acquire_async")
              return self.sandbox.id

          def get(self, sandbox_id: str) -> Sandbox:
              assert sandbox_id == self.sandbox.id
              return self.sandbox

      def runtime() -> SimpleNamespace:
          return SimpleNamespace(
              state={},
              context={"thread_id": "thread-1"},
              config={},
          )

      sync_provider = Provider()
      monkeypatch.setattr(
          sandbox_tools,
          "get_sandbox_provider",
          lambda: sync_provider,
      )
      sync_runtime = runtime()
      assert sandbox_tools.ensure_sandbox_initialized(sync_runtime) is sync_provider.sandbox
      assert sandbox_tools.ensure_sandbox_initialized(sync_runtime) is sync_provider.sandbox
      assert sync_provider.calls == ["acquire"]

      async_provider = Provider()
      monkeypatch.setattr(
          sandbox_tools,
          "get_sandbox_provider",
          lambda: async_provider,
      )
      async_runtime = runtime()
      assert await sandbox_tools.ensure_sandbox_initialized_async(async_runtime) is async_provider.sandbox
      assert await sandbox_tools.ensure_sandbox_initialized_async(async_runtime) is async_provider.sandbox
      assert async_provider.calls == ["acquire_async"]
  ```

  Run this node on the current legacy implementation and require PASS. It records existing behavior before movement.

- [ ] **Step 2: add the authorization-before-thread-dispatch characterization**

  Add `import threading` and this parameterized test:

  ```python
  @pytest.mark.asyncio
  @pytest.mark.parametrize(
      "operation",
      ["before_sandbox_exec", "before_sandbox_write"],
  )
  async def test_async_tool_wrapper_authorizes_before_off_thread_invocation(
      monkeypatch: pytest.MonkeyPatch,
      operation: str,
  ) -> None:
      events: list[str] = []
      owner_thread_id = threading.get_ident()
      invoked_thread_id: int | None = None
      runtime = SimpleNamespace(
          state={},
          context={"thread_id": "thread-1"},
          config={},
      )

      async def initialize(_runtime: object) -> object:
          events.append("initialize")
          return object()

      async def authorize(context: object, seen_operation: str) -> None:
          assert context is runtime.context
          assert seen_operation == operation
          events.append(f"authorize:{seen_operation}")

      def invoke(call_runtime: object, payload: object) -> str:
          nonlocal invoked_thread_id
          assert call_runtime is runtime
          assert payload == "payload"
          invoked_thread_id = threading.get_ident()
          events.append("invoke")
          return "OK"

      monkeypatch.setattr(
          sandbox_tools,
          "ensure_sandbox_initialized_async",
          initialize,
      )
      monkeypatch.setattr(
          "deerflow.sandbox.sandbox.check_authorization_boundary",
          authorize,
      )

      result = await sandbox_tools._run_sync_tool_after_async_sandbox_init(
          invoke,
          runtime,
          "payload",
          authorization_operation=operation,
      )

      assert result == "OK"
      assert events == [
          "initialize",
          f"authorize:{operation}",
          "invoke",
      ]
      assert invoked_thread_id is not None
      assert invoked_thread_id != owner_thread_id
  ```

  Run both parameters on the legacy implementation and require PASS. Do not exercise private Skill Secret materialization in this new test because existing Host Execution tests already cover materialize-after-queue and clearing behavior.

- [ ] **Step 3: add the failing runtime-owner identity test**

  ```python
  def test_runtime_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.sandbox.tooling.runtime")
      names = (
          "_sanitize_error",
          "get_thread_data",
          "is_local_sandbox",
          "sandbox_from_runtime",
          "ensure_sandbox_initialized",
          "ensure_sandbox_initialized_async",
          "_run_sync_tool_after_async_sandbox_init",
          "_RuntimeContextOverlay",
          "ensure_thread_directories_exist",
      )
      for name in names:
          assert getattr(legacy, name) is getattr(owner, name)
  ```

  Run this node before creating `runtime.py`. Expected: RED with `ModuleNotFoundError`.

- [ ] **Step 4: move runtime implementation without changing its ordering**

  Move the exact definitions from Step 3. Import `mask_local_paths_in_output()` from `tooling.path_mapping`; do not import the façade. Use this public surface:

  ```python
  __all__ = [
      "ensure_sandbox_initialized",
      "ensure_sandbox_initialized_async",
      "ensure_thread_directories_exist",
      "get_thread_data",
      "is_local_sandbox",
      "sandbox_from_runtime",
  ]
  ```

  Preserve all of these details:

  - existing Sandbox state is reused only under the current mount rules;
  - exact Run-scoped mounts force `acquire_with_mounts` or `acquire_with_mounts_async`;
  - sync code uses sync provider APIs and async code uses async provider APIs;
  - runtime state and context receive the same `sandbox_id` updates;
  - private runs do not create host `/mnt` directories;
  - async dispatch authorizes after initialization and before `asyncio.to_thread`;
  - per-call Secret context overlays and every clearing `finally` block stay intact.

- [ ] **Step 5: re-export runtime objects and migrate exact consumers**

  Replace the moved legacy definitions with explicit imports. Then:

  - `view_image_middleware.py` imports `sandbox_from_runtime` from `tooling.runtime`;
  - `view_image_tool.py` imports `get_thread_data` from `tooling.runtime`;
  - `vision/image_input.py` lazily imports `ensure_sandbox_initialized` and `sandbox_from_runtime` from `tooling.runtime`;
  - update the ownership comment in `sandbox/middleware.py` from `deerflow.sandbox.tools` to `deerflow.sandbox.tooling.runtime`;
  - import `ensure_thread_directories_exist` from `tooling.runtime` in `test_sandbox_tools_security.py` for its direct runtime behavior test;
  - change `test_current_upload_vision.py` to patch `tooling.runtime.get_sandbox_provider` rather than the façade;
  - change the two new runtime characterization tests to import and patch `tooling.runtime`.
  - in `test_isolated_bash_executes_directly_without_approval_port`, `test_aio_async_bash_materializes_exact_skill_secret_per_command`, and `test_aio_bash_refreshes_skill_secret_after_executor_queue`, change only `ensure_sandbox_initialized_async` patches to `tooling.runtime`; `_run_sync_tool_after_async_sandbox_init()` now resolves that global in its owner. Keep the sync initialization and directory patches on the still-legacy Bash call site until Task 8.

  Remove the now-unused `sandbox_tools` module import from `test_current_upload_vision.py` after changing its patch target.

  After these edits, remove the three image/vision consumer paths from `EXPECTED_LEGACY_PRODUCTION_CONSUMERS`. The expected set becomes the three middleware consumers plus `deerflow/runtime/host_execution_runner.py`.

- [ ] **Step 6: run runtime, vision, and path gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tool_initialization_errors.py \
    tests/test_sandbox_tools_security.py::test_private_local_sandbox_does_not_create_virtual_mnt_directories \
    tests/test_current_upload_vision.py \
    tests/test_inspect_image_tool.py \
    tests/test_host_execution_approval.py::test_isolated_bash_executes_directly_without_approval_port \
    tests/test_host_execution_approval.py::test_aio_async_bash_materializes_exact_skill_secret_per_command \
    tests/test_host_execution_approval.py::test_aio_bash_refreshes_skill_secret_after_executor_queue \
    -q
  ```

  Require both new runtime characterizations, private-directory protection, current-image Sandbox lookup, and image error redaction to pass.

- [ ] **Step 7: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/runtime.py \
    packages/harness/deerflow/agents/middlewares/view_image_middleware.py \
    packages/harness/deerflow/tools/builtins/view_image_tool.py \
    packages/harness/deerflow/vision/image_input.py \
    packages/harness/deerflow/sandbox/middleware.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tool_initialization_errors.py \
    tests/test_current_upload_vision.py \
    tests/test_host_execution_approval.py
  uvx ruff format --check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/runtime.py \
    packages/harness/deerflow/agents/middlewares/view_image_middleware.py \
    packages/harness/deerflow/tools/builtins/view_image_tool.py \
    packages/harness/deerflow/vision/image_input.py \
    packages/harness/deerflow/sandbox/middleware.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tool_initialization_errors.py \
    tests/test_current_upload_vision.py \
    tests/test_host_execution_approval.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 4 paths with message `refactor(sandbox): extract runtime tooling`.

## Task 5: Extract Host Execution planning and migrate the Worker runner

**Files:**

- Create: `backend/packages/harness/deerflow/sandbox/tooling/host_execution.py`
- Modify: `backend/packages/harness/deerflow/sandbox/tools.py:1616-1669,1720-2105`
- Modify: `backend/packages/harness/deerflow/runtime/host_execution_runner.py:53-58,397,670`
- Modify: `backend/tests/test_python_module_decomposition_sandbox_tools.py`
- Modify: `backend/tests/test_host_execution_approval.py:46-50,557-589,912-1173,1465-1502`

**Interfaces:**

- Consumes: `tooling.runtime`, `tooling.bash_policy`, Host Execution contracts/ports, runtime Secret carriers, and `sandbox.security` mode resolution.
- Produces: `prepare_local_host_execution`, `truncate_bash_output`, `mask_secret_values`, channel/environment/Skill-Secret projection, and `_approval_required_bash()`.

- [ ] **Step 1: freeze Secret masking, then add the failing Host Execution owner contract**

  First import `mask_secret_values` from the legacy module and add this passing characterization:

  ```python
  def test_host_execution_masks_overlapping_secrets_longest_first() -> None:
      assert mask_secret_values(
          "token-long token",
          {
              "short": "token",
              "long": "token-long",
          },
      ) == "[redacted] [redacted]"
  ```

  Run it on the legacy implementation and require PASS. Then add:

  ```python
  def test_host_execution_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module(
          "deerflow.sandbox.tooling.host_execution",
      )
      exact_names = (
          "CHANNEL_USER_ID_ENV",
          "mask_secret_values",
          "_truncate_bash_output",
          "_is_windows",
          "_channel_identity_state",
          "_channel_identity_prefix_from_state",
          "_channel_identity_prefix",
          "_github_env_from_runtime",
          "_runtime_app_config",
          "_runtime_host_bash_execution_mode",
          "_host_execution_agent_path",
          "_host_execution_environment_keys",
          "_host_execution_skill_secret_sources",
          "_host_execution_legacy_environment_keys",
          "_approval_scan_secrets",
          "_command_contains_secret",
          "_prepare_local_host_execution",
          "_approval_required_bash",
      )
      for name in exact_names:
          assert getattr(legacy, name) is getattr(owner, name)
      assert owner.prepare_local_host_execution is owner._prepare_local_host_execution
      assert owner.truncate_bash_output is owner._truncate_bash_output
  ```

  Run this node before creating the owner. Expected: RED with `ModuleNotFoundError`.

- [ ] **Step 2: move the complete identity, Secret, plan, and staging block**

  Move every Step 1 definition and its constants without changing branches or messages. Define this owner's own `logger = logging.getLogger(__name__)` for the existing GitHub-token warning. Add only these aliases and public surface:

  ```python
  prepare_local_host_execution = _prepare_local_host_execution
  truncate_bash_output = _truncate_bash_output

  __all__ = [
      "CHANNEL_USER_ID_ENV",
      "mask_secret_values",
      "prepare_local_host_execution",
      "truncate_bash_output",
  ]
  ```

  Keep the underlying function names underscored so the legacy aliases preserve metadata as well as identity. Keep `HostExecutionPlan` construction exact, including tool-call/run/thread coordinates, command/shell/cwd/timeout, environment keys, Skill Secret sources, legacy keys, Agent path, and channel identity. Keep Secret scanning and all dictionary clearing in the same `try/finally` scopes.

- [ ] **Step 3: expose exact legacy aliases without duplicating planning**

  Import every compatibility name from the owner into `tools.py`. The Bash Tool remains temporarily in the legacy module and consumes those imported objects. Do not move `HostExecutionApprovalPort` behavior or add a second approval adapter.

- [ ] **Step 4: switch `host_execution_runner.py` to public owner APIs**

  Replace the façade-private import with:

  ```python
  from deerflow.sandbox.tooling.host_execution import (
      mask_secret_values,
      prepare_local_host_execution,
      truncate_bash_output,
  )
  from deerflow.sandbox.tooling.path_mapping import mask_local_paths_in_output
  ```

  Change only the two call names:

  ```python
  def bounded(value: str) -> str:
      return truncate_bash_output(
          mask_secret_values(
              mask_local_paths_in_output(value, thread_data),
              injected_env,
          ),
          max_chars,
      )

  rebound, max_chars = prepare_local_host_execution(
      runtime,
      sandbox,
      description=plan.description,
      requested_command=plan.requested_command,
  )
  ```

  Preserve claim, final-spawn authorization, retry-safety fence, subprocess, output delivery, completion, and failure-settlement code byte-for-byte.

- [ ] **Step 5: migrate only Host Execution tests and monkeypatch globals**

  - Import `_host_execution_skill_secret_sources`, `mask_secret_values`, and public `prepare_local_host_execution` from `tooling.host_execution` in `test_host_execution_approval.py`.
  - For approval-path tests at current lines 912-1173 and 1465-1502, patch `tooling.host_execution.ensure_sandbox_initialized_async` and `tooling.host_execution.ensure_thread_directories_exist`.
  - Keep legacy `deerflow.sandbox.tools:bash_tool` configuration strings unchanged.
  - Keep direct-isolated Bash async initialization patches on `tooling.runtime` from Task 4. Keep their sync Bash-call-site patches on the legacy module until the Bash Tool itself moves in Task 8.

  Remove `deerflow/runtime/host_execution_runner.py` from `EXPECTED_LEGACY_PRODUCTION_CONSUMERS`; three middleware consumer paths remain.

- [ ] **Step 6: add the runner import contract**

  Parse `host_execution_runner.py` and assert it imports `prepare_local_host_execution` and `truncate_bash_output` from `deerflow.sandbox.tooling.host_execution`, imports `mask_local_paths_in_output` from `deerflow.sandbox.tooling.path_mapping`, and has no import from `deerflow.sandbox.tools`.

- [ ] **Step 7: run Host Execution behavior and selected PostgreSQL gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_host_execution_approval.py \
    tests/test_host_execution_continuation_runner.py \
    tests/test_host_execution_provider_classification.py \
    tests/test_run_worker_host_execution_pause.py \
    -q -m "not postgres and not provider_integration"

  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run --env-file ../.env python \
    tests/support/core_gate_plugin.py \
    'tests/test_execution_approval_lifecycle_postgres.py::test_local_host_execution_approval_is_consumed_once_with_receipt[None]' \
    tests/test_execution_approval_output_delivery_e2e_postgres.py::test_deferred_output_reaches_artifact_and_success_after_one_frozen_execution \
    tests/test_worker_lease_heartbeat_postgres.py::test_before_sandbox_exec_has_no_side_effect_when_database_lease_expired \
    -q -m "not provider_integration"
  ```

  Require zero PostgreSQL skips. Confirm pending approval stages once without spawn; continuation claim/spawn/complete remain once each; completion-adapter failure never respawns; exact Skill Secrets are revalidated, masked, and cleared.

- [ ] **Step 8: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/host_execution.py \
    packages/harness/deerflow/runtime/host_execution_runner.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_host_execution_approval.py
  uvx ruff format --check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/host_execution.py \
    packages/harness/deerflow/runtime/host_execution_runner.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_host_execution_approval.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 5 paths with message `refactor(sandbox): extract host execution planning`.

## Task 6: Extract Glob and Grep search Tools

**Files:**

- Create: `backend/packages/harness/deerflow/sandbox/tooling/search_tools.py`
- Modify: `backend/packages/harness/deerflow/sandbox/tools.py:93-96,433-458,473-498,2296-2470`
- Modify: `backend/tests/test_python_module_decomposition_sandbox_tools.py`
- Modify: `backend/tests/test_sandbox_tools_security.py:249-327`
- Modify: `backend/tests/test_sandbox_tool_initialization_errors.py`

**Interfaces:**

- Consumes: path authorization/mapping/masking from `tooling.path_mapping`, runtime initialization/error handling from `tooling.runtime`, `GrepMatch`, and current Tool configuration.
- Produces: the exact `glob_tool` and `grep_tool` objects, their coroutines, bounded result limits, and stable result formatting.

- [ ] **Step 1: record exact search result formatting, then add the failing owner identity test**

  First import `_format_glob_results` and `_format_grep_results` from the legacy module and add this passing characterization:

  ```python
  def test_search_result_formatting_is_frozen() -> None:
      match = GrepMatch(
          path="/root/a.txt",
          line_number=2,
          line="needle",
      )
      assert _format_glob_results("/root", [], False) == (
          "No files matched under /root"
      )
      assert _format_glob_results("/root", ["/root/a.txt"], True) == (
          "Found 1 paths under /root (showing first 1)\n"
          "1. /root/a.txt\n"
          "Results truncated. Narrow the path or pattern to see fewer matches."
      )
      assert _format_grep_results("/root", [], False) == (
          "No matches found under /root"
      )
      assert _format_grep_results("/root", [], True) == (
          "Results truncated while searching under /root; matches may exist "
          "beyond the provider scan budget. Narrow the path or add a glob filter."
      )
      assert _format_grep_results("/root", [match], True) == (
          "Found 1 matches under /root (showing first 1)\n"
          "/root/a.txt:2: needle\n"
          "Results truncated. Narrow the path or add a glob filter."
      )
  ```

  Run it against the legacy implementation and require PASS. Then add:

  ```python
  def test_search_tool_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module(
          "deerflow.sandbox.tooling.search_tools",
      )
      names = (
          "_get_tool_config_int",
          "_clamp_max_results",
          "_resolve_max_results",
          "_format_glob_results",
          "_format_grep_results",
          "glob_tool",
          "_glob_tool_async",
          "grep_tool",
          "_grep_tool_async",
      )
      for name in names:
          assert getattr(legacy, name) is getattr(owner, name)
      for name in ("glob_tool", "grep_tool"):
          assert getattr(legacy, name).func is getattr(owner, name).func
          assert getattr(legacy, name).coroutine is getattr(owner, name).coroutine
  ```

  Run this node before creating `search_tools.py`. Expected: RED with `ModuleNotFoundError`.

- [ ] **Step 2: move limits, formatting, and both decorated Tools once**

  Move the four result-limit constants, config/clamp/resolve helpers, both formatters, both Tool definitions, both async wrappers, and the two `.coroutine` assignments. Use:

  ```python
  __all__ = [
      "glob_tool",
      "grep_tool",
  ]
  ```

  Preserve requested-path error projection, local read-path validation, delegated hidden-runtime filtering, output masking, `max_results` defaults/caps, provider scan `truncated` handling, every error string, and the exact order of arguments passed to `sandbox.glob()` and `sandbox.grep()`.

- [ ] **Step 3: replace the legacy search block with exact aliases**

  Import `glob_tool`, `_glob_tool_async`, `grep_tool`, `_grep_tool_async`, and the frozen private helpers from `tooling.search_tools`. Remove both legacy `@tool` decorators and both legacy coroutine assignments. Do not create replacement wrappers.

- [ ] **Step 4: migrate only search Tool monkeypatch call sites**

  - In `test_delegated_workspace_scans_hide_every_task_scratch`, patch `ensure_sandbox_initialized`, `ensure_thread_directories_exist`, and `is_local_sandbox` on both the still-legacy `ls` call site and `tooling.search_tools` for `glob`/`grep`.
  - In `test_sandbox_tool_initialization_errors.py`, extend each parameter with the call-site module. Point `glob` and `grep` to `tooling.search_tools`; leave `ls` and `str_replace` on the legacy module until Task 7.
  - Move `_format_glob_results` and `_format_grep_results` imports to `tooling.search_tools`; keep the exact strings from Step 1 unchanged.
  - Do not split either test file or duplicate the existing fake Sandbox.

- [ ] **Step 5: run search, delegated-output, schema, and initialization gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_sandbox_tool_initialization_errors.py \
    -q
  ```

  Require exact Tool schema digests, owner/façade identity, empty/exact/truncated result text, requested-path errors, delegated scratch filtering, and initialization-error mapping to remain unchanged.

- [ ] **Step 6: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/search_tools.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_sandbox_tool_initialization_errors.py
  uvx ruff format --check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/search_tools.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_sandbox_tool_initialization_errors.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 6 paths with message `refactor(sandbox): extract search tools`.

## Task 7: Extract File Tools and preserve locking

**Files:**

- Create: `backend/packages/harness/deerflow/sandbox/tooling/files.py`
- Modify: `backend/packages/harness/deerflow/sandbox/tools.py:97,99-107,532-565,1672-1717,2232-2293,2473-2780`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/read_before_write_middleware.py:43`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py:115`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/tool_result_sanitization_middleware.py:68-70`
- Modify: `backend/tests/test_python_module_decomposition_sandbox_tools.py`
- Modify: `backend/tests/test_sandbox_tools_security.py:97-337,911-988,1024-1235,1673-2052`
- Modify: `backend/tests/test_sandbox_tool_initialization_errors.py`

**Interfaces:**

- Consumes: `tooling.path_mapping`, `tooling.runtime`, `get_file_operation_lock()`, and the current Sandbox file APIs.
- Produces: the exact `ls_tool`, `read_file_tool`, `write_file_tool`, and `str_replace_tool` objects/coroutines plus `read_current_file_content()` and file-specific bounds/errors.

- [ ] **Step 1: record the current File output and UTF-8 write-size bounds**

  Import `_truncate_ls_output` and `_truncate_read_file_output` from the legacy module and add these focused characterizations before moving production code:

  ```python
  @pytest.mark.parametrize(
      "truncate",
      [_truncate_ls_output, _truncate_read_file_output],
  )
  def test_file_read_output_truncators_honor_the_exact_limit(truncate) -> None:
      result = truncate("A" * 300, 160)
      assert len(result) <= 160
      assert result.startswith("A")
      assert "[truncated: showing first" in result


  def test_write_file_non_append_limit_counts_utf8_bytes(
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      monkeypatch.setenv("ACT_WEAVE_WRITE_FILE_MAX_BYTES", "4")
      result = write_file_tool.func(
          runtime=object(),
          description="verify byte limit",
          path="/mnt/user-data/outputs/report.txt",
          content="你好",
          append=False,
      )
      assert "write_file content (6 bytes) exceeds the 4-byte single-call limit" in result
  ```

  Run these nodes on the legacy implementation and require PASS. They freeze read/list bounds and the pre-initialization byte gate without introducing a fake Sandbox.

- [ ] **Step 2: add the failing File owner and Tool identity test**

  ```python
  def test_file_tool_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.sandbox.tooling.files")
      names = (
          "_truncate_write_file_error_detail",
          "_format_write_file_error",
          "_truncate_read_file_output",
          "_truncate_ls_output",
          "ls_tool",
          "_ls_tool_async",
          "read_current_file_content",
          "read_file_tool",
          "_read_file_tool_async",
          "_effective_write_file_max_bytes",
          "write_file_tool",
          "_write_file_tool_async",
          "str_replace_tool",
          "_str_replace_tool_async",
      )
      for name in names:
          assert getattr(legacy, name) is getattr(owner, name)
      for name in (
          "ls_tool",
          "read_file_tool",
          "write_file_tool",
          "str_replace_tool",
      ):
          assert getattr(legacy, name).func is getattr(owner, name).func
          assert getattr(legacy, name).coroutine is getattr(owner, name).coroutine
  ```

  Run this node before creating `files.py`. Expected: RED with `ModuleNotFoundError`.

- [ ] **Step 3: move File helpers and four decorated Tools exactly once**

  Move the write-size/error constants, write-error helpers, read/ls truncators, `ls`, shared full-content reader, `read_file`, `write_file`, `str_replace`, four async wrappers, and four `.coroutine` assignments. Use:

  ```python
  __all__ = [
      "ls_tool",
      "read_current_file_content",
      "read_file_tool",
      "str_replace_tool",
      "write_file_tool",
  ]
  ```

  Preserve exact requested-path messages, binary-file guidance, read ranges, local/Skill/ACP/custom-mount handling, path/error masking, byte limits, return values, and truncation markers. Preserve these critical sections verbatim:

  ```python
  with get_file_operation_lock(sandbox, path):
      sandbox.write_file(path, content, append)
  ```

  ```python
  with get_file_operation_lock(sandbox, path):
      content = sandbox.read_file(path)
      if not old_str:
          return "OK"
      if not content or old_str not in content:
          return f"Error: String to replace not found in file: {requested_path}"
      if replace_all:
          content = content.replace(old_str, new_str)
      else:
          content = content.replace(old_str, new_str, 1)
      sandbox.write_file(path, content)
  ```

  Do not move `ReadBeforeWriteMiddleware` locks into this module and do not merge the two lock scopes.

- [ ] **Step 4: replace the legacy File block with exact aliases**

  Explicitly import every Step 2 name into `tools.py`. Remove the four legacy decorators and coroutine assignments. Keep legacy reflection/config paths unchanged and do not add `__all__` to the façade.

- [ ] **Step 5: migrate the three exact production consumers**

  - `read_before_write_middleware.py` imports `read_current_file_content` from `tooling.files`.
  - `tool_result_sanitization_middleware.py` lazily imports `read_file_tool` from `tooling.files` and retains identity comparison.
  - `tool_error_handling_middleware.py` imports `read_file_tool` from `tooling.files`; its Bash import remains on the legacy path until Task 8.

  Remove the first two middleware paths from `EXPECTED_LEGACY_PRODUCTION_CONSUMERS`. Only `deerflow/agents/middlewares/tool_error_handling_middleware.py` remains.

- [ ] **Step 6: migrate File Tool monkeypatch call sites without test reorganization**

  In `test_sandbox_tools_security.py`, move init/dirs/local/path-helper patch targets to `tooling.files` for:

  - delegated write/read and the `ls` half of delegated workspace scanning;
  - Run-scoped read/write and exact mounted Skill reads;
  - three concurrent write/replace tests;
  - all `test_write_file_tool_*` error cases.

  `_sanitize_error()` lives in `tooling.runtime`; where a test stubs local classification or thread data specifically for sanitization, patch that owner as well as the File call site. In `test_sandbox_tool_initialization_errors.py`, point `ls` and `str_replace` parameters to `tooling.files`; `glob` and `grep` remain on `tooling.search_tools`.

  Move `_truncate_ls_output` and `_truncate_read_file_output` imports for the new bound test to `tooling.files`; leave its assertions unchanged.

- [ ] **Step 7: run File, middleware, concurrency, and compatibility gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run pytest \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_sandbox_tool_initialization_errors.py \
    tests/test_tool_call_control.py \
    tests/test_inspect_image_tool.py \
    tests/test_langchain_contract.py \
    -q
  ```

  Require Tool/schema identity, read-before-write behavior, upload sanitization identity, all three file-concurrency nodes, bounded errors, delegated output, exact Skill mount reads, and the UTF-8 byte limit to pass.

- [ ] **Step 8: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/files.py \
    packages/harness/deerflow/agents/middlewares/read_before_write_middleware.py \
    packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py \
    packages/harness/deerflow/agents/middlewares/tool_result_sanitization_middleware.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_sandbox_tool_initialization_errors.py
  uvx ruff format --check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/files.py \
    packages/harness/deerflow/agents/middlewares/read_before_write_middleware.py \
    packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py \
    packages/harness/deerflow/agents/middlewares/tool_result_sanitization_middleware.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_sandbox_tool_initialization_errors.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 7 paths with message `refactor(sandbox): extract file tools`.

## Task 8: Extract Bash Tool and finish the long-lived façade

**Files:**

- Create: `backend/packages/harness/deerflow/sandbox/tooling/bash.py`
- Rewrite: `backend/packages/harness/deerflow/sandbox/tools.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py:184`
- Modify: `backend/tests/test_python_module_decomposition_sandbox_tools.py`
- Modify: `backend/tests/test_sandbox_tools_security.py:887-1021,1236-1263`
- Modify: `backend/tests/test_host_execution_provider_classification.py:133-248`
- Modify: `backend/tests/test_host_execution_approval.py:1097-1462`

**Interfaces:**

- Consumes: `tooling.path_mapping`, `tooling.bash_policy`, `tooling.runtime`, `tooling.host_execution`, and unchanged `sandbox.security` policy.
- Produces: the single exact `bash_tool` object and `_bash_tool_async()`; `deerflow.sandbox.tools` becomes an imports-only façade over all seven owners.

- [ ] **Step 1: add a behavioral order test on the still-legacy Bash implementation**

  Import `deerflow.sandbox.tools` as `bash_owner` for the pre-move characterization and add:

  ```python
  def test_local_bash_masks_paths_then_secrets_then_truncates(
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      transform_order: list[str] = []
      executions: list[str] = []

      class Sandbox:
          def execute_command(
              self,
              command: str,
              *,
              env: dict[str, str] | None = None,
              timeout: int | None = None,
          ) -> str:
              del env, timeout
              executions.append(command)
              return "raw-output"

      runtime = SimpleNamespace(
          state={
              "sandbox": {"sandbox_id": "local"},
              "thread_data": _THREAD_DATA.copy(),
          },
          context={"thread_id": "thread-1"},
          config={},
      )
      app_config = SimpleNamespace(
          sandbox=SimpleNamespace(
              bash_output_max_chars=20,
              bash_command_timeout=60,
          ),
      )

      monkeypatch.setattr(
          bash_owner,
          "ensure_sandbox_initialized",
          lambda _runtime: Sandbox(),
      )
      monkeypatch.setattr(
          bash_owner,
          "ensure_thread_directories_exist",
          lambda _runtime: None,
      )
      monkeypatch.setattr(
          bash_owner,
          "_runtime_app_config",
          lambda _runtime: app_config,
      )
      monkeypatch.setattr(
          bash_owner,
          "_runtime_host_bash_execution_mode",
          lambda *_args: HostBashExecutionMode.LOCAL_LEGACY_ALLOW,
      )

      def transform(name: str, expected: str, result: str):
          def apply(value: str, *_args: object) -> str:
              assert value == expected
              transform_order.append(name)
              return result

          return apply

      monkeypatch.setattr(
          bash_owner,
          "mask_local_paths_in_output",
          transform("mask_local_paths_in_output", "raw-output", "paths-masked"),
      )
      monkeypatch.setattr(
          bash_owner,
          "mask_secret_values",
          transform("mask_secret_values", "paths-masked", "secrets-masked"),
      )
      monkeypatch.setattr(
          bash_owner,
          "_truncate_bash_output",
          transform("truncate_bash_output", "secrets-masked", "bounded"),
      )

      result = bash_tool.func(
          runtime=runtime,
          description="verify output transforms",
          command="/bin/echo hello",
      )

      assert result == "bounded"
      assert len(executions) == 1
  ```

  Finish with this exact order assertion:

  ```python
  assert transform_order == [
      "mask_local_paths_in_output",
      "mask_secret_values",
      "truncate_bash_output",
  ]
  ```

  Run the test on the current legacy implementation and require PASS. After moving Bash, import `tooling.bash` as `bash_owner`; the test body and assertions remain unchanged. This freezes the confidentiality-sensitive nesting before the final move.

- [ ] **Step 2: add the failing Bash owner identity test**

  ```python
  def test_bash_tool_owner_is_the_exact_legacy_export() -> None:
      owner = importlib.import_module("deerflow.sandbox.tooling.bash")
      assert legacy.bash_tool is owner.bash_tool
      assert legacy._bash_tool_async is owner._bash_tool_async
      assert legacy.bash_tool.func is owner.bash_tool.func
      assert legacy.bash_tool.coroutine is owner.bash_tool.coroutine
  ```

  Run this node before creating `bash.py`. Expected: RED with `ModuleNotFoundError`.

- [ ] **Step 3: move the Bash Tool and coroutine exactly once**

  Move the complete `@tool("bash", parse_docstring=True)` function, `_bash_tool_async()`, and `bash_tool.coroutine = _bash_tool_async` assignment. Use:

  ```python
  __all__ = ["bash_tool"]
  ```

  Preserve the full Tool docstring because it participates in the frozen call schema. Preserve runtime Secret readiness, GitHub environment injection, channel identity, execution-mode precedence, Local policy branches, cwd behavior, command timeout, authorization operation `before_sandbox_exec`, error sanitization, output transform order, and Secret clearing.

- [ ] **Step 4: rewrite `tools.py` as an explicit imports-only façade**

  The final module may contain a module docstring and explicit imports/aliases only. It must:

  - expose every public function and Tool previously consumed in the repository;
  - expose every underscore compatibility seam asserted in Tasks 2-8;
  - alias `_prepare_local_host_execution` and `_truncate_bash_output` to the exact Host Execution owner objects;
  - expose the seven exact Tool objects;
  - retain no function/class definitions, decorators, wrappers, coroutine assignments, runtime calls, logger, or `__all__`.

  Do not use wildcard imports. A representative Tool block is:

  ```python
  from deerflow.sandbox.tooling.bash import _bash_tool_async, bash_tool
  from deerflow.sandbox.tooling.files import (
      ls_tool,
      read_current_file_content,
      read_file_tool,
      str_replace_tool,
      write_file_tool,
  )
  from deerflow.sandbox.tooling.search_tools import glob_tool, grep_tool
  ```

- [ ] **Step 5: migrate the last production consumer and every Bash call-site patch**

  - `tool_error_handling_middleware.py` imports `bash_tool` from `tooling.bash`; `EXPECTED_LEGACY_PRODUCTION_CONSUMERS` becomes `frozenset()`.
  - In `test_sandbox_tools_security.py`, patch Bash call-site globals on `tooling.bash`.
  - In `test_host_execution_provider_classification.py`, import `tooling.bash` as the owner, patch its `_approval_required_bash` and `_run_sync_tool_after_async_sandbox_init`, and invoke its `_bash_tool_async()`.
  - In direct-isolated cases in `test_host_execution_approval.py`, retain async initialization patches on `tooling.runtime`, move sync Tool globals to `tooling.bash`, and keep approval preparation globals on `tooling.host_execution` according to the path actually exercised.
  - Keep every legacy reflection/configuration string in the same test file unchanged.

- [ ] **Step 6: add final façade, decorator-count, dependency, and import-order gates**

  Add `import subprocess`, `import sys`, and `from textwrap import dedent`, define `TOOLING_ROOT = HARNESS_ROOT / "deerflow" / "sandbox" / "tooling"`, and add:

  ```python
  def _absolute_imports(path: Path) -> set[str]:
      tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
      imports: set[str] = set()
      for node in ast.walk(tree):
          if isinstance(node, ast.Import):
              imports.update(alias.name for alias in node.names)
          elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
              imports.add(node.module)
      return imports


  def test_sandbox_tools_facade_is_imports_only_and_tools_are_created_once() -> None:
      facade_path = HARNESS_ROOT / "deerflow" / "sandbox" / "tools.py"
      facade_tree = ast.parse(
          facade_path.read_text(encoding="utf-8"),
          filename=str(facade_path),
      )
      assert all(
          isinstance(node, (ast.Import, ast.ImportFrom))
          or (
              isinstance(node, ast.Expr)
              and isinstance(node.value, ast.Constant)
              and isinstance(node.value.value, str)
          )
          for node in facade_tree.body
      )
      tool_decorators = [
          decorator
          for path in TOOLING_ROOT.glob("*.py")
          for node in ast.walk(
              ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
          )
          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
          for decorator in node.decorator_list
          if isinstance(decorator, ast.Call)
          and getattr(decorator.func, "id", None) == "tool"
      ]
      assert len(tool_decorators) == 7
      assert _legacy_production_consumers() == frozenset()


  def test_tooling_has_no_facade_app_or_database_imports() -> None:
      imports = set().union(
          *(
              _absolute_imports(path)
              for path in TOOLING_ROOT.glob("*.py")
          )
      )
      forbidden = {
          name
          for name in imports
          if name == "deerflow.sandbox.tools"
          or name == "app"
          or name.startswith("app.")
          or name == "sqlalchemy"
          or name.startswith("sqlalchemy.")
      }
      assert forbidden == set()
  ```

  Add this exact two-order subprocess test:

  ```python
  def test_tooling_and_legacy_import_cleanly_in_both_orders() -> None:
      owner_first = dedent(
          """
          from deerflow.sandbox.tooling import bash, bash_policy, files, host_execution, path_mapping, runtime, search_tools
          from deerflow.runtime import host_execution_runner as runner
          from deerflow.sandbox import tools as legacy
          assert legacy.bash_tool is bash.bash_tool
          assert legacy.ls_tool is files.ls_tool
          assert legacy.read_file_tool is files.read_file_tool
          assert legacy.write_file_tool is files.write_file_tool
          assert legacy.str_replace_tool is files.str_replace_tool
          assert legacy.glob_tool is search_tools.glob_tool
          assert legacy.grep_tool is search_tools.grep_tool
          assert runner.prepare_local_host_execution is host_execution.prepare_local_host_execution
          assert runner.mask_local_paths_in_output is path_mapping.mask_local_paths_in_output
          """
      )
      legacy_first = dedent(
          """
          from deerflow.sandbox import tools as legacy
          from deerflow.runtime import host_execution_runner as runner
          from deerflow.sandbox.tooling import bash, bash_policy, files, host_execution, path_mapping, runtime, search_tools
          assert legacy.bash_tool is bash.bash_tool
          assert legacy.ls_tool is files.ls_tool
          assert legacy.read_file_tool is files.read_file_tool
          assert legacy.write_file_tool is files.write_file_tool
          assert legacy.str_replace_tool is files.str_replace_tool
          assert legacy.glob_tool is search_tools.glob_tool
          assert legacy.grep_tool is search_tools.grep_tool
          assert runner.truncate_bash_output is host_execution.truncate_bash_output
          """
      )
      for source in (owner_first, legacy_first):
          completed = subprocess.run(
              [sys.executable, "-c", source],
              cwd=BACKEND_ROOT,
              capture_output=True,
              check=False,
              text=True,
          )
          assert completed.returncode == 0, completed.stderr
  ```

  The first subprocess imports owners in dependency order, then `host_execution_runner`, then the legacy façade. The second starts with the legacy façade. Together they prove all Tool identities and the runner's public Host Execution aliases without sharing `sys.modules` state.

- [ ] **Step 7: run the complete focused Batch 4 suite**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_sandbox_tool_initialization_errors.py \
    tests/test_host_execution_provider_classification.py \
    tests/test_host_execution_approval.py \
    tests/test_host_execution_continuation_runner.py \
    tests/test_run_worker_host_execution_pause.py \
    tests/test_current_upload_vision.py \
    tests/test_memory_tool_registration.py \
    tests/test_agent_assembly_golden.py \
    tests/test_middleware_assembly_contracts.py \
    -q -m "not postgres and not provider_integration"
  ```

  Require zero failures. Verify legacy config reflection, exact Tool identity/schema, provider classification, Host Approval staging, one-time continuation execution, path security, Secret masking/clearing, file locks, vision consumers, and middleware registration.

- [ ] **Step 8: static checkpoint and conditional commit**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  uvx ruff check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/bash.py \
    packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_host_execution_provider_classification.py \
    tests/test_host_execution_approval.py
  uvx ruff format --check \
    packages/harness/deerflow/sandbox/tools.py \
    packages/harness/deerflow/sandbox/tooling/bash.py \
    packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_host_execution_provider_classification.py \
    tests/test_host_execution_approval.py
  make detect-blocking-io
  cd ..
  git diff --check
  git status --short
  ```

  If authorized, commit only Task 8 paths with message `refactor(sandbox): extract bash tool`.

## Task 9: Document ownership and run complete Batch 4 verification

**Files:**

- Modify: `backend/AGENTS.md`
- Verify: every production and test path changed in Tasks 1-8
- Preserve: `backend/tests/test_skill_builder_durable_agent_postgres.py` and the four pre-existing untracked decomposition documents

**Interfaces:**

- Consumes: all seven stable owners, the long-lived façade, migrated internal consumers, and current compatibility/behavior tests.
- Produces: documented maintenance ownership plus current focused, PostgreSQL, import, static, and full-backend evidence.

- [ ] **Step 1: document the stable owner boundaries**

  Add this rule to `backend/AGENTS.md` under `## Where changes live`, formatted to the existing guide width:

  > Sandbox virtual-path mapping, Local Bash path policy, Sandbox initialization, Host Execution planning/redaction, and concrete file/search/Bash Tool objects are owned by `packages/harness/deerflow/sandbox/tooling/`. `deerflow.sandbox.tools` is a long-lived compatibility façade for published Tool configuration paths, while `deerflow.sandbox.security` remains the Host Bash policy authority. Internal Harness code imports the owning module; each LangChain Tool is decorated exactly once.

  Update no README, `config.example.yaml`, wizard output, frontend documentation, or feature changelog because runtime architecture and the supported configuration surface are unchanged.

- [ ] **Step 2: run the combined focused suite once**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest \
    tests/test_python_module_decomposition_contract.py \
    tests/test_python_module_decomposition_sandbox_tools.py \
    tests/test_sandbox_tools_security.py \
    tests/test_sandbox_tool_initialization_errors.py \
    tests/test_host_execution_provider_classification.py \
    tests/test_host_execution_approval.py \
    tests/test_host_execution_continuation_runner.py \
    tests/test_run_worker_host_execution_pause.py \
    tests/test_current_upload_vision.py \
    tests/test_inspect_image_tool.py \
    tests/test_memory_tool_registration.py \
    tests/test_agent_assembly_golden.py \
    tests/test_middleware_assembly_contracts.py \
    tests/test_tool_call_control.py \
    tests/test_langchain_contract.py \
    -q -m "not postgres and not provider_integration"
  ```

  Record exact count, duration, deselections, skips, and warning categories. Require zero failures. A focused/offline result does not certify PostgreSQL, a real external Sandbox, or a target deployment.

- [ ] **Step 3: run the selected PostgreSQL authority and one-time execution gate**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run --env-file ../.env python \
    tests/support/core_gate_plugin.py \
    'tests/test_execution_approval_lifecycle_postgres.py::test_local_host_execution_approval_is_consumed_once_with_receipt[None]' \
    tests/test_execution_approval_output_delivery_e2e_postgres.py::test_deferred_output_reaches_artifact_and_success_after_one_frozen_execution \
    tests/test_worker_lease_heartbeat_postgres.py::test_before_sandbox_exec_has_no_side_effect_when_database_lease_expired \
    -q -m "not provider_integration"
  ```

  Require `failed=0 skipped=0` against disposable `deerflow_test_*` databases. This protects one-time Approval/receipt consumption, the moved runner/helper path, output delivery, and lease validation before Sandbox execution.

- [ ] **Step 4: run fresh import-order and exact-object smoke**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  PYTHONPATH=. uv run python -c '
  from deerflow.sandbox.tooling import bash, bash_policy, files, host_execution, path_mapping, runtime, search_tools
  from deerflow.runtime import host_execution_runner
  from deerflow.sandbox import tools as legacy
  assert legacy.bash_tool is bash.bash_tool
  assert legacy.ls_tool is files.ls_tool
  assert legacy.read_file_tool is files.read_file_tool
  assert legacy.write_file_tool is files.write_file_tool
  assert legacy.str_replace_tool is files.str_replace_tool
  assert legacy.glob_tool is search_tools.glob_tool
  assert legacy.grep_tool is search_tools.grep_tool
  assert legacy._prepare_local_host_execution is host_execution.prepare_local_host_execution
  assert legacy._truncate_bash_output is host_execution.truncate_bash_output
  assert host_execution_runner.prepare_local_host_execution is host_execution.prepare_local_host_execution
  assert host_execution_runner.truncate_bash_output is host_execution.truncate_bash_output
  assert host_execution_runner.mask_local_paths_in_output is path_mapping.mask_local_paths_in_output
  assert not hasattr(legacy, "__all__")
  '

  PYTHONPATH=. uv run python -c '
  from deerflow.sandbox import tools as legacy
  from deerflow.runtime import host_execution_runner
  from deerflow.sandbox.tooling import bash, bash_policy, files, host_execution, path_mapping, runtime, search_tools
  assert legacy.bash_tool is bash.bash_tool
  assert legacy.ls_tool is files.ls_tool
  assert legacy.glob_tool is search_tools.glob_tool
  assert host_execution_runner.prepare_local_host_execution is host_execution.prepare_local_host_execution
  '
  ```

  Both commands must exit 0 without starting a Gateway, Worker, Scheduler, external Sandbox, or database connection.

- [ ] **Step 5: run repository-required format, lint, blocking-I/O, and full backend gates**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations/backend
  make format
  make lint
  make detect-blocking-io
  uv run --env-file ../.env make test
  ```

  Inspect `git status --short` immediately after `make format`. If formatting touches a file outside the Batch 4 set or the already-preserved P1 test, stop and separate the drift instead of including it. Require every command to exit 0 and the core suite to report `failed=0 skipped=0`. Do not use a production database or unapproved external Provider/Sandbox.

- [ ] **Step 6: perform final structural and scope review**

  ```bash
  cd /Users/jiangfeng/workspace/deer-flow/.worktrees/python-module-decomposition-foundations
  git diff --check
  git status --short
  git diff --stat eb3dd904df46dd08534fd9aff1a23cd93c72a33e
  ! rg -n '^[[:space:]]*(from|import)[[:space:]]+deerflow[.]sandbox[.]tools' \
    backend/packages/harness/deerflow \
    --glob '*.py' \
    --glob '!**/sandbox/tools.py'
  ! rg -n '^[[:space:]]*(from|import)[[:space:]]+(app|sqlalchemy)([[:space:].]|$)' \
    backend/packages/harness/deerflow/sandbox/tooling \
    --glob '*.py'
  rg -n '@tool[(]' backend/packages/harness/deerflow/sandbox/tooling --glob '*.py'
  ```

  The first two import scans must return no matches. The decorator scan must return exactly seven definitions: one each for Bash, ls, glob, grep, read_file, write_file, and str_replace. Review `tools.py` and require imports only, no `__all__`, no wrapper, and no Tool construction.

  Review every changed/untracked path individually. The diff from `eb3dd904` also contains the separate P1 test if it remains uncommitted; identify it explicitly and never claim it as Batch 4 work. Confirm no config string, script/wizard output, frontend document, Schema/DDL, Worker execution core, executor, Sandbox provider, or unrelated test refactor changed.

- [ ] **Step 7: request one final independent review**

  Use `superpowers:requesting-code-review` over the exact Batch 4 path set. If local commits were authorized, review the Batch 4 commit range; otherwise review all tracked and untracked Batch 4 files listed by `git status --short` while excluding the preserved P1 test and pre-existing documents from the implementation verdict.

  Require the reviewer to inspect:

  - seven owner responsibilities and dependency direction;
  - façade purity, absence of `__all__`, reflection compatibility, and both import orders;
  - exactly seven Tool decorators and exact Tool/func/coroutine identity/schema;
  - path, Sub-Agent output, Skill/ACP/custom-mount, and error-masking behavior;
  - sync/async initialization and authorization-before-thread-dispatch order;
  - Host Execution Plan coordinates, Secret scanning/clearing, public runner imports, claim/spawn/complete counts, and retry safety;
  - Bash mask-local-paths then mask-Secrets then truncate order;
  - file read/write/replace behavior and lock scope;
  - test monkeypatch ownership, current validation evidence, and scope exclusions.

  Resolve every Critical or Important finding, assess every Minor finding, and rerun the smallest affected focused group plus final structural gates before handoff.

- [ ] **Step 8: documentation checkpoint**

  If explicit local commits are authorized, commit only `backend/AGENTS.md` with message `docs(sandbox): document tooling owners`. Otherwise leave it unstaged and report its exact diff.

## Completion Criteria

- `deerflow.sandbox.tools` is a long-lived imports-only façade with no `__all__`, implementation, wrapper, decorator, coroutine binding, or runtime side effect.
- The seven `tooling` modules own exactly the responsibilities and dependency direction specified above; none imports the façade, `app.*`, SQLAlchemy, Worker execution core, or executor code.
- Exactly seven LangChain Tools are decorated once. Legacy and owner Tool, `.func`, `.coroutine`, name, parameters, call schema, description, and reflection-path behavior remain exact.
- `path_mapping.py` retains exact Skill/ACP/custom-mount authority, Sub-Agent output isolation, traversal rejection, host-path masking, and cache objects.
- `bash_policy.py` retains exact command tokenization, URL/brace handling, root/cwd restrictions, virtual-path rewriting, and messages while `sandbox.security` remains policy authority.
- `runtime.py` retains sync/async provider selection, Run-mount acquisition, state/context updates, private-directory protection, async initialization, one authorization check, then one off-thread invocation.
- `host_execution.py` retains exact identity/environment/Skill-Secret projection, Plan construction, Approval staging, Secret clearing, masking, and truncation; the runner imports public owner APIs and retains one-time spawn/settlement behavior.
- `files.py` retains exact ls/read/write/replace behavior, byte and output bounds, requested-path errors, local masking, and file-lock scopes. `search_tools.py` retains exact glob/grep limits, arguments, formatting, masking, and delegated filtering.
- `bash.py` retains execution-mode precedence, approval/direct paths, command/env/cwd behavior, Secret readiness and clearing, and path-mask then Secret-mask then truncation order.
- All seven internal production consumers import owners. Published config, wizard, frontend documentation, replay fixtures, and Host Bash legacy-string classification remain on `deerflow.sandbox.tools:<tool>`.
- Tests move only with corresponding owners and add focused characterization; no existing large test file or test framework is reorganized.
- Focused, selected PostgreSQL, import-order, reflection, dependency, Ruff, blocking-I/O, and full backend zero-skip gates have current passing evidence.
- The pre-existing P1 test change and four earlier untracked documents remain separately attributable and are not staged or committed as Batch 4 work without explicit authorization.

## Execution Handoff

Plan approval does not start implementation. After approval, choose one:

1. **Subagent-Driven (recommended):** execute Tasks 1-9 sequentially in this existing worktree, with a fresh implementation agent and independent specification/code-quality review per task.
2. **Inline Execution:** execute the same tasks with `superpowers:executing-plans` and the same checkpoints.

Do not create another branch or worktree unless the user explicitly changes the existing-worktree requirement.
