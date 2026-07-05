"""THE SDLC DEFINITION — the process itself, as data.

This file answers exactly one question: what is the pipeline? Which
steps run, in what order, of what kind, with which bounded back-edges.
It contains no execution logic (driver.py) and no engine mechanics
(engine/): customizing the SDLC means editing THIS definition, adding a
folder under sdlc_steps/, and binding it in the driver's handler
registry — engine code stays untouched.

Step names refer to sdlc_steps/<name>/ packages (their knowledge AND
implementation). Back-edge iteration bounds are policy KEYS resolved
from sdlc_steps/orchestrator/policy.yaml (+ project overlays) — the
definition names the rule, never the number.

Three phases:
- planning:  once per sprint (assess everything, pack under budgets)
- per_item:  once per selected backlog item, in priority order
- release:   once over the approved queue (and re-runnable: held PRs
             stay queued until a later pass merges them)
"""

from dataclasses import dataclass

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
    name: str                     # = sdlc_steps/<name>/
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
