#!/usr/bin/env bash
# One-command agents-cli eval for the risk assessor: seed a scratch store,
# run the REAL agent over the dataset (generate), grade the traces with a
# local deterministic metric (grade), print the scores, and tear the
# store down. No Vertex, no GCP project, no cost beyond Gemini inference.
#
# Prereqs: uv, the parent repo's .env with GOOGLE_API_KEY + MCP_TOKEN_*,
# and `uv sync --dev --extra eval` already run once in this directory.
#
# Usage: ./run_eval.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
PORT=8799
DB="${TMPDIR:-/tmp}/agentic-sdlc-eval-store.sqlite3"

cd "$REPO"
set -a; source .env; set +a
export DELIVERY_STORE_PORT="$PORT" DELIVERY_STORE_DB="$DB" DELIVERY_STORE_HOST=127.0.0.1

rm -f "$DB" "$DB-wal" "$DB-shm"
.venv/bin/python scripts/seed.py --project candidate-app >/dev/null
.venv/bin/python -m mcp_server.server >"$HERE/artifacts/store.log" 2>&1 &
STORE_PID=$!
trap 'kill $STORE_PID 2>/dev/null || true' EXIT
sleep 4

cd "$HERE"
DS=tests/eval/datasets/risk_assessor_dataset.json
uv run agents-cli eval generate --dataset "$DS"
TRACE=$(ls -t artifacts/traces/*.json | head -1)
uv run agents-cli eval grade --traces "$TRACE" --config tests/eval/eval_config.yaml
