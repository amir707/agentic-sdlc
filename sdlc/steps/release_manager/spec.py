"""Release manager — reasoning worker definition (deliberately thin).

Read-only view of the store: incidents, recent deploys, health samples.
Its decisions come back as structured text and the ORCHESTRATOR
executes merges and traffic shifts via deterministic tools — the agent
holds no credentials and cannot mutate anything directly. It only ever
receives PRs that carry a human approval record (design invariant 3).
"""

from sdlc.engine.agent_support import gemini_model, store_toolset
from sdlc.engine.config import ProjectConfig
from sdlc.ports.agents import AgentSpec


def build(project: ProjectConfig) -> AgentSpec:
    return AgentSpec(
        name="release_manager",
        instruction=project.prompt("release_manager"),
        model=gemini_model(),
        tools=[store_toolset([
            "list_open_incidents", "list_recent_deploys",
            "list_health_samples", "get_incident",
        ])],
    )
