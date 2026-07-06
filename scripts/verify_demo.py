#!/usr/bin/env python3
"""Deterministic demo eval: the audit trail IS the assertion surface.

Run a full rehearsal of the demo, then run this. It reads the delivery
store and asserts the expected decisions and reason codes were actually
recorded — so compliance evidence and test oracle are the same table,
and the demo cannot surprise on camera.

Two tiers:
- CORE: the guardrail events the demo's story depends on. These must
  fire regardless of exactly how the LLM assessor rated each item.
- STRICT (skippable with --lenient): the exact seeded sprint
  composition and refusal reasons. If assessor drift changes these,
  that is precisely what a rehearsal should catch before recording.

Usage: python scripts/verify_demo.py [--lenient] [--db delivery_store.sqlite3]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import db  # noqa: E402


class Checks:
    def __init__(self):
        self.results: list[tuple[bool, str]] = []

    def expect(self, ok: bool, label: str) -> None:
        self.results.append((bool(ok), label))
        print(f"  {'✅' if ok else '❌'} {label}", flush=True)

    @property
    def failed(self) -> int:
        return sum(1 for ok, _ in self.results if not ok)


def _audit(conn) -> list[dict]:
    return db.list_audit(conn)


def _by_decision(audit: list[dict], decision: str) -> list[dict]:
    return [e for e in audit if e["decision"] == decision]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lenient", action="store_true",
                        help="skip STRICT sprint-composition checks")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    if args.db:
        import os
        os.environ["DELIVERY_STORE_DB"] = args.db

    conn = db.connect()
    db.init_schema(conn)  # an un-run store fails checks, not the script
    audit = _audit(conn)
    checks = Checks()

    print("\n== CORE: planning ==")
    sprints = _by_decision(audit, "create_sprint")
    checks.expect(len(sprints) == 1, "exactly one sprint was created")
    refusals = _by_decision(audit, "refuse_item")
    checks.expect(len(refusals) >= 2,
                  f"packer refused at least two items ({len(refusals)} refusals)")
    checks.expect(
        any(r["factors"]["constraint"] == "recommend_split" for r in refusals),
        "at least one refusal cites recommend_split (split before sprinting)")
    checks.expect(
        any(r["factors"]["constraint"] == "risk_budget" for r in refusals),
        "at least one refusal cites the risk-points budget")

    print("\n== CORE: claimed-vs-actual guardrail (the PAY-102 trap) ==")
    escalations = _by_decision(audit, "escalate_risk_label")
    checks.expect(
        any(e["factors"].get("claimed_risk") == "low"
            and e["factors"].get("verified_risk") == "medium"
            for e in escalations),
        "verify escalated a claimed-low change to medium")
    flag_rejections = [e for e in _by_decision(audit, "reject_pr")
                       if e["factors"].get("reason_code") == "policy_flag_required"]
    checks.expect(len(flag_rejections) >= 1,
                  "policy_flag_required rejection fired (flag invariant)")

    print("\n== CORE: review and gate ==")
    checks.expect(len(_by_decision(audit, "approve_review")) >= 2,
                  "reviewer approved at least two PRs")
    checks.expect(len(_by_decision(audit, "post_dossier")) >= 2,
                  "approver posted at least two dossiers")
    checks.expect(len(_by_decision(audit, "human_approve")) >= 2,
                  "human approved at least two PRs at the gate")

    print("\n== CORE: incident-aware release ==")
    incidents = conn.execute("SELECT * FROM incidents").fetchall()
    payments = [dict(i) for i in incidents if i["area"] == "payments"]
    checks.expect(len(payments) >= 1, "a payments incident was opened")
    checks.expect(any(i["status"] == "resolved" for i in payments),
                  "the payments incident was resolved (hysteresis closure)")
    checks.expect(len(_by_decision(audit, "resolve_incident")) >= 1,
                  "the resolution was audited with recovery factors")
    holds = _by_decision(audit, "hold_merge")
    checks.expect(
        any("incident" in json.dumps(h["factors"]).lower() for h in holds),
        "release manager held a merge citing incident state")
    merges = _by_decision(audit, "merge_pr")
    checks.expect(len(merges) >= 2,
                  f"release manager merged at least two PRs ({len(merges)} merges)")

    print("\n== CORE: capacity accounting ==")
    usage = conn.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens+output_tokens),0)"
        " AS tokens FROM token_usage").fetchone()
    checks.expect(usage["calls"] >= 5,
                  f"token usage metered per invocation ({usage['calls']} calls, "
                  f"{usage['tokens']} tokens)")

    if not args.lenient:
        print("\n== STRICT: seeded sprint composition ==")
        if sprints:
            items = sprints[0]["factors"].get("items", [])
            checks.expect(
                items == ["PAY-101", "CAT-201", "PAY-102", "CAT-202"],
                f"sprint is exactly the seeded plan (got {items})")
        expected_refusals = {"CORE-301": "recommend_split",
                            "PAY-103": "risk_budget"}
        actual = {r["factors"]["item"]: r["factors"]["constraint"]
                  for r in refusals}
        for item, constraint in expected_refusals.items():
            checks.expect(actual.get(item) == constraint,
                          f"{item} refused on {constraint} "
                          f"(got {actual.get(item)})")

    total = len(checks.results)
    print(f"\n{'PASS' if checks.failed == 0 else 'FAIL'}: "
          f"{total - checks.failed}/{total} checks passed")
    if checks.failed and not args.lenient:
        print("hint: if only STRICT checks failed, the assessor rated items "
              "differently this run — re-seed and re-run, or use --lenient")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
