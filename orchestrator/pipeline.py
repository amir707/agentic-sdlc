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
- The node decisions: what each graph node DOES — call the single-shot
  step (steps.py), apply the bounded-loop policy, record the governance
  outcome (governance.py), and name the Route taken. ADK's FunctionNode
  wraps each of these in one line; nothing here imports the framework,
  so every branch is unit-testable with a fake ctx.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from mcp_server.vocab import Actor, Decision, ItemStatus
from orchestrator import governance, steps
from orchestrator.dependency_graph import UnparseableSource
from orchestrator.rejection import Rejection
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


@dataclass
class NodeResult:
    """What a node hands back to the graph: its output (the next node's
    input) and the Route it took (None on an unrouted edge)."""
    output: object
    route: Route | None = None


_UNPARSEABLE_FIX = ("Your change does not parse: {detail}. Fix the syntax "
                    "error so every file compiles and the tests run.")


def _max_reviews(ctx) -> int:
    return int(ctx.project.policy("orchestrator")["max_fix_iterations"])


# --- coder / review loop ------------------------------------------------------

async def coder(ctx, item: dict, branch: str, state: PipelineState) -> NodeResult:
    """Fresh item: implement and open the PR. On resume (state.pr set)
    skip re-implementation — every downstream node is SHA-idempotent (G5)."""
    if state.pr is None:
        await steps.run_coder(ctx, item, branch)
        state.pr = await steps.open_pr(ctx, item, branch)
        await ctx.set_status(item["id"], ItemStatus.IN_REVIEW, state.pr)
    return NodeResult(output=state.pr)


async def code_reviewer(ctx, item: dict, state: PipelineState) -> NodeResult:
    """One review round, with the bounded fix loop's policy applied:
    approve -> APPROVED; out of scope -> bounce to the author;
    changes requested -> CHANGES_REQUESTED while budget remains, else
    escalate. Code that does not parse is coder rework, not an engine
    crash — same bounded round."""
    max_reviews = _max_reviews(ctx)
    # Resume idempotency: this head may already carry an approval (G5).
    if steps.review_already_approved(ctx, state.pr):
        return NodeResult("already approved", Route.APPROVED)
    try:
        verdict = await steps.review_once(ctx, item, state.pr,
                                          state.review_rounds)
    except UnparseableSource as broken:
        if state.review_rounds >= max_reviews:
            await governance.escalate(
                ctx, item, state.pr, Actor.CODE_REVIEWER,
                f"no approval after {max_reviews} fix iterations")
            return NodeResult("unparseable, budget exhausted", Route.ESCALATE)
        await governance.bounce(ctx, item,
                                Rejection(state.pr, "code_unparseable", "coder",
                                          f"the code does not parse: {broken}"),
                                actor=Actor.CODE_REVIEWER)
        state.review_rounds += 1
        return NodeResult(_UNPARSEABLE_FIX.format(detail=broken),
                          Route.CHANGES_REQUESTED)

    if verdict.verdict == "approve":
        await ctx.audit(Actor.CODE_REVIEWER, Decision.APPROVE_REVIEW,
                        {"pr": state.pr, "iterations": state.review_rounds + 1})
        return NodeResult(verdict.reasoning, Route.APPROVED)
    if verdict.verdict == "out_of_scope":
        await governance.bounce(ctx, item,
                                Rejection(state.pr, "out_of_scope", "author",
                                          verdict.reasoning),
                                actor=Actor.CODE_REVIEWER)
        return NodeResult(verdict.reasoning, Route.OUT_OF_SCOPE)
    if state.review_rounds >= max_reviews:
        await governance.escalate(
            ctx, item, state.pr, Actor.CODE_REVIEWER,
            f"no approval after {max_reviews} fix iterations")
        return NodeResult("fix budget exhausted", Route.ESCALATE)
    state.review_rounds += 1
    return NodeResult(verdict.model_dump_json(), Route.CHANGES_REQUESTED)


async def coder_fix(ctx, item: dict, branch: str, state: PipelineState,
                    feedback: str) -> NodeResult:
    """One fix round on the reviewer's feedback. A round with NO code
    change is an impasse (reviewer demanded, coder declined): put the
    disagreement on the artifact and hand it to a human — re-reviewing
    an identical diff resolves nothing."""
    changed, reply = await steps.run_coder(ctx, item, branch, feedback=feedback)
    if not changed:
        ctx.repo_host.post_comment(state.pr, (
            "**🤖 AI coder — response to review (no code changes "
            f"made)**\n\n{reply or '(no reasoning returned)'}"))
        await governance.escalate(
            ctx, item, state.pr, Actor.CODE_REVIEWER,
            "coder declined the requested changes (no-change fix round)")
        return NodeResult("impasse", Route.IMPASSE)
    return NodeResult("fixed", Route.FIXED)
