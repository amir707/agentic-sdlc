#!/usr/bin/env python3
"""Human-readable snapshot of the delivery store (read-only).

Layout: the CURRENT story first — the sprint's items with their PR and
lifecycle status (both owned by the STORE: backlog_items.status/pr, set
by the orchestrator at every transition — GitHub is only the artifact),
live workers, open incidents, and this sprint's token spend. Below the
divider: history, every line carrying local time and PR/item ids.

Usage: make status   (or: make watch for a self-refreshing view)
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import db                      # noqa: E402
from orchestrator.activity import read_board, read_recent_history  # noqa: E402

_LABELS = {
    "pending": "not started",
    "in_review": "in review",
    "verified": "verified (labels applied)",
    "preprod_passed": "preprod passed",
    "awaiting_approval": "awaiting gate decision (/approve on the PR)",
    "queued": "approved — queued for release",
    "released": "MERGED + released",
    "rejected": "rejected",
    "escalated": "escalated to a human",
    "failed": "failed preprod",
}


def section(title: str) -> None:
    print(f"\n== {title} ==")


def _elapsed(seconds: float) -> str:
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s" \
        if seconds >= 60 else f"{seconds:.0f}s"


def _local(ts) -> str:
    """Store timestamps are UTC ISO (or epoch floats); render local."""
    from datetime import datetime
    if ts is None:
        return "-"
    if isinstance(ts, (int, float)):
        moment = datetime.fromtimestamp(ts).astimezone()
    else:
        moment = datetime.fromisoformat(ts).astimezone()
    return moment.strftime("%H:%M:%S %Z")


def _item_line(row: dict, board: dict | None) -> str:
    current = (board or {}).get("current", {})
    # A terminal store status outranks a leftover NOW entry (a crashed
    # or path-skipping run can leave the board stale; the store cannot).
    if row["status"] in ("released", "rejected") and row["id"] in current:
        current = {}
    if row["id"] in current:
        entry = current[row["id"]]
        busy = _elapsed(time.time() - entry["since"])
        status = f"NOW {entry['step']} ({busy}) — {entry['detail']}"
    else:
        status = _LABELS.get(row["status"], row["status"])
    pr_label = f"PR #{row['pr']}" if row["pr"] else "—"
    return (f"  {row['id']:<9} {pr_label:<7} "
            f"[{row['implementation']:<5}] {status}")


def main() -> None:
    conn = db.connect()
    db.init_schema(conn)
    board = read_board()
    sprint = db.current_sprint(conn)
    backlog = {r["id"]: dict(r) for r in
               conn.execute("SELECT * FROM backlog_items")}
    pr_to_item = {r["pr"]: r["id"] for r in backlog.values() if r["pr"]}

    # ---------------- CURRENT ----------------
    if sprint:
        section(f"SPRINT #{sprint['id']} — latest status")
        for item_id in sprint["item_ids"]:
            if item_id in backlog:
                print(_item_line(backlog[item_id], board))
        print(f"  rationale: {sprint['rationale'][:110]}")
    else:
        section("SPRINT — none yet")
        print("  run the orchestrator to assess + pack")

    # Anything busy that is not a sprint item (e.g. RELEASE).
    extra = {k: v for k, v in ((board or {}).get("current") or {}).items()
             if not sprint or k not in sprint["item_ids"]}
    if extra:
        section("also active now")
        for key, entry in sorted(extra.items()):
            busy = _elapsed(time.time() - entry["since"])
            print(f"  {key:<9} {entry['step']:<16} {busy:>7}  {entry['detail']}")

    section("open incidents")
    open_incidents = conn.execute(
        "SELECT * FROM incidents WHERE status='open' ORDER BY id").fetchall()
    for i in open_incidents:
        print(f"  #{i['id']} {i['area']:<9} error_rate={i['error_rate']} "
              f"opened={_local(i['opened_at'])}")
    if not open_incidents:
        print("  none")

    section(f"token usage this sprint"
            f"{f' (#{sprint['id']})' if sprint else ''}")
    current_usage = db.summarize_token_usage(
        conn, sprint["id"] if sprint else None)
    for u in current_usage:
        print(f"  {u['agent']:<16} {u['model']:<28} "
              f"in={u['input_tokens']} out={u['output_tokens']} "
              f"calls={u['calls']}")
    if not current_usage:
        print("  none yet")

    # ---------------- HISTORY ----------------
    print("\n" + "-" * 66 + " history")

    history = read_recent_history(limit=10)
    if history:
        section("recently completed steps (newest first)")
        for h in reversed(history):
            print(f"  {_local(h['ended']):<13} {h['item']:<9} "
                  f"{h['step']:<16} {_elapsed(h['seconds']):>7}  "
                  f"-> {h['outcome']}")

    section("resolved incidents (newest first)")
    resolved = conn.execute(
        "SELECT * FROM incidents WHERE status='resolved' "
        "ORDER BY id DESC").fetchall()
    for i in resolved:
        print(f"  #{i['id']} {i['area']:<9} opened={_local(i['opened_at'])} "
              f"resolved={_local(i['resolved_at'])}")
    if not resolved:
        print("  none")

    section("deploys (newest first)")
    for d in conn.execute("SELECT * FROM deploys ORDER BY id DESC"):
        item = pr_to_item.get(d["pr"], "?")
        print(f"  {_local(d['ts']):<13} {item:<9} PR #{d['pr']:<4} "
              f"{d['revision']:<10} traffic={d['traffic']:<8} "
              f"area={d['area'] or '?'}")

    section("audit tail (last 12, newest first)")
    audit = db.list_audit(conn)
    for e in reversed(audit[-12:]):
        pr = e["factors"].get("pr")
        ref = f"PR#{pr}" if pr else e["factors"].get("item", "")
        print(f"  {_local(e['ts']):<13} #{e['id']:>3} {e['actor']:<16} "
              f"{e['decision']:<26} {ref:<8} "
              f"{json.dumps(e['factors'])[:52]}")

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

    section("token usage (all-time, across every run)")
    for u in db.summarize_token_usage(conn):
        print(f"  {u['agent']:<16} {u['model']:<28} "
              f"in={u['input_tokens']} out={u['output_tokens']} "
              f"calls={u['calls']}")


if __name__ == "__main__":
    main()
