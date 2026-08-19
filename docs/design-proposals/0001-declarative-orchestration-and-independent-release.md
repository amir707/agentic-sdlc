# Proposal 0001 — ADK-native orchestration, independent release, config-driven projects

Status: **draft / not started** · Author: retro after the capstone submission ·
Supersedes nothing · Depends on nothing

> This is a **future-work spec**, not a description of current behavior.
> It exists so the work can be resumed later without re-deriving the
> reasoning. Read the "Guardrails" section (§4) before touching anything —
> several current behaviors are load-bearing and MUST survive the
> refactor. If a change here would violate a guardrail, the change is
> wrong, not the guardrail.
>
> **Grounding:** verified against `google-adk==2.3.0` (installed). The
> Workflow graph API, `ResumabilityConfig`, and `RequestInput` HITL all
> exist in this version; `sdlc/adapters/adk/item_workflow.py` already imports and
> builds a `Workflow`. So the target below is an *adoption*, not a bet on
> unreleased features.

---

## 1. Why this exists (context)

The system shipped for a capstone under a deadline. Three decisions were
the *shippable* ones but not the ones we'd carry forward. A post-submission
retro identified them.

1. **The pipeline flow is encoded three times.** `sdlc/definition.py`
   declares the pipeline as data (phases, typed steps, back-edges with
   policy keys). `sdlc/bindings.py` *re-encodes the same flow
   imperatively* — a status switch, hand-written loops, `asyncio` fan-out
   (~128 asyncio touchpoints in that one file). `sdlc/adapters/adk/item_workflow.py`
   renders it a *third* time as an ADK `Workflow` graph, kept in sync by a
   parity test. **Only the driver executes.** We maintain parity between a
   thing that runs and two things that don't.

2. **The durable machinery is hand-rolled on `asyncio`.** The gate poll
   loop, the resume-from-store status switch in `process_item`, the release
   recheck `while asyncio.sleep(...)` loop, the `Semaphore`-bounded coder
   fan-out, and the escalation-timestamp comparisons are a hand-built
   durable-workflow runtime living inside an asyncio driver. This is the
   most imperative, least readable code — and it is not orchestration
   *logic*, it is a *runtime* we re-implemented. ADK 2.x ships that runtime.

3. **Release governance is coupled to the sprint orchestrator.**
   `run_release_pass` runs *inline* in the driver process, sharing its
   lifetime, `release_lock`, and the in-memory `ctx.approved` list. Release
   reacts to a different timeline (approvals, incidents, confidence windows)
   and should be its own resumable, event-driven loop over store state.

**Multi-project is now real.** The demo and store are parameterized by
`PROJECT`, and a second governed bundle exists
(`projects-config/candidate-app-2/`). This validates the config-driven
thesis — the engine stays neutral, projects are data — and it sets up the
larger future direction in §7: **hand the pipeline shape, knowledge, and
behavior to each project, composed from off-the-shelf building blocks.**

The through-line: **we coordinate through the store and through artifacts
(the PR, the live service); each control loop should be independent,
declarative, and resumable.** We proved that thesis for the agents; we did
not finish it for orchestration and release, and we hand-rolled the runtime
that ADK already provides.

---

## 2. Goals

- Make pipeline flow **declarative and executed once** — one graph, no
  parity-checked shadow, no imperative re-encoding.
- **Replace hand-rolled `asyncio` durability with native ADK primitives**
  (graph engine, `ResumabilityConfig`, `RequestInput`, `RetryConfig`,
  `max_concurrency`, ambient triggers).
- **Decouple release** into an independent, store-driven, event-triggered
  loop, per project.
- Set up **config-driven per-project behavior** (§7): projects supply
  pipeline shape + knowledge; the engine composes from reusable behaviors.
- Keep every governance guarantee (§4) intact.

## 3. Non-goals (do not drift here)

