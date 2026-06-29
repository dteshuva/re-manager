#!/usr/bin/env bash
# Full-stack dev server: database + backend + frontend with auto-recovery
# To run: ./dev.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/.dev-logs"
mkdir -p "$LOG_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# PIDs for cleanup
declare -a PIDS=()

CLEANED_UP=false
cleanup() {
  if $CLEANED_UP; then return; fi
  CLEANED_UP=true
  echo -e "\n${YELLOW}Shutting down...${NC}"

  # Kill all child processes
  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done

  # Give them a moment to exit gracefully
  sleep 1

  # Force kill any remaining processes
  for pid in "${PIDS[@]}"; do
    kill -9 "$pid" 2>/dev/null || true
  done

  # Stop the database
  echo -e "${BLUE}Stopping database...${NC}"
  cd "$ROOT/backend"
  bash scripts/dev_db.sh stop 2>&1 | grep -v "could not open" || true

  echo -e "${GREEN}Shutdown complete${NC}"
  exit 0
}

trap cleanup SIGINT SIGTERM EXIT

log_info() {
  echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $1"
}

log_success() {
  echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓${NC} $1"
}

log_error() {
  echo -e "${RED}[$(date '+%H:%M:%S')] ✗${NC} $1"
}

log_warn() {
  echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠${NC} $1"
}

# Check if port is in use
port_in_use() {
  lsof -i ":$1" >/dev/null 2>&1
}

# Kill process on port
kill_port() {
  local port=$1
  if port_in_use "$port"; then
    log_warn "Port $port in use, killing existing process..."
    lsof -i ":$port" -t | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
}

# Start database
start_database() {
  log_info "Starting database..."

  # Stop any existing instance
  cd "$ROOT/backend"
  bash scripts/dev_db.sh stop 2>&1 | grep -v "could not open" || true
  sleep 1

  # Start fresh
  if bash scripts/dev_db.sh start > "$LOG_DIR/db.log" 2>&1; then
    log_success "Database started (port 5433)"
    sleep 2 # Give DB time to be ready
    return 0
  else
    log_error "Failed to start database. See $LOG_DIR/db.log"
    cat "$LOG_DIR/db.log"
    return 1
  fi
}

# Start backend
start_backend() {
  log_info "Starting backend..."

  # Kill any existing process on 8000
  kill_port 8000

  cd "$ROOT/backend"

  # Ensure migrations are up to date
  if ! PYTHONPATH=.pydeps .pydeps/bin/alembic upgrade head > "$LOG_DIR/migrations.log" 2>&1; then
    log_error "Failed to run migrations. See $LOG_DIR/migrations.log"
    cat "$LOG_DIR/migrations.log"
    return 1
  fi

  # Seed only on a fresh/empty database — seed.py wipes and reseeds on every
  # run, which takes ~40s, so skip it once real data exists.
  local prop_count
  prop_count="$(PGPASSWORD= psql -h localhost -p 5433 -U postgres -d re_manager -tAc \
    "SELECT count(*) FROM properties" 2>/dev/null || echo 0)"

  if [ "$prop_count" = "0" ]; then
    log_info "Empty database, seeding sample data (~40s)..."
    if ! PYTHONPATH=.pydeps python3 -m app.seed > "$LOG_DIR/seed.log" 2>&1; then
      log_error "Failed to seed database. See $LOG_DIR/seed.log"
      cat "$LOG_DIR/seed.log"
      return 1
    fi
  else
    log_info "Database already has data ($prop_count properties), skipping seed"
  fi

  # Start the server
  PYTHONPATH=.pydeps .pydeps/bin/uvicorn app.main:app --port 8000 --reload > "$LOG_DIR/backend.log" 2>&1 &
  PIDS+=($!)

  # Give it a moment to start
  sleep 2

  if port_in_use 8000; then
    log_success "Backend started (port 8000)"
    return 0
  else
    log_error "Backend failed to start. See $LOG_DIR/backend.log"
    cat "$LOG_DIR/backend.log"
    return 1
  fi
}

# Start frontend
start_frontend() {
  log_info "Starting frontend..."

  # Kill any existing process on 5173 (default Vite port)
  kill_port 5173

  cd "$ROOT/frontend"

  # Ensure dependencies are installed
  if [ ! -d "node_modules" ]; then
    log_info "Installing frontend dependencies..."
    npm install > "$LOG_DIR/npm-install.log" 2>&1
  fi

  # Type check
  if ! node_modules/.bin/tsc --noEmit > "$LOG_DIR/tsc.log" 2>&1; then
    log_error "TypeScript errors found. See $LOG_DIR/tsc.log"
    cat "$LOG_DIR/tsc.log"
    return 1
  fi

  # Start dev server
  npm run dev > "$LOG_DIR/frontend.log" 2>&1 &
  PIDS+=($!)

  # Give it a moment to start
  sleep 3

  if port_in_use 5173; then
    log_success "Frontend started (port 5173)"
    return 0
  else
    log_error "Frontend failed to start. See $LOG_DIR/frontend.log"
    cat "$LOG_DIR/frontend.log"
    return 1
  fi
}

# Main startup
main() {
  log_info "=========================================="
  log_info "Starting re-manager full stack"
  log_info "=========================================="
  log_info "Logs: $LOG_DIR"

  local retries=0
  local max_retries=2

  while [ $retries -lt $((max_retries + 1)) ]; do
    if [ $retries -gt 0 ]; then
      log_warn "Retry attempt $retries of $max_retries..."
    fi

    # Start database (only once, don't retry)
    if [ $retries -eq 0 ]; then
      if ! start_database; then
        log_error "Cannot proceed without database"
        exit 1
      fi
    fi

    # Start backend and frontend
    local backend_ok=true
    local frontend_ok=true

    start_backend || backend_ok=false
    start_frontend || frontend_ok=false

    if $backend_ok && $frontend_ok; then
      log_success "=========================================="
      log_success "All services running!"
      log_success "=========================================="
      log_info "Backend:  http://localhost:8000"
      log_info "Frontend: http://localhost:5173"
      log_info "Database: localhost:5433"
      log_info ""
      log_info "Press Ctrl+C to stop all services"
      log_info "=========================================="

      # Monitor for any dead processes and keep the script running
      while true; do
        sleep 5
        for pid in "${PIDS[@]}"; do
          if ! kill -0 "$pid" 2>/dev/null; then
            log_error "A service died unexpectedly. Restarting..."
            # Re-enter retry loop
            retries=$((max_retries + 1))
            break 2
          fi
        done
      done
    else
      retries=$((retries + 1))
      if [ $retries -le $max_retries ]; then
        log_warn "Some services failed. Retrying in 5 seconds..."
        # Kill any partial starts
        for pid in "${PIDS[@]}"; do
          kill -9 "$pid" 2>/dev/null || true
        done
        PIDS=()
        sleep 5
      fi
    fi
  done

  log_error "Failed to start all services after $max_retries retries"
  exit 1
}

main
