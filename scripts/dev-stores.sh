#!/usr/bin/env bash
# Start local Postgres 16 + Redis without Docker (for sandboxes). Idempotent.
# Prints the env vars integration tests use. With Docker, prefer: docker compose -f deploy/compose.yaml up -d postgres redis
set -euo pipefail
PGBIN=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)
PGDIR=${HUDDLE_PGDIR:-/tmp/huddle-pg}
if ! pg_isready -q -h 127.0.0.1 -p 5432 2>/dev/null; then
  if [ "$(id -u)" = 0 ]; then
    id -u huddlepg >/dev/null 2>&1 || useradd -m huddlepg
    mkdir -p "$PGDIR"; chown huddlepg "$PGDIR"
    RUN="su huddlepg -c"
  else
    RUN="bash -c"
  fi
  [ -f "$PGDIR/PG_VERSION" ] || $RUN "$PGBIN/initdb -D $PGDIR -A trust >/dev/null"
  $RUN "$PGBIN/pg_ctl -D $PGDIR -o '-c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp' -l $PGDIR/log -w start >/dev/null"
fi
if ! redis-cli -h 127.0.0.1 ping >/dev/null 2>&1; then
  redis-server --daemonize yes --bind 127.0.0.1 --save "" >/dev/null
fi
echo "export HUDDLE_TEST_POSTGRES_DSN=postgresql://huddlepg@127.0.0.1:5432/postgres"
echo "export HUDDLE_TEST_REDIS_URL=redis://127.0.0.1:6379/0"