- **Do NOT convert the coder↔reviewer loop to an ADK `LoopAgent` of
  sub-agents / A2A / session hand-off.** See G1. The loop is deliberately
  artifact-mediated. ADK nodes may *host* the steps, but node-to-node data
  must stay a *signal*, never "the reviewer reads the coder's claims."
- **Do NOT widen the coder's tool surface** (G2). Four sandboxed workspace
  functions are a security boundary.
- **Do NOT change the store's MCP tool surface or its append-only /
  role-token properties** (G3).
- This is structural + a durability swap. The demo scenario and
  `verify_demo.py` assertions stay byte-for-byte identical.

---

## 4. Guardrails (load-bearing — must survive)

Each maps to the ADK feature that must preserve it.

- **G1 — Artifact mediation for the trust model.** The reviewer/verify
  steps re-derive from ground truth (the **local workspace diff**), never
  from the coder's claims. *ADK impact:* node I/O (`node_input`, `state`)
  between coder and reviewer carries only a signal ("PR #N, head SHA"); the
  reviewer node re-reads the workspace. Never pass the coder agent's text
  output as the reviewer's source of truth.

- **G2 — Capability enforcement over prompt enforcement.** Agents hold no
  git/network/cloud/GitHub credentials; the engine commits, pushes,
  deploys. The missing tool *is* the guardrail. *ADK impact:* side effects
  live in `FunctionNode` handlers the engine owns, never in agent tools.

- **G3 — The store is the single source of truth and the audit oracle.**
  Governance/lifecycle state lives in the store; the audit log is
  append-only and doubles as the eval oracle. *ADK impact:* ADK sessions
  own **execution** state only (where the graph is, resume cursors).
  Governance truth stays in the store; a merge/deploy decision is validated
  against the store + GitHub, never against ADK session state alone.

