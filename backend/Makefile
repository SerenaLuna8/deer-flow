GATEWAY_WORKERS ?= 1

install:
	uv sync

dev:
	GATEWAY_WORKERS=1 PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 --reload

gateway:
	GATEWAY_WORKERS=$(GATEWAY_WORKERS) PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 --workers $(GATEWAY_WORKERS)

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

migrate-sqlite:
	PYTHONPATH=. uv run python scripts/migrate_sqlite_to_postgres.py $(ARGS)

migrate-assets:
	PYTHONPATH=. uv run python scripts/migrate_assets.py $(ARGS)

rotate-credentials:
	PYTHONPATH=. uv run python scripts/rotate_credentials.py $(ARGS)

detect-blocking-io:
	@PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run python ../scripts/detect_blocking_io_static.py --output ../.deer-flow/blocking-io-findings.json

# Generate a new alembic revision by autogenerating the diff against the live
# ORM models. Usage: make migrate-rev MSG="add foo column to runs"
# The Gateway runs `alembic upgrade head` automatically at startup via
# `bootstrap_schema`, so there is intentionally no `migrate` / `migrate-stamp`
# target -- the single execution path keeps ops mistakes off the table.
# The script creates a random PostgreSQL database from explicit
# POSTGRES_ADMIN_URL, upgrades it to head, diffs the live models, then drops it.
migrate-rev: export MIGRATION_MESSAGE := $(value MSG)
migrate-rev:
	@if [ -z "$$MIGRATION_MESSAGE" ]; then echo 'usage: make migrate-rev MSG="describe the change"'; exit 1; fi
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run python scripts/_autogen_revision.py
