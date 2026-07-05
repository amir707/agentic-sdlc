# ADR-0005: Human approval gate as allowlisted PR command comments

**Status:** accepted

## Context

The approval gate needs a human decision with three outcomes (approve /
reject with reason / hold). The cheapest implementation is a CLI
`input()` prompt in the orchestrator terminal; the alternative is a
decision expressed on the PR itself.

## Decision

The gate is a PR command comment: `/approve`, `/reject <reason>`, or
`/hold`, authored by a user on the project's configurable approvers
list (`approvers` in the approver step policy — default is an empty list at `sdlc_steps/approver/policy.yaml`; each project must set its own at `projects-config/<name>/sdlc_steps/approver/policy.yaml`). The approver agent
posts its decision-factors dossier as a PR comment first, so the human
decides on the same artifact that carries the evidence. The
orchestrator blocks, polling the PR until a valid command from an
allowlisted author appears; command comments from non-listed authors
are ignored and the ignore is audited.

## Consequences

The gate is identity-checked (an allowlist, not whoever holds the
terminal), the decision is permanently attached to the artifact it
governs, and the demo shows a real GitHub interaction. The polling wait
is a deliberate carve-out from the no-polling rule (ADR-0003). The
production successor is the same shape asynchronously: a GitHub review
request, with the pipeline suspending at "awaiting approval" and
resuming on decision.

## Amendment (gate modes: poll / nudge / ADK suspend)

The decision's authority never moves — it is always the allowlisted
GitHub comment. What became configurable is WHEN the orchestrator looks
for it (`gate_mode` in the approver step policy):

- **poll** (default): check the PR every few seconds (`await_decision`).
- **nudge**: block until the operator presses Enter, then check exactly
  once (`check_decision`). No busy polling.
- In the ADK Workflow expression the gate is a NATIVE `RequestInput`
  suspend: resuming the workflow in the chat channel triggers one
  `check_decision` look at the PR; no valid command there → it suspends
  again with a fresh interrupt_id. The resume is a nudge, never a
  decision — whoever resumes the chat cannot approve anything.

Two correctness details shared by all modes: the scan baseline is
captured when the DOSSIER is posted (a human who decides before the
gate first looks is still seen), and a `/hold` advances the baseline
past itself so a later `/approve` becomes visible.
