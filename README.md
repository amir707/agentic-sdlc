# Agentic SDLC

**The management layer that makes agent-written code shippable.**

Everyone demos agents that write code. Sprint Governor demos what's
missing above them: a system that governs a fleet of coding agents the
way a good engineering manager governs a team — it plans a sprint under
a risk budget and a token budget, verifies claimed risk against actual
diffs, gates approvals through humans, and makes incident-aware release
decisions against a live deployed service.

Built for the Kaggle "AI Agents: Intensive Vibe Coding Capstone
Project" (Agents for Business track). The SDLC runtime and its integrations are project-agnostic;
the governed service lives in its own repo:
[candidate-app](https://github.com/amir707/candidate-app) — agents open
real PRs there — and everything specific to it lives under
`projects-config/candidate-app/`.

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

```
sdlc_steps/       ONE FOLDER PER WORKER holding everything the worker is:
                  its knowledge (prompts.md, policy.yaml) and its code
                  (__init__.py implementation for deterministic workers —
                  sprint_packer, verify, preprod_ci, incident_resolver,
                  monitor — and spec.py model+tool wiring for reasoning
                  workers — risk_assessor, coder, code_reviewer, approver,
                  release_manager). Root policy.yaml holds shared keys.
orchestrator/     the SDLC process runtime: definition.py (the pipeline as data),
                  driver.py (sequential executor), and execution mechanics
                  (config overlays, invoker, git workspace, approval gate,
                  rejection, dependency graph, agent helpers)
projects-config/  one folder per governed project: project.yaml (repo,
                  areas, smoke endpoints), backlog.json (seed), .env
                  (project tokens, gitignored), and sdlc_steps/<step>/
                  overlays (customised-prompt.md, policy.yaml) mirroring
                  the root hierarchy
tools/            agent-facing tools: fs_tools (sandboxed workspace edits
                  and tests), diff_analysis (pull request diff parser)
adapters/         boundary adapters: repo_host (GitHub REST API),
                  store_client (MCP store client), deploy (Cloud Run deployer)
mcp_server/       the delivery-store MCP server (single source of truth)
scripts/          demo driver, deterministic eval, seeder, setup
docs/             architecture, invariants, ADRs, setup runbook
```

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

| Command | What it does |
|---|---|
| `make reset` | full world reset: governed repo to baseline, branches deleted, baseline redeployed, store reseeded |
| `make mcp` | the delivery-store MCP server (localhost, per-role bearer tokens) |
| `make monitor` | synthetic prober against the live Cloud Run URL |
| `make orchestrate [PARALLEL=2]` | the pipeline; gates pause for `/approve` comments on the PRs |
| `make demo` | the conductor: chaos beats + closing receipts |
| `make watch` | live view: who is doing what, sprint status, audit tail |
| `make verify-demo` | deterministic eval: asserts the audit trail contains the expected decisions |
| `make reset-item ITEM=X` | surgical single-item replay |

The engine provisions its own checkout of the governed repo (cloned
into tmp scratch, healed if broken, deleted after a clean run) — no
local copy needs to exist.

## Security model

Capability enforcement over prompt enforcement: agents cannot be talked
out of what they have no tool to do. Merging without human approval is
structurally impossible; the audit log is append-only because no
mutation tool exists; the coder's entire effect surface is four
sandboxed workspace functions. The human gate is identity-checked
(allowlisted GitHub logins; unauthorized commands are ignored and
audited). Least privilege throughout: per-role store tokens (only the
monitor opens incidents, only the resolver closes them), per-agent tool
filters, credential isolation (agents never hold cloud or GitHub
credentials). No secrets in code or git history. A deterministic merge
gate re-verifies and preprod-deploys any head lacking evidence before
it can ship. Local trust model stated honestly: localhost bearer tokens
are the demo-scale rung; the production ladder (MCP OAuth, workload
identity, secret manager) leaves the tool-surface scoping unchanged.

## Evals

Two layers (see [evals/](evals/)): a deterministic pipeline eval
(`verify_demo.py` — the audit trail doubles as the test oracle, so
compliance evidence and assertions are the same table) and a per-agent
dataset for the risk assessor in the Vertex evaluation schema.

## Scaling path (documented, deliberately not built)

State is externalized (GitHub + the store behind MCP), so each runtime
choice has a drop-in successor that leaves agents and the MCP surface
untouched: the for-loop scheduler becomes a work queue; the blocking
gate becomes a webhook-resumed suspension (the ADK Workflow expression
in `adapters/adk/workflow.py` already renders the pipeline as a
resumable graph); SQLite becomes Postgres behind the same tools; local
credentials become workload identity; the tmp-scratch checkout becomes
the ephemeral-worker clone it already imitates. The release-governance
slice (verify, monitor/resolver, release manager) is deployable to a
real team in ADVISORY MODE almost immediately: it comments "I would
hold this, and why" without merging, building the trust data that
would justify autonomy later.

## Honest limitations

Real delivery is a state machine with many back-edges; this build
implements the ones that matter most (review-fix loop, flag-policy
return, human-gate rejection, escalation with human override,
post-approval re-verification) and admits the rest as data. The
groomed-backlog assumption does the heaviest lifting — the messy left
half of the SDLC (ambiguity, discovery, negotiation) is out of scope.
The toy candidate app flatters every agent: real risk assessment runs
on tribal knowledge and large-codebase blast radii. Merge conflicts are
detected and escalated, never auto-resolved. And the demo compresses a
sprint into minutes; we do not claim it runs a real team's sprint
today — the defensible claim is a real solution to the
build-to-release governance slice, demonstrated on a deliberately
compressed substrate.