- **G4 — Every wait is policy-bounded** (invariant #7). *ADK impact:*
  back-edge cycles get bounded by `RetryConfig(max_attempts=...)` /
  `LoopAgent(max_iterations=...)` sourced from policy; gate waits get a
  node `timeout`; exhaustion still emits the audited escalation.

- **G5 — Idempotency by SHA-keyed markers.** Stages posting to a PR or
  deploying are guarded by `_marker(kind, head_sha)`. *ADK impact:* keep
  the markers; ADK resume can replay a node, so exactly-once-per-commit
  must stay enforced by the handler, not assumed from the engine.

- **G6 — Deterministic vs reasoning classification is honest.** A component
  is a reasoning agent only if it reasons. *ADK impact:* deterministic
  steps are plain `FunctionNode`s, reasoning steps are `LlmAgent` nodes —
  do not relabel one as the other to fit a graph shape.

---

## 5. The asyncio → ADK mapping (the heart of this proposal)

Every hand-rolled mechanism has a native ADK 2.3 replacement. This table
IS the "use real ADK instead of asyncio" answer.

| Hand-rolled today | Where | Native ADK replacement |
|---|---|---|
| `asyncio.Semaphore(parallel)` bounding coder fan-out | `driver.run_pipeline` | `Workflow(max_concurrency=N)` or `@node(parallel_worker=True)` over the item list |
| Retry loop for 429s / transient provider errors | `sdlc/adapters/adk/invoker.py` | `FunctionNode(retry_config=RetryConfig(max_attempts, backoff_factor, exceptions))` |
| Gate `while ... asyncio.sleep()` poll loop | `sdlc/governance/gate.py` | `RequestInput(interrupt_id=f"gate_{sha}")` node; resume reads `ctx.resume_inputs` |
| Resume-from-store status switch | `driver.process_item` | `App(resumability_config=ResumabilityConfig(is_resumable=True))` + `DatabaseSessionService` — automatic durable resume |
| Release recheck `while asyncio.sleep(45)` | `driver.run_release_pass` | Scheduled trigger: Cloud Scheduler → Pub/Sub → ambient `trigger_sources=["pubsub"]` endpoint (a Timer) |
| `/approve` discovered by polling GitHub | `gate.py` | GitHub webhook → Pub/Sub → ambient trigger resumes the suspended workflow, which does ONE `check_decision()` (see below) |
| `extract_json()` + `model_validate()` on agent text | `driver.py` | `LlmAgent(output_schema=Model)` nodes — validated output is the node result |
| Bounded fix/flag back-edges as hand-written loops | `driver.run_verify`, review loop | Routed cycle edges (`(check, fix, "changes")`), bounded by policy-sourced `max_attempts` |
| Escalation-timestamp comparisons for "is this /approve fresh?" | `_escalation_override` | Event/interrupt id per attempt (`gate_{sha}`); a new head = new interrupt, stale replies ignored structurally |

**The gate, precisely (preserves the ADR-0005 identity model).** ADK HITL
provides the *waiting mechanics*; GitHub provides the *authenticated
decision*. The gate node yields `RequestInput`; the resume trigger
(webhook) does not carry authority — it merely causes ONE
`check_decision()` look at the allowlisted PR comment. No valid command
there → the node suspends again with a fresh `interrupt_id`. This is
exactly what `sdlc/adapters/adk/item_workflow.py` already prototypes; §6 makes it the
executing path.

---

## 6. Workstreams

Three, executable in order. Each is shippable alone; do not start a later
one before the earlier is green (`make test` + one full local sprint +
`make verify-demo`).

### Workstream A — Make the ADK Workflow the executor (delete the shadow)

**Highest leverage. This is the core change.**

Today there are three representations and the *imperative* one runs. Invert
it: the **ADK `Workflow` becomes the single execution path**, built from
`definition.py`. Delete the imperative flow in `driver.py` and delete the
parity test — there is nothing to keep in parity because there is one graph.

- Build the per-item `Workflow` from `definition.py` (the graph generator
  already exists in `sdlc/adapters/adk/item_workflow.py`; promote it from "for
  `adk web`" to "the runner runs it").
- Step handlers stay: each becomes a `FunctionNode` (deterministic) or
  `LlmAgent` node (reasoning). They keep doing the git/store side effects
  (G2) and keep their SHA markers (G5).
- Back-edges become routed cycle edges bounded by policy (G4).
- Run via `App(root_agent=workflow, resumability_config=...)` + `Runner` +
  `DatabaseSessionService`. Resume becomes automatic — delete the
  `process_item` status switch (G3: the store stays governance truth; ADK
  owns execution-cursor state).
- Parallel items: `max_concurrency` (or `ParallelWorker`) replaces the
  `Semaphore`.

**Design-pattern note.** The original draft proposed *building* a ~150-line
interpreter over `definition.py`. Reject that now: ADK's graph engine
already IS a durable interpreter. Building our own would re-implement
exactly what 2.3 ships. `definition.py` stays the **declarative source**;
the ADK `Workflow` is the **interpreter**; handlers are the **Strategy**
implementations bound by name. Ports & Adapters (the `AgentInvoker`
boundary) stays — but note much of what the invoker hand-rolls (retry) also
moves to `RetryConfig`.

Acceptance: the demo audit log is byte-for-byte identical; `driver.py`
shrinks to *handlers*, not flow; `test_framework_boundary.py`'s parity test
is deleted and replaced by "the workflow executes the definition" tests;
`asyncio` in the engine drops toward zero.

### Workstream B — Independent, event-triggered release loop (per project)

**Scope is per-project** (see §1). The loop runs for one project at a time;
its inputs and its single-flight guard are project-scoped, so two projects'
releases never contend.

- Release becomes its own `Workflow` (or ambient app), triggered by events,
  not called inline by the sprint run.
- Inputs, all project-scoped from the store: `queued` PRs + open incidents
  + deploy history + confidence windows. `ctx.approved` disappears — it was
  always "items with `status=queued`," already persisted.
- Triggers (native ADK, §12 of the ADK cheatsheet): a **Cloud Scheduler →
  Pub/Sub** tick (the confidence-window Timer) and a **GitHub webhook →
  Pub/Sub** push (PR approved / incident closed), both hitting the ambient
  `trigger_sources` endpoint. No `while sleep`.
- Keep `release_lock` semantics as a per-project single-flight guard.
- Keep the deterministic merge gate (re-verify + preprod-deploy any head
  lacking evidence) and incident-resolver-runs-first ordering.
- The sprint orchestrator shrinks to: drive items to `status=queued`.

Acceptance: killing the sprint orchestrator does not strand a held PR — an
independent trigger still merges it once the incident closes (`make demo`
beat 7 works with the sprint process already exited). A held PR in one
project neither blocks nor is blocked by another project's release.

### Workstream C — ADK-idiomatic upgrades (cheap, high readability)

Do independently; each is small.

- **Config-driven agents.** Replace `sdlc_steps/*/spec.py` builders with
  ADK declarative `AgentConfig` / YAML (verify the exact loader in 2.3
  before committing). Same agents, less code, reads as data — and it is the
  on-ramp to §7.
- **Plugins for cross-cutting concerns.** Metering, throttling
  (`GEMINI_RPM`), and audit are per-agent callbacks in `build_llm_agent`
  today. Register them ONCE as ADK `Plugin`s via `App(plugins=[...])`. This
  also cleanly relocates the rate-limit that currently lives in asyncio.
- **Durable sessions in prod.** `DatabaseSessionService` (Cloud SQL /
  Postgres) for ADK execution state — pairs with the store-durability gap
  in §6a. Do NOT put governance state here (G3).
- **Lean on ADK's eval harness** for the risk-assessor dataset (already in
  `evals/`).
- Do NOT (G1/G2) expose git/deploy as agent tools or add multi-agent
  session coordination.

Acceptance: agent definitions are data; cross-cutting callbacks are
registered once; a process restart mid-sprint loses nothing without a manual
rerun; `make test` green.

---

### Workstream D — ReleaseTarget port (deploy-agnostic release)

**The last un-ported seam.** "Release" currently means "merge the PR AND
shift Cloud Run traffic to the pr-N tag": the merge half goes through
the RepoHost port (host-swappable), but the activation half is a DIRECT
import of sdlc/adapters/gcloud.py (raw gcloud) from the driver and from
sdlc_steps/preprod_ci — no port, no per-project choice.

Goal: a `ReleaseTarget` protocol in the core with domain semantics —
`deploy_candidate(candidate, source_dir) -> url` (stand it up somewhere
probe-able), `promote(candidate)` (make it THE release), `live_url()`
(monitor probe base). CloudRunTarget wraps today's deploy.py; other
targets are one adapter each (compose/k8s/package-publish/noop for
governance-only projects). Selection is per-project data: project.yaml
`target: cloud_run` + a target-specific block (the existing cloud_run:
key is the seed), resolved by the composition root from a registry —
the §7 behavior-catalog pattern applied to infrastructure.

- preprod_ci splits: test-running stays generic; candidate deploy +
  smoke go through the port (smoke_endpoints already project config).
- The store's deploys vocabulary generalizes lightly (traffic
  "preprod"/"100" -> candidate/live) or stays as-is at demo scale.
