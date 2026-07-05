#!/usr/bin/env python3
"""Human-readable snapshot of the delivery store (read-only).

The store is a single SQLite file precisely so it can be inspected with
any client; this is the curated view: assessments beside their claims,
the current sprint, incidents, deploys, and the audit tail.

Usage: make status   (or: .venv/bin/python scripts/store_status.py)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import db  # noqa: E402


def section(title: str) -> None:
    print(f"\n== {title} ==")


def _elapsed(seconds: float) -> str:
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s" \
        if seconds >= 60 else f"{seconds:.0f}s"


def _local(ts: str | None) -> str:
    """Store timestamps are UTC ISO; render in the machine's timezone."""
    if not ts:
        return "-"
    from datetime import datetime
    local = datetime.fromisoformat(ts).astimezone()
    return local.strftime("%H:%M:%S %Z")


def _render_activity() -> None:
    """Live 'who is doing what, since when' from the activity board."""
    import time

    from orchestrator.activity import read_board, read_recent_history

    board = read_board()
    section("NOW: active workers (live)")
    if not board or not board.get("current"):
        age = time.time() - board["updated"] if board else None
        print("  idle" + (f" (board updated {_elapsed(age)} ago)"
                          if age is not None else " (no run yet)"))
    else:
        stale = time.time() - board["updated"] > 300
        for item, entry in sorted(board["current"].items()):
            busy = _elapsed(time.time() - entry["since"])
            print(f"  {item:<9} {entry['step']:<16} {busy:>7}  "
                  f"{entry['detail']}")
        if stale:
            print("  (board >5min stale — the run may have crashed)")

    history = read_recent_history()
    if history:
        section("recently completed steps")
        for h in history:
            print(f"  {h['item']:<9} {h['step']:<16} "
                  f"{_elapsed(h['seconds']):>7}  -> {h['outcome']}")


def main() -> None:
    conn = db.connect()
    db.init_schema(conn)

    _render_activity()

    section("backlog vs assessments (claimed -> assessed)")
    rows = conn.execute("""
        SELECT b.id, b.claimed_risk, b.implementation, b.priority_rank,
               a.risk, a.effort, a.token_estimate, a.recommend_split
        FROM backlog_items b
        LEFT JOIN (SELECT x.* FROM assessments x
                   JOIN (SELECT item_id, MAX(ts) ts FROM assessments
                         GROUP BY item_id) m
                     ON x.item_id = m.item_id AND x.ts = m.ts) a
          ON a.item_id = b.id
        ORDER BY b.priority_rank""").fetchall()
    for r in rows:
        assessed = (f"{r['risk']:<6} effort={r['effort']} "
                    f"~{r['token_estimate']} tok"
                    + ("  SPLIT!" if r["recommend_split"] else "")
                    ) if r["risk"] else "— not assessed yet"
        print(f"  {r['id']:<9} claimed={r['claimed_risk']:<6} "
              f"[{r['implementation']:<5}]  ->  {assessed}")

    section("current sprint")
    sprint = db.current_sprint(conn)
    if sprint:
        print(f"  #{sprint['id']} items={sprint['item_ids']}")
        print(f"  rationale: {sprint['rationale']}")
    else:
        print("  none yet")

    section("incidents")
    for i in conn.execute("SELECT * FROM incidents ORDER BY id"):
        resolved = f" resolved={_local(i['resolved_at'])}" \
            if i["resolved_at"] else ""
        print(f"  #{i['id']} {i['area']:<9} {i['status']:<9} "
              f"error_rate={i['error_rate']} "
              f"opened={_local(i['opened_at'])}{resolved}")
    section("deploys")
    for d in conn.execute("SELECT * FROM deploys ORDER BY id"):
        print(f"  pr#{d['pr']} {d['revision']:<10} traffic={d['traffic']:<8} "
              f"{_local(d['ts'])}")

    sprint_id = sprint["id"] if sprint else None
    section(f"token usage since sprint #{sprint_id} (current run)"
            if sprint_id else "token usage (current run)")
    current = db.summarize_token_usage(conn, sprint_id)
    for u in current:
        print(f"  {u['agent']:<16} {u['model']:<28} "
              f"in={u['input_tokens']} out={u['output_tokens']} "
              f"calls={u['calls']}")
    if not current:
        print("  none yet")
    section("token usage (all-time, across every run)")
    for u in db.summarize_token_usage(conn):
        print(f"  {u['agent']:<16} {u['model']:<28} "
              f"in={u['input_tokens']} out={u['output_tokens']} "
              f"calls={u['calls']}")

    section("audit tail (last 10)")
    for e in db.list_audit(conn)[-10:]:
        print(f"  #{e['id']:>3} {e['actor']:<16} {e['decision']:<26} "
              f"{json.dumps(e['factors'])[:70]}")


if __name__ == "__main__":
    main()
