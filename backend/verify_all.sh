#!/usr/bin/env bash
# Run the whole verification suite against a THROWAWAY, freshly-seeded database.
#
# Why a scratch database:
#   verify_phase1..10 are seed-FIXTURE tests. They assert against the exact demo dataset
#   app/seed.py produces (24 months Jan-2024..Dec-2025, four planted anomalies with
#   hand-computed magnitudes). They also mutate as they go — locking periods, reclassifying
#   categories, importing rows, creating and deleting properties — and several of them
#   perturb fixtures the later ones depend on.
#
#   That means they cannot pass against a working database that also holds real,
#   hand-entered data (extra months move "latest month", extra properties move counts), and
#   running them against one would corrupt it. So this script builds a scratch database from
#   the current migrations + seed, runs everything there, and drops it. Your real database is
#   never touched.
#
# Usage:
#   cd backend && bash verify_all.sh
#
# Env:
#   PGHOST/PGPORT/PGUSER  default to the dev cluster (localhost:5433, postgres)
#   SCRATCH_DB            default re_manager_verify

set -uo pipefail

export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5433}"
export PGUSER="${PGUSER:-postgres}"
SCRATCH_DB="${SCRATCH_DB:-re_manager_verify}"
PYDEPS="${PYDEPS:-.pydeps}"

SCRATCH_URL="postgresql+psycopg2://${PGUSER}@${PGHOST}:${PGPORT}/${SCRATCH_DB}"

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[0;34m%s\033[0m\n' "$*"; }

cleanup() {
  psql -d postgres -tAc "DROP DATABASE IF EXISTS ${SCRATCH_DB};" >/dev/null 2>&1
}
trap cleanup EXIT

blue "Building scratch database ${SCRATCH_DB} (your real DB is untouched)..."
cleanup
psql -d postgres -tAc "CREATE DATABASE ${SCRATCH_DB};" >/dev/null || { red "could not create ${SCRATCH_DB}"; exit 1; }

if ! DATABASE_URL="$SCRATCH_URL" PYTHONPATH="$PYDEPS" python3 -m alembic upgrade head >/tmp/verify_migrate.log 2>&1; then
  red "migrations failed:"; tail -20 /tmp/verify_migrate.log; exit 1
fi
green "  migrations applied"

if ! DATABASE_URL="$SCRATCH_URL" PYTHONPATH="$PYDEPS" python3 -m app.seed >/tmp/verify_seed.log 2>&1; then
  red "seed failed:"; tail -20 /tmp/verify_seed.log; exit 1
fi
green "  seeded"

# Snapshot the pristine seed so each script starts from identical state — they mutate, and
# several would otherwise break fixtures the next one relies on.
TEMPLATE_DB="${SCRATCH_DB}_tpl"
psql -d postgres -tAc "DROP DATABASE IF EXISTS ${TEMPLATE_DB};" >/dev/null 2>&1
psql -d postgres -tAc "CREATE DATABASE ${TEMPLATE_DB} TEMPLATE ${SCRATCH_DB};" >/dev/null 2>&1
cleanup_all() {
  psql -d postgres -tAc "DROP DATABASE IF EXISTS ${SCRATCH_DB};" >/dev/null 2>&1
  psql -d postgres -tAc "DROP DATABASE IF EXISTS ${TEMPLATE_DB};" >/dev/null 2>&1
}
trap cleanup_all EXIT

echo
fails=0
for script in verify_phase1.py verify_phase2.py verify_phase3.py verify_phase4.py \
              verify_phase5.py verify_phase6.py verify_phase7.py verify_phase8.py \
              verify_phase9.py verify_phase10.py verify_phase11.py verify_phase12.py \
              verify_phase13.py verify_phase14.py verify_isolation.py; do
  [ -f "$script" ] || continue
  psql -d postgres -tAc "DROP DATABASE IF EXISTS ${SCRATCH_DB};" >/dev/null 2>&1
  psql -d postgres -tAc "CREATE DATABASE ${SCRATCH_DB} TEMPLATE ${TEMPLATE_DB};" >/dev/null 2>&1
  out=$(DATABASE_URL="$SCRATCH_URL" PYTHONPATH="$PYDEPS" python3 "$script" 2>&1)
  if echo "$out" | grep -qi "PASSED"; then
    green "  PASS  $script"
  else
    red   "  FAIL  $script"
    echo "$out" | tail -12 | sed 's/^/        /'
    fails=$((fails + 1))
  fi
done

echo
if [ "$fails" -gt 0 ]; then
  red "${fails} script(s) failed."
  exit 1
fi
green "All verification scripts passed."
