#!/usr/bin/env bash
#
# serve.sh — Unified ActWeave service launcher
#
# Usage:
#   ./scripts/serve.sh [--dev|--prod] [--daemon] [--stop|--restart]
#
# Modes:
#   --dev       Development mode with hot-reload (default)
#   --prod      Production mode, pre-built frontend, no hot-reload
#   --daemon    Run all services in background. On macOS, launchd keeps a
#               foreground supervisor alive after this command returns.
#
# Actions:
#   --skip-install  Skip dependency installation (faster restart)
#   --stop      Stop all running services and exit
#   --restart   Stop all services, then start with the given mode flags
#
# Examples:
#   ./scripts/serve.sh --dev                 # Full stack, hot reload
#   ./scripts/serve.sh --prod                # Full stack, production mode
#   ./scripts/serve.sh --dev --daemon        # Full stack, background
#   ./scripts/serve.sh --stop                # Stop all services
#   ./scripts/serve.sh --restart --dev       # Restart dev services
#
# Must be run from the repo root directory.

set -e

REPO_ROOT="$(builtin cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd -P)"
cd "$REPO_ROOT"

# ── Load .env ────────────────────────────────────────────────────────────────

if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    source "$REPO_ROOT/.env"
    set +a
fi

# Model API keys are encrypted inside model-domain PostgreSQL rows. Keep common legacy
# provider variables out of Gateway, Worker, and Scheduler process environments
# so a provider cannot silently bypass the exact admitted secret generation.
# Tool/process secrets remain available through their own distinct names.
# The opt-in Claude Code/Codex CLI handoff variables are intentionally separate.
unset "ANTHROPIC_API_KEY"
unset "DEEPSEEK_API_KEY"
unset "GEMINI_API_KEY"
unset "MIMO_API_KEY"
unset "MINIMAX_API_KEY"
unset "MOONSHOT_API_KEY"
unset "NOVITA_API_KEY"
unset "OPENCODE_API_KEY"
unset "OPENAI_API_KEY"
unset "OPENROUTER_API_KEY"
unset "STEPFUN_API_KEY"
unset "VLLM_API_KEY"
unset "VOLCENGINE_API_KEY"

# POSTGRES_ADMIN_URL is an installation/upgrade-only superuser credential.
# Runtime roles use DATABASE_URL and must never inherit the management URL from
# either the parent shell or the repository .env loaded above.
unset "POSTGRES_ADMIN_URL"

_pick_python() {
    local candidate
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info.major >= 3 else 1)' >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# ── Argument parsing ─────────────────────────────────────────────────────────

DEV_MODE=true
DAEMON_MODE=false
SKIP_INSTALL=false
ACTION="start"   # start | stop | restart

for arg in "$@"; do
    case "$arg" in
        --dev)     DEV_MODE=true ;;
        --prod)    DEV_MODE=false ;;
        --daemon)  DAEMON_MODE=true ;;
        --skip-install) SKIP_INSTALL=true ;;
        --stop)    ACTION="stop" ;;
        --restart) ACTION="restart" ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 [--dev|--prod] [--daemon] [--skip-install] [--stop|--restart]"
            exit 1
            ;;
    esac
done

# ── Stop helper ──────────────────────────────────────────────────────────────

# Every deer-flow worktree (the main checkout + each linked worktree) hardcodes
# the same dev ports (8001/3000/2026), so a service started from ANY of them
# must be reclaimable from here — otherwise `make stop`/`make dev` in this
# worktree can neither kill nor take over a port held by a sibling worktree.
# ACT_WEAVE_ROOTS is that set of roots; processes living outside all of them
# (e.g. an unrelated project on port 3000) are still never touched.
# Sorted most-specific-first (longest path first): a linked worktree lives
# under the main checkout, so both roots are substrings of its files — checking
# the deeper root first attributes a reclaimed port to the right worktree.
ACT_WEAVE_ROOTS="$(
    {
        printf '%s\n' "$REPO_ROOT"
        git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null |
            awk '/^worktree /{print $2}'
    } | awk 'NF && !seen[$0]++ {print length($0)"\t"$0}' | sort -rn | sed 's/^[0-9]*\t//'
)"

