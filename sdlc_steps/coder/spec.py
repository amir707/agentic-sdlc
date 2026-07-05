"""Coder — reasoning worker definition (deliberately thin).

Claude via LiteLLM: the heaviest workload gets the strongest coding
model (a different family from the reviewer, to decorrelate failure
modes). Its ONLY tools are the sandboxed workspace (engine/fs_tools.py)
— no git, no network, no store access; the orchestrator handles
branches, pushes, and PRs afterwards.
"""

import os

from engine.config import ProjectConfig
from engine.fs_tools import make_workspace_tools
from engine.invoker import AgentSpec


def build(project: ProjectConfig, workspace_dir: str) -> AgentSpec:
    return AgentSpec(
        name="coder",
        instruction=project.prompt("coder"),
        model=os.environ.get("CODER_MODEL", "anthropic/claude-sonnet-5"),
        tools=make_workspace_tools(workspace_dir),
    )
