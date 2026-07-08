#!/usr/bin/env bash
# Tidy a Cloud Run service: remove every 0%-traffic tag (stale pr-N
# previews) and delete every revision no longer serving traffic.
# Revisions serving traffic — and their tags — are never touched.
#
# Usage: scripts/cleanup_cloud_service.sh <service> [region] [project]
#        region defaults to $GCP_REGION, project to the gcloud default.
set -euo pipefail

SERVICE=${1:?usage: cleanup_cloud_service.sh <service> [region] [project]}
REGION=${2:-${GCP_REGION:?pass a region or set GCP_REGION}}
PROJECT=${3:-$(gcloud config get-value project 2>/dev/null)}
GCLOUD=(gcloud --project "$PROJECT")

read -r STALE_TAGS SERVING < <("${GCLOUD[@]}" run services describe \
    "$SERVICE" --region "$REGION" --format=json | python3 -c '
import json, sys
traffic = json.load(sys.stdin)["status"]["traffic"]
stale = [t["tag"] for t in traffic if t.get("tag") and not t.get("percent")]
serving = [t["revisionName"] for t in traffic if t.get("percent")]
print(",".join(stale) or "-", ",".join(serving) or "-")')

if [ "$STALE_TAGS" != "-" ]; then
  echo "removing stale tags: $STALE_TAGS"
  "${GCLOUD[@]}" run services update-traffic "$SERVICE" --region "$REGION" \
    --remove-tags "$STALE_TAGS"
else
  echo "no stale tags"
fi

for rev in $("${GCLOUD[@]}" run revisions list --service "$SERVICE" \
    --region "$REGION" --format="value(metadata.name)"); do
  case ",$SERVING," in
    *",$rev,"*) echo "keeping $rev (serving)" ;;
    *) "${GCLOUD[@]}" run revisions delete "$rev" --region "$REGION" \
         --quiet && echo "deleted $rev" ;;
  esac
done
