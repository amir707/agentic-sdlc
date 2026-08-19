"""Approver — reasoning worker definition (deliberately thin).

Pure reasoning over the payload (preprod result, verified labels,
review threads, originating item): it assembles the dossier text and
nothing else. The DECISION belongs to a human on the approvers list,
given on the PR itself (sdlc/governance/gate.py, ADR-0005).
"""

from sdlc.engine.agent_support import gemini_model
from sdlc.engine.config import ProjectConfig
from sdlc.ports.agents import AgentSpec
from sdlc.governance.schemas import Dossier


def build(project: ProjectConfig) -> AgentSpec:
    return AgentSpec(
        name="approver",
        instruction=project.prompt("approver"),
        model=gemini_model(),
        tools=[],
        # Tool-less, so the Dossier contract is enforced natively via
        # output_schema; the orchestrator renders it for the human.
        output_schema=Dossier,
    )
