# Python Module Decomposition Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the compatibility gates from Batch 0 and split the low-risk contracts, codec, validation, Skill package integrity, and Provider Request layers from Batch 1 without changing behavior.

**Architecture:** Existing modules remain compatibility façades and re-export the exact objects owned by new leaf modules. Pure data and calculation move first; application transactions, Builder lifecycle orchestration, Router composition, Worker execution, and database ownership remain in their current modules.

**Tech Stack:** Python 3.12+, dataclasses, Pydantic-compatible DTOs, FastAPI, LangChain/LangGraph, SQLAlchemy, pytest, Ruff, GNU Make.

**Spec:** `docs/superpowers/specs/2026-09-02-python-module-decomposition-design.md`

## Global Constraints

- Start from commit `0d421ede2d52a2cf22a5c8fedfdfbb10e6e1394c` or stop and re-audit every referenced line and fingerprint against the new HEAD.
- At execution time, create an isolated worktree with `superpowers:using-git-worktrees` unless the user explicitly chooses the saved checkout.
- The approved spec and this plan are currently uncommitted; before isolated execution, either obtain authorization for a docs-only commit or use an explicitly approved working-tree starting state so both files travel with the task.
- Preserve unrelated user changes; never reset, restore, stage, commit, or push them.
- Commit steps in this plan are conditional: run them only after explicit user authorization. Without authorization, run `git status --short` and leave changes unstaged.
- Preserve the dependency direction `app.* -> deerflow.*`; Harness must not import `app.*`; `actweave_knowledge` must not import `app.*` or `deerflow.*`.
- Do not change HTTP contracts, database Schema, error codes, error text, JSON representations, Provider fingerprints, Tool identities, lock order, transaction ownership, or process responsibilities.
- Gateway never executes an Agent Graph; Worker remains the sole executor; Scheduler remains admission-only.
- A compatibility façade must re-export the owning object; it must not duplicate implementation, decorate a second Tool, create a wrapper class, or create a second Router.
- Keep `SkillDesignService` and `AgentDesignService` lifecycle, generation, Commit, Cancel, and transaction methods in their current modules in this plan.
- Tests may be added or migrated only to protect the production modules moved by this plan; do not perform general test-file cleanup.
- Every task ends with focused verification. Every commit step is a reviewer gate and may be omitted only because commit authorization is absent, not because tests were skipped.

## File Map

| Path | Responsibility after this plan |
| --- | --- |
| `backend/tests/test_python_module_decomposition_contract.py` | Stable import, Router, Tool, dependency-direction, and façade identity gates |
| `backend/app/shared_assets/skill_package_integrity.py` | Skill package DTOs, normalization, file changes, checksums, archive analysis, and persisted-file integrity |
| `backend/app/shared_assets/skill_design_contracts.py` | Immutable Skill Design commands, statuses, turns, and views |
| `backend/app/shared_assets/skill_design_codec.py` | Skill Design JSON/checksum/clarification/validation codecs with no Service import |
| `backend/app/shared_assets/skill_design_validation.py` | Skill Design command and Draft validation with no Service import |
| `backend/app/shared_assets/agent_design_contracts.py` | Immutable Agent Design commands, statuses, turns, blueprint, and views |
| `backend/app/shared_assets/agent_design_codec.py` | Agent Design blueprint, JSON, cursor, clarification, and view codecs |
| `backend/app/shared_assets/agent_design_validation.py` | Agent Design command, slug, capability, and blueprint validation |
| `backend/packages/harness/deerflow/agents/middlewares/provider_request_profile.py` | Provider request contracts, canonicalization, profile construction, and fingerprinting |
| `backend/packages/harness/deerflow/agents/middlewares/provider_request_measurement.py` | Frozen-profile Context measurement from graph state |
| `backend/packages/harness/deerflow/agents/middlewares/provider_request_guard.py` | Provider dispatch Evidence lifecycle and final request guard |
| Existing `*_service.py` and `provider_request_usage.py` modules | Compatibility façades plus lifecycle/orchestration that is deliberately not moved yet |

---

### Task 1: Freeze Batch 0 compatibility and dependency contracts

**Files:**
- Create: `backend/tests/test_python_module_decomposition_contract.py`

**Interfaces:**
- Consumes: current `private_work.router`, `project_assets.project_router`, `project_assets.catalog_router`, Builder `__all__`, Provider Request `__all__`, and Sandbox Tool objects.
- Produces: stable manifest digests and reusable compatibility assertions for every later task.

- [ ] **Step 1: Add the characterization test file**

Create the file with these exact baseline values and helpers:

```python
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from fastapi.routing import APIRoute

from app.gateway.routers.private_work import router as private_work_router
from app.gateway.routers.project_assets import catalog_router, project_router
from app.shared_assets import agent_design_service, skill_design_service
from deerflow.agents.middlewares import provider_request_usage
from deerflow.sandbox import tools as sandbox_tools

BACKEND_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_ROUTE_DIGESTS = {
    "private_work": (45, "7867b20667bd2ccd1d5934b6db162a05efe54d8ed0e2637b58e0ed76098b7ca3"),
    "project_assets": (58, "66a88150e12038d66e561a1577456ddb3630e1f3263b31ab77a43f5aaf4d28b6"),
    "asset_catalog": (5, "2bf16801b2f52d284dee015b4b2ade6d15df02ad695816766c94e4cc3fb12a8d"),
}

EXPECTED_EXPORT_DIGESTS = {
    "skill_design": (22, "b9e7397e798f62e2ba3c2c2e58f48939c661c7e937a2c3d687ad321035affed6"),
    "agent_design": (28, "cb915819337a8b5c5ed19d6483393f1e44d68c9bc1b4aa30915138b3fad2f55c"),
    "provider_request": (33, "d0ed55a5319db01842783940394121132548c91b2e537ff0901b41b23d12a030"),
}


def _callable_name(value: object) -> str:
    module = getattr(value, "__module__", "")
    name = getattr(value, "__qualname__", getattr(value, "__name__", type(value).__name__))
    return f"{module}:{name}"


def _response_name(value: object) -> str | None:
    if value is None:
        return None
    return _callable_name(value)


def _route_digest(router) -> tuple[int, str]:
    rows: list[dict[str, object]] = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        rows.append(
            {
                "methods": sorted(route.methods or ()),
                "path": route.path,
                "name": route.name,
                "status_code": route.status_code,
                "response_model": _response_name(route.response_model),
                "route_class": _callable_name(type(route)),
                "dependencies": sorted(
                    _callable_name(getattr(dependency, "call", None))
                    for dependency in route.dependant.dependencies
                ),
            }
        )
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return len(rows), hashlib.sha256(encoded).hexdigest()


def _export_digest(module) -> tuple[int, str]:
    names = tuple(module.__all__)
    encoded = json.dumps(names, separators=(",", ":")).encode()
    return len(names), hashlib.sha256(encoded).hexdigest()


def _absolute_imports(root: Path) -> set[str]:
    imports: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module)
    return imports


def test_router_manifests_match_the_pre_split_baseline() -> None:
    assert _route_digest(private_work_router) == EXPECTED_ROUTE_DIGESTS["private_work"]
    assert _route_digest(project_router) == EXPECTED_ROUTE_DIGESTS["project_assets"]
    assert _route_digest(catalog_router) == EXPECTED_ROUTE_DIGESTS["asset_catalog"]


def test_public_export_inventories_match_the_pre_split_baseline() -> None:
    assert _export_digest(skill_design_service) == EXPECTED_EXPORT_DIGESTS["skill_design"]
    assert _export_digest(agent_design_service) == EXPECTED_EXPORT_DIGESTS["agent_design"]
    assert _export_digest(provider_request_usage) == EXPECTED_EXPORT_DIGESTS["provider_request"]


def test_sandbox_tools_keep_their_public_shapes() -> None:
    expected = {
        "bash_tool": "bash",
        "ls_tool": "ls",
        "glob_tool": "glob",
        "grep_tool": "grep",
        "read_file_tool": "read_file",
        "write_file_tool": "write_file",
        "str_replace_tool": "str_replace",
    }
    for attribute, tool_name in expected.items():
        tool = getattr(sandbox_tools, attribute)
        assert tool.name == tool_name
        assert callable(tool.func)
        assert callable(tool.coroutine)


def test_package_dependency_direction_is_one_way() -> None:
    harness_imports = _absolute_imports(BACKEND_ROOT / "packages" / "harness" / "deerflow")
    assert not {name for name in harness_imports if name == "app" or name.startswith("app.")}

    knowledge_imports = _absolute_imports(BACKEND_ROOT / "packages" / "knowledge" / "actweave_knowledge")
    forbidden = {
        name
        for name in knowledge_imports
        if name == "app"
        or name.startswith("app.")
        or name == "deerflow"
        or name.startswith("deerflow.")
    }
    assert not forbidden
```

