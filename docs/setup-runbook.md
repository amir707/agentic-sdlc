# Setup runbook (replayable)

Every ad-hoc command run outside the repos during the build is recorded
here, in order, parameterized so the whole environment can be replayed
on a different Google/GitHub account. `scripts/setup.py` automates the
same steps interactively; this file is the raw record.

Parameters used in the original build:

```bash
export GCP_PROJECT=kaggle-codelab1          # any billing-enabled project
export GCP_REGION=australia-southeast2      # any Cloud Run region
export GH_OWNER=amir707                     # GitHub account/org
```

---

# Part A — LOCAL rung

Engine, store, monitor, and orchestrator run on your machine; only the
governed candidate-app lives on Cloud Run. Sections 3, 6, 7, 8 issue
gcloud commands (they target the candidate-app service) but are run
FROM your machine. This is the rung `scripts/setup.py` automates and
the demo recording uses.

## 1. One-time local tooling (already present on the build machine)

```bash
# gcloud CLI authenticated:  gcloud auth login && gcloud config set project $GCP_PROJECT
# GitHub CLI authenticated:  gh auth login
# uv (Python manager):       brew install uv
gcloud config list        # verify account + project
gh auth status            # verify account
```

## 2. Google Cloud project APIs (once per project)

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project "$GCP_PROJECT"
```

## 3. Local secrets (.env, never committed)

Secrets are split: system-level in `.env` (model keys, MCP role
tokens), project-level in `config/<project>/.env` (GitHub PAT, GCP
target, chaos config token).

```bash
cd agentic-sdlc
cp .env.example .env
cp projects-config/candidate-app/.env.example projects-config/candidate-app/.env
# generate the three MCP role tokens (system .env) + chaos config token
# (project .env):
python3 - <<'EOF'
import secrets
for name in ("CONFIG_TOKEN", "MCP_TOKEN_AGENTS", "MCP_TOKEN_MONITOR", "MCP_TOKEN_RESOLVER"):
    print(f"{name}={secrets.token_urlsafe(24)}")
EOF
# then add your own: ANTHROPIC_API_KEY, GOOGLE_API_KEY (system .env);
# GITHUB_TOKEN (project .env; fine-grained PAT scoped to
# $GH_OWNER/candidate-app: contents + pull requests, read/write)
```

## 4. Baseline deploy (smoke the highest-friction path early)

```bash
cd agentic-sdlc
set -a; source .env; source projects-config/candidate-app/.env; set +a
.venv/bin/python -m adapters.deploy baseline  # gcloud run deploy --source
.venv/bin/python -m adapters.deploy url       # print the live URL
curl "$(.venv/bin/python -m adapters.deploy url)/health"
```

## 5. (Recommended) dedicated deploy service account

Least-privilege deploy identity, instead of your user credentials:

```bash
gcloud iam service-accounts create agentic-sdlc-deploy \
  --display-name "agentic-sdlc deploy" --project "$GCP_PROJECT"
for role in roles/run.admin roles/cloudbuild.builds.editor \
            roles/artifactregistry.writer roles/iam.serviceAccountUser \
            roles/storage.admin; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
    --member "serviceAccount:agentic-sdlc-deploy@$GCP_PROJECT.iam.gserviceaccount.com" \
    --role "$role"
done
```

---

Any command executed later in the build that is not part of the repos
gets appended to this file at the time it is run.

## 6. Cloud Run tidy-up: drop stale PR-preview tags and revisions

Run when old `pr-N` preview tags pile up on a governed app's service.
Scripted: removes every 0%-traffic tag and deletes every revision not
serving traffic; serving revisions and their tags are never touched.

```bash
scripts/cleanup_cloud_service.sh <service> [region] [project]

# e.g. the two current projects:
scripts/cleanup_cloud_service.sh candidate-app australia-southeast2 kaggle-codelab1
scripts/cleanup_cloud_service.sh shopping-api  australia-southeast2 kaggle-codelab2
```

---

# Part B — GOOGLE CLOUD rung

The engine itself moves to Cloud Run: the delivery store becomes a
service, the orchestrator becomes a job. Everything in Part A stays
valid (the same candidate-app service is the deploy target); Part B is
additive. `make watch`/`make monitor` still run locally, pointed at the
cloud store via DELIVERY_STORE_URL. Day-to-day command reference (local
vs cloud, per category): README, "Running it".

## 7. Cloud rung: delivery store + orchestrator on Cloud Run

One image, two roles (see Dockerfile). Demo-scale choices, stated
honestly: container-disk SQLite behind min=max=1 instance (Cloud SQL is
the successor), public store URL guarded by the same per-role bearer
tokens (IAM ID tokens are the successor), gate polling inside the job
(GitHub webhook -> job execution is the successor).

```bash
PROJECT_ID=$(gcloud config get-value project)
REGION=australia-southeast2
SA=agentic-sdlc-orch

