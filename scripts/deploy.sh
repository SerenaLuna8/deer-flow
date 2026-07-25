#!/usr/bin/env bash
#
# deploy.sh - Build, start, or stop DeerFlow production services
#
# Commands:
#   deploy.sh                    — build + start
#   deploy.sh build              — build all images (mode-agnostic)
#   deploy.sh start              — start from pre-built images
#   deploy.sh down               — stop and remove containers
#
# Sandbox mode (local / aio / provisioner) is auto-detected from config.yaml.
#
# Examples:
#   deploy.sh                    # build + start
#   deploy.sh build              # build all images
#   deploy.sh start              # start pre-built images
#   deploy.sh down               # stop and remove containers
#
# Must be run from the repo root directory.

set -e

case "${1:-}" in
    build|start|down)
        CMD="$1"
        if [ -n "${2:-}" ]; then
            echo "Unknown argument: $2"
            echo "Usage: deploy.sh [build|start|down]"
            exit 1
        fi
        ;;
    "")
        CMD=""
        ;;
    *)
        echo "Unknown argument: $1"
        echo "Usage: deploy.sh [build|start|down]"
        exit 1
        ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env"
DOCKER_DIR="$REPO_ROOT/docker"
if [ -f "$ENV_FILE" ]; then
    COMPOSE_CMD=(docker compose --env-file "$ENV_FILE" -p deer-flow -f "$DOCKER_DIR/docker-compose.yaml")
else
    COMPOSE_CMD=(docker compose -p deer-flow -f "$DOCKER_DIR/docker-compose.yaml")
fi

load_uv_extras_from_dotenv() {
    local line=""
    local value=""

    [ -f "$ENV_FILE" ] || return 0
    [ -z "${UV_EXTRAS+x}" ] || return 0

    line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?UV_EXTRAS[[:space:]]*=' "$ENV_FILE" | tail -n 1 || true)"
    [ -n "$line" ] || return 0

    value="${line#*=}"
    value="${value%$'\r'}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    export UV_EXTRAS="$value"
}

load_uv_extras_from_dotenv

# ── Colors ────────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# ── DEER_FLOW_HOME ────────────────────────────────────────────────────────────

if [ -z "$DEER_FLOW_HOME" ]; then
    export DEER_FLOW_HOME="$REPO_ROOT/backend/.deer-flow"
fi
echo -e "${BLUE}DEER_FLOW_HOME=$DEER_FLOW_HOME${NC}"
mkdir -p "$DEER_FLOW_HOME"

# ── DEER_FLOW_REPO_ROOT (for skills host path in DooD) ───────────────────────

export DEER_FLOW_REPO_ROOT="$REPO_ROOT"

# ── config.yaml ───────────────────────────────────────────────────────────────

if [ -z "$DEER_FLOW_CONFIG_PATH" ]; then
    export DEER_FLOW_CONFIG_PATH="$REPO_ROOT/config.yaml"
fi

if  [ "$CMD" != "down" ] && [ ! -f "$DEER_FLOW_CONFIG_PATH" ]; then
    # Try to seed from repo (config.example.yaml is the canonical template)
    if [ -f "$REPO_ROOT/config.example.yaml" ]; then
        cp "$REPO_ROOT/config.example.yaml" "$DEER_FLOW_CONFIG_PATH"
        echo -e "${GREEN}✓ Seeded config.example.yaml → $DEER_FLOW_CONFIG_PATH${NC}"
        echo -e "${YELLOW}⚠ config.yaml was seeded from the example template.${NC}"
        echo "  Run 'make setup' to generate a minimal config, or edit $DEER_FLOW_CONFIG_PATH manually before use."
    else
        echo -e "${RED}✗ No config.yaml found.${NC}"
        echo "  Run 'make setup' from the repo root (recommended),"
        echo "  or 'make config' for the full template, then set the required model API keys."
        exit 1
    fi
else
    echo -e "${GREEN}✓ config.yaml: $DEER_FLOW_CONFIG_PATH${NC}"
