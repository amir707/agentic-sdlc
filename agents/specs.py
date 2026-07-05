"""Agent definitions — deliberately thin.

An agent here is just: a composed prompt (base + project customisation,
via engine.config), a model (coder on Claude via LiteLLM, everything
else Gemini — different families to decorrelate failure modes), and a
narrow tool surface. All behavioral knowledge lives in
sdlc-steps/<step>/prompts.md; all numbers live in policy files. Nothing
in this module encodes judgment.

Tool narrowness is the security story: each agent's McpToolset carries
a tool_filter listing exactly what that role needs — the store's
role tokens bound what COULD be called, the filter bounds what the
model even sees.
"""

import os

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)

from engine.config import ProjectConfig
from engine.fs_tools import make_workspace_tools
from engine.invoker import AgentSpec


def _gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-flash-latest")


def _store_toolset(tool_filter: list[str]) -> McpToolset:
    port = os.environ.get("DELIVERY_STORE_PORT", "8787")
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": f"Bearer {os.environ['MCP_TOKEN_AGENTS']}"},
        ),
        tool_filter=tool_filter,
    )


def risk_assessor(project: ProjectConfig) -> AgentSpec:
    return AgentSpec(
        name="risk_assessor",
        instruction=project.prompt("risk-assessor"),
        model=_gemini_model(),
        tools=[_store_toolset(["get_item", "record_assessment"])],
    )


def coder(project: ProjectConfig, workspace_dir: str) -> AgentSpec:
    """Claude via LiteLLM; the heaviest workload gets the strongest
    coding model. Its ONLY tools are the sandboxed workspace (see
    engine/fs_tools.py) — no git, no network, no store access."""
    return AgentSpec(
        name="coder",
        instruction=project.prompt("coder"),
        model=os.environ.get("CODER_MODEL", "anthropic/claude-sonnet-5"),
        tools=make_workspace_tools(workspace_dir),
    )


def code_reviewer(project: ProjectConfig, workspace_dir: str) -> AgentSpec:
    """Gemini, read-only workspace access: it may open any file for
    context, but the diff and coverage numbers arrive in the payload
    and its verdict goes back as structured text — the orchestrator
    posts it to the PR (artifact-mediated, ADR-0003)."""
    read_only = [t for t in make_workspace_tools(workspace_dir)
                 if t.__name__ in ("list_files", "read_file")]
    return AgentSpec(
        name="code_reviewer",
        instruction=project.prompt("code-reviewer"),
        model=os.environ.get("REVIEWER_MODEL", _gemini_model()),
        tools=read_only,
    )


def approver(project: ProjectConfig) -> AgentSpec:
    """Pure reasoning over the payload (preprod result, labels, review
    threads, item): assembles the dossier text; the human decides."""
    return AgentSpec(
        name="approver",
        instruction=project.prompt("approver"),
        model=_gemini_model(),
        tools=[],
    )


def release_manager(project: ProjectConfig) -> AgentSpec:
    """Read-only view of the store; its decisions come back as
    structured text and the ORCHESTRATOR executes merges/traffic
    shifts via deterministic tools. The agent holds no credentials
    and cannot mutate anything directly."""
    return AgentSpec(
        name="release_manager",
        instruction=project.prompt("release-manager"),
        model=_gemini_model(),
        tools=[_store_toolset([
            "list_open_incidents", "list_recent_deploys",
            "list_health_samples", "get_incident",
        ])],
    )
