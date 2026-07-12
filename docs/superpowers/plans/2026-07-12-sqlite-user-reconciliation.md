# SQLite User Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit, fingerprint-bound and auditable reconciliation for exactly expected duplicate legacy users.

**Architecture:** A frozen request validates CLI intent and source fingerprints. A pure UnionPlan reconciliation phase produces per-source absorbed-user and fixed-allowlist user-id remap decisions, reused by preflight, dry-run, snapshot verification and writes. The existing ledger records both absorbed users and normalized dependent rows.

**Tech Stack:** Python 3.12, argparse, sqlite3 read-only inventory, SQLAlchemy metadata, asyncpg, pytest.

## Global Constraints

- Default behavior remains fail-closed.
- Never modify source SQLite or write real `deerflow`.
- Never infer user-reference columns by name; use the documented fixed allowlist.
- Never render email, user ids, paths, credentials or URLs.

---

### Task 1: Opt-in request and pure reconciliation plan

**Files:**
- Modify: `backend/scripts/migrate_sqlite_to_postgres.py`
- Test: `backend/tests/test_sqlite_to_postgres_migration.py`

- [ ] Write failing tests for absent opt-in, fingerprint/order/count mismatch, role mismatch and ambiguous email matches.
- [ ] Run each test and confirm it fails for the missing reconciliation API.
- [ ] Add frozen request/decision types and build decisions before registry conflict checks.
- [ ] Run focused tests and confirm default conflicts remain unchanged.

### Task 2: Fixed user-reference normalization and ledger audit

**Files:**
- Modify: `backend/scripts/migrate_sqlite_to_postgres.py`
- Test: `backend/tests/test_sqlite_to_postgres_migration.py`

- [ ] Write failing tests that preserve both scheduled tasks, remap every allowlisted column, and leave `bot_user_id` unchanged.
- [ ] Write failing migration tests for absorbed-user `reconciled` ledger rows and idempotent replay of normalized dependent rows.
- [ ] Apply the pure per-source decision in preflight and `_migrate_business_table`.
- [ ] Run focused tests and confirm transformed rows retain original source keys.

### Task 3: CLI and snapshot binding

**Files:**
- Modify: `backend/scripts/migrate_sqlite_to_postgres.py`
- Test: `backend/tests/test_sqlite_to_postgres_migration.py`

- [ ] Write failing CLI tests for explicit flags, source SHA order, expected count and redaction.
- [ ] Parse the opt-in request and pass it through original and snapshot UnionPlan construction.
- [ ] Assert snapshot decisions equal original decisions before any write.
- [ ] Run CLI dry-run tests and confirm no backup or target mutation.

### Task 4: Documentation and frozen verification

**Files:**
- Modify: `backend/AGENTS.md`
- Modify: `README_zh.md`

- [ ] Document the fixed allowlist, explicit flags, safety gates and ledger behavior.
- [ ] Run focused, affected, blocking-I/O, Ruff, format, lock and full backend tests.
- [ ] Commit the reviewed implementation and documentation.
