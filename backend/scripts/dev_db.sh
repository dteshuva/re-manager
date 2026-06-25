#!/usr/bin/env bash
# Start a throwaway, user-owned Postgres cluster for local dev — no root / Docker needed.
# Useful when you don't have a system Postgres running. For Supabase, just point
# DATABASE_URL at your project instead and skip this script.
#
#   ./scripts/dev_db.sh start    # initdb (first run) + start on port 5433, create DB
#   ./scripts/dev_db.sh stop     # stop the cluster
#   ./scripts/dev_db.sh psql     # open a psql shell
#
# Connection URL: postgresql+psycopg2://postgres@localhost:5433/re_manager
set -euo pipefail

PORT="${PGPORT_LOCAL:-5433}"
DBNAME="${PGDATABASE_LOCAL:-re_manager}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PGDATA="$ROOT/.localdb"
# Find the Postgres bin dir (Debian/Ubuntu layout or PATH).
PGBIN="$(dirname "$(command -v pg_ctl || echo /usr/lib/postgresql/*/bin/pg_ctl | tr ' ' '\n' | tail -1)")"
export PATH="$PGBIN:$PATH"

case "${1:-start}" in
  start)
    if [ ! -d "$PGDATA" ]; then
      echo "initdb -> $PGDATA"
      initdb -D "$PGDATA" -U postgres --auth=trust >/dev/null
    fi
    pg_ctl -D "$PGDATA" -o "-p $PORT -k /tmp" -l "$PGDATA/server.log" -w start
    createdb -h localhost -p "$PORT" -U postgres "$DBNAME" 2>/dev/null || true
    echo "Postgres up on localhost:$PORT, db '$DBNAME'"
    echo "DATABASE_URL=postgresql+psycopg2://postgres@localhost:$PORT/$DBNAME"
    ;;
  stop)
    pg_ctl -D "$PGDATA" -w stop
    ;;
  psql)
    psql -h localhost -p "$PORT" -U postgres "$DBNAME"
    ;;
  *)
    echo "usage: $0 {start|stop|psql}" >&2
    exit 1
    ;;
esac
