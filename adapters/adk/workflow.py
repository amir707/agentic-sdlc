"""The per-item SDLC as an executing ADK 2 `Workflow` (ADR-0007).

This is no longer a display-only shadow of an imperative driver: it IS
the per-item execution path. `orchestrator/definition.py` remains the
framework-neutral truth (what the pipeline is); this module renders that
truth as a native ADK graph whose nodes delegate to the SAME single-shot
handlers the engine owns (run_coder, review_once, verify_once, ...), and
`adapters/adk/executor.py` runs it on ADK's engine. The definition's
bounded back-edges become routed cycle edges (ADK rejects unconditional
cycles); `adk web` can display and step the same graph the runner runs.

The human gate is a native ADK SUSPEND (`RequestInput`) with the twist
that keeps the identity model intact (ADR-0005): the resume carries NO
authority. The decision lives only in the allowlisted GitHub PR comment;
resuming merely triggers ONE `check_decision()` look. No valid command
there → the node suspends again (fresh interrupt_id per try). ADK
supplies the waiting mechanics; GitHub supplies the authenticated
decision; neither impersonates the other.

Governance boundary (G3): lifecycle status lives in the STORE and is set
at each transition here; terminal nodes return a small JSON outcome the
executor maps back to sprint.py, which hands off to the release flow. ADK owns
execution-cursor state only.
"""

from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import FunctionNode, Workflow

from mcp_server.vocab import STATUS_LABELS, Actor, Decision, ItemStatus
from orchestrator import governance, steps
from orchestrator.gate import check_decision

# Name-level edge table (source, target, route|None). Cycle edges carry
# routes (ADK rejects unconditional cycles) and realize the definition's
# back-edges:
#   code_reviewer -> coder_fix -> code_reviewer   (changes_requested)
#   verify -> coder_flag_fix -> verify            (policy_flag_required)
EDGE_TABLE: list[tuple[str, str, str | None]] = [
    ("START", "coder", None),
    ("coder", "code_reviewer", None),
    ("code_reviewer", "verify", "approved"),
    ("code_reviewer", "coder_fix", "changes_requested"),
    ("code_reviewer", "rejected", "out_of_scope"),
    ("code_reviewer", "escalated", "escalate"),
    ("coder_fix", "code_reviewer", "fixed"),
    ("coder_fix", "escalated", "impasse"),
    ("verify", "preprod_ci", "labeled"),
    ("verify", "coder_flag_fix", "policy_flag_required"),
    ("verify", "escalated", "escalate"),
    ("coder_flag_fix", "verify", None),
    ("preprod_ci", "approver", "passed"),
    ("preprod_ci", "failed", "failed"),
    ("approver", "approval_gate", None),
    ("approval_gate", "queued", "approve"),
    ("approval_gate", "rejected", "reject"),
]

# The four terminal nodes; their JSON `outcome` is the executor's result.
TERMINALS = (ItemStatus.QUEUED, ItemStatus.REJECTED, ItemStatus.FAILED,
             ItemStatus.ESCALATED)


