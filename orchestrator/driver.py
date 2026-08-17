"""The driver façade: binds the SDLC definition to its implementations.

definition.py says WHAT the process is; the modules below carry it out:

  planning.py      assess every item, pack a sprint (once per store)
  steps.py         the single-shot per-item handlers the ADK pipeline's
                   nodes call (coder, review, verify, preprod, approver)
  sprint.py        resume dispatch from store status + the run loop
  release_flow.py  the queue read and per-PR merge/hold decision the
                   release Workflow's nodes call
  governance.py    the ONE way an item is escalated / failed / held /
                   bounced (uniform evidence)
  context.py       RunContext, built by a composition root

This file keeps the HANDLERS registry (every definition step name ->
its implementation; test_definition asserts coverage) and re-exports
the entry-point surface so composition roots import one name.

Coordination is through the store and artifacts, never in memory: the
PR is the artifact between coder and reviewer; the store is the artifact
between everything else and the single source of lifecycle truth (G3).
"""

from orchestrator.context import RunContext, build_context  # noqa: F401
from orchestrator.gate import check_decision
from orchestrator.planning import run_risk_assessor, run_sprint_packer
from orchestrator.release_flow import (  # noqa: F401
    decide_release_pr, release_queue, run_release_pass, trigger_release)
from orchestrator.sprint import process_item, run_pipeline  # noqa: F401
from orchestrator.steps import (
    review_once, run_approver, run_coder, run_preprod_ci, verify_once)
from sdlc_steps import incident_resolver


# The explicit binding: definition step name -> handler.
# Every definition step name binds to its implementation. Planning and
# release steps are driver functions; the per-item steps are executed by
# the ADK Workflow (adapters/adk/workflow.py) and bind to the single-shot
# handlers its nodes call — the fix/flag/gate LOOPS are graph edges, not
# Python loops (ADR-0007). `test_definition` asserts this map covers the
# definition; `check_decision` is the gate's atom.
HANDLERS = {
    "risk_assessor": run_risk_assessor,
    "sprint_packer": run_sprint_packer,
    "coder": run_coder,
    "code_reviewer": review_once,
    "verify": verify_once,
    "preprod_ci": run_preprod_ci,
    "approver": run_approver,
    "approval_gate": check_decision,
    "incident_resolver": incident_resolver.run,
    "release_manager": run_release_pass,
}