- [ ] **Step 2: Run the characterization tests on the untouched baseline**

Run:

```bash
cd backend
PYTHONPATH=. uv run pytest tests/test_python_module_decomposition_contract.py -q
```

Expected: `4 passed`. These tests characterize existing behavior and therefore pass before production movement.

- [ ] **Step 3: Run the existing focused baseline**

Run:

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_skill_builder_execution_options.py \
  tests/test_skill_builder_draft_projection.py \
  tests/test_agent_builder_safety_lifecycle.py \
  tests/test_provider_request_usage.py \
  tests/test_sandbox_tools_security.py -q
```

Expected: all selected tests pass. Record exact counts in the execution log.

- [ ] **Step 4: Commit only if explicitly authorized**

```bash
git add backend/tests/test_python_module_decomposition_contract.py
git commit -m "test: freeze python module decomposition contracts"
```

Without commit authorization, run `git status --short` and leave the file unstaged.

---

### Task 2: Extract Skill package DTOs and integrity implementation

**Files:**
- Create: `backend/app/shared_assets/skill_package_integrity.py`
- Modify: `backend/app/shared_assets/skill_service.py:73-596`
- Modify: `backend/app/shared_assets/skill_service.py:2020-2140`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Test: `backend/tests/test_system_asset_bootstrap_exact_history.py`
- Test: `backend/tests/test_skill_distribution_export.py`
- Test: `backend/tests/test_skill_builder_draft_projection.py`

**Interfaces:**
- Consumes: `SkillArchiveFile`, `SkillVersionRecord`, `SkillVersionFileMetadataRecord`, Skill frontmatter parser/validator, and current archive size limits.
- Produces: `SkillFileView`, `SkillFileContentView`, `SkillFileChange`, `SkillSecretRequirementView`, `SkillArchivePreview`, `SkillDraftSnapshot`, `normalize_skill_files(...)`, `analyze_skill_files(...)`, and `verified_archive_files(...)`.

- [ ] **Step 1: Add a failing owning-module identity test**

Append:

```python
def test_skill_package_integrity_is_the_owning_module() -> None:
    from app.shared_assets import skill_package_integrity as owning
    from app.shared_assets import skill_service as legacy

    exact_names = (
        "SkillFileView",
        "SkillFileContentView",
        "SkillFileChange",
        "SkillSecretRequirementView",
        "SkillArchivePreview",
        "SkillDraftSnapshot",
        "normalize_skill_files",
    )
    for name in exact_names:
        assert getattr(legacy, name) is getattr(owning, name)
    assert legacy._analyze_skill_files is owning.analyze_skill_files
    assert legacy.SkillService._verified_archive_files is owning.verified_archive_files
```

- [ ] **Step 2: Run the new test and observe the expected failure**

Run:

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_skill_package_integrity_is_the_owning_module -q
```

Expected: collection or execution fails with `ImportError` because `skill_package_integrity.py` does not exist.

- [ ] **Step 3: Move the exact Skill package definitions**

Move these declarations without changing field order, default values, exception types, text, or calculation bodies:

```text
Constants:
  MAX_SKILL_TEXT_PREVIEW_BYTES
  MAX_SKILL_EDIT_TEXT_BYTES
  MAX_SKILL_FILE_CHANGES
  _SYMLINK_MEDIA_TYPES
  _WIN32_INVALID_SEGMENT_CHARS
  _WIN32_RESERVED_BASENAMES
  _EXECUTABLE_MEDIA_TYPES

DTOs:
  SkillFileView
  SkillFileContentView
  SkillFileChange
  SkillSecretRequirementView
  SkillArchivePreview
  SkillDraftSnapshot

Functions:
  _validate_archive_file
  normalize_skill_files
  _canonical_skill_path
  _validate_file_changes
  _apply_file_changes
  _decode_preview_content
  _file_views
  _snapshot_checksum
  _snapshot_checksum_for_files
  _preflight_skill_frontmatter
  _analyze_skill_files
  _archive_files
  _verified_archive_files
  _metadata_file_views
```

End the new module with exact public aliases:

```python
analyze_skill_files = _analyze_skill_files
verified_archive_files = _verified_archive_files

__all__ = [
    "MAX_SKILL_EDIT_TEXT_BYTES",
    "MAX_SKILL_FILE_CHANGES",
    "MAX_SKILL_TEXT_PREVIEW_BYTES",
    "SkillArchivePreview",
    "SkillDraftSnapshot",
    "SkillFileChange",
    "SkillFileContentView",
    "SkillFileView",
    "SkillSecretRequirementView",
    "analyze_skill_files",
    "normalize_skill_files",
    "verified_archive_files",
]
```

- [ ] **Step 4: Convert `skill_service.py` into the compatibility entry for moved names**

Import the moved symbols and retain the old names:

```python
from app.shared_assets.skill_package_integrity import (
    MAX_SKILL_EDIT_TEXT_BYTES,
    MAX_SKILL_FILE_CHANGES,
    MAX_SKILL_TEXT_PREVIEW_BYTES,
    SkillArchivePreview,
    SkillDraftSnapshot,
    SkillFileChange,
    SkillFileContentView,
    SkillFileView,
    SkillSecretRequirementView,
    _analyze_skill_files,
    _apply_file_changes,
    _archive_files,
    _decode_preview_content,
    _file_views,
    _metadata_file_views,
    _snapshot_checksum,
    _snapshot_checksum_for_files,
    analyze_skill_files,
    normalize_skill_files,
    verified_archive_files,
)
```

Replace the three moved static-method bodies with aliases inside `SkillService`:

```python
_archive_files = staticmethod(_archive_files)
_verified_archive_files = staticmethod(verified_archive_files)
_metadata_file_views = staticmethod(_metadata_file_views)
```

Ensure the legacy module-level `_analyze_skill_files` references the same object as `analyze_skill_files`.

- [ ] **Step 5: Run identity and focused behavior tests**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_system_asset_bootstrap_exact_history.py \
  tests/test_skill_distribution_export.py \
  tests/test_skill_builder_draft_projection.py -q
```

Expected: all tests pass; `SkillService` remains the lifecycle owner.

- [ ] **Step 6: Run Ruff on the two production modules**

```bash
cd backend
uvx ruff check app/shared_assets/skill_package_integrity.py app/shared_assets/skill_service.py
uvx ruff format --check app/shared_assets/skill_package_integrity.py app/shared_assets/skill_service.py
```

Expected: both commands exit 0.

- [ ] **Step 7: Commit only if explicitly authorized**

```bash
git add \
  backend/app/shared_assets/skill_package_integrity.py \
  backend/app/shared_assets/skill_service.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(skills): extract package integrity"
