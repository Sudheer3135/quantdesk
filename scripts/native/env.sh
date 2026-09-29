# Shared settings for running QuantDesk directly on the Mac, without Docker.
# Sourced by scripts/start.sh, stop.sh and status.sh. The project path ends
# in a space, so every path below is quoted where it is used.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PG_BIN="/opt/homebrew/opt/postgresql@16/bin"
PG_DATA="/opt/homebrew/var/postgresql@16"
PG_PORT=5433            # 5432 belongs to the separately installed PostgreSQL 17

REDIS_PORT=6379
API_PORT=8000
WEB_PORT=5173

# Redis cannot take a path containing a space on its command line, so its
# runtime files live under the home directory; everything else logs into
# the project's ignored logs/ folder.
RUN_DIR="$HOME/.quantdesk/run"
LOG_DIR="$ROOT/logs/native"

# The API process: what start.sh launches and stop.sh looks for.
PYTHON="$ROOT/.venv/bin/python"
UVICORN="$ROOT/.venv/bin/uvicorn"
BACKEND_DIR="$ROOT/backend"
API_APP="app.main:app"
# Seconds stop.sh waits if neither the recorded deadline nor the policy can
# be read. Never shorter than the policy's default (10 + 120 + 15).
API_STOP_FALLBACK=300

# The backend reads .env from the directory it starts in. These override the
# Docker hostnames in .env (postgres, redis) with the native services;
# environment variables win over the file in pydantic-settings.
export DATABASE_URL="postgresql+psycopg://quant:quant@localhost:${PG_PORT}/quantdesk"
export REDIS_URL="redis://localhost:${REDIS_PORT}/0"

port_busy() { nc -z 127.0.0.1 "$1" >/dev/null 2>&1; }

# The process listening on a port — the server itself, not whatever shell
# launched it. Recording the launcher's pid was a real bug: stop.sh reported
# "stopped api" while uvicorn went on serving.
listener_pid() { lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1; }

# That pid only if the process is this project's (its working directory is
# inside ROOT). On 28-Aug a different app held :8000 and :5173; these scripts
# must never stop, or mistake for QuantDesk, something that isn't.
our_listener() {
  local pid cwd
  pid="$(listener_pid "$1")"
  [ -n "$pid" ] || return 1
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
  case "$cwd" in
    "$ROOT"|"$ROOT"/*) echo "$pid" ;;
    *) return 1 ;;
  esac
}

# The API's shutdown deadline, from its own settings (app.shutdown_policy):
# uvicorn's grace for open connections, the writer drain, and a margin for
# the exit. Sets API_SHUTDOWN_GRACE and API_STOP_DEADLINE, whole seconds.
# Read from the project root with the same environment the API gets —
# Settings finds .env relative to the working directory, and the API runs
# from the root; read from backend/ it would silently use the defaults.
api_shutdown_policy() {
  local out
  out="$(cd "$ROOT" && PYTHONPATH="$BACKEND_DIR${PYTHONPATH:+:$PYTHONPATH}" \
         "$PYTHON" -m app.shutdown_policy 2>/dev/null)" || return 1
  API_SHUTDOWN_GRACE="$(printf '%s\n' "$out" | sed -n 's/^grace=\([0-9][0-9]*\)$/\1/p')"
  API_STOP_DEADLINE="$(printf '%s\n' "$out" | sed -n 's/^deadline=\([0-9][0-9]*\)$/\1/p')"
  [ -n "$API_SHUTDOWN_GRACE" ] && [ -n "$API_STOP_DEADLINE" ]
}

# Launch the API with the policy's connection grace, and record the
# deadline stop.sh must allow this process — read now, from the settings it
# starts with, so a later change to them cannot shorten its shutdown. The
# API is told that deadline too, and refuses to start if its own effective
# policy would not fit inside it (shutdown_policy.check_supervisor).
# fd 9 — the lifecycle lock — is closed for the API, or the running API
# would hold the lock for its whole life. It has to be its own `exec 9>&-`:
# bash 3.2 keeps a copy of fd 9 in the program when `9>&-` is written on
# the exec'd command itself.
start_api() {
  api_shutdown_policy || { echo "cannot read the API shutdown policy (app.shutdown_policy)"; return 1; }
  echo "$API_STOP_DEADLINE" >"$RUN_DIR/backend.stop_deadline"
  # Started from the project root so the backend finds .env. No --reload:
  # a file watcher running all session is heat for nothing on a trading day.
  ( exec 9>&-
    cd "$ROOT" && QUANTDESK_STOP_DEADLINE_SECONDS="$API_STOP_DEADLINE" \
      exec nohup "$UVICORN" "$API_APP" --app-dir "$BACKEND_DIR" \
      --host 127.0.0.1 --port "$API_PORT" \
      --timeout-graceful-shutdown "$API_SHUTDOWN_GRACE" >>"$LOG_DIR/backend.log" 2>&1 ) &
}

# One lifecycle operation at a time: start.sh and stop.sh each hold this
# for their whole run, so a start cannot launch an API while a stop is
# between "the old API is gone" and "its database is stopped", and two
# starts or two stops cannot interleave. A kernel lock (flock) on fd 9 of
# the calling script: released when that script exits however it exits,
# kill -9 included, so it cannot go stale. Every long-lived process the
# scripts launch is started in a subshell that first runs `exec 9>&-`, or it
# would inherit the lock (see start_api).
# Separate from the database writer lease, which it never touches.
lifecycle_lock() {  # operation name
  # The path is taken when the lock is, not when this file is sourced, so
  # it always follows the RUN_DIR in effect.
  LIFECYCLE_LOCK="$RUN_DIR/lifecycle.lock"
  mkdir -p "$RUN_DIR"
  exec 9>>"$LIFECYCLE_LOCK"
  if ! "$PYTHON" -c 'import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' 2>/dev/null; then
    echo "another QuantDesk lifecycle operation is in progress:" \
         "$(cat "$LIFECYCLE_LOCK.owner" 2>/dev/null || echo unknown)"
    echo "wait for it to finish, then run $1 again"
    exec 9>&-
    return 1
  fi
  echo "$1 (pid $$, since $(date '+%H:%M:%S'))" >"$LIFECYCLE_LOCK.owner"
}

# A pid only if it is this project's API: uvicorn serving $API_APP, working
# directory inside ROOT.
is_our_api() {
  local cwd
  kill -0 "$1" 2>/dev/null || return 1
  case "$(ps -o command= -p "$1" 2>/dev/null)" in
    *uvicorn*"$API_APP"*) ;;
    *) return 1 ;;
  esac
  cwd="$(lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
  case "$cwd" in "$ROOT"|"$ROOT"/*) return 0 ;; *) return 1 ;; esac
}

# Every live API process of this project. Not only the listener: uvicorn
# closes its socket the moment it starts shutting down, and a draining API
# has no port left to be found by.
api_pids() {
  local pid candidates
  candidates="$(cat "$RUN_DIR/backend.pid" 2>/dev/null) $(listener_pid "$API_PORT") \
$(pgrep -f "uvicorn.*$API_APP" 2>/dev/null)"
  for pid in $candidates; do
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    is_our_api "$pid" && echo "$pid"
  done | sort -u | tr '\n' ' '
}
