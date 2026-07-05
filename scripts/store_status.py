#!/usr/bin/env python3
"""Human-readable snapshot of the delivery store (read-only).

Layout: the CURRENT story first — the sprint's items with their PR and
latest status (derived from the audit trail: the item<->PR mapping and
every stage outcome live there), live workers, open incidents, and this
sprint's token spend. Below the divider: history, every line carrying
local time and PR/item ids.

Usage: make status   (or: make watch for a self-refreshing view)
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import db                      # noqa: E402
from orchestrator.activity import read_board, read_recent_history  # noqa: E402


def section(title: str) -> None:
    print(f"\n== {title} ==")


def _elapsed(seconds: float) -> str:
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s" \
        if seconds >= 60 else f"{seconds:.0f}s"


def _local(ts) -> str:
    """Store timestamps are UTC ISO (or epoch floats); render local."""
    from datetime import datetime, timezone
    if ts is None:
        return "-"
    if isinstance(ts, (int, float)):
        moment = datetime.fromtimestamp(ts).astimezone()
    else:
        moment = datetime.fromisoformat(ts).astimezone()
    return moment.strftime("%H:%M:%S %Z")


def _pr_of(entry: dict):
    return entry["factors"].get("pr")


def _item_pr_map(audit: list[dict]) -> dict[str, int]:
    """item -> PR from open_pr / resume_pr / human_pr audit events."""
    mapping: dict[str, int] = {}
    for entry in audit:
        if entry["decision"] in ("open_pr", "resume_pr", "human_pr"):
            mapping[entry["factors"]["item"]] = entry["factors"]["pr"]
    return mapping


def _github_fallback(items: list[dict]) -> dict[str, int]:
    """PRs opened before the audit mapping existed: resolve them once
    via the deterministic branch name. Needs GITHUB_TOKEN in env (make
    exports it); silently skipped when unavailable."""
    import os

    token = os.environ.get("GITHUB_TOKEN")
    if not token or not items:
        return {}
    try:
        from adapters.repo_host import GitHubRepoHost
        from orchestrator.driver import _branch

        repo_line = next(
            line for line in (Path(__file__).resolve().parent.parent /
                              "projects-config" / "candidate-app" /
                              "project.yaml").read_text().splitlines()
            if line.startswith("repo:"))
        host = GitHubRepoHost(repo_line.split(":", 1)[1].strip(), token)
        mapping = {}
        for item in items:
            found = host.find_pr(_branch(item), state="all")
            if found:
                mapping[item["id"]] = found["number"]
        return mapping
    except Exception:
        return {}  # a status view must never crash on a lookup


# Newest matching audit decision wins; order = storyline precedence.
_STATUS_BY_DECISION = [
    ("merge_pr", "MERGED + released"),
    ("hold_merge", "HELD by release manager"),
    ("reject_pr", None),  # reason filled from factors
    ("human_approve", "approved — queued for release"),
    ("human_hold", "gate: on hold"),
    ("post_dossier", "awaiting gate decision"),
    ("preprod_result", None),  # passed/failed from factors
    ("escalate_to_human", "escalated to a human"),
    ("approve_review", "review approved"),
    ("escalate_risk_label", "risk escalated by verify"),
    ("open_pr", "in review"),
    ("resume_pr", "in review (resumed)"),
    ("human_pr", "in review (human PR)"),
]


def _item_status(item_id: str, pr: int | None, audit: list[dict],
                 board: dict | None) -> str:
    # A live worker beats any recorded state.
    current = (board or {}).get("current", {})
    if item_id in current:
        entry = current[item_id]
        busy = _elapsed(time.time() - entry["since"])
        return f"NOW {entry['step']} ({busy}) — {entry['detail']}"
    if pr is None:
        return "not started"
    for entry in reversed(audit):
        if _pr_of(entry) != pr:
            continue
        for decision, label in _STATUS_BY_DECISION:
            if entry["decision"] == decision:
                if decision == "reject_pr":
                    return (f"rejected ({entry['factors'].get('reason_code')}"
                            f" -> {entry['factors'].get('return_to')})")
                if decision == "preprod_result":
                    return "preprod passed" if entry["factors"].get("passed") \
                        else "preprod FAILED"
                return label
    return "in review"


def main() -> None:
    conn = db.connect()
    db.init_schema(conn)
    audit = db.list_audit(conn)
    board = read_board()
    sprint = db.current_sprint(conn)
    pr_map = _item_pr_map(audit)

    # ---------------- CURRENT ----------------
    if sprint:
        section(f"SPRINT #{sprint['id']} — latest status")
        backlog = {r["id"]: dict(r) for r in
                   conn.execute("SELECT * FROM backlog_items")}
        unmapped = [backlog[i] for i in sprint["item_ids"]
                    if i in backlog and i not in pr_map
                    and backlog[i]["implementation"] == "agent"]
        pr_map.update(_github_fallback(unmapped))
        for item_id in sprint["item_ids"]:
            pr = pr_map.get(item_id)
            pr_label = f"PR #{pr}" if pr else "—"
            status = _item_status(item_id, pr, audit, board)
            impl = backlog.get(item_id, {}).get("implementation", "?")
            print(f"  {item_id:<9} {pr_label:<7} [{impl:<5}] {status}")
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
        section("recently completed steps")
        for h in history:
            print(f"  {_local(h['ended']):<13} {h['item']:<9} "
                  f"{h['step']:<16} {_elapsed(h['seconds']):>7}  "
                  f"-> {h['outcome']}")

    section("resolved incidents")
    resolved = conn.execute(
        "SELECT * FROM incidents WHERE status='resolved' ORDER BY id").fetchall()
    for i in resolved:
        print(f"  #{i['id']} {i['area']:<9} opened={_local(i['opened_at'])} "
              f"resolved={_local(i['resolved_at'])}")
    if not resolved:
        print("  none")

    section("deploys")
    pr_to_item = {pr: item for item, pr in pr_map.items()}
    for d in conn.execute("SELECT * FROM deploys ORDER BY id"):
        item = pr_to_item.get(d["pr"], "?")
        print(f"  {_local(d['ts']):<13} {item:<9} PR #{d['pr']:<4} "
              f"{d['revision']:<10} traffic={d['traffic']}")

    section("audit tail (last 12)")
    for e in audit[-12:]:
        ref = f"PR#{_pr_of(e)}" if _pr_of(e) else \
            e["factors"].get("item", "")
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