fi

# ── BETTER_AUTH_SECRET ───────────────────────────────────────────────────────
# Required by Next.js in production. Generated once and persisted so auth
# sessions survive container restarts.

_secret_file="$DEER_FLOW_HOME/.better-auth-secret"
if [ -z "$BETTER_AUTH_SECRET" ]; then
    if [ -f "$_secret_file" ]; then
        export BETTER_AUTH_SECRET
        BETTER_AUTH_SECRET="$(cat "$_secret_file")"
        echo -e "${GREEN}✓ BETTER_AUTH_SECRET loaded from $_secret_file${NC}"
    else
        export BETTER_AUTH_SECRET
        if command -v python3 > /dev/null 2>&1 && \
            BETTER_AUTH_SECRET="$(python3 -c 'import sys; sys.version_info >= (3, 6) or sys.exit(1); import secrets; print(secrets.token_hex(32))' 2>/dev/null)"; then
            true
        elif command -v python > /dev/null 2>&1 && \
            BETTER_AUTH_SECRET="$(python -c 'import sys; sys.version_info >= (3, 6) or sys.exit(1); import secrets; print(secrets.token_hex(32))' 2>/dev/null)"; then
            true
        elif command -v openssl > /dev/null 2>&1 && \
            BETTER_AUTH_SECRET="$(openssl rand -hex 32)"; then
            true
        else
            echo -e "${RED}✗ Cannot generate BETTER_AUTH_SECRET: python3, python, and openssl are all unavailable.${NC}" >&2
            echo -e "${RED}  Set BETTER_AUTH_SECRET manually before running make up.${NC}" >&2
            exit 1
        fi
        echo "$BETTER_AUTH_SECRET" > "$_secret_file"
        chmod 600 "$_secret_file"
        echo -e "${GREEN}✓ BETTER_AUTH_SECRET generated → $_secret_file${NC}"
    fi
fi

# ── DEER_FLOW_INTERNAL_AUTH_TOKEN ────────────────────────────────────────────
# Shared by all Gateway workers so channel workers can call internal Gateway
# APIs even when the request is handled by a different Uvicorn worker.

_internal_auth_token_file="$DEER_FLOW_HOME/.internal-auth-token"
if  [ "$CMD" != "down" ] && [ -z "$DEER_FLOW_INTERNAL_AUTH_TOKEN" ]; then
    if [ -f "$_internal_auth_token_file" ]; then
        export DEER_FLOW_INTERNAL_AUTH_TOKEN
        DEER_FLOW_INTERNAL_AUTH_TOKEN="$(cat "$_internal_auth_token_file")"
        echo -e "${GREEN}✓ DEER_FLOW_INTERNAL_AUTH_TOKEN loaded from $_internal_auth_token_file${NC}"
    else
        export DEER_FLOW_INTERNAL_AUTH_TOKEN
        if command -v python3 > /dev/null 2>&1 && \
            DEER_FLOW_INTERNAL_AUTH_TOKEN="$(python3 -c 'import sys; sys.version_info >= (3, 6) or sys.exit(1); import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null)"; then
            true
        elif command -v python > /dev/null 2>&1 && \
            DEER_FLOW_INTERNAL_AUTH_TOKEN="$(python -c 'import sys; sys.version_info >= (3, 6) or sys.exit(1); import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null)"; then
            true
        elif command -v openssl > /dev/null 2>&1 && \
            DEER_FLOW_INTERNAL_AUTH_TOKEN="$(openssl rand -hex 32)"; then
            true
        else
            echo -e "${RED}✗ Cannot generate DEER_FLOW_INTERNAL_AUTH_TOKEN: python3, python, and openssl are all unavailable.${NC}" >&2
            echo -e "${RED}  Set DEER_FLOW_INTERNAL_AUTH_TOKEN manually before running make up.${NC}" >&2
            exit 1
        fi
        echo "$DEER_FLOW_INTERNAL_AUTH_TOKEN" > "$_internal_auth_token_file"
        chmod 600 "$_internal_auth_token_file"
        echo -e "${GREEN}✓ DEER_FLOW_INTERNAL_AUTH_TOKEN generated → $_internal_auth_token_file${NC}"
    fi