```

---

### Task 3: Migrate production callers to Skill package public seams

**Files:**
- Modify: `backend/app/shared_assets/bootstrap/service.py`
- Modify: `backend/app/shared_assets/catalog_provider.py`
- Modify: `backend/app/shared_assets/resolver.py`
- Modify: `backend/app/shared_assets/skill_design_service.py`
- Modify: `backend/app/private_work/legacy_run_skill_snapshot_writer.py`
- Modify: `backend/scripts/generate_public_system_skill_catalog.py`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`

**Interfaces:**
- Consumes: `analyze_skill_files(...)`, `normalize_skill_files(...)`, and `verified_archive_files(...)` from Task 2.
- Produces: no production call to `SkillService._verified_archive_files` and no active bootstrap/catalog call to `_analyze_skill_files`.

- [ ] **Step 1: Add a failing source-ownership test**

Append:

```python
def test_production_consumers_use_skill_package_integrity_owner() -> None:
    paths = (
        BACKEND_ROOT / "app/shared_assets/bootstrap/service.py",
        BACKEND_ROOT / "app/shared_assets/catalog_provider.py",
        BACKEND_ROOT / "app/shared_assets/resolver.py",
        BACKEND_ROOT / "app/shared_assets/skill_design_service.py",
        BACKEND_ROOT / "app/private_work/legacy_run_skill_snapshot_writer.py",
        BACKEND_ROOT / "scripts/generate_public_system_skill_catalog.py",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "SkillService._verified_archive_files" not in source
        assert "_analyze_skill_files" not in source
```

- [ ] **Step 2: Run the test and observe the expected failure**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_production_consumers_use_skill_package_integrity_owner -q
```

Expected: FAIL because current production consumers still use the private Service seam.

- [ ] **Step 3: Replace private Service calls with owning-module imports**

Use this exact ownership mapping:

```text
bootstrap/service.py:                    analyze_skill_files, normalize_skill_files
catalog_provider.py:                     verified_archive_files
resolver.py:                             verified_archive_files
skill_design_service.py:                 verified_archive_files
legacy_run_skill_snapshot_writer.py:     verified_archive_files
generate_public_system_skill_catalog.py: analyze_skill_files
```

Import only the names assigned to each file from this module:

```python
from app.shared_assets.skill_package_integrity import (
    analyze_skill_files,
    normalize_skill_files,
    verified_archive_files,
)
```

Apply only the required substitutions:

```text
SkillService._verified_archive_files(...) -> verified_archive_files(...)
_analyze_skill_files(...)                 -> analyze_skill_files(...)
```

Do not change call arguments, exception handling, transaction boundaries, or returned values.

- [ ] **Step 4: Verify no prohibited production reference remains**

```bash
rg -n 'SkillService\._verified_archive_files|skill_service import _analyze_skill_files' \
  backend/app backend/scripts/generate_public_system_skill_catalog.py
```

Expected: no output.

- [ ] **Step 5: Run catalog and focused tests**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_system_asset_bootstrap_exact_history.py \
  tests/test_legacy_run_skill_snapshot_writer.py \
  tests/test_skill_builder_revision_contract.py -q
PYTHONPATH=. uv run python -m scripts.generate_public_system_skill_catalog --check
```

Expected: tests pass and the catalog check exits 0 without rewriting files.

- [ ] **Step 6: Commit only if explicitly authorized**

```bash
git add \
  backend/app/shared_assets/bootstrap/service.py \
  backend/app/shared_assets/catalog_provider.py \
  backend/app/shared_assets/resolver.py \
  backend/app/shared_assets/skill_design_service.py \
  backend/app/private_work/legacy_run_skill_snapshot_writer.py \
  backend/scripts/generate_public_system_skill_catalog.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(skills): use package integrity owner"
```

---

### Task 4: Extract Skill Design immutable contracts

**Files:**
- Create: `backend/app/shared_assets/skill_design_contracts.py`
- Modify: `backend/app/shared_assets/skill_design_service.py:159-416`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Test: `backend/tests/test_skill_builder_revision_contract.py`
- Test: `backend/tests/test_skill_builder_session_summary.py`

**Interfaces:**
- Consumes: `SkillFileChange` from `skill_package_integrity`, `SkillAssetView`/`SkillVersionView`, `SkillBuilderRunAdmission`, and `SkillBuilderDependencySnapshot`.
- Produces: the exact immutable Skill Design types listed below; the legacy service module re-exports the same objects.

- [ ] **Step 1: Add a failing contract identity test**

Append:

```python
def test_skill_design_contracts_are_exact_reexports() -> None:
    from app.shared_assets import skill_design_contracts as owning
    from app.shared_assets import skill_design_service as legacy

    names = (
        "SkillDesignStatus",
        "SkillDesignProgressStatus",
        "SkillDesignServiceErrorCode",
        "CreateSkillDesignSession",
        "CreateSkillDesignRevisionSession",
        "SkillDesignMessage",
        "SkillDesignProgressItem",
        "SkillDesignClarificationOption",
        "SkillDesignClarificationRequest",
        "SkillDesignClarificationResponse",
        "SkillDesignTurnAttachment",
        "SkillDesignMessageTurn",
        "SkillDesignClarificationTurn",
        "SkillDesignDraftUpdateTurn",
        "SkillDesignTurn",
        "SubmitSkillDesignTurn",
        "ValidateSkillDesignSession",
        "CommitSkillDesignSession",
        "CancelSkillDesignSession",
        "SetSkillDesignExecutionPreference",
        "SkillDesignExecutionPreference",
        "SkillDesignFileView",
        "SkillDesignBaseFile",
        "SkillDesignSecretRequirement",
        "SkillDesignValidation",
        "SkillDesignSessionView",
        "SkillDesignSessionSummary",
        "SkillDesignCommitResult",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owning, name)
```

- [ ] **Step 2: Run the identity test and observe the expected failure**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_skill_design_contracts_are_exact_reexports -q
```

Expected: FAIL with `ImportError` because `skill_design_contracts.py` is absent.

- [ ] **Step 3: Move the exact declarations into `skill_design_contracts.py`**

Move every type named in the test, preserving dataclass decorators, slot/frozen settings, union order, field order, defaults, and annotations. The new module imports `SkillFileChange` from `skill_package_integrity`; it must not import `skill_design_service`.

Define `__all__` with the same `names` tuple from the test. Do not move `_RepositoryFactory`, `_SkillDesignTerminalSink`, `_BuilderToolTransaction`, `_SkillBuilderDraftState`, or `SkillDesignService`.

- [ ] **Step 4: Re-export the owning types from `skill_design_service.py`**

Replace the removed declarations with one explicit import block:

```python
from app.shared_assets.skill_design_contracts import (
    CancelSkillDesignSession,
    CommitSkillDesignSession,
    CreateSkillDesignRevisionSession,
    CreateSkillDesignSession,
    SetSkillDesignExecutionPreference,
    SkillDesignBaseFile,
    SkillDesignClarificationOption,
    SkillDesignClarificationRequest,
    SkillDesignClarificationResponse,
    SkillDesignClarificationTurn,
    SkillDesignCommitResult,
    SkillDesignDraftUpdateTurn,
    SkillDesignExecutionPreference,
    SkillDesignFileView,
    SkillDesignMessage,
    SkillDesignMessageTurn,
    SkillDesignProgressItem,
    SkillDesignProgressStatus,
    SkillDesignSecretRequirement,
    SkillDesignServiceErrorCode,
    SkillDesignSessionSummary,
    SkillDesignSessionView,
    SkillDesignStatus,
    SkillDesignTurn,
    SkillDesignTurnAttachment,
    SkillDesignValidation,
    SubmitSkillDesignTurn,
    ValidateSkillDesignSession,
)
```

Keep the existing legacy `__all__` digest unchanged.

- [ ] **Step 5: Run contract-focused tests**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_skill_builder_revision_contract.py \
  tests/test_skill_builder_session_summary.py \
  tests/test_skill_builder_execution_options.py -q
```

