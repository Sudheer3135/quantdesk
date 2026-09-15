#!/usr/bin/env bash
# Stop everything scripts/start.sh started, and confirm it actually stopped.
# Nothing keeps running afterwards, so the Mac is idle once the market closes.
set -uo pipefail
source "$(dirname "$0")/native/env.sh"

stop_server() {  # name, port, pidfile
  local name="$1" port="$2" pidfile="$3" pids="" pid
  [ -f "$pidfile" ] && pids="$(cat "$pidfile")"
  pid="$(our_listener "$port")" && pids="$pids $pid"
  pids="$(printf '%s\n' $pids | sort -u | tr '\n' ' ')"

  local stopped=0
  for pid in $pids; do
    kill -0 "$pid" 2>/dev/null || continue
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    stopped=1
  done
  rm -f "$pidfile"

  # Believe the port, not the kill. This is the check the first version
  # lacked, which is how it printed "stopped" over a server still serving.
  if our_listener "$port" >/dev/null; then
    echo "FAILED to stop $name — still listening on :$port"
    return 1
  elif port_busy "$port"; then
    echo "$name stopped; :$port is held by another application (left alone)"
  elif [ "$stopped" = 1 ]; then
    echo "stopped $name"
  else
    echo "$name was not running"
  fi
}

status=0
stop_server dashboard "$WEB_PORT" "$RUN_DIR/frontend.pid" || status=1
stop_server api "$API_PORT" "$RUN_DIR/backend.pid" || status=1   # before the database

if port_busy "$REDIS_PORT"; then
  redis-cli -p "$REDIS_PORT" shutdown nosave >/dev/null 2>&1
  for _ in $(seq 1 20); do port_busy "$REDIS_PORT" || break; sleep 0.25; done
  port_busy "$REDIS_PORT" && { echo "FAILED to stop cache"; status=1; } || echo "stopped cache"
else
  echo "cache was not running"
fi
rm -f "$RUN_DIR/redis.pid"

if "$PG_BIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1; then
  "$PG_BIN/pg_ctl" -D "$PG_DATA" -m fast -w stop >/dev/null && echo "stopped database" || { echo "FAILED to stop database"; status=1; }
else
  echo "database was not running"
fi
exit $status