- DELIBERATELY NOT abstracted: the PR-as-artifact assumption. It is
  load-bearing (G1 artifact mediation; ADR-0005 gate authority lives in
  a PR comment). Any Git-hosted project fits it; making the engine
  artifact-agnostic would re-solve review mediation and gate identity
  for marginal gain. Stated engine invariant, not a knob.

**D2 — the deploy spec comes FROM the project bundle (true agnosticism).**
A named-adapter catalog is only agnostic up to its catalog. The stronger
form: the bundle carries `deploy.yaml` — `candidate:` (stand PR-N up
somewhere probe-able + its URL), `promote:` (make it THE release),
`live_url:` (monitor base) — as argv command templates with {checkout}/
{candidate}/env substitution, and the engine ships ONE generic
`CommandTarget` executing that contract (no shell; exit code + declared
URL enforced). cloud_run becomes a preset expanding to the same spec.
`candidate: none` degrades honestly: preprod = tests only, smoke off,
monitor off, release-manager audit records "no runtime signal".
The spec feeds the DETERMINISTIC steps (preprod_ci, the engine's promote
action) — never the release_manager agent (G2/G6: judgment stays
mechanics- and credential-free).

**D3 — projects-config detaches into a sibling repo (onboarding = two
repo URLs).** Treat the config bundle exactly like the candidate repo: a
provisioned checkout (clone into scratch, heal, refresh per pass) from
`config_repo` registered at onboarding. Config changes become versioned
and auditable via that repo's history (governable through the same PR
gate later). Preconditions: (a) secrets LEAVE the bundle — Secret
Manager per project, local .env only as the dev rung; (b) enabling code
step, cheap, do early: config.py's PROJECTS root -> PROJECTS_CONFIG_ROOT
env (default ./projects-config). New trust rule to write down: the
config repo's deploy spec runs with ENGINE credentials — same trust
model as CI config in-repo; onboarding review + change review are the
control.

