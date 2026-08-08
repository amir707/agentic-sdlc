#!/bin/sh
# Delivery-store container boot (see Dockerfile CMD).
#
# Serverless durability via Litestream (demo rung): with
# LITESTREAM_REPLICA_URL set (gcs://bucket/path in the cloud; file://…
# in tests), the SQLite file is RESTORED from the replica on boot and
# continuously REPLICATED while serving — so the service can run at
# min-instances=0 and cold-start with the world intact. The container
# disk is a cache of the replica, exactly as a checkout is a cache of
# GitHub. Without the env var: plain local behavior, unchanged.
#
# Order matters: restore FIRST, seed only if the world is truly empty —
# a reseed must never shadow a restorable history.
set -e

DB="${DELIVERY_STORE_DB:-delivery_store.sqlite3}"

if [ -n "$LITESTREAM_REPLICA_URL" ]; then
    echo "[store-boot] litestream restore from $LITESTREAM_REPLICA_URL"
    litestream restore -if-replica-exists -if-db-not-exists \
        -o "$DB" "$LITESTREAM_REPLICA_URL"
    python scripts/seed.py --if-empty --project "${PROJECT:-candidate-app}"
    echo "[store-boot] serving with continuous replication"
    exec litestream replicate \
        -exec "python -m mcp_server.server" \
        "$DB" "$LITESTREAM_REPLICA_URL"
fi

python scripts/seed.py --if-empty --project "${PROJECT:-candidate-app}"
exec python -m mcp_server.server