Expected: all tests pass and the legacy export digest remains `b9e7397e...`.

- [ ] **Step 6: Commit only if explicitly authorized**

```bash
git add \
  backend/app/shared_assets/skill_design_contracts.py \
  backend/app/shared_assets/skill_design_service.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(skill-builder): extract design contracts"
```

---

### Task 5: Extract Skill Design codec functions

**Files:**
- Create: `backend/app/shared_assets/skill_design_codec.py`
- Modify: `backend/app/shared_assets/skill_design_service.py:4177-4216`
- Modify: `backend/app/shared_assets/skill_design_service.py:4299-4527`
- Modify: `backend/app/shared_assets/skill_design_service.py:4662-4749`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`

**Interfaces:**
- Consumes: Skill Design contracts, `SkillArchivePreview`, generation `ClarificationQuestion`, persisted Session rows, and existing errors.
- Produces: exact functions `_conversation_brief`, `_validation_from_preview`, `_validation_matches_preview`, `_validation_json`, `_validation_from_json`, `_message_json`, `_progress_json`, `_clarification_request`, `_clarification_json`, `_clarification_from_json`, `_session_summary`, `_idempotency_hash`, `_request_checksum`, `_jsonable`, and `_stable_generation_error_message`.

- [ ] **Step 1: Add a failing static-function identity test**

Append:

```python
def test_skill_design_codec_is_the_owning_module() -> None:
    from app.shared_assets import skill_design_codec as owning
    from app.shared_assets.skill_design_service import SkillDesignService

    names = (
        "_conversation_brief",
        "_validation_from_preview",
        "_validation_matches_preview",
        "_validation_json",
        "_validation_from_json",
        "_message_json",
        "_progress_json",
        "_clarification_request",
        "_clarification_json",
        "_clarification_from_json",
        "_session_summary",
        "_idempotency_hash",
        "_request_checksum",
        "_jsonable",
        "_stable_generation_error_message",
    )
    for name in names:
        assert SkillDesignService.__dict__[name].__func__ is getattr(owning, name)
```

- [ ] **Step 2: Run the identity test and observe the expected failure**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_skill_design_codec_is_the_owning_module -q
```

Expected: FAIL because `skill_design_codec.py` does not exist.

- [ ] **Step 3: Move codec bodies without importing the Service**

Move the named functions as module-level functions. Replace internal references such as:

```python
SkillDesignService._jsonable(value)
```

with:

```python
_jsonable(value)
```

The new module must not import `skill_design_service`. Keep `_session_view`, `_file_views`, `_candidate_files`, Draft checksum helpers, and persistence mutation helpers in the Service for later batches.

- [ ] **Step 4: Alias codec functions on `SkillDesignService`**

Import each owning function with an `_impl` suffix and replace the former body with an exact static alias:

```python
from app.shared_assets.skill_design_codec import _jsonable as _jsonable_impl

class SkillDesignService:
    _jsonable = staticmethod(_jsonable_impl)
```

Use this complete alias block after importing each owning function with the `_impl` suffix:

```python
_conversation_brief = staticmethod(_conversation_brief_impl)
_validation_from_preview = staticmethod(_validation_from_preview_impl)
_validation_matches_preview = staticmethod(_validation_matches_preview_impl)
_validation_json = staticmethod(_validation_json_impl)
_validation_from_json = staticmethod(_validation_from_json_impl)
_message_json = staticmethod(_message_json_impl)
_progress_json = staticmethod(_progress_json_impl)
_clarification_request = staticmethod(_clarification_request_impl)
_clarification_json = staticmethod(_clarification_json_impl)
_clarification_from_json = staticmethod(_clarification_from_json_impl)
_session_summary = staticmethod(_session_summary_impl)
_idempotency_hash = staticmethod(_idempotency_hash_impl)
_request_checksum = staticmethod(_request_checksum_impl)
_jsonable = staticmethod(_jsonable_impl)
_stable_generation_error_message = staticmethod(_stable_generation_error_message_impl)
```

- [ ] **Step 5: Prove the leaf module has no façade import**

```bash
rg -n 'skill_design_service' backend/app/shared_assets/skill_design_codec.py
```

Expected: no output.

- [ ] **Step 6: Run focused tests**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_skill_builder_session_summary.py \
  tests/test_skill_builder_design_record.py \
  tests/test_skill_builder_draft_projection.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit only if explicitly authorized**

```bash
git add \
  backend/app/shared_assets/skill_design_codec.py \
  backend/app/shared_assets/skill_design_service.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(skill-builder): extract design codec"
```

---

### Task 6: Extract Skill Design validation functions

**Files:**
- Create: `backend/app/shared_assets/skill_design_validation.py`
- Modify: `backend/app/shared_assets/skill_design_service.py:119-130`
- Modify: `backend/app/shared_assets/skill_design_service.py:3687-4174`
- Modify: `backend/app/shared_assets/skill_design_service.py:4219-4296`
- Modify: `backend/app/shared_assets/skill_design_service.py:4676-4712`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`

**Interfaces:**
- Consumes: Skill Design contracts, Skill Design codec, Skill package integrity, Project capability/context, Design rows, and current error types.
- Produces: command validation and Draft validation while preserving every `SkillDesignService._validate_*` and `_require_*` static seam.

- [ ] **Step 1: Add a failing validation-owner test**

Append:

```python
def test_skill_design_validation_is_the_owning_module() -> None:
    from app.shared_assets import skill_design_validation as owning
    from app.shared_assets.skill_design_service import SkillDesignService

    names = (
        "_validate_create",
        "_validate_create_revision",
        "_validate_turn",
        "_validate_execution_preference",
        "_validate_turn_model_name",
        "_validate_turn_reasoning_effort",
        "_validate_turn_attachments",
        "_validate_validation",
        "_validate_commit",
        "_validate_cancel",
        "_require_context",
        "_require_capability",
        "_require_nonterminal",
        "_require_revise_target_live",
        "_require_expected_revision",
        "_require_matching_operation",
        "_require_message_capacity",
        "_require_matching_clarification_response",
        "_candidate_files",
        "_validate_builder_files",
        "_validate_partial_builder_files",
        "_require_preview_name",
        "_valid_revision",
        "_validate_uuid",
        "_validate_idempotency_key",
        "_bounded_text",
    )
    for name in names:
        assert SkillDesignService.__dict__[name].__func__ is getattr(owning, name)
```

- [ ] **Step 2: Run the new test and observe the expected failure**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_skill_design_validation_is_the_owning_module -q
```

Expected: FAIL because `skill_design_validation.py` does not exist.

- [ ] **Step 3: Move validation constants and functions**

Move the exact functions in the test plus `_SLUG_PATTERN`, `_CHECKSUM_PATTERN`, `_MAX_IDEMPOTENCY_KEY_CHARS`, `_MAX_MESSAGE_CHARS`, `_MAX_SESSION_MESSAGES`, `_MAX_DISPLAY_NAME_CHARS`, `_MAX_BUILDER_FILES`, `_MAX_BUILDER_FILE_BYTES`, and `_MAX_BUILDER_TOTAL_BYTES`. Replace all internal `SkillDesignService._...` calls with local functions, and import `_jsonable` plus `_clarification_from_json` from `skill_design_codec`.

