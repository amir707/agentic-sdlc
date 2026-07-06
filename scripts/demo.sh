#!/usr/bin/env bash
# Demo conductor. Runs in ITS OWN terminal alongside the pipeline —
# it never runs the orchestrator itself (mixing both on one stdin made
# prompts ambiguous). It owns exactly three things: preflight, the
# chaos toggles at the right story beats, and the closing receipts.
#
# Terminal layout for the demo:
#   A: make mcp          (delivery store)
#   B: make monitor      (live error rates — the star of beats 5 & 7)
#   C: make orchestrate  (the pipeline; answer ITS prompts there)
#   D: make demo         (THIS script: chaos beats + receipts)
#   E: make watch        (optional: live workers + store view)
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
    || { echo "delivery store not running -> start 'make mcp' first"; exit 1; }
echo "live URL: $LIVE_URL"
chaos off
echo "known-good start: chaos OFF, service healthy"

pause "BEATS 1-4 happen in the ORCHESTRATOR terminal:
    start 'make orchestrate' there now (if not already running).
    Per item it codes, reviews, verifies, runs CI, then pauses at the
    gate for your /approve comment on the GitHub PR. RELEASE IS
    IMMEDIATE: each approval triggers a release decision on the spot,
    one PR at a time.
    THE ONE RULE: the FIRST dossier will be the PAYMENTS PR (PAY-101).
    Do NOT /approve it yet — come back HERE first." \
    "confirm the payments dossier is posted and NOT yet approved"

# --- beat 5: incident opens ------------------------------------------------------
echo
echo "BEAT 5: flipping chaos ON now — watch the MONITOR terminal:"
echo "        payments error_rate climbs, then 'incident #N open'."
chaos on
pause "Wait for the monitor to print the incident (one ~15s window)." \
    "confirm the incident is open"

# --- beat 6: hold + contrast -------------------------------------------------------
pause "BEAT 6: now /approve the PAYMENTS PR on GitHub. Watch the
    ORCHESTRATOR: its immediate release decision HOLDS that PR, citing
    the open incident (audited with factors). Then keep approving the
    other PRs as their dossiers arrive — each gets its own decision,
    and the catalog PRs MERGE one by one DURING the incident
    (different area: the contrast is the point). Come back HERE when
    the orchestrator asks:
    'held PRs remain; run another release pass?'" \
    "confirm the hold happened and the sprint items are done"

# --- beat 7: recovery + merge -------------------------------------------------------
echo
echo "BEAT 7: flipping chaos OFF now — watch the MONITOR terminal:"
echo "        two consecutive healthy windows (~30s) let the resolver close it."
chaos off
pause "After ~2 healthy windows, answer 'Y' to the orchestrator's
    release-pass prompt: the resolver closes the incident and the held
    payments PR MERGES (traffic shifts). When the orchestrator exits,
    come back." \
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
