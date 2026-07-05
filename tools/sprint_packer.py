"""Sprint packer (deterministic solver, NOT an agent).

Greedy selection in the PM's priority order, subject to three budgets
from the sprint-packer policy:

- risk points (risk_points cost per assessed risk, risk_budget cap),
- token budget (assessor's estimate; human-implemented items cost ZERO
  tokens — humans and agents draw from different capacity pools but
  the same risk pool),
- reviewer capacity (reviewers x prs_per_reviewer review slots; every
  item consumes one, human or agent).

Items the assessor recommended splitting are excluded outright
("split before sprinting" — splitting is cheap pre-code). Every refusal
names the constraint that refused it; the refusal list is a first-class
output, not a log line.
"""

from dataclasses import dataclass


@dataclass
class Refusal:
    item_id: str
    constraint: str  # recommend_split | risk_budget | token_budget | reviewer_capacity
    detail: str


@dataclass
class PackResult:
    selected: list[dict]
    refused: list[Refusal]
    rationale: str


def pack(items: list[dict], assessments: dict[str, dict],
         policy: dict) -> PackResult:
    """items: backlog rows (ordered by priority_rank).
    assessments: item_id -> latest assessment row.
    policy: resolved sprint-packer policy.
    """
    risk_points: dict[str, int] = policy["risk_points"]
    risk_left = int(policy["risk_budget"])
    tokens_left = int(policy["token_budget"])
    capacity = policy["reviewer_capacity"]
    slots_left = int(capacity["reviewers"]) * int(capacity["prs_per_reviewer"])

    selected: list[dict] = []
    refused: list[Refusal] = []

    for item in sorted(items, key=lambda i: i["priority_rank"]):
        assessment = assessments.get(item["id"])
        if assessment is None:
            refused.append(Refusal(item["id"], "unassessed",
                                   "no assessment recorded"))
            continue

        if assessment.get("recommend_split"):
            refused.append(Refusal(
                item["id"], "recommend_split",
                f"split before sprinting: {assessment.get('split_reason') or 'assessor recommendation'}"))
            continue

        cost = int(risk_points[assessment["risk"]])
        if cost > risk_left:
            refused.append(Refusal(
                item["id"], "risk_budget",
                f"needs {cost} risk point(s), {risk_left} remaining"))
            continue

        tokens = 0 if item["implementation"] == "human" \
            else int(assessment["token_estimate"])
        if tokens > tokens_left:
            refused.append(Refusal(
                item["id"], "token_budget",
                f"needs ~{tokens} tokens, {tokens_left} remaining"))
            continue

        if slots_left < 1:
            refused.append(Refusal(
                item["id"], "reviewer_capacity", "no review slots remaining"))
            continue

        risk_left -= cost
        tokens_left -= tokens
        slots_left -= 1
        selected.append(item)

    rationale = (
        f"packed {len(selected)} of {len(items)} items in priority order; "
        f"remaining: {risk_left} risk point(s), {tokens_left} tokens, "
        f"{slots_left} review slot(s); refused: "
        + (", ".join(f"{r.item_id} ({r.constraint})" for r in refused) or "none")
    )
    return PackResult(selected=selected, refused=refused, rationale=rationale)
