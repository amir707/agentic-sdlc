"""THE SDLC DEFINITION — the process itself, as data.

This file answers exactly one question: what is the pipeline? Which
steps run, in what order, of what kind, with which bounded back-edges.
It contains no execution logic (planning.py, steps.py, pipeline.py,
sprint.py, release_flow.py) and no engine mechanics: customizing the
SDLC means editing THIS definition (steps + the per-item graph), adding
a folder under sdlc/steps/, and binding it in driver.HANDLERS — engine
code stays untouched.

A project may vary the pipeline's SHAPE along axes that are safe to
vary (PipelineShape, from projects-config/<name>/pipeline.yaml — today:
human_gate); per_item_edges(shape) composes the graph. Guarantees are
not knobs and unknown keys are rejected (proposal 0001 §7).

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

# --- the per-project SHAPE (proposal 0001 §7, first increment) ------------------
#
# A project may vary the pipeline's shape along axes that are SAFE to
# vary; everything else is a load-bearing guarantee (G1-G6) and is not a
# knob. The line is drawn HERE, in code, not in a config schema:
#   - human_gate: whether a human /approve on the PR is required before
#     an item is queued for release. Off = the approver still posts its
#     dossier (the audit artifact) and the item queues immediately; the
#     machine gates (verify, preprod) and the release guard still run.
# NOT configurable: the coder/review loop, verify + flag policy, preprod
# (release promotes the revision it produces), the release guard, the
# audit. projects-config/<name>/pipeline.yaml sets the shape; unknown
# keys are rejected so a guarantee cannot be "turned off" by typo.

@dataclass(frozen=True)
class PipelineShape:
    human_gate: bool = True

    @classmethod
    def from_mapping(cls, data: dict | None) -> "PipelineShape":
        data = dict(data or {})
        unknown = set(data) - {f for f in cls.__dataclass_fields__}
        if unknown:
            raise ValueError(
                f"pipeline.yaml: unknown key(s) {sorted(unknown)} — the "
                f"configurable shape is {sorted(cls.__dataclass_fields__)}; "
                "everything else is an engine guarantee, not a knob")
        for key, value in data.items():
            if not isinstance(value, bool):
                raise ValueError(f"pipeline.yaml: {key} must be true|false")
        return cls(**data)


DEFAULT_SHAPE = PipelineShape()


def per_item_edges(shape: PipelineShape = DEFAULT_SHAPE
                   ) -> tuple[tuple[str, str, Route | None], ...]:
    """The per-item graph for one project's shape:
    (source, target, route | None) — an unrouted edge is the only way out."""
    edges: list[tuple[str, str, Route | None]] = [
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
    ]
    if shape.human_gate:
        edges += [
            ("approver", "approval_gate", None),
            ("approval_gate", "queued", Route.APPROVE),
            ("approval_gate", "rejected", Route.REJECT),
        ]
    else:
        edges += [("approver", "queued", None)]   # dossier posted, no gate
    return tuple(edges)


# The default shape's graph — what the docs render and most tests pin.
PER_ITEM_EDGES = per_item_edges(DEFAULT_SHAPE)


def per_item_nodes(edges=PER_ITEM_EDGES) -> tuple[str, ...]:
    """Every node the graph names, in first-appearance order."""
    seen: list[str] = []
    for src, dst, _ in edges:
        for n in (src, dst):
            if n != START and n not in seen:
                seen.append(n)
    return tuple(seen)
