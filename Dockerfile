# One image, two roles — the command chooses at deploy time:
#   delivery store (Cloud Run service): the default CMD below
#   orchestrator  (Cloud Run Job):      python -m orchestrator --project ...
# Secrets are NEVER baked in (.env is dockerignored); they arrive as
# Secret Manager references on the service/job.
FROM python:3.12-slim

# git: engine-provisioned checkouts; gcloud: the deterministic deploy
# tool (adapters/deploy.py shells it; builds happen in Cloud Build).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl gnupg ca-certificates \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] \
https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/*

# uv: the engine builds the governed repo's venv at provision time.
# The commit identity is the engine's own (no human, no AI co-author).
RUN pip install --no-cache-dir uv \
    && git config --system user.name "agentic-sdlc" \
    && git config --system user.email "orchestrator@agentic-sdlc.invalid"

# Litestream: serverless durability for the store's SQLite (restore on
# boot, continuous replication to GCS) — lets the store service run at
# min-instances=0. TARGETARCH comes from BuildKit (amd64 on Cloud
# Build, arm64 on Apple-silicon local builds).
ARG TARGETARCH=amd64
ADD https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-${TARGETARCH}.tar.gz /tmp/litestream.tar.gz
RUN tar -C /usr/local/bin -xzf /tmp/litestream.tar.gz litestream \
    && rm /tmp/litestream.tar.gz && litestream version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Store boot (scripts/store_entrypoint.sh): with LITESTREAM_REPLICA_URL
# set, restore-from-replica then serve with continuous replication
# (min-instances=0, single writer: keep max-instances=1); without it,
# idempotent seed + serve as before. Cloud Run injects PORT;
# DELIVERY_STORE_HOST=0.0.0.0 is set on the service. Production
# successor: Firestore behind the same mcp_server tool surface
# (contract: tests/test_store_backend_contract.py).
CMD ["sh", "scripts/store_entrypoint.sh"]
