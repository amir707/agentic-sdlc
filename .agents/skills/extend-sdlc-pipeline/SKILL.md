---
name: extend-sdlc-pipeline
description: Add or modify a step, policy, back-edge, or agent in the
  agentic-sdlc engine without breaking its structural invariants. Use
  when enhancing the pipeline (new SDLC step, new gate, new policy
  key, new agent) or changing how existing steps execute.
---

# Extending the agentic-sdlc pipeline

## Read first (in this order)

1. `docs/design-invariants.md` — what must stay true, and which test
   enforces each invariant.
2. `orchestrator/definition.py` — the pipeline as data: phases, steps
   typed reasoning|deterministic|gate, back-edges with policy keys.
3. `docs/adr/0007-framework-as-implementation-detail.md` — why the
   SDLC core never imports ADK.

## Adding a new step

1. Declare it in `orchestrator/definition.py` (phase, type, and any
   back-edge with the policy key that bounds it). The definition is
   frozen data — no logic.
2. Create `sdlc_steps/<step_name>/` (underscore names only):
   - `prompts.md` — base prompt (reasoning steps only)
   - `policy.yaml` — engine DEFAULTS; must stay project-neutral
     (empty lists/values, never a real path, area, or endpoint)
   - `spec.py` — AgentSpec factory `build(project, ...)` (reasoning)
     or plain functions (deterministic)
3. Bind the name in `HANDLERS` at the bottom of
   `orchestrator/driver.py` — the explicit definition→execution map.
4. Reasoning step? Add a 2-line dev-UI stub under
   `tests/debug/adk_web/<step_name>/` (copy an existing one; they all
   delegate to `_bootstrap.py` so the dev UI runs the REAL spec).
5. Project specifics (real areas, paths, prompt additions) go ONLY in
   `projects-config/<project>/sdlc_steps/<step_name>/` as
   `policy.yaml` / `customised-prompt.md` overlays.

## Rules that break silently if forgotten

- **Core never imports ADK.** Anything framework-specific lives in
  `adapters/adk/`; specs describe agents neutrally (AgentSpec,
  StoreTools). `tests/test_framework_boundary.py` AST-scans for
  violations and checks definition/Workflow-graph parity — run it
  before and after.
- **The store owns item lifecycle.** New pipeline outcomes must write
  a status via `ctx.set_status(...)` (statuses are validated in
  `mcp_server/server.py`); the resume switch in `process_item` must
  handle any status you add. Never infer state from GitHub.
- **Idempotency by marker.** Any stage that posts to a PR or deploys
  must guard with `_marker(kind, head_sha)` so a resumed run skips
  proven work exactly once per commit.
- **Capability over prompt.** Enforce new restrictions by not
  providing the tool, not by prompt text (see fs_tools' denylist and
  the store's per-role tools for the pattern). A tool refusal returns
  an "ERROR: ..." string to the model — never raises.
- **Every consequential decision is audited** with its factors:
  `ctx.audit(actor, decision, factors)`. The demo eval
  (`scripts/verify_demo.py`) asserts against the audit log, so new
  governance behavior usually needs a check added there too.
- **Locks:** anything deploying to the shared Cloud Run service takes
  `ctx.ci_lock`; anything merging/releasing goes through
  `run_release_pass` (serialized by `release_lock`). Parallel workers
  otherwise collide.

## Definition of done

`make test` green (structural tests catch drift), one full local
sprint (`make seed && make orchestrate`), then `make verify-demo`.
Commit is a separate step — never chained onto the change.
