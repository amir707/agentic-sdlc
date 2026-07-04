---
name: mcp-server-authoring
description: Delivery-store MCP server conventions (use when adding or changing tools on mcp_server/)
---

# Delivery-store authoring conventions

Dev-time skill for whoever (human or agent) touches `mcp_server/`.
Never injected into runtime agents.

## Non-negotiables

- **Append-only audit**: never add an update or delete tool for the
  audit table. The absence of the tool IS the security property
  (design invariant 1). Same discipline for any table whose history is
  evidence (health_samples, deploys, token_usage).
- **Role scoping**: `open_incident` / `record_health_sample` are
  monitor-role only; `resolve_incident` is resolver-role only; other
  writes are agents-role. Enforce with `_require(...)` at the top of
  the tool, and add a role-scoping test for every new scoped tool.
- **Narrow tools**: one tool = one intention. No generic `query(sql)`
  or `update(table, ...)` tools, ever. If a caller needs a new
  question answered, add a narrowly-named read tool.
- **Isolation**: `mcp_server/` imports nothing from agents/, tools/, or
  the orchestrator. Dependency direction is core -> MCP client -> this
  server, never the reverse.

## Mechanics

- FastMCP over streamable HTTP, `stateless_http=True` (per-request
  independence is what makes the middleware-set role contextvar reach
  the tool).
- Bearer tokens map to roles in `BearerRoleMiddleware`; tokens come
  from env (`MCP_TOKEN_*`), are distinct, and never appear in code.
- SQLite in WAL mode via `mcp_server/db.py`; every table change updates
  SCHEMA there and the tests.
- Tool results are plain dicts/lists (FastMCP structuredContent);
  clients read `structuredContent`, not the text rendering.
- Tests: `tests/test_delivery_store.py` runs the real server as a
  subprocess over HTTP — keep the security assertions (401 on unknown
  token, role rejections, no audit mutation tools) passing and extend
  them with each new tool.
