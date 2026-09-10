#!/usr/bin/env bash
# ==============================================================================
# Nebula Services Management CLI (start | stop | restart | status | logs)
# ==============================================================================

set -e

# Detect project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load and export environment variables from .env if present
# Загрузка и экспорт переменных окружения из .env при наличии
if [[ -f "$ROOT_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT_DIR/.env"
    set +a
fi

RUN_DIR="$ROOT_DIR/.run"
LOG_DIR="$RUN_DIR/logs"

# Absolute canonical storage paths (bilingual config contract)
export NEBULA_DATA_DIR="${NEBULA_DATA_DIR:-$ROOT_DIR/data}"
export NEBULA_CAPTURE_SPOOL_DIR="${NEBULA_CAPTURE_SPOOL_DIR:-$NEBULA_DATA_DIR/spool_capture}"
export NEBULA_BACKEND_SPOOL_DIR="${NEBULA_BACKEND_SPOOL_DIR:-$NEBULA_DATA_DIR/spool_backend}"
export NEBULA_DB_PATH="${NEBULA_DB_PATH:-$NEBULA_DATA_DIR/nebula.db}"
export NEBULA_BACKUP_DIR="${NEBULA_BACKUP_DIR:-$NEBULA_DATA_DIR/backups}"

mkdir -p "$RUN_DIR"
mkdir -p "$LOG_DIR"
mkdir -p "$NEBULA_DATA_DIR"
mkdir -p "$NEBULA_CAPTURE_SPOOL_DIR"
mkdir -p "$NEBULA_BACKEND_SPOOL_DIR"
mkdir -p "$NEBULA_BACKUP_DIR"

# Color definitions
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

is_running() {
    local pid_file="$1"
    if [[ -f "$pid_file" ]]; then
        local pid
        pid=$(cat "$pid_file" 2>/dev/null || echo "")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

wait_for_url() {
    local url="$1"
    local max_retries="${2:-20}"
    local delay="${3:-0.5}"
    local count=0

    while [[ $count -lt $max_retries ]]; do
        if curl -s -f -o /dev/null "$url" 2>/dev/null; then
            return 0
        fi
        sleep "$delay"
        count=$((count + 1))
    done
    return 1
}

# ------------------------------------------------------------------------------
# START COMMANDS
# ------------------------------------------------------------------------------
start_backend() {
    local pid_file="$RUN_DIR/backend.pid"
    local log_file="$LOG_DIR/backend.log"

    if is_running "$pid_file"; then
        echo -e "${YELLOW}● Backend API already running (PID: $(cat "$pid_file"))${NC}"
        return 0
    fi

    # Check if port 8000 is occupied by an external process
    local port_occupier
    port_occupier=$(lsof -ti :8000 2>/dev/null || echo "")
    if [[ -n "$port_occupier" ]]; then
        echo -e "${YELLOW}Port 8000 is already in use by PID $port_occupier. Attempting to terminate...${NC}"
        kill -9 "$port_occupier" 2>/dev/null || true
        sleep 1
    fi

    echo -ne "  Starting Backend API (FastAPI)... "
    cd "$ROOT_DIR"
    nohup uv run uvicorn backend.api.app:app --host 127.0.0.1 --port 8000 > "$log_file" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$pid_file"

    if wait_for_url "http://127.0.0.1:8000/healthz" 20 0.4; then
        echo -e "${GREEN}[OK]${NC} (PID: $new_pid, http://127.0.0.1:8000)"
    else
        echo -e "${RED}[FAILED]${NC}"
        echo -e "${RED}Backend failed to start. Last log lines:${NC}"
        tail -n 10 "$log_file"
        rm -f "$pid_file"
        return 1
    fi
}

start_worker() {
    local pid_file="$RUN_DIR/worker.pid"
    local log_file="$LOG_DIR/worker.log"

    if is_running "$pid_file"; then
        echo -e "${YELLOW}● Worker daemon already running (PID: $(cat "$pid_file"))${NC}"
        return 0
    fi

    echo -ne "  Starting Pipeline Worker (STT + LLM daemon)... "
    cd "$ROOT_DIR"
    nohup uv run python -m backend.workers.pipeline > "$log_file" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$pid_file"
    sleep 0.8

    if kill -0 "$new_pid" 2>/dev/null; then
        echo -e "${GREEN}[OK]${NC} (PID: $new_pid)"
    else
        echo -e "${RED}[FAILED]${NC}"
        echo -e "${RED}Worker failed to start. Last log lines:${NC}"
        tail -n 10 "$log_file"
        rm -f "$pid_file"
        return 1
    fi
}

start_desktop() {
    local pid_file="$RUN_DIR/desktop.pid"
    local log_file="$LOG_DIR/desktop.log"

    if is_running "$pid_file"; then
        echo -e "${YELLOW}● Desktop App already running (PID: $(cat "$pid_file"))${NC}"
        return 0
    fi

    # Check if port 1420 is occupied by an orphaned vite process
    local port_occupier
    port_occupier=$(lsof -ti :1420 2>/dev/null || echo "")
    if [[ -n "$port_occupier" ]]; then
        kill -9 $port_occupier 2>/dev/null || true
        sleep 0.5
    fi

    echo -ne "  Starting Desktop Shell (Tauri 2 + React)... "
    cd "$ROOT_DIR"
    nohup npm --prefix apps/desktop run tauri dev > "$log_file" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$pid_file"
    sleep 2.0

    if kill -0 "$new_pid" 2>/dev/null; then
        echo -e "${GREEN}[OK]${NC} (PID: $new_pid, logging to .run/logs/desktop.log)"
    else
        echo -e "${RED}[FAILED]${NC}"
        echo -e "${RED}Desktop app failed to start. Last log lines:${NC}"
        tail -n 15 "$log_file"
        rm -f "$pid_file"
        return 1
    fi
}

# ------------------------------------------------------------------------------
# STOP COMMANDS
# ------------------------------------------------------------------------------
stop_process() {
    local name="$1"
    local pid_file="$2"

    if ! is_running "$pid_file"; then
        echo -e "  $name is not running."
        rm -f "$pid_file"
        return 0
    fi

    local pid
    pid=$(cat "$pid_file")
    echo -ne "  Stopping $name (PID: $pid)... "

    kill -15 "$pid" 2>/dev/null || true

    # Wait up to 5 seconds for graceful shutdown
    local count=0
    while kill -0 "$pid" 2>/dev/null && [[ $count -lt 10 ]]; do
        sleep 0.5
        count=$((count + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
        echo -e "${YELLOW}[FORCE KILLED]${NC}"
    else
        echo -e "${GREEN}[STOPPED]${NC}"
    fi

    rm -f "$pid_file"
}

stop_backend() {
    stop_process "Backend API" "$RUN_DIR/backend.pid"
    # Ensure port 8000 is fully freed
    local p
    p=$(lsof -ti :8000 2>/dev/null || echo "")
    if [[ -n "$p" ]]; then
        kill -9 $p 2>/dev/null || true
    fi
}

stop_worker() {
    stop_process "Pipeline Worker" "$RUN_DIR/worker.pid"
}

stop_desktop() {
    stop_process "Desktop App" "$RUN_DIR/desktop.pid"
    # Ensure port 1420 and any child vite or nebula-desktop processes are cleaned up
    local p
    p=$(lsof -ti :1420 2>/dev/null || echo "")
    if [[ -n "$p" ]]; then
        kill -9 $p 2>/dev/null || true
    fi
    pkill -f "nebula-desktop" 2>/dev/null || true
    pkill -f "vite" 2>/dev/null || true
}

# ------------------------------------------------------------------------------
# STATUS COMMAND
# ------------------------------------------------------------------------------
print_status() {
    echo -e "${BOLD}======================================================${NC}"
    echo -e "${BOLD}       NEBULA SYSTEM STATUS & HEALTH CHECK            ${NC}"
    echo -e "${BOLD}======================================================${NC}"

    # Backend
    local backend_pid_file="$RUN_DIR/backend.pid"
    if is_running "$backend_pid_file"; then
        local b_pid
        b_pid=$(cat "$backend_pid_file")
        local health_out
        health_out=$(curl -s "http://127.0.0.1:8000/healthz" 2>/dev/null || echo "")
        if [[ -n "$health_out" ]]; then
            echo -e "  Backend API:     ${GREEN}● RUNNING${NC} (PID: $b_pid, port: 8000, healthz: OK)"
        else
            echo -e "  Backend API:     ${YELLOW}● HANGING / UNRESPONSIVE${NC} (PID: $b_pid)"
        fi
    else
        echo -e "  Backend API:     ${RED}○ STOPPED${NC}"
    fi

    # Worker
    local worker_pid_file="$RUN_DIR/worker.pid"
    if is_running "$worker_pid_file"; then
        local w_pid
        w_pid=$(cat "$worker_pid_file")
        echo -e "  Pipeline Worker: ${GREEN}● RUNNING${NC} (PID: $w_pid, active queue processing)"
    else
        echo -e "  Pipeline Worker: ${RED}○ STOPPED${NC}"
    fi

    # Desktop
    local desktop_pid_file="$RUN_DIR/desktop.pid"
    if is_running "$desktop_pid_file"; then
        local d_pid
        d_pid=$(cat "$desktop_pid_file")
        echo -e "  Desktop Shell:   ${GREEN}● RUNNING${NC} (PID: $d_pid, Tauri 2 UI window)"
    else
        echo -e "  Desktop Shell:   ${RED}○ STOPPED${NC}"
    fi

    echo -e "${BOLD}------------------------------------------------------${NC}"

    # Database Integrity & Stats (if backend running)
    if is_running "$backend_pid_file"; then
        local integ_out
        integ_out=$(curl -s "http://127.0.0.1:8000/api/v1/system/integrity" 2>/dev/null || echo "")
        if [[ "$integ_out" =~ "true" ]]; then
            echo -e "  DB Integrity:    ${GREEN}● PRAGMA integrity_check: OK (SQLite WAL)${NC}"
        else
            echo -e "  DB Integrity:    ${RED}● Corrupted / Failed check${NC}"
        fi
    fi

    echo -e "${BOLD}======================================================${NC}"
}

# ------------------------------------------------------------------------------
# LOGS COMMAND
# ------------------------------------------------------------------------------
view_logs() {
    local target="${1:-all}"
    local follow="${2:-}"

    if [[ "$target" == "backend" ]]; then
        local file="$LOG_DIR/backend.log"
    elif [[ "$target" == "worker" ]]; then
        local file="$LOG_DIR/worker.log"
    elif [[ "$target" == "desktop" ]]; then
        local file="$LOG_DIR/desktop.log"
    else
        echo -e "${CYAN}Available log targets: backend | worker | desktop${NC}"
        echo -e "Example: ./scripts/nebula.sh logs backend -f"
        return 0
    fi

    if [[ ! -f "$file" ]]; then
        echo -e "${YELLOW}Log file $file does not exist yet.${NC}"
        return 0
    fi

    if [[ "$follow" == "-f" || "$follow" == "--follow" ]]; then
        tail -f "$file"
    else
        tail -n 40 "$file"
    fi
}

# ------------------------------------------------------------------------------
# CLI DISPATCHER
# ------------------------------------------------------------------------------
CMD="${1:-help}"
TARGET="${2:-all}"
FLAG="${3:-}"

case "$CMD" in
    start)
        echo -e "${BOLD}Starting Nebula services [$TARGET]...${NC}"
        if [[ "$TARGET" == "backend" ]]; then
            start_backend
        elif [[ "$TARGET" == "worker" ]]; then
            start_worker
        elif [[ "$TARGET" == "desktop" ]]; then
            start_desktop
        elif [[ "$TARGET" == "all" || -z "$TARGET" ]]; then
            start_backend
            start_worker
            start_desktop
        else
            echo -e "${RED}Unknown start target: $TARGET. Choose: all, backend, worker, desktop${NC}"
            exit 1
        fi
        echo -e "${GREEN}${BOLD}Done.${NC}\n"
        print_status
        ;;

    stop)
        echo -e "${BOLD}Stopping Nebula services [$TARGET]...${NC}"
        if [[ "$TARGET" == "backend" ]]; then
            stop_backend
        elif [[ "$TARGET" == "worker" ]]; then
            stop_worker
        elif [[ "$TARGET" == "desktop" ]]; then
            stop_desktop
        elif [[ "$TARGET" == "all" || -z "$TARGET" ]]; then
            stop_desktop
            stop_worker
            stop_backend
        else
            echo -e "${RED}Unknown stop target: $TARGET. Choose: all, backend, worker, desktop${NC}"
            exit 1
        fi
        echo -e "${GREEN}${BOLD}Done.${NC}\n"
        print_status
        ;;

    restart)
        echo -e "${BOLD}Restarting Nebula services [$TARGET]...${NC}"
        "$0" stop "$TARGET"
        sleep 1
        "$0" start "$TARGET"
        ;;

    status)
        print_status
        ;;

    logs)
        view_logs "$TARGET" "$FLAG"
        ;;

    help|--help|-h)
        echo -e "${BOLD}Nebula Management Tool${NC}"
        echo -e "Usage: ./scripts/nebula.sh [command] [target] [options]\n"
        echo -e "${CYAN}Commands:${NC}"
        echo -e "  start   [all|backend|worker|desktop]   Start services in background"
        echo -e "  stop    [all|backend|worker|desktop]   Stop running services gracefully"
        echo -e "  restart [all|backend|worker|desktop]   Restart services"
        echo -e "  status                                 Show current status and health of all components"
        echo -e "  logs    [backend|worker|desktop] [-f]  View logs (-f for continuous live follow)"
        echo -e "\n${CYAN}Convenience scripts:${NC}"
        echo -e "  ./scripts/start.sh    (equivalent to ./scripts/nebula.sh start all)"
        echo -e "  ./scripts/stop.sh     (equivalent to ./scripts/nebula.sh stop all)"
        echo -e "  ./scripts/restart.sh  (equivalent to ./scripts/nebula.sh restart all)"
        echo -e "  ./scripts/status.sh   (equivalent to ./scripts/nebula.sh status)"
        ;;

    *)
        echo -e "${RED}Unknown command: $CMD${NC}"
        echo -e "Run './scripts/nebula.sh help' for usage."
        exit 1
        ;;
esac
