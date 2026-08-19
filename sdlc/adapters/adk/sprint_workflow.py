"""The sprint resume pass as an ADK Workflow (the resident sprint
orchestrator's root agent).

One node on purpose: the sprint pass is already composed of ADK graphs
per item (PipelineExecutor) plus deterministic planning/release phases —
this wrapper makes the WHOLE pass triggerable by ADK's ambient machinery
without re-expressing its internals. Each event runs one resume pass:
items resume from store status, every awaiting gate gets exactly ONE
authenticated look (the service sets GATE_WAIT_MINUTES=0), queued PRs
get a release pass, and the pass ends — the service goes back to
listening, parked on network I/O. Idempotent over the store, so
duplicate and spurious events are harmless.
"""

from google.adk.workflow import Workflow

from sdlc.sprint import flow as sprint
def build_sprint_workflow(ctx, parallel: int = 1) -> Workflow:
    async def resume_pass(node_input):
        # deprovision=False: a resident service keeps the warm checkout
        # across events (provision() heals it if it ever breaks).
        await sprint.run_pipeline(ctx, parallel=parallel, deprovision=False)
        return {"outcome": "pass_complete"}

    resume_pass.__name__ = "resume_pass"
    return Workflow(name="agentic_sdlc_sprint",
                    edges=[("START", resume_pass)])
