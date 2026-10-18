#!/usr/bin/env bash
# Apply pending schema migrations to the desk's database — deliberately.
#
#   ./scripts/migrate.sh           dry run: schema state, pending revisions,
#                                  and every writer holding the schema lock
#   ./scripts/migrate.sh --apply   migrate under the exclusive schema lock
#
# Starting the desk never migrates (start.sh, docker-compose.yml). This, or
# `docker compose --profile migrate run --rm migrate --apply`, is the only
# way the schema moves. Whether writers are running is decided by the
# database's schema lock (app.migration_guard), not by a port: a writer
# anywhere — another port, a container, a backfill — refuses the migration,
# and no writer can start while it runs. Take a backup first.
set -euo pipefail
source "$(dirname "$0")/native/env.sh"
mkdir -p "$LOG_DIR"
cd "$ROOT/backend"

# Prefer the project's venv, but fall back to a runnable interpreter when the
# tree is copied without that environment (for example in script safety tests).
PYTHON_BIN="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "Refusing migration: no runnable python interpreter found"
  exit 1
fi

# An explicit scratch target (QUANTDESK_DATABASE_URL, see env.sh) is named
# before anything runs, and refused if it is in fact the desk's own database:
# the override exists for other databases, and the desk's needs none.
if [ -n "${QUANTDESK_DATABASE_URL:-}" ]; then
  if ! target="$(NATIVE_DATABASE_URL="$NATIVE_DATABASE_URL" "$PYTHON_BIN" - <<'PY'
import os
import sys

from sqlalchemy import make_url

from app.redact import describe

LOOPBACK = {"localhost", "127.0.0.1", "::1", None, ""}


def place(url):
    u = make_url(url)
    host = "loopback" if u.host in LOOPBACK else u.host
    return (u.get_backend_name(), host, u.port, u.database)


target = os.environ["DATABASE_URL"]
print(describe(target))
sys.exit(3 if place(target) == place(os.environ["NATIVE_DATABASE_URL"]) else 0)
PY
)"; then
    echo "Refusing QUANTDESK_DATABASE_URL (${target:-unreadable}): it names the" \
         "desk's own database or cannot be read. Unset it to work on the desk's database."
    exit 1
  fi
  echo "database: $target (explicit QUANTDESK_DATABASE_URL)"
fi

if [ "${1:-}" = "--apply" ]; then
  "$PYTHON_BIN" -m app.migrate --apply 2>&1 | tee -a "$LOG_DIR/migrations.log"
  exit "${PIPESTATUS[0]}"
fi
exec "$PYTHON_BIN" -m app.migrate "$@"
