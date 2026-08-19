"""THE SDLC DEFINITION — the process itself, as data.

This file answers exactly one question: what is the pipeline? Which
steps run, in what order, of what kind, with which bounded back-edges.
It contains no execution logic (planning.py, steps.py, pipeline.py,
sprint.py, release_flow.py) and no engine mechanics: customizing the
SDLC means editing THIS definition (steps + the per-item graph), adding
a folder under sdlc/steps/, and binding it in driver.HANDLERS — engine
code stays untouched.

This definition is LOAD-BEARING: sdlc/adapters/adk/item_workflow.py builds the
executing ADK graph from PER_ITEM_EDGES below (it renders, it does not
redefine), and docs/architecture.md's pipeline diagram is generated
from the same data (scripts/render_pipeline.py). One definition, no
shadows.

Step names refer to sdlc/steps/<name>/ packages (their knowledge AND
implementation). Back-edge iteration bounds are policy KEYS resolved
from sdlc.steps/orchestrator/policy.yaml (+ project overlays) — the
definition names the rule, never the number.

Three phases:
- planning:  once per sprint (assess everything, pack under budgets)
- per_item:  once per selected backlog item, in priority order
- release:   immediately after EVERY gate approval (trickle), one PR
             and one deployment at a time, each decision seeing the
             previous deploy; held PRs stay queued and are reconsidered
             on every pass, plus a final end-of-sprint pass
"""

from dataclasses import dataclass
from enum import StrEnum

REASONING = "reasoning"        # LLM decision loop (an agent)
DETERMINISTIC = "deterministic"  # solver / script / threshold check
GATE = "gate"                  # blocks on a human decision


@dataclass(frozen=True)
class BackEdge:
    """A bounded return path — rejection is one mechanism, reasons are
    data (design invariant 4)."""
    to: str                       # step that receives the work back
    reason_code: str
    max_iterations_policy: str    # key in the orchestrator policy


@dataclass(frozen=True)
class Step:
    name: str                     # = sdlc/steps/<name>/
    kind: str
    back_edge: BackEdge | None = None


@dataclass(frozen=True)
class SdlcDefinition:
    planning: tuple[Step, ...]
    per_item: tuple[Step, ...]
    release: tuple[Step, ...]

    def all_steps(self) -> tuple[Step, ...]:
        return self.planning + self.per_item + self.release


SDLC = SdlcDefinition(
    planning=(
        Step("risk_assessor", REASONING),
        Step("sprint_packer", DETERMINISTIC),
    ),
    per_item=(
        Step("coder", REASONING),
        Step("code_reviewer", REASONING,
             back_edge=BackEdge(to="coder", reason_code="changes_requested",
                                max_iterations_policy="max_fix_iterations")),
        Step("verify", DETERMINISTIC,
             back_edge=BackEdge(to="coder", reason_code="policy_flag_required",
                                max_iterations_policy="max_flag_fix_iterations")),
        Step("preprod_ci", DETERMINISTIC),
        Step("approver", REASONING),
        Step("approval_gate", GATE),
    ),
    release=(
        Step("incident_resolver", DETERMINISTIC),
        Step("release_manager", REASONING),
    ),
)


# --- the per-item GRAPH ---------------------------------------------------------
#
# The steps above say what runs; the graph says how the per-item journey
# is wired: which node hands to which, on which ROUTE. Cycle edges must
# carry routes (an engine cannot loop unconditionally), and every
# BackEdge above is realized as one — its reason_code IS the route.
#
# Nodes beyond the Steps: the two fix legs (coder_fix, coder_flag_fix —
# the coder re-entered with feedback) and the four TERMINALS, whose name
# is the item's resulting store status.

class Route(StrEnum):
    """Edge labels. A typo is an AttributeError, not a silently dead edge."""
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


START = "START"
TERMINALS = ("queued", "rejected", "failed", "escalated")

# (source, target, route | None) — an unrouted edge is the only way out.
PER_ITEM_EDGES: tuple[tuple[str, str, Route | None], ...] = (
    (START, "coder", None),
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
)


def per_item_nodes() -> tuple[str, ...]:
    """Every node the graph names, in first-appearance order."""
    seen: list[str] = []
    for src, dst, _ in PER_ITEM_EDGES:
        for n in (src, dst):
            if n != START and n not in seen:
                seen.append(n)
    return tuple(seen)
