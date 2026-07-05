"""The AgentInvoker port — the SDLC core's ENTIRE knowledge of agents.

Framework-neutral by design (ADR-0007): the core describes WHAT an
agent is (AgentSpec: prompt, model name, tool needs) and WHAT an
invocation yields (Invocation: final text + token usage). It never
imports an agent framework. The one implementation lives in
adapters/adk/invoker.py (Google ADK 2); swapping frameworks means one
new adapter and zero core changes.

Tool needs are declared, not constructed:
- a plain Python callable  -> a function tool (frameworks wrap natively)
- StoreTools(tool_filter)  -> role-scoped delivery-store MCP tools,
  materialized by the adapter into the framework's MCP client

Agents are invocations, not daemons: state lives in GitHub and the
delivery store, never in the agent.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class StoreTools:
    """Declarative need for delivery-store MCP tools (narrow filter).

    Tool narrowness is the security story: the store's role tokens
    bound what COULD be called; this filter bounds what the model even
    sees. The adapter materializes it into a real MCP toolset."""
    tool_filter: tuple[str, ...]


@dataclass
class AgentSpec:
    """Framework-neutral agent description."""
    name: str
    instruction: str
    model: str                       # "gemini-*" native, else via LiteLLM
    tools: list = field(default_factory=list)   # callables and/or StoreTools
    output_schema: type | None = None  # pydantic model; only for tool-less agents


@dataclass
class Invocation:
    text: str
    input_tokens: int
    output_tokens: int


class AgentInvoker(Protocol):
    """The port. One implementation: adapters.adk.invoker.ADKInvoker."""

    async def invoke(self, spec: AgentSpec, message: str) -> Invocation: ...
