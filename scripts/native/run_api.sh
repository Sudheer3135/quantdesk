#!/usr/bin/env bash
# The API in the foreground, for a supervisor — systemd on the Azure host
# (scripts/native/systemd/quantdesk-api.service). The same launch start.sh
# makes: schema checked (never applied), uvicorn's grace and the stop
# deadline read from app.shutdown_policy, one process on 127.0.0.1. Only
# nohup is gone, so the supervisor owns the process and sees it exit.
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$RUN_DIR" "$LOG_DIR"

# One API, ever: the app's lifespan starts the scheduler, the Angel feed and
# the paper engine, and uvicorn runs the lifespan *before* it binds the port,
# so a second copy would trade and log in to Angel before failing to bind.
if pids="$(api_pids)" && [ -n "${pids// /}" ]; then
  echo "refusing to start: a QuantDesk API is already running (pid ${pids% })"
  exit 1
fi
if port_busy "$API_PORT"; then
  echo "refusing to start: :$API_PORT is already held"
  exit 1
fi

if ! schema="$(cd "$ROOT/backend" && "$PYTHON" -m app.schema_check 2>&1)"; then
  echo "$schema"
  echo "API not started."
  exit 1
fi
echo "$schema"

api_shutdown_policy || { echo "cannot read the API shutdown policy (app.shutdown_policy)"; exit 1; }
# The supervisor's stop timeout must cover the API's whole drain, or it
# would SIGKILL a drain in progress.
if [ "$API_STOP_DEADLINE" -gt "${QUANTDESK_SUPERVISOR_STOP_SECONDS:-$API_STOP_FALLBACK}" ]; then
  echo "refusing to start: the shutdown policy needs ${API_STOP_DEADLINE}s, the supervisor" \
       "allows ${QUANTDESK_SUPERVISOR_STOP_SECONDS:-$API_STOP_FALLBACK}s"
  exit 1
fi
echo "$API_STOP_DEADLINE" >"$RUN_DIR/backend.stop_deadline"
echo "$$" >"$RUN_DIR/backend.pid"

cd "$ROOT"
export QUANTDESK_STOP_DEADLINE_SECONDS="$API_STOP_DEADLINE"
exec "$UVICORN" "$API_APP" --app-dir "$BACKEND_DIR" \
  --host 127.0.0.1 --port "$API_PORT" \
  --timeout-graceful-shutdown "$API_SHUTDOWN_GRACE"
