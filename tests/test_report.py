"""mcp_server/report.render: the operator report, rendered store-side
from a real (temporary) SQLite file — the same text /status serves and
scripts/store_status.py colourizes."""

import sqlite3

import pytest

from mcp_server import db, report
from mcp_server.vocab import Actor, Decision, ItemStatus


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DELIVERY_STORE_DB", str(tmp_path / "s.sqlite3"))
    c = db.connect()
    db.init_schema(c)
    c.execute("INSERT INTO backlog_items VALUES "
              "('PAY-101','Refunds','d','story','agent','low','x','payments',1,?,7)",
              (ItemStatus.QUEUED,))
    c.execute("INSERT INTO backlog_items VALUES "
              "('CAT-201','Count','d','story','agent','low','x','catalog',2,?,NULL)",
              (ItemStatus.PENDING,))
    c.commit()
    db.create_sprint(c, ["PAY-101", "CAT-201"], "because")
    db.append_audit(c, Actor.RELEASE_MANAGER, Decision.HOLD_MERGE, {"pr": 7, "rule": "incident"})
    db.record_deploy(c, 7, "pr-7", "preprod", "payments")
    return c


def test_render_covers_every_section_from_store_data_alone(conn):
    text = report.render(conn)
    for section in ("SPRINT #1", "open incidents", "token usage this sprint",
                    "resolved incidents", "deploys", "audit tail",
                    "backlog vs assessments", "token usage (all-time"):
        assert section in text, section
    assert "PAY-101   PR #7   [agent] approved — queued for release" in text
    assert "CAT-201   —       [agent] not started" in text
    assert "hold_merge" in text and "PR#7" in text
    assert "traffic=preprod" in text


def test_board_and_history_are_inputs_not_imports(conn):
    """The engine's activity view is passed in; the store module never
    imports orchestrator (it stays standalone)."""
    import ast, pathlib
    src = pathlib.Path(report.__file__).read_text()
    imported = {n.module for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(m.startswith("orchestrator") for m in imported)

    board = {"current": {"CAT-201": {"step": "coder", "since": 0, "detail": "implementing"}}}
    text = report.render(conn, board=board, history=[
        {"ended": "2026-08-17T00:00:00+00:00", "item": "PAY-101", "step": "verify",
         "seconds": 3, "outcome": "labeled"}])
    assert "NOW coder" in text and "implementing" in text
    assert "recently completed steps" in text and "-> labeled" in text
    # a terminal store status outranks a stale NOW entry
    conn.execute("UPDATE backlog_items SET status=? WHERE id='CAT-201'", (ItemStatus.RELEASED,))
    conn.commit()
    assert "NOW coder" not in report.render(conn, board=board)