# True if PID has an open file/cwd under any deer-flow worktree root. The
# trailing slash keeps a sibling dir like ".../deer-flow-notes" from matching
# the ".../deer-flow" root.
_is_deerflow_pid() {
    local pid=$1 files root

    # Daemon children inherit ACT_WEAVE_DAEMON_ROOT from run_service. Checking
    # it (Linux only — macOS has no /proc) identifies processes like
    # next-server that lsof misses, so the name/port reaps in stop_all can
    # claim them.
    if [ -r "/proc/$pid/environ" ] &&
        tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -Fxq "ACT_WEAVE_DAEMON_ROOT=$REPO_ROOT"; then
        return 0
    fi

    files=$(lsof -b -w -p "$pid" 2>/dev/null) || return 1
    while IFS= read -r root; do
        [ -n "$root" ] || continue
        case "$files" in
            *"$root"/*) return 0 ;;
        esac
    done <<< "$ACT_WEAVE_ROOTS"
    return 1
}

# Report ports about to be reclaimed from a *different* worktree, so explicit
# stop/restart actions do not silently kill someone else's run.
_report_reclaimed_ports() {
    local port pid files root owner
    for port in 8001 3000 2026; do
        for pid in $(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null); do
            _is_deerflow_pid "$pid" || continue
            files=$(lsof -b -w -p "$pid" 2>/dev/null) || continue
            case "$files" in *"$REPO_ROOT"/*) continue ;; esac  # this worktree — normal
            owner=""
            while IFS= read -r root; do
                [ -n "$root" ] || continue
                case "$files" in *"$root"/*) owner="$root"; break ;; esac
            done <<< "$ACT_WEAVE_ROOTS"
            echo "  ↻ Reclaiming port $port from another worktree: ${owner:-?}"
            break
        done
    done
}

_kill_repo_processes() {
    local pattern=$1
    local pid
    local pids=""

    while IFS= read -r pid; do
        if [ -n "$pid" ] && _is_deerflow_pid "$pid"; then
            case " $pids " in
                *" $pid "*) ;;
                *) pids="$pids $pid" ;;
            esac
        fi
    done < <(pgrep -f "$pattern" 2>/dev/null || true)

    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
    fi
}

_kill_repo_port() {
    local port=$1
    local pid
    local pids=""

    while IFS= read -r pid; do
        if [ -n "$pid" ] && _is_deerflow_pid "$pid"; then
            case " $pids " in
                *" $pid "*) ;;
                *) pids="$pids $pid" ;;
            esac
        fi
    done < <(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)

    if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null || true
    fi
}

_is_port_listening() {
    local port=$1 status

    if command -v lsof >/dev/null 2>&1; then
        # All local ActWeave services bind an IPv4 socket. An unrelated
        # IPv6-only listener (for example [::1]:8001) does not conflict with
        # that socket and must not block startup.
        if lsof -nP -a -i4TCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
            return 0
        else
            status=$?
        fi
        [ "$status" -eq 1 ] && return 1
        return 0
    fi

    if command -v ss >/dev/null 2>&1; then
        if ss -ltn "( sport = :$port )" 2>/dev/null | tail -n +2 | grep -q .; then
            return 0
        fi
        return 1
    fi

    if command -v netstat >/dev/null 2>&1; then
        if netstat -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(^|[.:])${port}$"; then
            return 0
        fi
        return 1
    fi

    return 1
}

_is_repo_nginx_pid() {
    local pid=$1
    local command
    local args

    command=$(ps -p "$pid" -o comm= 2>/dev/null) || return 1
    # nginx rewrites argv[0] for master/worker processes. On macOS,
    # `ps -o comm=` can report that rewritten form instead of the binary name.
    case "$command" in
        nginx|*/nginx|nginx:*) ;;
        *) return 1 ;;
    esac

    args=$(ps -p "$pid" -o args= 2>/dev/null) || return 1
    local root
    while IFS= read -r root; do
        [ -n "$root" ] || continue
        case "$args" in
            *"$root"/docker/nginx/nginx.local.conf*|*"$root"/*) return 0 ;;
        esac
    done <<< "$ACT_WEAVE_ROOTS"

    _is_deerflow_pid "$pid"
}

_kill_repo_nginx() {
    local pid
    local pids=""

    if [ -f "$REPO_ROOT/logs/nginx.pid" ]; then
        read -r pid < "$REPO_ROOT/logs/nginx.pid" || true
        if [ -n "$pid" ] && _is_repo_nginx_pid "$pid"; then
            pids="$pids $pid"
        fi
    fi

    while IFS= read -r pid; do
        if [ -n "$pid" ] && _is_repo_nginx_pid "$pid"; then
            case " $pids " in
                *" $pid "*) ;;
                *) pids="$pids $pid" ;;
            esac
        fi
    done < <(pgrep -f nginx 2>/dev/null || true)

    if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null || true
    fi
}

# `launchctl submit` jobs are scoped to the current login, so the fixed labels
# below cannot collide across users. The application itself uses fixed local
# ports, which already limits a login to one dev and one prod stack.
_macos_daemon_labels() {
    printf '%s\n' \
        "com.actweave.deerflow.dev" \
        "com.actweave.deerflow.prod"
}

_stop_macos_daemon_supervisors() {
    [ "$(uname -s)" = "Darwin" ] || return 0
    command -v launchctl >/dev/null 2>&1 || return 0

    local label
    while IFS= read -r label; do
        launchctl remove "$label" >/dev/null 2>&1 || true
    done < <(_macos_daemon_labels)
}

stop_all() {
    echo "Stopping all services..."
    _stop_macos_daemon_supervisors
    _report_reclaimed_ports
    _kill_repo_processes "uvicorn app.gateway.app:app"
    _kill_repo_processes "python -m app.worker.app"
    _kill_repo_processes "python -m app.scheduler.app"
    _kill_repo_processes "next dev"
    _kill_repo_processes "next start"
    _kill_repo_processes "next-server"
    nginx -c "$REPO_ROOT/docker/nginx/nginx.local.conf" -p "$REPO_ROOT" -s quit 2>/dev/null || true
    sleep 1
    _kill_repo_nginx
    # Force-kill any survivors still holding the service ports. 2026 is included
    # so a lingering nginx (or any deer-flow process) that _kill_repo_nginx did
    # not match by name still gets reclaimed — otherwise `make dev` fails its
    # nginx port preflight.
    _kill_repo_port 8001
    _kill_repo_port 3000
    _kill_repo_port 2026
    ./scripts/cleanup-containers.sh deer-flow-sandbox 2>/dev/null || true
    echo "✓ All services stopped"
}

# ── Action routing ───────────────────────────────────────────────────────────

if [ "$ACTION" = "stop" ]; then
    stop_all
    exit 0
fi

if [ "$ACTION" = "restart" ]; then
    stop_all
    sleep 1
fi

# Mode label for banner
if $DEV_MODE; then
    MODE_LABEL="DEV (hot-reload enabled)"
else
    MODE_LABEL="PROD (optimized)"
fi

if $DAEMON_MODE; then
    MODE_LABEL="$MODE_LABEL [daemon]"
fi

# Resolve pnpm through the same runner used by check/install/doctor. Exporting
# both paths preserves whitespace when run_service invokes a child shell.
if ! ACT_WEAVE_PNPM_PYTHON="$(_pick_python)"; then
    echo "Python 3 is required to locate pnpm or its Corepack fallback."
    exit 1
fi
ACT_WEAVE_PNPM_RUNNER="$REPO_ROOT/scripts/pnpm.py"
export ACT_WEAVE_PNPM_PYTHON ACT_WEAVE_PNPM_RUNNER

if $DEV_MODE; then
    FRONTEND_CMD='"$ACT_WEAVE_PNPM_PYTHON" "$ACT_WEAVE_PNPM_RUNNER" run dev'
else
    FRONTEND_CMD="env BETTER_AUTH_SECRET=$($ACT_WEAVE_PNPM_PYTHON -c 'import secrets; print(secrets.token_hex(16))') \"\$ACT_WEAVE_PNPM_PYTHON\" \"\$ACT_WEAVE_PNPM_RUNNER\" run preview"
fi

# Runtime path defaults. Local `make dev` launches Gateway from `backend/`,
# so pin ActWeave-owned state to the expected backend runtime directory and
# create it before uvicorn builds its reload exclude filter.
if [ -z "$ACT_WEAVE_PROJECT_ROOT" ]; then
    export ACT_WEAVE_PROJECT_ROOT="$REPO_ROOT"
fi

BACKEND_RUNTIME_HOME="$REPO_ROOT/backend/.deer-flow"
if [ -z "$ACT_WEAVE_HOME" ]; then
    export ACT_WEAVE_HOME="$BACKEND_RUNTIME_HOME"
fi

# `backend/sandbox` is excluded from uvicorn's reload watcher below. uvicorn only
# excludes an absolute path directly when it already exists as a directory;
# otherwise it globs the pattern, and Python 3.12's pathlib rejects absolute glob
# patterns with NotImplementedError, crashing `make dev` on a fresh checkout
# (#3459 / #3454). Creating it here keeps every absolute exclude on the is_dir path.
mkdir -p "$ACT_WEAVE_HOME" "$BACKEND_RUNTIME_HOME" "$REPO_ROOT/backend/sandbox"
ACT_WEAVE_HOME="$(cd "$ACT_WEAVE_HOME" && pwd -P)"
BACKEND_RUNTIME_HOME="$(cd "$BACKEND_RUNTIME_HOME" && pwd -P)"
export ACT_WEAVE_HOME

# Extra flags for uvicorn
if $DEV_MODE && ! $DAEMON_MODE; then
    GATEWAY_WORKERS=1
    GATEWAY_EXTRA_FLAGS="--reload --reload-include='*.yaml' --reload-include='.env' --reload-exclude='*.pyc' --reload-exclude='__pycache__' --reload-exclude='$REPO_ROOT/backend/sandbox' --reload-exclude='$ACT_WEAVE_HOME' --reload-exclude='$BACKEND_RUNTIME_HOME'"
else
    GATEWAY_WORKERS="${GATEWAY_WORKERS:-1}"
    case "$GATEWAY_WORKERS" in
        0 | *[!0-9]*)
            echo "✗ GATEWAY_WORKERS must be a positive integer."
            exit 1
            ;;
    esac
    GATEWAY_EXTRA_FLAGS="--workers $GATEWAY_WORKERS"
fi
export GATEWAY_WORKERS

# ── Config check ─────────────────────────────────────────────────────────────

if [ -n "${ACT_WEAVE_CONFIG_PATH:-}" ]; then
    if [ ! -f "$ACT_WEAVE_CONFIG_PATH" ]; then
        echo "✗ ACT_WEAVE_CONFIG_PATH does not name a file: $ACT_WEAVE_CONFIG_PATH"
        exit 1
    fi
    CONFIG_DIR="$(builtin cd "$(dirname "$ACT_WEAVE_CONFIG_PATH")" >/dev/null 2>&1 && pwd -P)"
    export ACT_WEAVE_CONFIG_PATH="$CONFIG_DIR/$(basename "$ACT_WEAVE_CONFIG_PATH")"
else
    export ACT_WEAVE_CONFIG_PATH="$REPO_ROOT/config.yaml"
fi

RUNTIME_ROOT="$REPO_ROOT"
LOG_ROOT="$RUNTIME_ROOT/logs"

_macos_daemon_label() {
    if $DEV_MODE; then
        printf '%s\n' "com.actweave.deerflow.dev"
    else
        printf '%s\n' "com.actweave.deerflow.prod"
    fi
}

_start_macos_daemon() {
    local label mode_flag stdout_log stderr_log service_target port
    local -a start_args launch_env

    label="$(_macos_daemon_label)"
    if $DEV_MODE; then
        mode_flag="--dev"
    else
        mode_flag="--prod"
    fi

    start_args=()
    if $SKIP_INSTALL; then
        start_args+=("--skip-install")
    fi

    for port in 8001 3000 2026; do
        if _is_port_listening "$port"; then
            echo "✗ Cannot start the background supervisor because port $port is already in use."
            echo "  Run 'make stop' first, or free the unrelated process manually."
            return 1
        fi
    done

    mkdir -p "$LOG_ROOT"
    if $DEV_MODE; then
        stdout_log="$LOG_ROOT/dev-daemon-supervisor.out.log"
        stderr_log="$LOG_ROOT/dev-daemon-supervisor.err.log"
    else
        stdout_log="$LOG_ROOT/prod-daemon-supervisor.out.log"
        stderr_log="$LOG_ROOT/prod-daemon-supervisor.err.log"
    fi
    service_target="gui/$(id -u)/$label"

    if launchctl print "$service_target" >/dev/null 2>&1; then
        echo "✗ The background supervisor is already registered ($label)."
        echo "  Run 'make stop' before starting it again."
        return 1
    fi

    # launchd does not reliably inherit the interactive shell's PATH. Pass
    # only non-secret runtime paths here; the supervised launcher loads .env
    # itself, so database and credential values never appear in launchctl args.
    launch_env=(
        /usr/bin/env
        "PATH=$PATH"
        "ACT_WEAVE_CONFIG_PATH=$ACT_WEAVE_CONFIG_PATH"
        "ACT_WEAVE_HOME=$ACT_WEAVE_HOME"
        "ACT_WEAVE_PROJECT_ROOT=$ACT_WEAVE_PROJECT_ROOT"
    )
    if [ -n "${GATEWAY_WORKERS:-}" ]; then
        launch_env+=("GATEWAY_WORKERS=$GATEWAY_WORKERS")
    fi
    if [ -n "${UV_EXTRAS:-}" ]; then
        launch_env+=("UV_EXTRAS=$UV_EXTRAS")
    fi

    echo "Registering macOS background supervisor ($label)..."
    launchctl submit -l "$label" -o "$stdout_log" -e "$stderr_log" -- \
        "${launch_env[@]}" /bin/bash "$REPO_ROOT/scripts/serve.sh" "$mode_flag" "${start_args[@]}"

    if ! ./scripts/wait-for-port.sh 8001 30 "Gateway" || \
        ! ./scripts/wait-for-port.sh 3000 120 "Frontend" || \
        ! ./scripts/wait-for-port.sh 2026 10 "Nginx"; then
        echo "✗ Background supervisor did not finish starting the full stack."
        launchctl remove "$label" >/dev/null 2>&1 || true
        [ -f "$stderr_log" ] && tail -20 "$stderr_log"
        return 1
    fi

    echo "✓ Background supervisor is running ($label)"
    echo "  🌐 http://localhost:2026"
    echo "  📋 Supervisor logs: $stdout_log and $stderr_log"
    echo "  🛑 Stop: make stop"
}

if [ ! -f "$ACT_WEAVE_CONFIG_PATH" ]; then
    echo "✗ No ActWeave config file found."
    echo "  Run 'make setup' (recommended) or 'make config' to generate config.yaml."
    exit 1
fi

# ── Install dependencies ────────────────────────────────────────────────────

# macOS terminal/process runners may tear down nohup descendants when their
# owning execution session exits. Keep the existing POSIX fallback below for
# other hosts, but use launchd here so a foreground parent remains responsible
# for the complete local stack and its cleanup.
if $DAEMON_MODE && [ "$(uname -s)" = "Darwin" ]; then
    _start_macos_daemon
    exit $?
fi

# Pick a runnable Python for the extras detector. On Windows/Git Bash,
# `python3` can resolve to the Microsoft Store alias in WindowsApps, which is
# present on PATH but not executable from Bash.
DETECT_PYTHON="$ACT_WEAVE_PNPM_PYTHON"

# Resolve existing optional extras (for example ollama or discord) from
# UV_EXTRAS or config.yaml so that
# `uv sync` does not wipe out optional dependencies on every restart. See
# scripts/detect_uv_extras.py and Issue #2754 for context. The detector
# whitelists extra names against `^[A-Za-z][A-Za-z0-9_-]*$`, so the unquoted
# splat below only sees valid uv argument tokens.
#
# Stderr is intentionally NOT redirected so the user sees:
#   - whitelist warnings (e.g. "ignoring invalid UV_EXTRAS entry ';'");
#   - detector crashes (e.g. unexpected Python error).
# `|| true` keeps `set -e` from killing dev startup on a detector failure;
# the result is just an empty UV_EXTRAS_FLAGS, which means "no extras".
UV_EXTRAS_FLAGS=""
if [ -n "$DETECT_PYTHON" ]; then
    UV_EXTRAS_FLAGS=$("$DETECT_PYTHON" "$REPO_ROOT/scripts/detect_uv_extras.py" || { echo "[serve.sh] detect_uv_extras.py failed (exit $?) — proceeding without extras" >&2; echo ""; })
fi

if ! $SKIP_INSTALL; then
    echo "Syncing dependencies..."
    if [ -n "$UV_EXTRAS_FLAGS" ]; then
        echo "  • uv extras: $UV_EXTRAS_FLAGS"
    fi
    # `--all-packages` propagates selected extras into workspace members.
    # Intentionally unquoted to splat multiple `--extra X` pairs.
    (cd backend && uv sync --quiet --all-packages $UV_EXTRAS_FLAGS) || { echo "✗ Backend dependency install failed"; exit 1; }
    "$ACT_WEAVE_PNPM_PYTHON" "$ACT_WEAVE_PNPM_RUNNER" install --silent || { echo "✗ Frontend dependency install failed"; exit 1; }
    echo "✓ Dependencies synced"
else
    echo "⏩ Skipping dependency install (--skip-install)"
fi

# ── Banner ───────────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo "  Starting ActWeave"
echo "=========================================="
echo ""
echo "  Mode: $MODE_LABEL"
echo ""
echo "  Services:"
echo "    Gateway     → localhost:8001  (admission/query/SSE)"
echo "    Worker      → background      (Agent graph execution)"
echo "    Scheduler   → background      (Automation polling)"
echo "    Frontend    → localhost:3000  (Next.js)"
echo "    Nginx       → localhost:2026  (reverse proxy)"
echo ""

# ── Cleanup handler ──────────────────────────────────────────────────────────

STARTED_PIDS=()
STARTED_PROCESS_NAMES=()

remember_started_pid() {
    STARTED_PIDS+=("$1")
    STARTED_PROCESS_NAMES+=("$2")
}

stop_started() {
    local pid
    for pid in "${STARTED_PIDS[@]}"; do
        [ -n "$pid" ] || continue
        kill_process_tree "$pid"
    done
    STARTED_PIDS=()
    STARTED_PROCESS_NAMES=()
}

kill_process_tree() {
    local pid="$1" child
    while IFS= read -r child; do
        [ -n "$child" ] && kill_process_tree "$child"
    done < <(pgrep -P "$pid" 2>/dev/null || true)
    kill "$pid" 2>/dev/null || true
}

startup_failure() {
    local status="${1:-1}"
    trap - INT TERM
    stop_started
    exit "$status"
}

cleanup() {
    local status="${1:-0}"
    trap - INT TERM
    echo ""
    stop_started
    exit "$status"
}

# Bash 3.2 (the macOS system Bash) has no `wait -n`. Poll every child that this
# launcher started so one required process exiting tears down the whole stack.
# Returning a fixed non-zero status lets launchd's keepalive restart a complete
# stack instead of leaving Gateway/Frontend alive without Worker execution.
supervise_started_processes() {
    local index pid name child_status
    while true; do
        for ((index = 0; index < ${#STARTED_PIDS[@]}; index++)); do
            pid="${STARTED_PIDS[$index]}"
            [ -n "$pid" ] || continue
            kill -0 "$pid" 2>/dev/null && continue

            if wait "$pid"; then
                child_status=0
            else
                child_status=$?
            fi
            STARTED_PIDS[$index]=""
            name="${STARTED_PROCESS_NAMES[$index]}"
            echo "✗ $name exited after startup (status $child_status); stopping the remaining services." >&2
            return 1
        done
        sleep 1
    done
}

trap 'cleanup 130' INT
trap 'cleanup 143' TERM

# ── Helper: start a service ──────────────────────────────────────────────────

# run_service NAME COMMAND PORT TIMEOUT
# On non-macOS hosts, daemon mode wraps with nohup. Waits for port to be ready.
run_service() {
    local name="$1" cmd="$2" port="$3" timeout="$4"

    if _is_port_listening "$port"; then
        echo "✗ $name cannot start because port $port is already in use."
        echo "  If it belongs to this worktree, run 'make stop'; otherwise free the port manually."
        startup_failure 1
    fi

    echo "Starting $name..."
    if $DAEMON_MODE; then
        # Tag the daemon so every descendant (pnpm → next → next-server)
        # carries ACT_WEAVE_DAEMON_ROOT in its environment, letting
        # _is_deerflow_pid recognize it at stop time.
        nohup env ACT_WEAVE_DAEMON_ROOT="$REPO_ROOT" sh -c "$cmd" > /dev/null 2>&1 &
    else
        sh -c "$cmd" &
    fi
    remember_started_pid "$!" "$name"

    ./scripts/wait-for-port.sh "$port" "$timeout" "$name" || {
        local logfile="$LOG_ROOT/$(echo "$name" | tr '[:upper:]' '[:lower:]' | tr ' ' '-').log"
        echo "✗ $name failed to start."
        [ -f "$logfile" ] && tail -20 "$logfile"
        startup_failure 1
    }
    echo "✓ $name started on localhost:$port"
}

# run_process NAME COMMAND LOGFILE
# Background roles have no public port. They must remain alive through their
# startup window; Gateway health later reports their durable DB registration.
run_process() {
    local name="$1" cmd="$2" logfile="$3" pid attempt
    echo "Starting $name..."
    if $DAEMON_MODE; then
        nohup env ACT_WEAVE_DAEMON_ROOT="$REPO_ROOT" sh -c "$cmd" > /dev/null 2>&1 &
    else
        sh -c "$cmd" &
    fi
    pid=$!
    remember_started_pid "$pid" "$name"
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || {
            echo "✗ $name failed to start."
            [ -f "$logfile" ] && tail -20 "$logfile"
            startup_failure 1
        }
        sleep 0.2
    done
    echo "✓ $name started"
}

# ── Start services ───────────────────────────────────────────────────────────

mkdir -p "$LOG_ROOT"
mkdir -p "$RUNTIME_ROOT/temp/client_body_temp" "$RUNTIME_ROOT/temp/proxy_temp" "$RUNTIME_ROOT/temp/fastcgi_temp" "$RUNTIME_ROOT/temp/uwsgi_temp" "$RUNTIME_ROOT/temp/scgi_temp"

# 1. Gateway API
run_service "Gateway" \
    "cd backend && exec env PYTHONPATH=. uv run uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001 $GATEWAY_EXTRA_FLAGS > '$LOG_ROOT/gateway.log' 2>&1" \
    8001 30

# 2. Required durable Worker
run_process "Worker" \
    "cd backend && exec env PYTHONPATH=. uv run python -m app.worker.app > '$LOG_ROOT/worker.log' 2>&1" \
    "$LOG_ROOT/worker.log"

# 3. Independent Scheduler. Platform automations.enabled controls polling;
# the process still owns Memory Dream/Seal admission.
run_process "Scheduler" \
    "cd backend && exec env PYTHONPATH=. uv run python -m app.scheduler.app > '$LOG_ROOT/scheduler.log' 2>&1" \
    "$LOG_ROOT/scheduler.log"

# 4. Frontend
run_service "Frontend" \
    "cd frontend && exec $FRONTEND_CMD > '$LOG_ROOT/frontend.log' 2>&1" \
    3000 120

# 5. Nginx
run_service "Nginx" \
    "exec nginx -g 'daemon off;' -c '$REPO_ROOT/docker/nginx/nginx.local.conf' -p '$RUNTIME_ROOT' > '$LOG_ROOT/nginx.log' 2>&1" \
    2026 10

# ── Ready ────────────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo "  ✓ ActWeave is running!  [$MODE_LABEL]"
echo "=========================================="
echo ""
echo "  🌐 http://localhost:2026"
echo ""
echo "  Routing: Frontend → Nginx → Gateway"
echo "  API:       /api/*  →  Gateway admission/query/SSE (8001)"
echo "  Execution: durable jobs → Worker Agent graph execution"
echo ""
echo "  📋 Logs: $LOG_ROOT/{gateway,worker,scheduler,frontend,nginx}.log"
echo ""

if $DAEMON_MODE; then
    echo "  🛑 Stop: make stop"
    # Detach — trap is no longer needed
    trap - INT TERM
else
    echo "  Press Ctrl+C to stop all services"
    supervise_started_processes || cleanup "$?"
fi
