#!/usr/bin/env bash
# Demo conductor. Runs in ITS OWN terminal alongside the pipeline —
# it never runs the orchestrator itself (mixing both on one stdin made
# prompts ambiguous). It owns exactly three things: preflight, the
# chaos toggles at the right story beats, and the closing receipts.
#
# Terminal layout for the demo:
#   A: make mcp PROJECT=candidate-app          (delivery store)
#   B: make monitor PROJECT=candidate-app      (live error rates — beats 5 & 7)
#   C: make orchestrate PROJECT=candidate-app PARALLEL=2  (the pipeline)
#   D: make demo         (THIS script: chaos beats + receipts)
#   E: make watch PROJECT=candidate-app        (live workers + store view)
#
# Every prompt below says what Enter does HERE and what you do in the
# OTHER terminals. Nothing here reads orchestrator state; the live
# service is the only medium between chaos and the monitor.

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=.venv/bin/python

set -a
source .env
source projects-config/candidate-app/.env
set +a

LIVE_URL="$($PYTHON -m adapters.deploy url)"

chaos() {  # chaos on|off
    local flag=false state=OFF
    if [ "$1" = "on" ]; then flag=true; state=ON; fi
    local resp
    resp=$(curl -s -X POST "$LIVE_URL/config/chaos" \
        -H "X-Config-Token: $CONFIG_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"payments\": $flag}")
    echo "chaos for payments is now $state (service confirmed: $resp)"
}

pause() {
    echo
    echo "$1"
    read -r -p "    [Enter] = $2 "
}

# --- preflight ----------------------------------------------------------------
echo "== preflight =="
curl -sf "$LIVE_URL/health" >/dev/null \
    || { echo "candidate-app not healthy at $LIVE_URL"; exit 1; }
curl -s -o /dev/null "http://127.0.0.1:${DELIVERY_STORE_PORT:-8787}/mcp" \
    || { echo "delivery store not running -> start 'make mcp PROJECT=candidate-app' first"; exit 1; }
echo "live URL: $LIVE_URL"
chaos off
echo "known-good start: chaos OFF, service healthy"

pause "──────────────────────────────────────────────────────────
 BEATS 1-4 · plan → code → review → verify → CI → dossier
──────────────────────────────────────────────────────────
 DO:    start (if not running), in the ORCHESTRATOR terminal:
          make orchestrate PROJECT=candidate-app PARALLEL=2
 WATCH: each item pauses at its gate with a dossier on its PR.
        Approving = releasing: every /approve triggers an
        immediate release decision, one PR at a time.

 ⚠ THE ONE RULE ─ do NOT /approve any PAYMENTS PR yet.
   Wait for its dossier to appear, then come back HERE." \
    "confirm a payments dossier is posted and NOT yet approved"

# --- beat 5: incident opens ------------------------------------------------------
echo
echo "──────────────────────────────────────────────────────────"
echo " BEAT 5 · chaos ON → the monitor discovers an incident"
echo "──────────────────────────────────────────────────────────"
chaos on
pause " WATCH: the MONITOR terminal — payments error_rate climbs,
        then 'incident #N open' (one ~15s window).
 DO:    nothing — approve NOTHING until the incident is open." \
    "confirm the incident is open"

# --- beat 6: hold + contrast -------------------------------------------------------
pause "──────────────────────────────────────────────────────────
 BEAT 6 · the hold, and the contrast
──────────────────────────────────────────────────────────
 DO:    1. /approve the PAYMENTS PR on GitHub
        2. keep approving the OTHER PRs as their dossiers arrive
 WATCH: the ORCHESTRATOR —
        · the payments PR is HELD, citing the incident (audited)
        · the catalog PRs MERGE during the incident
          (different area — the contrast is the point)
 DONE WHEN it settles into:
        '[release] held PRs remain — next pass in 45s'" \
    "confirm the hold happened and the sprint items are done"

# --- beat 7: recovery + merge -------------------------------------------------------
echo
echo "──────────────────────────────────────────────────────────"
echo " BEAT 7 · recovery → the held PR merges BY ITSELF"
echo "──────────────────────────────────────────────────────────"
chaos off
pause " DO:    nothing. Hands off — this beat is autonomous.
 WATCH: MONITOR — two healthy windows (~30s) …
        ORCHESTRATOR — on its next recheck the resolver closes
        the incident and the held payments PR MERGES by itself
        (traffic shifts). No human in that loop.
 DONE WHEN the orchestrator exits." \
    "show the receipts (verify_demo + audit tail)"

# --- receipts ---------------------------------------------------------------------
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
