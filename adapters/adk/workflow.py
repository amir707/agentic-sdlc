"""ADK 2.0 Workflow expression of the per-item SDLC graph.

The same pipeline two ways, one implementation of each step:
orchestrator/definition.py is the framework-neutral truth, the
sequential driver is the guaranteed execution path, and THIS module
renders the per-item flow as a native ADK `Workflow` — the definition's
bounded back-edges become routed cycle edges, so `adk web` can display
the governance graph and step through it. Every node delegates to the
SAME single-shot functions the driver uses (review_once, verify_once,
run_coder, ...); nothing is reimplemented.

The human gate maps to a blocking node rather than ADK's chat-side
`RequestInput` HITL on purpose: our human input arrives as an
allowlisted PR comment on GitHub (ADR-0005), not as a chat turn, so the
node polls the artifact exactly as the driver does.
"""

from google.adk.events.event import Event
from google.adk.workflow import Workflow

from orchestrator import driver
from orchestrator.definition import SDLC

# Name-level edge table (source, target, route|None). Kept as plain
# data so tests can assert parity with orchestrator/definition.py
# without constructing ADK objects. Cycle edges carry routes (ADK
# rejects unconditional cycles) and realize the definition's back-edges:
#   code_reviewer -> coder_fix -> code_reviewer   (changes_requested)
#   verify -> coder_flag_fix -> verify            (policy_flag_required)
EDGE_TABLE: list[tuple[str, str, str | None]] = [
    ("START", "coder", None),
    ("coder", "code_reviewer", None),
    ("code_reviewer", "verify", "approved"),
    ("code_reviewer", "coder_fix", "changes_requested"),
    ("code_reviewer", "rejected", "out_of_scope"),
    ("code_reviewer", "escalated", "escalate"),
    ("coder_fix", "code_reviewer", None),
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

BACK_EDGE_NODES = {"code_reviewer": "coder_fix", "verify": "coder_flag_fix"}


def build_item_workflow(ctx, item: dict, branch: str) -> Workflow:
    """One backlog item's journey as an ADK Workflow.

    State (PR number, iteration counters) lives in a closure because it
    is per-run scaffolding; durable truth stays in GitHub and the store
    exactly as in the driver.
    """
    flow = ctx.project.policy("orchestrator")
    state: dict = {"pr": None, "review_rounds": 0, "flag_fixes": 0,
                   "verified": None}

    async def coder(node_input):
        await driver.run_coder(ctx, item, branch)
        state["pr"] = await driver.open_pr(ctx, item, branch)
        return Event(output=state["pr"])

    async def code_reviewer(node_input):
        verdict = await driver.review_once(ctx, item, state["pr"],
                                           state["review_rounds"])
        if verdict.verdict == "approve":
            return Event(output=verdict.reasoning, route="approved")
        if verdict.verdict == "out_of_scope":
            from orchestrator.rejection import Rejection, reject
            await reject(ctx.store, ctx.repo_host,
                         Rejection(state["pr"], "out_of_scope", "author",
                                   verdict.reasoning),
                         actor="code_reviewer")
            return Event(output=verdict.reasoning, route="out_of_scope")
        if state["review_rounds"] >= int(flow["max_fix_iterations"]):
            return Event(output="fix budget exhausted", route="escalate")
        state["review_rounds"] += 1
        return Event(output=verdict.model_dump_json(),
                     route="changes_requested")

    async def coder_fix(node_input):
        await driver.run_coder(ctx, item, branch, feedback=str(node_input))
        return Event(output="fixed")

    async def verify(node_input):
        result = await driver.verify_once(ctx, item, state["pr"])
        state["verified"] = result
        if not result.needs_flag:
            return Event(output=result.title_prefix, route="labeled")
        if state["flag_fixes"] >= int(flow["max_flag_fix_iterations"]):
            return Event(output="flag budget exhausted", route="escalate")
        state["flag_fixes"] += 1
        from orchestrator.rejection import Rejection, reject
        await reject(ctx.store, ctx.repo_host,
                     Rejection(state["pr"], "policy_flag_required", "coder",
                               f"verified risk {result.verified_risk} "
                               "requires a feature flag"),
                     actor="verify")
        return Event(output=result.verified_risk,
                     route="policy_flag_required")

    async def coder_flag_fix(node_input):
        await driver.run_coder(ctx, item, branch, feedback=(
            "Policy violation: wrap the NEW behavior behind a feature flag "
            "(default off) in flags.json; test both flag states."))
        return Event(output="flagged")

    async def preprod_ci(node_input):
        ok = await driver.run_preprod_ci(ctx, item, state["pr"],
                                         state["verified"])
        return Event(output=ok, route="passed" if ok else "failed")

    async def approver(node_input):
        await driver.run_approver(ctx, item, state["pr"], state["verified"])
        return Event(output="dossier posted")

    async def approval_gate(node_input):
        approved = await driver.run_approval_gate(ctx, item, state["pr"])
        return Event(output=approved, route="approve" if approved else "reject")

    def queued(node_input):
        driver_entry = driver.ApprovedPR(pr=state["pr"], item=item,
                                         verified=state["verified"])
        ctx.approved.append(driver_entry)
        return f"PR #{state['pr']} queued for release"

    def rejected(node_input):
        return f"PR #{state['pr']} rejected"

    def failed(node_input):
        return f"PR #{state['pr']} failed preprod"

    def escalated(node_input):
        return f"PR #{state['pr']} escalated to a human"

    nodes = {"coder": coder, "code_reviewer": code_reviewer,
             "coder_fix": coder_fix, "verify": verify,
             "coder_flag_fix": coder_flag_fix, "preprod_ci": preprod_ci,
             "approver": approver, "approval_gate": approval_gate,
             "queued": queued, "rejected": rejected, "failed": failed,
             "escalated": escalated}
    for name, fn in nodes.items():
        fn.__name__ = name

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


def definition_parity() -> dict:
    """Structural parity facts for tests: the Workflow covers every
    per-item definition step and realizes every declared back-edge as
    a routed cycle."""
    names = {src for src, _, _ in EDGE_TABLE if src != "START"} \
        | {dst for _, dst, _ in EDGE_TABLE}
    cycles = {
        step.name: (step.name, BACK_EDGE_NODES.get(step.name))
        for step in SDLC.per_item if step.back_edge
    }
    realized = {
        step: fix in names
        and (fix, step, None) in [(s, d, r) for s, d, r in EDGE_TABLE]
        for step, (_, fix) in cycles.items()
    }
    return {"node_names": names, "back_edges_realized": realized}