Acceptance: the engine imports sdlc/adapters/gcloud.py nowhere; candidate-app
runs unchanged on the cloud_run preset; a `candidate: none` project
completes a full sprint (merge-only releases, monitor off) with an
identical audit shape; the whole suite passes with PROJECTS_CONFIG_ROOT
pointing OUTSIDE the engine repo.

## 6a. Related gaps to track (not workstreams)

Surfaced during operation; real, orthogonal to the workstreams. None blocks
the demo; each is production-hardening.

- **Store durability.** ~~The cloud store is SQLite on a Cloud Run
  instance's ephemeral disk, alive only via `min-instances=1`.~~
  **DONE (demo rung): Litestream → GCS.** The store container restores
  the SQLite file from the replica at boot and replicates continuously
  while serving (`scripts/store_entrypoint.sh`; runbook §7), so the
  service runs at `min-instances=0` and a redeploy/crash/node migration
  loses nothing — the container disk is a cache of the replica.
  Production rung: **Firestore** (or Cloud SQL) behind the same MCP tool
  surface; the swap recipe is pinned mechanically in
  `tests/test_store_backend_contract.py` — implement the pinned function
  surface as `db_firestore.py`, switch the import in `mcp_server/server.py`,
  and go green on `tests/test_delivery_store.py` (the behavioral
  contract). Single-writer constraint stands while on Litestream:
  `max-instances=1`.

- **Spend-bounded kill-switch.** G4 bounds *waits*; nothing halts a sprint
  that blows past its token budget mid-flight. Add a pre-invocation check
  (an ADK `before_model_callback` plugin is the natural home) against
  store-summed sprint spend that aborts the item (audited escalation) when
  over budget — a hard stop complementing the wait-bounds.

- **Schema-validation re-prompt.** Verdicts validate against Pydantic at the
  boundary (fail loudly, good) but a `ValidationError` fails the run rather
  than re-asking. With Workstream A, `LlmAgent(output_schema=...)` +
  `RetryConfig` gives a bounded re-ask for free; then escalate. Store
  checkpoints make a hard failure cheap to resume, so this is an
  optimization, not a correctness fix.

---

## 7. Future direction — config-driven, off-the-shelf per-project behavior

The larger goal: **move hardcoded logic and knowledge out of the engine and
into each project, so a project's behavior is composed from reusable,
off-the-shelf building blocks and configured, not coded.** Multi-project
(§1) is the first step; this is where it goes.

The engine already separates *what* (`definition.py`, `sdlc_steps/*`) from
*mechanics* (engine/), and projects already overlay policy and prompts
(`projects-config/<p>/`). Extend that to the whole pipeline:

> **Status (first increment landed):** item 1 is real for the safe axis —
> `projects-config/<name>/pipeline.yaml` sets a `PipelineShape`
> (`human_gate: true|false`), `sdlc/definition.py::per_item_edges(shape)`
> composes the graph and the ADK adapter builds from it; item 4 is
> enforced in code (unknown keys are rejected — guarantees are not knobs).
> Items 2 (behavior catalog) and 3 remain future work.

