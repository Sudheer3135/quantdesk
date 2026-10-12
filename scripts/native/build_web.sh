#!/usr/bin/env bash
# Compile the dashboard into static files for the public HTTPS site
# (scripts/native/caddy). Not the Vite dev server run_web.sh starts for the
# SSH tunnel: plain files, served by Caddy, that talk to the API on the page's
# own origin under /api.
#
#   ./scripts/native/build_web.sh [output-dir]      default: frontend/dist
#
# The build carries no secret. Anything named VITE_* is compiled into
# JavaScript every visitor downloads, so a VITE_API_KEY in the environment or
# in a frontend/.env* file stops the build, and the finished files are
# searched for every value in the project's .env before they are called done.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$ROOT/frontend/dist}"

if [ -n "${VITE_API_KEY:-}" ]; then
  echo "refusing to build: VITE_API_KEY is set in the environment — the public build must not carry the API key"
  exit 1
fi
for f in "$ROOT"/frontend/.env*; do
  [ -f "$f" ] || continue
  if grep -q '^[[:space:]]*VITE_API_KEY=' "$f"; then
    echo "refusing to build: $(basename "$f") sets VITE_API_KEY — the public build must not carry the API key"
    exit 1
  fi
done

cd "$ROOT/frontend"
# Same origin: the browser asks the site it loaded the page from, and the
# proxy forwards /api to the API. No host, no port, no key.
VITE_API_URL=/api ./node_modules/.bin/vite build --outDir "$OUT" --emptyOutDir

# Believe the files, not the flags above. Values are compared, never printed.
if grep -rqE 'localhost:8000|VITE_API_KEY' "$OUT"; then
  echo "FAILED: the build still names localhost:8000 or VITE_API_KEY"
  exit 1
fi
if [ -f "$ROOT/.env" ]; then
  while IFS= read -r line; do
    name="${line%%=*}"
    value="${line#*=}"
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    # Short values (true, 5m, a port) are settings, not secrets, and would
    # match by accident.
    [ "${#value}" -ge 12 ] || continue
    if grep -rqF -- "$value" "$OUT"; then
      echo "FAILED: the value of $name from .env appears in the built files"
      exit 1
    fi
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ROOT/.env")
fi
echo "built $OUT — same-origin /api, no key, no .env value"
