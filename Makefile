# DeerFlow - Unified Development Environment

.PHONY: help config config-upgrade check install test test-project-foundation-postgres setup setup-db setup-m4-migration-db migrate-db migrate-sqlite migrate-assets migrate-private-work migrate-automations migrate-reliability reconcile-usage backup-db restore-db drill-restore rotate-credentials check-db doctor support-bundle detect-thread-boundaries detect-blocking-io dev dev-daemon start start-daemon gateway worker scheduler nginx stop up down clean docker-init docker-start docker-stop docker-logs docker-logs-frontend docker-logs-gateway

BASH ?= bash
BACKEND_UV_RUN = cd backend && uv run
PROJECT_FOUNDATION_POSTGRES_TESTS = \
	tests/integration/test_m1_postgres_cutover.py \
	tests/integration/test_project_isolation_postgres.py \
	tests/integration/test_m2_project_governance_postgres.py \
	tests/integration/test_m3_shared_assets_postgres.py \
	tests/integration/test_m4_private_work_postgres.py \
	tests/integration/test_m4_private_work_migration_postgres.py \
	tests/integration/test_m5_project_automation_postgres.py \
	tests/integration/test_m5_automation_migration_postgres.py \
	tests/test_m6_reliability_migration_postgres.py \
	tests/test_m6_reliability_schema_postgres.py \
	tests/test_m6_process_readiness.py \
	tests/test_m6_job_repository_postgres.py \
	tests/test_m6_durable_stream_postgres.py \
	tests/test_m6_quota_service_postgres.py \
	tests/test_m6_audit_redaction.py \
	tests/test_m6_audit_integration_postgres.py \
	tests/test_m6_restore_postgres.py \
	tests/test_m6_release_gate_postgres.py \
	tests/test_m6_worker_crash_recovery_postgres.py \
	tests/test_m6_gateway_reconnect_process.py

# Detect OS for Windows compatibility
ifeq ($(OS),Windows_NT)
    SHELL := cmd.exe
    PYTHON ?= python
    # Run repo shell scripts through Git Bash when Make is launched from cmd.exe / PowerShell.
    RUN_WITH_GIT_BASH = call scripts\run-with-git-bash.cmd
else
    PYTHON ?= python3
    RUN_WITH_GIT_BASH =
endif

help:
	@echo "DeerFlow Development Commands:"
	@echo "  make setup           - Interactive setup wizard (recommended for new users)"
	@echo "  make doctor          - Check configuration and system requirements"
	@echo "  make support-bundle  - Create a redacted issue summary, AI draft, and evidence bundle"
	@echo "  make config          - Generate local config files (aborts if config already exists)"
	@echo "  make config-upgrade  - Merge new fields from config.example.yaml into config.yaml"
	@echo "  make check           - Check if all required tools are installed"
	@echo "  make test            - Run the fixed M1-M6 PostgreSQL release gate (0 skip)"
	@echo "  make test-project-foundation-postgres - Run the fixed M1-M6 PostgreSQL release gate"
	@echo "  make setup-db        - 创建并初始化 PostgreSQL 数据库"
	@echo "  make setup-m4-migration-db - 创建/验证固定在0007的legacy SQLite迁移库"
	@echo "  make migrate-db      - 仅升级已存在 PostgreSQL 数据库"
	@echo "  make migrate-sqlite  - 只读预检/备份并迁移 legacy SQLite（参数通过 ARGS 传入）"
	@echo "  make migrate-assets  - 显式预检/备份并迁移 legacy shared assets（参数通过 ARGS 传入）"
	@echo "  make migrate-private-work - 显式 dry-run/execute 迁移 legacy private work（参数通过 ARGS 传入）"
	@echo "  make migrate-automations - 显式迁移 legacy automations，必须先 dry-run 再 execute（参数通过 ARGS 传入）"
	@echo "  make migrate-reliability - 显式执行 M6 reliability dry-run/cutover（参数通过 ARGS 传入）"
	@echo "  make reconcile-usage - 预检或执行 quota reconciliation（参数通过 ARGS 传入）"
	@echo "  make backup-db      - 创建外部、认证加密 PostgreSQL backup archive（参数通过 ARGS 传入）"
	@echo "  make restore-db     - 恢复认证 archive 到全新 deerflow_restore_* 数据库（参数通过 ARGS 传入）"
	@echo "  make drill-restore  - 恢复到随机临时数据库，验证后只删除该临时库（参数通过 ARGS 传入）"
	@echo "  make rotate-credentials - 分批轮换 credential envelopes（参数通过 ARGS 传入）"
	@echo "  make check-db        - 只读检查 PostgreSQL 数据库状态"
	@echo "  make detect-thread-boundaries - Inventory async/thread boundary points"
	@echo "  make detect-blocking-io        - Inventory blocking IO that may block the backend event loop"
	@echo "  make install         - Install all dependencies (frontend + backend + pre-commit hooks)"
	@echo "  make setup-sandbox   - Pre-pull sandbox container image (recommended)"
	@echo "  make dev             - Start all services in development mode (with hot-reloading)"
	@echo "  make dev-daemon      - Start dev services in background (daemon mode)"
	@echo "  make start           - Start all services in production mode (optimized, no hot-reloading)"
	@echo "  make start-daemon    - Start prod services in background (daemon mode)"
	@echo "  make nginx           - Start nginx alone in the foreground (local dev config)"
	@echo "  make stop            - Stop all running services"
	@echo "  make clean           - Clean up processes and temporary files"
	@echo ""
	@echo "Docker Production Commands:"
	@echo "  make up              - Build and start production Docker services (localhost:2026)"
	@echo "  make down            - Stop and remove production Docker containers"
	@echo ""
	@echo "Docker Development Commands:"
	@echo "  make docker-init     - Pull the sandbox image"
	@echo "  make docker-start    - Start Docker services (mode-aware from config.yaml, localhost:2026)"
	@echo "  make docker-stop     - Stop Docker development services"
	@echo "  make docker-logs     - View Docker development logs"
	@echo "  make docker-logs-frontend - View Docker frontend logs"
	@echo "  make docker-logs-gateway - View Docker gateway logs"

