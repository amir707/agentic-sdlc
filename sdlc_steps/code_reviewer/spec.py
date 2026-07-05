"""Code reviewer — reasoning worker definition (deliberately thin).

Gemini (different family from the coder). Read-only workspace access:
it may open any file for context, but the diff and coverage numbers
arrive in the payload and its verdict goes back as structured text —
the orchestrator posts it to the PR (artifact-mediated, ADR-0003).
"""

import os

from engine.agent_support import gemini_model
from engine.config import ProjectConfig
from engine.fs_tools import make_workspace_tools
from engine.invoker import AgentSpec


def build(project: ProjectConfig, workspace_dir: str) -> AgentSpec:
    read_only = [t for t in make_workspace_tools(workspace_dir)
                 if t.__name__ in ("list_files", "read_file")]
    return AgentSpec(
        name="code_reviewer",
        instruction=project.prompt("code_reviewer"),
        model=os.environ.get("REVIEWER_MODEL", gemini_model()),
        tools=read_only,
    )
