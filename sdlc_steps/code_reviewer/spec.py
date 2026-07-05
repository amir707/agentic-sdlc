"""Code reviewer — reasoning worker definition (deliberately thin).

Gemini (different family from the coder). Read-only workspace access:
it may open any file for context, but the diff and coverage numbers
arrive in the payload and its verdict goes back as structured text —
the orchestrator posts it to the PR (artifact-mediated, ADR-0003).
"""

import os

from orchestrator.agent_support import gemini_model
from orchestrator.config import ProjectConfig
from tools.fs_tools import make_workspace_tools
from orchestrator.invoker import AgentSpec


def build(project: ProjectConfig, workspace_dir: str, diff_text: str) -> AgentSpec:
    read_only = [t for t in make_workspace_tools(workspace_dir)
                 if t.__name__ in ("list_files", "read_file")]

    def analyze_diff() -> dict:
        """Analyze the current PR's unified diff for files touched, areas, and flag coverage.

        Returns:
            dict: A dictionary containing:
                - files_touched (list[str]): files changed in the PR.
                - areas_touched (list[str]): areas of the candidate app impacted by these changes.
                - flag_coverage (dict): details of whether changed code is covered by feature flags.
        """
        from tools import diff_analysis
        files = diff_analysis.files_touched(diff_text)
        areas = diff_analysis.areas_touched(diff_text, project)
        flag = diff_analysis.flag_coverage(diff_text)
        return {
            "files_touched": files,
            "areas_touched": sorted(areas),
            "flag_coverage": flag
        }

    return AgentSpec(
        name="code_reviewer",
        instruction=project.prompt("code_reviewer"),
        model=os.environ.get("REVIEWER_MODEL", gemini_model()),
        tools=read_only + [analyze_diff],
    )
