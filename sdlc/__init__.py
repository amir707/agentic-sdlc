"""agentic-sdlc — the governed delivery engine, as one package.

Skim the tree and you have the architecture:

  definition.py   THE SDLC as data: steps, the per-item graph, Route
  context.py      RunContext — the handles one run carries (typed to ports)
  bindings.py     definition step name -> implementation

  ports/          what the engine NEEDS from the world (Protocols only)
  governance/     the RULES both clocks obey: outcomes, gate, rejection, markers, schemas
  sprint/         THE SPRINT CLOCK: planning, per-item actions + pipeline, resume/run flow
  release/        THE RELEASE CLOCK: one event = one pass over the queue
  engine/         deterministic mechanics, no domain rules (config, git, provisioning, ...)
  steps/          one folder per worker: its knowledge (prompts, policy) and code
  tools/          the coder's sandboxed tools — its entire effect surface (G2)
  adapters/       concrete implementations of ports (GitHub, gcloud, the store, ADK)
  app/            entry points + the ONE composition root (bootstrap)

Dependency direction: app -> adapters -> {sprint, release} -> governance
-> ports/definition/context; engine is a leaf everyone may use. sprint
may hand off to release (trigger); release never imports sprint. Only
app/ and adapters/ may name a framework or a concrete tool
(tests/test_framework_boundary.py enforces all of this).
"""
