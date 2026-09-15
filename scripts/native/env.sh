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
