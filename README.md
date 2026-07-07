# Agentic SDLC

**The governance layer that decides whether — and when — agent-written code ships.**

Everyone demos agents that write code. Agentic SDLC demos what's
missing above them: a system that governs a fleet of coding agents the
way a good engineering manager governs a team — it plans a sprint under
a risk budget and a token budget, verifies claimed risk against actual
diffs, gates approvals through humans, and makes incident-aware release
decisions against a live deployed service.

Built for the Kaggle "AI Agents: Intensive Vibe Coding Capstone
Project" (Agents for Business track).

The engine is project-agnostic: each governed project lives in its own
repo, and every per-project customisation (policies, prompts, area
maps, backlog) lives here under `projects-config/<project-name>/` — a
folder per project, which is the practical multi-project answer at
capstone scale (a config service would be the production successor).
The demo project is
[candidate-app](https://github.com/amir707/candidate-app); agents open
real PRs there.

## Three ideas to steal

1. **Risk budget per sprint, in points.** Medium risk costs 1 point,
   high costs 2, the sprint cap is 2 — two mediums OR one high, never
   both. Enforced by a deterministic packer, not by vibes.
2. **Token budget as capacity.** Token spend is sprint capacity:
   capacity planning for AI developers. Humans and agents draw from
   different capacity pools but the same risk pool.
3. **Claimed-versus-actual verification.** A guardrail diffs what a
   story claimed its risk was against what the change actually touches
   (dependency-graph blast radius), and escalates mismatches.

## Architecture

Thin reasoning agents over a thick deterministic substrate — see
[docs/architecture.md](docs/architecture.md) for the pipeline diagram,
the honest agent-vs-tool classification, and the knowledge
architecture. Design rationale lives in [docs/adr/](docs/adr/);
structural guarantees in
[docs/design-invariants.md](docs/design-invariants.md).

## Repository map

| Folder | What lives there |
|---|---|
| `sdlc_steps/` | **one folder per worker** — everything a worker *is*: its knowledge (`prompts.md`, `policy.yaml`) and its code. Root `policy.yaml` holds shared keys |
| `orchestrator/` | the process runtime: `definition.py` (the pipeline as data), `driver.py` (the executor), and mechanics (config overlays, invoker port, git workspace, gate, rejection, dependency graph) |
| `projects-config/` | **one folder per governed project**: `project.yaml` (repo, areas, smoke endpoints), `backlog.json`, `.env` (gitignored), and `sdlc_steps/<step>/` overlays mirroring the root hierarchy |
| `adapters/` | boundary adapters: GitHub REST (`repo_host`), MCP store client, Cloud Run deployer — plus `adapters/adk/`, the ONE package that speaks ADK |
| `mcp_server/` | the delivery-store MCP server — the single source of truth |
| `tools/` | agent-facing tools: sandboxed workspace (`fs_tools`), diff analysis |
| `scripts/` | demo conductor, deterministic eval, seeder, setup, resets |
| `docs/` | architecture, design invariants, ADRs, setup runbook |

Inside `sdlc_steps/`, a worker's code takes one of two shapes —
`__init__.py` implementations for the **deterministic** workers
(sprint_packer, verify, preprod_ci, incident_resolver, monitor),
`spec.py` model+tool wiring for the **reasoning** ones (risk_assessor,
coder, code_reviewer, approver, release_manager).

Composition per invocation: base `sdlc_steps/<step>/prompts.md` (opens
with immutable core rules) → project
`projects-config/<name>/sdlc_steps/<step>/customised-prompt.md` if
present (extends, never overrides) → task payload. Policy resolves the
same way: step defaults (plus shared `sdlc_steps/policy.yaml`) merged
with the project's mirrored overrides.

## Setup

See [docs/setup-runbook.md](docs/setup-runbook.md) for the complete,
replayable environment setup (GCP, GitHub, env, deploy). Interactive
installer: `python scripts/setup.py` (validates each step, idempotent).

Secrets policy: no keys or passwords in code or git history — env vars
via a local `.env` only ([.env.example](.env.example) lists every name).

## Running it

One-time: `python3 scripts/setup.py` (validated, idempotent — collects
model keys + a fine-grained GitHub PAT, generates role tokens, enables
GCP APIs, seeds the store). Then, in separate terminals:

Every world-selecting command names its project explicitly
(`PROJECT=<name>`) — each project gets its own store file, and nothing
guesses which world you mean.

**Run the pipeline**

| Command | Local rung | Cloud rung (runbook Part B) |
|---|---|---|
| store server | `make mcp PROJECT=x` | already running: the `delivery-store` Cloud Run service |
| pipeline | `make orchestrate PROJECT=x [PARALLEL=2]` — gates pause for `/approve` comments on the PRs | `gcloud run jobs execute orchestrator --region $REGION` |
| synthetic prober | `make monitor PROJECT=x` | `DELIVERY_STORE_URL="$STORE_URL" make monitor PROJECT=x` |

**Observe**

| Command | Local rung | Cloud rung |
|---|---|---|
| snapshot | `make status PROJECT=x` | `DELIVERY_STORE_URL="$STORE_URL" make status PROJECT=x` (curls the store's `/status` route) |
| live view | `make watch PROJECT=x` | same `DELIVERY_STORE_URL` prefix (no live worker timers — the activity board is on the job's disk) |
| pipeline logs | the orchestrate terminal | `gcloud beta run jobs logs tail orchestrator --region $REGION` |

**Reset / seed / verify** (these open the SQLite file directly — local rung only)

| Command | What it does |
|---|---|
| `make seed PROJECT=x` | destructive: wipe + reseed x's store from its `backlog.json` |
| `make reset-item ITEM=Y PROJECT=x` | surgical single-item replay: close its PR, delete its branch, status → pending |
| `make reset-demo` | candidate-app's full demo reset: repo to baseline, branches deleted, baseline redeployed, store reseeded |
| `make demo` | the conductor: chaos beats + closing receipts (candidate-app rig) |
| `make verify-demo PROJECT=x` | deterministic eval: asserts the audit trail contains the expected decisions |
| `make try-setup NAME=x` | preview onboarding a new project: scaffold, inspect, keep or delete |

For the cloud rung, `STORE_URL` is the delivery-store service plus
`/mcp`:

```bash
STORE_URL="$(gcloud run services describe delivery-store \
  --region "$REGION" --format='value(status.url)')/mcp"
```

`DELIVERY_STORE_URL` is the one switch: everything that talks to the
store over MCP follows it to the cloud; the reset/seed/verify group
opens the SQLite file directly and stays local (the runbook's Part B
records the deploy sequence and this rung's caveats).

The engine provisions its own checkout of the governed repo (cloned
into tmp scratch, healed if broken, deleted after a clean run) — no
local copy needs to exist.

> **💸 Cost caveat:** both Cloud Run services (the governed app and,
> on the cloud rung, the delivery store) run with `min-instances=1` —
> always-on instances **bill every hour**, demo or no demo. When you
> finish testing, tear them down (or scale to zero):
> [setup-runbook §12](docs/setup-runbook.md) has the commands, and §6
> trims stale PR revisions/tags on services you keep.

## Security model

The governing principle: **capability enforcement over prompt
enforcement** — agents cannot be talked out of what they have no tool
to do.

- **No merge without a human.** Structurally impossible, not
  prompt-discouraged: the release manager only ever receives PRs that
  carry a human approval record from the gate.
- **Append-only audit log** — by construction: the store exposes no
  update or delete tool for the audit table.
- **Sandboxed coder.** Its entire effect surface is four workspace
  functions (list/read/write/run-tests) inside one checkout — no git,
  no network, no credentials, no paths outside.
- **Identity-checked human gate.** Decisions are `/approve`, `/reject`,
  `/hold` comments from allowlisted GitHub logins; unauthorized
  commands are ignored *and audited*.
- **Least privilege throughout.** Per-role store tokens (only the
  monitor opens incidents, only the resolver closes them), per-agent
  tool filters.
- **Credential isolation.** Agents never hold cloud or GitHub
  credentials — deploys go through the deterministic deploy tool,
  pushes through the engine. No secrets in code or git history.
- **Deterministic merge gate.** Any head lacking preprod evidence is
  re-verified and re-deployed before it can ship — including commits
  landing after approval.
- **Honest trust ladder.** Localhost bearer tokens are the demo-scale
  rung; the production rungs (MCP OAuth, workload identity, secret
  manager) change transport, never the tool-surface scoping.

## Evals

Two layers (see [evals/](evals/)): a deterministic pipeline eval
(`verify_demo.py` — the audit trail doubles as the test oracle, so
compliance evidence and assertions are the same table) and a per-agent
dataset for the risk assessor in the Vertex evaluation schema.

## Scaling path (documented, deliberately not built)

State is externalized (GitHub + the store behind MCP), so each runtime
choice has a drop-in successor that leaves agents and the MCP surface
untouched:

- **Scheduler**: the for-loop driver → a work queue.
- **Gate**: blocking poll → webhook-resumed suspension (the ADK
  Workflow expression in `adapters/adk/workflow.py` already renders
  the pipeline as a resumable graph).
- **Store**: SQLite → Postgres behind the same MCP tools.
- **Credentials**: local tokens → workload identity.
- **Checkout**: the tmp-scratch clone → the ephemeral-worker clone it
  already imitates.

The credible first deployment: the release-governance slice (verify,
monitor/resolver, release manager) runs against a real team in
**advisory mode** almost immediately — it comments "I would hold this,
and why" without merging, building the trust data that would justify
autonomy later.

## Honest limitations

- **Fewer back-edges than real delivery.** The ones that matter most
  are built (review-fix loop, flag-policy return, human-gate
  rejection, escalation with human override, post-approval
  re-verification); the rest are admitted as data in the pipeline
  definition.
- **The groomed-backlog assumption does the heaviest lifting.** The
  messy left half of the SDLC — ambiguity, discovery, negotiation —
  is out of scope.
- **The toy candidate app flatters every agent.** Real risk assessment
  runs on tribal knowledge and large-codebase blast radii.
- **Merge conflicts are detected and escalated, never auto-resolved.**
- **The demo compresses a sprint into minutes.** We do not claim it
  runs a real team's sprint today — the defensible claim is a real
  solution to the build-to-release governance slice, demonstrated on
  a deliberately compressed substrate.
