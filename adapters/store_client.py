"""Thin MCP client for the delivery store (engine side).

Deterministic engine components (orchestrator, verify, CI, monitor,
resolver) talk to the store through this client — the same MCP surface
the agents use, so the store's role scoping applies to everyone. The
caller's role is whatever token it holds; nothing engine-side bypasses
the server.
"""

import json
import os
from urllib.parse import urlsplit

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def auth_headers(token: str, url: str) -> dict[str, str]:
    """Headers for one store request.

    The role token travels in X-Store-Token so the Authorization header
    stays free for Cloud Run's IAM layer: with STORE_IAM_AUTH set (the
    cloud job/services set it), a Google-signed identity token for the
    store's origin is attached per request — tokens expire hourly, so
    they are fetched here, not at client construction. Locally neither
    branch fires and the server accepts X-Store-Token directly.
    """
    headers = {"X-Store-Token": token}
    if os.environ.get("STORE_IAM_AUTH", "").lower() in ("1", "true", "yes"):
        import google.auth.transport.requests
        import google.oauth2.id_token
        parts = urlsplit(url)
        audience = f"{parts.scheme}://{parts.netloc}"
        id_token = google.oauth2.id_token.fetch_id_token(
            google.auth.transport.requests.Request(), audience)
        headers["Authorization"] = f"Bearer {id_token}"
    return headers


def store_url() -> str:
    """Where the delivery store is: DELIVERY_STORE_URL for a remote
    store (Cloud Run, or the gcloud proxy), else the local loopback rung
    on DELIVERY_STORE_PORT. Every store client resolves through HERE."""
    port = os.environ.get("DELIVERY_STORE_PORT", "8787")
    return (os.environ.get("DELIVERY_STORE_URL")
            or f"http://127.0.0.1:{port}/mcp")


def store_base_url() -> str:
    """The store's origin (for its custom routes: /status, /state)."""
    return store_url().removesuffix("/mcp").rstrip("/")


class DeliveryStore:
    def __init__(self, token: str, url: str | None = None):
        self.url = url or store_url()
        self._token = token

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
        headers = auth_headers(self._token, self.url)
        async with streamablehttp_client(self.url, headers=headers) as (r, w, _):
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
