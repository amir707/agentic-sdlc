"""delivery-store: the custom MCP server (single source of truth).

Why MCP here and nowhere else: the store is the one piece of shared
state crossed by every component (assessor, packer, monitor, CI,
approver, release manager — including agents on two model families),
so it is the one genuine shared boundary in the system. Placing MCP on
it enforces security properties at the interface instead of by
convention:

- The audit log is append-only because no update or delete tool exists.
- Only the monitor role can open incidents or record health samples;
  only the resolver role can resolve incidents. Roles are carried by
  per-caller bearer tokens (MCP_TOKEN_AGENTS / MCP_TOKEN_MONITOR /
  MCP_TOKEN_RESOLVER), checked by ASGI middleware before any tool runs.

Runs as one local HTTP service (localhost only) so there is visibly one
source of truth, curl-able while debugging. Everything single-caller
(deploy script, diff analysis, packer) stays a plain function — wrapping
those in MCP would be protocol decoration.
"""

import contextvars
import os

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from mcp_server import db

HOST = "127.0.0.1"
PORT = int(os.environ.get("DELIVERY_STORE_PORT", "8787"))

# Role of the caller for the current request, set by the auth middleware.
_caller_role: contextvars.ContextVar[str] = contextvars.ContextVar("caller_role")

# stateless_http: every request is independent, so the middleware-set
# role reliably reaches the tool handling that request.
mcp = FastMCP("delivery-store", host=HOST, port=PORT, stateless_http=True)


def _require(*roles: str) -> None:
    role = _caller_role.get(None)
    if role not in roles:
        raise PermissionError(
            f"tool restricted to roles {roles}, caller role is {role!r}")


# --- backlog / assessments -------------------------------------------------

@mcp.tool()
def list_backlog() -> list[dict]:
    """All backlog items, ordered by the PM's priority rank."""
    with db.connect() as conn:
        return db.list_backlog(conn)


@mcp.tool()
def get_item(item_id: str) -> dict:
    """One backlog item by id."""
    with db.connect() as conn:
        item = db.get_item(conn, item_id)
    if item is None:
        raise ValueError(f"no backlog item {item_id}")
    return item


@mcp.tool()
def set_item_status(item_id: str, status: str, pr: int | None = None) -> dict:
    """Record an item's lifecycle transition (and its PR, once known).
    The orchestrator resumes from THIS status — never from GitHub."""
    _require("agents")
    allowed = ("pending", "in_review", "verified", "preprod_passed",
               "awaiting_approval", "queued", "released", "rejected",
               "escalated", "failed")
    if status not in allowed:
        raise ValueError(f"status must be one of {allowed}")
    with db.connect() as conn:
        item = db.set_item_status(conn, item_id, status, pr)
    if item is None:
        raise ValueError(f"no backlog item {item_id}")
    return item


@mcp.tool()
def record_assessment(item_id: str, risk: str, effort: str,
                      token_estimate: int, rationale: str,
                      recommend_split: bool = False,
                      split_reason: str | None = None) -> dict:
    """Record the risk assessor's judgment for an item."""
    _require("agents")
    if risk not in ("low", "medium", "high"):
        raise ValueError("risk must be low|medium|high")
    if effort not in ("S", "M", "L"):
        raise ValueError("effort must be S|M|L")
    with db.connect() as conn:
        db.record_assessment(conn, item_id, risk, effort, token_estimate,
                             rationale, recommend_split, split_reason)
    return {"recorded": item_id}


@mcp.tool()
def list_assessments() -> list[dict]:
    """Latest assessment per item (packer input)."""
    with db.connect() as conn:
        return db.latest_assessments(conn)


# --- sprints ---------------------------------------------------------------

@mcp.tool()
def create_sprint(item_ids: list[str], rationale: str) -> dict:
    """Persist the packed sprint with the packer's rationale."""
    _require("agents")
    with db.connect() as conn:
        return db.create_sprint(conn, item_ids, rationale)


@mcp.tool()
def get_current_sprint() -> dict | None:
    """The most recently created sprint, or null."""
    with db.connect() as conn:
        return db.current_sprint(conn)


# --- incidents / health (role-scoped paths) --------------------------------

@mcp.tool()
def open_incident(area: str, error_rate: float) -> dict:
    """Open an incident for an area. MONITOR ROLE ONLY. Idempotent per area."""
    _require("monitor")
    with db.connect() as conn:
        return db.open_incident(conn, area, error_rate)