Do not move `_append_turn_input`, `_append_row_message`, `_session_view`, `_file_views`, lifecycle methods, repository calls, or transaction code.

- [ ] **Step 4: Alias every moved function back onto the Service**

Use explicit static aliases, following this exact form:

```python
from app.shared_assets.skill_design_validation import _validate_turn as _validate_turn_impl

class SkillDesignService:
    _validate_turn = staticmethod(_validate_turn_impl)
```

Use this complete alias block after importing each function with the `_impl` suffix:

```python
_validate_create = staticmethod(_validate_create_impl)
_validate_create_revision = staticmethod(_validate_create_revision_impl)
_validate_turn = staticmethod(_validate_turn_impl)
_validate_execution_preference = staticmethod(_validate_execution_preference_impl)
_validate_turn_model_name = staticmethod(_validate_turn_model_name_impl)
_validate_turn_reasoning_effort = staticmethod(_validate_turn_reasoning_effort_impl)
_validate_turn_attachments = staticmethod(_validate_turn_attachments_impl)
_validate_validation = staticmethod(_validate_validation_impl)
_validate_commit = staticmethod(_validate_commit_impl)
_validate_cancel = staticmethod(_validate_cancel_impl)
_require_context = staticmethod(_require_context_impl)
_require_capability = staticmethod(_require_capability_impl)
_require_nonterminal = staticmethod(_require_nonterminal_impl)
_require_revise_target_live = staticmethod(_require_revise_target_live_impl)
_require_expected_revision = staticmethod(_require_expected_revision_impl)
_require_matching_operation = staticmethod(_require_matching_operation_impl)
_require_message_capacity = staticmethod(_require_message_capacity_impl)
_require_matching_clarification_response = staticmethod(_require_matching_clarification_response_impl)
_candidate_files = staticmethod(_candidate_files_impl)
_validate_builder_files = staticmethod(_validate_builder_files_impl)
_validate_partial_builder_files = staticmethod(_validate_partial_builder_files_impl)
_require_preview_name = staticmethod(_require_preview_name_impl)
_valid_revision = staticmethod(_valid_revision_impl)
_validate_uuid = staticmethod(_validate_uuid_impl)
_validate_idempotency_key = staticmethod(_validate_idempotency_key_impl)
_bounded_text = staticmethod(_bounded_text_impl)
```

- [ ] **Step 5: Prove the leaf module does not import its façade**

```bash
rg -n 'skill_design_service' backend/app/shared_assets/skill_design_validation.py
```

Expected: no output.

- [ ] **Step 6: Run Skill Builder validation tests**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_skill_builder_execution_options.py \
  tests/test_skill_builder_draft_projection.py \
  tests/test_skill_builder_revision_contract.py -q
```

Expected: all tests pass with unchanged validation exceptions.

- [ ] **Step 7: Commit only if explicitly authorized**

```bash
git add \
  backend/app/shared_assets/skill_design_validation.py \
  backend/app/shared_assets/skill_design_service.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(skill-builder): extract design validation"
```

---

### Task 7: Extract Agent Design immutable contracts

**Files:**
- Create: `backend/app/shared_assets/agent_design_contracts.py`
- Modify: `backend/app/shared_assets/agent_design_service.py:151-348`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Test: `backend/tests/test_agent_builder_contract_version.py`
- Test: `backend/tests/test_agent_builder_safety_lifecycle.py`

**Interfaces:**
- Consumes: `AgentModelSettings`, `SkillAssetRef`, generation conflict contracts, and Agent Service result views.
- Produces: exact immutable Agent Design DTOs and enums; the legacy service module re-exports the same objects.

- [ ] **Step 1: Add a failing contract identity test**

Append:

```python
def test_agent_design_contracts_are_exact_reexports() -> None:
    from app.shared_assets import agent_design_contracts as owning
    from app.shared_assets import agent_design_service as legacy

    names = (
        "AgentDesignStatus",
        "AgentDesignProgressStatus",
        "AgentDesignServiceErrorCode",
        "CreateAgentDesignSession",
        "AgentDesignBlueprint",
        "AgentDesignMessage",
        "AgentDesignProgressItem",
        "AgentDesignClarificationOption",
        "AgentDesignClarificationRequest",
        "AgentDesignClarificationResponse",
        "AgentDesignMessageTurn",
        "AgentDesignClarificationTurn",
        "AgentDesignBlueprintTurn",
        "AgentDesignTurn",
        "SubmitAgentDesignTurn",
        "SetAgentDesignGenerationPreference",
        "CommitAgentDesignSession",
        "CancelAgentDesignSession",
        "AgentDesignSessionView",
        "AgentDesignSessionSummary",
        "AgentDesignSessionPage",
        "AgentDesignCommitResult",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owning, name)
```

- [ ] **Step 2: Run the identity test and observe the expected failure**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_agent_design_contracts_are_exact_reexports -q
```

Expected: FAIL because `agent_design_contracts.py` does not exist.

- [ ] **Step 3: Move the exact declarations and re-export them**

Move every type in the identity test, preserving field order, defaults, dataclass options, and union order. Define the new module's `__all__` with the same names. Import the types explicitly into `agent_design_service.py`; keep the legacy `__all__` digest unchanged.

Do not move `_RepositoryFactory`, `AgentDesignService`, generation control, or Service constants in this task.

- [ ] **Step 4: Run contract-focused tests**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_agent_builder_contract_version.py \
  tests/test_agent_builder_safety_lifecycle.py \
  tests/test_agent_builder_model_selection.py -q
```

Expected: all tests pass and the legacy export digest remains `cb915819...`.

- [ ] **Step 5: Commit only if explicitly authorized**

```bash
git add \
  backend/app/shared_assets/agent_design_contracts.py \
  backend/app/shared_assets/agent_design_service.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(agent-builder): extract design contracts"
```

---

### Task 8: Extract Agent Design codec functions

**Files:**
- Create: `backend/app/shared_assets/agent_design_codec.py`
- Modify: `backend/app/shared_assets/agent_design_service.py:1341-1350`
- Modify: `backend/app/shared_assets/agent_design_service.py:2793-2806`
- Modify: `backend/app/shared_assets/agent_design_service.py:2852-3323`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`

**Interfaces:**
- Consumes: Agent Design contracts, generation conflicts/clarifications, Agent payload contracts, persisted Agent Design rows, and current public errors.
- Produces: checksum, payload, cursor, JSON, clarification, and view projections while preserving Service method seams.

- [ ] **Step 1: Add a failing codec-owner test**

Append a test covering these names:

```python
def test_agent_design_codec_is_the_owning_module() -> None:
    from app.shared_assets import agent_design_codec as owning
    from app.shared_assets.agent_design_service import AgentDesignService

    static_names = (
        "blueprint_checksum",
        "_agent_payload",
        "_encode_session_cursor",
        "_decode_session_cursor",
        "_request_checksum",
        "_jsonable",
        "_message_json",
        "_progress_json",
        "_blueprint_json",
        "_candidate_metadata_from_json",
        "_remaining_conflicts_after_blueprint_update",
        "_blueprint_from_json",
        "_clarification_request",
        "_clarification_json",
        "_clarification_from_json",
        "_clarification_answers",
        "_clarification_history",
        "_session_view",
        "_session_summary",
        "_stable_generation_error_message",
    )
    class_names = (
        "_has_blocking_conflicts",
        "_clarification_set_json",
        "_clarifications_from_json",
    )
    for name in (*static_names, *class_names):
        assert AgentDesignService.__dict__[name].__func__ is getattr(owning, name)
```