# 9.1 one-time: Artifact Registry repo + image build (Cloud Build)
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com
gcloud artifacts repositories create agentic-sdlc \
  --repository-format=docker --location="$REGION"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/agentic-sdlc/engine:latest"
gcloud builds submit --tag "$IMAGE" .   # from the agentic-sdlc repo root

# 9.2 one-time: secrets. Engine keys live in .env; GITHUB_TOKEN is
# project-scoped and lives in the project bundle's .env.
for s in ANTHROPIC_API_KEY GOOGLE_API_KEY \
         MCP_TOKEN_AGENTS MCP_TOKEN_MONITOR MCP_TOKEN_RESOLVER; do
  printf '%s' "$(grep "^$s=" .env | cut -d= -f2-)" \
    | gcloud secrets create "$s" --data-file=-
done
for s in GITHUB_TOKEN CONFIG_TOKEN; do
  printf '%s' "$(grep "^$s=" projects-config/candidate-app/.env \
    | cut -d= -f2-)" | gcloud secrets create "$s" --data-file=-
done

# 9.3 one-time: service account for the orchestrator job
gcloud iam service-accounts create "$SA"
SA_EMAIL="$SA@$PROJECT_ID.iam.gserviceaccount.com"
# demo shortcut (least-privilege alternative: run.admin +
# iam.serviceAccountUser + cloudbuild.builds.editor + storage.admin
# on the build bucket + artifactregistry.writer):
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" --role=roles/editor
for s in ANTHROPIC_API_KEY GOOGLE_API_KEY GITHUB_TOKEN CONFIG_TOKEN \
         MCP_TOKEN_AGENTS MCP_TOKEN_MONITOR MCP_TOKEN_RESOLVER; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:$SA_EMAIL" \
    --role=roles/secretmanager.secretAccessor
done

# 9.4 the delivery store (Cloud Run service, single instance)
gcloud run deploy delivery-store --image "$IMAGE" --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --allow-unauthenticated --min-instances=1 --max-instances=1 \
  --no-cpu-throttling --memory=512Mi \
  --set-env-vars=DELIVERY_STORE_HOST=0.0.0.0,PROJECT=candidate-app \
  --set-secrets=MCP_TOKEN_AGENTS=MCP_TOKEN_AGENTS:latest,MCP_TOKEN_MONITOR=MCP_TOKEN_MONITOR:latest,MCP_TOKEN_RESOLVER=MCP_TOKEN_RESOLVER:latest
STORE_URL="$(gcloud run services describe delivery-store \
  --region "$REGION" --format='value(status.url)')/mcp"

# 9.5 the orchestrator (Cloud Run Job)
gcloud run jobs create orchestrator --image "$IMAGE" --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --command=python --args=-m,orchestrator,--project,candidate-app,--parallel,2 \
  --task-timeout=3600 --max-retries=0 --memory=2Gi --cpu=2 \
  --set-env-vars="DELIVERY_STORE_URL=$STORE_URL,GCP_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,CLOUD_RUN_SERVICE=candidate-app,CODER_MODEL=anthropic/claude-sonnet-5,REVIEWER_MODEL=gemini-flash-lite-latest,GEMINI_MODEL=gemini-flash-lite-latest,GEMINI_RPM=12" \
  --set-secrets=ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,GOOGLE_API_KEY=GOOGLE_API_KEY:latest,GITHUB_TOKEN=GITHUB_TOKEN:latest,CONFIG_TOKEN=CONFIG_TOKEN:latest,MCP_TOKEN_AGENTS=MCP_TOKEN_AGENTS:latest,MCP_TOKEN_RESOLVER=MCP_TOKEN_RESOLVER:latest

# 9.6 run a sprint; approvals: comment /approve on the PR while the job
# polls, or after it exits re-execute — the store resumes the world
gcloud run jobs execute orchestrator --region "$REGION"
gcloud beta run jobs logs tail orchestrator --region "$REGION"

# 9.7 monitor and watch stay local, pointed at the cloud store
DELIVERY_STORE_URL="$STORE_URL" make monitor
DELIVERY_STORE_URL="$STORE_URL" make watch   # curls the store's /status

# image update after code changes
gcloud builds submit --tag "$IMAGE" . && \
  gcloud run jobs update orchestrator --image "$IMAGE" --region "$REGION" && \
  gcloud run services update delivery-store --image "$IMAGE" --region "$REGION"
