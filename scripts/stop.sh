#!/usr/bin/env bash
# Stop everything scripts/start.sh started, and confirm it actually stopped.
# Nothing keeps running afterwards, so the Mac is idle once the market closes.
#
# Order matters (Pass 2E-B). The API is signalled once and given its whole
# shutdown deadline (app.shutdown_policy: connection grace + writer drain +
# exit margin) — it may be finishing a database write, and the verified
# drain either confirms every writer stopped or ends the process itself
# (exit 70). Only once the API process is gone are Redis and PostgreSQL
# stopped: a draining writer keeps its database until its transaction ends.
# The whole run holds the lifecycle lock, so no start.sh can launch an API
# between those two steps (see env.sh).
set -uo pipefail
source "$(dirname "$0")/native/env.sh"
lifecycle_lock ./scripts/stop.sh || exit 1

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

any_alive() { local pid; for pid in "$@"; do kill -0 "$pid" 2>/dev/null && return 0; done; return 1; }

# 0: the API is gone after a clean drain (or was not running).
# 2: the API is gone, but through its fail-safe or the emergency kill.
# 1: the API is still alive — nothing it depends on may be stopped.
stop_api() {
  local pids deadline log_from ticks=0 limit killed=0
  pids="$(api_pids)"
  if [ -z "${pids// /}" ]; then
    rm -f "$RUN_DIR/backend.pid" "$RUN_DIR/backend.stop_deadline"
    echo "api was not running"
    return 0
  fi

  # The deadline recorded when this API started; else the current policy;
  # else a fallback longer than the policy's default.
  deadline="$(cat "$RUN_DIR/backend.stop_deadline" 2>/dev/null)"
  case "$deadline" in
    ''|*[!0-9]*)
      if api_shutdown_policy; then deadline="$API_STOP_DEADLINE"; else deadline="$API_STOP_FALLBACK"; fi ;;
  esac

  log_from=$(( $(wc -l <"$LOG_DIR/backend.log" 2>/dev/null || echo 0) + 1 ))
  # SIGTERM, once, and never SIGINT after it: a SIGINT during shutdown makes
  # uvicorn force-exit and skip the lifespan shutdown — the writer drain.
  kill -TERM $pids 2>/dev/null
  echo "api stopping (pid ${pids% }) — allowing up to ${deadline}s to drain writers"
  # The deadline is counted in quarter-second sleeps, not read off the wall
  # clock. The API times its drain on a monotonic clock that stands still
  # while the Mac sleeps; the wall clock ($SECONDS, date) does not, and a
  # lid closed mid-shutdown would otherwise see stop.sh wake past its
  # deadline and SIGKILL a drain that had barely run. A sleep never counts
  # the Mac's sleep, and each tick is at least 0.25 s awake, so the API
  # always has had its whole deadline first.
  limit=$((deadline * 4))
  while any_alive $pids; do
    if [ "$ticks" -ge "$limit" ]; then
      echo "EMERGENCY: api still running ${deadline}s after SIGTERM, past its whole" \
           "shutdown deadline — sending SIGKILL"
      kill -KILL $pids 2>/dev/null
      killed=1
      for _ in $(seq 1 20); do any_alive $pids || break; sleep 0.25; done
      break
    fi
    if [ "$ticks" -gt 0 ] && [ $((ticks % 60)) -eq 0 ]; then
      echo "           still draining ($((ticks / 4))s of ${deadline}s)"
    fi
    sleep 0.25
    ticks=$((ticks + 1))
  done

  if any_alive $pids || [ -n "$(api_pids | tr -d ' ')" ]; then
    echo "FAILED to stop api — pid(s) still alive: $(api_pids)"
    return 1
  fi
  rm -f "$RUN_DIR/backend.pid" "$RUN_DIR/backend.stop_deadline"
  if [ "$killed" = 1 ]; then
    echo "api killed after ${deadline}s"
    return 2
  fi
  if tail -n +"$log_from" "$LOG_DIR/backend.log" 2>/dev/null \
      | grep -q "writers not confirmed drained"; then
    echo "api exited through its fail-safe after $((ticks / 4))s: writers not" \
         "confirmed drained, lease left to PostgreSQL (see logs/native/backend.log)"
    return 2
  fi
  echo "stopped api (exited $((ticks / 4))s after SIGTERM)"
  return 0
}

status=0
stop_server dashboard "$WEB_PORT" "$RUN_DIR/frontend.pid" || status=1

stop_api
case $? in
  0) ;;
  2) status=1 ;;
  *) echo "cache and database left running: the API may still be writing to them"
     exit 1 ;;
esac

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
