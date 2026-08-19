"""mcp_server/vocab: the store's lifecycle words, usable everywhere.

Statuses must survive the trip engine -> MCP (JSON) -> SQLite -> JSON
-> engine as plain text, and the server must accept exactly the enum
set — no more literal tuples that drift from the enum.
"""

import json
import sqlite3

from mcp_server.vocab import STATUS_LABELS, ItemStatus


def test_status_is_a_plain_string_on_the_wire_and_in_sqlite():
    assert json.dumps({"s": ItemStatus.QUEUED}) == '{"s": "queued"}'
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (s TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (ItemStatus.ESCALATED,))
    assert conn.execute("SELECT s FROM t").fetchone()[0] == "escalated"
    assert ItemStatus("escalated") is ItemStatus.ESCALATED  # reads back


def test_every_status_has_an_operator_label():
    assert set(STATUS_LABELS) == set(ItemStatus)


def test_terminal_and_parked_partition_the_end_states():
    assert {s for s in ItemStatus if s.is_terminal} == {
        ItemStatus.RELEASED, ItemStatus.REJECTED}
    assert {s for s in ItemStatus if s.is_parked} == {
        ItemStatus.ESCALATED, ItemStatus.FAILED}


def test_server_accepts_exactly_the_enum_set():
    """set_item_status validates against the enum, not a hand-kept
    tuple: an unknown status is rejected, every member is accepted."""
    from mcp_server import server
    import pytest
    server._caller_role.set("agents")

    with pytest.raises(ValueError):
        server.set_item_status("PAY-101", "shipped")  # not a status


def test_no_audit_decision_is_spelled_outside_the_enum():
    """Every audit emission in the engine and store names its decision
    via Decision.*; a bare literal is a rename waiting to break the
    oracle. (Scans call sites: ctx.audit(...), append_audit(...),
    decision=...)."""
    import re
    from mcp_server.vocab import Decision
    from sdlc.engine.paths import REPO_ROOT as root
    literal = re.compile(
        r'(?:\.audit\(\s*[^,]+,\s*|append_audit\([^,]+,\s*[^,]+,\s*|decision=)"([a-z_]+)"')
    offenders = []
    for pkg in ("sdlc", "mcp_server", "scripts"):
        for f in (root / pkg).rglob("*.py"):
            for m in literal.finditer(f.read_text()):
                offenders.append(f"{f.relative_to(root)}: {m.group(1)}")
    assert not offenders, offenders
    # and every enum member is used somewhere (no dead vocabulary)
    src = "".join(f.read_text() for pkg in ("sdlc", "mcp_server", "scripts")
                  for f in (root / pkg).rglob("*.py"))
    unused = [d.name for d in Decision
              if f"Decision.{d.name}" not in src and f"AuditDecision.{d.name}" not in src
              and d not in (Decision.HUMAN_APPROVE, Decision.HUMAN_REJECT, Decision.HUMAN_HOLD)]  # built via Decision.human()
    assert not unused, unused
