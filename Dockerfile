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

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Store boot: idempotent seed (never wipes an existing world), then
# serve. Cloud Run injects PORT; DELIVERY_STORE_HOST=0.0.0.0 is set on
# the service. Demo rung: container-disk SQLite (single instance);
# the production successor is Cloud SQL.
CMD ["sh", "-c", "python scripts/seed.py --if-empty --project ${PROJECT:-candidate-app} && python -m mcp_server.server"]
