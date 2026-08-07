"""The PipelineExecutor port — how the core runs one item's per-item
pipeline without knowing which framework executes it (ADR-0007).

`definition.py` says WHAT the per-item pipeline is; an executor RUNS it.
The core (driver) calls this port; the composition root (`__main__`)
injects a concrete executor. The ADK implementation
(`adapters/adk/executor.py`) runs the pipeline as a native ADK Workflow.
Keeping the port here means the core never imports a framework — the
same boundary the AgentInvoker port draws for agent turns.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ItemOutcome:
    """Where the per-item pipeline left the item. Lifecycle status and the
    release queue already live in the store / ctx (G3); this is only what
    the driver needs to decide the next move (e.g. run the release pass)."""
    kind: str            # queued | rejected | failed | escalated | awaiting
    pr: int | None


class PipelineExecutor(Protocol):
    async def run_item(self, ctx, item: dict, branch: str,
                       existing_pr: int | None = None) -> ItemOutcome:
        """Run one backlog item's per-item pipeline to a terminal state."""
        ...


class ReleaseExecutor(Protocol):
    async def run_pass(self, ctx) -> None:
        """Run ONE release pass over store state (Workstream B). Release
        is its own control loop with its own clock (approvals, incidents,
        confidence windows), so it is its own executor — a separate ADK
        Workflow, not a branch of the per-item one. The core calls this
        port; the ADK implementation runs the release graph."""
        ...
