GATEWAY_WORKERS ?= 1

install:
	uv sync

dev:
	GATEWAY_WORKERS=1 PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 --reload

gateway:
	GATEWAY_WORKERS=$(GATEWAY_WORKERS) PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 --workers $(GATEWAY_WORKERS)

worker:
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run python -m app.worker.app

scheduler:
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run python -m app.scheduler.app

test:
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest tests/ -v

test-blocking-io:
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest tests/blocking_io -q --tb=short

lint:
	uvx ruff check .
	uvx ruff format --check .

format:
	uvx ruff check . --fix && uvx ruff format .

setup-db:
	PYTHONPATH=. uv run python scripts/setup_postgres.py

migrate-db:
	PYTHONPATH=. uv run python scripts/setup_postgres.py --migrate-only

check-db:
	PYTHONPATH=. uv run python scripts/check_postgres.py

reconcile-usage:
	PYTHONPATH=. uv run python scripts/reconcile_usage.py $(ARGS)

rotate-credentials:
	PYTHONPATH=. uv run python scripts/rotate_credentials.py $(ARGS)

import-project-skills:
	PYTHONPATH=. uv run python scripts/import_project_skills.py $(ARGS)

detect-blocking-io:
	@PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run python ../scripts/detect_blocking_io_static.py --output ../.deer-flow/blocking-io-findings.json

# Generate a new Alembic revision by autogenerating the diff against the live
# ORM models. Usage: make migrate-rev MSG="add foo column to runs"
# Runtime startup is read-only and never runs Alembic or repairs schema.
# `make migrate-db` is retained as the explicit forward-only path for future
# committed revisions; manual stamp and downgrade remain unsupported.
# The script creates a random PostgreSQL database from explicit
# POSTGRES_ADMIN_URL, upgrades it to head, diffs the live models, then drops it.
migrate-rev: export MIGRATION_MESSAGE := $(value MSG)
migrate-rev:
	@if [ -z "$$MIGRATION_MESSAGE" ]; then echo 'usage: make migrate-rev MSG="describe the change"'; exit 1; fi
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run python scripts/_autogen_revision.py
