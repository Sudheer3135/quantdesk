#!/usr/bin/env bash
# What is running, and is the API healthy.
set -uo pipefail
source "$(dirname "$0")/native/env.sh"
row() { printf "%-10s %s\n" "$1" "$2"; }
"$PG_BIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1 && row database "running on :$PG_PORT" || row database "stopped"
port_busy "$REDIS_PORT" && row cache "running on :$REDIS_PORT" || row cache "stopped"
if curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then
  row api "healthy on :$API_PORT"
  row market "$(curl -fsS "http://127.0.0.1:$API_PORT/market/status" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["session"], "-", d.get("reason") or "")')"
  row feed "$(curl -fsS "http://127.0.0.1:$API_PORT/health/feed" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("price via", d.get("live_price_source"), "/", d.get("transport"))')"
else
  row api "not answering"
fi
port_busy "$WEB_PORT" && row dashboard "http://localhost:$WEB_PORT" || row dashboard "stopped"
row logs "$LOG_DIR"