```

Caveats at this rung: cloud `make watch` lacks the live NOW-worker
timers (the activity board is on the job's disk) and `make verify-demo`
still reads local SQLite. A store instance recycle — including
`gcloud run services update` — loses the world (reseed + rerun); set
TZ=Australia/Sydney on the service for local-time reports.

## 8. Field notes: hiccups from the first cloud deployment

Every failure hit while standing Part B up, with its fix — already
folded into section 9 where applicable.

- **Tag vs digest.** `gcloud builds submit --tag` moves the `:latest`
  tag in the registry only. Running services/jobs stay pinned to the
  digest resolved at their last update — after every rebuild you MUST
  `gcloud run jobs update --image` (and `services update` if the store
  changed). Same tag string, new pinned digest.
- **Secret exists but "versions/latest was not found".** A
  `gcloud secrets create` fed an empty string (grep missed the key)
  creates a versionless secret; later `create` says it already exists.
  Fix: `gcloud secrets versions add NAME --data-file=-`.
- **Which .env holds which secret.** Engine keys (model APIs, MCP role
  tokens) live in `.env`; GITHUB_TOKEN and CONFIG_TOKEN are
  project-scoped and live in `projects-config/<name>/.env`. The first
  deploy missed both; the job died at first PR push / first preprod
  deploy respectively.
- **Store 401 on secret mount.** A service deployed without
  `--service-account` runs as the default compute SA, which has no
  secretAccessor grants. Both service and job run as $SA_EMAIL.
- **`jobs create` → "already exists".** A create that failed validation
  still leaves the job resource; rerun as `gcloud run jobs update` with
  identical flags.
- **Agents couldn't reach the store while the driver could.** The
  agents' McpToolset hardcoded loopback; fixed in code — every store
  client resolves DELIVERY_STORE_URL first.
- **Gemini `503 UNAVAILABLE` (high demand) killed a run.** Fixed in
  code: the invoker retries transient provider errors (429/503/529)
  with backoff; only daily quotas fail fast.
- **OpenTelemetry "Failed to detach context" tracebacks** in job logs
  are benign ADK/MCP teardown noise (severity ERROR only because they
  are stderr tracebacks). Judge a run by its `[pipeline]`/`[release]`
  lines and the container exit code.
- **Fail-fast is process-wide by design.** One unhandled error (almost
  always config, e.g. a missing env var) exits the whole job; per-item
  failures are governed outcomes and do not stop the sprint. Rerunning
  the job resumes from the store.

## 9. Re-spinning the orchestrator without a terminal

```bash
# self-heal transient crashes: Cloud Run retries the task, resume makes
# it safe
gcloud run jobs update orchestrator --region "$REGION" --max-retries=2

# heartbeat: re-execute hourly (re-checks gates, held PRs, incidents)
gcloud scheduler jobs create http orchestrator-heartbeat \
  --location "$REGION" --schedule "0 * * * *" \
  --uri "https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/orchestrator:run" \
  --http-method POST --oauth-service-account-email "$SA_EMAIL"
# pause while driving runs by hand (nothing guards concurrent
# executions of the job):
gcloud scheduler jobs pause orchestrator-heartbeat --location "$REGION"
```

The production successor remains a GitHub webhook (issue_comment ->
jobs.run), so the /approve comment itself triggers the resuming run.

## 11. Stop a cloud orchestrator run

Cancelling is safe: every transition is checkpointed in the store first,
so a cancelled execution is just the crashed-run path — the next
execution resumes each item from its stored status.

```bash
REGION=australia-southeast2

# find the running execution (RUNNING column > 0)
gcloud run jobs executions list --job orchestrator --region "$REGION"

# cancel it (SIGTERM, ~10s grace)
gcloud run jobs executions cancel <EXECUTION-NAME> --region "$REGION" --quiet
```

## 12. Tear down: stop the hourly bill after testing

Both Cloud Run services run with min-instances=1 (the governed app for
per-instance chaos state, the store for its container-disk world) and
the store adds --no-cpu-throttling — always-on instances bill EVERY
HOUR whether or not anything runs. Idle jobs and secrets cost ~nothing.

```bash
REGION=australia-southeast2

# the always-on pieces (the actual money):
gcloud run services delete delivery-store --region "$REGION" --quiet
gcloud run services delete candidate-app  --region "$REGION" --quiet
# cheaper alternative if you want the demo resumable: scale to zero
#   gcloud run services update <svc> --region "$REGION" --min-instances=0

# tidiness (near-zero cost while idle):
gcloud run jobs delete orchestrator --region "$REGION" --quiet
gcloud scheduler jobs delete orchestrator-heartbeat \
  --location "$REGION" --quiet             # if section 10 was used
gcloud artifacts repositories delete agentic-sdlc \
  --location "$REGION" --quiet             # image storage

# stale PR revisions/tags on a service you are KEEPING: section 6
# (scripts/cleanup_cloud_service.sh) trims them without teardown.
```