@mcp.tool()
def record_health_sample(area: str, error_rate: float) -> dict:
    """Record one sliding-window health sample. MONITOR ROLE ONLY."""
    _require("monitor")
    with db.connect() as conn:
        db.record_health_sample(conn, area, error_rate)
    return {"recorded": area, "error_rate": error_rate}


@mcp.tool()
def resolve_incident(incident_id: int, factors: dict) -> dict:
    """Resolve an incident after sustained recovery. RESOLVER ROLE ONLY.

    Detection (monitor) and closure (resolver) are separate concerns
    with separately scoped tokens; factors carry the recovery evidence.
    """
    _require("resolver")
    with db.connect() as conn:
        incident = db.resolve_incident(conn, incident_id)
        if incident is None:
            raise ValueError(f"no incident {incident_id}")
        db.append_audit(conn, "incident_resolver", "resolve_incident",
                        {"incident": incident_id, **factors})
    return incident


@mcp.tool()
def get_incident(incident_id: int) -> dict:
    """One incident by id."""
    with db.connect() as conn:
        incident = db.get_incident(conn, incident_id)
    if incident is None:
        raise ValueError(f"no incident {incident_id}")
    return incident


@mcp.tool()
def list_open_incidents() -> list[dict]:
    """All currently open incidents."""
    with db.connect() as conn:
        return db.list_open_incidents(conn)


@mcp.tool()
def list_health_samples(area: str, window_seconds: int) -> list[dict]:
    """Health samples for an area within the trailing window."""
    with db.connect() as conn:
        return db.list_health_samples(conn, area, window_seconds)


# --- deploys ---------------------------------------------------------------

@mcp.tool()
def record_deploy(pr: int, revision: str, traffic: str) -> dict:
    """Record a deploy event (preprod tag or traffic shift)."""
    _require("agents")
    with db.connect() as conn:
        db.record_deploy(conn, pr, revision, traffic)
    return {"recorded": revision}


@mcp.tool()
def list_recent_deploys(window_minutes: int) -> list[dict]:
    """Deploys within the trailing window (release_manager confidence input)."""
    with db.connect() as conn:
        return db.list_recent_deploys(conn, window_minutes)


# --- token usage -----------------------------------------------------------

@mcp.tool()
def record_token_usage(agent: str, model: str, input_tokens: int,
                       output_tokens: int) -> dict:
    """Meter one agent invocation (token budget = sprint capacity)."""
    _require("agents")
    with db.connect() as conn:
        db.record_token_usage(conn, agent, model, input_tokens, output_tokens)
    return {"recorded": agent}


@mcp.tool()
def summarize_token_usage(sprint_id: int | None = None) -> list[dict]:
    """Per agent+model token totals since the sprint began."""
    with db.connect() as conn:
        return db.summarize_token_usage(conn, sprint_id)


# --- audit (append-only BY CONSTRUCTION: no update/delete tool exists) -----

@mcp.tool()
def append_audit(actor: str, decision: str, factors: dict) -> dict:
    """Append one consequential decision with its factors."""
    _require("agents", "monitor", "resolver")
    with db.connect() as conn:
        return db.append_audit(conn, actor, decision, factors)


@mcp.tool()
def list_audit() -> list[dict]:
    """The full audit trail, oldest first."""
    with db.connect() as conn:
        return db.list_audit(conn)


# --- auth middleware ---------------------------------------------------------

class BearerRoleMiddleware:
    """Maps per-caller bearer tokens to roles before any tool executes.

    Token values come from env; the mapping token->role is the entire
    trust decision, so an unknown or missing token is rejected at the
    door with 401 rather than reaching tool code.
    """

    def __init__(self, app, tokens: dict[str, str]):
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        role = self.tokens.get(token)
        if role is None:
            response = JSONResponse({"error": "invalid bearer token"},
                                    status_code=401)
            await response(scope, receive, send)
            return
        _caller_role.set(role)
        await self.app(scope, receive, send)


def build_app():
    tokens = {}
    for env_name, role in (("MCP_TOKEN_AGENTS", "agents"),
                           ("MCP_TOKEN_MONITOR", "monitor"),
                           ("MCP_TOKEN_RESOLVER", "resolver")):
        value = os.environ.get(env_name)
        if not value:
            raise SystemExit(f"delivery-store: {env_name} must be set")
        tokens[value] = role
    if len(tokens) != 3:
        raise SystemExit("delivery-store: role tokens must be distinct")
    with db.connect() as conn:
        db.init_schema(conn)
    return BearerRoleMiddleware(mcp.streamable_http_app(), tokens)


if __name__ == "__main__":
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="warning")
