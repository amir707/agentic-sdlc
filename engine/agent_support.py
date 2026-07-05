"""Shared wiring helpers for reasoning-step spec.py files.

Tool narrowness is the security story: each agent's McpToolset carries
a tool_filter listing exactly what that step needs — the store's role
tokens bound what COULD be called, the filter bounds what the model
even sees.
"""

import os

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)


def gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-flash-latest")


def store_toolset(tool_filter: list[str]) -> McpToolset:
    port = os.environ.get("DELIVERY_STORE_PORT", "8787")
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": f"Bearer {os.environ['MCP_TOKEN_AGENTS']}"},
        ),
        tool_filter=tool_filter,
    )
