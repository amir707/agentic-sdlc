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
list (`approvers` in the approver step policy — engine default is an empty list at `sdlc_steps/approver/policy.yaml`; each project must set its own at `config/projects/<name>/sdlc_steps/approver/policy.yaml`). The approver agent
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