fi

# ── DEER_FLOW_PROXY_AUTH_TOKEN ──────────────────────────────────────────────
# Dedicated nginx-to-Gateway attestation. This is deliberately independent of
# the internal channel token so a proxy compromise cannot mint internal users.

_proxy_auth_token_file="$DEER_FLOW_HOME/.proxy-auth-token"
if [ "$CMD" != "down" ] && [ -z "$DEER_FLOW_PROXY_AUTH_TOKEN" ]; then
    if [ -f "$_proxy_auth_token_file" ]; then
        export DEER_FLOW_PROXY_AUTH_TOKEN
        DEER_FLOW_PROXY_AUTH_TOKEN="$(cat "$_proxy_auth_token_file")"
        echo -e "${GREEN}✓ DEER_FLOW_PROXY_AUTH_TOKEN loaded from $_proxy_auth_token_file${NC}"
    else
        export DEER_FLOW_PROXY_AUTH_TOKEN
        if command -v python3 > /dev/null 2>&1 && \
            DEER_FLOW_PROXY_AUTH_TOKEN="$(python3 -c 'import sys; sys.version_info >= (3, 6) or sys.exit(1); import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null)"; then
            true
        elif command -v python > /dev/null 2>&1 && \
            DEER_FLOW_PROXY_AUTH_TOKEN="$(python -c 'import sys; sys.version_info >= (3, 6) or sys.exit(1); import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null)"; then
            true
        elif command -v openssl > /dev/null 2>&1 && \
            DEER_FLOW_PROXY_AUTH_TOKEN="$(openssl rand -hex 32)"; then
            true
        else
            echo -e "${RED}✗ Cannot generate DEER_FLOW_PROXY_AUTH_TOKEN: python3, python, and openssl are all unavailable.${NC}" >&2
            echo -e "${RED}  Set DEER_FLOW_PROXY_AUTH_TOKEN manually before running make up.${NC}" >&2
            exit 1
        fi
        echo "$DEER_FLOW_PROXY_AUTH_TOKEN" > "$_proxy_auth_token_file"
        chmod 600 "$_proxy_auth_token_file"
        echo -e "${GREEN}✓ DEER_FLOW_PROXY_AUTH_TOKEN generated → $_proxy_auth_token_file${NC}"
    fi
fi
if [ "$CMD" != "down" ] && [ "${#DEER_FLOW_PROXY_AUTH_TOKEN}" -lt 32 ]; then
    echo -e "${RED}✗ DEER_FLOW_PROXY_AUTH_TOKEN must contain at least 32 characters.${NC}" >&2
    exit 1
fi

# ── UV_EXTRAS auto-detection ─────────────────────────────────────────────────
# The production Dockerfile accepts UV_EXTRAS as a single build-arg token and
# adds the --extra prefix itself. Convert the detector's uv flag string
# ("--extra ollama --extra discord") to a comma-joined name token.

if [ "$CMD" != "down" ] && [ -z "$UV_EXTRAS" ]; then
    _detect_python=""
    for _python in python3 python; do
        if command -v "$_python" >/dev/null 2>&1 && \
            "$_python" -c 'import sys; sys.version_info >= (3, 6) or sys.exit(1)' >/dev/null 2>&1; then
            _detect_python="$_python"
            break
        fi
    done
fi