1. **Pipeline shape becomes per-project data.** Move `definition.py` (the
   graph: steps, order, back-edges) into each project bundle. The engine
   loads it and builds the ADK `Workflow` (Workstream A) from it. Two
   projects can then have *different pipelines* — a low-risk internal tool
   might skip the human gate; a payments service adds an extra review step —
   with zero engine changes.

2. **A catalog of off-the-shelf behaviors (Strategy + Registry).** Each step
   `kind` resolves to a named *behavior* from an engine-provided library:
   reasoning-agent templates (coder, reviewer, risk-assessor), deterministic
   solvers (sprint-packer, blast-radius verify), and gates. A project's
   definition references behaviors by name and parameterizes them via
   `policy.yaml` + `AgentConfig` (Workstream C) — it does not write code.
   "Add a project" becomes "compose behaviors + supply knowledge."

3. **Knowledge is project-owned data.** Prompts, policies, area maps, risk
   thresholds, and flag rules live entirely in the bundle (mostly true
   already). The engine ships behaviors and guarantees; the project ships
   judgment.

4. **Guarantees stay engine-enforced (not configurable away).** G1–G6 are
   NOT project knobs. A project composes behaviors and tunes thresholds; it
   cannot turn off artifact mediation, capability enforcement, the
   append-only audit, or the bounded loops. This is the line between
   "configurable behavior" and "load-bearing guarantee."

This is the natural end state of Workstreams A + C: ADK Workflow interprets
a *project-supplied* definition, built from *config-driven* agents drawn
from a *behavior catalog*, over a store whose guarantees the engine owns.

---

## 7a. Deployment model — in-boundary engine, not multi-tenant SaaS

**Decision: the engine ships as a deployable product that runs inside the
customer's network boundary** (container / Helm chart), following the
precedent of GitLab Runner, GitHub Actions self-hosted runners, ArgoCD,
and Jenkins — tools in the same position (need the code, the test infra,
and the deploy targets) that all converged on the same answer.

Why in-boundary wins over a hosted multi-tenant service:

- **The secrets problem mostly dissolves.** No multi-tenant vault holding
  many companies' GitHub tokens and AI keys. Each deployment is single
  tenant: the customer's tokens live in *their* env / secret manager,
  inside *their* walls, like any internal tool. The engine never becomes
  a cross-customer credential honeypot.
