"""The store's operator report, rendered on the store side.

`render(conn)` is what the server's /status route serves and what
scripts/store_status.py prints locally (colourized). It reads the
SQLite file directly — this module IS the store, so that is not a
bypass — and takes the engine's activity board as an INPUT rather than
importing it: mcp_server stays standalone (imports nothing from the
engine), and the board lives on the engine's disk anyway.
"""

import json
import time
from datetime import datetime, timezone

from mcp_server import db
from mcp_server.vocab import STATUS_LABELS, ItemStatus

_LABELS = STATUS_LABELS


def _elapsed(seconds: float) -> str:
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s" \
        if seconds >= 60 else f"{seconds:.0f}s"


def _local(ts) -> str:
    """Store timestamps are UTC ISO (or epoch floats); render local."""
    if ts is None:
        return "-"
    if isinstance(ts, (int, float)):
        moment = datetime.fromtimestamp(ts).astimezone()
    else:
        moment = datetime.fromisoformat(ts).astimezone()
    return moment.strftime("%H:%M:%S %Z")


def _when(ts) -> str:
    """Absolute local time for the record, relative age for the human."""
    if ts is None:
        return "-"
    return f"{_local(ts)} ({_ago(ts)})"


def _ago(ts) -> str:
    if isinstance(ts, (int, float)):
        moment = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        moment = datetime.fromisoformat(ts)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    seconds = max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


_FACTOR_PRIORITY = ("reason_code", "rule", "reason", "reasoning")


def _fmt_factors(factors: dict, width: int = 60) -> str:
    """Audit factors as key=value, the WHY first, ids already on the
    line (pr/item) dropped — instead of truncated raw JSON."""
    shown = {k: v for k, v in factors.items()
             if k not in ("pr", "item") and v not in (None, "")}
    keys = ([k for k in _FACTOR_PRIORITY if k in shown]
            + [k for k in shown if k not in _FACTOR_PRIORITY])
    parts = []
    for key in keys:
        value = shown[key]
        if isinstance(value, bool):
            value = "yes" if value else "no"
        elif not isinstance(value, str):
            value = json.dumps(value)
        parts.append(f"{key}={value}")
    line = "  ".join(parts)
    return line if len(line) <= width else line[:width - 1] + "…"


def _item_line(row: dict, board: dict | None) -> str:
    current = (board or {}).get("current", {})
    # A terminal store status outranks a leftover NOW entry (a crashed
    # or path-skipping run can leave the board stale; the store cannot).
    if ItemStatus(row["status"]).is_terminal and row["id"] in current:
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


def _usage_table(rows) -> list[str]:
    out: list[str] = []
    total_in = total_out = total_calls = 0
    for u in rows:
        out.append(f"  {u['agent']:<16} {u['model']:<28} "
              f"in={u['input_tokens']:>9,} out={u['output_tokens']:>7,} "
              f"calls={u['calls']}")
        total_in += u["input_tokens"]
        total_out += u["output_tokens"]
        total_calls += u["calls"]
    if len(rows) > 1:
        out.append(f"  {'total':<16} {'':<28} "
              f"in={total_in:>9,} out={total_out:>7,} calls={total_calls}")
    return out


def render(conn, *, board: dict | None = None,
           history: list[dict] | tuple = ()) -> str:
    """The whole store as an operator report. `board` / `history` are
    the ENGINE's activity view (sdlc/engine/activity.py), passed in by
    the caller that has them (the local CLI); the store server renders
    without them — the board lives on the engine's disk, not here."""
    out: list[str] = []
    _p = out.append

    def section(title: str) -> None:
        _p(f"\n== {title} ==")

    sprint = db.current_sprint(conn)
    backlog = {r["id"]: dict(r) for r in
               conn.execute("SELECT * FROM backlog_items")}
    pr_to_item = {r["pr"]: r["id"] for r in backlog.values() if r["pr"]}

    # ---------------- CURRENT ----------------
    if sprint:
        section(f"SPRINT #{sprint['id']} — latest status")
        for item_id in sprint["item_ids"]:
            if item_id in backlog:
                _p(_item_line(backlog[item_id], board))
        _p(f"  rationale: {sprint['rationale'][:110]}")
    else:
        section("SPRINT — none yet")
        _p("  run the orchestrator to assess + pack")

    # Anything busy that is not a sprint item (e.g. RELEASE).
    extra = {k: v for k, v in ((board or {}).get("current") or {}).items()
             if not sprint or k not in sprint["item_ids"]}
    if extra:
        section("also active now")
        for key, entry in sorted(extra.items()):
            busy = _elapsed(time.time() - entry["since"])
            _p(f"  {key:<9} {entry['step']:<16} {busy:>7}  {entry['detail']}")

    section("open incidents")
    open_incidents = conn.execute(
        "SELECT * FROM incidents WHERE status='open' ORDER BY id").fetchall()
    for i in open_incidents:
        _p(f"  #{i['id']} {i['area']:<9} error_rate={i['error_rate']} "
              f"opened={_when(i['opened_at'])}")
    if not open_incidents:
        _p("  none")

    section(f"token usage this sprint"
            f"{f' (#{sprint['id']})' if sprint else ''}")
    current_usage = db.summarize_token_usage(
        conn, sprint["id"] if sprint else None)
    out.extend(_usage_table(current_usage))
    if not current_usage:
        _p("  none yet")

    # ---------------- HISTORY ----------------
    _p("\n" + "-" * 66 + " history")

    if history:
        section("recently completed steps (newest first)")
        for h in reversed(history):
            _p(f"  {_when(h['ended']):<24} {h['item']:<9} "
                  f"{h['step']:<16} {_elapsed(h['seconds']):>7}  "
                  f"-> {h['outcome']}")

    section("resolved incidents (newest first)")
    resolved = conn.execute(
        "SELECT * FROM incidents WHERE status='resolved' "
        "ORDER BY id DESC").fetchall()
    for i in resolved:
        _p(f"  #{i['id']} {i['area']:<9} opened={_local(i['opened_at'])} "
              f"resolved={_local(i['resolved_at'])}")
    if not resolved:
        _p("  none")

    section("deploys (newest first)")
    for d in conn.execute("SELECT * FROM deploys ORDER BY id DESC"):
        item = pr_to_item.get(d["pr"], "?")
        _p(f"  {_when(d['ts']):<24} {item:<9} PR #{d['pr']:<4} "
              f"{d['revision']:<10} traffic={d['traffic']:<8} "
              f"area={d['area'] or '?'}")

    section("audit tail (last 12, newest first)")
    audit = db.list_audit(conn)
    for e in reversed(audit[-12:]):
        pr = e["factors"].get("pr")
        ref = f"PR#{pr}" if pr else e["factors"].get("item", "")
        _p(f"  {_when(e['ts']):<24} #{e['id']:>3} {e['actor']:<16} "
              f"{e['decision']:<26} {ref:<8} "
              f"{_fmt_factors(e['factors'])}")

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
        _p(f"  {r['id']:<9} claimed={r['claimed_risk']:<6} "
              f"[{r['implementation']:<5}]  ->  {assessed}")

    section("token usage (all-time, across every run)")
    out.extend(_usage_table(db.summarize_token_usage(conn)))
    return "\n".join(out) + "\n"
