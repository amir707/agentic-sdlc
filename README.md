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
`config/projects/candidate-app/`.

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
config/projects/  one folder per governed project: project.yaml (repo,
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
`config/projects/<name>/sdlc_steps/<step>/customised-prompt.md` if
present (extends, never overrides) → task payload. Policy resolves the
same way: step defaults (plus shared `sdlc_steps/policy.yaml`) merged
with the project's mirrored overrides.

## Setup

See [docs/setup-runbook.md](docs/setup-runbook.md) for the complete,
replayable environment setup (GCP, GitHub, env, deploy). Interactive
installer: `python scripts/setup.py` (validates each step, idempotent).

Secrets policy: no keys or passwords in code or git history — env vars
via a local `.env` only ([.env.example](.env.example) lists every name).

## Status

Under construction (build compressed into Jul 5–6, 2026). This README
gains the full run guide, demo script, security model, scaling path,
and honest-limitations sections as the pieces land.
