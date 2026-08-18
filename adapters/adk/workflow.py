"""The per-item SDLC as an executing ADK 2 `Workflow` (ADR-0007).

This is no longer a display-only shadow of an imperative driver: it IS
the per-item execution path. `orchestrator/definition.py` remains the
framework-neutral truth (what the pipeline is); this module renders that
truth as a native ADK graph whose nodes are one-line wrappers over the
engine's own node decisions (`orchestrator/pipeline.py`: policy, loop
budgets, governance outcomes, routes — all framework-free), and
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

from mcp_server.vocab import STATUS_LABELS, ItemStatus
from orchestrator import pipeline
from orchestrator.pipeline import PipelineState, Route

# Name-level edge table (source, target, route|None). Cycle edges carry
# routes (ADK rejects unconditional cycles) and realize the definition's
# back-edges:
#   code_reviewer -> coder_fix -> code_reviewer   (changes_requested)
#   verify -> coder_flag_fix -> verify            (policy_flag_required)
EDGE_TABLE: list[tuple[str, str, Route | None]] = [
    ("START", "coder", None),
    ("coder", "code_reviewer", None),
    ("code_reviewer", "verify", Route.APPROVED),
    ("code_reviewer", "coder_fix", Route.CHANGES_REQUESTED),
    ("code_reviewer", "rejected", Route.OUT_OF_SCOPE),
    ("code_reviewer", "escalated", Route.ESCALATE),
    ("coder_fix", "code_reviewer", Route.FIXED),
    ("coder_fix", "escalated", Route.IMPASSE),
    ("verify", "preprod_ci", Route.LABELED),
    ("verify", "coder_flag_fix", Route.POLICY_FLAG_REQUIRED),
    ("verify", "escalated", Route.ESCALATE),
    ("coder_flag_fix", "verify", None),
    ("preprod_ci", "approver", Route.PASSED),
    ("preprod_ci", "failed", Route.FAILED),
    ("approver", "approval_gate", None),
    ("approval_gate", "queued", Route.APPROVE),
    ("approval_gate", "rejected", Route.REJECT),
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
    state = PipelineState(pr=existing_pr)

    def _terminal(kind: ItemStatus) -> dict:
        ctx.board.finish(item["id"], STATUS_LABELS[kind])
        return {"outcome": kind, "pr": state.pr}

    def _event(result: pipeline.NodeResult) -> Event:
        return Event(output=result.output, route=result.route)

    async def coder(node_input):
        return _event(await pipeline.coder(ctx, item, branch, state))

    async def code_reviewer(node_input):
        return _event(await pipeline.code_reviewer(ctx, item, state))

    async def coder_fix(node_input):
        return _event(await pipeline.coder_fix(ctx, item, branch, state,
                                               feedback=str(node_input)))

    async def verify(node_input):
        return _event(await pipeline.verify(ctx, item, state))

    async def coder_flag_fix(node_input):
        return _event(await pipeline.coder_flag_fix(ctx, item, branch, state,
                                                    instruction=str(node_input)))

    async def preprod_ci(node_input):
        return _event(await pipeline.preprod_ci(ctx, item, state))

    async def approver(node_input):
        return _event(await pipeline.approver(ctx, item, state))

    async def approval_gate(node_input):
        """Native HITL suspend. The resume is a NUDGE, never a decision:
        each rerun performs exactly one authenticated look at the PR
        (pipeline.approval_gate); no route means 'no decision yet' and
        the node suspends again under a fresh interrupt id."""
        look = await pipeline.approval_gate(ctx, item, state)
        if look.route is not None:
            yield _event(look)
            return
        yield RequestInput(interrupt_id=pipeline.gate_interrupt_id(state),
                           message=str(look.output))

    async def queued(node_input):
        await pipeline.queued(ctx, item, state)
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
    by_source: dict[str, list[tuple[str, Route | None]]] = {}
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
