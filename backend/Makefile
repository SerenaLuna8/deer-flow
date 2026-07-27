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
