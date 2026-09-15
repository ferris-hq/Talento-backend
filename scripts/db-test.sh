#!/usr/bin/env bash
# Applies all migrations to a throwaway database on plain Postgres and runs the SQL tests.
#
#   scripts/db-test.sh                 # uses local Postgres via default psql settings
#   PGHOST=localhost PGUSER=postgres scripts/db-test.sh
#
# Requires Postgres 15+ (column-level grants + gen_random_uuid are built in).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB="talento_test_$(date +%s)_$$"
PSQL=(psql -X -q -v ON_ERROR_STOP=1 --set=client_min_messages=warning)

cleanup() { dropdb --if-exists "$DB" >/dev/null 2>&1 || true; }
trap cleanup EXIT

createdb "$DB"
echo "==> database $DB"

"${PSQL[@]}" -d "$DB" -f "$ROOT/supabase/tests/local/auth_stub.sql"

for migration in "$ROOT"/supabase/migrations/*.sql; do
  echo "==> migrate $(basename "$migration")"
  "${PSQL[@]}" -d "$DB" -f "$migration"
done

status=0
for test in "$ROOT"/supabase/tests/*_test.sql; do
  echo "==> test $(basename "$test")"
  set +e
  output="$(psql -X -v ON_ERROR_STOP=1 -d "$DB" -f "$test" 2>&1)"
  code=$?
  set -e
  # Show the "ok - ..." notices and any error lines.
  printf '%s\n' "$output" | sed -n 's/^psql:[^:]*:[0-9]*: NOTICE:  /  /p; /ERROR/p'
  [[ $code -eq 0 ]] || status=1
done

if [[ $status -eq 0 ]]; then echo "==> all database tests passed"; else echo "==> database tests FAILED"; fi
exit $status
