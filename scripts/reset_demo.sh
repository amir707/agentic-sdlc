#!/usr/bin/env bash
# FULL demo reset — the two-sided one. `make seed` alone resets only the
# store; the world (candidate-app main, its branches, the Cloud Run
# traffic) keeps the previous run's merges, so a "fresh" sprint would
# find its items already shipped and skip them. This resets everything:
#
#   1. candidate-app origin/main -> the baseline root commit (force)
#   2. delete all remote item/* branches
#   3. redeploy the baseline to Cloud Run (traffic 100% -> clean build)
#   4. reseed the store (+ activity board)
#
# Closed/merged PRs remain visible in GitHub history (not deletable);
# PR numbering keeps rising. That is cosmetic.

set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
source projects-config/candidate-app/.env
set +a

CAND="$CANDIDATE_APP_DIR"

echo "== 1/4 reset candidate-app main to baseline =="
git -C "$CAND" fetch -q origin
ROOT_COMMIT=$(git -C "$CAND" rev-list --max-parents=0 origin/main)
echo "   baseline commit: $ROOT_COMMIT"
git -C "$CAND" push -q --force origin "$ROOT_COMMIT:refs/heads/main"

echo "== 2/4 delete remote item/* branches =="
git -C "$CAND" ls-remote --heads origin 'item/*' | awk '{print $2}' \
  | sed 's#refs/heads/##' | while read -r branch; do
    echo "   deleting $branch"
    git -C "$CAND" push -q origin --delete "$branch" || true
done
git -C "$CAND" checkout -qf main
git -C "$CAND" fetch -q origin
git -C "$CAND" reset -q --hard origin/main
git -C "$CAND" clean -qfd

echo "== 3/4 redeploy baseline to Cloud Run (takes a minute or two) =="
.venv/bin/python -m adapters.deploy baseline

echo "== 4/4 reseed the store =="
.venv/bin/python scripts/seed.py

echo
echo "reset complete: baseline serving 100% traffic, store seeded, world clean."
