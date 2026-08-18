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
from orchestrator.gate import check_decision
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


# --- verify / flag loop -------------------------------------------------------

def _max_flag_fixes(ctx) -> int:
    return int(ctx.project.policy("orchestrator")["max_flag_fix_iterations"])


_FLAG_FIX = ("Policy violation: this change's verified risk requires the NEW "
             "behavior to be gated behind a feature flag (default off) in "
             "flags.json. Wrap it and keep tests covering both flag states.")


async def verify(ctx, item: dict, state: PipelineState) -> NodeResult:
    """One verify pass with the bounded flag loop's policy: labels
    applied -> LABELED; flag missing -> bounce to the coder while budget
    remains, else escalate. Unparseable code makes measurement
    impossible — same bounded rework loop, same route back."""
    max_flag_fixes = _max_flag_fixes(ctx)
    try:
        result = await steps.verify_once(ctx, item, state.pr)
    except UnparseableSource as broken:
        if state.flag_fixes >= max_flag_fixes:
            await governance.escalate(
                ctx, item, state.pr, Actor.VERIFY,
                f"code still unparseable after {max_flag_fixes} fix")
            return NodeResult("unparseable, budget exhausted", Route.ESCALATE)
        await governance.bounce(ctx, item,
                                Rejection(state.pr, "code_unparseable", "coder",
                                          f"the code does not parse: {broken}"),
                                actor=Actor.VERIFY)
        state.flag_fixes += 1
        return NodeResult(_UNPARSEABLE_FIX.format(detail=broken),
                          Route.POLICY_FLAG_REQUIRED)

    state.verified = result
    if not result.needs_flag:
        await ctx.set_status(item["id"], ItemStatus.VERIFIED, state.pr)
        return NodeResult(result.title_prefix, Route.LABELED)
    if state.flag_fixes >= max_flag_fixes:
        await governance.escalate(
            ctx, item, state.pr, Actor.VERIFY,
            f"flag still missing after {max_flag_fixes} fix")
        return NodeResult("flag budget exhausted", Route.ESCALATE)
    state.flag_fixes += 1
    await governance.bounce(ctx, item,
                            Rejection(state.pr, "policy_flag_required", "coder",
                                      f"verified risk {result.verified_risk} "
                                      "requires a feature flag; none gates the "
                                      "new behavior"),
                            actor=Actor.VERIFY)
    return NodeResult(_FLAG_FIX, Route.POLICY_FLAG_REQUIRED)


async def coder_flag_fix(ctx, item: dict, branch: str, state: PipelineState,
                         instruction: str) -> NodeResult:
    """One fix round on the instruction verify chose (flag policy OR a
    syntax error) — one path serves both, returning to verify."""
    await steps.run_coder(ctx, item, branch, feedback=instruction)
    return NodeResult("flagged")


# --- preprod / approver / gate -------------------------------------------------

async def preprod_ci(ctx, item: dict, state: PipelineState) -> NodeResult:
    """Deploy the head to preprod and smoke it. Serialized on ctx.ci_lock:
    concurrent deploys against ONE Cloud Run service fight over revision
    creation even when coders run in parallel."""
    async with ctx.ci_lock:
        ok = await steps.run_preprod_ci(ctx, item, state.pr, state.verified)
    if ok:
        await ctx.set_status(item["id"], ItemStatus.PREPROD_PASSED, state.pr)
        return NodeResult(ok, Route.PASSED)
    await ctx.set_status(item["id"], ItemStatus.FAILED, state.pr)
    return NodeResult(ok, Route.FAILED)


async def approver(ctx, item: dict, state: PipelineState) -> NodeResult:
    """Post the dossier; remember the gate baseline (comment index right
    after it, so a decision made before the gate first looks is seen)."""
    state.gate_baseline = await steps.run_approver(
        ctx, item, state.pr, state.verified)
    await ctx.set_status(item["id"], ItemStatus.AWAITING_APPROVAL, state.pr)
    return NodeResult("dossier posted")


async def approval_gate(ctx, item: dict, state: PipelineState) -> NodeResult:
    """ONE authenticated look at the PR (ADR-0005): the decision's
    authority is the allowlisted GitHub comment, never the resume.
    APPROVE / REJECT route on; otherwise route is None and the output is
    the operator-facing wait message — the adapter turns that into a
    suspend (interrupt id from state.gate_tries) and the next look
    happens on the next nudge or event."""
    approvers = ctx.project.policy("approver")["approvers"]
    decision = await check_decision(
        ctx.repo_host, ctx.store, state.pr, approvers,
        state.gate_baseline, state.gate_ignores)

    if decision and decision.kind == "approve":
        return NodeResult(True, Route.APPROVE)
    if decision and decision.kind == "reject":
        await governance.bounce(ctx, item,
                                Rejection(state.pr, "human_declined", "backlog",
                                          decision.reason or "no reason given"),
                                actor=Actor.APPROVAL_GATE)
        return NodeResult(False, Route.REJECT)
    if decision:  # hold: advance the baseline past it, keep waiting
        state.gate_baseline = decision.comment_index + 1

    state.gate_tries += 1
    held = f" (on hold by {decision.author})" if decision else ""
    return NodeResult(
        f"PR #{state.pr} awaits a decision on GitHub{held}: an allowlisted "
        "approver comments /approve, /reject <reason>, or /hold on the PR. "
        "Decide there, then reply here (anything) to re-check.")


def gate_interrupt_id(state: PipelineState) -> str:
    """Fresh per suspend so each nudge reruns the gate; the executor
    parses the PR back out of it (pr_from_gate_interrupt)."""
    return f"gate_pr{state.pr}_try{state.gate_tries}"


def pr_from_gate_interrupt(interrupt_id: str) -> int | None:
    try:
        return int(interrupt_id.split("_pr", 1)[1].split("_try", 1)[0])
    except (IndexError, ValueError):
        return None


async def queued(ctx, item: dict, state: PipelineState) -> None:
    """The store status IS the release queue: setting it here is the
    whole hand-off; the sprint flow triggers a release pass after."""
    await ctx.set_status(item["id"], ItemStatus.QUEUED, state.pr)


# --- how long a run waits at the gate -----------------------------------------

@dataclass(frozen=True)
class GateWait:
    """The run-level policy for a suspended gate — a WAIT concern, kept
    apart from the gate's authority model above. Event-triggered
    services set GATE_WAIT_MINUTES=0: every gate gets exactly one look
    per event and the run parks the item (awaiting); the next event
    re-checks. Interactive runs either nudge (operator presses Enter
    after commenting on GitHub) or poll for up to the budget."""
    mode: str            # "poll" | "nudge"
    budget_seconds: float
    poll_seconds: float

    @classmethod
    def from_ctx(cls, ctx) -> "GateWait":
        import os
        policy = ctx.project.policy("approver")
        budget = float(os.environ.get("GATE_WAIT_MINUTES")
                       or policy.get("gate_wait_minutes", 5)) * 60.0
        return cls(mode=policy.get("gate_mode", "poll"),
                   budget_seconds=budget,
                   poll_seconds=float(policy.get("gate_poll_seconds", 10)))

    def next_action(self, waited_seconds: float) -> str:
        """'park' (return awaiting), 'nudge' (block on operator), or
        'poll' (sleep poll_seconds and look again)."""
        if self.budget_seconds <= 0:
            return "park"
        if self.mode == "nudge":
            return "nudge"
        return "park" if waited_seconds >= self.budget_seconds else "poll"
