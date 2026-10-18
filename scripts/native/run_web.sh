#!/usr/bin/env bash
# The dashboard in the foreground, for a supervisor (quantdesk-web.service).
# The same Vite start.sh launches: on 127.0.0.1 only — reached through an SSH
# tunnel, never published — with the API's address and key.
set -euo pipefail
source "$(dirname "$0")/env.sh"

if port_busy "$WEB_PORT"; then
  echo "refusing to start: :$WEB_PORT is already held"
  exit 1
fi
API_KEY_VALUE="$(sed -n 's/^API_KEY=//p' "$ROOT/.env" 2>/dev/null | tail -1)"
cd "$ROOT/frontend"
export VITE_API_URL="http://localhost:$API_PORT" VITE_API_KEY="$API_KEY_VALUE"
exec ./node_modules/.bin/vite --host 127.0.0.1 --port "$WEB_PORT" --strictPort