- **Deploy access becomes boring.** Inside the network the engine can run
  the project's deploy spec (Workstream D2) or call the internal CD
  system with a service account the customer grants it. The
  workflow-dispatch indirection ("ask GitHub Actions to deploy with the
  customer's own secrets") remains a supported `deploy.yaml` style for
  teams that prefer the engine to hold no deploy access at all — but it
  is an option, not the required architecture.
- **The value ceiling rises.** The high-leverage roadmap items — per-test
  coverage maps, blast-radius measurement, running the project's real
  test suite against staging — require reaching internal services and
  test infrastructure. A SaaS outside the boundary can never do that
  well; in-boundary, it is just process execution.

Accepted costs (clear-eyed):

- **The customer operates it** — upgrades, backups, support against
  environments we cannot see. Classic on-prem tax; mitigate with a
  boring, self-contained runtime (one container + one store file/DB) and
  the runbook discipline this repo already practices.
- **Learning stays per-customer.** The incident→guardrail loop learns
  only from that customer's incidents. No cross-customer network effect
  — which most customers prefer anyway.

**Hybrid endgame (phase 2, optional):** a thin cloud control plane for
login (Google IdP), cross-project dashboard, and onboarding UI, with the
in-boundary engine as the "runner." Two hard rules if/when built:

1. The control plane **never holds deploy credentials** and never reaches
   into the customer network — the engine connects *outbound* to the
   control plane (runner pattern), never the reverse.
2. Governance truth stays in the in-boundary store (G3); the control
   plane renders read models (the `/state` contract already exists for
   exactly this shape — `dashboard/api/state.js` is the prototype).

Onboarding a project is the same artifact in both phases: candidate repo
URL + config repo URL (D3) + the project's `deploy.yaml` (D2). Nothing in
Workstream D changes; this section only fixes *where the engine runs*.
Note the current architecture already **is** the single-tenant in-boundary
deployment — phase 1 is packaging, not redesign.

---

## 8. Recommendation (which solution)

**Adopt ADK as the orchestration runtime; do not build a custom
interpreter, and do not (yet) adopt Temporal.**

- **Chosen: ADK Workflow + `ResumabilityConfig` + `DatabaseSessionService`
  + ambient triggers.** It replaces every hand-rolled asyncio mechanism
  (§5), it is already installed (2.3.0) and already partially written
  (`sdlc/adapters/adk/item_workflow.py`), and it deletes the most code (imperative
  flow + parity test + poll/sleep/semaphore/retry). Best maintainability
  and readability return per unit of risk. Strongest capstone story too:
  "we express the governed pipeline as a native ADK graph and ADK runs it."

- **Rejected — custom interpreter over `definition.py`.** The original
  draft's idea. It reinvents ADK's durable graph engine. Only revisit if we
  deliberately leave ADK.

- **Rejected for now — Temporal / external durable engine.** Heavier, a
  second runtime to operate, and ADK's resumability + ambient triggers
  already cover durable resume, signals (webhook → trigger), and timers
  (scheduler → trigger) at this scale. Keep as the fallback *if* this
  becomes infrastructure whose scale or multi-language needs outgrow ADK.

- **Rejected — status quo asyncio.** It is the maintainability problem.

Sequence: **A** (ADK executor) → **C2** (plugins + config agents, cheap and
independent) → **B** (event-triggered release) → then the §7 config-driven
build-out once A and C have proven the shape. Do the store-durability move
(6a) together with C's `DatabaseSessionService`. Stop after any workstream
if the goal is met; each must leave the demo and `verify_demo.py` unchanged.

---

## 9. Key files (orientation for whoever resumes this)

- `sdlc/definition.py` — the pipeline as data (target: per-project;
  the thing the ADK Workflow is built from).
- `sdlc/bindings.py` — current imperative executor + handlers (target:
  shrink to `FunctionNode`/`LlmAgent` handlers; A deletes the flow).
- `sdlc/adapters/adk/item_workflow.py` — the ADK graph, today non-executing + parity
  (target: promote to THE executor; delete the parity test).
- `sdlc/governance/gate.py` — hand-rolled poll loop (target: `RequestInput`).
- `sdlc/adapters/adk/invoker.py` — AgentInvoker port + per-agent callbacks
  (target: retry → `RetryConfig`; callbacks → Plugins). **Keep the port.**
- `sdlc_steps/*/spec.py` — agent/step builders (C target: `AgentConfig`).
- `sdlc_steps/*/prompts.md`, `policy.yaml` — knowledge + budgets (§7: fully
  project-owned; never fold into code).
- `mcp_server/` — the store + tool surface (G3: do not change surface).
- `docs/design-invariants.md` + `docs/adr/0001-0007` — the "why"; invariant
  #7 (bounded loops) and ADR-0003 (orchestrator-driven, no A2A) map to
  G1/G4; ADR-0005 (PR comment authority) maps to the gate design in §5.
- `scripts/verify_demo.py` — the audit-trail eval; the regression oracle for
  every workstream.

---

## 10. One-line summary

Let ADK's graph engine run the pipeline it already draws (deleting the
imperative shadow and every hand-rolled asyncio loop), run release as an
independent event-triggered loop, and move toward projects that compose
their pipeline and behavior from an off-the-shelf catalog — while keeping
the one adversarial loop artifact-mediated and every governance guarantee
engine-enforced. Real ADK instead of a re-implemented runtime; far less
imperative glue; not one guarantee compromised.
