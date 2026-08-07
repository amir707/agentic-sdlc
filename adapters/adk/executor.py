"""Run a per-item SDLC `Workflow` on ADK's engine (Workstream A).

`adapters/adk/workflow.py` builds the graph; this module executes it via
an `App` + `Runner` with resumability, and drives the human gate's
`RequestInput` suspend with the project's gate-wait policy (the same
`nudge` / `poll` / budget semantics the sequential driver used).

What crosses the boundary back to the driver is a small `ItemOutcome`
(G3): the store already carries lifecycle status (the workflow nodes set
it at each transition — `status=queued` IS the release queue); the
driver reads the outcome to decide whether to run a release pass. ADK
owns execution-cursor state only.
"""

import asyncio
import os

from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import InMemoryRunner
from google.genai import types

from adapters.adk.workflow import build_item_workflow
from orchestrator.executor import ItemOutcome

_APP_NAME = "agentic_sdlc"


def _start_message() -> types.Content:
    return types.Content(role="user",
                         parts=[types.Part.from_text(text="start")])


def _resume_message(interrupt_id: str) -> types.Content:
    """A gate NUDGE: a function-response keyed to the suspended node's
    interrupt id. It carries no decision — it only makes the gate node
    rerun and take one authenticated look at the PR (ADR-0005)."""
    return types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(
            id=interrupt_id, name="adk_request_input",
            response={"nudge": "recheck"}))])


def _pr_from_interrupt(interrupt_id: str) -> int | None:
    # interrupt ids are "gate_pr{pr}_try{n}" (see workflow.approval_gate).
    try:
        return int(interrupt_id.split("_pr", 1)[1].split("_try", 1)[0])
    except (IndexError, ValueError):
        return None


async def _drive(runner, session_id: str, user_id: str,
                 message: types.Content) -> tuple[str | None, dict | None]:
    """One run/resume pass. Returns (interrupt_id, terminal_outcome):
    a suspended run yields an interrupt id; a completed run yields the
    terminal node's {"outcome", "pr"} dict."""
    interrupt: str | None = None
    outcome: dict | None = None
    async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                        new_message=message):
        long_running = getattr(event, "long_running_tool_ids", None)
        if long_running:
            interrupt = next(iter(long_running))
        out = getattr(event, "output", None)
        if isinstance(out, dict) and "outcome" in out:
            outcome = out
    return interrupt, outcome


class ADKPipelineExecutor:
    """Runs the per-item pipeline as a native ADK Workflow (PipelineExecutor
    port). Stateless; the composition root injects one instance."""

    async def run_item(self, ctx, item: dict, branch: str,
                       existing_pr: int | None = None) -> ItemOutcome:
        return await run_item_workflow(ctx, item, branch,
                                       existing_pr=existing_pr)


async def run_item_workflow(ctx, item: dict, branch: str,
                            existing_pr: int | None = None) -> ItemOutcome:
    """Execute the item's per-item pipeline on ADK; return its outcome."""
    flow = build_item_workflow(ctx, item, branch, existing_pr=existing_pr)
    app = App(name=_APP_NAME, root_agent=flow,
              resumability_config=ResumabilityConfig(is_resumable=True))
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name=_APP_NAME, user_id=item["id"])

    policy = ctx.project.policy("approver")
    mode = policy.get("gate_mode", "poll")
    # GATE_WAIT_MINUTES env overrides policy: the resident sprint service
    # sets it to 0 so an EVENT-triggered pass gives every gate exactly one
    # authenticated look and never waits — the next event re-checks.
    budget = float(os.environ.get("GATE_WAIT_MINUTES")
                   or policy.get("gate_wait_minutes", 5)) * 60.0
    poll = float(policy.get("gate_poll_seconds", 10))

    message = _start_message()
    waited = 0.0
    while True:
        interrupt, outcome = await _drive(runner, session.id, item["id"],
                                          message)
        if outcome is not None:
            return ItemOutcome(kind=outcome["outcome"], pr=outcome.get("pr"))
        if interrupt is None:
            # Neither a terminal nor a suspend: the graph stalled. Treat
            # as an escalation rather than silently succeeding.
            return ItemOutcome(kind="escalated", pr=existing_pr)

        pr = _pr_from_interrupt(interrupt)
        if budget <= 0:
            # Event semantics regardless of gate_mode: one look happened
            # inside the gate node; park and let the next event re-check.
            print(f"[gate] PR #{pr}: no decision on this look — the item "
                  "stays awaiting_approval; the next event re-checks",
                  flush=True)
            return ItemOutcome(kind="awaiting", pr=pr)
        if mode == "nudge":
            # The decision's authority is the GitHub comment; pressing
            # Enter (like the ADK resume) only triggers one look at it.
            await asyncio.to_thread(
                input, f"[gate] decide on PR #{pr} via a GitHub comment, "
                       "then press Enter to re-check: ")
        else:
            if waited >= budget:
                print(f"[gate] no decision on PR #{pr} within "
                      f"{budget / 60:.0f}m — releasing this run; the item "
                      "stays awaiting_approval and any rerun re-checks",
                      flush=True)
                return ItemOutcome(kind="awaiting", pr=pr)
            await asyncio.sleep(poll)
            waited += poll
        message = _resume_message(interrupt)