def build_item_workflow(ctx, item: dict, branch: str,
                        existing_pr: int | None = None) -> Workflow:
    """One backlog item's journey as an ADK Workflow.

    `existing_pr` makes the coder node idempotent on resume: a run that
    already opened a PR skips re-implementation and continues the graph
    (every downstream node is itself SHA-idempotent, G5). Per-run
    scaffolding (PR number, iteration counters) lives in a closure;
    durable truth stays in GitHub and the store exactly as before.
    """
    flow = ctx.project.policy("orchestrator")
    state: dict = {"pr": existing_pr, "review_rounds": 0, "flag_fixes": 0,
                   "verified": None, "gate_baseline": 0, "gate_tries": 0,
                   "gate_ignores": set()}

    def _terminal(kind: ItemStatus) -> dict:
        ctx.board.finish(item["id"], STATUS_LABELS[kind])
        return {"outcome": kind, "pr": state["pr"]}

    async def coder(node_input):
        if state["pr"] is None:                      # fresh item
            await steps.run_coder(ctx, item, branch)
            state["pr"] = await steps.open_pr(ctx, item, branch)
            await ctx.set_status(item["id"], ItemStatus.IN_REVIEW, state["pr"])
        return Event(output=state["pr"])

    max_reviews = int(flow["max_fix_iterations"])
    max_flag_fixes = int(flow["max_flag_fix_iterations"])
    _UNPARSEABLE_FIX = ("Your change does not parse: {detail}. Fix the syntax "
                        "error so every file compiles and the tests run.")

    async def code_reviewer(node_input):
        from orchestrator.dependency_graph import UnparseableSource
        from orchestrator.rejection import Rejection
        # Resume idempotency: this head may already carry an approval (G5).
        if steps.review_already_approved(ctx, state["pr"]):
            return Event(output="already approved", route="approved")
        try:
            verdict = await steps.review_once(ctx, item, state["pr"],
                                               state["review_rounds"])
        except UnparseableSource as broken:
            # Agent-written code that does not parse is coder rework, not
            # an engine crash (code_unparseable) — one bounded round.
            if state["review_rounds"] >= max_reviews:
                await governance.escalate(
                    ctx, item, state["pr"], Actor.CODE_REVIEWER,
                    f"no approval after {max_reviews} fix iterations")
                return Event(output="unparseable, budget exhausted",
                             route="escalate")
            await governance.bounce(ctx, item,
                         Rejection(state["pr"], "code_unparseable", "coder",
                                   f"the code does not parse: {broken}"),
                         actor=Actor.CODE_REVIEWER)
            state["review_rounds"] += 1
            return Event(output=_UNPARSEABLE_FIX.format(detail=broken),
                         route="changes_requested")

        if verdict.verdict == "approve":
            await ctx.audit(Actor.CODE_REVIEWER, Decision.APPROVE_REVIEW,
                            {"pr": state["pr"],
                             "iterations": state["review_rounds"] + 1})
            return Event(output=verdict.reasoning, route="approved")
        if verdict.verdict == "out_of_scope":
            await governance.bounce(ctx, item,
                         Rejection(state["pr"], "out_of_scope", "author",
                                   verdict.reasoning),
                         actor=Actor.CODE_REVIEWER)
            return Event(output=verdict.reasoning, route="out_of_scope")
        if state["review_rounds"] >= max_reviews:
            await governance.escalate(
                ctx, item, state["pr"], Actor.CODE_REVIEWER,
                f"no approval after {max_reviews} fix iterations")
            return Event(output="fix budget exhausted", route="escalate")
        state["review_rounds"] += 1
        return Event(output=verdict.model_dump_json(),
                     route="changes_requested")

    async def coder_fix(node_input):
        changed, reply = await steps.run_coder(ctx, item, branch,
                                                feedback=str(node_input))
        if not changed:
            # Impasse: reviewer demanded changes, coder declined. Put the
            # disagreement ON THE ARTIFACT and hand it to a human —
            # re-reviewing an identical diff resolves nothing.
            ctx.repo_host.post_comment(state["pr"], (
                "**🤖 AI coder — response to review (no code changes "
                f"made)**\n\n{reply or '(no reasoning returned)'}"))
            await governance.escalate(
                ctx, item, state["pr"], Actor.CODE_REVIEWER,
                "coder declined the requested changes (no-change fix round)")
            return Event(output="impasse", route="impasse")
        return Event(output="fixed", route="fixed")

    async def verify(node_input):
        from orchestrator.dependency_graph import UnparseableSource
        from orchestrator.rejection import Rejection
        try:
            result = await steps.verify_once(ctx, item, state["pr"])
        except UnparseableSource as broken:
            # Measurement is impossible until the code parses; same
            # bounded rework loop as a missing flag.
            if state["flag_fixes"] >= max_flag_fixes:
                await governance.escalate(
                    ctx, item, state["pr"], Actor.VERIFY,
                    f"code still unparseable after {max_flag_fixes} fix")
                return Event(output="unparseable, budget exhausted",
                             route="escalate")
            await governance.bounce(ctx, item,
                         Rejection(state["pr"], "code_unparseable", "coder",
                                   f"the code does not parse: {broken}"),
                         actor=Actor.VERIFY)
            state["flag_fixes"] += 1
            return Event(output=_UNPARSEABLE_FIX.format(detail=broken),
                         route="policy_flag_required")

        state["verified"] = result
        if not result.needs_flag:
            await ctx.set_status(item["id"], ItemStatus.VERIFIED, state["pr"])
            return Event(output=result.title_prefix, route="labeled")
        if state["flag_fixes"] >= max_flag_fixes:
            await governance.escalate(
                ctx, item, state["pr"], Actor.VERIFY,
                f"flag still missing after {max_flag_fixes} fix")
            return Event(output="flag budget exhausted", route="escalate")
        state["flag_fixes"] += 1
        await governance.bounce(ctx, item,
                     Rejection(state["pr"], "policy_flag_required", "coder",
                               f"verified risk {result.verified_risk} "
                               "requires a feature flag; none gates the new "
                               "behavior"),
                     actor=Actor.VERIFY)
        return Event(output=(
            "Policy violation: this change's verified risk requires the NEW "
            "behavior to be gated behind a feature flag (default off) in "
            "flags.json. Wrap it and keep tests covering both flag states."),
            route="policy_flag_required")

    async def coder_flag_fix(node_input):
        # node_input is the fix instruction verify chose (flag policy OR
        # a syntax error) — one fix path serves both, returning to verify.
        await steps.run_coder(ctx, item, branch, feedback=str(node_input))
        return Event(output="flagged")

    async def preprod_ci(node_input):
        # Concurrent preprod deploys against ONE Cloud Run service fight
        # over revision creation; serialize them even when coders run in
        # parallel (same guard the sequential loop held).
        async with ctx.ci_lock:
            ok = await steps.run_preprod_ci(ctx, item, state["pr"],
                                             state["verified"])
        if ok:
            await ctx.set_status(item["id"], ItemStatus.PREPROD_PASSED, state["pr"])
            return Event(output=ok, route="passed")
        await ctx.set_status(item["id"], ItemStatus.FAILED, state["pr"])
        return Event(output=ok, route="failed")

    async def approver(node_input):
        state["gate_baseline"] = await steps.run_approver(
            ctx, item, state["pr"], state["verified"])
        await ctx.set_status(item["id"], ItemStatus.AWAITING_APPROVAL, state["pr"])
        return Event(output="dossier posted")

    async def approval_gate(node_input):
        """Native HITL suspend. The resume is a NUDGE, never a decision:
        each rerun performs exactly one authenticated look at the PR."""
        approvers = ctx.project.policy("approver")["approvers"]
        decision = await check_decision(
            ctx.repo_host, ctx.store, state["pr"], approvers,
            state["gate_baseline"], state["gate_ignores"])

        if decision and decision.kind == "approve":
            yield Event(output=True, route="approve")
            return
        if decision and decision.kind == "reject":
            from orchestrator.rejection import Rejection
            await governance.bounce(ctx, item,
                         Rejection(state["pr"], "human_declined", "backlog",
                                   decision.reason or "no reason given"),
                         actor=Actor.APPROVAL_GATE)
            yield Event(output=False, route="reject")
            return
        if decision:  # hold: advance the baseline past it, keep waiting
            state["gate_baseline"] = decision.comment_index + 1

        state["gate_tries"] += 1
        held = f" (on hold by {decision.author})" if decision else ""
        yield RequestInput(
            interrupt_id=f"gate_pr{state['pr']}_try{state['gate_tries']}",
            message=(f"PR #{state['pr']} awaits a decision on GitHub"
                     f"{held}: an allowlisted approver comments /approve, "
                     "/reject <reason>, or /hold on the PR. Decide there, "
                     "then reply here (anything) to re-check."))

    async def queued(node_input):
        # The store status IS the release queue (Workstream B): the
        # release pass reads status=queued, so setting it here is all the
        # hand-off the release loop needs. The driver runs a release pass
        # after the executor returns.
        await ctx.set_status(item["id"], ItemStatus.QUEUED, state["pr"])
        return _terminal(ItemStatus.QUEUED)

    def rejected(node_input):
        return _terminal(ItemStatus.REJECTED)

    def failed(node_input):
        return _terminal(ItemStatus.FAILED)

    def escalated(node_input):
        return _terminal(ItemStatus.ESCALATED)

    nodes = {"coder": coder, "code_reviewer": code_reviewer,
             "coder_fix": coder_fix, "verify": verify,
             "coder_flag_fix": coder_flag_fix, "preprod_ci": preprod_ci,
             "approver": approver, "approval_gate": approval_gate,
             "queued": queued, "rejected": rejected, "failed": failed,
             "escalated": escalated}
    for name, fn in nodes.items():
        fn.__name__ = name
    # The gate must RERUN on resume (re-check GitHub) rather than treat
    # the chat reply as its output — the reply is a nudge, not a value.
    nodes["approval_gate"] = FunctionNode(
        func=approval_gate, name="approval_gate", rerun_on_resume=True)

    # This ADK version encodes routing as (source, {route: target, ...});
    # unrouted edges are plain (source, target). Group the table by source.
    by_source: dict[str, list[tuple[str, str | None]]] = {}
    for src, dst, route in EDGE_TABLE:
        by_source.setdefault(src, []).append((dst, route))

    edges = []
    for src, targets in by_source.items():
        src_node = "START" if src == "START" else nodes[src]
        routed = {route: nodes[dst] for dst, route in targets
                  if route is not None}
        plain = [nodes[dst] for dst, route in targets if route is None]
        if routed:
            edges.append((src_node, routed))
        for target in plain:
            edges.append((src_node, target))

    return Workflow(name=f"item_{item['id'].replace('-', '_')}", edges=edges)
