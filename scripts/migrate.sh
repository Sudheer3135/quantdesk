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

if [ "${1:-}" = "--apply" ]; then
  "$ROOT/.venv/bin/python" -m app.migrate --apply 2>&1 | tee -a "$LOG_DIR/migrations.log"
  exit "${PIPESTATUS[0]}"
fi
exec "$ROOT/.venv/bin/python" -m app.migrate "$@"