## Setup & Diagnosis
test: test-project-foundation-postgres

test-project-foundation-postgres:
	@$(BACKEND_UV_RUN) python tests/support/release_gate_plugin.py $(PROJECT_FOUNDATION_POSTGRES_TESTS) -ra

setup:
	@$(BACKEND_UV_RUN) python ../scripts/setup_wizard.py

doctor:
	@$(BACKEND_UV_RUN) python ../scripts/doctor.py

setup-db:
	@$(MAKE) -C backend setup-db

setup-m4-migration-db:
	@$(MAKE) -C backend setup-m4-migration-db

migrate-db:
	@$(MAKE) -C backend migrate-db

migrate-sqlite:
	@$(MAKE) -C backend migrate-sqlite ARGS="$(ARGS)"

migrate-assets:
	@$(MAKE) -C backend migrate-assets ARGS="$(ARGS)"

migrate-private-work:
	@$(MAKE) -C backend migrate-private-work ARGS="$(ARGS)"

migrate-automations:
	@$(MAKE) -C backend migrate-automations ARGS="$(ARGS)"

migrate-reliability:
	@$(MAKE) -C backend migrate-reliability ARGS="$(ARGS)"

reconcile-usage:
	@$(MAKE) -C backend reconcile-usage ARGS="$(ARGS)"

gateway:
	@$(MAKE) -C backend gateway

worker:
	@$(MAKE) -C backend worker

scheduler:
	@$(MAKE) -C backend scheduler

backup-db:
	@$(MAKE) -C backend backup-db ARGS="$(ARGS)"

restore-db:
	@$(MAKE) -C backend restore-db ARGS="$(ARGS)"

drill-restore:
	@$(MAKE) -C backend drill-restore ARGS="$(ARGS)"

rotate-credentials:
	@$(MAKE) -C backend rotate-credentials ARGS="$(ARGS)"

check-db:
	@$(MAKE) -C backend check-db

support-bundle:
	@$(BACKEND_UV_RUN) python ../scripts/support_bundle.py --include-doctor

detect-thread-boundaries:
	@$(PYTHON) ./scripts/detect_thread_boundaries.py

detect-blocking-io:
	@$(MAKE) -C backend detect-blocking-io

config:
	@$(PYTHON) ./scripts/configure.py

config-upgrade:
	@$(RUN_WITH_GIT_BASH) ./scripts/config-upgrade.sh

# Check required tools
check:
	@$(PYTHON) ./scripts/check.py

# Install all dependencies
install:
	@echo "Installing backend dependencies..."
	@cd backend && uv sync
	@echo "Installing frontend dependencies..."
	@cd frontend && pnpm install
	@echo "Installing pre-commit hooks..."
	@uv tool install pre-commit
	@pre-commit install --overwrite
	@echo "✓ All dependencies installed"
	@echo ""
	@echo "=========================================="
	@echo "  Optional: Pre-pull Sandbox Image"
	@echo "=========================================="
	@echo ""
	@echo "If you plan to use Docker/Container-based sandbox, you can pre-pull the image:"
	@echo "  make setup-sandbox"
	@echo ""

# Pre-pull sandbox Docker image (optional but recommended)
setup-sandbox:
	@$(RUN_WITH_GIT_BASH) ./scripts/setup-sandbox.sh

# Start all services in development mode (with hot-reloading)
dev:
	@$(PYTHON) ./scripts/check.py
	@$(RUN_WITH_GIT_BASH) ./scripts/serve.sh --dev

# Start all services in production mode (with optimizations)
start:
	@$(PYTHON) ./scripts/check.py
	@$(RUN_WITH_GIT_BASH) ./scripts/serve.sh --prod

# Start all services in daemon mode (background)
dev-daemon:
	@$(PYTHON) ./scripts/check.py
	@$(RUN_WITH_GIT_BASH) ./scripts/serve.sh --dev --daemon

# Start prod services in daemon mode (background)
start-daemon:
	@$(PYTHON) ./scripts/check.py
	@$(RUN_WITH_GIT_BASH) ./scripts/serve.sh --prod --daemon

# Start nginx alone in the foreground with the local dev config
nginx:
	@$(RUN_WITH_GIT_BASH) ./scripts/nginx.sh

# Stop all services
stop:
	@$(RUN_WITH_GIT_BASH) ./scripts/serve.sh --stop

# Clean up
clean: stop
	@echo "Cleaning up..."
	@-rm -rf backend/.deer-flow 2>/dev/null || true
	@-rm -rf logs/*.log 2>/dev/null || true
	@echo "✓ Cleanup complete"

# ==========================================
# Docker Development Commands
# ==========================================

# Initialize Docker containers and install dependencies
docker-init:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh init

# Start Docker development environment
docker-start:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh start

# Stop Docker development environment
docker-stop:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh stop

# View Docker development logs
docker-logs:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh logs

# View Docker development logs
docker-logs-frontend:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh logs --frontend
docker-logs-gateway:
	@$(RUN_WITH_GIT_BASH) ./scripts/docker.sh logs --gateway
# ==========================================
# Production Docker Commands
# ==========================================

# Build and start production services
up:
	@$(RUN_WITH_GIT_BASH) ./scripts/deploy.sh

# Stop and remove production containers
down:
	@$(RUN_WITH_GIT_BASH) ./scripts/deploy.sh down
