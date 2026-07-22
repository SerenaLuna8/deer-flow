# DeerFlow - Unified Development Environment

.DEFAULT_GOAL := help

.PHONY: \
	help \
	setup config config-upgrade check doctor install setup-sandbox support-bundle \
	setup-db migrate-db check-db reconcile-usage rotate-credentials \
	test test-project-foundation-postgres print-project-foundation-postgres-tests \
	test-project-saas-postgres print-project-saas-postgres-tests release-acceptance \
	detect-thread-boundaries detect-blocking-io \
	dev dev-daemon start start-daemon gateway worker scheduler nginx stop clean \
	docker-init docker-start docker-stop docker-logs docker-logs-frontend docker-logs-gateway up down

BACKEND_UV_RUN = cd backend && uv run
PROJECT_FOUNDATION_POSTGRES_TESTS = \
	tests/test_m7_final_baseline_postgres.py \
	tests/test_m7_asset_bootstrap_postgres.py \
	tests/integration/test_project_isolation_postgres.py \
	tests/integration/test_m2_project_governance_postgres.py \
	tests/integration/test_m3_shared_assets_postgres.py \
	tests/integration/test_m3_mcp_credentials_postgres.py \
	tests/integration/test_m4_private_work_postgres.py \
	tests/integration/test_m5_project_automation_postgres.py \
	tests/test_m6_process_readiness.py \
	tests/test_m6_job_repository_postgres.py \
	tests/test_m6_durable_stream_postgres.py \
	tests/test_m6_quota_service_postgres.py \
	tests/test_m6_audit_redaction.py \
	tests/test_m6_audit_integration_postgres.py \
	tests/test_m6_retention_purge_postgres.py \
	tests/test_m6_worker_crash_recovery_postgres.py \
	tests/test_m6_gateway_reconnect_process.py \
	tests/test_m7_process_boundary.py \
	tests/test_m7_source_absence.py \
	tests/test_m7_release_gate_postgres.py

M8_RELEASE_POSTGRES_TESTS = $(PROJECT_FOUNDATION_POSTGRES_TESTS) \
	tests/test_m8_isolation_matrix_postgres.py \
	tests/test_m8_capacity_postgres.py \
	tests/test_m8_release_gate_postgres.py

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

SERVE = $(RUN_WITH_GIT_BASH) ./scripts/serve.sh
DOCKER = $(RUN_WITH_GIT_BASH) ./scripts/docker.sh
DEPLOY = $(RUN_WITH_GIT_BASH) ./scripts/deploy.sh

help:
	@echo "DeerFlow 命令"
	@echo ""
	@echo "本地服务："
	@echo "  make dev                              启动开发环境（热更新，入口 localhost:2026）"
	@echo "  make dev-daemon                       后台启动开发环境"
	@echo "  make start                            启动本地生产模式"
	@echo "  make start-daemon                     后台启动本地生产模式"
	@echo "  make gateway                          单独启动 Gateway"
	@echo "  make worker                           单独启动 Worker"
	@echo "  make scheduler                        单独启动 Scheduler"
	@echo "  make nginx                            单独启动本地 Nginx"
	@echo "  make stop                             停止本地服务"
	@echo "  make clean                            停止服务并清理本地运行状态和日志"
	@echo ""
	@echo "配置与安装："
	@echo "  make setup                            运行交互式初始化向导"
	@echo "  make config                           从示例生成本地配置"
	@echo "  make config-upgrade                   升级并补齐 config.yaml"
	@echo "  make check                            检查必要工具"
	@echo "  make doctor                           检查配置和运行环境"
	@echo "  make install                          安装前后端依赖和 pre-commit hooks"
	@echo "  make setup-sandbox                    预拉取 Sandbox 容器镜像"
	@echo "  make support-bundle                   生成脱敏诊断材料"
	@echo ""
	@echo "PostgreSQL 与运维："
	@echo "  make setup-db                         创建并初始化数据库"
	@echo "  make migrate-db                       验证或初始化空数据库"
	@echo "  make check-db                         只读检查数据库状态"
	@echo "  make reconcile-usage ARGS=...         校准配额用量"
	@echo "  make rotate-credentials ARGS=...      轮换 Credential envelope"
	@echo ""
	@echo "测试与发布验收："
	@echo "  make test                             运行 M1-M8 PostgreSQL 发布门禁"
	@echo "  make test-project-foundation-postgres 运行 M1-M7 PostgreSQL 诊断前缀"
	@echo "  make test-project-saas-postgres       运行 M1-M8 PostgreSQL 发布门禁"
	@echo "  make release-acceptance               运行完整 M8 宿主机验收"
	@echo "  make detect-thread-boundaries         检查异步和线程边界"
	@echo "  make detect-blocking-io               检查后端阻塞 IO"
	@echo ""
	@echo "Docker："
	@echo "  make docker-init                      拉取 Sandbox 镜像"
	@echo "  make docker-start                     启动 Docker 开发环境"
	@echo "  make docker-stop                      停止 Docker 开发环境"
	@echo "  make docker-logs                      查看 Docker 日志"
	@echo "  make docker-logs-frontend             查看 Frontend 日志"
	@echo "  make docker-logs-gateway              查看 Gateway 日志"
	@echo "  make up                               构建并启动生产容器"
	@echo "  make down                             停止生产容器"

