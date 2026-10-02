#!/usr/bin/env bash
# Run the `db`-marked tests against a throwaway PostgreSQL container (debt TD-48).
#
#   make test-db                      # postgres:16-alpine, all db tests
#   make test-db PYTEST_ARGS='-k wave13'
#   TEST_DB_IMAGE=postgres:17 make test-db
#
# The container listens on 127.0.0.1 only (a random free port), trusts local connections
# (no password: nothing in it outlives the run), and is removed on exit -- pass, fail or
# Ctrl-C. Each test runs inside the `pg_conn` fixture's transaction, which is rolled back at
# teardown; only `_ensure_tables` is committed, into this disposable database.
#
# Never point this at a shared database: it sets PGHOST/PGPORT/PGUSER/PGDATABASE itself
# and ignores whatever the caller exported.
set -euo pipefail

IMAGE="${TEST_DB_IMAGE:-postgres:16-alpine}"
NAME="bifrost-core-testdb-$$"
READY_TIMEOUT_S="${TEST_DB_READY_TIMEOUT_S:-60}"

if ! command -v docker >/dev/null 2>&1; then
  echo "test-db: docker is required (start Docker Desktop, or run 'make test-all' with PGHOST set)" >&2
  exit 2
fi

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

docker run -d --name "$NAME" \
  -e POSTGRES_USER=bifrost \
  -e POSTGRES_DB=bifrost \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p 127.0.0.1::5432 \
  "$IMAGE" >/dev/null

# The image restarts postgres once after initdb; wait for the final server, reachable over TCP.
deadline=$((SECONDS + READY_TIMEOUT_S))
until docker exec "$NAME" pg_isready -h 127.0.0.1 -U bifrost -d bifrost -q 2>/dev/null \
  && docker exec "$NAME" psql -h 127.0.0.1 -U bifrost -d bifrost -tAc 'SELECT 1' >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    echo "test-db: $IMAGE not ready after ${READY_TIMEOUT_S}s" >&2
    docker logs "$NAME" >&2 || true
    exit 1
  fi
  sleep 1
done

PORT="$(docker port "$NAME" 5432/tcp | head -n1 | sed 's/.*://')"
echo "test-db: $IMAGE on 127.0.0.1:$PORT ($NAME)"

# src/ first, so a worktree tests its own code rather than an editable install elsewhere.
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
export PGHOST=127.0.0.1 PGPORT="$PORT" PGUSER=bifrost PGDATABASE=bifrost PGPASSWORD=
unset GOLDEN_SOURCE_HOST GOLDEN_SOURCE_PORT GOLDEN_SOURCE_DATABASE GOLDEN_SOURCE_USER GOLDEN_SOURCE_PASSWORD

# shellcheck disable=SC2086  # PYTEST_ARGS is a word list on purpose
pytest -m db ${PYTEST_ARGS:-}
