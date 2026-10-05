#!/usr/bin/env bash
# The merge gate, runnable locally as one command. CI runs exactly these legs (plus scanners).
# Run before every commit: a commit verified against fewer legs gambles a CI round trip.
# Every leg runs even if an earlier one fails, so one run surfaces every failure.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
run() { echo "==> $*"; "$@" || fail=1; }

run uv run ruff format --check .
run uv run ruff check .
run uv run bandit -c pyproject.toml -r libs services -q
run uv run pyright
# CI runs every test, including the integration tests that need Postgres and Redis. Do the same
# whenever the stores are configured (eval "$(scripts/dev-stores.sh)"); otherwise say so, because
# a gate that skips them can pass what CI fails (it did: a budget test after a capacity change).
if [ -n "${HUDDLE_TEST_POSTGRES_DSN:-}" ] && [ -n "${HUDDLE_TEST_REDIS_URL:-}" ]; then
  run uv run pytest -q
else
  echo "NOTE: HUDDLE_TEST_POSTGRES_DSN/HUDDLE_TEST_REDIS_URL not set: skipping integration tests"
  echo "      (CI runs them). Start stores with: eval \"\$(scripts/dev-stores.sh)\""
  run uv run pytest -m "not integration" -q
fi
if [ -f apps/web/package.json ] && [ -d apps/web/node_modules ]; then
  (cd apps/web && run npm run --silent lint && run npm run --silent typecheck && run npm test --silent)
fi
run uv run python scripts/check_index.py

if [ "$fail" -ne 0 ]; then echo "GATE FAILED"; exit 1; fi
echo "GATE PASSED"
