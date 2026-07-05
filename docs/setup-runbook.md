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

## 1. One-time local tooling (already present on the build machine)

```bash
# gcloud CLI authenticated:  gcloud auth login && gcloud config set project $GCP_PROJECT
# GitHub CLI authenticated:  gh auth login
# uv (Python manager):       brew install uv
gcloud config list        # verify account + project
gh auth status            # verify account
```

## 2. GitHub repos (private during development)

```bash
# candidate-app: run from a checkout of the candidate-app directory
git init -b main && git add -A
git commit -m "Baseline candidate app: payments/catalog areas, feature flags, protected chaos toggle"
gh repo create candidate-app --private --source . --push

# agentic-sdlc: same pattern from the agentic-sdlc directory
# (repo was created as sprint-governor and renamed:
#  gh repo rename agentic-sdlc --yes)
git init -b main && git add -A
git commit -m "Agentic SDLC: initial structure"
gh repo create agentic-sdlc --private --source . --push
```

## 3. Google Cloud project APIs (once per project)

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project "$GCP_PROJECT"
```

## 4. Python environments (per checkout)

```bash
# candidate-app
cd candidate-app
uv venv --python 3.12 .venv
uv pip install -p .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q

# agentic-sdlc
cd agentic-sdlc
uv venv --python 3.12 .venv
uv pip install -p .venv/bin/python -r requirements-dev.txt
```

## 5. Local secrets (.env, never committed)

Secrets are split: engine-level in `.env` (model keys, MCP role
tokens), project-level in `config/<project>/.env` (GitHub PAT, GCP
target, chaos config token).

```bash
cd agentic-sdlc
cp .env.example .env
cp config/projects/candidate-app/.env.example config/projects/candidate-app/.env
# generate the three MCP role tokens (engine .env) + chaos config token
# (project .env):
python3 - <<'EOF'
import secrets
for name in ("CONFIG_TOKEN", "MCP_TOKEN_AGENTS", "MCP_TOKEN_MONITOR", "MCP_TOKEN_RESOLVER"):
    print(f"{name}={secrets.token_urlsafe(24)}")
EOF
# then add your own: ANTHROPIC_API_KEY, GOOGLE_API_KEY (engine .env);
# GITHUB_TOKEN (project .env; fine-grained PAT scoped to
# $GH_OWNER/candidate-app: contents + pull requests, read/write)
```

## 6. Baseline deploy (smoke the highest-friction path early)

```bash
cd agentic-sdlc
set -a; source .env; source config/projects/candidate-app/.env; set +a
.venv/bin/python -m engine.deploy baseline  # gcloud run deploy --source
.venv/bin/python -m engine.deploy url       # print the live URL
curl "$(.venv/bin/python -m engine.deploy url)/health"
```

## 7. (Recommended) dedicated deploy service account

Least-privilege deploy identity, instead of your user credentials:

```bash
gcloud iam service-accounts create sprint-governor-deploy \
  --display-name "sprint-governor deploy" --project "$GCP_PROJECT"
for role in roles/run.admin roles/cloudbuild.builds.editor \
            roles/artifactregistry.writer roles/iam.serviceAccountUser \
            roles/storage.admin; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
    --member "serviceAccount:sprint-governor-deploy@$GCP_PROJECT.iam.gserviceaccount.com" \
    --role "$role"
done
```

---

Any command executed later in the build that is not part of the repos
gets appended to this file at the time it is run.
