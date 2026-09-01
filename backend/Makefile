GATEWAY_WORKERS ?= 1
SETUP_ENV_FILE := $(wildcard ../.env)
RUNTIME_ENV_RUNNER = uv run python scripts/run_runtime.py --

.PHONY: reset-db

install:
	uv sync --all-packages --extra extraction-local

dev:
	GATEWAY_WORKERS=1 PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 $(RUNTIME_ENV_RUNNER) python -m uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 --reload

gateway:
	GATEWAY_WORKERS=$(GATEWAY_WORKERS) PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 $(RUNTIME_ENV_RUNNER) python -m uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 --workers $(GATEWAY_WORKERS)

worker:
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 $(RUNTIME_ENV_RUNNER) python -m app.worker.app

scheduler:
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 $(RUNTIME_ENV_RUNNER) python -m app.scheduler.app

test:
	PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run python tests/support/core_gate_plugin.py tests/ -q -m "not provider_integration"

test-provider-integration:
	ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION=1 ACT_WEAVE_RUN_REAL_APPLE_CONTAINER_TEST=1 PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest tests/ -q -m provider_integration

lint:
	uvx ruff check .
	uvx ruff format --check .

format:
	uvx ruff check . --fix && uvx ruff format .

setup-db:
	PYTHONPATH=. uv run $(if $(SETUP_ENV_FILE),--env-file $(SETUP_ENV_FILE)) python scripts/setup_postgres.py

reset-db:
	uv run $(if $(SETUP_ENV_FILE),--env-file $(SETUP_ENV_FILE)) python -m scripts.reset_postgres

check-db:
	PYTHONPATH=. $(RUNTIME_ENV_RUNNER) python scripts/check_postgres.py

upgrade-system-assets:
	PYTHONPATH=. $(RUNTIME_ENV_RUNNER) python scripts/upgrade_system_assets.py $(ARGS)

prepare-run-event-partitions:
	PYTHONPATH=. $(RUNTIME_ENV_RUNNER) python scripts/prepare_run_event_partitions.py

prune-run-events:
	PYTHONPATH=. $(RUNTIME_ENV_RUNNER) python scripts/prune_run_event_partitions.py $(ARGS)

reconcile-usage:
	PYTHONPATH=. $(RUNTIME_ENV_RUNNER) python scripts/reconcile_usage.py $(ARGS)

import-project-skills:
	PYTHONPATH=. $(RUNTIME_ENV_RUNNER) python scripts/import_project_skills.py $(ARGS)

detect-blocking-io:
	@PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run python ../scripts/detect_blocking_io_static.py --output ../.fluva-flow/blocking-io-findings.json
