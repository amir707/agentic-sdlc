"""What the engine needs from the world — Protocols only, no implementations.

  agents.py     AgentInvoker (+ AgentSpec, StoreTools, Invocation): run one agent
  execution.py  PipelineExecutor / ReleaseExecutor (+ ItemOutcome): run one graph
  world.py      RepoHost, Store, Deployer (+ RepoHostError, DeployError)

Adapters satisfy these structurally; the composition root (app/bootstrap)
is the only place a concrete is chosen.
"""

from sdlc.ports.agents import AgentInvoker, AgentSpec, Invocation, StoreTools  # noqa: F401
from sdlc.ports.execution import ItemOutcome, PipelineExecutor, ReleaseExecutor  # noqa: F401
from sdlc.ports.world import DeployError, Deployer, RepoHost, RepoHostError, Store  # noqa: F401