- [ ] **Step 2: Run the test and observe the expected failure**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_agent_design_codec_is_the_owning_module -q
```

Expected: FAIL because `agent_design_codec.py` does not exist.

- [ ] **Step 3: Move codec functions with their current signatures**

Move the named functions and `_CLARIFICATION_SET_KIND` to the new module. Preserve the `cls` first argument on the three classmethod functions; this keeps subclass and monkeypatch semantics unchanged without importing `AgentDesignService`.

Within purely static functions, replace `AgentDesignService._jsonable` and other façade references with local functions. The new module must not import `agent_design_service`.

- [ ] **Step 4: Alias static and class methods back onto the Service**

Import each owning function with an `_impl` suffix and use this exact block:

```python
blueprint_checksum = staticmethod(blueprint_checksum_impl)
_agent_payload = staticmethod(_agent_payload_impl)
_encode_session_cursor = staticmethod(_encode_session_cursor_impl)
_decode_session_cursor = staticmethod(_decode_session_cursor_impl)
_request_checksum = staticmethod(_request_checksum_impl)
_jsonable = staticmethod(_jsonable_impl)
_message_json = staticmethod(_message_json_impl)
_progress_json = staticmethod(_progress_json_impl)
_blueprint_json = staticmethod(_blueprint_json_impl)
_candidate_metadata_from_json = staticmethod(_candidate_metadata_from_json_impl)
_remaining_conflicts_after_blueprint_update = staticmethod(_remaining_conflicts_after_blueprint_update_impl)
_blueprint_from_json = staticmethod(_blueprint_from_json_impl)
_clarification_request = staticmethod(_clarification_request_impl)
_clarification_json = staticmethod(_clarification_json_impl)
_clarification_from_json = staticmethod(_clarification_from_json_impl)
_clarification_answers = staticmethod(_clarification_answers_impl)
_clarification_history = staticmethod(_clarification_history_impl)
_session_view = staticmethod(_session_view_impl)
_session_summary = staticmethod(_session_summary_impl)
_stable_generation_error_message = staticmethod(_stable_generation_error_message_impl)
_has_blocking_conflicts = classmethod(_has_blocking_conflicts_impl)
_clarification_set_json = classmethod(_clarification_set_json_impl)
_clarifications_from_json = classmethod(_clarifications_from_json_impl)
```

The underlying descriptor `__func__` must be the owning function tested above.

- [ ] **Step 5: Run Agent Builder codec tests**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_agent_builder_contract_version.py \
  tests/test_agent_builder_interview_flow.py \
  tests/test_agent_builder_safety_lifecycle.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit only if explicitly authorized**

```bash
git add \
  backend/app/shared_assets/agent_design_codec.py \
  backend/app/shared_assets/agent_design_service.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(agent-builder): extract design codec"
```

---

### Task 9: Extract Agent Design validation functions and slug constants

**Files:**
- Create: `backend/app/shared_assets/agent_design_validation.py`
- Modify: `backend/app/shared_assets/agent_design_service.py:107-123`
- Modify: `backend/app/shared_assets/agent_design_service.py:2397-2845`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`

**Interfaces:**
- Consumes: Agent Design contracts/codec, Project context/capabilities, generation result contracts, and Agent Design rows.
- Produces: slug constants and exact command/blueprint validation while preserving Service static/classmethod seams.

- [ ] **Step 1: Add a failing validation-owner test**

Append:

```python
def test_agent_design_validation_is_the_owning_module() -> None:
    from app.shared_assets import agent_design_validation as owning
    from app.shared_assets import agent_design_service as legacy

    class_names = (
        "_validate_create",
        "_validate_turn",
        "_validate_generation_preference",
        "_validate_commit",
        "_validate_cancel",
        "_candidate_blueprint",
        "_require_capability",
    )
    static_names = (
        "_validate_blueprint",
        "_require_context",
        "_require_nonterminal",
        "_require_expected_revision",
        "_require_matching_operation",
        "_valid_revision",
        "_validate_uuid",
        "_validate_idempotency_key",
        "_bounded_text",
    )
    for name in (*class_names, *static_names):
        assert legacy.AgentDesignService.__dict__[name].__func__ is getattr(owning, name)
    assert legacy.AGENT_DESIGN_SLUG_MIN_LENGTH is owning.AGENT_DESIGN_SLUG_MIN_LENGTH
    assert legacy.AGENT_DESIGN_SLUG_MAX_LENGTH is owning.AGENT_DESIGN_SLUG_MAX_LENGTH
    assert legacy.AGENT_DESIGN_SLUG_PATTERN is owning.AGENT_DESIGN_SLUG_PATTERN
```

- [ ] **Step 2: Run the test and observe the expected failure**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_agent_design_validation_is_the_owning_module -q
```

Expected: FAIL because `agent_design_validation.py` does not exist.

- [ ] **Step 3: Move validation functions and constants**

Move the exact functions, `_valid_agent_design_slug`, the three public slug constants, `_SLUG_PATTERN`, `_CAPABILITY_PATTERN`, `_MAX_IDEMPOTENCY_KEY_CHARS`, `_MAX_MESSAGE_CHARS`, `_MAX_DESCRIPTION_CHARS`, and `_MAX_TOOL_GROUPS`. Preserve `cls` parameters on classmethod functions and replace Service references inside static validation with local functions or imports from `agent_design_codec`.

Keep lifecycle timing, conflict constraints, `_PUBLIC_ERROR_PATTERN`, `_constraint_name`, `_DEFAULT_STALE_GENERATING_SECONDS`, and generation polling constants in `agent_design_service.py`.

- [ ] **Step 4: Alias every validation seam back onto the Service**

Import each owning function with an `_impl` suffix and use this exact block:

```python
_validate_create = classmethod(_validate_create_impl)
_validate_turn = classmethod(_validate_turn_impl)
_validate_generation_preference = classmethod(_validate_generation_preference_impl)
_validate_commit = classmethod(_validate_commit_impl)
_validate_cancel = classmethod(_validate_cancel_impl)
_candidate_blueprint = classmethod(_candidate_blueprint_impl)
_require_capability = classmethod(_require_capability_impl)
_validate_blueprint = staticmethod(_validate_blueprint_impl)
_require_context = staticmethod(_require_context_impl)
_require_nonterminal = staticmethod(_require_nonterminal_impl)
_require_expected_revision = staticmethod(_require_expected_revision_impl)
_require_matching_operation = staticmethod(_require_matching_operation_impl)
_valid_revision = staticmethod(_valid_revision_impl)
_validate_uuid = staticmethod(_validate_uuid_impl)
_validate_idempotency_key = staticmethod(_validate_idempotency_key_impl)
_bounded_text = staticmethod(_bounded_text_impl)
```

Re-export the three slug constants from the legacy service module so its existing `__all__` remains unchanged.

- [ ] **Step 5: Run validation-focused tests**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_agent_builder_model_selection.py \
  tests/test_agent_builder_allowed_assets.py \
  tests/test_agent_builder_safety_lifecycle.py -q
```

Expected: all tests pass with unchanged errors and validation results.

- [ ] **Step 6: Commit only if explicitly authorized**

```bash
git add \
  backend/app/shared_assets/agent_design_validation.py \
  backend/app/shared_assets/agent_design_service.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(agent-builder): extract design validation"
```

---

### Task 10: Extract Provider Request profile and fingerprint ownership