# Tests and release acceptance
test: test-project-saas-postgres

test-project-foundation-postgres:
	@cd backend && DEER_FLOW_RELEASE_GATE_LABEL=M1-M7 uv run python tests/support/release_gate_plugin.py $(PROJECT_FOUNDATION_POSTGRES_TESTS) -ra

print-project-foundation-postgres-tests:
	@echo $(PROJECT_FOUNDATION_POSTGRES_TESTS)

test-project-saas-postgres:
	@cd backend && DEER_FLOW_RELEASE_GATE_LABEL=M1-M8 uv run python tests/support/release_gate_plugin.py $(M8_RELEASE_POSTGRES_TESTS) -ra

print-project-saas-postgres-tests:
	@echo $(M8_RELEASE_POSTGRES_TESTS)

release-acceptance:
	@cd backend && uv run python scripts/run_release_acceptance.py

# Configuration and diagnostics
setup:
	@$(BACKEND_UV_RUN) python ../scripts/setup_wizard.py

doctor:
	@$(BACKEND_UV_RUN) python ../scripts/doctor.py

# PostgreSQL and operations
setup-db:
	@$(MAKE) -C backend setup-db

migrate-db:
	@$(MAKE) -C backend migrate-db

reconcile-usage:
	@$(MAKE) -C backend reconcile-usage ARGS="$(ARGS)"

rotate-credentials:
	@$(MAKE) -C backend rotate-credentials ARGS="$(ARGS)"

check-db:
	@$(MAKE) -C backend check-db

# Support and static diagnostics
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

check:
	@$(PYTHON) ./scripts/check.py

# Dependency installation
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

setup-sandbox:
	@$(RUN_WITH_GIT_BASH) ./scripts/setup-sandbox.sh

# Local service lifecycle
gateway:
	@$(MAKE) -C backend gateway

worker:
	@$(MAKE) -C backend worker

scheduler:
	@$(MAKE) -C backend scheduler

dev: check
	@$(SERVE) --dev

start: check
	@$(SERVE) --prod

dev-daemon: check
	@$(SERVE) --dev --daemon

start-daemon: check
	@$(SERVE) --prod --daemon

nginx:
	@$(RUN_WITH_GIT_BASH) ./scripts/nginx.sh

stop:
	@$(SERVE) --stop

clean: stop
	@echo "Cleaning up..."
	@-rm -rf backend/.deer-flow 2>/dev/null || true
	@-rm -rf logs/*.log 2>/dev/null || true
	@echo "✓ Cleanup complete"

# Docker development
docker-init:
	@$(DOCKER) init

docker-start:
	@$(DOCKER) start

docker-stop:
	@$(DOCKER) stop

docker-logs:
	@$(DOCKER) logs

docker-logs-frontend:
	@$(DOCKER) logs --frontend

docker-logs-gateway:
	@$(DOCKER) logs --gateway

# Docker production
up:
	@$(DEPLOY)

down:
	@$(DEPLOY) down
