"""Risk assessor — reasoning worker definition (deliberately thin).

Judgment lives in prompts.md next to this file; numbers live in policy
files. This module only wires prompt + model + a narrow tool surface.
"""

from orchestrator.agent_support import gemini_model, store_toolset
from orchestrator.config import ProjectConfig
from orchestrator.invoker import AgentSpec


def build(project: ProjectConfig) -> AgentSpec:
    return AgentSpec(
        name="risk_assessor",
        instruction=project.prompt("risk_assessor"),
        model=gemini_model(),
        tools=[store_toolset(["get_item", "record_assessment"])],
    )
