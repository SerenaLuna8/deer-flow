# M7 Task 2 Independent Review Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five Important and one Minor Task 2 review findings without entering M7 Tasks 3-11.

**Architecture:** GitHub's obsolete ownerless webhook registry becomes an explicit empty fail-closed registry with no filesystem discovery. Bootstrap idempotency validates a reconstructed canonical PostgreSQL graph and rejects every builtin-principal authority reference. Exact private-run assets remain the only runtime source for model, soul, tools, MCP, and Skills; tests and prompt rendering encode that boundary.

**Tech Stack:** Python 3.12, FastAPI/LangGraph harness, SQLAlchemy async PostgreSQL, pytest/anyio, Ruff.

## Global Constraints

- Work only in `/Users/jiangfeng/deer-flow/.worktrees/m7-legacy-cleanup`.
- Use strict RED/GREEN TDD for every production behavior.
- Use `backend/.venv/bin/pytest` with `PYTHONPATH=packages/harness`.
- PostgreSQL tests use only randomized `deerflow_test_*` databases created through `POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres`.
- Do not start M7 Tasks 3-11 or reintroduce removed compatibility exports.
- Produce one repair commit after all covering gates pass.

---

### Task 1: Fail ownerless GitHub webhook fan-out closed

**Files:**
- Modify: `backend/tests/test_github_registry.py`
- Modify: `backend/tests/test_github_dispatcher.py`
- Modify: `backend/app/gateway/github/registry.py`
- Modify: `backend/app/gateway/github/dispatcher.py`

**Interfaces:**
- Produces: `build_github_agent_registry() -> dict[...]` returning an empty authoritative registry without filesystem calls.

- [x] Add a dispatcher test that forbids `Path.exists`, `Path.iterdir`, `Path.stat`, and `load_agent_config`, then asserts a valid webhook produces no matches/messages.
- [x] Run the focused test and record the expected filesystem-access failure.
- [x] Replace the filesystem/mtime registry implementation with a deterministic empty fail-closed registry and update obsolete registry tests.
- [x] Run registry and dispatcher suites green.

### Task 2: Validate the complete canonical bootstrap graph

**Files:**
- Modify: `backend/tests/test_m7_asset_bootstrap_postgres.py`
- Modify: `backend/app/shared_assets/bootstrap/catalog.json`
- Create: `backend/app/shared_assets/bootstrap/content/deerflow-docs-v1.mcp.json`
- Modify: `backend/app/shared_assets/bootstrap/content/project-assistant-v1.agent.json`
- Modify: `backend/app/shared_assets/bootstrap/service.py`

**Interfaces:**
- Produces: idempotent validation of every canonical parent, version, Skill file, Agent ref, MCP slot, identity/order/content field.

- [x] Add PostgreSQL tests that mutate representative parent/version/child fields for Agent, Skill, and MCP and expect `BootstrapConflict`.
- [x] Run them and record shallow-check RED failures.
- [x] Add a canonical MCP entry/slot and make the Agent reference it.
- [x] Reconstruct expected row dictionaries and exact ordered child tuples in bootstrap validation; reject missing, extra, or changed data.
- [x] Run all bootstrap tests green with zero skips.

### Task 3: Reject builtin-principal authority references

**Files:**
- Modify: `backend/tests/test_m7_asset_bootstrap_postgres.py`
- Modify: `backend/app/shared_assets/bootstrap/service.py`

**Interfaces:**
- Produces: `_ensure_builtin_principal()` rejection for credential actor references and all three binding creator/updater references.

- [x] Add PostgreSQL tests for a forbidden credential reference and each Agent/Skill/MCP binding creator/updater category.
- [x] Run and record expected RED acceptance.
- [x] Add locked/existence queries over every forbidden reference table/column and raise `BootstrapConflict`.
- [x] Run tests green and preserve zero credential/binding fresh bootstrap.

### Task 4: Remove full-suite test residue

**Files:**
- Delete or update every test returned by the exact removed-symbol scan, including `backend/tests/test_runtime_paths.py`, `backend/tests/test_skills_loader.py`, and legacy portions of `backend/tests/test_remote_sandbox_backend.py`.

**Interfaces:**
- Produces: zero test imports/references to `get_or_new_skill_storage`, `get_or_new_user_skill_storage`, `user_should_see_legacy_skills`, or `SkillCategory.LEGACY`.

- [x] Run collection/static scan to capture current import/attribute failures.
- [x] Delete tests whose only subject is removed storage and port mixed tests to exact snapshots or `include_legacy_skills=False`.
- [x] Run the affected collection/tests green and repeat the static scan to zero hits.

### Task 5: Restore exact private lead-agent wiring coverage

**Files:**
- Modify: `backend/tests/test_lead_agent_skills.py`
- Modify only if RED exposes a defect: `backend/packages/harness/deerflow/agents/lead_agent/agent.py`

**Interfaces:**
- Consumes: `_make_lead_agent(config, app_config=..., private_runtime=...)`.
- Proves: exact model, soul, tool groups, MCP tools, Skill mount/read-only state, and no global Agent/Skill/MCP fallback.

- [x] Add a real-wiring test that patches only external model/graph/tool factories, injects forged global config/storage/cache values, and captures `_make_lead_agent` arguments.
- [x] Run and record RED for any missing exact contract or fallback call.
- [x] Apply the minimal wiring fix if required, then run the exact-runtime test green.

### Task 6: Render exact Skills as read-only and cache by mutability

**Files:**
- Modify: `backend/tests/test_lead_agent_skills.py`
- Modify: `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`

**Interfaces:**
- Produces: `[run exact, read-only]` label and prompt signature including `runtime_read_only`.

- [x] Add tests rendering otherwise identical mutable/read-only Skills and assert distinct prompt/cache identities.
- [x] Run and record RED showing `[custom, editable]` or a cache collision.
- [x] Extend the Skill signature and mutability label to include `runtime_read_only`.
- [x] Run prompt tests green.

### Task 7: Final verification, report, and commit

**Files:**
- Modify: `.superpowers/sdd/task-2-report.md`
- Modify docs only if repaired architecture changed existing guidance.

- [x] Run the complete required PostgreSQL gate with zero skips.
- [x] Run GitHub registry/dispatcher, exact runtime, collection-residue, client/ACP/sandbox, Ruff/format, compileall, OpenAPI, static scan, and diff checks.
- [x] Append exact RED/GREEN repair evidence and self-review to the Task 2 report.
- [ ] Review the staged diff for scope, then commit once with a Task 2 repair message.
