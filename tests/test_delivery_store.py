"""Delivery-store MCP server tests: run the real server as a subprocess
and exercise it over streamable HTTP, the same way agents reach it.

The security-relevant assertions are the point: role-scoped tools
reject the wrong token's role, unknown tokens die at the middleware
with 401, and the audit surface has no mutation tools at all.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parent.parent
PORT = 8899
URL = f"http://127.0.0.1:{PORT}/mcp"

TOKENS = {
    "agents": "test-token-agents",
    "monitor": "test-token-monitor",
    "resolver": "test-token-resolver",
}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("store") / "store.sqlite3"
    env = os.environ | {
        "DELIVERY_STORE_DB": str(db_file),
        "DELIVERY_STORE_PORT": str(PORT),
        "MCP_TOKEN_AGENTS": TOKENS["agents"],
        "MCP_TOKEN_MONITOR": TOKENS["monitor"],
        "MCP_TOKEN_RESOLVER": TOKENS["resolver"],
    }
    # Fail LOUDLY if a stale server squats on the port: connecting to a
    # leftover process (empty temp DB) once made every test lie.
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
        raise RuntimeError(
            f"port {PORT} already in use — kill the stale test server "
            "(pkill -f mcp_server.server on 8899) and rerun")
    except OSError:
        pass

    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server"], cwd=ROOT, env=env)
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("delivery-store server exited at startup")
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("delivery-store server did not start")

    # Seed backlog directly through the repository layer. Everything
    # from here runs under try/finally: a seeding crash must still
    # terminate the server (an orphan once poisoned every later run).
    try:
        sys.path.insert(0, str(ROOT))
        from mcp_server import db
        os.environ["DELIVERY_STORE_DB"] = str(db_file)
        items = json.loads(
            (ROOT / "projects-config" / "candidate-app" / "backlog.json").read_text())
        conn = db.connect()
        db.init_schema(conn)
        conn.executemany(
            "INSERT INTO backlog_items (id, title, description, type, "
            "implementation, claimed_risk, claimed_impact, area_hint, "
            "priority_rank) VALUES (:id, :title, :description, :type, "
            ":implementation, :claimed_risk, :claimed_impact, :area_hint, "
            ":priority_rank)",
            items)
        conn.commit()
        conn.close()
        yield URL
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def _call(role: str, tool: str, args: dict | None = None):
    headers = {"Authorization": f"Bearer {TOKENS[role]}"}
    async with streamablehttp_client(URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, args or {})


def _payload(result):
    assert not result.isError, result.content
    # FastMCP exposes tool return values via structuredContent (a list
    # comes back as {"result": [...]}); content[] is a display rendering
    # that splits lists into one text item per element.
    sc = result.structuredContent
    if isinstance(sc, dict) and set(sc) == {"result"}:
        return sc["result"]
    if sc is not None:
        return sc
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_backlog_seeded(server):
    result = await _call("agents", "list_backlog")
    items = _payload(result)
    assert len(items) == 10
    assert items[0]["id"] == "PAY-101"  # ordered by priority rank


@pytest.mark.anyio
async def test_unknown_token_rejected_at_middleware(server):
    headers = {"Authorization": "Bearer wrong-token"}
    with pytest.raises(Exception):
        async with streamablehttp_client(URL, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()


@pytest.mark.anyio
async def test_incident_paths_are_role_scoped(server):
    # Agents must not open incidents...
    result = await _call("agents", "open_incident",
                         {"area": "payments", "error_rate": 0.5})
    assert result.isError

    # ...the monitor can.
    incident = _payload(await _call("monitor", "open_incident",
                                    {"area": "payments", "error_rate": 0.5}))
    assert incident["status"] == "open"

    # Idempotent: second open for the same area returns the same incident.
    again = _payload(await _call("monitor", "open_incident",
                                 {"area": "payments", "error_rate": 0.9}))
    assert again["id"] == incident["id"]

    # The monitor must not resolve its own incident...
    result = await _call("monitor", "resolve_incident",
                         {"incident_id": incident["id"], "factors": {}})
    assert result.isError

    # ...only the resolver closes, and the closure is audited.
    resolved = _payload(await _call(
        "resolver", "resolve_incident",
        {"incident_id": incident["id"],
         "factors": {"healthy_windows": 2, "error_rate": 0.0}}))
    assert resolved["status"] == "resolved"

    audit = _payload(await _call("agents", "list_audit"))
    assert any(e["decision"] == "resolve_incident" for e in audit)


@pytest.mark.anyio
async def test_audit_is_append_only_by_construction(server):
    entry = _payload(await _call(
        "agents", "append_audit",
        {"actor": "test", "decision": "hold_merge", "factors": {"pr": 7}}))
    assert entry["id"] > 0

    # The property itself: no update or delete tool exists on the server.
    headers = {"Authorization": f"Bearer {TOKENS['agents']}"}
    async with streamablehttp_client(URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
    assert "append_audit" in tools and "list_audit" in tools
    forbidden = {name for name in tools
                 if "audit" in name and name not in ("append_audit", "list_audit")}
    assert not forbidden


@pytest.mark.anyio
async def test_sprint_roundtrip(server):
    sprint = _payload(await _call(
        "agents", "create_sprint",
        {"item_ids": ["PAY-101", "CAT-201"], "rationale": "test pack"}))
    assert sprint["item_ids"] == ["PAY-101", "CAT-201"]
    current = _payload(await _call("agents", "get_current_sprint"))
    assert current["id"] == sprint["id"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_item_lifecycle_is_store_owned(server):
    """The orchestrator resumes from backlog_items.status/pr — set via
    the agents role, validated, and readable by everyone."""
    updated = _payload(await _call("agents", "set_item_status",
                                   {"item_id": "PAY-101",
                                    "status": "in_review", "pr": 42}))
    assert updated["status"] == "in_review" and updated["pr"] == 42

    # status-only update keeps the recorded PR
    updated = _payload(await _call("agents", "set_item_status",
                                   {"item_id": "PAY-101",
                                    "status": "released"}))
    assert updated["status"] == "released" and updated["pr"] == 42

    # unknown lifecycle values are rejected
    result = await _call("agents", "set_item_status",
                         {"item_id": "PAY-101", "status": "shipped-ish"})
    assert result.isError

    # writes are agents-role only
    result = await _call("monitor", "set_item_status",
                         {"item_id": "PAY-101", "status": "pending"})
    assert result.isError
