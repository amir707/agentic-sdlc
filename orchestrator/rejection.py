"""The unified PR rejection mechanism (design invariant 4).

One edge, used by every stage that can bounce a PR; the reasons are
data. New rejection reasons are new codes plus policy entries — never
new mechanisms.

Launch reason codes:
- policy_flag_required  (verify, return_to=coder; PR stays open for the
  bounded flag-fix iteration)
- code_unparseable      (reviewer/verify, return_to=coder; the code does
  not even parse, so nothing downstream can measure it)
- out_of_scope          (reviewer, return_to=author)
- human_declined        (approval gate, return_to=backlog; PR closed)
"""

from dataclasses import dataclass


@dataclass
class Rejection:
    pr: int
    reason_code: str
    return_to: str          # coder | author | backlog
    reasoning: str


async def reject(store, repo_host, rejection: Rejection, actor: str) -> None:
    """Post the reasoning on the PR, audit it, and close the PR only
    when the item returns to the backlog (other returns continue on the
    same PR)."""
    repo_host.post_comment(rejection.pr, (
        f"**Rejected — `{rejection.reason_code}`** "
        f"(returned to {rejection.return_to})\n\n{rejection.reasoning}"))
    await store.call("append_audit", actor=actor, decision="reject_pr",
                     factors={
                         "pr": rejection.pr,
                         "reason_code": rejection.reason_code,
                         "return_to": rejection.return_to,
                         "reasoning": rejection.reasoning,
                     })
    if rejection.return_to == "backlog":
        repo_host.close_pr(rejection.pr)