**Files:**
- Create: `backend/packages/harness/deerflow/agents/middlewares/provider_request_profile.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/provider_request_usage.py:1-164`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/provider_request_usage.py:305-1106`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`
- Test: `backend/tests/test_provider_request_usage.py`
- Test: `backend/tests/test_agents_md_constants.py`

**Interfaces:**
- Consumes: LangChain messages/tools/model request types, Provider cost adapter, runtime-policy material, and public Run errors.
- Produces: Provider profile contracts, schema canonicalization, adapter declarations, closure/policy identity, profile construction, and exact fingerprinting.

- [ ] **Step 1: Add a failing profile-owner identity test**

Append:

```python
def test_provider_request_profile_is_the_owning_module() -> None:
    from deerflow.agents.middlewares import provider_request_profile as owning
    from deerflow.agents.middlewares import provider_request_usage as legacy

    names = (
        "ProviderRequestUsageUnsupported",
        "ProviderRequestProfileDrift",
        "ContextCapacityExceeded",
        "ProviderRequestComponentSnapshot",
        "ProviderToolSchemaFact",
        "ProviderRequestProfileSnapshot",
        "ProviderRequestMeasurementSnapshot",
        "ProviderRequestComponent",
        "ProviderRequestContextMeasurement",
        "ProviderRequestMaterialMeasurement",
        "ProviderRequestProfile",
        "provider_tool_schema_fact",
        "canonicalize_full_tools",
        "collect_middleware_tools",
        "collect_middleware_system_prompts",
        "collect_custom_middleware_request_contract",
        "contains_visual_material",
        "resolve_provider_adapter",
        "declared_visual_max_tokens_per_image",
        "provider_request_closure_identity",
        "provider_request_runtime_policy_identity",
        "provider_request_runtime_policy_compatibility_identity",
        "build_provider_request_profile",
        "build_provider_request_profile_snapshot_from_facts",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owning, name)
```

- [ ] **Step 2: Run the identity test and observe the expected failure**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_provider_request_profile_is_the_owning_module -q
```

Expected: FAIL because `provider_request_profile.py` does not exist.

- [ ] **Step 3: Move profile contracts and calculation as one coherent leaf**

Move the constants, exceptions, TypedDicts, dataclasses, canonicalization helpers, `ProviderRequestProfile`, and public functions named in the identity test. Include the private visual/image allowance constants used by `test_agents_md_constants.py`.

Do not move `ProviderDispatchOutcomeAmbiguous`, `ProviderRequestEvidenceObserver`, durable observer transitions, graph-state measurement functions, response helpers, or `FinalProviderRequestGuard`.

- [ ] **Step 4: Import and re-export profile symbols from the legacy module**

Use explicit imports, not wildcard imports. Keep every name in the existing `provider_request_usage.__all__` available and preserve the digest `d0ed55a5...`.

- [ ] **Step 5: Run profile and fingerprint tests**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_provider_request_usage.py \
  tests/test_agents_md_constants.py \
  tests/test_llm_error_handling_middleware.py -q
```

Expected: all tests pass with byte-identical fingerprints.

- [ ] **Step 6: Commit only if explicitly authorized**

```bash
git add \
  backend/packages/harness/deerflow/agents/middlewares/provider_request_profile.py \
  backend/packages/harness/deerflow/agents/middlewares/provider_request_usage.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(provider): extract request profile"
```

---

### Task 11: Extract Provider Request Context measurement

**Files:**
- Create: `backend/packages/harness/deerflow/agents/middlewares/provider_request_measurement.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/provider_request_usage.py:1109-1294`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py:53-59`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`

**Interfaces:**
- Consumes: `ProviderRequestProfile`, `ProviderRequestProfileSnapshot`, component measurements, and graph state.
- Produces: `measure_profile_snapshot_context(snapshot: Mapping[str, object], state: Mapping[str, object] | None) -> ProviderRequestContextMeasurement` and `measure_profile_context(profile: ProviderRequestProfile, state: Mapping[str, object] | None) -> ProviderRequestContextMeasurement`.

- [ ] **Step 1: Add a failing measurement-owner test**

Append:

```python
def test_provider_request_measurement_is_the_owning_module() -> None:
    from deerflow.agents.middlewares import provider_request_measurement as owning
    from deerflow.agents.middlewares import provider_request_usage as legacy

    assert legacy.measure_profile_snapshot_context is owning.measure_profile_snapshot_context
    assert legacy.measure_profile_context is owning.measure_profile_context
```

- [ ] **Step 2: Run the test and observe the expected failure**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_provider_request_measurement_is_the_owning_module -q
```

Expected: FAIL because `provider_request_measurement.py` does not exist.

- [ ] **Step 3: Move graph-state measurement functions**

Move `_state_ephemeral_material`, `_todo_ephemeral_material`, `_slash_request_ephemeral_material`, `measure_profile_snapshot_context`, and `measure_profile_context`. Import all profile contracts from `provider_request_profile`; do not import `provider_request_usage`.

- [ ] **Step 4: Re-export from the legacy module and migrate Summarization production imports**

Change `summarization_middleware.py` to import profile types/functions from `provider_request_profile` and Context measurement functions from `provider_request_measurement`. Leave tests that intentionally exercise the legacy façade unchanged.

- [ ] **Step 5: Run Context measurement and compaction tests**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_provider_request_usage.py \
  tests/test_compaction_trigger_capacity_clamp.py \
  tests/test_memory_snip_compaction.py \
  tests/test_context_compaction_receipt_basis.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit only if explicitly authorized**

```bash
git add \
  backend/packages/harness/deerflow/agents/middlewares/provider_request_measurement.py \
  backend/packages/harness/deerflow/agents/middlewares/provider_request_usage.py \
  backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(provider): extract request measurement"
```

---

### Task 12: Extract the Provider dispatch guard and finish the compatibility façade

**Files:**
- Create: `backend/packages/harness/deerflow/agents/middlewares/provider_request_guard.py`
- Modify: `backend/packages/harness/deerflow/agents/middlewares/provider_request_usage.py`
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/agent.py:47-56`
- Modify: `backend/app/private_work/mcp_runtime_contracts.py:16-18`
- Modify: `backend/tests/test_python_module_decomposition_contract.py`

**Interfaces:**
- Consumes: profile and measurement modules, Provider outcome classifiers, Context Evidence contracts, and LangChain middleware APIs.
- Produces: `ProviderDispatchOutcomeAmbiguous`, `ProviderRequestEvidenceObserver`, and `FinalProviderRequestGuard(profile: ProviderRequestProfile, *, cost_adapter=None, evidence_observer=None)`.

- [ ] **Step 1: Add a failing guard-owner and façade test**

Append:

```python
def test_provider_request_guard_is_the_owner_and_usage_is_a_facade() -> None:
    from deerflow.agents.middlewares import provider_request_guard as owning
    from deerflow.agents.middlewares import provider_request_usage as legacy

    names = (
        "ProviderDispatchOutcomeAmbiguous",
        "ProviderRequestEvidenceObserver",
        "FinalProviderRequestGuard",
    )
    for name in names:
        assert getattr(legacy, name) is getattr(owning, name)
    assert _export_digest(legacy) == EXPECTED_EXPORT_DIGESTS["provider_request"]
```

