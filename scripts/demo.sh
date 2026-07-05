#!/usr/bin/env bash
# One-take demo driver. Guides the operator through the 8-beat script,
# running everything that can be automated and pausing where the story
# needs a human action (gate decisions on GitHub, chaos toggles).
#
# Prerequisites (one-time): docs/setup-runbook.md or scripts/setup.py —
# baseline deployed, .env files filled, store seeded.
#
# Terminals: this script assumes the delivery store (make mcp) and the
# monitor (make monitor) are already running in their own terminals; it
# checks both and refuses to start otherwise.
#
# Beats (see also the Kaggle writeup):
#  1. seeded backlog -> assessor -> packer refusals (risk budget, split)
#  2. coder PRs; reviewer (different model family) fix loop
#  3. verify escalates PAY-102, flag invariant returns it to the coder
#  4. CI: tagged Cloud Run revisions + live smoke tests
#  5. chaos ON -> monitor opens a payments incident (watch its terminal)
#  6. release pass: catalog merges, payments PR held citing the incident
#  7. chaos OFF -> resolver closes after recovery -> held PR merges
#  8. "The Build" beat: toolchain (recorded separately over the repo)

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=.venv/bin/python
PARALLEL="${DEMO_PARALLEL:-1}"

# --- env ---------------------------------------------------------------------
set -a
source .env
source projects-config/candidate-app/.env
set +a

LIVE_URL="$($PYTHON -m adapters.deploy url)"
STORE_URL="http://127.0.0.1:${DELIVERY_STORE_PORT:-8787}/mcp"

pause() { echo; read -r -p ">>> $1  [Enter to continue] "; }

chaos() {  # chaos on|off
    local flag=false; [ "$1" = "on" ] && flag=true
    curl -s -X POST "$LIVE_URL/config/chaos" \
        -H "X-Config-Token: $CONFIG_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"payments\": $flag}"
    echo " <- chaos payments=$flag"
}

# --- preflight ----------------------------------------------------------------
echo "== preflight =="
curl -sf "$LIVE_URL/health" >/dev/null || { echo "candidate-app not healthy at $LIVE_URL"; exit 1; }
curl -s -o /dev/null -w "" "$STORE_URL" || { echo "delivery store not running (make mcp)"; exit 1; }
echo "live URL: $LIVE_URL"
echo "reminder: monitor should be probing in its own terminal (make monitor)"
chaos off  # known-good starting state

pause "Beat 1-4: full pipeline (assess, pack, code, review, verify, CI, gate).
    Decide each gate on the PR as prompted. Chaos beats come after."

# --- the pipeline (beats 1-4 + gates) -----------------------------------------
$PYTHON -m orchestrator --project candidate-app --parallel "$PARALLEL" &
ORCH_PID=$!

# Beat 5/7 guidance runs alongside the orchestrator's release phase:
pause "Beat 5: when all PRs are approved and BEFORE answering the
    release-pass prompt, flip chaos ON and watch the monitor open a
    payments incident (about one window)."
chaos on

pause "Beat 6: now let the release pass run (answer its prompt in the
    orchestrator terminal). Expect: catalog merges, payments HELD citing
    the incident. Then continue here."

pause "Beat 7: flip chaos OFF; wait ~2 healthy windows; run another
    release pass when prompted — the held PR merges."
chaos off

wait $ORCH_PID

# --- the receipts ---------------------------------------------------------------
echo
echo "== verify_demo: asserting the audit trail =="
$PYTHON scripts/verify_demo.py || true
echo
echo "== audit tail (the closing shot) =="
$PYTHON - <<'EOF'
import json, sys
sys.path.insert(0, ".")
from mcp_server import db
conn = db.connect()
for entry in db.list_audit(conn)[-12:]:
    print(f"#{entry['id']:>3} {entry['actor']:<18} {entry['decision']:<28} "
          f"{json.dumps(entry['factors'])[:80]}")
EOF