if [ "$CMD" != "down" ] && [ -z "$UV_EXTRAS" ] && [ -n "$_detect_python" ]; then
    _uv_extras_flags="$("$_detect_python" "$REPO_ROOT/scripts/detect_uv_extras.py" 2>/dev/null || true)"
    _uv_extras=""
    set -- $_uv_extras_flags
    while [ "$#" -gt 0 ]; do
        if [ "$1" = "--extra" ] && [ "$#" -gt 1 ]; then
            if [ -z "$_uv_extras" ]; then
                _uv_extras="$2"
            else
                _uv_extras="$_uv_extras,$2"
            fi
            shift 2
        else
            shift
        fi
    done
    if [ -n "$_uv_extras" ]; then
        export UV_EXTRAS="$_uv_extras"
        echo -e "${GREEN}✓ Auto-detected UV_EXTRAS=${UV_EXTRAS} from config.yaml${NC}"
    fi
fi

# ── detect_sandbox_mode ───────────────────────────────────────────────────────

detect_sandbox_mode() {
    local sandbox_use=""
    local provisioner_url=""

    [ -f "$DEER_FLOW_CONFIG_PATH" ] || { echo "local"; return; }

    sandbox_use=$(awk '
        /^[[:space:]]*sandbox:[[:space:]]*$/ { in_sandbox=1; next }
        in_sandbox && /^[^[:space:]#]/ { in_sandbox=0 }
        in_sandbox && /^[[:space:]]*use:[[:space:]]*/ {
            line=$0; sub(/^[[:space:]]*use:[[:space:]]*/, "", line); print line; exit
        }
    ' "$DEER_FLOW_CONFIG_PATH")

    provisioner_url=$(awk '
        /^[[:space:]]*sandbox:[[:space:]]*$/ { in_sandbox=1; next }
        in_sandbox && /^[^[:space:]#]/ { in_sandbox=0 }
        in_sandbox && /^[[:space:]]*provisioner_url:[[:space:]]*/ {
            line=$0; sub(/^[[:space:]]*provisioner_url:[[:space:]]*/, "", line); print line; exit
        }
    ' "$DEER_FLOW_CONFIG_PATH")

    if [[ "$sandbox_use" == *"deerflow.community.aio_sandbox:AioSandboxProvider"* ]]; then
        if [ -n "$provisioner_url" ]; then
            echo "provisioner"
        else
            echo "aio"
        fi
    else
        echo "local"
    fi
}

detect_scheduler_enabled() {
    [ -f "$DEER_FLOW_CONFIG_PATH" ] || { echo "false"; return; }
    awk '
        /^[[:space:]]*scheduler:[[:space:]]*$/ { in_scheduler=1; next }
        in_scheduler && /^[^[:space:]#]/ { in_scheduler=0 }
        in_scheduler && /^[[:space:]]*enabled:[[:space:]]*/ {
            line=$0; sub(/^[[:space:]]*enabled:[[:space:]]*/, "", line)
            sub(/[[:space:]]*#.*/, "", line); gsub(/["\047[:space:]]/, "", line)
            print (tolower(line)=="true" ? "true" : "false"); exit
        }
        END { if (!in_scheduler) {} }
    ' "$DEER_FLOW_CONFIG_PATH" | head -n 1 | grep -E '^(true|false)$' || echo "false"
}

# ── down ──────────────────────────────────────────────────────────────────────

if [ "$CMD" = "down" ]; then
    # Set minimal env var defaults so docker compose can parse the file without
    # warning about unset variables that appear in volume specs.
    export DEER_FLOW_HOME="${DEER_FLOW_HOME:-$REPO_ROOT/backend/.deer-flow}"
    export DEER_FLOW_CONFIG_PATH="${DEER_FLOW_CONFIG_PATH:-$DEER_FLOW_HOME/config.yaml}"
    export DEER_FLOW_REPO_ROOT="${DEER_FLOW_REPO_ROOT:-$REPO_ROOT}"
    export BETTER_AUTH_SECRET="${BETTER_AUTH_SECRET:-placeholder}"
    export DEER_FLOW_INTERNAL_AUTH_TOKEN="${DEER_FLOW_INTERNAL_AUTH_TOKEN:-placeholder}"
    export DEER_FLOW_PROXY_AUTH_TOKEN="${DEER_FLOW_PROXY_AUTH_TOKEN:-placeholder-placeholder-placeholder-00}"
    "${COMPOSE_CMD[@]}" down
    exit 0
fi

# ── build ────────────────────────────────────────────────────────────────────
# Build produces mode-agnostic images. No --gateway or sandbox detection needed.

if [ "$CMD" = "build" ]; then
    echo "=========================================="
    echo "  DeerFlow — Building Images"
    echo "=========================================="
    echo ""

    "${COMPOSE_CMD[@]}" build

    echo ""
    echo "=========================================="
    echo "  ✓ Images built successfully"
    echo "=========================================="
    echo ""
    echo "  Next: deploy.sh start"
    echo ""
    exit 0
fi

# ── Banner ────────────────────────────────────────────────────────────────────

echo "=========================================="
echo "  DeerFlow Production Deployment"
echo "=========================================="
echo ""

# ── Detect runtime configuration ────────────────────────────────────────────
# Only needed for start / up — determines whether provisioner is launched.

sandbox_mode="$(detect_sandbox_mode)"
echo -e "${BLUE}Sandbox mode: $sandbox_mode${NC}"

echo -e "${BLUE}Runtime: Gateway admission API + independent Worker execution${NC}"

services="frontend gateway worker nginx"

scheduler_enabled="$(detect_scheduler_enabled)"
if [ "$scheduler_enabled" = "true" ]; then
    COMPOSE_CMD+=(--profile scheduler)
    services="$services scheduler"
    echo -e "${BLUE}Scheduler enabled${NC}"
else
    echo -e "${BLUE}Scheduler disabled${NC}"
fi

if [ "$sandbox_mode" = "provisioner" ]; then
    services="$services provisioner"
fi

# ── DEER_FLOW_DOCKER_SOCKET (aio / pure-DooD mode only) ──────────────────────
# Only aio mode (AioSandboxProvider without provisioner_url) needs the host
# Docker socket. It is mounted via the opt-in docker-compose.dood.yaml overlay,
# appended here, so the default (local) and provisioner modes never expose the
# host daemon. Mounting the socket grants root-equivalent host control.

if [ -z "$DEER_FLOW_DOCKER_SOCKET" ]; then
    export DEER_FLOW_DOCKER_SOCKET="/var/run/docker.sock"
fi

if [ "$sandbox_mode" = "aio" ]; then
    if [ ! -S "$DEER_FLOW_DOCKER_SOCKET" ]; then
        echo -e "${RED}⚠ Docker socket not found at $DEER_FLOW_DOCKER_SOCKET${NC}"
        echo "  AioSandboxProvider (DooD) will not work."
        exit 1
    fi
    echo -e "${GREEN}✓ Docker socket: $DEER_FLOW_DOCKER_SOCKET${NC}"
    echo -e "${YELLOW}  Mounting host Docker socket into gateway (DooD = host root-equivalent).${NC}"
    COMPOSE_CMD+=(-f "$DOCKER_DIR/docker-compose.dood.yaml")
fi

echo ""

# ── Start / Up ───────────────────────────────────────────────────────────────

if [ "$CMD" = "start" ]; then
    echo "Starting containers (no rebuild)..."
    echo ""
    # shellcheck disable=SC2086
    "${COMPOSE_CMD[@]}" up -d --remove-orphans $services
else
    # Default: build + start
    echo "Building images and starting containers..."
    echo ""
    # shellcheck disable=SC2086
    "${COMPOSE_CMD[@]}" up --build -d --remove-orphans $services
fi

echo ""
echo "=========================================="
echo "  DeerFlow is running!"
echo "=========================================="
echo ""
echo "  🌐 Application: http://localhost:${PORT:-2026}"
echo "  📡 API Gateway: http://localhost:${PORT:-2026}/api/*"
echo "  🤖 Runtime:     Independent Worker"
echo ""
echo "  Manage:"
echo "    make down        — stop and remove containers"
echo "    make docker-logs — view logs"
echo ""
