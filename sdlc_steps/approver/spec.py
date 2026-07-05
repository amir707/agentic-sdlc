"""Approver — reasoning worker definition (deliberately thin).

Pure reasoning over the payload (preprod result, verified labels,
review threads, originating item): it assembles the dossier text and
nothing else. The DECISION belongs to a human on the approvers list,
given on the PR itself (orchestrator/gate.py, ADR-0005).
"""

from orchestrator.agent_support import gemini_model
from orchestrator.config import ProjectConfig
from orchestrator.invoker import AgentSpec


def build(project: ProjectConfig) -> AgentSpec:
    return AgentSpec(
        name="approver",
        instruction=project.prompt("approver"),
        model=gemini_model(),
        tools=[],
    )
