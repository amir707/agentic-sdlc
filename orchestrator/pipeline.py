"""The per-item pipeline's state and vocabulary (framework-free).

The per-item journey — coder → review → verify → preprod → approver →
gate, with its fix/flag loops — EXECUTES on ADK's graph engine
(adapters/adk/workflow.py, ADR-0007). What that graph carries between
nodes, and the words its edges are routed by, are the engine's business,
not the framework's; they live here so they can be typed, read, and
tested without ADK.

- Route: the edge labels. Cycle edges must carry routes (ADK rejects
  unconditional cycles) and every routed decision names one of these,
  so a typo is an AttributeError, not a silently dead edge.
- PipelineState: the per-run scaffolding one item's graph accumulates
  (PR number, bounded-loop counters, the verify result, gate cursor).
  Durable truth stays in GitHub and the store (G3); this is the cursor
  the ADK session owns.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from sdlc_steps.verify import VerifyResult


class Route(StrEnum):
    # code_reviewer ->
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    OUT_OF_SCOPE = "out_of_scope"
    # coder_fix ->
    FIXED = "fixed"
    IMPASSE = "impasse"
    # verify ->
    LABELED = "labeled"
    POLICY_FLAG_REQUIRED = "policy_flag_required"
    # preprod_ci ->
    PASSED = "passed"
    FAILED = "failed"
    # approval_gate ->
    APPROVE = "approve"
    REJECT = "reject"
    # any bounded loop, on exhaustion ->
    ESCALATE = "escalate"


@dataclass
class PipelineState:
    """One item's in-flight scaffolding. `pr` is None until the coder
    node opens the PR (or is set on resume so the coder node skips
    re-implementation, G5)."""
    pr: int | None = None
    review_rounds: int = 0        # bounded by policy max_fix_iterations
    flag_fixes: int = 0           # bounded by policy max_flag_fix_iterations
    verified: VerifyResult | None = None
    gate_baseline: int = 0        # comment index the gate looks after
    gate_tries: int = 0           # distinct interrupt ids per suspend
    gate_ignores: set = field(default_factory=set)  # already-audited bad commands
