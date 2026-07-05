"""Thin MCP client for the delivery store (engine side).

Deterministic engine components (orchestrator, verify, CI, monitor,
resolver) talk to the store through this client — the same MCP surface
the agents use, so the store's role scoping applies to everyone. The
caller's role is whatever token it holds; nothing engine-side bypasses
the server.
"""

import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class DeliveryStore:
    def __init__(self, token: str, url: str | None = None):
        port = os.environ.get("DELIVERY_STORE_PORT", "8787")
        self.url = url or f"http://127.0.0.1:{port}/mcp"
        self.headers = {"Authorization": f"Bearer {token}"}

    @classmethod
    def for_agents(cls) -> "DeliveryStore":
        return cls(os.environ["MCP_TOKEN_AGENTS"])

    @classmethod
    def for_monitor(cls) -> "DeliveryStore":
        return cls(os.environ["MCP_TOKEN_MONITOR"])

    @classmethod
    def for_resolver(cls) -> "DeliveryStore":
        return cls(os.environ["MCP_TOKEN_RESOLVER"])

    async def call(self, tool: str, **args):
        """One tool call per connection: the server is stateless and the
        callers are episodic, so simplicity beats connection reuse."""
        async with streamablehttp_client(self.url, headers=self.headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
        if result.isError:
            detail = result.content[0].text if result.content else "unknown"
            raise StoreError(f"{tool}: {detail}")
        sc = result.structuredContent
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        if sc is not None:
            return sc
        return json.loads(result.content[0].text) if result.content else None


class StoreError(Exception):
    pass
