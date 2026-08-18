"""RunContext: the handles one run carries (built by orchestrator/bootstrap.py).

Coordination is through the store and artifacts, never in memory: the
PR is the artifact between coder and reviewer; the store is the
artifact between everything else and the single source of lifecycle
truth (G3). The context holds the handles to reach them, plus the two
ADR-0007 execution ports (per-item pipeline, release pass) that the
composition root injects.
"""

import asyncio
from dataclasses import dataclass, field

from orchestrator.activity import ActivityBoard
from orchestrator.config import ProjectConfig
from orchestrator.executor import PipelineExecutor, ReleaseExecutor
from orchestrator.invoker import AgentInvoker, Invocation
from orchestrator.ports import Deployer, RepoHost, Store
from orchestrator.workspace import Workspace


@dataclass
class RunContext:
    project: ProjectConfig
    store: Store                 # agents-role handle
    repo_host: RepoHost
    invoker: AgentInvoker
    workspace: Workspace
    # The two ADR-0007 execution ports, injected by the composition root
    # (None only in unit tests that never run them): the per-item pipeline
    # and the release pass — separate Workflows, separate clocks.
    executor: PipelineExecutor | None = None
    release_executor: ReleaseExecutor | None = None
    # The traffic-shift tool (release) and the resolver-role store handle
    # (incident hygiene before a sprint / release pass) — ports too.
    deployer: Deployer | None = None
    resolver_store: Store | None = None
    # Concurrent preprod deploys against ONE Cloud Run service would
    # fight over revision creation; CI is the one per-item stage that
    # must queue even when coders run in parallel.
    ci_lock: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    # Live "who is doing what, since when" (rendered by make watch).
    board: ActivityBoard = field(default_factory=ActivityBoard)
    # Release passes are serialized: with --parallel, two gate approvals
    # must not run two release managers over the same queue at once.
    release_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def invoke(self, spec, message: str) -> Invocation:
        """Every invocation is metered: token spend is sprint capacity."""
        result = await self.invoker.invoke(spec, message)
        await self.store.call(
            "record_token_usage", agent=spec.name, model=spec.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens)
        return result

    async def audit(self, actor: str, decision: str, factors: dict) -> None:
        await self.store.call("append_audit", actor=actor,
                              decision=decision, factors=factors)

    async def set_status(self, item_id: str, status: str,
                         pr: int | None = None) -> None:
        """Item lifecycle lives in the STORE; the orchestrator resumes
        from this, never from GitHub (the PR is only the artifact)."""
        await self.store.call("set_item_status", item_id=item_id,
                              status=status, pr=pr)
