# ADR-0006: The SDLC definition is data, separate from orchestrator mechanics

**Status:** accepted

## Context

The pipeline (which steps, what order, which bounded back-edges, where
the human gate sits) was at risk of living implicitly inside driver
code and orchestration mechanisms, making "change the process" mean "edit the
runtime".

## Decision

Three explicit layers:

- `orchestrator/definition.py` — WHAT the process is: the SDLC as a
  frozen data structure (planning / per-item / release phases; steps
  typed reasoning | deterministic | gate; back-edges that name their
  iteration-bound POLICY KEY, never a number).
- `orchestrator/driver.py` — HOW it executes: a sequential,
  inspectable driver with an explicit HANDLERS registry binding each
  definition step name to its `sdlc_steps/<name>/` implementation.
- `orchestrator/` utilities, `tools/`, and `adapters/` — the mechanisms both lean on
  (invoker, repo host, store client, gate, rejection, config overlays),
  knowing nothing about the pipeline's shape.

A structural test (`tests/test_definition.py`) keeps definition,
handlers, step folders, and policy keys consistent.

## Consequences

Customizing the SDLC = edit the definition, add an `sdlc_steps/`
folder, bind one handler; the core mechanics are untouched. The definition is
also the durable seam (section 15 scaling path): a Temporal-style
or ADK `Workflow` executor is a driver swap over the same data, not a
rewrite. And the definition doubles as documentation — the process fits
on one screen.
