#!/usr/bin/env bash
# Start QuantDesk on the Mac: PostgreSQL 16, Redis, the API and the dashboard.
# Safe to run twice — anything already running is left alone.
set -euo pipefail
source "$(dirname "$0")/native/env.sh"
mkdir -p "$RUN_DIR" "$LOG_DIR"

# Two copies of the backend would open two Angel sessions on one account.
if command -v docker >/dev/null 2>&1 && docker ps -q --filter name=quantdesk-backend 2>/dev/null | grep -q .; then
  echo "The Docker version of QuantDesk is running. Stop it first: docker compose down"
  exit 1
fi

echo "database   ..."
if ! "$PG_BIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1; then
  LC_ALL=en_US.UTF-8 "$PG_BIN/pg_ctl" -D "$PG_DATA" -l "$LOG_DIR/postgres.log" -w start >/dev/null
fi
echo "           PostgreSQL 16 on :$PG_PORT"

echo "cache      ..."
if ! port_busy "$REDIS_PORT"; then
  redis-server --port "$REDIS_PORT" --bind 127.0.0.1 --daemonize yes \
    --pidfile "$RUN_DIR/redis.pid" --logfile "$RUN_DIR/redis.log" \
    --dir "$RUN_DIR" --save "" --appendonly no
  for _ in $(seq 1 20); do port_busy "$REDIS_PORT" && break; sleep 0.25; done
fi
echo "           Redis on :$REDIS_PORT"

# Checked, never applied. Starting used to run `alembic upgrade head`, which
# is how migration 0010 reached the live database on 28 Sep without anyone
# deciding it should. Migrating is ./scripts/migrate.sh, run on purpose.
echo "schema     ..."
if ! schema="$(cd "$ROOT/backend" && "$ROOT/.venv/bin/python" -m app.schema_check 2>&1)"; then
  echo "           $schema"
  echo "           API not started."
  exit 1
fi
echo "           $schema"

echo "api        ..."
if pid="$(our_listener "$API_PORT")"; then
  echo "           already running (pid $pid)"
elif port_busy "$API_PORT"; then
  echo "           :$API_PORT is held by another application, not QuantDesk — close it first"
  exit 1
else
  # Started from the project root so the backend finds .env. No --reload:
  # a file watcher running all session is heat for nothing on a trading day.
  ( cd "$ROOT" && exec nohup "$ROOT/.venv/bin/uvicorn" app.main:app --app-dir backend \
      --host 127.0.0.1 --port "$API_PORT" >>"$LOG_DIR/backend.log" 2>&1 ) &
  for i in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1 && break
    [ "$i" = 60 ] && { echo "           API did not come up — see logs/native/backend.log"; exit 1; }
    sleep 1
  done
  pid="$(our_listener "$API_PORT")"
fi
echo "$pid" >"$RUN_DIR/backend.pid"
echo "           http://127.0.0.1:$API_PORT"

echo "dashboard  ..."
if pid="$(our_listener "$WEB_PORT")"; then
  echo "           already running (pid $pid)"
elif port_busy "$WEB_PORT"; then
  echo "           :$WEB_PORT is held by another application, not QuantDesk — close it first"
  exit 1
else
  API_KEY_VALUE="$(sed -n 's/^API_KEY=//p' "$ROOT/.env" | tail -1)"
  # Vite is called directly on 127.0.0.1. The npm "dev" script binds
  # 0.0.0.0, which would publish the desk to the local network.
  ( cd "$ROOT/frontend" && VITE_API_URL="http://localhost:$API_PORT" VITE_API_KEY="$API_KEY_VALUE" \
      exec nohup ./node_modules/.bin/vite --host 127.0.0.1 --port "$WEB_PORT" --strictPort \
      >>"$LOG_DIR/frontend.log" 2>&1 ) &
  for i in $(seq 1 60); do
    port_busy "$WEB_PORT" && break
    [ "$i" = 60 ] && { echo "           dashboard did not come up — see logs/native/frontend.log"; exit 1; }
    sleep 0.5
  done
  pid="$(our_listener "$WEB_PORT")"
fi
echo "$pid" >"$RUN_DIR/frontend.pid"
echo "           http://localhost:$WEB_PORT"
echo
echo "QuantDesk is running. Stop it with: ./scripts/stop.sh"