- [ ] **Step 2: Run the test and observe the expected failure**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py::test_provider_request_guard_is_the_owner_and_usage_is_a_facade -q
```

Expected: FAIL because `provider_request_guard.py` does not exist.

- [ ] **Step 3: Move Evidence and guard implementation**

Move these exact units:

```text
ProviderDispatchOutcomeAmbiguous
ProviderRequestEvidenceObserver
_join_durable_observer_transition
_record_ambiguity_despite_cancellation
_record_proven_no_response_failure
_record_provider_failed_response
_model_response
_provider_input_tokens
_runtime_run_id
_runtime_token_usage_tracking_enabled
_attach_measurement
FinalProviderRequestGuard
```

The new module imports profile and measurement ownership modules directly and must not import `provider_request_usage`.

- [ ] **Step 4: Reduce `provider_request_usage.py` to explicit compatibility imports**

The legacy module must contain its docstring, explicit imports from the three owning modules, direct imports for `PROVIDER_REQUEST_MEASUREMENT_STATE_KEY`, `PROVIDER_REQUEST_PROFILE_STATE_KEY`, and `ProviderNoResponseProvenError`, plus the unchanged 33-name `__all__`.

Do not retain duplicate constants, classes, or function bodies in the façade.

- [ ] **Step 5: Migrate production consumers to owning modules**

Use these dependency-specific imports:

```python
# lead_agent/agent.py
from deerflow.agents.middlewares.provider_request_guard import (
    FinalProviderRequestGuard,
    ProviderRequestEvidenceObserver,
)
from deerflow.agents.middlewares.provider_request_profile import (
    build_provider_request_profile,
    collect_custom_middleware_request_contract,
    collect_middleware_system_prompts,
    collect_middleware_tools,
    declared_visual_max_tokens_per_image,
    provider_request_runtime_policy_identity,
)

# app/private_work/mcp_runtime_contracts.py
from deerflow.agents.middlewares.provider_request_profile import provider_tool_schema_fact
```

- [ ] **Step 6: Prove owning modules do not import the façade**

```bash
rg -n 'provider_request_usage' \
  backend/packages/harness/deerflow/agents/middlewares/provider_request_profile.py \
  backend/packages/harness/deerflow/agents/middlewares/provider_request_measurement.py \
  backend/packages/harness/deerflow/agents/middlewares/provider_request_guard.py
```

Expected: no output.

- [ ] **Step 7: Run the complete Provider Request focused suite**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_provider_request_usage.py \
  tests/test_provider_request_context_evidence_guard.py \
  tests/test_provider_request_context_cancellation.py \
  tests/test_provider_failed_response_retry_chain.py \
  tests/test_provider_no_response_retry_chain.py \
  tests/test_lead_agent_trusted_extension.py \
  tests/test_skill_builder_provider_execution.py \
  tests/test_subagent_sdk_runner_profile.py -q
```

Expected: all tests pass; cancellation and durable Evidence ordering remain unchanged.

- [ ] **Step 8: Commit only if explicitly authorized**

```bash
git add \
  backend/packages/harness/deerflow/agents/middlewares/provider_request_guard.py \
  backend/packages/harness/deerflow/agents/middlewares/provider_request_usage.py \
  backend/packages/harness/deerflow/agents/lead_agent/agent.py \
  backend/app/private_work/mcp_runtime_contracts.py \
  backend/tests/test_python_module_decomposition_contract.py
git commit -m "refactor(provider): separate final request guard"
```

---

### Task 13: Document Batch 0–1 ownership and run release gates

**Files:**
- Modify: `backend/AGENTS.md:46-70`
- Modify: `README.md:241-270`
- Verify: all files changed by Tasks 1–12

**Interfaces:**
- Consumes: every owning module and compatibility façade created by this plan.
- Produces: documented ownership, complete local verification evidence, and a clean handoff for the separate Gateway Router plan.

- [ ] **Step 1: Update the backend ownership guide with exact boundaries**

Add these rules under “Where changes live”:

```markdown
- Builder `*_contracts.py`, `*_codec.py`, and `*_validation.py` modules own
  immutable payloads and pure transformations; the corresponding Service owns
  authorization, optimistic revisions, lifecycle transitions, and transactions.
- `skill_package_integrity.py` owns Skill package byte/path/frontmatter facts;
  `SkillService` owns the Project Skill aggregate and Version lifecycle.
- Provider Request ownership is split into profile, measurement, and guard;
  `provider_request_usage.py` is a compatibility export surface only.
```

- [ ] **Step 2: Update README architecture without creating a changelog**

Add this paragraph after the Gateway/Worker process-boundary paragraph:

```markdown
后端内部继续保持 HTTP Adapter、应用域事务与 Harness Runtime 的单向依赖。
为兼容既有调用保留的 Python 入口只转发 owning module 的同一对象，不维护第二套业务实现。
```

- [ ] **Step 3: Run all focused Batch 0–1 tests together**

```bash
cd backend
PYTHONPATH=. uv run pytest \
  tests/test_python_module_decomposition_contract.py \
  tests/test_system_asset_bootstrap_exact_history.py \
  tests/test_skill_distribution_export.py \
  tests/test_legacy_run_skill_snapshot_writer.py \
  tests/test_skill_builder_session_summary.py \
  tests/test_skill_builder_design_record.py \
  tests/test_skill_builder_execution_options.py \
  tests/test_skill_builder_draft_projection.py \
  tests/test_skill_builder_revision_contract.py \
  tests/test_agent_builder_contract_version.py \
  tests/test_agent_builder_interview_flow.py \
  tests/test_agent_builder_model_selection.py \
  tests/test_agent_builder_allowed_assets.py \
  tests/test_agent_builder_safety_lifecycle.py \
  tests/test_provider_request_usage.py \
  tests/test_provider_request_context_evidence_guard.py \
  tests/test_provider_request_context_cancellation.py \
  tests/test_provider_failed_response_retry_chain.py \
  tests/test_provider_no_response_retry_chain.py \
  tests/test_compaction_trigger_capacity_clamp.py \
  tests/test_memory_snip_compaction.py \
  tests/test_context_compaction_receipt_basis.py \
  tests/test_lead_agent_trusted_extension.py \
  tests/test_skill_builder_provider_execution.py \
  tests/test_subagent_sdk_runner_profile.py -q
```

Expected: zero failures and zero unexpected skips. Record the exact pass count.

- [ ] **Step 4: Run the System Skill catalog check**

```bash
cd backend
PYTHONPATH=. uv run python -m scripts.generate_public_system_skill_catalog --check
```

Expected: exit 0 and no generated-file diff.

- [ ] **Step 5: Run backend static gates**

```bash
cd backend
make lint
make detect-blocking-io
```

Expected: both targets exit 0. Inspect `.fluva-flow/blocking-io-findings.json` only if the detector reports findings; do not add it to Git.

- [ ] **Step 6: Run the complete backend core suite**

```bash
cd backend
make test
```

Expected: the core suite completes with zero failures and zero skips under the required non-production development `DATABASE_URL`. This proves the local core suite only; it does not certify a live Provider, external Sandbox, browser matrix, or target deployment.

- [ ] **Step 7: Inspect the final diff and status**

```bash
git diff --check
git status --short
git diff --stat
git diff -- backend/AGENTS.md README.md
```

Expected: only Batch 0–1 production modules, their focused tests, and the two approved documentation files appear.

- [ ] **Step 8: Commit documentation only if explicitly authorized**

```bash
git add backend/AGENTS.md README.md
git commit -m "docs: record python module ownership"
```

Without commit authorization, leave documentation unstaged with the rest of the implementation.

## Execution Completion Report

At handoff, report:

- the exact tasks completed;
- every command run and its pass/fail count;
- whether PostgreSQL-backed `make test` ran;
- whether any commit was created under explicit authorization;
- remaining compatibility façades;
- confirmation that Router, Sandbox Tool, Worker, database Schema, and user-visible behavior were not modified by this plan;
- any verification boundary not exercised locally.
